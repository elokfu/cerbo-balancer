#!/usr/bin/env python3
"""Read-only Pylontech-compatible ASCII probe for a Dyness RS485 port.

The probe implements only the documented Pylontech read commands. It never
sends a write, wake, configuration, or charge/discharge-control command.
"""

import argparse
import json
import sys
import time

import serial


READ_COMMANDS = ((0x4F, b""), (0x51, b""), (0x42, b"FF"), (0x44, b"FF"))


def checksum(data):
    return (-sum(data)) & 0xFFFF


def length_field(info):
    length = len(info)
    if length > 0xFFF:
        raise ValueError("Pylontech INFO is too long")
    length_checksum = (-(length >> 8) - ((length >> 4) & 0xF) - (length & 0xF)) & 0xF
    return f"{length_checksum:X}{length:03X}".encode("ascii")


def frame(address, cid2, info=b"", version=b"20"):
    body = b"".join(
        (
            version,
            f"{address:02X}".encode("ascii"),
            b"46",
            f"{cid2:02X}".encode("ascii"),
            length_field(info),
            info,
        )
    )
    return b"~" + body + f"{checksum(body):04X}".encode("ascii") + b"\r"


def parse_response(response, address, cid2):
    if not response or response[0:1] != b"~" or response[-1:] != b"\r":
        return None, "invalid start/end"
    if len(response) < 17:
        return None, "short response"
    body = response[1:-5]
    received_checksum = response[-5:-1]
    try:
        version = response[1:3].decode("ascii")
        response_address = int(response[3:5], 16)
        cid1 = response[5:7].decode("ascii")
        rtn = int(response[7:9], 16)
        encoded_length = response[9:13].decode("ascii")
        length = int(encoded_length[1:], 16)
        info = response[13:-5]
        expected_checksum = f"{checksum(body):04X}".encode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None, "invalid ASCII field"
    if response_address != address:
        return None, "unexpected address"
    if cid1 != "46":
        return None, "unexpected CID1"
    if len(info) != length:
        return None, "length mismatch"
    if received_checksum.upper() != expected_checksum:
        return None, "checksum mismatch"
    return {
        "version": version,
        "address": response_address,
        "cid1": cid1,
        "cid2": cid2,
        "returnCode": rtn,
        "length": length,
        "infoAscii": info.decode("ascii", errors="replace"),
        "infoHex": info.hex().upper(),
    }, None


def read_frame(port, address, cid2, info, timeout):
    request = frame(address, cid2, info)
    port.reset_input_buffer()
    sent_at = time.time()
    port.write(request)
    port.flush()
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(256)
        if chunk:
            response.extend(chunk)
            if response[-1:] == b"\r":
                break
    parsed, error = parse_response(bytes(response), address, cid2)
    return {
        "timestamp": sent_at,
        "port": port.port,
        "baudRate": port.baudrate,
        "parity": "N",
        "address": address,
        "cid1": "46",
        "cid2": f"{cid2:02X}",
        "tx": request.decode("ascii", errors="replace").replace("\r", "\\r"),
        "rx": bytes(response).hex().upper() if response else None,
        "valid": error is None,
        "error": error,
        "response": parsed,
    }


def parse_addresses(value):
    addresses = []
    for part in value.split(","):
        if "-" in part:
            first, last = (int(item, 0) for item in part.split("-", 1))
            addresses.extend(range(first, last + 1))
        else:
            addresses.append(int(part, 0))
    if any(address < 1 or address > 254 for address in addresses):
        raise ValueError("Pylontech addresses must be 1..254")
    return sorted(set(addresses))


def main():
    parser = argparse.ArgumentParser(description="Read-only Pylontech RS485 probe")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--addresses", default="1-254")
    parser.add_argument("--timeout", type=float, default=0.7)
    args = parser.parse_args()
    addresses = parse_addresses(args.addresses)
    with serial.Serial(
        args.port,
        baudrate=args.baud,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.05,
    ) as port:
        for address in addresses:
            discovery = read_frame(port, address, *READ_COMMANDS[0], args.timeout)
            print(json.dumps(discovery, separators=(",", ":")), flush=True)
            if not discovery["valid"]:
                continue
            for cid2, info in READ_COMMANDS[1:]:
                result = read_frame(port, address, cid2, info, args.timeout)
                print(json.dumps(result, separators=(",", ":")), flush=True)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
