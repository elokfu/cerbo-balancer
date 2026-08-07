import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dyness_rs485_service import JsonlStore, ReadOnlyPoller, effective_control  # noqa: E402


class DynessServiceTests(unittest.TestCase):
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
