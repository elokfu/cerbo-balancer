import os
import select
import socket
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional

from .models import CANFrame


CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_FRAME = struct.Struct("=IB3x8s")


def parse_can_frame(raw: bytes, interface: str, wall_ts: Optional[float] = None,
                    mono_ts: Optional[float] = None, msg_flags: int = 0) -> CANFrame:
    if len(raw) < CAN_FRAME.size:
        raise ValueError("short CAN frame: %d bytes" % len(raw))
    encoded_id, dlc, payload = CAN_FRAME.unpack(raw[:CAN_FRAME.size])
    extended = bool(encoded_id & CAN_EFF_FLAG)
    is_error = bool(encoded_id & CAN_ERR_FLAG)
    can_id = encoded_id & (CAN_EFF_MASK if (extended or is_error) else CAN_SFF_MASK)
    dlc = min(int(dlc), 8)
    return CANFrame(
        wall_ts=time.time() if wall_ts is None else wall_ts,
        mono_ts=time.monotonic() if mono_ts is None else mono_ts,
        can_id=can_id,
        is_extended=extended,
        is_remote=bool(encoded_id & CAN_RTR_FLAG),
        is_error=is_error,
        is_local=bool(msg_flags & socket.MSG_DONTROUTE),
        dlc=dlc,
        data=b"" if encoded_id & CAN_RTR_FLAG else payload[:dlc],
        interface=interface,
    )


def socketcan_interfaces(sys_class_net: Path = Path("/sys/class/net")) -> List[Dict]:
    result = []
    if not sys_class_net.exists():
        return result
    for path in sorted(sys_class_net.glob("can*")):
        try:
            if int((path / "type").read_text().strip()) != 280:
                continue
            state = (path / "operstate").read_text().strip()
            result.append({"name": path.name, "state": state, "active": state in ("up", "unknown")})
        except (OSError, ValueError):
            continue
    return result


class SocketCANReceiver:
    """Receive-only SocketCAN adapter. This class intentionally has no send API."""

    def __init__(self, interface: str):
        if not hasattr(socket, "AF_CAN"):
            raise RuntimeError("SocketCAN is not available on this platform")
        self.interface = interface
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.bind((interface,))

    def receive(self, timeout: float = 1.0) -> Optional[CANFrame]:
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        raw, _, msg_flags, _ = self.sock.recvmsg(CAN_FRAME.size)
        wall = time.time()
        mono = time.monotonic()
        return parse_can_frame(raw, self.interface, wall, mono, msg_flags)

    def close(self) -> None:
        self.sock.close()


def resolve_interface(requested: str, listen_s: float = 0.25) -> str:
    if requested != "auto":
        names = [row["name"] for row in socketcan_interfaces()]
        if requested not in names:
            raise RuntimeError("SocketCAN interface not found: %s" % requested)
        return requested
    candidates = [row["name"] for row in socketcan_interfaces() if row["active"]]
    if not candidates:
        raise RuntimeError("no active SocketCAN interface found")
    for name in candidates:
        receiver = None
        try:
            receiver = SocketCANReceiver(name)
            if receiver.receive(listen_s) is not None:
                return name
        except OSError:
            pass
        finally:
            if receiver:
                receiver.close()
    raise RuntimeError("CAN interfaces exist but no traffic was observed: %s" % ", ".join(candidates))
