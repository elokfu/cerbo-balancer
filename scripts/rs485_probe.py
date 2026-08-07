#!/usr/bin/env python3
"""Narrow, read-only Modbus RTU probe for the Dyness B3 RS485 port.

The script implements only function codes 03 and 04. It has no Modbus write
function and does not alter serial or battery configuration. The default test
is deliberately limited to slave address 1 and the candidate register blocks.
"""

import argparse
import json
import struct
import sys
import time

import serial


BLOCKS = ((0, 4), (21, 16), (49, 4))


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def request(slave, function, start, count):
    body = struct.pack(">BBHH", slave, function, start, count)
    checksum = crc16(body)
    return body + struct.pack("<H", checksum)


def parse_response(response, slave, function, count):
    if len(response) < 5:
        return None, None, "short response"
    if response[0] != slave:
        return None, None, "unexpected slave address"
    if response[1] == function | 0x80:
        return None, response, f"exception code {response[2]:02x}"
    if response[1] != function:
        return None, response, "unexpected function"
    byte_count = response[2]
    expected = 3 + byte_count + 2
    if len(response) != expected:
        return None, response, "unexpected response length"
    if byte_count != count * 2:
        return None, response, "unexpected byte count"
    received_crc = struct.unpack("<H", response[-2:])[0]
    if crc16(response[:-2]) != received_crc:
        return None, response, "CRC mismatch"
    return list(struct.unpack(">" + "H" * count, response[3:-2])), response, None


def read_response(port, expected_slave, expected_function, count):
    deadline = time.monotonic() + 0.7
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(256)
        if chunk:
            data.extend(chunk)
            if len(data) >= 3 and len(data) >= 3 + data[2] + 2:
                break
    return parse_response(bytes(data), expected_slave, expected_function, count)


def parse_addresses(value):
    addresses = []
    for part in value.split(","):
        if "-" in part:
            first, last = (int(item, 0) for item in part.split("-", 1))
            addresses.extend(range(first, last + 1))
        else:
            addresses.append(int(part, 0))
    if any(address < 1 or address > 247 for address in addresses):
        raise ValueError("Modbus RTU slave addresses must be 1..247")
    return sorted(set(addresses))


def main():
    parser = argparse.ArgumentParser(description="Read-only Dyness B3 Modbus RTU probe")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--addresses", default="auto", help="auto, comma-separated addresses, or ranges")
    parser.add_argument("--baud", default="9600", help="baud rate; B3 test is fixed at 9600")
    args = parser.parse_args()

    addresses = list(range(1, 248)) if args.addresses == "auto" else parse_addresses(args.addresses)
    baud_rates = [int(value) for value in args.baud.split(",")]
    results = []
    for baud_rate in baud_rates:
        with serial.Serial(args.port, baudrate=baud_rate, bytesize=8, parity="N", stopbits=1, timeout=0.05) as port:
            found = []
            for slave in addresses:
                for function in (3, 4):
                    start, count = BLOCKS[0]
                    frame = request(slave, function, start, count)
                    sent_at = time.time()
                    port.reset_input_buffer()
                    port.write(frame)
                    port.flush()
                    registers, response, error = read_response(port, slave, function, count)
                    results.append({
                        "timestamp": sent_at,
                        "port": args.port,
                        "baudRate": baud_rate,
                        "parity": "N",
                        "slave": slave,
                        "function": function,
                        "start": start,
                        "count": count,
                        "tx": frame.hex().upper(),
                        "rx": response.hex().upper() if response else None,
                        "registers": registers,
                        "valid": error is None,
                        "error": error,
                    })
                    if error is None:
                        found.append((slave, function))
                        break
                if found:
                    break
            for slave, function in found:
                for start, count in BLOCKS[1:]:
                    frame = request(slave, function, start, count)
                    sent_at = time.time()
                    port.reset_input_buffer()
                    port.write(frame)
                    port.flush()
                    registers, response, error = read_response(port, slave, function, count)
                    results.append({
                        "timestamp": sent_at,
                        "port": args.port,
                        "baudRate": baud_rate,
                        "parity": "N",
                        "slave": slave,
                        "function": function,
                        "start": start,
                        "count": count,
                        "tx": frame.hex().upper(),
                        "rx": response.hex().upper() if response else None,
                        "registers": registers,
                        "valid": error is None,
                        "error": error,
                    })
            if found:
                break
    for result in results:
        print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
