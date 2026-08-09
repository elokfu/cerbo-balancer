import sys
import csv
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dyness_rs485_service as service  # noqa: E402
from dyness_rs485_service import CsvLogger, JsonlStore, ReadOnlyPoller, effective_control  # noqa: E402
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

    def test_effective_control_exposes_requested_and_arbitrated_values(self):
        snapshot = {
            "valid": True,
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {
                    "chargeEnabled": True,
                    "dischargeEnabled": True,
                },
            },
            "batteries": [
                {"temperatures": [25.0, 26.0, 27.0, 26.0, 25.0]},
            ],
        }
        command = {
            "mode": "ACTIVE",
            "timestamp": 999000,
            "requestedVoltage": 57.0,
            "requestedCurrent": 80.0,
            "chargeEnabled": True,
            "reason": "BALANCE_CURRENT_CONTROL",
        }
        with patch.object(service, "now_ms", return_value=1000000):
            control = effective_control(snapshot, command)

        self.assertEqual(control["requestedVoltage"], 57.0)
        self.assertEqual(control["requestedCurrent"], 80.0)
        self.assertEqual(control["commandReason"], "BALANCE_CURRENT_CONTROL")
        self.assertEqual(control["commandAgeMs"], 1000)
        self.assertEqual(control["effectiveChargeVoltage"], 56.5)
        self.assertEqual(control["effectiveChargeCurrent"], 56.0)
        self.assertTrue(control["effectiveChargeEnabled"])
        self.assertTrue(control["allowToCharge"])
        self.assertEqual(control["reason"], "BMS_OR_SAFETY_LIMIT")

    def test_effective_control_marks_zero_ccl_and_controller_inhibit(self):
        base = {
            "valid": True,
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 0.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {
                    "chargeEnabled": True,
                    "dischargeEnabled": True,
                },
            },
            "batteries": [{"temperatures": [25.0]}],
        }
        command = {
            "mode": "ACTIVE",
            "timestamp": 999000,
            "requestedVoltage": 55.2,
            "requestedCurrent": 20.0,
            "chargeEnabled": True,
        }
        with patch.object(service, "now_ms", return_value=1000000):
            zero_ccl = effective_control(base, command)
        self.assertEqual(zero_ccl["effectiveChargeCurrent"], 0.0)
        self.assertFalse(zero_ccl["effectiveChargeEnabled"])
        self.assertEqual(zero_ccl["reason"], "BMS_CCL_ZERO")

        command["chargeEnabled"] = False
        with patch.object(service, "now_ms", return_value=1000000):
            inhibited = effective_control({**base, "limits": {**base["limits"], "chargeCurrent": 20.0}}, command)
        self.assertEqual(inhibited["requestedCurrent"], 20.0)
        self.assertEqual(inhibited["effectiveChargeCurrent"], 0.0)
        self.assertFalse(inhibited["effectiveChargeEnabled"])
        self.assertEqual(inhibited["reason"], "CONTROLLER_CHARGE_INHIBIT")

    def test_csv_logs_requested_and_effective_virtual_bms_output(self):
        snapshot = {
            "timestamp": 1000000,
            "valid": True,
            "serialPort": "/dev/ttyUSB0",
            "baud": 115200,
            "system": {"voltage61": 53.25, "soc61": 98, "maximumBmsTemperature61": 31.5},
            "limits": {
                "chargeVoltage": 56.5,
                "chargeCurrent": 56.0,
                "dischargeCurrentSigned": -198.8,
                "statusFlags": {"chargeEnabled": True, "dischargeEnabled": True},
            },
            "aggregate": {"vmin": 3.317, "vmax": 3.329, "spread": 0.012, "summedBatteryCurrent": 1.5},
            "inventory": {"activeAddresses": [2]},
            "batteries": [{
                "address": 2,
                "valid": True,
                "voltage": 53.25,
                "current": 1.5,
                "effectiveCells": [{"index": index, "voltage": 3.32} for index in range(1, 17)],
                "temperatures": [25.0, 26.0, 27.0, 26.0, 25.0],
            }],
        }
        command = {
            "mode": "ACTIVE",
            "timestamp": 999000,
            "requestedVoltage": 57.0,
            "requestedCurrent": 80.0,
            "chargeEnabled": True,
            "reason": "BALANCE_CURRENT_CONTROL",
        }
        with patch.object(service, "now_ms", return_value=1000000):
            snapshot["effectiveControl"] = effective_control(snapshot, command)
        with tempfile.TemporaryDirectory() as root:
            logger = CsvLogger(root)
            result = logger.write(snapshot, {"enabled": True, "filename": "arbitration.csv"})
            self.assertTrue(result["written"])
            contents = (Path(root) / service.CSV_LOG_DIRECTORY / "arbitration.csv").read_text(encoding="utf-8")
            self.assertIn("# virtual_bms_service=com.victronenergy.battery.rs485_dyness", contents)
            self.assertIn("controller_requested_voltage_v", contents)
            self.assertIn("virtual_bms_effective_ccl_a", contents)
            rows = list(csv.DictReader(line for line in contents.splitlines() if not line.startswith("#")))
            self.assertEqual(rows[0]["controller_requested_voltage_v"], "57.00")
            self.assertEqual(rows[0]["virtual_bms_effective_cvl_v"], "56.50")
            self.assertEqual(rows[0]["virtual_bms_effective_ccl_a"], "56.00")
            self.assertEqual(rows[0]["virtual_bms_arbitration_reason"], "BMS_OR_SAFETY_LIMIT")

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

        def query(_port, address, cid2, timeout=None):
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
            if cid2 == 0x42 and timeout is not None:
                discovery_timeouts.append(timeout)
            if cid2 == 0x42 and address == 3:
                return response(3, 0x42, pack_info_3)
            return frames[cid2] if address == 2 else None

        poller.query = query
        rounds = (len(service.EXPECTED_ADDRESSES) + service.NORMAL_DISCOVERY_PROBES_PER_POLL - 1) // service.NORMAL_DISCOVERY_PROBES_PER_POLL
        snapshots = [poller.poll() for _ in range(rounds)]

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

    def test_csv_schema_is_fixed_to_start_inventory_and_stops_on_missing_battery(self):
        self.assertEqual(service.CsvLogger._format_voltage(3343), "3.34")
        self.assertEqual(service.CsvLogger._format_voltage(53.25), "53.25")
        self.assertEqual(service.CsvLogger._format_cell_voltage(3343), "3.343")
        self.assertEqual(service.CsvLogger._format_voltage(3.335), "3.33")
        self.assertEqual(service.CsvLogger._format_voltage(3.335, 3), "3.335")
        self.assertEqual(service.CsvLogger._format_spread_mv(0.007999999), "8")
        columns = service.CsvLogger.columns_for((2,))
        self.assertLess(columns.index("battery_02_cell_01_v"), columns.index("ccl_a"))
        self.assertLess(columns.index("battery_02_temp_05_c"), columns.index("controller_requested_voltage_v"))

        def snapshot(addresses, timestamp=1_700_000_000_000, valid=True):
            return {
                "timestamp": timestamp,
                "valid": valid,
                "serialPort": "/dev/ttyUSB0",
                "baud": 115200,
                "inventory": {"activeAddresses": list(addresses)},
                "system": {"voltage61": 53.25, "soc61": 98},
                "limits": {"chargeCurrent": 56.0, "dischargeCurrentSigned": -198.8,
                           "statusFlags": {"chargeEnabled": True, "dischargeEnabled": True}},
                "aggregate": {"vmin": 3.317, "vmax": 3.329, "spread": 0.012,
                              "summedBatteryCurrent": 1.5},
                "batteries": [{"address": address, "valid": True, "voltage": 53.25,
                               "current": 1.5,
                               "effectiveCells": [{"index": 1, "voltage": 3317},
                                                   {"index": 2, "voltage": 3.329}],
                               "temperatures": [26.0, 26.1, 26.2, 26.3, 26.4]}
                              for address in addresses],
            }

        with tempfile.TemporaryDirectory() as directory:
            logger = service.CsvLogger(directory)
            self.assertRegex(logger._format_timestamp(0), r"^\d{2}:\d{2}:\d{2}$")
            control = {"enabled": True, "filename": "session.csv"}
            first = logger.write(snapshot([2, 3]), control)
            second = logger.write(snapshot([2, 3, 4], 1_700_000_006_000), control)
            stopped = logger.write(snapshot([2], 1_700_000_012_000, valid=False), control)
            after_stop = logger.write(snapshot([2, 3], 1_700_000_018_000), control)
            target = Path(directory) / service.CSV_LOG_DIRECTORY / "session.csv"
            lines = target.read_text(encoding="utf-8").splitlines()

            self.assertTrue(first["written"])
            self.assertTrue(second["written"])
            self.assertTrue(stopped["stopped"])
            self.assertTrue(after_stop["stopped"])
            self.assertIn("# initial_addresses=2,3", lines)
            header = next(line for line in lines if not line.startswith("#"))
            self.assertIn("battery_02_voltage_v", header)
            self.assertIn("battery_03_voltage_v", header)
            self.assertNotIn("battery_04_voltage_v", header)
            self.assertIn("battery_02_temp_05_c", header)
            self.assertNotIn("battery_02_temp_06_c", header)
            self.assertIn("timestamp,sample_number,", header)
            self.assertIn("vmin_v,vmax_v,spread_mv", header)
            rows = [line for line in lines if line and not line.startswith("#")]
            self.assertEqual(len(rows), 3)
            self.assertRegex(rows[1].split(",", 2)[0], r"^\d{2}:\d{2}:\d{2}$")
            self.assertEqual(rows[2].split(",", 2)[1], "2")
            self.assertIn(",3.317,3.329,", rows[1])
            self.assertIn(",3.317,3.329,", rows[2])
            csv_rows = list(csv.DictReader(line for line in rows))
            self.assertEqual(csv_rows[0]["vmin_v"], "3.317")
            self.assertEqual(csv_rows[0]["vmax_v"], "3.329")
            self.assertEqual(csv_rows[0]["battery_02_cell_01_v"], "3.317")


if __name__ == "__main__":
    unittest.main()
