import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import Config
from .models import CellReading
from .storage import SessionStore, new_session_id


class DBusTelemetry:
    def __init__(self, config: Config, bus=None):
        self.config = config
        if bus is None:
            try:
                import dbus
            except ImportError as exc:
                raise RuntimeError("dbus-python is unavailable") from exc
            bus = dbus.SystemBus()
        self.bus = bus
        self._cell_paths = {}
        self._pack_path = None

    def available(self) -> bool:
        try:
            return bool(self.bus.name_has_owner(self.config.dbus_service))
        except Exception:
            return False

    def _value(self, path: str):
        obj = self.bus.get_object(self.config.dbus_service, path)
        try:
            return obj.GetValue(dbus_interface="com.victronenergy.BusItem")
        except TypeError:
            return obj.GetValue()

    def _find_pack(self):
        candidates = [self.config.pack_voltage_path, "/Dc/0/Voltage", "/System/Voltage"]
        if self._pack_path:
            candidates.insert(0, self._pack_path)
        for path in dict.fromkeys(candidates):
            try:
                value = float(self._value(path))
                self._pack_path = path
                return path, value
            except Exception:
                continue
        raise RuntimeError("pack voltage path not found")

    def _find_cell(self, index: int):
        configured = self.config.cell_path_template.format(index=index)
        candidates = [configured, "/Voltages/Cell%d" % index,
                      "/Voltages/Cell%02d" % index, "/Cell/%d/Volts" % index,
                      "/Cells/%d/Voltage" % index]
        if index in self._cell_paths:
            candidates.insert(0, self._cell_paths[index])
        for path in dict.fromkeys(candidates):
            try:
                value = float(self._value(path))
                self._cell_paths[index] = path
                return path, value
            except Exception:
                continue
        raise RuntimeError("cell %d voltage path not found" % index)

    def discover(self) -> Dict:
        result = {"service": self.config.dbus_service, "available": self.available(),
                  "pack_voltage_path": self.config.pack_voltage_path, "cells": []}
        if not result["available"]:
            result["error"] = "service has no owner"
            return result
        try:
            pack_path, pack_value = self._find_pack()
            result["pack_voltage_path"] = pack_path
            result["pack_voltage_v"] = pack_value
        except Exception as exc:
            result["pack_error"] = str(exc)
        for index in range(1, 17):
            entry = {"index": index}
            try:
                path, value = self._find_cell(index)
                entry["path"] = path
                entry["value_v"] = value
                entry["available"] = True
            except Exception as exc:
                entry["path"] = self.config.cell_path_template.format(index=index)
                entry["available"] = False
                entry["error"] = str(exc)
            result["cells"].append(entry)
        return result

    def sample(self) -> List[CellReading]:
        if not self.available():
            raise RuntimeError("D-Bus service unavailable: %s" % self.config.dbus_service)
        wall = time.time()
        mono = time.monotonic()
        _, pack = self._find_pack()
        direct = []
        for index in range(1, 16):
            _, value = self._find_cell(index)
            direct.append(value)
        readings = [CellReading(wall, mono, i + 1, value, False, "dbus", pack)
                    for i, value in enumerate(direct)]
        if self.config.cell16_direct:
            _, value16 = self._find_cell(16)
            reconstructed = False
        else:
            value16 = pack - sum(direct)
            reconstructed = True
        readings.append(CellReading(wall, mono, 16, value16, reconstructed, "dbus", pack))
        return readings


