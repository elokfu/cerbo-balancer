#!/usr/bin/env python3
"""Supervised, read-only Dyness RS485 telemetry and virtual BMS service.

The service only sends documented read requests (CID2 42, 44, 61, and 63). It
does not implement a serial write/configuration path or any charger write.
When pyserial or the Cerbo D-Bus runtime is unavailable it emits an explicit
unavailable snapshot and remains safe to run in shadow mode.
"""

from __future__ import annotations

import argparse
import csv
from collections import deque
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - exercised on a non-Cerbo host
    serial = None

from dyness_rs485_protocol import (
    parse_limits,
    parse_pack_telemetry,
    parse_status_44,
    parse_system_61,
    request,
)

DEFAULT_PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A602K5MM-if00-port0"
DEFAULT_STATE_DIR = "/data/home/nodered"
CSV_LOG_DIRECTORY = "cerbo-balancer-csv"
DETAILED_TELEMETRY_DIRECTORY = "cerbo-balancer-telemetry"
DETAILED_TELEMETRY_LATEST = "cerbo-balancer-latest.json"
SUMMARY_LOG_NAME = "cerbo-balancer-summary.jsonl"
DETAILED_RETENTION_MS = 24 * 60 * 60 * 1000
SUMMARY_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
SUMMARY_INTERVAL_MS = 60 * 1000
COMMAND_FRESHNESS_MS = 20 * 1000
# TEMPORARY TEST OVERRIDE: permit bounded discharge while RS485 telemetry is invalid.
COMMUNICATION_FAULT_DISCHARGE_FALLBACK_A = 100.0
EXPECTED_ADDRESSES = tuple(range(2, 17))
NORMAL_DISCOVERY_INTERVAL = 60.0
RECOVERY_DISCOVERY_INTERVAL = 10.0
MAX_DISCOVERY_MISSES = 10
SERIAL_RECONNECT_BACKOFF = 2.0
STATUS44_INTERVAL = 5.0
# CID2 44 is diagnostic-only. Its bounded timeout leaves enough room in the
# eight-second primary telemetry cycle for three battery CID2 61/42 reads,
# system limits, and incremental discovery.
STATUS44_QUERY_TIMEOUT = 0.25
# Discovery must never monopolise the serial bus. Two normal probes per poll
# complete a scan within 60 seconds. Recovery checks the full address range in
# one eight-second cycle using the bounded discovery timeout.
NORMAL_DISCOVERY_PROBES_PER_POLL = 2
RECOVERY_DISCOVERY_PROBES_PER_POLL = len(EXPECTED_ADDRESSES)
DISCOVERY_QUERY_TIMEOUT = 0.12
VIRTUAL_BATTERY_SERVICE = "com.victronenergy.battery.rs485_dyness"
GUARDIAN_BATTERY_SERVICE = "com.victronenergy.battery.rs485_dyness_guardian"
VIRTUAL_BATTERY_SELECTION = "com.victronenergy.battery/100"
CAN_BATTERY_SELECTION = "com.victronenergy.battery/512"


def now_ms() -> int:
    return int(time.time() * 1000)


def cerbo_timezone() -> tuple[str, Any]:
    """Read the Cerbo-configured timezone from Victron Settings D-Bus."""
    try:
        import dbus  # type: ignore

        bus = dbus.SystemBus()
        item = bus.get_object("com.victronenergy.settings", "/Settings/System/TimeZone")
        value = item.GetValue(dbus_interface="com.victronenergy.BusItem")
        name = str(value)
        return name, ZoneInfo(name)
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        local_timezone = datetime.now().astimezone().tzinfo
        return "system-local", local_timezone


def active_bms_selection(values: dict[str, Any]) -> dict[str, Any]:
    """Normalize systemcalc's active battery/BMS selection readback."""
    battery_service = str(values.get("ActiveBatteryService") or "")
    bms_service = str(values.get("ActiveBmsService") or "")
    try:
        instance = int(values.get("ActiveBmsInstance"))
    except (TypeError, ValueError):
        instance = None

    if (
        instance == 100
        or battery_service == VIRTUAL_BATTERY_SELECTION
        or bms_service == VIRTUAL_BATTERY_SERVICE
        or bms_service == GUARDIAN_BATTERY_SERVICE
        or instance == 101
    ):
        source = "virtual"
        label = "RS485 virtual BMS active"
    elif instance == 512 or battery_service == CAN_BATTERY_SELECTION:
        source = "can"
        label = "CAN Dyness BMS active"
    else:
        source = "unknown"
        label = "BMS selection unknown"

    return {
        "source": source,
        "label": label,
        "virtualSelected": source == "virtual",
        "activeBatteryService": battery_service or None,
        "activeBmsService": bms_service or None,
        "activeBmsInstance": instance,
        "readbackValid": bool(battery_service or bms_service or instance is not None),
    }


def charge_control_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Normalize the read-only limits configured in the Cerbo GX UI.

    Venus uses a positive maximum voltage to enable its managed-battery
    voltage cap. The charge-current setting uses a negative value when its
    system-wide limit is disabled, while zero remains a valid current limit.
    """
    voltage = values.get("MaxChargeVoltage")
    current = values.get("MaxChargeCurrent")
    voltage = float(voltage) if isinstance(voltage, (int, float)) else None
    current = float(current) if isinstance(current, (int, float)) else None
    return {
        "source": "com.victronenergy.settings/Settings/SystemSetup",
        "readbackValid": voltage is not None and current is not None,
        "maxChargeVoltage": voltage,
        "maxChargeCurrent": current,
        "voltageLimitEnabled": voltage is not None and voltage > 0,
        "currentLimitEnabled": current is not None and current >= 0,
    }


def decode_system_cell_location(value: Any) -> tuple[int | None, int | None]:
    """Decode Dyness CID2 61's packed ``0xCCBB`` cell/battery location."""
    if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        return None, None
    cell_index = (value >> 8) & 0xFF
    battery_address = value & 0xFF
    if not 1 <= cell_index <= 16 or not 1 <= battery_address <= 0xFF:
        return None, None
    return battery_address, cell_index


def system_cell_extrema(system: dict[str, Any]) -> dict[str, Any]:
    """Return the authoritative pack extrema reported by CID2 61.

    CID2 42 remains the source for each battery's cell vector.  This helper
    deliberately does not inspect that vector: aggregate extrema must come
    from the BMS system summary or remain unavailable.
    """
    minimum = system.get("minimumCellVoltage61")
    maximum = system.get("maximumCellVoltage61")
    min_address, min_index = decode_system_cell_location(system.get("minimumCellId61"))
    max_address, max_index = decode_system_cell_location(system.get("maximumCellId61"))
    valid = (
        isinstance(minimum, (int, float)) and 2.0 <= minimum <= 4.5
        and isinstance(maximum, (int, float)) and 2.0 <= maximum <= 4.5
        and maximum >= minimum
        and min_address is not None and min_index is not None
        and max_address is not None and max_index is not None
    )
    if not valid:
        return {
            "source": "CID2_61_SYSTEM_SUMMARY",
            "valid": False,
            "vmin": None,
            "vmax": None,
            "spread": None,
            "minCellAddress": None,
            "minCellIndex": None,
            "maxCellAddress": None,
            "maxCellIndex": None,
        }
    return {
        "source": "CID2_61_SYSTEM_SUMMARY",
        "valid": True,
        "vmin": float(minimum),
        "vmax": float(maximum),
        "spread": round(float(maximum) - float(minimum), 6),
        "minCellAddress": min_address,
        "minCellIndex": min_index,
        "maxCellAddress": max_address,
        "maxCellIndex": max_index,
    }


def virtual_bms_state(control: dict[str, Any], selection: dict[str, Any]) -> str:
    """Create the concise human-readable state shown by GX/VRM."""
    cvl = control.get("effectiveChargeVoltage")
    ccl = control.get("effectiveChargeCurrent")
    charge = "enabled" if control.get("effectiveChargeEnabled") else "inhibited"
    cvl_text = f"{float(cvl):.2f} V" if isinstance(cvl, (int, float)) else "—"
    ccl_text = f"{float(ccl):.1f} A" if isinstance(ccl, (int, float)) else "—"
    return (
        f"{selection.get('label', 'BMS selection unknown')} · "
        f"CVL {cvl_text} · CCL {ccl_text} · charge {charge} · "
        f"{control.get('reason') or 'no arbitration reason'}"
    )


def default_serial_health() -> dict[str, Any]:
    return {
        "state": "disconnected",
        "connected": False,
        "ownerConflict": False,
        "reconnectCount": 0,
        "lastConnectedAt": None,
        "lastPollAt": None,
        "lastValidAt": None,
        "lastError": None,
        "lastErrorType": None,
        "lastPollDurationMs": None,
    }


def unavailable_snapshot(
    reason: str,
    port: str,
    baud: int,
    serial_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": now_ms(), "telemetryAge": None, "valid": False,
        "source": "rs485-dyness", "serialPort": port, "baud": baud,
        "reason": reason, "system": {"voltage61": None, "soc61": None},
        "limits": None, "discovery": {"scannedAddresses": list(EXPECTED_ADDRESSES),
        "respondingAddresses": [], "respondingCount": 0}, "batteries": [],
        "aggregate": {"source": "CID2_61_SYSTEM_SUMMARY", "valid": False,
        "summedBatteryCurrent": None, "vmin": None, "vmax": None,
        "spread": None, "minCellAddress": None, "minCellIndex": None,
        "maxCellAddress": None, "maxCellIndex": None, "minimumTemperature": None,
        "maximumTemperature": None, "averageTemperature": None},
        "cellTelemetryValid": False, "decoderHealth": "unavailable",
        "serialHealth": serial_health or default_serial_health(),
    }


class JsonlStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, name: str, value: dict[str, Any]) -> None:
        with (self.root / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")

    def ensure_json(self, name: str, value: dict[str, Any]) -> None:
        target = self.root / name
        if not target.exists():
            target.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def write_json(self, name: str, value: dict[str, Any]) -> None:
        target = self.root / name
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def read_json(self, name: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.root / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None


def parsed_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove protocol payloads before a snapshot is published or persisted."""
    value = json.loads(json.dumps(snapshot))
    value.pop("rawFrames", None)
    system = value.get("system")
    if isinstance(system, dict):
        system.pop("trailingHex61", None)
    for battery in value.get("batteries") or []:
        if not isinstance(battery, dict):
            continue
        for key in ("rawInfo", "trailingInfo", "rawCapacityTail"):
            battery.pop(key, None)
        status = battery.get("status44")
        if isinstance(status, dict):
            status.pop("rawInfo", None)
            status.pop("trailingInfo", None)
    return value


class RollingTelemetryStore:
    """Persist parsed detailed telemetry and a compact monthly summary."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.detailed_directory = self.root / DETAILED_TELEMETRY_DIRECTORY
        self.detailed_directory.mkdir(parents=True, exist_ok=True)
        self.summary_path = self.root / SUMMARY_LOG_NAME
        self.latest_path = self.root / DETAILED_TELEMETRY_LATEST
        self.timezone_name, self.timezone = cerbo_timezone()
        self.last_summary_timestamp: int | None = self._read_last_summary_timestamp()
        self.last_detailed_prune = 0
        self.last_summary_prune = 0

    @staticmethod
    def _segment_name(timestamp: int) -> str:
        value = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        return f"telemetry-{value:%Y%m%d-%H}.jsonl"

    def _read_last_summary_timestamp(self) -> int | None:
        try:
            with self.summary_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                position = stream.tell()
                stream.seek(max(0, position - 8192))
                lines = stream.read().decode("utf-8", errors="ignore").splitlines()
            for line in reversed(lines):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = value.get("timestamp")
                if isinstance(timestamp, int):
                    return timestamp
        except OSError:
            pass
        return None

    @staticmethod
    def _append(path: Path, value: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")

    @staticmethod
    def _replace_with_lines(path: Path, lines: list[str]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text("".join(lines), encoding="utf-8")
        temporary.replace(path)

    def _prune_detailed(self, timestamp: int) -> None:
        if timestamp - self.last_detailed_prune < 60 * 60 * 1000:
            return
        cutoff = timestamp - DETAILED_RETENTION_MS
        for path in self.detailed_directory.glob("telemetry-*.jsonl"):
            try:
                if path.stat().st_mtime * 1000 < cutoff:
                    path.unlink()
            except OSError:
                continue
        self.last_detailed_prune = timestamp

    def _prune_summary(self, timestamp: int) -> None:
        if timestamp - self.last_summary_prune < 60 * 60 * 1000 or not self.summary_path.exists():
            return
        cutoff = timestamp - SUMMARY_RETENTION_MS
        retained: list[str] = []
        try:
            with self.summary_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value.get("timestamp"), int) and value["timestamp"] >= cutoff:
                        retained.append(json.dumps(value, separators=(",", ":")) + "\n")
            self._replace_with_lines(self.summary_path, retained)
        except OSError:
            pass
        self.last_summary_prune = timestamp

    def _summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        system = snapshot.get("system") or {}
        aggregate = snapshot.get("aggregate") or {}
        batteries = snapshot.get("batteries") or []
        inventory = snapshot.get("inventory") or {}
        expected = set(snapshot.get("expectedAddresses") or inventory.get("activeAddresses") or [])
        responding = {battery.get("address") for battery in batteries}
        complete = bool(snapshot.get("valid") is True and expected and expected == responding and all(
            battery.get("valid") is True for battery in batteries
        ))
        valid = snapshot.get("valid") is True
        battery_currents = [
            {"battery": battery.get("address"), "currentA": battery.get("current")}
            for battery in batteries
            if battery.get("valid") is True and isinstance(battery.get("current"), (int, float))
        ]
        timestamp = snapshot.get("timestamp")
        if not isinstance(timestamp, int):
            timestamp = now_ms()
        local_time = datetime.fromtimestamp(timestamp / 1000, self.timezone).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "timestamp": timestamp,
            "localTime": local_time,
            "socPercent": system.get("soc61") if valid else None,
            "systemVoltageV": system.get("voltage61") if valid else None,
            "vminV": aggregate.get("vmin") if valid else None,
            "vminBattery": aggregate.get("minCellAddress") if valid else None,
            "vminCell": aggregate.get("minCellIndex") if valid else None,
            "vmaxV": aggregate.get("vmax") if valid else None,
            "vmaxBattery": aggregate.get("maxCellAddress") if valid else None,
            "vmaxCell": aggregate.get("maxCellIndex") if valid else None,
            "totalBatteryCurrentA": aggregate.get("summedBatteryCurrent") if complete else None,
            "batteryCurrents": battery_currents,
        }

    def record(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        value = parsed_snapshot(snapshot)
        timestamp = value.get("timestamp")
        if not isinstance(timestamp, int):
            timestamp = now_ms()
            value["timestamp"] = timestamp
        self._append(self.detailed_directory / self._segment_name(timestamp), value)
        temporary = self.latest_path.with_name(f".{self.latest_path.name}.tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(self.latest_path)
        if self.last_summary_timestamp is None or timestamp - self.last_summary_timestamp >= SUMMARY_INTERVAL_MS:
            summary = self._summary(value)
            self._append(self.summary_path, summary)
            self.last_summary_timestamp = timestamp
        self._prune_detailed(timestamp)
        self._prune_summary(timestamp)
        return value


class CsvLogger:
    summary_columns = [
        "timestamp", "sample_number", "system_voltage_v", "soc_percent", "bms_temperature_c",
        "vmin_v", "vmax_v", "spread_mv", "battery_current_a",
    ]
    control_columns = [
        "ccl_a", "dcl_a", "charge_enabled", "discharge_enabled",
        "controller_requested_voltage_v", "controller_requested_current_a",
        "controller_charge_enabled", "controller_command_reason",
        "controller_command_fresh", "controller_command_age_s",
        "virtual_bms_effective_cvl_v", "virtual_bms_effective_ccl_a",
        "virtual_bms_effective_dcl_a", "virtual_bms_charge_enabled",
        "virtual_bms_discharge_enabled", "virtual_bms_allow_to_charge",
        "virtual_bms_allow_to_discharge", "virtual_bms_thermal_factor",
        "virtual_bms_charge_blocked_by_status",
        "virtual_bms_discharge_blocked_by_status",
        "virtual_bms_charge_blocked_by_controller",
        "virtual_bms_output_valid", "virtual_bms_arbitration_reason",
        "virtual_bms_authority_state", "virtual_bms_controller_request_applied",
        "controller_feed_forward_gain", "controller_feed_forward_unscaled_a",
        "controller_feed_forward_effective_a", "controller_p_term_a",
        "controller_i_term_a", "controller_output_saturated",
        "controller_output_slew_limited",
    ]
    battery_fields = [
        "present", "valid", "voltage_v", "current_a", "soc_percent",
        "vmin_v", "vmin_location", "vmax_v", "vmax_location", "spread_mv",
        "status1", "status2",
        "status3", "status4", "status5",
        "cell16_raw_v", "cell16_common_v", "cell16_raw_offset_mv",
        "cell16_filtered_offset_mv", "cell16_target_offset_mv",
        "cell16_pack_residual_mv", "cell16_filter_gain",
        "cell16_persistent_error_samples", "cell16_constraint_source",
        "cell16_constraint_applied", "cell16_corroborated",
        *[f"cell_{index:02d}_v" for index in range(1, 17)],
        *[f"temp_{index:02d}_c" for index in range(1, 6)],
    ]

    def __init__(self, root: str):
        self.directory = Path(root) / CSV_LOG_DIRECTORY
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timezone_name, self.timezone = cerbo_timezone()
        self.active_filename: str | None = None
        self.initial_addresses: tuple[int, ...] | None = None
        self.stopped = False
        self.next_sample_number = 1

    @classmethod
    def columns_for(cls, addresses: tuple[int, ...]) -> list[str]:
        battery_columns = [
            f"battery_{address:02d}_{field}"
            for address in addresses
            for field in cls.battery_fields
        ]
        return cls.summary_columns + battery_columns + cls.control_columns

    @staticmethod
    def _addresses_from_snapshot(snapshot: dict[str, Any]) -> tuple[int, ...]:
        inventory = snapshot.get("inventory") or {}
        addresses = inventory.get("activeAddresses")
        if not isinstance(addresses, list):
            addresses = [battery.get("address") for battery in snapshot.get("batteries") or []]
        valid = sorted({int(address) for address in addresses if isinstance(address, int)})
        return tuple(address for address in valid if 2 <= address <= 255)

    def _read_existing_session(self, target: Path) -> tuple[tuple[int, ...] | None, int]:
        addresses: tuple[int, ...] | None = None
        sample_count = 0
        try:
            with target.open("r", encoding="utf-8") as stream:
                data_lines: list[str] = []
                for line in stream:
                    if line.startswith("# initial_addresses="):
                        raw = line.partition("=")[2].strip()
                        addresses = tuple(sorted({
                            int(item) for item in raw.split(",")
                            if item.strip().isdigit() and 2 <= int(item) <= 255
                        }))
                    elif line and not line.startswith("#"):
                        data_lines.append(line)
                if len(data_lines) > 1:
                    reader = csv.DictReader(data_lines)
                    for row in reader:
                        try:
                            sample_count = max(sample_count, int(row.get("sample_number", "0")))
                        except (TypeError, ValueError):
                            sample_count += 1
        except OSError:
            return None, 0
        return addresses, sample_count

    def _stop(self, filename: str, reason: str) -> dict[str, Any]:
        self.stopped = True
        return {"stopped": True, "filename": filename, "reason": reason}

    @staticmethod
    def _format_cell_voltage(value: Any) -> str:
        """Return a cell voltage in volts with a fixed three decimals."""
        return CsvLogger._format_voltage(value, 3, millivolt_threshold=100)

    @staticmethod
    def _format_voltage(value: Any, decimals: int = 2, millivolt_threshold: float = 100) -> str:
        """Normalize volts or millivolts and format a voltage value."""
        if not isinstance(value, (int, float)):
            return ""
        voltage = float(value)
        if abs(voltage) > millivolt_threshold:
            voltage /= 1000.0
        return f"{voltage:.{decimals}f}"

    @staticmethod
    def _format_number(value: Any, decimals: int = 2) -> str:
        if not isinstance(value, (int, float)):
            return ""
        return f"{float(value):.{decimals}f}"

    @staticmethod
    def _format_status_byte(value: Any) -> str:
        """Format a CID2 0x44 status byte as a fixed two-digit hex value."""
        if not isinstance(value, (int, float)) or not 0 <= value <= 0xFF:
            return ""
        return f"0x{int(value):02X}"

    @staticmethod
    def _format_spread_mv(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return ""
        spread_mv = float(value) * 1000 if abs(float(value)) < 1 else float(value)
        return f"{spread_mv:.0f}"

    def _format_timestamp(self, value: Any) -> str:
        if not isinstance(value, (int, float)):
            return ""
        return datetime.fromtimestamp(value / 1000, self.timezone).strftime("%H:%M:%S")

    def write(self, snapshot: dict[str, Any], control: dict[str, Any] | None) -> dict[str, Any]:
        if not control or control.get("enabled") is not True:
            self.active_filename = None
            self.initial_addresses = None
            self.stopped = False
            self.next_sample_number = 1
            return {"written": False}
        name = control.get("filename")
        if not isinstance(name, str) or not name.endswith(".csv") or "/" in name or "\\" in name or name != Path(name).name:
            return {"written": False}
        if name != self.active_filename:
            self.active_filename = name
            self.initial_addresses = None
            self.stopped = False
            self.next_sample_number = 1
        target = self.directory / name
        exists = target.exists()
        if self.initial_addresses is None and exists:
            self.initial_addresses, sample_count = self._read_existing_session(target)
            self.next_sample_number = sample_count + 1
        if self.stopped:
            return {"written": False, "stopped": True, "filename": name,
                    "reason": "battery disappeared during CSV recording"}
        if self.initial_addresses is None:
            if not snapshot.get("valid"):
                return {"written": False}
            self.initial_addresses = self._addresses_from_snapshot(snapshot)
            if not self.initial_addresses:
                return {"written": False}
        current_addresses = {
            int(battery.get("address"))
            for battery in snapshot.get("batteries") or []
            if isinstance(battery.get("address"), int)
        }
        missing = sorted(set(self.initial_addresses) - current_addresses)
        if missing:
            return self._stop(name, f"battery disappeared during CSV recording: {missing}")
        if not snapshot.get("valid"):
            return {"written": False}
        columns = self.columns_for(self.initial_addresses)
        if exists:
            with target.open("r", encoding="utf-8") as stream:
                header = next((line.rstrip("\n") for line in stream if not line.startswith("#")), "")
            existing_columns = next(csv.reader([header]), []) if header else []
            if existing_columns != columns:
                return self._stop(name, "CSV schema changed; start a new recording file")
        system = snapshot.get("system") or {}
        limits = snapshot.get("limits") or {}
        aggregate = snapshot.get("aggregate") or {}
        control_output = snapshot.get("effectiveControl") or {}
        status_flags = limits.get("statusFlags") or {}
        command_age_s = control_output.get("commandAgeMs")
        if isinstance(command_age_s, (int, float)):
            command_age_s = float(command_age_s) / 1000.0
        row = {column: "" for column in columns}
        timestamp = snapshot.get("timestamp")
        readable_timestamp = self._format_timestamp(timestamp)
        row.update({
            "timestamp": readable_timestamp,
            "sample_number": self.next_sample_number,
            "system_voltage_v": self._format_voltage(system.get("voltage61")),
            "soc_percent": self._format_number(system.get("soc61")),
            "bms_temperature_c": self._format_number(system.get("maximumBmsTemperature61")),
            "vmin_v": self._format_voltage(aggregate.get("vmin"), 3),
            "vmax_v": self._format_voltage(aggregate.get("vmax"), 3),
            "spread_mv": self._format_spread_mv(aggregate.get("spread")),
            "battery_current_a": self._format_number(aggregate.get("summedBatteryCurrent")),
            "ccl_a": self._format_number(limits.get("chargeCurrent")),
            "dcl_a": self._format_number(limits.get("dischargeCurrentSigned")),
            "charge_enabled": status_flags.get("chargeEnabled"),
            "discharge_enabled": status_flags.get("dischargeEnabled"),
            "controller_requested_voltage_v": self._format_voltage(control_output.get("requestedVoltage")),
            "controller_requested_current_a": self._format_number(control_output.get("requestedCurrent")),
            "controller_charge_enabled": control_output.get("controllerChargeEnabled"),
            "controller_command_reason": control_output.get("commandReason"),
            "controller_command_fresh": control_output.get("commandFresh"),
            "controller_command_age_s": self._format_number(command_age_s),
            "virtual_bms_effective_cvl_v": self._format_voltage(control_output.get("effectiveChargeVoltage")),
            "virtual_bms_effective_ccl_a": self._format_number(control_output.get("effectiveChargeCurrent")),
            "virtual_bms_effective_dcl_a": self._format_number(control_output.get("effectiveDischargeCurrent")),
            "virtual_bms_charge_enabled": control_output.get("effectiveChargeEnabled"),
            "virtual_bms_discharge_enabled": control_output.get("effectiveDischargeEnabled"),
            "virtual_bms_allow_to_charge": control_output.get("allowToCharge"),
            "virtual_bms_allow_to_discharge": control_output.get("allowToDischarge"),
            "virtual_bms_thermal_factor": self._format_number(control_output.get("thermalFactor"), 3),
            "virtual_bms_charge_blocked_by_status": control_output.get("chargeBlockedByStatus"),
            "virtual_bms_discharge_blocked_by_status": control_output.get("dischargeBlockedByStatus"),
            "virtual_bms_charge_blocked_by_controller": control_output.get("chargeBlockedByController"),
            "virtual_bms_output_valid": control_output.get("outputValid"),
            "virtual_bms_arbitration_reason": control_output.get("reason"),
            "virtual_bms_authority_state": control_output.get("authorityState"),
            "virtual_bms_controller_request_applied": control_output.get("controllerRequestApplied"),
            "controller_feed_forward_gain": self._format_number(control_output.get("feedForwardGain"), 3),
            "controller_feed_forward_unscaled_a": self._format_number(control_output.get("feedForwardCurrent")),
            "controller_feed_forward_effective_a": self._format_number(control_output.get("effectiveFeedForwardCurrent")),
            "controller_p_term_a": self._format_number(control_output.get("pTerm"), 3),
            "controller_i_term_a": self._format_number(control_output.get("iTerm"), 3),
            "controller_output_saturated": control_output.get("outputSaturated"),
            "controller_output_slew_limited": control_output.get("outputSlewLimited"),
        })
        for battery in snapshot.get("batteries") or []:
            address = int(battery.get("address"))
            if address not in self.initial_addresses:
                continue
            prefix = f"battery_{address:02d}_"; row[prefix + "present"] = True; row[prefix + "valid"] = battery.get("valid"); row[prefix + "voltage_v"] = self._format_voltage(battery.get("voltage")); row[prefix + "current_a"] = self._format_number(battery.get("current"))
            row[prefix + "soc_percent"] = battery.get("soc") if isinstance(battery.get("soc"), int) else ""
            row[prefix + "vmin_v"] = self._format_voltage(battery.get("vmin"), 3)
            row[prefix + "vmax_v"] = self._format_voltage(battery.get("vmax"), 3)
            row[prefix + "spread_mv"] = self._format_spread_mv(battery.get("spread"))
            row[prefix + "vmin_location"] = f"battery {battery.get('vminAddress')} cell {battery.get('vminIndex')}"
            row[prefix + "vmax_location"] = f"battery {battery.get('vmaxAddress')} cell {battery.get('vmaxIndex')}"
            row[prefix + "cell16_raw_v"] = self._format_cell_voltage(battery.get("rawCalculatedCellVoltage"))
            row[prefix + "cell16_common_v"] = self._format_cell_voltage(battery.get("cell16CommonVoltage"))
            row[prefix + "cell16_raw_offset_mv"] = self._format_number(battery.get("cell16RawOffsetMv"), 3)
            row[prefix + "cell16_filtered_offset_mv"] = self._format_number(battery.get("cell16FilteredOffsetMv"), 3)
            row[prefix + "cell16_target_offset_mv"] = self._format_number(battery.get("cell16TargetOffsetMv"), 3)
            row[prefix + "cell16_pack_residual_mv"] = self._format_number(battery.get("cell16PackResidualMv"), 3)
            row[prefix + "cell16_filter_gain"] = self._format_number(battery.get("cell16FilterGain"), 2)
            row[prefix + "cell16_persistent_error_samples"] = battery.get("cell16PersistentErrorSamples")
            row[prefix + "cell16_constraint_source"] = battery.get("cell16ConstraintSource")
            row[prefix + "cell16_constraint_applied"] = battery.get("cell16ConstraintApplied")
            row[prefix + "cell16_corroborated"] = battery.get("cell16Corroborated")
            status = battery.get("status44") or {}
            for index in range(1, 6):
                row[prefix + f"status{index}"] = self._format_status_byte(
                    (status.get(f"status{index}") or {}).get("raw")
                )
            for cell in battery.get("effectiveCells") or []: row[prefix + f"cell_{int(cell.get('index')):02d}_v"] = self._format_cell_voltage(cell.get("voltage"))
            for index, value in enumerate(battery.get("temperatures") or [], 1): row[prefix + f"temp_{index:02d}_c"] = value
        row = {column: row.get(column, "") for column in columns}
        with target.open("a", newline="", encoding="utf-8") as stream:
            if not exists:
                stream.write(f"# schema_version=13\n# serial_port={snapshot.get('serialPort')}\n# baud={snapshot.get('baud')}\n# poll_interval_seconds=8\n# timestamp_format=HH:MM:SS {self.timezone_name}\n# virtual_bms_service=com.victronenergy.battery.rs485_dyness\n# virtual_bms_device_instance=100\n# dvcc_authority=cerbo_battery_monitor_selection\n# status1_bits=bit7 pack under-voltage protection; bit6 charge temperature protection; bit5 discharge temperature protection; bit4 discharge over-current protection; bit3 reserved; bit2 charge over-current protection; bit1 cell under-voltage protection; bit0 over-voltage protection\n# status2_bits=bit7-bit4 reserved; bit3 module power active; bit2 discharge MOSFET on; bit1 charge MOSFET on; bit0 precharge MOSFET on\n# status3_bits=bit7 effective charging; bit6 effective discharging; bit5 heater active; bit4-bit2 reserved; bit3 fully charged; bit2-bit1 reserved; bit0 buzzer active\n# status4_bits=bit7-bit0 cell voltage-check faults for cells 8-1 respectively\n# status5_bits=bit7-bit0 cell voltage-check faults for cells 16-9 respectively\n# initial_addresses={','.join(str(address) for address in self.initial_addresses)}\n")
                csv.DictWriter(stream, fieldnames=columns).writeheader()
            csv.DictWriter(stream, fieldnames=columns).writerow(row)
        self.next_sample_number += 1
        return {"written": True, "filename": name, "initialAddresses": list(self.initial_addresses)}


def thermal_factor(snapshot: dict[str, Any]) -> float:
    temperatures = [temperature for battery in snapshot.get("batteries", [])
                    for temperature in battery.get("temperatures", [])]
    if not temperatures:
        return 0.0
    cold = 0.0 if min(temperatures) <= 0 else 0.25 if min(temperatures) <= 5 else 1.0
    hottest = max(temperatures)
    hot = 0.0 if hottest >= 55 else 0.5 if hottest >= 50 else 1.0
    return min(cold, hot)


def effective_control(snapshot: dict[str, Any], command: dict[str, Any] | None) -> dict[str, Any]:
    limits = snapshot.get("limits") or {}
    status_flags = limits.get("statusFlags") or {}
    valid = bool(snapshot.get("valid"))
    bms_cvl = float(limits.get("chargeVoltage", 55.0)) if valid else None
    bms_ccl = max(0.0, float(limits.get("chargeCurrent", 0.0))) if valid else 0.0
    bms_dcl = (abs(float(limits.get("dischargeCurrentSigned", 0.0)))
               if valid else COMMUNICATION_FAULT_DISCHARGE_FALLBACK_A)
    ui_settings = snapshot.get("chargeControlSettings") or {}
    ui_voltage = ui_settings.get("maxChargeVoltage")
    ui_current = ui_settings.get("maxChargeCurrent")
    ui_voltage_cap = float(ui_voltage) if (
        ui_settings.get("voltageLimitEnabled") is True
        and isinstance(ui_voltage, (int, float))
    ) else None
    ui_current_cap = max(0.0, float(ui_current)) if (
        ui_settings.get("currentLimitEnabled") is True
        and isinstance(ui_current, (int, float))
    ) else None
    factor = thermal_factor(snapshot) if valid else 1.0
    charge_blocked = status_flags.get("chargeEnabled") is False
    discharge_blocked = status_flags.get("dischargeEnabled") is False
    if charge_blocked:
        bms_ccl = 0.0
    if discharge_blocked:
        bms_dcl = 0.0
    command_age = now_ms() - command["timestamp"] if command and isinstance(command.get("timestamp"), (int, float)) else None
    # The poller reads the command at the start of each eight-second cycle,
    # before Node-RED can react to the snapshot produced by that same cycle.
    # Two complete cycles therefore fit inside the command validity budget.
    fresh_command = bool(command_age is not None and 0 <= command_age <= COMMAND_FRESHNESS_MS)
    requested_voltage = float(command.get("requestedVoltage", 55.2)) if fresh_command else 55.2
    requested_current = float(command.get("requestedCurrent", 100.0)) if fresh_command else 100.0
    controller_charge_enabled = bool(command.get("chargeEnabled", True)) if fresh_command else True
    charge_request_current = requested_current if controller_charge_enabled else 0.0
    selection = snapshot.get("activeBms") or {}
    selection_valid = selection.get("readbackValid") is True
    virtual_selected = selection.get("virtualSelected") is True
    authority_state = "APPLIED" if selection_valid and virtual_selected else "SHADOW" if selection_valid else "UNKNOWN"
    controller_request_applied = bool(authority_state == "APPLIED" and fresh_command and valid)
    # Invalid pack telemetry must remain charge-capable at the explicitly
    # requested conservative fallback. Valid master limits and permission are
    # still authoritative whenever they are available.
    normal_voltage = ui_voltage_cap if ui_voltage_cap is not None else 55.2
    normal_current = ui_current_cap if ui_current_cap is not None else 100.0
    applied_voltage = requested_voltage if controller_request_applied else normal_voltage
    applied_current = charge_request_current if controller_request_applied else normal_current
    applied_charge_enabled = controller_charge_enabled if controller_request_applied else True
    voltage_ceilings = [applied_voltage, bms_cvl, 56.5]
    current_ceilings = [applied_current, bms_ccl]
    if ui_voltage_cap is not None:
        voltage_ceilings.append(ui_voltage_cap)
    if ui_current_cap is not None:
        current_ceilings.append(ui_current_cap)
    else:
        # Retain the old conservative ceiling only when the GX setting is not
        # enabled or cannot be read. A valid UI limit replaces this fallback.
        current_ceilings.append(100.0)
    effective_voltage = min(voltage_ceilings) if valid else min(55.0, normal_voltage)
    effective_current = min(current_ceilings) * factor if valid else min(10.0, normal_current)
    effective_charge_enabled = bool(
        (not valid) or
        (applied_charge_enabled and effective_current > 0 and
         status_flags.get("chargeEnabled") is True)
    )
    effective_discharge_enabled = bool(
        bms_dcl > 0 and status_flags.get("dischargeEnabled") is not False
    )
    if not valid:
        reason = "RS485_TELEMETRY_INVALID"
    elif not fresh_command:
        reason = "COMMAND_STALE_OR_UNAVAILABLE"
    elif authority_state == "UNKNOWN":
        reason = "BMS_SELECTION_UNKNOWN_SHADOW"
    elif authority_state == "SHADOW":
        reason = "CAN_BMS_SELECTED_SHADOW"
    elif charge_blocked:
        reason = "BMS_CHARGE_PERMISSION_DISABLED"
    elif not controller_charge_enabled:
        reason = "CONTROLLER_CHARGE_INHIBIT"
    elif bms_ccl <= 0:
        reason = "BMS_CCL_ZERO"
    elif factor < 1.0:
        reason = "THERMAL_DERATING"
    elif effective_current < charge_request_current or effective_voltage < requested_voltage:
        reason = "BMS_OR_SAFETY_LIMIT"
    else:
        reason = "BMS_LIMITS_ACCEPTED"
    return {
        "authorityState": authority_state,
        "controllerRequestApplied": controller_request_applied,
        "commandFresh": fresh_command,
        "commandAgeMs": command_age,
        "commandReason": command.get("reason") if isinstance(command, dict) else None,
        "feedForwardGain": command.get("feedForwardGain") if isinstance(command, dict) else None,
        "feedForwardCurrent": command.get("feedForwardCurrent") if isinstance(command, dict) else None,
        "effectiveFeedForwardCurrent": command.get("effectiveFeedForwardCurrent") if isinstance(command, dict) else None,
        "pTerm": command.get("pTerm") if isinstance(command, dict) else None,
        "iTerm": command.get("iTerm") if isinstance(command, dict) else None,
        "outputSaturated": command.get("outputSaturated") if isinstance(command, dict) else None,
        "outputSlewLimited": command.get("outputSlewLimited") if isinstance(command, dict) else None,
        "requestedVoltage": requested_voltage if fresh_command else None,
        "requestedCurrent": requested_current if fresh_command else None,
        "controllerChargeEnabled": controller_charge_enabled if fresh_command else None,
        "bmsChargeVoltage": bms_cvl if valid else None,
        "bmsChargeCurrent": bms_ccl if valid else None,
        "bmsDischargeCurrent": bms_dcl if valid else None,
        "cerboMaxChargeVoltage": ui_voltage,
        "cerboMaxChargeCurrent": ui_current,
        "cerboVoltageLimitEnabled": ui_settings.get("voltageLimitEnabled") is True,
        "cerboCurrentLimitEnabled": ui_settings.get("currentLimitEnabled") is True,
        "effectiveChargeVoltage": effective_voltage,
        "effectiveChargeCurrent": effective_current,
        "effectiveDischargeCurrent": bms_dcl,
        "effectiveChargeEnabled": effective_charge_enabled,
        "effectiveDischargeEnabled": effective_discharge_enabled,
        "allowToCharge": effective_charge_enabled,
        "allowToDischarge": effective_discharge_enabled,
        "thermalFactor": factor,
        "statusFlags": status_flags,
        "chargeBlockedByStatus": charge_blocked,
        "chargeBlockedByController": bool(controller_request_applied and not controller_charge_enabled),
        "dischargeBlockedByStatus": discharge_blocked,
        "outputValid": valid,
        "reason": reason,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def soc61_is_valid(soc61: Any) -> bool:
    """A CID2=61 SOC is publishable only as a whole percentage 0..100.

    An absent or invalid SOC must never be coerced to 0%: that would make the
    selected battery look empty and trigger empty-battery protection.
    """
    return isinstance(soc61, int) and not isinstance(soc61, bool) and 0 <= soc61 <= 100


class Cell16Estimator:
    """Track cell 16 as a filtered offset from common cell movement."""

    RESTART_VOLTAGE_JUMP_V = 0.5
    STALE_STATE_MS = 20_000
    OFFSET_WINDOW = 5
    NORMAL_GAIN = 0.15
    FAST_GAIN = 0.30
    MAX_ERROR_V = 0.020
    FAST_ERROR_V = 0.010
    FAST_SAMPLES = 5

    def __init__(self) -> None:
        self._state: dict[int, dict[str, Any]] = {}

    @staticmethod
    def _common_voltage(values: list[float]) -> float:
        ordered = sorted(values)
        return sum(ordered[3:-3]) / 9.0

    def estimate(
        self,
        battery: dict[str, Any],
        extrema: dict[str, Any],
        timestamp_ms: int,
    ) -> dict[str, Any] | None:
        address = battery.get("address")
        cells = battery.get("reportedCells") or []
        voltage = battery.get("voltage")
        values = [float(cell) for cell in cells if isinstance(cell, (int, float))]
        if (
            address is None or len(values) != 15 or
            not isinstance(voltage, (int, float)) or
            not 40.0 <= float(voltage) <= 70.0 or
            not extrema.get("valid") or
            not isinstance(timestamp_ms, int)
        ):
            self.reset(address)
            return None
        minimum = extrema.get("vmin")
        maximum = extrema.get("vmax")
        if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
            self.reset(address)
            return None
        pack_voltage = float(voltage)
        reported_sum = sum(values)
        common = self._common_voltage(values)
        raw_cell16 = pack_voltage - reported_sum
        raw_offset = raw_cell16 - common
        previous = self._state.get(address)
        if previous is not None and timestamp_ms == previous["timestamp"]:
            return dict(previous["result"])
        if previous is not None and (
            timestamp_ms - previous["timestamp"] > self.STALE_STATE_MS or
            abs(pack_voltage - previous["voltage"]) > self.RESTART_VOLTAGE_JUMP_V
        ):
            self.reset(address)
            previous = None
        if previous is None:
            raw_offsets = deque([raw_offset], maxlen=self.OFFSET_WINDOW)
            filtered_offset = raw_offset
            persistent_sign = 0
            persistent_samples = 0
            gain = self.NORMAL_GAIN
        else:
            raw_offsets = deque(previous["rawOffsets"], maxlen=self.OFFSET_WINDOW)
            raw_offsets.append(raw_offset)
            filtered_offset = previous["filteredOffset"]
            target = _median(list(raw_offsets))
            error = target - filtered_offset
            sign = 1 if error > 0 else -1 if error < 0 else 0
            if abs(error) > self.FAST_ERROR_V:
                persistent_sign = sign
                persistent_samples = previous["persistentSamples"] + 1 if sign == previous["persistentSign"] else 1
            else:
                persistent_sign = 0
                persistent_samples = 0
            gain = self.FAST_GAIN if persistent_samples >= self.FAST_SAMPLES else self.NORMAL_GAIN
            filtered_offset += gain * min(max(error, -self.MAX_ERROR_V), self.MAX_ERROR_V)
        target_offset = _median(list(raw_offsets))
        candidate = common + filtered_offset
        constraint_source = "CID61_RANGE"
        corroborated = False
        if extrema.get("minCellAddress") == address and extrema.get("minCellIndex") == 16:
            published = float(minimum)
            constraint_source = "CID61_MIN"
            corroborated = True
        elif extrema.get("maxCellAddress") == address and extrema.get("maxCellIndex") == 16:
            published = float(maximum)
            constraint_source = "CID61_MAX"
            corroborated = True
        else:
            published = min(max(candidate, float(minimum)), float(maximum))
        constraint_applied = corroborated or abs(published - candidate) > 1e-12
        filtered_offset = published - common
        result = {
            "rawCalculatedCellVoltage": raw_cell16,
            "calculatedCellVoltage": published,
            "cell16CommonVoltage": common,
            "cell16RawOffsetMv": raw_offset * 1000.0,
            "cell16FilteredOffsetMv": filtered_offset * 1000.0,
            "cell16TargetOffsetMv": target_offset * 1000.0,
            "cell16PackResidualMv": (pack_voltage - (reported_sum + published)) * 1000.0,
            "cell16FilterGain": gain,
            "cell16PersistentErrorSamples": persistent_samples,
            "cell16ConstraintSource": constraint_source,
            "cell16ConstraintApplied": constraint_applied,
            "cell16Corroborated": corroborated,
        }
        self._state[address] = {
            "rawOffsets": list(raw_offsets),
            "filteredOffset": filtered_offset,
            "persistentSign": persistent_sign,
            "persistentSamples": persistent_samples,
            "voltage": pack_voltage,
            "timestamp": timestamp_ms,
            "result": result,
        }
        return dict(result)

    def reset(self, address: int | None) -> None:
        if address is not None:
            self._state.pop(address, None)

    def prune(self, known_addresses: set[int], timestamp_ms: int) -> None:
        for address, state in list(self._state.items()):
            if address not in known_addresses or timestamp_ms - state["timestamp"] > self.STALE_STATE_MS:
                del self._state[address]


class ReadOnlyPoller:
    def __init__(self, port: str, baud: int, timeout: float, inventory: dict[str, Any] | None = None):
        self.port_name, self.baud, self.timeout = port, baud, timeout
        self.serial_port: Any | None = None
        self.serial_retry_at = 0.0
        self.serial_health = default_serial_health()
        self.active_addresses: list[int] = []
        self.pending_removal: dict[int, dict[str, Any]] = {}
        self.last_seen_at: dict[int, int] = {}
        self.next_discovery_at = 0.0
        self.next_status_at = 0.0
        self.status44_cache: dict[int, dict[str, Any]] = {}
        self.last_discovery_at: int | None = None
        self.discovery_scan: dict[str, Any] | None = None
        self.cell16_estimator = Cell16Estimator()
        if inventory:
            self.load_inventory(inventory)

    def _owner_conflict(self) -> bool:
        if not os.path.exists(self.port_name):
            return False
        try:
            result = subprocess.run(
                ["fuser", self.port_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.3,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False
        owners: set[int] = set()
        for token in f"{result.stdout} {result.stderr}".split():
            try:
                owners.add(int(token))
            except ValueError:
                continue
        return any(owner != os.getpid() for owner in owners)

    def _set_serial_error(self, error: Exception, error_type: str) -> None:
        self.serial_health.update({
            "state": "error",
            "connected": False,
            "lastError": str(error),
            "lastErrorType": error_type,
        })

    def _close_serial(self, reason: str | None = None) -> None:
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except (OSError, AttributeError):
                pass
        self.serial_port = None
        self.serial_health["connected"] = False
        self.serial_health["state"] = "disconnected"
        if reason:
            self.serial_health["lastError"] = reason
        self.serial_retry_at = time.monotonic() + SERIAL_RECONNECT_BACKOFF

    def _open_serial(self) -> Any | None:
        if self.serial_port is not None and getattr(self.serial_port, "is_open", True):
            return self.serial_port
        if time.monotonic() < self.serial_retry_at:
            return None
        if not os.path.exists(self.port_name):
            self.serial_health.update({
                "state": "disconnected",
                "connected": False,
                "ownerConflict": False,
                "lastError": "RS485 adapter is disconnected",
                "lastErrorType": "usb_disconnect",
            })
            return None
        try:
            options = {
                "baudrate": self.baud,
                "bytesize": 8,
                "parity": serial.PARITY_NONE,
                "stopbits": 1,
                "timeout": 0.05,
                "write_timeout": self.timeout,
                "exclusive": True,
            }
            try:
                self.serial_port = serial.Serial(self.port_name, **options)
            except TypeError:
                options.pop("exclusive")
                self.serial_port = serial.Serial(self.port_name, **options)
            self.serial_port.reset_input_buffer()
            connected_at = now_ms()
            self.serial_health.update({
                "state": "connected",
                "connected": True,
                "ownerConflict": self._owner_conflict(),
                "reconnectCount": self.serial_health["reconnectCount"] + 1,
                "lastConnectedAt": connected_at,
                "lastError": None,
                "lastErrorType": None,
            })
            return self.serial_port
        except Exception as error:
            self.serial_port = None
            self.serial_health["ownerConflict"] = self._owner_conflict()
            self._set_serial_error(error, "open")
            self.serial_retry_at = time.monotonic() + SERIAL_RECONNECT_BACKOFF
            return None

    def load_inventory(self, inventory: dict[str, Any]) -> None:
        active = inventory.get("activeAddresses", [])
        active_addresses: set[int] = set()
        for raw_address in active:
            try:
                address = int(raw_address)
            except (TypeError, ValueError):
                continue
            if address in EXPECTED_ADDRESSES:
                active_addresses.add(address)
        self.active_addresses = sorted(active_addresses)
        self.last_seen_at = {}
        for raw_address, timestamp in (inventory.get("lastSeenAt", {}) or {}).items():
            try:
                address = int(raw_address)
            except (TypeError, ValueError):
                continue
            if address in EXPECTED_ADDRESSES and isinstance(timestamp, (int, float)):
                self.last_seen_at[address] = int(timestamp)
        self.pending_removal = {}
        for item in inventory.get("pendingRemoval", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                address = int(item.get("address", -1))
                missed = int(item.get("missedScans", 0))
            except (TypeError, ValueError):
                continue
            if address in EXPECTED_ADDRESSES and 0 < missed < MAX_DISCOVERY_MISSES:
                self.pending_removal[address] = {
                    "missedScans": missed,
                    "lastSeenAt": self.last_seen_at.get(address),
                    "lastMissingAt": item.get("lastMissingAt"),
                }
        self.active_addresses = [address for address in self.active_addresses if address not in self.pending_removal]
        self.next_discovery_at = 0.0

    def export_inventory(self) -> dict[str, Any]:
        return {
            "version": 1,
            "activeAddresses": list(self.active_addresses),
            "pendingRemoval": [
                {"address": address, **details}
                for address, details in sorted(self.pending_removal.items())
            ],
            "lastSeenAt": {str(address): timestamp for address, timestamp in self.last_seen_at.items()},
        }

    def inventory_snapshot(self) -> dict[str, Any]:
        interval = RECOVERY_DISCOVERY_INTERVAL if self.pending_removal else NORMAL_DISCOVERY_INTERVAL
        scan_in_progress = self.discovery_scan is not None
        remaining = (0.0 if scan_in_progress else
                     max(0.0, self.next_discovery_at - time.monotonic())
                     if self.next_discovery_at else None)
        return {
            "activeAddresses": list(self.active_addresses),
            "pendingRemoval": [
                {
                    "address": address,
                    "missedScans": details["missedScans"],
                    "maxAttempts": MAX_DISCOVERY_MISSES,
                    "lastSeenAt": details.get("lastSeenAt"),
                    "lastMissingAt": details.get("lastMissingAt"),
                }
                for address, details in sorted(self.pending_removal.items())
            ],
            "discoveryMode": "recovery" if self.pending_removal else "normal",
            "scanIntervalSeconds": interval,
            "nextDiscoveryInSeconds": round(remaining, 1) if remaining is not None else None,
            "lastDiscoveryAt": self.last_discovery_at,
            "scanInProgress": scan_in_progress,
            "scannedAddressCount": self.discovery_scan["index"] if scan_in_progress else 0,
            "scanAddressCount": len(EXPECTED_ADDRESSES),
        }

    def decorate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        inventory = self.inventory_snapshot()
        discovery = snapshot.setdefault("discovery", {})
        discovery["activeAddresses"] = list(self.active_addresses)
        discovery["pendingRemoval"] = inventory["pendingRemoval"]
        discovery["scanIntervalSeconds"] = inventory["scanIntervalSeconds"]
        discovery["nextDiscoveryInSeconds"] = inventory["nextDiscoveryInSeconds"]
        snapshot["inventory"] = inventory
        return snapshot

    def apply_discovery(self, responding_addresses: list[int], timestamp: int) -> None:
        responding = set(responding_addresses)
        known = set(self.active_addresses) | set(self.pending_removal)
        self.active_addresses = sorted(responding)
        for address in responding:
            self.last_seen_at[address] = timestamp
            self.pending_removal.pop(address, None)
        for address in sorted(known - responding):
            details = self.pending_removal.setdefault(address, {
                "missedScans": 0,
                "lastSeenAt": self.last_seen_at.get(address),
                "lastMissingAt": None,
            })
            details["missedScans"] += 1
            details["lastMissingAt"] = timestamp
            if details["missedScans"] >= MAX_DISCOVERY_MISSES:
                self.pending_removal.pop(address, None)
                self.last_seen_at.pop(address, None)
        self.last_discovery_at = timestamp
        interval = RECOVERY_DISCOVERY_INTERVAL if self.pending_removal else NORMAL_DISCOVERY_INTERVAL
        self.next_discovery_at = time.monotonic() + interval

    def query(self, port: Any, address: int, cid2: int, timeout: float | None = None) -> bytes | None:
        port.reset_input_buffer()
        port.write(request(address, cid2))
        port.flush()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        frame = bytearray()
        started = False
        while time.monotonic() < deadline:
            byte = port.read(1)
            if not byte:
                continue
            if byte == b"~":
                frame = bytearray(byte)
                started = True
                continue
            if started:
                frame.extend(byte)
                if byte == b"\r":
                    return bytes(frame)
        return None

    def _start_discovery(self) -> None:
        if self.discovery_scan is None and time.monotonic() >= self.next_discovery_at:
            self.discovery_scan = {"index": 0, "responding": set()}

    def _advance_discovery(self, port: Any, active_batteries: dict[int, dict[str, Any]]) -> tuple[list[int], bool]:
        """Probe a small bounded part of a complete inventory scan.

        Active telemetry is gathered first.  Discovery replies are used only
        to update inventory after every address has been checked, so a partial
        scan can never make an otherwise healthy active pack incomplete.
        """
        self._start_discovery()
        if self.discovery_scan is None:
            return [], False
        scan = self.discovery_scan
        probes = (RECOVERY_DISCOVERY_PROBES_PER_POLL if self.pending_removal
                  else NORMAL_DISCOVERY_PROBES_PER_POLL)
        scanned: list[int] = []
        while scan["index"] < len(EXPECTED_ADDRESSES) and len(scanned) < probes:
            address = EXPECTED_ADDRESSES[scan["index"]]
            scan["index"] += 1
            scanned.append(address)
            active = active_batteries.get(address)
            if active is not None and active.get("valid"):
                scan["responding"].add(address)
                continue
            frame = self.query(port, address, 0x42, DISCOVERY_QUERY_TIMEOUT)
            if not frame:
                continue
            try:
                if parse_pack_telemetry(frame, address).valid:
                    scan["responding"].add(address)
            except (UnicodeDecodeError, ValueError):
                # A malformed discovery response is not a valid inventory hit.
                continue
        complete = scan["index"] >= len(EXPECTED_ADDRESSES)
        if complete:
            self.apply_discovery(sorted(scan["responding"]), now_ms())
            self.discovery_scan = None
        return scanned, complete

    def poll(self, command: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        self.serial_health["ownerConflict"] = self._owner_conflict()
        if serial is None:
            snapshot = unavailable_snapshot("pyserial is unavailable", self.port_name, self.baud, self.serial_health.copy())
            snapshot["effectiveControl"] = effective_control(snapshot, command)
            return self.decorate_snapshot(snapshot)
        port = self._open_serial()
        if port is None:
            reason = self.serial_health.get("lastError") or "RS485 adapter is disconnected"
            snapshot = unavailable_snapshot(reason, self.port_name, self.baud, self.serial_health.copy())
            snapshot["effectiveControl"] = effective_control(snapshot, command)
            return self.decorate_snapshot(snapshot)
        try:
            system = self.query(port, 2, 0x61)
            limits_frame = self.query(port, 2, 0x63)
            batteries = []
            raw_frames = []
            system_values: dict[str, Any] = {
                "voltage61": None,
                "current61": None,
                "soc61": None,
            }
            system61_by_address: dict[int, dict[str, Any]] = {}
            system_voltage = system_soc = None
            errors = []
            if system:
                try:
                    parsed_system = parse_system_61(system, 2)
                    system_values = parsed_system.as_dict()
                    system61_by_address[2] = system_values
                    system_voltage, system_soc = parsed_system.voltage, parsed_system.soc
                    raw_frames.append({"cid2": "61", "address": 2, "frame": system.decode("ascii")})
                except ValueError as error:
                    errors.append(f"CID2=61: {error}")
            else:
                errors.append("CID2=61 timeout")
            limits = None
            if limits_frame:
                try:
                    limits = parse_limits(limits_frame).as_dict()
                    raw_frames.append({"cid2": "63", "address": 2, "frame": limits_frame.decode("ascii")})
                except ValueError as error:
                    errors.append(f"CID2=63: {error}")
            else:
                errors.append("CID2=63 timeout")
            addresses = tuple(self.active_addresses)
            status_addresses = set(addresses) if (
                addresses and not self.pending_removal and
                time.monotonic() >= self.next_status_at
            ) else set()
            status_errors: list[str] = []
            # CID2=0x61 is a master/system-summary response on this Dyness
            # installation.  Slave addresses still provide their own CID2=42
            # analog telemetry and CID2=44 status, but do not answer CID2=61.
            # Do not turn those expected no-replies into communication faults.
            for address in addresses:
                frame = self.query(port, address, 0x42)
                if not frame:
                    errors.append(f"CID2=42 address {address:02X} timeout")
                    continue
                try:
                    battery = parse_pack_telemetry(frame, address).as_dict()
                    addressed_system = system61_by_address.get(address)
                    addressed_extrema = system_cell_extrema(addressed_system or {})
                    addressed_soc = addressed_system.get("soc61") if addressed_system else None
                    battery.update({
                        "system61": addressed_system,
                        "system61Valid": bool(
                            addressed_extrema["valid"] and
                            isinstance(addressed_soc, int) and 0 <= addressed_soc <= 100
                        ),
                        "soc": addressed_soc,
                        "vmin": addressed_extrema["vmin"],
                        "vminId": addressed_system.get("minimumCellId61") if addressed_system else None,
                        "vminAddress": addressed_extrema["minCellAddress"],
                        "vminIndex": addressed_extrema["minCellIndex"],
                        "vmax": addressed_extrema["vmax"],
                        "vmaxId": addressed_system.get("maximumCellId61") if addressed_system else None,
                        "vmaxAddress": addressed_extrema["maxCellAddress"],
                        "vmaxIndex": addressed_extrema["maxCellIndex"],
                        "spread": addressed_extrema["spread"],
                        "controllerExtremaSource": "CID2_61_ADDRESSED_SYSTEM_SUMMARY",
                    })
                    battery["systemVoltageDeltaMv"] = ((battery["voltage"] - system_voltage) * 1000
                                                         if system_voltage is not None else None)
                    status44 = self.status44_cache.get(address)
                    if address in status_addresses:
                        status_frame = self.query(port, address, 0x44, STATUS44_QUERY_TIMEOUT)
                        if status_frame:
                            try:
                                status44 = parse_status_44(status_frame, address).as_dict()
                                status44.update({"timestamp": now_ms(), "error": None})
                                self.status44_cache[address] = status44
                                raw_frames.append({
                                    "cid2": "44",
                                    "address": address,
                                    "frame": status_frame.decode("ascii"),
                                })
                            except (UnicodeDecodeError, ValueError) as error:
                                status_errors.append(
                                    f"CID2=44 address {address:02X}: {error}"
                                )
                                status44 = {
                                    "available": False,
                                    "timestamp": now_ms(),
                                    "error": str(error),
                                }
                                self.status44_cache[address] = status44
                        else:
                            error = f"CID2=44 address {address:02X} timeout"
                            status_errors.append(error)
                            status44 = {
                                "available": False,
                                "timestamp": now_ms(),
                                "error": error,
                            }
                            self.status44_cache[address] = status44
                    if status44 is None:
                        status44 = {
                            "available": False,
                            "timestamp": now_ms(),
                            "error": "CID2=44 status not available yet",
                        }
                    battery["status44"] = {
                        **status44,
                        "ageMs": max(0, now_ms() - status44["timestamp"]),
                    }
                    batteries.append(battery)
                    raw_frames.append({"cid2": "42", "address": address, "frame": frame.decode("ascii")})
                except (UnicodeDecodeError, ValueError) as error:
                    errors.append(f"CID2=42 address {address:02X}: {error}")
            if status_addresses:
                self.next_status_at = time.monotonic() + STATUS44_INTERVAL
            # Preserve the expected set used for this active telemetry sample.
            # A completed discovery can add a new battery, which is polled as
            # active on the following eight-second sample rather than causing a
            # one-sample false "incomplete telemetry" fault.
            expected_addresses_before_discovery = set(self.active_addresses)
            discovery_scanned, discovery_complete = self._advance_discovery(
                port, {item["address"]: item for item in batteries}
            )
            sample_timestamp = now_ms()
            global_extrema = system_cell_extrema(system_values)
            known_addresses = set(self.active_addresses)
            if global_extrema["valid"] and (
                global_extrema["minCellAddress"] not in known_addresses or
                global_extrema["maxCellAddress"] not in known_addresses
            ):
                global_extrema = {**global_extrema, "valid": False}
                errors.append("CID2=61 cell extrema identify an unknown battery")
            # Pack subtraction is a noisy per-battery observation.  Filter its
            # offset from common cell movement, then constrain the result with
            # fresh CID2 61 global extrema before it can affect control.
            for battery in batteries:
                effective_cells = battery.get("effectiveCells") or []
                address = battery.get("address")
                if battery.get("calculatedCellIndex") == 16 and len(effective_cells) == 16:
                    estimate = self.cell16_estimator.estimate(
                        battery, global_extrema, sample_timestamp
                    )
                    if estimate is None:
                        battery["valid"] = False
                        battery["validationErrors"] = (battery.get("validationErrors") or []) + [
                            "cell 16 reconstruction unavailable"
                        ]
                        self.cell16_estimator.reset(address)
                        continue
                    battery.update(estimate)
                    estimated = estimate["calculatedCellVoltage"]
                    effective_cells[15]["voltage"] = estimated
                    battery["calculatedCellVoltage"] = estimated
                    battery["reconstructedCellSum"] = sum(
                        float(cell.get("voltage")) for cell in effective_cells
                    )
                    battery["reconstructedVoltageDeltaMv"] = (
                        battery["reconstructedCellSum"] - float(battery["voltage"])
                    ) * 1000.0
                    battery["cell16Source"] = "reconstructed"
                    errors_for_reconstruction = [
                        error for error in battery.get("validationErrors") or []
                        if not error.startswith("cell voltage outside") and
                        not error.startswith("cell sum differs")
                    ]
                    if not 2.5 <= estimated <= 4.5:
                        errors_for_reconstruction.append(
                            "calculated cell 16 outside 2.5..4.5 V validation range"
                        )
                    if abs(battery["reconstructedVoltageDeltaMv"]) > 100.0:
                        errors_for_reconstruction.append(
                            "cell sum differs from battery voltage by "
                            f"{battery['reconstructedVoltageDeltaMv']:.1f} mV"
                        )
                    battery["validationErrors"] = errors_for_reconstruction
                    battery["valid"] = not errors_for_reconstruction
                values = [
                    float(cell.get("voltage"))
                    for cell in effective_cells
                    if isinstance(cell.get("voltage"), (int, float))
                ]
                if battery.get("valid") and len(values) == 16:
                    minimum = min(values)
                    maximum = max(values)
                    min_index = values.index(minimum) + 1
                    max_index = values.index(maximum) + 1
                    battery.update({
                        "vmin": minimum,
                        "vmax": maximum,
                        "spread": maximum - minimum,
                        "vminAddress": battery.get("address"),
                        "vminIndex": min_index,
                        "vmaxAddress": battery.get("address"),
                        "vmaxIndex": max_index,
                        "controllerExtremaSource": "CID2_42_CELL_ARRAY",
                    })
                if not battery.get("valid"):
                    self.cell16_estimator.reset(address)
            self.cell16_estimator.prune(set(self.active_addresses), sample_timestamp)
            valid_batteries = [item for item in batteries if item["valid"]]
            expected_addresses = expected_addresses_before_discovery
            responding_addresses = {item["address"] for item in batteries}
            complete_battery_set = bool(expected_addresses) and not self.pending_removal and responding_addresses == expected_addresses
            currents = [item["current"] for item in valid_batteries]
            temps = [temperature for item in valid_batteries for temperature in item["temperatures"]]
            all_expected_valid = (
                complete_battery_set and len(valid_batteries) == len(batteries)
            )
            extrema = global_extrema
            if not extrema["valid"]:
                errors.append("CID2=61 cell extrema unavailable or invalid")
            snapshot = {
                "timestamp": now_ms(), "telemetryAge": 0,
                "valid": bool(system_voltage is not None and limits and all_expected_valid and extrema["valid"]),
                "source": "rs485-dyness", "serialPort": self.port_name, "baud": self.baud,
                "reason": "; ".join(errors) if errors else None,
                "system": system_values, "limits": limits,
                "discovery": {"scannedAddresses": discovery_scanned or list(addresses),
                "respondingAddresses": [item["address"] for item in batteries], "respondingCount": len(batteries),
                "scanType": "incremental-complete" if discovery_complete else "incremental" if discovery_scanned else "active"},
                "expectedAddresses": sorted(expected_addresses),
                "batteries": batteries,
                "status44Health": {
                    "state": "healthy" if not status_errors else "degraded",
                    "errors": status_errors,
                    "pollIntervalSeconds": STATUS44_INTERVAL,
                },
                "aggregate": {**extrema,
                "summedBatteryCurrent": sum(currents) if all_expected_valid else None,
                "minimumTemperature": min(temps) if temps else None, "maximumTemperature": max(temps) if temps else None,
                "averageTemperature": sum(temps) / len(temps) if temps else None},
                "cellTelemetryValid": bool(all_expected_valid and extrema["valid"]),
                "addressedSystemTelemetryValid": bool(system is not None and extrema["valid"]),
                "addressedSystemTelemetrySource": "CID2_61_MASTER_ADDRESS_02",
                "decoderHealth": "healthy" if not errors else "degraded", "rawFrames": raw_frames,
            }
            self.serial_health.update({
                "state": "connected" if not errors else "degraded",
                "connected": True,
                "ownerConflict": self._owner_conflict(),
                "lastPollAt": snapshot["timestamp"],
                "lastPollDurationMs": round((time.monotonic() - started) * 1000, 1),
            })
            if snapshot["valid"]:
                self.serial_health["lastValidAt"] = snapshot["timestamp"]
            snapshot["serialHealth"] = self.serial_health.copy()
            snapshot["effectiveControl"] = effective_control(snapshot, command)
            return self.decorate_snapshot(snapshot)
        except Exception as error:  # serial errors must fail safe and be observable
            error_type = "usb_disconnect" if isinstance(error, OSError) else "serial"
            self._set_serial_error(error, error_type)
            self._close_serial()
            snapshot = unavailable_snapshot(f"serial poll failed: {error}", self.port_name, self.baud, self.serial_health.copy())
            snapshot["serialHealth"]["lastPollDurationMs"] = round((time.monotonic() - started) * 1000, 1)
            snapshot["effectiveControl"] = effective_control(snapshot, command)
            return self.decorate_snapshot(snapshot)


class DbusPublisher:
    """Small read-only virtual battery publisher for the Cerbo system bus."""

    def __init__(
        self,
        service_name: str = VIRTUAL_BATTERY_SERVICE,
        device_instance: int = 100,
        product_name: str = "Dyness RS485 virtual BMS",
        publish: bool = True,
        guardian: bool = False,
    ) -> None:
        self.items: dict[str, Any] = {}
        self.available = False
        self.publishing = False
        try:
            import dbus  # type: ignore
            import dbus.mainloop.glib  # type: ignore
            import dbus.service  # type: ignore
            from gi.repository import GLib  # type: ignore
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

            class Item(dbus.service.Object):  # type: ignore
                def __init__(self, bus, path, initial):
                    super().__init__(bus, path)
                    self.value = initial

                @dbus.service.method("com.victronenergy.BusItem", out_signature="v")
                def GetValue(self):
                    return self.value

                @dbus.service.method("com.victronenergy.BusItem", in_signature="v", out_signature="i")
                def SetValue(self, value):
                    # The virtual BMS has no charger-control write path. The
                    # controller communicates through its command file and
                    # only telemetry/limits are exposed here.
                    return 1

                @dbus.service.method("com.victronenergy.BusItem", out_signature="s")
                def GetText(self):
                    return str(self.value)

            class Root(dbus.service.Object):  # type: ignore
                def __init__(self, bus, values):
                    super().__init__(bus, "/")
                    self.values = values

                def properties(self):
                    result = {}
                    for path, value in self.values.items():
                        result[path] = dbus.Dictionary(
                            {"Value": value.value, "Text": dbus.String(str(value.value))},
                            signature="sv",
                        )
                    return dbus.Dictionary(result, signature="sa{sv}")

                @dbus.service.method("com.victronenergy.BusItem", out_signature="a{sa{sv}}")
                def GetItems(self):
                    return self.properties()

                @dbus.service.method("com.victronenergy.BusItem", out_signature="a{sv}")
                def GetValue(self):
                    return dbus.Dictionary(
                        {path.lstrip("/"): value.value for path, value in self.values.items()},
                        signature="sv",
                    )

                @dbus.service.method("com.victronenergy.BusItem", in_signature="a{sv}", out_signature="i")
                def SetValues(self, values):
                    # No control/configuration writes are accepted through the
                    # virtual BMS root.
                    return -1

                @dbus.service.signal("com.victronenergy.BusItem", signature="a{sa{sv}}")
                def ItemsChanged(self, values):
                    return values

            self.dbus = dbus
            self.Item = Item
            self.Root = Root
            self.bus = dbus.SystemBus()
            self.system_root = self.bus.get_object("com.victronenergy.system", "/")
            self.objects = {}
            if not publish:
                self.available = True
                return
            self.name = dbus.service.BusName(service_name, self.bus)
            defaults = {
                "/DeviceInstance": dbus.UInt32(device_instance),
                "/Connected": dbus.Boolean(True),
                "/ProductId": dbus.UInt32(0xC065),
                "/ProductName": dbus.String(product_name),
                "/Mgmt/Connection": dbus.String("Dyness RS485"),
                "/FirmwareVersion": dbus.String("0.1.0"),
                "/Dc/0/Voltage": dbus.Double(53.0),
                "/Dc/0/Current": dbus.Double(0.0),
                "/Dc/0/Power": dbus.Double(0.0),
                "/Info/MaxChargeVoltage": dbus.Double(53.0),
                "/Info/MaxChargeCurrent": dbus.Double(0.0),
                "/Info/MaxDischargeCurrent": dbus.Double(0.0),
                "/Bms/AllowToCharge": dbus.Boolean(False),
                "/Bms/AllowToDischarge": dbus.Boolean(False),
                "/Bms/StatusRaw": dbus.UInt32(0),
                "/Bms/Status": dbus.String("permissions"),
                "/Bms/ChargeEnabled": dbus.Boolean(False),
                "/Bms/DischargeEnabled": dbus.Boolean(False),
                "/Bms/StrongCharge": dbus.Boolean(False),
                "/Bms/FullCharge": dbus.Boolean(False),
                "/Bms/UnknownStatusBits": dbus.UInt32(0),
                "/State": dbus.String("RS485 unavailable"),
                "/Control/RequestedChargeVoltage": dbus.Double(0.0),
                "/Control/RequestedChargeCurrent": dbus.Double(0.0),
                "/Control/RequestedChargeEnabled": dbus.Boolean(False),
                "/Control/CommandFresh": dbus.Boolean(False),
                "/Control/CommandAge": dbus.UInt32(0),
                "/Control/ThermalFactor": dbus.Double(0.0),
                "/Control/OutputValid": dbus.Boolean(False),
                "/Control/ArbitrationReason": dbus.String("RS485 unavailable"),
                "/Control/CommandReason": dbus.String("none"),
                "/Control/BmsChargeVoltage": dbus.Double(0.0),
                "/Control/BmsChargeCurrent": dbus.Double(0.0),
                "/Control/ActiveBmsService": dbus.String(""),
                "/Control/ActiveBmsInstance": dbus.Int32(-1),
                "/Control/ActiveBmsLabel": dbus.String("BMS selection unknown"),
                "/Control/AuthorityState": dbus.String("UNKNOWN"),
                "/Control/ControllerRequestApplied": dbus.Boolean(False),
            }
            if guardian:
                defaults.update({
                    "/Alarms/Communication": dbus.UInt32(2),
                    "/Guardian/Mode": dbus.String("BOOTSTRAP"),
                    "/Guardian/Ready": dbus.Boolean(False),
                    "/Guardian/SourceAgeMs": dbus.UInt32(0xFFFFFFFF),
                    "/Guardian/LastValidTimestamp": dbus.String(""),
                    "/Guardian/Reason": dbus.String("waiting for valid RS485 telemetry"),
                })
            for path, value in defaults.items():
                self.objects[path] = Item(self.bus, path, value)
            self.root = Root(self.bus, self.objects)
            self.available = True
            self.publishing = True
            threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
        except Exception:
            self.available = False

    def read_active_selection(self) -> dict[str, Any]:
        if not self.available:
            return active_bms_selection({})
        try:
            normalized = {}
            for key in ("ActiveBatteryService", "ActiveBmsService", "ActiveBmsInstance"):
                item = self.bus.get_object("com.victronenergy.system", f"/{key}")
                normalized[key] = item.GetValue(
                    dbus_interface="com.victronenergy.BusItem"
                )
            return active_bms_selection(normalized)
        except Exception:
            return active_bms_selection({})

    def read_charge_control_settings(self) -> dict[str, Any]:
        if not self.available:
            return charge_control_settings({})
        try:
            values = {}
            for key in ("MaxChargeVoltage", "MaxChargeCurrent"):
                item = self.bus.get_object(
                    "com.victronenergy.settings", f"/Settings/SystemSetup/{key}"
                )
                values[key] = item.GetValue(
                    dbus_interface="com.victronenergy.BusItem"
                )
            return charge_control_settings(values)
        except Exception:
            return charge_control_settings({})

    def annotate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        value = dict(snapshot)
        value["activeBms"] = self.read_active_selection()
        value["chargeControlSettings"] = self.read_charge_control_settings()
        return value

    def update(self, snapshot: dict[str, Any]) -> None:
        if not self.available or not self.publishing:
            return
        dbus = self.dbus
        system = snapshot.get("system") or {}
        limits = snapshot.get("limits") or {}
        aggregate = snapshot.get("aggregate") or {}
        control = snapshot.get("effectiveControl") or {}
        status_flags = limits.get("statusFlags") or {}
        selection = snapshot.get("activeBms") or self.read_active_selection()
        voltage = system.get("voltage61") if snapshot.get("valid") else 53.0
        current = aggregate.get("summedBatteryCurrent") if snapshot.get("valid") else 0.0
        ccl = control.get("effectiveChargeCurrent")
        dcl = control.get("effectiveDischargeCurrent")
        cvl = control.get("effectiveChargeVoltage")
        requested_voltage = control.get("requestedVoltage")
        requested_current = control.get("requestedCurrent")
        command_age = control.get("commandAgeMs")
        command_age = max(0, min(int(command_age), 0xFFFFFFFF)) if isinstance(command_age, (int, float)) else 0
        active_instance = selection.get("activeBmsInstance")
        active_instance = active_instance if isinstance(active_instance, int) else -1
        state_text = virtual_bms_state(control, selection)
        soc61 = system.get("soc61")
        values = {
            "/Dc/0/Voltage": dbus.Double(float(voltage or 53.0)),
            "/Dc/0/Current": dbus.Double(float(current or 0.0)),
            "/Dc/0/Power": dbus.Double(float((voltage or 53.0) * (current or 0.0))),
            "/Info/MaxChargeVoltage": dbus.Double(float(cvl or 55.0)),
            "/Info/MaxChargeCurrent": dbus.Double(float(ccl or 0.0)),
            "/Info/MaxDischargeCurrent": dbus.Double(float(dcl or 0.0)),
            "/Bms/AllowToCharge": dbus.Boolean(bool(control.get("effectiveChargeEnabled"))),
            "/Bms/AllowToDischarge": dbus.Boolean(bool(control.get("effectiveDischargeEnabled"))),
            "/Bms/StatusRaw": dbus.UInt32(int(limits.get("statusRaw") or 0)),
            "/Bms/Status": dbus.String("permissions"),
            "/Bms/ChargeEnabled": dbus.Boolean(bool(control.get("effectiveChargeEnabled"))),
            "/Bms/DischargeEnabled": dbus.Boolean(bool(control.get("effectiveDischargeEnabled"))),
            "/Bms/StrongCharge": dbus.Boolean(bool(status_flags.get("strongCharge"))),
            "/Bms/FullCharge": dbus.Boolean(bool(status_flags.get("fullCharge"))),
            "/Bms/UnknownStatusBits": dbus.UInt32(int(status_flags.get("unknownReservedBits") or 0)),
            # The D-Bus service remains connected and advertises the explicit
            # fallback even while telemetry itself is invalid.
            "/Connected": dbus.Boolean(True),
            "/State": dbus.String(state_text if control else snapshot.get("reason") or "RS485 unavailable"),
            "/Control/RequestedChargeVoltage": dbus.Double(float(requested_voltage or 0.0)),
            "/Control/RequestedChargeCurrent": dbus.Double(float(requested_current or 0.0)),
            "/Control/RequestedChargeEnabled": dbus.Boolean(bool(control.get("controllerChargeEnabled"))),
            "/Control/CommandFresh": dbus.Boolean(bool(control.get("commandFresh"))),
            "/Control/CommandAge": dbus.UInt32(command_age),
            "/Control/ThermalFactor": dbus.Double(float(control.get("thermalFactor") or 0.0)),
            "/Control/OutputValid": dbus.Boolean(bool(control.get("outputValid"))),
            "/Control/ArbitrationReason": dbus.String(str(control.get("reason") or "none")),
            "/Control/CommandReason": dbus.String(str(control.get("commandReason") or "none")),
            "/Control/BmsChargeVoltage": dbus.Double(float(control.get("bmsChargeVoltage") or 0.0)),
            "/Control/BmsChargeCurrent": dbus.Double(float(control.get("bmsChargeCurrent") or 0.0)),
            "/Control/ActiveBmsService": dbus.String(str(selection.get("activeBmsService") or "")),
            "/Control/ActiveBmsInstance": dbus.Int32(active_instance),
            "/Control/ActiveBmsLabel": dbus.String(str(selection.get("label") or "BMS selection unknown")),
            "/Control/AuthorityState": dbus.String(str(control.get("authorityState") or "UNKNOWN")),
            "/Control/ControllerRequestApplied": dbus.Boolean(bool(control.get("controllerRequestApplied"))),
        }
        # SOC is published only from a valid addressed CID2=61 value.  A 0%
        # SOC is never published while telemetry is unavailable: an absent
        # /Soc path makes the system fall back to voltage-based SOC instead of
        # triggering empty-battery protection, and during invalid windows the
        # last-known-good SOC stays in place.
        if soc61_is_valid(soc61):
            values["/Soc"] = dbus.Double(float(soc61))
        self._apply_values(values)

    def update_guardian_status(
        self,
        mode: str,
        ready: bool,
        source_age_ms: int | None,
        last_valid_timestamp: int | None,
        reason: str,
    ) -> None:
        if not self.available or not self.publishing:
            return
        dbus = self.dbus
        bounded_age = 0xFFFFFFFF if source_age_ms is None else max(
            0, min(int(source_age_ms), 0xFFFFFFFF)
        )
        self._apply_values({
            "/Alarms/Communication": dbus.UInt32(0 if mode == "NORMAL" else 2),
            "/Guardian/Mode": dbus.String(mode),
            "/Guardian/Ready": dbus.Boolean(ready),
            "/Guardian/SourceAgeMs": dbus.UInt32(bounded_age),
            "/Guardian/LastValidTimestamp": dbus.String(
                str(last_valid_timestamp) if last_valid_timestamp is not None else ""
            ),
            "/Guardian/Reason": dbus.String(reason),
        })

    def _apply_values(self, values: dict[str, Any]) -> None:
        """Create missing D-Bus paths lazily and update changed values."""
        dbus = self.dbus
        changed = dbus.Dictionary({}, signature="sa{sv}")
        for path, value in values.items():
            item = self.objects.get(path)
            if item is None:
                item = self.Item(self.bus, path, value)
                self.objects[path] = item
                changed[path] = dbus.Dictionary(
                    {"Value": value, "Text": dbus.String(str(value))},
                    signature="sv",
                )
            elif item.value != value:
                item.value = value
                changed[path] = dbus.Dictionary(
                    {"Value": value, "Text": dbus.String(str(value))},
                    signature="sv",
                )
        if changed:
            self.root.ItemsChanged(changed)


def emit_snapshot(snapshot: dict[str, Any], quiet: bool, once: bool) -> None:
    """Write diagnostic JSON unless a long-running service requested quiet output."""
    if once or not quiet:
        print(json.dumps(snapshot, separators=(",", ":")), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=0.7)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--publisher-mode",
        choices=("integrated", "telemetry-only"),
        default="integrated",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress continuous JSON snapshots on stdout (ignored with --once)",
    )
    args = parser.parse_args()
    store = JsonlStore(args.state_dir)
    store.ensure_json("cerbo-balancer-config.json", {"automaticBalancingEnabled": True})
    store.ensure_json("cerbo-balancer-state.json", {
        "version": 5,
        "state": "NORMAL",
        "automaticBalancingEnabled": True,
    })
    store.ensure_json("cerbo-balancer-command.json", {"version": 0})
    store.ensure_json("cerbo-balancer-csv-logging.json", {"enabled": False, "filename": None})
    store.ensure_json("cerbo-balancer-rs485-inventory.json", {"version": 1, "activeAddresses": [], "pendingRemoval": [], "lastSeenAt": {}})
    store.ensure_json("cerbo-balancer-sessions.jsonl", {})
    poller = ReadOnlyPoller(args.port, args.baud, args.timeout, store.read_json("cerbo-balancer-rs485-inventory.json"))
    dbus_publisher = DbusPublisher(publish=args.publisher_mode == "integrated")
    csv_logger = CsvLogger(args.state_dir)
    telemetry_store = RollingTelemetryStore(args.state_dir)
    while True:
        cycle_started = time.monotonic()
        command = store.read_json("cerbo-balancer-command.json")
        snapshot = dbus_publisher.annotate_snapshot(poller.poll(command))
        # D-Bus UI limits are sampled after the serial poll and must be part of
        # the same cycle's final virtual-BMS arbitration.
        snapshot["effectiveControl"] = effective_control(snapshot, command)
        csv_logger.write(snapshot, store.read_json("cerbo-balancer-csv-logging.json"))
        store.write_json("cerbo-balancer-rs485-inventory.json", poller.export_inventory())
        published_snapshot = telemetry_store.record(snapshot)
        dbus_publisher.update(published_snapshot)
        emit_snapshot(published_snapshot, args.quiet, args.once)
        if args.once:
            return 0
        # Keep the requested cadence measured from the start of each poll.
        # A slow but still bounded read starts the next cycle immediately
        # rather than adding another full interval of avoidable telemetry age.
        time.sleep(max(0.0, args.interval - (time.monotonic() - cycle_started)))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
