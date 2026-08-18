import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dyness_rs485_protocol import decode_capacity_tail_42
from dyness_rs485_service import CapacitySocEstimator


def battery(address=2, remaining=277200, total=280000, current=0.0, vmax=3.49):
    cells = [{"index": index, "voltage": 3.40} for index in range(1, 17)]
    cells[-1]["voltage"] = vmax
    return {
        "address": address, "valid": True, "current": current,
        "remainingCapacityRawMah": remaining, "totalCapacityRawMah": total,
        "remainingCapacityAh": remaining / 1000, "totalCapacityAh": total / 1000,
        "capacitySource": "EXTENDED_24BIT_MAH", "effectiveCells": cells,
    }


class CapacitySocTests(unittest.TestCase):
    def test_extended_tail_and_legacy_rejection(self):
        tail = bytes.fromhex("FFFF04FFFF001B043AA00445C0")
        decoded = decode_capacity_tail_42(tail)
        self.assertEqual(decoded["remainingCapacityRawMah"], 277152)
        self.assertEqual(decoded["totalCapacityRawMah"], 280000)
        self.assertEqual(decoded["capacitySource"], "EXTENDED_24BIT_MAH")
        self.assertIsNone(decode_capacity_tail_42(bytes.fromhex("0000040000001B043AA00445C0")))

    def test_voltage_reset_is_per_battery(self):
        estimator = CapacitySocEstimator()
        batteries = [battery(2, vmax=3.500), battery(3, vmax=3.499)]
        result = estimator.update(batteries, 1000, {2, 3})
        self.assertEqual(batteries[0]["socInterpolatedPercent"], 100.0)
        self.assertLess(batteries[1]["socInterpolatedPercent"], 100.0)
        self.assertIn("SOC_INTERPOLATED_FULL_RESET", [e["type"] for e in result["events"]])

    def test_capacity_100_alone_does_not_anchor(self):
        estimator = CapacitySocEstimator()
        item = battery(2, remaining=280000, vmax=3.49)
        estimator.update([item], 1000, {2})
        self.assertIsNone(item["socInterpolatedPercent"])

    def test_step_below_100_reanchors_without_full_training(self):
        estimator = CapacitySocEstimator()
        full = battery(2, remaining=280000, vmax=3.49)
        estimator.update([full], 1000, {2})
        below = battery(2, remaining=277200, vmax=3.49)
        estimator.update([below], 9000, {2})
        self.assertEqual(below["socInterpolatedPercent"], 99.0)
        self.assertNotIn("SOC_CAPACITY_CORRECTION", below["socEvents"])

    def test_trapezoidal_discharge(self):
        estimator = CapacitySocEstimator()
        first = battery(current=-10.0)
        estimator.update([first], 1000, {2})
        second = battery(current=-10.0)
        estimator.update([second], 9000, {2})
        self.assertLess(second["socInterpolatedPercent"], first["socInterpolatedPercent"])

    def test_persistence_and_removal(self):
        with tempfile.TemporaryDirectory() as root:
            estimator = CapacitySocEstimator(root)
            estimator.update([battery(vmax=3.5)], 1000, {2})
            restored = CapacitySocEstimator(root)
            below = battery(vmax=3.49, current=-1)
            restored.update([below], 9000, {2})
            self.assertIsNotNone(below["socInterpolatedPercent"])
            restored.update([], 17000, set())
            self.assertNotIn(2, restored._state)


if __name__ == "__main__":
    unittest.main()
