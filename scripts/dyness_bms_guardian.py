#!/usr/bin/env python3
"""Persistent Victron D-Bus guardian for the Dyness RS485 telemetry worker."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any

from dyness_rs485_service import (
    DbusPublisher,
    GUARDIAN_BATTERY_SERVICE,
    now_ms,
    soc61_is_valid,
)

DEFAULT_SOURCE = "/data/home/nodered/cerbo-balancer-latest.json"
DEFAULT_STATE = "/data/home/nodered/cerbo-balancer-guardian-state.json"
FRESHNESS_MS = 20_000
FALLBACK_CVL = 54.0
FALLBACK_CCL = 20.0
FALLBACK_DCL = 100.0
RECOVERY_SAMPLES = 2


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class GuardianStateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.backup = self.path.with_suffix(self.path.suffix + ".bak")

    @staticmethod
    def valid(value: dict[str, Any] | None) -> bool:
        return bool(
            value
            and value.get("version") == 1
            and isinstance(value.get("lastGoodSnapshot"), dict)
            and soc61_is_valid(value.get("lastSoc"))
            and isinstance(value.get("lastValidTimestamp"), int)
        )

    def load(self) -> dict[str, Any] | None:
        for path in (self.path, self.backup):
            value = read_json(path)
            if self.valid(value):
                return value
        return None

    def save(self, snapshot: dict[str, Any], timestamp: int, soc: int) -> None:
        value = {
            "version": 1,
            "lastValidTimestamp": timestamp,
            "lastSoc": soc,
            "lastGoodSnapshot": snapshot,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        if self.valid(read_json(self.path)):
            shutil.copyfile(self.path, self.backup)
        temporary.replace(self.path)


class GuardianController:
    def __init__(self, state: dict[str, Any] | None = None):
        state = state or {}
        self.last_good = copy.deepcopy(state.get("lastGoodSnapshot"))
        self.last_soc = state.get("lastSoc") if soc61_is_valid(state.get("lastSoc")) else None
        self.last_valid_timestamp = state.get("lastValidTimestamp")
        self.last_source_timestamp: int | None = None
        self.recovery_samples = 0
        self.mode = "BOOTSTRAP"

    @staticmethod
    def source_status(snapshot: dict[str, Any] | None, timestamp: int) -> tuple[bool, int | None]:
        if not isinstance(snapshot, dict):
            return False, None
        source_timestamp = snapshot.get("timestamp")
        system = snapshot.get("system") or {}
        control = snapshot.get("effectiveControl") or {}
        if not isinstance(source_timestamp, int):
            return False, None
        age = max(0, timestamp - source_timestamp)
        valid = bool(
            snapshot.get("valid") is True
            and age <= FRESHNESS_MS
            and soc61_is_valid(system.get("soc61"))
            and control.get("outputValid") is True
        )
        return valid, age

    def fallback(self, measurements: dict[str, float] | None = None) -> dict[str, Any]:
        measurements = measurements or {}
        snapshot = copy.deepcopy(self.last_good or {})
        system = dict(snapshot.get("system") or {})
        aggregate = dict(snapshot.get("aggregate") or {})
        limits = dict(snapshot.get("limits") or {})
        flags = dict(limits.get("statusFlags") or {})
        control = dict(snapshot.get("effectiveControl") or {})
        voltage = measurements.get("voltage", system.get("voltage61", 53.0))
        current = measurements.get("current", aggregate.get("summedBatteryCurrent", 0.0))
        system["voltage61"] = float(voltage)
        if soc61_is_valid(self.last_soc):
            system["soc61"] = self.last_soc
        aggregate["summedBatteryCurrent"] = float(current)
        flags.update({"chargeEnabled": True, "dischargeEnabled": True})
        limits["statusFlags"] = flags
        control.update({
            "effectiveChargeVoltage": FALLBACK_CVL,
            "effectiveChargeCurrent": FALLBACK_CCL,
            "effectiveDischargeCurrent": FALLBACK_DCL,
            "effectiveChargeEnabled": True,
            "effectiveDischargeEnabled": True,
            "controllerChargeEnabled": True,
            "outputValid": True,
            "reason": "RS485_FALLBACK_54V_20A",
            "commandReason": "RS485_COMMUNICATION_FALLBACK",
            "authorityState": "APPLIED",
            "controllerRequestApplied": False,
        })
        snapshot.update({
            "valid": True,
            "reason": "RS485_FALLBACK_54V_20A",
            "system": system,
            "aggregate": aggregate,
            "limits": limits,
            "effectiveControl": control,
        })
        return snapshot

    def evaluate(
        self,
        snapshot: dict[str, Any] | None,
        timestamp: int,
        measurements: dict[str, float] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        valid, age = self.source_status(snapshot, timestamp)
        new_valid = bool(valid and snapshot and snapshot.get("timestamp") != self.last_source_timestamp)
        saved = False
        if new_valid and snapshot is not None:
            self.last_source_timestamp = snapshot["timestamp"]
            self.last_good = copy.deepcopy(snapshot)
            self.last_soc = snapshot["system"]["soc61"]
            self.last_valid_timestamp = snapshot["timestamp"]
            self.recovery_samples += 1
            saved = True
        elif not valid:
            self.recovery_samples = 0

        if valid and self.mode == "NORMAL":
            output = copy.deepcopy(snapshot)
            mode = "NORMAL"
            reason = "fresh RS485 telemetry"
        elif valid and self.recovery_samples >= RECOVERY_SAMPLES:
            self.mode = "NORMAL"
            output = copy.deepcopy(snapshot)
            mode = "NORMAL"
            reason = "two consecutive valid RS485 samples"
        elif self.last_good is not None:
            self.mode = "RECOVERY" if valid else "FALLBACK"
            output = self.fallback(measurements)
            mode = self.mode
            reason = "waiting for second valid sample" if valid else "RS485 telemetry unavailable or stale"
        else:
            self.mode = "BOOTSTRAP"
            output = None
            mode = "BOOTSTRAP"
            reason = "no validated RS485 state available"

        diagnostics = {
            "mode": mode,
            "ready": self.last_good is not None,
            "sourceAgeMs": age,
            "lastValidTimestamp": self.last_valid_timestamp,
            "reason": reason,
        }
        return output, diagnostics, saved


def vebus_measurements(publisher: DbusPublisher) -> dict[str, float]:
    if not publisher.available:
        return {}
    result: dict[str, float] = {}
    for key, path in (("voltage", "/Dc/0/Voltage"), ("current", "/Dc/0/Current")):
        try:
            item = publisher.bus.get_object("com.victronenergy.vebus.ttyS4", path)
            value = item.GetValue(dbus_interface="com.victronenergy.BusItem")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = float(value)
        except Exception:
            continue
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    store = GuardianStateStore(args.state)
    controller = GuardianController(store.load())
    publisher = DbusPublisher(
        service_name=GUARDIAN_BATTERY_SERVICE,
        device_instance=101,
        product_name="Dyness RS485 BMS guardian",
        guardian=True,
    )
    source = Path(args.source)
    while True:
        timestamp = now_ms()
        snapshot = read_json(source)
        output, diagnostics, saved = controller.evaluate(
            snapshot, timestamp, vebus_measurements(publisher)
        )
        if saved and controller.last_good is not None and soc61_is_valid(controller.last_soc):
            store.save(
                controller.last_good,
                int(controller.last_valid_timestamp),
                int(controller.last_soc),
            )
        if output is not None:
            output = publisher.annotate_snapshot(output)
            publisher.update(output)
        publisher.update_guardian_status(
            diagnostics["mode"],
            diagnostics["ready"],
            diagnostics["sourceAgeMs"],
            diagnostics["lastValidTimestamp"],
            diagnostics["reason"],
        )
        if args.once:
            print(json.dumps(diagnostics, separators=(",", ":")))
            return 0
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
