"""Read Dyness/Pylon-compatible batteries over RS485.

The script assumes batteries are connected in parallel, so CID2=61's
system voltage is used as the pack voltage for every responding battery.
CID2=42 reports 15 cells; the sixteenth cell is calculated as:

    pack_voltage - sum(the 15 reported cells)

Only read-only protocol commands are sent.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import serial


@dataclass
class Battery:
    address: int
    cells: list[float]
    temperatures: list[float]


@dataclass
class Limits:
    charge_voltage: float
    discharge_voltage: float
    charge_current_raw: int
    discharge_current_raw: int
    status: int


def checksum(body: str) -> str:
    return f"{(-sum(body.encode('ascii'))) & 0xFFFF:04X}"


def request(address: int, cid2: int) -> bytes:
    info = f"{address:02X}"
    body = f"20{address:02X}46{cid2:02X}E002{info}"
    return f"~{body}{checksum(body)}\r".encode("ascii")


def read_frame(port: serial.Serial, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    frame = bytearray()
    started = False

    while time.monotonic() < deadline:
        byte = port.read(1)
        if not byte:
            continue
        if byte == b"~":
            frame = bytearray(byte)
            started = True
            continue
        if started:
            frame.extend(byte)
            if byte == b"\r":
                return frame[:-1].decode("ascii", errors="replace")

    return None


def query(port: serial.Serial, address: int, cid2: int, timeout: float) -> str | None:
    port.reset_input_buffer()
    port.write(request(address, cid2))
    port.flush()
    return read_frame(port, timeout)


def response_info(frame: str) -> str:
    if not frame.startswith("~") or len(frame) < 17:
        raise ValueError(f"invalid response frame: {frame!r}")
    # SOI + VER + ADR + CID1 + RTN + length-checksum + length.
    return frame[13:-4]


def parse_pack_telemetry(frame: str, address: int) -> tuple[list[float], list[float]]:
    info = response_info(frame)
    if info[0:2] != "00" or info[2:4] != f"{address:02X}":
        raise ValueError(f"unexpected CID2=42 response for address {address:02X}: {frame}")

    count = int(info[4:6], 16)
    values_start = 6
    values_end = values_start + count * 4
    if count != 15 or len(info) < values_end + 2:
        raise ValueError(f"expected 15 cell values, got {count}: {frame}")

    cells = [int(info[i : i + 4], 16) / 1000 for i in range(values_start, values_end, 4)]
    temperature_count = int(info[values_end : values_end + 2], 16)
    temperatures_start = values_end + 2
    temperatures_end = temperatures_start + temperature_count * 4
    if len(info) < temperatures_end:
        raise ValueError(f"truncated temperature block: {frame}")

    # Pylon fixed-point temperatures are Kelvin*10 plus 2731.
    temperatures = [
        (int(info[i : i + 4], 16) - 2731) / 10
        for i in range(temperatures_start, temperatures_end, 4)
    ]
    return cells, temperatures


def parse_system_voltage(frame: str) -> float:
    info = response_info(frame)
    if len(info) < 4:
        raise ValueError(f"missing system voltage: {frame}")
    return int(info[:4], 16) / 1000


def parse_system_soc(frame: str) -> int:
    info = response_info(frame)
    if len(info) < 10:
        raise ValueError(f"missing system SOC: {frame}")
    return int(info[8:10], 16)


def parse_limits(frame: str) -> Limits:
    info = response_info(frame)
    if len(info) < 18:
        raise ValueError(f"missing charge/discharge limits: {frame}")
    return Limits(
        charge_voltage=int(info[0:4], 16) / 1000,
        discharge_voltage=int(info[4:8], 16) / 1000,
        charge_current_raw=int(info[8:12], 16),
        discharge_current_raw=int(info[12:16], 16),
        status=int(info[16:18], 16),
    )


def parse_addresses(value: str) -> list[int]:
    addresses = []
    for item in value.split(","):
        address = int(item, 0)
        if not 2 <= address <= 0xFF:
            raise argparse.ArgumentTypeError(f"invalid Pylon address: {item}")
        addresses.append(address)
    return addresses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--addresses", type=parse_addresses, default=[2, 3])
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args()

    with serial.Serial(
        args.port,
        args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
    ) as port:
        system_frame = query(port, 2, 0x61, args.timeout)
        if system_frame is None:
            raise SystemExit("No CID2=61 system response received")
        pack_voltage = parse_system_voltage(system_frame)
        system_soc = parse_system_soc(system_frame)

        limits_frame = query(port, 2, 0x63, args.timeout)
        limits = parse_limits(limits_frame) if limits_frame else None

        batteries: list[Battery] = []
        for address in args.addresses:
            frame = query(port, address, 0x42, args.timeout)
            if frame is None:
                print(f"Battery {address:02X}: no response")
                continue
            try:
                cells, temperatures = parse_pack_telemetry(frame, address)
                batteries.append(Battery(address, cells, temperatures))
            except ValueError as error:
                print(f"Battery {address:02X}: {error}")

    print(f"System/parallel pack voltage: {pack_voltage:.3f} V")
    print(f"System SOC: {system_soc}%")
    parallel_count = max(1, len(batteries))
    if limits is not None:
        charge_current = limits.charge_current_raw / 10
        discharge_current_signed = (
            limits.discharge_current_raw - 0x10000
            if limits.discharge_current_raw & 0x8000
            else limits.discharge_current_raw
        ) / 10
        print("Charge/discharge limits (CID2=63):")
        print(f"  Charge voltage limit:    {limits.charge_voltage:.3f} V")
        print(f"  Discharge voltage limit: {limits.discharge_voltage:.3f} V")
        print(
            f"  CCL (charge current):    {charge_current:.1f} A total "
            f"({charge_current / parallel_count:.1f} A/battery)"
        )
        print(
            f"  DCL (discharge current): {discharge_current_signed:.1f} A total "
            f"({discharge_current_signed / parallel_count:.1f} A/battery) "
            f"[raw 0x{limits.discharge_current_raw:04X}]"
        )
        print(f"  Charge/discharge status: 0x{limits.status:02X}")
    print(f"Batteries responding: {len(batteries)}")

    for battery in batteries:
        calculated = pack_voltage - sum(battery.cells)
        cells = [*battery.cells, calculated]
        print(f"\nBattery address {battery.address:02X}")
        for index, voltage in enumerate(cells, start=1):
            source = "calculated" if index == 16 else "measured"
            print(f"  Cell {index:02d}: {voltage:7.3f} V ({source})")

        if not 2.5 <= calculated <= 4.5:
            print("  WARNING: calculated cell 16 is outside a normal LiFePO4 range")

        print("  Temperatures:")
        for index, temperature in enumerate(battery.temperatures, start=1):
            print(f"    Sensor {index:02d}: {temperature:5.1f} deg C")

        if battery.temperatures:
            minimum = min(battery.temperatures)
            maximum = max(battery.temperatures)
            average = sum(battery.temperatures) / len(battery.temperatures)
            print(f"  Temperature range: {minimum:.1f}-{maximum:.1f} deg C (average {average:.1f} deg C)")
            if maximum >= 60:
                print("  WARNING: high battery temperature")
            elif maximum >= 45:
                print("  NOTICE: elevated battery temperature")
            else:
                print("  Temperature interpretation: normal operating range")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
