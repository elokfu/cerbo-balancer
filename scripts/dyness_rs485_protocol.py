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


def u8(data: bytes, offset: int) -> int:
    return data[offset]


def u16be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "big", signed=False)


def s16be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "big", signed=True)


STATUS_FLAGS = {
    7: ("chargeEnabled", "charge enabled"),
    6: ("dischargeEnabled", "discharge enabled"),
    5: ("strongCharge", "strong charge"),
    4: ("fullCharge", "full charge"),
}


def decode_status(status: int) -> dict[str, Any]:
    """Decode CID2=63 permission/state bits without inventing alarms."""
    flags = {
        name: bool(status & (1 << bit))
        for bit, (name, _description) in STATUS_FLAGS.items()
    }
    reserved = status & 0x0F
    return {
        **flags,
        "active": [
            {"name": name, "description": description, "bit": bit}
            for bit, (name, description) in STATUS_FLAGS.items()
            if flags[name]
        ],
        "unknownReservedBits": reserved,
        "unknownReservedHex": f"0x{reserved:X}",
        "state": "permissions",
    }


STATUS1_FLAGS = {
    7: "pack under-voltage protection",
    6: "charge temperature protection",
    5: "discharge temperature protection",
    4: "discharge over-current protection",
    2: "charge over-current protection",
    1: "cell under-voltage protection",
    0: "over-voltage protection",
}
STATUS2_MASK = 0x0F
STATUS3_FLAGS = {
    7: "effective charging",
    6: "effective discharging",
    5: "heater active",
    3: "fully charged",
    0: "buzzer active",
}
STATUS3_MASK = sum(1 << bit for bit in STATUS3_FLAGS)


def _active_status_flags(value: int, labels: dict[int, str]) -> list[dict[str, Any]]:
    return [
        {"bit": bit, "name": label, "description": label}
        for bit, label in labels.items()
        if value & (1 << bit)
    ]


def _reserved_status_bits(value: int, known_mask: int) -> dict[str, Any]:
    reserved = value & (~known_mask & 0xFF)
    return {"bits": reserved, "hex": f"0x{reserved:02X}"}


def _cell_faults(value: int, first_cell: int) -> list[int]:
    return [
        first_cell + bit
        for bit in range(8)
        if value & (1 << bit)
    ]


@dataclass
class StatusTelemetry44:
    """Decoded CID2=0x44 per-battery alarm and status telemetry."""

    data_flag: int
    address: int
    cell_alarms: list[int]
    temperature_alarms: list[int]
    charge_current_alarm: int
    module_voltage_alarm: int
    discharge_current_alarm: int
    status1: int
    status2: int
    status3: int
    status4: int
    status5: int
    raw_info: str
    trailing_info: str

    def as_dict(self) -> dict[str, Any]:
        status1_flags = _active_status_flags(self.status1, STATUS1_FLAGS)
        status3_flags = _active_status_flags(self.status3, STATUS3_FLAGS)
        return {
            "available": True,
            "dataFlag": self.data_flag,
            "address": self.address,
            "status1": {
                "raw": self.status1,
                "active": status1_flags,
                "reserved": _reserved_status_bits(self.status1, 0xF7),
            },
            "status2": {
                "raw": self.status2,
                "prechargeMosfet": bool(self.status2 & 0x01),
                "chargeMosfet": bool(self.status2 & 0x02),
                "dischargeMosfet": bool(self.status2 & 0x04),
                "modulePowerActive": bool(self.status2 & 0x08),
                "reserved": _reserved_status_bits(self.status2, STATUS2_MASK),
            },
            "status3": {
                "raw": self.status3,
                "effectiveCharging": bool(self.status3 & 0x80),
                "effectiveDischarging": bool(self.status3 & 0x40),
                "heaterActive": bool(self.status3 & 0x20),
                "fullyCharged": bool(self.status3 & 0x08),
                "buzzerActive": bool(self.status3 & 0x01),
                "active": status3_flags,
                "reserved": _reserved_status_bits(self.status3, STATUS3_MASK),
            },
            "status4": {
                "raw": self.status4,
                "cellFaults": _cell_faults(self.status4, 1),
            },
            "status5": {
                "raw": self.status5,
                "cellFaults": _cell_faults(self.status5, 9),
            },
            "alarms": {
                "cell": self.cell_alarms,
                "temperature": self.temperature_alarms,
                "chargeCurrent": self.charge_current_alarm,
                "moduleVoltage": self.module_voltage_alarm,
                "dischargeCurrent": self.discharge_current_alarm,
            },
            "rawInfo": self.raw_info,
            "trailingInfo": self.trailing_info,
        }


def _temperature(raw: int) -> float | None:
    value = raw / 10.0 - 273.15
    return value if -40.0 <= value <= 100.0 else None


def _percent(raw: int) -> int | None:
    return raw if 0 <= raw <= 100 else None


def _cycle_count(raw: int) -> int | None:
    return raw if raw != 0xFFFF else None


def _cell_voltage(raw: int) -> float | None:
    value = raw / 1000.0
    return value if 2.0 <= value <= 4.5 else None


