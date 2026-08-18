import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

from .canbus import SocketCANReceiver, resolve_interface, socketcan_interfaces
from .config import Config
from .storage import SessionStore, list_sessions, new_session_id, recover_incomplete_sessions, storage_ok
from .telemetry import LiveTelemetry


class CaptureManager:
    def __init__(self, config: Config, receiver_factory=SocketCANReceiver,
                 telemetry_factory=LiveTelemetry):
        self.config = config
        self.receiver_factory = receiver_factory
        self.telemetry_factory = telemetry_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.store = None
        self.receiver = None
        self.telemetry = None
        self.capture_thread = None
        self.telemetry_thread = None
        self.session_id = None
        self.started = None
        self.stop_reason = None
        self.last_error = None
        self.last_cells = []
        self.telemetry_health = {"state": "idle"}
        self.frame_stats = {}
        recover_incomplete_sessions(self.config.sessions_dir)

    def preflight(self) -> Dict:
        result = {"ok": False, "interfaces": socketcan_interfaces(), "storage": {}}
        ok, message = storage_ok(self.config.data_dir, None, self.config.max_session_mb,
                                 self.config.min_free_mb)
        result["storage"] = {"ok": ok, "message": message, "path": str(self.config.data_dir)}
        if not ok:
            result["error"] = message
            return result
        try:
            result["selected_interface"] = resolve_interface(self.config.interface)
        except Exception as exc:
            result["error"] = str(exc)
            return result
        try:
            telemetry = self.telemetry_factory(self.config)
            result["dbus"] = telemetry.discover()
            direct_ok = result["dbus"].get("pack_voltage_v") is not None and all(
                cell.get("available") for cell in result["dbus"].get("cells", [])[:15])
            if not direct_ok:
                result["error"] = "D-Bus pack voltage or cells 1-15 are unavailable"
                return result
        except Exception as exc:
            result["error"] = "D-Bus preflight failed: %s" % exc
            return result
        result["ok"] = True
        return result

    def start(self, notes: str = "") -> Dict:
        with self.lock:
            if self.store is not None:
                raise RuntimeError("capture is already running")
        check = self.preflight()
        if not check["ok"]:
            raise RuntimeError(check.get("error", "preflight failed"))
        interface = check["selected_interface"]
        receiver = self.receiver_factory(interface)
        telemetry = self.telemetry_factory(self.config)
        session_id = new_session_id("capture")
        metadata = {"preflight": check, "safety": "receive-only; no CAN transmit API"}
        store = SessionStore.create(self.config.sessions_dir, session_id, "live",
                                    interface=interface, notes=notes, metadata=metadata)
        with self.lock:
            self.stop_event = threading.Event()
            self.store = store
            self.receiver = receiver
            self.telemetry = telemetry
            self.session_id = session_id
            self.started = time.time()
            self.stop_reason = None
            self.last_error = None
            self.last_cells = []
            self.telemetry_health = {"state": "starting"}
            self.frame_stats = {}
            self.telemetry_thread = threading.Thread(target=self._telemetry_loop, name="dyness-telemetry", daemon=True)
            self.capture_thread = threading.Thread(target=self._capture_loop, name="dyness-can-capture", daemon=True)
            self.telemetry_thread.start()
            self.capture_thread.start()
        return self.status()

    def stop(self, reason: str = "user") -> Dict:
        with self.lock:
            thread = self.capture_thread
            if self.store is None:
                return self.status()
            self.stop_reason = reason
            self.stop_event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        return self.status()

    def _capture_loop(self):
        frames_since_flush = 0
        reason = "capture_ended"
        try:
            while not self.stop_event.is_set():
                frame = self.receiver.receive(1.0)
                if frame is None:
                    continue
                with self.lock:
                    self.store.add_frame(frame)
                    self._update_frame_stats(frame)
                frames_since_flush += 1
                if frames_since_flush >= 100:
                    with self.lock:
                        self.store.flush()
                        path = self.store.path
                    frames_since_flush = 0
                    ok, message = storage_ok(self.config.data_dir, path, self.config.max_session_mb,
                                             self.config.min_free_mb)
                    if not ok:
                        reason = "storage_limit: %s" % message
                        self.stop_reason = reason
                        self.stop_event.set()
        except Exception as exc:
            self.last_error = str(exc)
            reason = "capture_error: %s" % exc
            self.stop_reason = reason
            self.stop_event.set()
        finally:
            self.stop_event.set()
            if self.telemetry_thread and self.telemetry_thread is not threading.current_thread():
                self.telemetry_thread.join(timeout=max(2.0, self.config.telemetry_interval_s + 1.0))
            with self.lock:
                if self.receiver:
                    self.receiver.close()
                if self.store:
                    self.store.stop(self.stop_reason or reason)
                    self.store.close()
                self.store = None
                self.receiver = None
                self.telemetry = None
                self.capture_thread = None
                self.telemetry_thread = None

    def _telemetry_loop(self):
        while not self.stop_event.is_set():
            try:
                readings = self.telemetry.sample()
                with self.lock:
                    if self.store is None:
                        return
                    self.store.add_cells(readings)
                    self.last_cells = [{"index": r.cell_index, "voltage_v": r.voltage_v,
                                        "reconstructed": r.reconstructed, "source": r.source,
                                        "wall_ts": r.wall_ts} for r in readings]
                    self.telemetry_health = {"state": "ok", "last_sample": readings[0].wall_ts,
                                             "cell_count": len(readings)}
            except Exception as exc:
                self.telemetry_health = {"state": "error", "error": str(exc), "last_attempt": time.time()}
            self.stop_event.wait(self.config.telemetry_interval_s)

    def _update_frame_stats(self, frame):
        key = (frame.can_id, frame.is_extended, frame.is_local)
        now = time.time()
        stat = self.frame_stats.get(key)
        previous = stat["data"] if stat else b""
        changed = [i for i, value in enumerate(frame.data)
                   if i >= len(previous) or previous[i] != value]
        if stat is None:
            stat = {"can_id": frame.can_id, "is_extended": frame.is_extended,
                    "count": 0, "first_ts": now}
            self.frame_stats[key] = stat
        stat.update({"count": stat["count"] + 1, "last_ts": now, "dlc": frame.dlc,
                     "data": frame.data, "data_hex": frame.data.hex().upper(),
                     "changed_bytes": changed, "is_remote": frame.is_remote,
                     "is_error": frame.is_error, "is_local": frame.is_local,
                     "direction": "CERBO_TX" if frame.is_local else "BUS_RX"})

    def status(self) -> Dict:
        with self.lock:
            active = self.store is not None
            stats = []
            for value in self.frame_stats.values():
                row = dict(value)
                row.pop("data", None)
                elapsed = max(0.001, row.get("last_ts", time.time()) - row.get("first_ts", time.time()))
                row["rate_hz"] = row["count"] / elapsed
                stats.append(row)
            stats.sort(key=lambda row: (row["can_id"], row["is_extended"], row["is_local"]))
            return {"active": active, "session_id": self.session_id,
                    "started": self.started, "stop_reason": self.stop_reason,
                    "last_error": self.last_error, "telemetry": dict(self.telemetry_health),
                    "cells": list(self.last_cells), "frames": stats}
