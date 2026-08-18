import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dyness_bms_guardian import (  # noqa: E402
    FALLBACK_CCL,
    FALLBACK_CVL,
    FALLBACK_DCL,
    GuardianController,
    GuardianStateStore,
)


def valid_snapshot(timestamp=100_000, soc=80):
    return {
        "timestamp": timestamp,
        "valid": True,
        "system": {"voltage61": 53.2, "soc61": soc},
        "aggregate": {"summedBatteryCurrent": 4.0},
        "limits": {"statusRaw": 192, "statusFlags": {
            "chargeEnabled": True, "dischargeEnabled": True,
        }},
        "effectiveControl": {
            "outputValid": True,
            "effectiveChargeVoltage": 55.2,
            "effectiveChargeCurrent": 56.0,
            "effectiveDischargeCurrent": 596.4,
            "effectiveChargeEnabled": True,
            "effectiveDischargeEnabled": True,
        },
    }


class GuardianControllerTests(unittest.TestCase):
    def test_requires_two_distinct_valid_samples(self):
        controller = GuardianController()
        first, status, _ = controller.evaluate(valid_snapshot(), 100_100)
        self.assertEqual(status["mode"], "RECOVERY")
        self.assertEqual(first["effectiveControl"]["effectiveChargeVoltage"], FALLBACK_CVL)

        repeated, status, _ = controller.evaluate(valid_snapshot(), 100_200)
        self.assertEqual(status["mode"], "RECOVERY")
        self.assertEqual(repeated["effectiveControl"]["effectiveChargeCurrent"], FALLBACK_CCL)

        second = valid_snapshot(108_000)
        normal, status, _ = controller.evaluate(second, 108_100)
        self.assertEqual(status["mode"], "NORMAL")
        self.assertEqual(normal["effectiveControl"]["effectiveChargeCurrent"], 56.0)

    def test_stale_snapshot_uses_fixed_fallback_and_last_soc(self):
        controller = GuardianController()
        controller.evaluate(valid_snapshot(), 100_100)
        controller.evaluate(valid_snapshot(108_000), 108_100)
        fallback, status, _ = controller.evaluate(
            valid_snapshot(108_000), 130_001, {"voltage": 52.7, "current": -8.0}
        )
        control = fallback["effectiveControl"]
        self.assertEqual(status["mode"], "FALLBACK")
        self.assertEqual(control["effectiveChargeVoltage"], FALLBACK_CVL)
        self.assertEqual(control["effectiveChargeCurrent"], FALLBACK_CCL)
        self.assertEqual(control["effectiveDischargeCurrent"], FALLBACK_DCL)
        self.assertTrue(control["effectiveChargeEnabled"])
        self.assertTrue(control["effectiveDischargeEnabled"])
        self.assertEqual(fallback["system"]["soc61"], 80)
        self.assertEqual(fallback["system"]["voltage61"], 52.7)
        self.assertEqual(fallback["aggregate"]["summedBatteryCurrent"], -8.0)

    def test_invalid_snapshot_never_synthesizes_zero_soc(self):
        controller = GuardianController()
        controller.evaluate(valid_snapshot(soc=67), 100_100)
        fallback, _, _ = controller.evaluate({"valid": False}, 100_200)
        self.assertEqual(fallback["system"]["soc61"], 67)

    def test_missing_state_is_not_ready(self):
        output, status, _ = GuardianController().evaluate(None, 100_000)
        self.assertIsNone(output)
        self.assertFalse(status["ready"])
        self.assertEqual(status["mode"], "BOOTSTRAP")


class GuardianStateStoreTests(unittest.TestCase):
    def test_persists_and_loads_last_good_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GuardianStateStore(str(Path(directory) / "state.json"))
            snapshot = valid_snapshot()
            store.save(snapshot, snapshot["timestamp"], 80)
            loaded = store.load()
            self.assertEqual(loaded["lastSoc"], 80)
            self.assertEqual(loaded["lastGoodSnapshot"]["timestamp"], 100_000)


if __name__ == "__main__":
    unittest.main()
