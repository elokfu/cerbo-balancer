import json
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import __version__
from .models import CANFrame, CellReading


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY, created_utc TEXT NOT NULL, started_utc TEXT,
  stopped_utc TEXT, status TEXT NOT NULL, source TEXT NOT NULL,
  interface TEXT, notes TEXT NOT NULL DEFAULT '', software_version TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}', stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS frames (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, wall_ts REAL NOT NULL, mono_ts REAL NOT NULL,
  can_id INTEGER NOT NULL, is_extended INTEGER NOT NULL, is_remote INTEGER NOT NULL,
  is_error INTEGER NOT NULL, is_local INTEGER NOT NULL DEFAULT 0,
  dlc INTEGER NOT NULL, data BLOB NOT NULL, interface TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_candidate ON frames(can_id, is_extended, wall_ts);
CREATE TABLE IF NOT EXISTS cell_samples (
  sample_id INTEGER NOT NULL, wall_ts REAL NOT NULL, mono_ts REAL NOT NULL,
  cell_index INTEGER NOT NULL, voltage_v REAL NOT NULL, reconstructed INTEGER NOT NULL,
  source TEXT NOT NULL, pack_voltage_v REAL,
  PRIMARY KEY(sample_id, cell_index)
);
CREATE INDEX IF NOT EXISTS idx_cells_time ON cell_samples(cell_index, wall_ts);
CREATE TABLE IF NOT EXISTS candidates (
  cell_index INTEGER NOT NULL, rank INTEGER NOT NULL, can_id INTEGER,
  is_extended INTEGER, is_local INTEGER, byte_offset INTEGER, endian TEXT, scale_mv REAL,
  sample_count INTEGER NOT NULL, coverage REAL, correlation REAL, rmse_mv REAL,
  median_error_mv REAL, movement_agreement REAL, lag_s REAL, score REAL,
  verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(cell_index, rank)
);
"""


def new_session_id(prefix: str = "capture") -> str:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S") + ("%03dZ" % (now.microsecond // 1000))
    return "%s-%s" % (prefix, stamp)


def safe_session_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("invalid session id")
    return value


class SessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self._sample_id = self._next_sample_id()

    @classmethod
    def create(cls, sessions_dir: Path, session_id: str, source: str,
               interface: Optional[str] = None, notes: str = "",
               metadata: Optional[Dict] = None):
        session_id = safe_session_id(session_id)
        path = Path(sessions_dir) / (session_id + ".sqlite")
        if path.exists():
            raise FileExistsError("session already exists: %s" % session_id)
        store = cls(path)
        now = datetime.now(timezone.utc).isoformat()
        store.db.execute(
            "INSERT INTO session(id,created_utc,started_utc,status,source,interface,notes,software_version,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (session_id, now, now, "recording", source, interface, notes,
             __version__, json.dumps(metadata or {}, sort_keys=True)))
        store.db.commit()
        return store

    def _next_sample_id(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(sample_id),0)+1 AS n FROM cell_samples").fetchone()
        return int(row["n"])

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(frames)")}
        if "is_local" not in columns:
            self.db.execute("ALTER TABLE frames ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0")
        candidate_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(candidates)")}
        if "is_local" not in candidate_columns:
            self.db.execute("ALTER TABLE candidates ADD COLUMN is_local INTEGER")

    def add_frame(self, frame: CANFrame) -> None:
        self.db.execute(
            "INSERT INTO frames(wall_ts,mono_ts,can_id,is_extended,is_remote,is_error,is_local,dlc,data,interface) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (frame.wall_ts, frame.mono_ts, frame.can_id, int(frame.is_extended),
             int(frame.is_remote), int(frame.is_error), int(frame.is_local),
             frame.dlc, frame.data, frame.interface))

    def add_cells(self, readings: Iterable[CellReading]) -> None:
        rows = list(readings)
        if not rows:
            return
        sid = self._sample_id
        self._sample_id += 1
        self.db.executemany(
            "INSERT INTO cell_samples(sample_id,wall_ts,mono_ts,cell_index,voltage_v,reconstructed,source,pack_voltage_v) VALUES(?,?,?,?,?,?,?,?)",
            [(sid, r.wall_ts, r.mono_ts, r.cell_index, r.voltage_v,
              int(r.reconstructed), r.source, r.pack_voltage_v) for r in rows])

    def flush(self) -> None:
        self.db.commit()

    def stop(self, reason: str = "user") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("UPDATE session SET status='stopped', stopped_utc=?, stop_reason=?", (now, reason))
        self.db.commit()

    def info(self) -> Dict:
        row = self.db.execute("SELECT * FROM session LIMIT 1").fetchone()
        result = dict(row) if row else {}
        result["frame_count"] = self.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        result["cell_sample_count"] = self.db.execute("SELECT COUNT(DISTINCT sample_id) FROM cell_samples").fetchone()[0]
        result["size_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return result

    def replace_candidates(self, rows: List[Dict]) -> None:
        self.db.execute("DELETE FROM candidates")
        columns = ["cell_index", "rank", "can_id", "is_extended", "is_local", "byte_offset", "endian",
                   "scale_mv", "sample_count", "coverage", "correlation", "rmse_mv",
                   "median_error_mv", "movement_agreement", "lag_s", "score", "verdict", "detail"]
        self.db.executemany(
            "INSERT INTO candidates(%s) VALUES(%s)" % (",".join(columns), ",".join("?" for _ in columns)),
            [[row.get(c) for c in columns] for row in rows])
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def list_sessions(sessions_dir: Path) -> List[Dict]:
    result = []
    for path in sorted(Path(sessions_dir).glob("*.sqlite"), reverse=True):
        try:
            store = SessionStore(path)
            result.append(store.info())
            store.close()
        except sqlite3.DatabaseError as exc:
            result.append({"id": path.stem, "status": "damaged", "error": str(exc)})
    return result


def recover_incomplete_sessions(sessions_dir: Path) -> int:
    recovered = 0
    now = datetime.now(timezone.utc).isoformat()
    for path in Path(sessions_dir).glob("*.sqlite"):
        try:
            store = SessionStore(path)
            cursor = store.db.execute(
                "UPDATE session SET status='interrupted', stopped_utc=?, stop_reason='service_restart' WHERE status='recording'",
                (now,))
            if cursor.rowcount:
                recovered += cursor.rowcount
            store.db.commit()
            store.close()
        except sqlite3.DatabaseError:
            continue
    return recovered


def storage_ok(data_dir: Path, session_path: Optional[Path], max_session_mb: int,
               min_free_mb: int):
    data_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(data_dir)).free
    if free < min_free_mb * 1024 * 1024:
        return False, "free space is below %d MiB" % min_free_mb
    if session_path and session_path.exists() and session_path.stat().st_size >= max_session_mb * 1024 * 1024:
        return False, "session reached %d MiB limit" % max_session_mb
    return True, "ok"
