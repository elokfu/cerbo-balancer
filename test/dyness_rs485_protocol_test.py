import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dyness_rs485_protocol import (  # noqa: E402
    checksum,
    decode_status,
    length_field,
    parse_limits,
    parse_pack_telemetry,
    parse_system_soc,
    parse_system_voltage,
    request,
)


def response(address, cid2, info):
    body = f"20{address:02X}4600{length_field(len(info))}{info}"
    return f"~{body}{checksum(body)}\r"


class DynessProtocolTests(unittest.TestCase):
    def test_request_is_read_only_protocol_frame(self):
        self.assertTrue(request(2, 0x42).startswith(b"~20024642"))
        self.assertTrue(request(2, 0x42).endswith(b"\r"))

    def test_fifteen_cells_use_same_battery_voltage_for_cell_sixteen(self):
        cells = "".join(f"{value:04X}" for value in [3500] * 15)
        info = f"00020F{cells}00" + "0000" + f"{56000:04X}"
        battery = parse_pack_telemetry(response(2, 0x42, info), 2)
        self.assertEqual(battery.calculated_cell_index, 16)
        self.assertAlmostEqual(battery.calculated_cell_voltage, 3.5)
        self.assertAlmostEqual(battery.reconstructed_cell_sum, 56.0)

    def test_sixteen_cells_are_all_reported(self):
        cells = "".join(f"{value:04X}" for value in [3500] * 16)
        info = f"000310{cells}00" + "0000" + f"{56000:04X}"
        battery = parse_pack_telemetry(response(3, 0x42, info), 3)
        self.assertIsNone(battery.calculated_cell_index)
        self.assertTrue(all(item["source"] == "reported" for item in battery.effective_cells))

    def test_per_battery_current_is_signed_and_temperature_is_decoded(self):
        cells = "".join(f"{value:04X}" for value in [3500] * 16)
        info = f"000210{cells}02{2981:04X}{3031:04X}FFCE{56000:04X}"
        battery = parse_pack_telemetry(response(2, 0x42, info), 2)
        self.assertAlmostEqual(battery.current, -5.0)
        self.assertAlmostEqual(battery.temperatures[0], 25.0)
        self.assertAlmostEqual(battery.temperatures[1], 30.0)

    def test_system_and_limits_values(self):
        system = response(2, 0x61, "D00000005A")
        self.assertAlmostEqual(parse_system_voltage(system), 53.248)
        self.assertEqual(parse_system_soc(system), 90)
        limits = response(2, 0x63, "DCB0C350003800C800")
        parsed = parse_limits(limits)
        self.assertAlmostEqual(parsed.charge_voltage, 56.496)
        self.assertEqual(parsed.charge_current_raw, 56)
        self.assertAlmostEqual(parsed.discharge_current_signed, 20.0)

    def test_status_byte_decodes_all_dyness_master_flags(self):
        status = decode_status(0xFF)
        self.assertTrue(status["cellUnderVoltageWarning"])
        self.assertTrue(status["cellOverVoltageWarning"])
        self.assertTrue(status["underTemperatureWarning"])
        self.assertTrue(status["overTemperatureWarning"])
        self.assertTrue(status["dischargeOverCurrentWarning"])
        self.assertTrue(status["chargeOverCurrentWarning"])
        self.assertTrue(status["cclActive"])
        self.assertTrue(status["protectionActive"])
        self.assertEqual(status["severity"], "protection")
        self.assertEqual(len(status["active"]), 8)

    def test_non_ascii_frame_is_rejected_before_text_parsing(self):
        valid = response(2, 0x61, "D00000005A").encode("ascii")
        corrupted = valid[:10] + b"\xff" + valid[11:]
        with self.assertRaisesRegex(ValueError, "non-ASCII"):
            parse_system_voltage(corrupted)


if __name__ == "__main__":
    unittest.main()
