from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CANFrame:
    wall_ts: float
    mono_ts: float
    can_id: int
    is_extended: bool
    is_remote: bool
    is_error: bool
    is_local: bool
    dlc: int
    data: bytes
    interface: str


@dataclass(frozen=True)
class CellReading:
    wall_ts: float
    mono_ts: float
    cell_index: int
    voltage_v: float
    reconstructed: bool
    source: str
    pack_voltage_v: Optional[float] = None