def decode_capacity_tail_42(trailing: bytes | str) -> dict[str, Any] | None:
    """Decode the optional CID2=0x42 lifetime/capacity tail.

    Dyness/Pylon responses may append legacy 16-bit capacity fields followed
    by the extended 24-bit mAh values.  The legacy fields are saturated for
    larger packs, so they are accepted only as structural markers.  An
    absent or unrecognised tail is optional telemetry and does not invalidate
    the voltage/cell measurement.
    """
    if isinstance(trailing, str):
        try:
            trailing = bytes.fromhex(trailing)
        except ValueError:
            return None
    if len(trailing) < 13:
        return None
    legacy_remaining = u16be(trailing, 0)
    user_items = u8(trailing, 2)
    legacy_total = u16be(trailing, 3)
    cycle_count = u16be(trailing, 5)
    remaining_mah = int.from_bytes(trailing[7:10], "big")
    total_mah = int.from_bytes(trailing[10:13], "big")
    if legacy_remaining != 0xFFFF or legacy_total != 0xFFFF or user_items != 0x04:
        return None
    if cycle_count > 20000 or total_mah <= 0 or remaining_mah > total_mah * 1.05:
        return None
    return {
        "cycleCount": cycle_count,
        "remainingCapacityAh": remaining_mah / 1000.0,
        "totalCapacityAh": total_mah / 1000.0,
        "capacitySoc": remaining_mah / total_mah * 100.0,
        "rawCapacityTail": trailing[:13].hex().upper(),
    }


@dataclass
class SystemTelemetry61:
    voltage: float | None
    current: float | None
    soc: int | None
    average_cycle_count: int | None
    maximum_cycle_count: int | None
    average_soh: int | None
    minimum_soh: int | None
    maximum_cell_voltage: float | None
    maximum_cell_id: int | None
    minimum_cell_voltage: float | None
    minimum_cell_id: int | None
    average_cell_temperature: float | None
    maximum_cell_temperature: float | None
    maximum_cell_temperature_id: int | None
    minimum_cell_temperature: float | None
    minimum_cell_temperature_id: int | None
    average_mosfet_temperature: float | None
    maximum_mosfet_temperature: float | None
    maximum_mosfet_temperature_id: int | None
    minimum_mosfet_temperature: float | None
    minimum_mosfet_temperature_id: int | None
    average_bms_temperature: float | None
    maximum_bms_temperature: float | None
    maximum_bms_temperature_id: int | None
    minimum_bms_temperature: float | None
    minimum_bms_temperature_id: int | None
    trailing_hex: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "voltage61": self.voltage,
            "current61": self.current,
            "soc61": self.soc,
            "averageCycleCount61": self.average_cycle_count,
            "maximumCycleCount61": self.maximum_cycle_count,
            "averageSoh61": self.average_soh,
            "minimumSoh61": self.minimum_soh,
            "maximumCellVoltage61": self.maximum_cell_voltage,
            "maximumCellId61": self.maximum_cell_id,
            "minimumCellVoltage61": self.minimum_cell_voltage,
            "minimumCellId61": self.minimum_cell_id,
            "averageCellTemperature61": self.average_cell_temperature,
            "maximumCellTemperature61": self.maximum_cell_temperature,
            "maximumCellTemperatureId61": self.maximum_cell_temperature_id,
            "minimumCellTemperature61": self.minimum_cell_temperature,
            "minimumCellTemperatureId61": self.minimum_cell_temperature_id,
            "averageMosfetTemperature61": self.average_mosfet_temperature,
            "maximumMosfetTemperature61": self.maximum_mosfet_temperature,
            "maximumMosfetTemperatureId61": self.maximum_mosfet_temperature_id,
            "minimumMosfetTemperature61": self.minimum_mosfet_temperature,
            "minimumMosfetTemperatureId61": self.minimum_mosfet_temperature_id,
            "averageBmsTemperature61": self.average_bms_temperature,
            "maximumBmsTemperature61": self.maximum_bms_temperature,
            "maximumBmsTemperatureId61": self.maximum_bms_temperature_id,
            "minimumBmsTemperature61": self.minimum_bms_temperature,
            "minimumBmsTemperatureId61": self.minimum_bms_temperature_id,
            "trailingHex61": self.trailing_hex,
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
    cycle_count: int | None = None
    remaining_capacity_ah: float | None = None
    total_capacity_ah: float | None = None
    capacity_soc: float | None = None
    raw_capacity_tail: str = ""

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
            "cycleCount": self.cycle_count,
            "remainingCapacityAh": self.remaining_capacity_ah,
            "totalCapacityAh": self.total_capacity_ah,
            "capacitySoc": self.capacity_soc,
            "rawCapacityTail": self.raw_capacity_tail,
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
    capacity = decode_capacity_tail_42(trailing_info)
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
        cycle_count=capacity["cycleCount"] if capacity else None,
        remaining_capacity_ah=capacity["remainingCapacityAh"] if capacity else None,
        total_capacity_ah=capacity["totalCapacityAh"] if capacity else None,
        capacity_soc=capacity["capacitySoc"] if capacity else None,
        raw_capacity_tail=capacity["rawCapacityTail"] if capacity else "",
    )


