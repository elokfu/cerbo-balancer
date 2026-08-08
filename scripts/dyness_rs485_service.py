#!/usr/bin/env python3
"""Supervised, read-only Dyness RS485 telemetry and virtual BMS service.

The service only sends documented read requests (CID2 42, 44, 61, and 63). It
does not implement a serial write/configuration path or any charger write.
When pyserial or the Cerbo D-Bus runtime is unavailable it emits an explicit
unavailable snapshot and remains safe to run in shadow mode.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

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
EXPECTED_ADDRESSES = tuple(range(2, 17))
NORMAL_DISCOVERY_INTERVAL = 60.0
RECOVERY_DISCOVERY_INTERVAL = 10.0
MAX_DISCOVERY_MISSES = 10
SERIAL_RECONNECT_BACKOFF = 2.0
STATUS44_INTERVAL = 5.0


def now_ms() -> int:
    return int(time.time() * 1000)


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
        "aggregate": {"summedBatteryCurrent": None, "vmin": None, "vmax": None,
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
    bms_cvl = float(limits.get("chargeVoltage", 53.0)) if valid else 53.0
    bms_ccl = max(0.0, float(limits.get("chargeCurrent", 0.0))) if valid else 0.0
    bms_dcl = abs(float(limits.get("dischargeCurrentSigned", 0.0))) if valid else 0.0
    factor = thermal_factor(snapshot)
    charge_blocked = status_flags.get("chargeEnabled") is False
    discharge_blocked = status_flags.get("dischargeEnabled") is False
    if charge_blocked:
        bms_ccl = 0.0
    if discharge_blocked:
        bms_dcl = 0.0
    active_command = command if command and command.get("mode") == "ACTIVE" else None
    command_age = now_ms() - active_command["timestamp"] if active_command and isinstance(active_command.get("timestamp"), (int, float)) else None
    fresh_command = bool(command_age is not None and 0 <= command_age <= 5000)
    requested_voltage = float(active_command.get("requestedVoltage", 55.2)) if fresh_command else 55.2
    requested_current = float(active_command.get("requestedCurrent", 100.0)) if fresh_command else 100.0
    controller_charge_enabled = bool(active_command.get("chargeEnabled", True)) if fresh_command else True
    if not controller_charge_enabled:
        requested_current = 0.0
    return {
        "mode": "ACTIVE" if fresh_command and valid else "TEST",
        "commandFresh": fresh_command,
        "effectiveChargeVoltage": min(requested_voltage, bms_cvl, 53.0 if charge_blocked else 56.5) if valid else 53.0,
        "effectiveChargeCurrent": min(requested_current, bms_ccl, 100.0) * factor if valid else 0.0,
        "effectiveDischargeCurrent": bms_dcl,
        "thermalFactor": factor,
        "statusFlags": status_flags,
        "chargeBlockedByStatus": charge_blocked,
        "chargeBlockedByController": not controller_charge_enabled,
        "dischargeBlockedByStatus": discharge_blocked,
        "reason": None if valid else "RS485 telemetry invalid or stale",
    }


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
        remaining = max(0.0, self.next_discovery_at - time.monotonic()) if self.next_discovery_at else None
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

    def query(self, port: Any, address: int, cid2: int) -> bytes | None:
        port.reset_input_buffer()
        port.write(request(address, cid2))
        port.flush()
        deadline = time.monotonic() + self.timeout
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
            system_voltage = system_soc = None
            errors = []
            if system:
                try:
                    parsed_system = parse_system_61(system)
                    system_values = parsed_system.as_dict()
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
            discovery_due = time.monotonic() >= self.next_discovery_at
            status_due = time.monotonic() >= self.next_status_at
            addresses = EXPECTED_ADDRESSES if discovery_due else tuple(self.active_addresses)
            status_errors: list[str] = []
            for address in addresses:
                frame = self.query(port, address, 0x42)
                if not frame:
                    continue
                try:
                    battery = parse_pack_telemetry(frame, address).as_dict()
                    battery["systemVoltageDeltaMv"] = ((battery["voltage"] - system_voltage) * 1000
                                                         if system_voltage is not None else None)
                    status44 = self.status44_cache.get(address)
                    if status_due:
                        status_frame = self.query(port, address, 0x44)
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
            if status_due:
                self.next_status_at = time.monotonic() + STATUS44_INTERVAL
            if discovery_due:
                self.apply_discovery([item["address"] for item in batteries], now_ms())
            valid_batteries = [item for item in batteries if item["valid"]]
            expected_addresses = set(self.active_addresses)
            responding_addresses = {item["address"] for item in batteries}
            complete_battery_set = bool(expected_addresses) and not self.pending_removal and responding_addresses == expected_addresses
            cells = [(item["address"], cell) for item in valid_batteries for cell in item["effectiveCells"]]
            vmax_entry = max(cells, key=lambda entry: entry[1]["voltage"], default=(None, {"index": None, "voltage": None}))
            vmin_entry = min(cells, key=lambda entry: entry[1]["voltage"], default=(None, {"index": None, "voltage": None}))
            currents = [item["current"] for item in valid_batteries]
            temps = [temperature for item in valid_batteries for temperature in item["temperatures"]]
            all_expected_valid = complete_battery_set and len(valid_batteries) == len(batteries)
            snapshot = {
                "timestamp": now_ms(), "telemetryAge": 0, "valid": bool(system_voltage is not None and limits and all_expected_valid),
                "source": "rs485-dyness", "serialPort": self.port_name, "baud": self.baud,
                "reason": "; ".join(errors) if errors else None,
                "system": system_values, "limits": limits,
                "discovery": {"scannedAddresses": list(addresses),
                "respondingAddresses": [item["address"] for item in batteries], "respondingCount": len(batteries),
                "scanType": "full" if discovery_due else "active"},
                "batteries": batteries,
                "status44Health": {
                    "state": "healthy" if not status_errors else "degraded",
                    "errors": status_errors,
                    "pollIntervalSeconds": STATUS44_INTERVAL,
                },
                "aggregate": {"summedBatteryCurrent": sum(currents) if all_expected_valid else None,
                "vmin": vmin_entry[1]["voltage"], "vmax": vmax_entry[1]["voltage"],
                "spread": (vmax_entry[1]["voltage"] - vmin_entry[1]["voltage"] if cells else None),
                "minCellAddress": vmin_entry[0], "minCellIndex": vmin_entry[1]["index"],
                "maxCellAddress": vmax_entry[0], "maxCellIndex": vmax_entry[1]["index"],
                "minimumTemperature": min(temps) if temps else None, "maximumTemperature": max(temps) if temps else None,
                "averageTemperature": sum(temps) / len(temps) if temps else None},
                "cellTelemetryValid": bool(all_expected_valid and cells),
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

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self.available = False
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
            self.name = dbus.service.BusName("com.victronenergy.battery.rs485_dyness", self.bus)
            self.objects = {}
            defaults = {
                "/DeviceInstance": dbus.UInt32(100),
                "/ProductId": dbus.UInt32(0xC065),
                "/ProductName": dbus.String("Dyness RS485 virtual BMS"),
                "/FirmwareVersion": dbus.String("0.1.0"),
                "/Soc": dbus.Double(0.0),
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
                "/Connected": dbus.Boolean(False),
                "/State": dbus.String("RS485 unavailable"),
            }
            for path, value in defaults.items():
                self.objects[path] = Item(self.bus, path, value)
            self.root = Root(self.bus, self.objects)
            self.available = True
            threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
        except Exception:
            self.available = False

    def update(self, snapshot: dict[str, Any]) -> None:
        if not self.available:
            return
        dbus = self.dbus
        system = snapshot.get("system") or {}
        limits = snapshot.get("limits") or {}
        aggregate = snapshot.get("aggregate") or {}
        control = snapshot.get("effectiveControl") or {}
        status_flags = limits.get("statusFlags") or {}
        status_active = status_flags.get("active") or []
        voltage = system.get("voltage61") if snapshot.get("valid") else 53.0
        current = aggregate.get("summedBatteryCurrent") if snapshot.get("valid") else 0.0
        ccl = control.get("effectiveChargeCurrent") if snapshot.get("valid") else 0.0
        dcl = control.get("effectiveDischargeCurrent") if snapshot.get("valid") else 0.0
        cvl = control.get("effectiveChargeVoltage") if snapshot.get("valid") else 53.0
        values = {
            "/Soc": dbus.Double(float(system.get("soc61") or 0.0)),
            "/Dc/0/Voltage": dbus.Double(float(voltage or 53.0)),
            "/Dc/0/Current": dbus.Double(float(current or 0.0)),
            "/Dc/0/Power": dbus.Double(float((voltage or 53.0) * (current or 0.0))),
            "/Info/MaxChargeVoltage": dbus.Double(float(cvl or 53.0)),
            "/Info/MaxChargeCurrent": dbus.Double(float(ccl or 0.0)),
            "/Info/MaxDischargeCurrent": dbus.Double(float(dcl or 0.0)),
            "/Bms/AllowToCharge": dbus.Boolean(bool(snapshot.get("valid") and ccl is not None and ccl > 0 and status_flags.get("chargeEnabled") is True and not control.get("chargeBlockedByStatus") and not control.get("chargeBlockedByController"))),
            "/Bms/AllowToDischarge": dbus.Boolean(bool(snapshot.get("valid") and dcl is not None and dcl > 0 and status_flags.get("dischargeEnabled") is True and not control.get("dischargeBlockedByStatus"))),
            "/Bms/StatusRaw": dbus.UInt32(int(limits.get("statusRaw") or 0)),
            "/Bms/Status": dbus.String("permissions"),
            "/Bms/ChargeEnabled": dbus.Boolean(bool(status_flags.get("chargeEnabled"))),
            "/Bms/DischargeEnabled": dbus.Boolean(bool(status_flags.get("dischargeEnabled"))),
            "/Bms/StrongCharge": dbus.Boolean(bool(status_flags.get("strongCharge"))),
            "/Bms/FullCharge": dbus.Boolean(bool(status_flags.get("fullCharge"))),
            "/Bms/UnknownStatusBits": dbus.UInt32(int(status_flags.get("unknownReservedBits") or 0)),
            "/Connected": dbus.Boolean(bool(snapshot.get("valid"))),
            "/State": dbus.String(
                ", ".join(item["description"] for item in status_active)
                if status_active else "Valid RS485 telemetry" if snapshot.get("valid")
                else snapshot.get("reason") or "RS485 unavailable"
            ),
        }
        for path, value in values.items():
            if path in self.objects:
                self.objects[path].value = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=0.7)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    store = JsonlStore(args.state_dir)
    store.ensure_json("cerbo-balancer-config.json", {"mode": "TEST", "enabled": False})
    store.ensure_json("cerbo-balancer-state.json", {"version": 1, "state": "NORMAL", "mode": "TEST"})
    store.ensure_json("cerbo-balancer-command.json", {"version": 0, "mode": "TEST"})
    store.ensure_json("cerbo-balancer-rs485-inventory.json", {"version": 1, "activeAddresses": [], "pendingRemoval": [], "lastSeenAt": {}})
    store.ensure_json("cerbo-balancer-sessions.jsonl", {})
    poller = ReadOnlyPoller(args.port, args.baud, args.timeout, store.read_json("cerbo-balancer-rs485-inventory.json"))
    dbus_publisher = DbusPublisher()
    while True:
        snapshot = poller.poll(store.read_json("cerbo-balancer-command.json"))
        store.write_json("cerbo-balancer-rs485-inventory.json", poller.export_inventory())
        store.append("cerbo-balancer-telemetry.jsonl", snapshot)
        dbus_publisher.update(snapshot)
        print(json.dumps(snapshot, separators=(",", ":")), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
