#!/usr/bin/env python3
"""Read-only Dyness/Pylon-compatible RS485 protocol helpers.

The parser is deliberately independent from serial I/O so captured frames can
be tested offline.  It implements only the read requests used by the balancer;
there are no write, wake, or configuration commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def checksum(body: str) -> str:
    return f"{(-sum(body.encode('ascii'))) & 0xFFFF:04X}"


def request(address: int, cid2: int) -> bytes:
    if not 2 <= address <= 0xFF:
        raise ValueError("Dyness/Pylon address must be between 2 and 255")
    if not 0 <= cid2 <= 0xFF:
        raise ValueError("CID2 must be one byte")
    info = f"{address:02X}"
    body = f"20{address:02X}46{cid2:02X}E002{info}"
    return f"~{body}{checksum(body)}\r".encode("ascii")


def _clean_frame(frame: str | bytes) -> str:
    if isinstance(frame, bytes):
        if any(value > 0x7F for value in frame):
            raise ValueError("non-ASCII byte in response frame")
        try:
            frame = frame.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("non-ASCII response frame") from error
    if frame.endswith("\r"):
        frame = frame[:-1]
    return frame


def response_info(frame: str | bytes) -> str:
    frame = _clean_frame(frame)
    if not frame.startswith("~") or len(frame) < 18:
        raise ValueError(f"invalid response frame: {frame!r}")
    return frame[13:-4]


def length_field(length: int) -> str:
    if not 0 <= length <= 0xFFF:
        raise ValueError("INFO length must fit the Pylontech 12-bit field")
    check = (-(length >> 8) - ((length >> 4) & 0xF) - (length & 0xF)) & 0xF
    return f"{check:X}{length:03X}"


def parse_response(frame: str | bytes, address: int, cid2: int) -> dict[str, Any]:
    """Validate a response and return raw INFO for the CID-specific parser."""
    clean = _clean_frame(frame)
    if not clean.startswith("~") or len(clean) < 18:
        raise ValueError("invalid response framing")
    body = clean[1:-4]
    received = clean[-4:].upper()
    if checksum(body) != received:
        raise ValueError("checksum mismatch")
    if len(body) < 12:
        raise ValueError("response header is truncated")
    response_address = int(body[2:4], 16)
    if response_address != address:
        raise ValueError(f"unexpected response address {response_address:02X}")
    if body[4:6] != "46":
        raise ValueError(f"unexpected CID1 {body[4:6]}")
    return_code = int(body[6:8], 16)
    if return_code != 0:
        raise ValueError(f"Dyness returned error code {return_code:02X} for CID2 {cid2:02X}")
    encoded_length = body[8:12]
    if len(encoded_length) != 4:
        raise ValueError("missing INFO length")
    length = int(encoded_length[1:], 16)
    expected_length_check = (-(length >> 8) - ((length >> 4) & 0xF) - (length & 0xF)) & 0xF
    if int(encoded_length[0], 16) != expected_length_check:
        raise ValueError("INFO length checksum mismatch")
    info = body[12:]
    if len(info) != length:
        raise ValueError(f"INFO length mismatch: expected {length}, got {len(info)}")
    return {
        "version": body[0:2],
        "address": response_address,
        "cid1": "46",
        "cid2": cid2,
        "returnCode": return_code,
        "length": length,
        "infoAscii": info,
        "infoHex": info,
        "rawFrame": clean,
    }


def signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


STATUS_FLAGS = {
    0: ("cellUnderVoltageWarning", "cell under-voltage warning"),
    1: ("cellOverVoltageWarning", "cell over-voltage warning"),
    2: ("underTemperatureWarning", "under-temperature warning"),
    3: ("overTemperatureWarning", "over-temperature warning"),
    4: ("dischargeOverCurrentWarning", "discharge over-current warning"),
    5: ("chargeOverCurrentWarning", "charge over-current warning"),
    6: ("cclActive", "CCL active"),
    7: ("protectionActive", "OVP/protection active"),
}


def decode_status(status: int) -> dict[str, Any]:
    """Decode the Dyness BMS master status byte without losing the raw value."""
    flags = {
        name: bool(status & (1 << bit))
        for bit, (name, _description) in STATUS_FLAGS.items()
    }
    return {
        **flags,
        "active": [
            {"name": name, "description": description, "bit": bit}
            for bit, (name, description) in STATUS_FLAGS.items()
            if flags[name]
        ],
        "severity": "protection" if flags["protectionActive"]
        else "warning" if any(flags.values()) else "normal",
    }


@dataclass
class BatteryTelemetry:
    address: int
    reported_cells: list[float]
    effective_cells: list[dict[str, Any]]
    temperatures: list[float]
    current: float
    voltage: float
    calculated_cell_index: int | None
    calculated_cell_voltage: float | None
    directly_reported_cell_sum: float
    reconstructed_cell_sum: float
    reconstructed_voltage_delta_mv: float
    valid: bool
    validation_errors: list[str]
    raw_info: str
    trailing_info: str

    def as_dict(self) -> dict[str, Any]:
        minimum = min(self.temperatures) if self.temperatures else None
        maximum = max(self.temperatures) if self.temperatures else None
        average = sum(self.temperatures) / len(self.temperatures) if self.temperatures else None
        temperature_state = "unavailable"
        if maximum is not None:
            temperature_state = "high" if maximum >= 60 else "elevated" if maximum >= 45 else "normal"
        return {
            "address": self.address,
            "reportedCells": self.reported_cells,
            "effectiveCells": self.effective_cells,
            "calculatedCellIndex": self.calculated_cell_index,
            "calculatedCellVoltage": self.calculated_cell_voltage,
            "directlyReportedCellSum": self.directly_reported_cell_sum,
            "reconstructedCellSum": self.reconstructed_cell_sum,
            "reconstructedVoltageDeltaMv": self.reconstructed_voltage_delta_mv,
            "temperatures": self.temperatures,
            "minimumTemperature": minimum,
            "maximumTemperature": maximum,
            "averageTemperature": average,
            "temperatureState": temperature_state,
            "current": self.current,
            "voltage": self.voltage,
            "valid": self.valid,
            "validationErrors": self.validation_errors,
            "rawInfo": self.raw_info,
            "trailingInfo": self.trailing_info,
        }


def parse_pack_telemetry(frame: str, address: int) -> BatteryTelemetry:
    response = parse_response(frame, address, 0x42)
    info = response["infoAscii"]
    if len(info) < 6 or info[0:2] != "00" or info[2:4] != f"{address:02X}":
        raise ValueError("unexpected CID2=42 INFO header")
    pos = 4
    cell_count = int(info[pos:pos + 2], 16)
    pos += 2
    if not 1 <= cell_count <= 32:
        raise ValueError(f"implausible cell count {cell_count}")
    cells: list[float] = []
    for _ in range(cell_count):
        if len(info) < pos + 4:
            raise ValueError("truncated cell voltage array")
        cells.append(int(info[pos:pos + 4], 16) / 1000.0)
        pos += 4
    if len(info) < pos + 2:
        raise ValueError("missing temperature count")
    temperature_count = int(info[pos:pos + 2], 16)
    pos += 2
    if temperature_count > 32:
        raise ValueError(f"implausible temperature count {temperature_count}")
    temperatures: list[float] = []
    for _ in range(temperature_count):
        if len(info) < pos + 4:
            raise ValueError("truncated temperature array")
        temperatures.append((int(info[pos:pos + 4], 16) - 2731) / 10.0)
        pos += 4
    if len(info) < pos + 8:
        raise ValueError("missing module current or voltage")
    current = signed16(int(info[pos:pos + 4], 16)) / 10.0
    pos += 4
    voltage = int(info[pos:pos + 4], 16) / 1000.0
    trailing_info = info[pos + 4:]
    directly_reported = sum(cells)
    effective = [{"index": i + 1, "voltage": value, "source": "reported"}
                 for i, value in enumerate(cells)]
    calculated_index = None
    calculated_voltage = None
    errors: list[str] = []
    if len(cells) == 15:
        calculated_voltage = voltage - directly_reported
        calculated_index = 16
        effective.append({"index": 16, "voltage": calculated_voltage, "source": "calculated"})
    elif len(cells) != 16:
        errors.append(f"expected 15 or 16 cells, received {len(cells)}")
    reconstructed = sum(item["voltage"] for item in effective)
    delta_mv = (reconstructed - voltage) * 1000.0
    if len(effective) == 16:
        if any(value < 2.5 or value > 4.5 for value in (item["voltage"] for item in effective)):
            errors.append("cell voltage outside 2.5..4.5 V validation range")
        if abs(delta_mv) > 100.0:
            errors.append(f"cell sum differs from battery voltage by {delta_mv:.1f} mV")
    return BatteryTelemetry(
        address=address,
        reported_cells=cells,
        effective_cells=effective,
        temperatures=temperatures,
        current=current,
        voltage=voltage,
        calculated_cell_index=calculated_index,
        calculated_cell_voltage=calculated_voltage,
        directly_reported_cell_sum=directly_reported,
        reconstructed_cell_sum=reconstructed,
        reconstructed_voltage_delta_mv=delta_mv,
        valid=not errors and len(effective) == 16,
        validation_errors=errors,
        raw_info=info,
        trailing_info=trailing_info,
    )


def parse_system_voltage(frame: str) -> float:
    info = parse_response(frame, 2, 0x61)["infoAscii"]
    if len(info) < 4:
        raise ValueError("missing system voltage")
    return int(info[:4], 16) / 1000.0


def parse_system_soc(frame: str) -> int:
    info = parse_response(frame, 2, 0x61)["infoAscii"]
    if len(info) < 10:
        raise ValueError("missing system SOC")
    return int(info[8:10], 16)


@dataclass
class Limits:
    charge_voltage: float
    discharge_voltage: float
    charge_current_raw: int
    discharge_current_raw: int
    status: int

    @property
    def charge_current(self) -> float:
        return self.charge_current_raw / 10.0

    @property
    def discharge_current_signed(self) -> float:
        return signed16(self.discharge_current_raw) / 10.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chargeVoltage": self.charge_voltage,
            "dischargeVoltage": self.discharge_voltage,
            "chargeCurrentRaw": self.charge_current_raw,
            "chargeCurrent": self.charge_current,
            "dischargeCurrentRaw": self.discharge_current_raw,
            "dischargeCurrentSigned": self.discharge_current_signed,
            "statusRaw": self.status,
            "statusFlags": decode_status(self.status),
        }


def parse_limits(frame: str) -> Limits:
    info = parse_response(frame, 2, 0x63)["infoAscii"]
    if len(info) < 18:
        raise ValueError("missing charge/discharge limits")
    return Limits(
        charge_voltage=int(info[0:4], 16) / 1000.0,
        discharge_voltage=int(info[4:8], 16) / 1000.0,
        charge_current_raw=int(info[8:12], 16),
        discharge_current_raw=int(info[12:16], 16),
        status=int(info[16:18], 16),
    )