def parse_status_44(frame: str, address: int) -> StatusTelemetry44:
    """Parse the complete Pylon/Dyness CID2=0x44 status response."""
    response = parse_response(frame, address, 0x44)
    info = response["infoAscii"]
    data = bytes.fromhex(info)
    if len(data) < 3:
        raise ValueError("CID2=44 INFO is too short")
    if data[1] != address:
        raise ValueError(
            f"unexpected CID2=44 address {data[1]:02X}, expected {address:02X}"
        )

    position = 2
    cell_alarm_count = data[position]
    position += 1
    if cell_alarm_count > 32:
        raise ValueError(f"implausible CID2=44 cell alarm count {cell_alarm_count}")
    if len(data) < position + cell_alarm_count + 1:
        raise ValueError("truncated CID2=44 cell alarms")
    cell_alarms = list(data[position:position + cell_alarm_count])
    position += cell_alarm_count

    temperature_alarm_count = data[position]
    position += 1
    if temperature_alarm_count > 32:
        raise ValueError(
            f"implausible CID2=44 temperature alarm count {temperature_alarm_count}"
        )
    required = temperature_alarm_count + 8
    if len(data) < position + required:
        raise ValueError("truncated CID2=44 alarm/status block")
    temperature_alarms = list(data[position:position + temperature_alarm_count])
    position += temperature_alarm_count

    charge_current_alarm = data[position]
    module_voltage_alarm = data[position + 1]
    discharge_current_alarm = data[position + 2]
    status1 = data[position + 3]
    status2 = data[position + 4]
    status3 = data[position + 5]
    status4 = data[position + 6]
    status5 = data[position + 7]
    position += 8

    return StatusTelemetry44(
        data_flag=data[0],
        address=data[1],
        cell_alarms=cell_alarms,
        temperature_alarms=temperature_alarms,
        charge_current_alarm=charge_current_alarm,
        module_voltage_alarm=module_voltage_alarm,
        discharge_current_alarm=discharge_current_alarm,
        status1=status1,
        status2=status2,
        status3=status3,
        status4=status4,
        status5=status5,
        raw_info=info,
        trailing_info=info[position * 2:],
    )


def parse_system_61(frame: str) -> SystemTelemetry61:
    """Parse the validated 49-byte CID2=61 system summary.

    Dyness/Pylon devices may use ``0xFFFF`` for unavailable counters and
    temperatures.  Those sentinels, and any temperature outside a physical
    BMS range, are returned as ``None`` instead of being displayed as values.
    """
    info = parse_response(frame, 2, 0x61)["infoAscii"]
    data = bytes.fromhex(info)
    if len(data) < 49:
        raise ValueError(f"CID2=61 INFO too short: got {len(data)} bytes")
    return SystemTelemetry61(
        voltage=(u16be(data, 0) / 1000.0
                 if 40.0 <= u16be(data, 0) / 1000.0 <= 70.0 else None),
        current=(s16be(data, 2) / 100.0
                 if abs(s16be(data, 2) / 100.0) <= 1000.0 else None),
        soc=_percent(u8(data, 4)),
        average_cycle_count=_cycle_count(u16be(data, 5)),
        maximum_cycle_count=_cycle_count(u16be(data, 7)),
        average_soh=_percent(u8(data, 9)),
        minimum_soh=_percent(u8(data, 10)),
        maximum_cell_voltage=_cell_voltage(u16be(data, 11)),
        maximum_cell_id=u16be(data, 13) if u16be(data, 11) != 0xFFFF else None,
        minimum_cell_voltage=_cell_voltage(u16be(data, 15)),
        minimum_cell_id=u16be(data, 17) if u16be(data, 15) != 0xFFFF else None,
        average_cell_temperature=_temperature(u16be(data, 19)),
        maximum_cell_temperature=_temperature(u16be(data, 21)),
        maximum_cell_temperature_id=u16be(data, 23) if _temperature(u16be(data, 21)) is not None else None,
        minimum_cell_temperature=_temperature(u16be(data, 25)),
        minimum_cell_temperature_id=u16be(data, 27) if _temperature(u16be(data, 25)) is not None else None,
        average_mosfet_temperature=_temperature(u16be(data, 29)),
        maximum_mosfet_temperature=_temperature(u16be(data, 31)),
        maximum_mosfet_temperature_id=u16be(data, 33) if _temperature(u16be(data, 31)) is not None else None,
        minimum_mosfet_temperature=_temperature(u16be(data, 35)),
        minimum_mosfet_temperature_id=u16be(data, 37) if _temperature(u16be(data, 35)) is not None else None,
        average_bms_temperature=_temperature(u16be(data, 39)),
        maximum_bms_temperature=_temperature(u16be(data, 41)),
        maximum_bms_temperature_id=u16be(data, 43) if _temperature(u16be(data, 41)) is not None else None,
        minimum_bms_temperature=_temperature(u16be(data, 45)),
        minimum_bms_temperature_id=u16be(data, 47) if _temperature(u16be(data, 45)) is not None else None,
        trailing_hex=data[49:].hex().upper(),
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
