import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dyness_rs485_service as service  # noqa: E402
from dyness_rs485_service import JsonlStore, ReadOnlyPoller, effective_control  # noqa: E402
from dyness_rs485_protocol import checksum, length_field  # noqa: E402


def response(address, cid2, info):
    body = f"20{address:02X}4600{length_field(len(info))}{info}"
    return f"~{body}{checksum(body)}\r".encode("ascii")


class DynessServiceTests(unittest.TestCase):
    def test_serial_session_is_reused_until_disconnect(self):
        class FakeSerialPort:
            is_open = True

            def __init__(self, *args, **kwargs):
                self.closed = False

            def reset_input_buffer(self):
                return None

            def close(self):
                self.closed = True
                self.is_open = False

        class FakeSerialModule:
            PARITY_NONE = "N"
            Serial = FakeSerialPort

        poller = ReadOnlyPoller("C:\\fake-dyness-rs485", 115200, 0.01)
        with patch.object(service, "serial", FakeSerialModule), patch.object(service.os.path, "exists", return_value=True):
            first = poller._open_serial()
            second = poller._open_serial()
            self.assertIs(first, second)
            poller._close_serial()
            self.assertIsNone(poller.serial_port)

    def test_missing_known_battery_is_pending_before_removal(self):
        poller = ReadOnlyPoller("C:\\missing-dyness-rs485", 115200, 0.01)
        poller.apply_discovery([2, 3], 1000)
        poller.apply_discovery([3], 2000)

        self.assertEqual(poller.active_addresses, [3])
        self.assertEqual(poller.pending_removal[2]["missedScans"], 1)
        self.assertEqual(poller.inventory_snapshot()["scanIntervalSeconds"], 10.0)

        for attempt in range(2, 10):
            poller.apply_discovery([3], 2000 + attempt)
        self.assertIn(2, poller.pending_removal)
        self.assertEqual(poller.pending_removal[2]["missedScans"], 9)

        poller.apply_discovery([3], 2010)
        self.assertNotIn(2, poller.pending_removal)
        self.assertEqual(poller.active_addresses, [3])
        self.assertEqual(poller.inventory_snapshot()["scanIntervalSeconds"], 60.0)

    def test_returning_battery_is_restored_and_new_battery_is_added(self):
        poller = ReadOnlyPoller("C:\\missing-dyness-rs485", 115200, 0.01)
        poller.apply_discovery([2, 3], 1000)
        poller.apply_discovery([3], 2000)
        poller.apply_discovery([2, 3, 4], 3000)

        self.assertEqual(poller.active_addresses, [2, 3, 4])
        self.assertNotIn(2, poller.pending_removal)
        self.assertEqual(poller.inventory_snapshot()["scanIntervalSeconds"], 60.0)

    def test_inventory_retry_state_survives_reload(self):
        inventory = {
            "activeAddresses": [3],
            "pendingRemoval": [{"address": 2, "missedScans": 4, "lastMissingAt": 2000}],
            "lastSeenAt": {"2": 1000, "3": 2000},
        }
        poller = ReadOnlyPoller("C:\\missing-dyness-rs485", 115200, 0.01, inventory)

        self.assertEqual(poller.active_addresses, [3])
        self.assertEqual(poller.pending_removal[2]["missedScans"], 4)
        self.assertEqual(poller.inventory_snapshot()["discoveryMode"], "recovery")

    def test_disconnected_adapter_is_explicitly_safe(self):
        poller = ReadOnlyPoller("C:\\missing-dyness-rs485", 115200, 0.01)
        poller.apply_discovery([2], 1000)
        snapshot = poller.poll()
        self.assertFalse(snapshot["valid"])
        self.assertFalse(snapshot["cellTelemetryValid"])
        self.assertEqual(snapshot["effectiveControl"]["effectiveChargeCurrent"], 0.0)
        self.assertEqual(snapshot["effectiveControl"]["effectiveChargeVoltage"], 53.0)
        self.assertEqual(poller.active_addresses, [2])
        self.assertEqual(snapshot["serialHealth"]["state"], "disconnected")

    def test_status44_is_polled_every_five_seconds_without_affecting_validity(self):
        poller = ReadOnlyPoller("C:\\fake-dyness-rs485", 115200, 0.01)
        poller.active_addresses = [2]
        poller.next_discovery_at = time.monotonic() + 60

        system_data = bytearray(49)
        system_data[0:2] = (54480).to_bytes(2, "big")
        system_data[4] = 100
        limits_info = f"{56500:04X}{48000:04X}{560:04X}{0xF83C:04X}C0"
        cells = "".join(f"{3500:04X}" for _ in range(16))
        pack_info = f"000210{cells}00{0:04X}{56000:04X}"
        status_info = "00020000000000F70FE98140"
        frames = {
            0x61: response(2, 0x61, system_data.hex().upper()),
            0x63: response(2, 0x63, limits_info),
            0x42: response(2, 0x42, pack_info),
            0x44: response(2, 0x44, status_info),
        }
        calls = []

        poller._open_serial = lambda: object()
        poller._owner_conflict = lambda: False

        def query(_port, address, cid2):
            calls.append(cid2)
            return frames[cid2]

        poller.query = query
        first = poller.poll()
        self.assertTrue(first["valid"])
        self.assertEqual(first["batteries"][0]["status44"]["status2"]["raw"], 0x0F)
        self.assertIn(0x44, calls)

        calls.clear()
        second = poller.poll()
        self.assertTrue(second["valid"])
        self.assertNotIn(0x44, calls)

        poller.next_status_at = time.monotonic()
        third = poller.poll()
        self.assertTrue(third["valid"])
        self.assertIn(0x44, calls)

    def test_incremental_discovery_does_not_block_active_telemetry(self):
        poller = ReadOnlyPoller("C:\\fake-dyness-rs485", 115200, 0.01)
        poller.active_addresses = [2]
        poller.next_discovery_at = time.monotonic()

        system_data = bytearray(49)
        system_data[0:2] = (54480).to_bytes(2, "big")
        system_data[4] = 100
        limits_info = f"{56500:04X}{48000:04X}{560:04X}{0xF83C:04X}C0"
        cells = "".join(f"{3500:04X}" for _ in range(16))
        pack_info = f"000210{cells}00{0:04X}{56000:04X}"
        pack_info_3 = f"000310{cells}00{0:04X}{56000:04X}"
        status_info = "00020000000000F70FE98140"
        frames = {
            0x61: response(2, 0x61, system_data.hex().upper()),
            0x63: response(2, 0x63, limits_info),
            0x42: response(2, 0x42, pack_info),
            0x44: response(2, 0x44, status_info),
        }
        discovery_timeouts = []

        poller._open_serial = lambda: object()
        poller._owner_conflict = lambda: False

        def query(_port, address, cid2, timeout=None):
            if timeout is not None:
                discovery_timeouts.append(timeout)
            if cid2 == 0x42 and address == 3:
                return response(3, 0x42, pack_info_3)
            return frames[cid2] if address == 2 else None

        poller.query = query
        snapshots = [poller.poll() for _ in range(len(service.EXPECTED_ADDRESSES))]

        self.assertTrue(all(snapshot["valid"] for snapshot in snapshots), [
            (snapshot["valid"], snapshot["reason"], snapshot.get("expectedAddresses"),
             [battery["address"] for battery in snapshot["batteries"]])
            for snapshot in snapshots
        ])
        self.assertTrue(snapshots[0]["inventory"]["scanInProgress"])
        self.assertEqual(snapshots[-1]["discovery"]["scanType"], "incremental-complete")
        self.assertEqual(poller.active_addresses, [2, 3])
        self.assertEqual(snapshots[-1]["expectedAddresses"], [2])
        self.assertTrue(all(timeout == service.DISCOVERY_QUERY_TIMEOUT for timeout in discovery_timeouts))

    def test_active_command_is_arbitrated_by_bms_limits_and_temperature(self):
        snapshot = {
            "valid": True,
            "batteries": [{"temperatures": [50.0]}],
            "limits": {"chargeVoltage": 56.4, "chargeCurrent": 56.0,
                       "dischargeCurrentSigned": -198.8},
        }
        command = {"mode": "ACTIVE", "timestamp": 0,
                   "requestedVoltage": 56.8, "requestedCurrent": 100.0}
        result = effective_control(snapshot, command)
        self.assertEqual(result["mode"], "TEST")  # deliberately stale command
        command["timestamp"] = __import__("time").time_ns() // 1_000_000
        result = effective_control(snapshot, command)
        self.assertEqual(result["mode"], "ACTIVE")
        self.assertEqual(result["effectiveChargeVoltage"], 56.4)
        self.assertEqual(result["effectiveChargeCurrent"], 28.0)

    def test_bms_permissions_block_the_affected_direction(self):
        snapshot = {
            "valid": True,
            "batteries": [{"temperatures": [25.0]}],
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {
                    "chargeEnabled": False,
                    "dischargeEnabled": False,
                },
            },
        }
        result = effective_control(snapshot, None)
        self.assertEqual(result["effectiveChargeCurrent"], 0.0)
        self.assertEqual(result["effectiveDischargeCurrent"], 0.0)
        self.assertTrue(result["chargeBlockedByStatus"])
        self.assertTrue(result["dischargeBlockedByStatus"])

    def test_protection_status_clamps_voltage_and_permissions(self):
        snapshot = {
            "valid": True,
            "batteries": [{"temperatures": [25.0]}],
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {"chargeEnabled": False, "dischargeEnabled": True},
            },
        }
        result = effective_control(snapshot, None)
        self.assertEqual(result["effectiveChargeVoltage"], 53.0)
        self.assertEqual(result["effectiveChargeCurrent"], 0.0)
        self.assertEqual(result["effectiveDischargeCurrent"], 198.8)

    def test_hot_battery_stops_charge_but_preserves_discharge(self):
        snapshot = {
            "valid": True,
            "batteries": [{"temperatures": [55.0]}],
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {"chargeEnabled": True, "dischargeEnabled": True},
            },
        }
        result = effective_control(snapshot, None)
        self.assertEqual(result["effectiveChargeCurrent"], 0.0)
        self.assertEqual(result["effectiveDischargeCurrent"], 198.8)

    def test_active_controller_can_inhibit_charge_without_affecting_discharge(self):
        snapshot = {
            "valid": True,
            "batteries": [{"temperatures": [25.0]}],
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {"chargeEnabled": True, "dischargeEnabled": True},
            },
        }
        command = {
            "mode": "ACTIVE",
            "timestamp": __import__("time").time_ns() // 1_000_000,
            "requestedVoltage": 56.5,
            "requestedCurrent": 0.0,
            "chargeEnabled": False,
            "reason": "BALANCE_DISCHARGE_RECOVERY",
        }
        result = effective_control(snapshot, command)
        self.assertEqual(result["effectiveChargeCurrent"], 0.0)
        self.assertTrue(result["chargeBlockedByController"])
        self.assertEqual(result["effectiveDischargeCurrent"], 198.8)

    def test_store_creates_runtime_files_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlStore(directory)
            store.ensure_json("cerbo-balancer-config.json", {"mode": "TEST"})
            self.assertEqual(store.read_json("cerbo-balancer-config.json")["mode"], "TEST")


if __name__ == "__main__":
    unittest.main()
