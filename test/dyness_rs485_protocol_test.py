import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dyness_rs485_protocol import (  # noqa: E402
    checksum,
    decode_status,
    length_field,
    parse_system_61,
    parse_limits,
    parse_pack_telemetry,
    parse_status_44,
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

    def test_system_summary_discards_unphysical_temperatures_and_sentinels(self):
        data = bytearray(49)
        data[0:2] = (54480).to_bytes(2, "big")
        data[2:4] = (10).to_bytes(2, "big", signed=True)
        data[4] = 100
        data[5:7] = (0xFFFF).to_bytes(2, "big")
        data[7:9] = (0xFFFF).to_bytes(2, "big")
        data[9] = 100
        data[10] = 255
        data[11:13] = (3524).to_bytes(2, "big")
        data[13:15] = (1794).to_bytes(2, "big")
        data[15:17] = (3342).to_bytes(2, "big")
        data[17:19] = (2818).to_bytes(2, "big")
        for offset, raw in ((19, 0xFFFF), (21, 2995), (25, 2990),
                            (29, 0xFFFF), (31, 3015), (35, 0xFFFF),
                            (39, 0xFFFF), (41, 3073), (45, 3073)):
            data[offset:offset + 2] = raw.to_bytes(2, "big")
        for offset, value in ((23, 1026), (27, 514), (33, 258),
                              (37, 0xFFFF), (43, 258), (47, 258)):
            data[offset:offset + 2] = value.to_bytes(2, "big")
        parsed = parse_system_61(response(2, 0x61, data.hex().upper()))
        self.assertAlmostEqual(parsed.voltage, 54.48)
        self.assertEqual(parsed.average_cycle_count, None)
        self.assertEqual(parsed.minimum_soh, None)
        self.assertIsNone(parsed.average_cell_temperature)
        self.assertAlmostEqual(parsed.maximum_bms_temperature, 34.15, places=2)
        self.assertAlmostEqual(parsed.minimum_bms_temperature, 34.15, places=2)

    def test_status_byte_decodes_permission_state_bits(self):
        status = decode_status(0xFF)
        self.assertTrue(status["chargeEnabled"])
        self.assertTrue(status["dischargeEnabled"])
        self.assertTrue(status["strongCharge"])
        self.assertTrue(status["fullCharge"])
        self.assertEqual(status["unknownReservedBits"], 0x0F)
        self.assertEqual(len(status["active"]), 4)

    def test_status44_decodes_full_register_set(self):
        # Flag/address, no variable alarm arrays, three alarm bytes, Status1-5.
        info = "00020000000000F70FE98140"
        parsed = parse_status_44(response(2, 0x44, info), 2).as_dict()

        self.assertEqual(parsed["status1"]["raw"], 0xF7)
        self.assertEqual(len(parsed["status1"]["active"]), 7)
        self.assertEqual(parsed["status2"]["raw"], 0x0F)
        self.assertTrue(parsed["status2"]["chargeMosfet"])
        self.assertTrue(parsed["status2"]["dischargeMosfet"])
        self.assertTrue(parsed["status2"]["modulePowerActive"])
        self.assertEqual(parsed["status3"]["raw"], 0xE9)
        self.assertTrue(parsed["status3"]["effectiveCharging"])
        self.assertTrue(parsed["status3"]["effectiveDischarging"])
        self.assertTrue(parsed["status3"]["fullyCharged"])
        self.assertTrue(parsed["status3"]["buzzerActive"])
        self.assertEqual(parsed["status4"]["cellFaults"], [1, 8])
        self.assertEqual(parsed["status5"]["cellFaults"], [15])

    def test_status44_rejects_truncated_status_block(self):
        with self.assertRaisesRegex(ValueError, "truncated CID2=44"):
            parse_status_44(response(2, 0x44, "00020000000000"), 2)

    def test_non_ascii_frame_is_rejected_before_text_parsing(self):
        valid = response(2, 0x61, "D00000005A").encode("ascii")
        corrupted = valid[:10] + b"\xff" + valid[11:]
        with self.assertRaisesRegex(ValueError, "non-ASCII"):
            parse_system_voltage(corrupted)


if __name__ == "__main__":
    unittest.main()
