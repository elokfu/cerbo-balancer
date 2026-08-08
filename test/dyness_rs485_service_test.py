import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dyness_rs485_service import JsonlStore, ReadOnlyPoller, effective_control  # noqa: E402


class DynessServiceTests(unittest.TestCase):
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
        snapshot = ReadOnlyPoller("C:\\missing-dyness-rs485", 115200, 0.01).poll()
        self.assertFalse(snapshot["valid"])
        self.assertFalse(snapshot["cellTelemetryValid"])
        self.assertEqual(snapshot["effectiveControl"]["effectiveChargeCurrent"], 0.0)
        self.assertEqual(snapshot["effectiveControl"]["effectiveChargeVoltage"], 53.0)

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

    def test_store_creates_runtime_files_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlStore(directory)
            store.ensure_json("cerbo-balancer-config.json", {"mode": "TEST"})
            self.assertEqual(store.read_json("cerbo-balancer-config.json")["mode"], "TEST")


if __name__ == "__main__":
    unittest.main()