class JSONTelemetry:
    """Read the atomic live snapshot produced by the existing RS485 decoder."""

    def __init__(self, config: Config):
        self.config = config

    def _snapshot(self):
        try:
            snapshot = json.loads(self.config.telemetry_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("RS485 telemetry JSON unavailable: %s" % exc)
        if not snapshot.get("valid") or not snapshot.get("cellTelemetryValid"):
            raise RuntimeError("RS485 telemetry JSON is not valid")
        timestamp = float(snapshot.get("timestamp", 0)) / 1000.0
        age = max(0.0, time.time() - timestamp)
        if timestamp <= 0 or age > self.config.max_telemetry_age_s:
            raise RuntimeError("RS485 telemetry JSON is stale (%.1f s)" % age)
        batteries = [item for item in snapshot.get("batteries", []) if item.get("valid")]
        if not batteries:
            raise RuntimeError("RS485 telemetry JSON has no valid battery")
        battery = batteries[0]
        cells = battery.get("effectiveCells", [])
        if len(cells) != 16:
            raise RuntimeError("RS485 telemetry JSON contains %d effective cells" % len(cells))
        return snapshot, battery, cells, timestamp

    def discover(self) -> Dict:
        result = {"provider": "json", "path": str(self.config.telemetry_json_path),
                  "available": False, "cells": []}
        try:
            snapshot, battery, cells, timestamp = self._snapshot()
            result.update({"available": True, "timestamp": timestamp,
                           "pack_voltage_v": float(battery.get("voltage", snapshot["system"]["voltage61"]))})
            for cell in cells:
                result["cells"].append({"index": int(cell["index"]),
                                        "path": "effectiveCells[%d]" % (int(cell["index"]) - 1),
                                        "value_v": float(cell["voltage"]), "available": True,
                                        "source": cell.get("source", "unknown")})
        except Exception as exc:
            result["error"] = str(exc)
        return result

    def sample(self) -> List[CellReading]:
        snapshot, battery, cells, wall = self._snapshot()
        mono = time.monotonic()
        pack = float(battery.get("voltage", snapshot["system"]["voltage61"]))
        readings = []
        for cell in cells:
            source = str(cell.get("source", "unknown"))
            readings.append(CellReading(wall, mono, int(cell["index"]), float(cell["voltage"]),
                                        source in ("calculated", "reconstructed"),
                                        "json:%s" % source, pack))
        return readings


class LiveTelemetry:
    """Prefer D-Bus cell paths and fall back to the decoder's atomic JSON snapshot."""

    def __init__(self, config: Config, bus=None):
        self.config = config
        self.dbus = DBusTelemetry(config, bus)
        self.json = JSONTelemetry(config)
        self.active = None

    def discover(self) -> Dict:
        dbus_result = self.dbus.discover()
        dbus_ok = dbus_result.get("pack_voltage_v") is not None and all(
            cell.get("available") for cell in dbus_result.get("cells", [])[:15])
        if dbus_ok:
            self.active = self.dbus
            dbus_result["provider"] = "dbus"
            return dbus_result
        json_result = self.json.discover()
        if json_result.get("available"):
            self.active = self.json
            json_result["dbus_fallback_reason"] = dbus_result.get("error", "cell paths unavailable")
            return json_result
        return {"provider": "none", "available": False, "cells": [],
                "error": "D-Bus cells unavailable; %s" % json_result.get("error", "JSON unavailable"),
                "dbus": dbus_result, "json": json_result}

    def sample(self) -> List[CellReading]:
        if self.active is None:
            self.discover()
        if self.active is None:
            raise RuntimeError("no live RS485 telemetry provider")
        return self.active.sample()


def _csv_time(value: str, previous: Optional[float]) -> float:
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        parsed = datetime.strptime(value, "%H:%M:%S")
        seconds = parsed.hour * 3600 + parsed.minute * 60 + parsed.second
        if previous is not None:
            day = int(previous // 86400)
            candidate = day * 86400 + seconds
            if candidate < previous - 12 * 3600:
                candidate += 86400
            return candidate
        return float(seconds)


def read_dyness_csv(path: Path) -> Tuple[Dict[str, str], List[List[CellReading]]]:
    metadata = {}
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        lines = handle.readlines()
    data_lines = []
    for line in lines:
        if line.startswith("#"):
            text = line[1:].strip()
            if "=" in text:
                key, value = text.split("=", 1)
                metadata[key.strip()] = value.strip()
        elif line.strip():
            data_lines.append(line)
    reader = csv.DictReader(data_lines)
    previous = None
    for row in reader:
        if not row.get("timestamp"):
            continue
        wall = _csv_time(row["timestamp"], previous)
        previous = wall
        mono = wall
        pack_text = row.get("battery_02_voltage_v") or row.get("system_voltage_v")
        pack = float(pack_text) if pack_text not in (None, "") else None
        sample = []
        for index in range(1, 17):
            key = "battery_02_cell_%02d_v" % index
            value = row.get(key)
            if value in (None, ""):
                continue
            sample.append(CellReading(wall, mono, index, float(value), index == 16,
                                      "csv", pack))
        if sample:
            rows.append(sample)
    return metadata, rows


def import_dyness_csv(csv_path: Path, sessions_dir: Path, session_id: Optional[str] = None) -> Dict:
    metadata, samples = read_dyness_csv(csv_path)
    session_id = session_id or new_session_id("csv")
    metadata = dict(metadata)
    metadata["input_file"] = str(Path(csv_path).resolve())
    store = SessionStore.create(sessions_dir, session_id, "csv", notes="Imported RS485 telemetry",
                                metadata=metadata)
    for sample in samples:
        store.add_cells(sample)
    store.stop("import_complete")
    info = store.info()
    store.close()
    return info
