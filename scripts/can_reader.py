#!/usr/bin/env python3
"""Receive-only SocketCAN reader for the Dyness BMS bus.

This process intentionally exposes only the SocketCAN receive path. It has no
CAN transmit operation and must not be used to configure an interface.
"""

import argparse
import json
import signal
import socket
import struct
import sys
import time


CAN_FRAME = struct.Struct("=IB3x8s")
running = True


def stop(_signum, _frame):
    global running
    running = False


def read_frame(sock, interface):
    raw = sock.recv(CAN_FRAME.size)
    can_id, dlc, data = CAN_FRAME.unpack(raw[: CAN_FRAME.size])
    dlc = min(dlc, 8)
    return {
        "timestamp": time.time(),
        "interface": interface,
        "id": can_id & 0x1FFFFFFF,
        "dlc": dlc,
        "data": list(data[:dlc]),
        "error": bool(can_id & socket.CAN_ERR_FLAG),
    }


def main():
    parser = argparse.ArgumentParser(description="Receive-only SocketCAN JSON reader")
    parser.add_argument("--interface", default="can1")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as sock:
        sock.bind((args.interface,))
        sock.settimeout(1.0)
        while running:
            try:
                frame = read_frame(sock, args.interface)
            except socket.timeout:
                continue
            except OSError as exc:
                if running:
                    print(json.dumps({"timestamp": time.time(), "interface": args.interface, "error": str(exc)}), flush=True)
                break
            print(json.dumps(frame, separators=(",", ":")), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
