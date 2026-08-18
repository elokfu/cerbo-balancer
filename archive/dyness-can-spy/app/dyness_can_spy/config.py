import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG = "/data/dyness-can-spy/config.ini"


@dataclass
class Config:
    data_dir: Path = Path("/data/dyness-can-spy")
    interface: str = "auto"
    dbus_service: str = "com.victronenergy.battery.rs485_dyness"
    pack_voltage_path: str = "/Dc/0/Voltage"
    cell_path_template: str = "/Voltages/Cell{index}"
    cell16_direct: bool = False
    telemetry_json_path: Path = Path("/data/home/nodered/cerbo-balancer-latest.json")
    max_telemetry_age_s: float = 30.0
    telemetry_interval_s: float = 1.0
    host: str = "0.0.0.0"
    port: int = 8765
    max_session_mb: int = 512
    min_free_mb: int = 256
    min_samples: int = 20
    min_movement_mv: float = 10.0
    max_lag_s: float = 10.0
    lag_step_s: float = 0.5
    max_hold_s: float = 10.0

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_config(path: Optional[str] = None) -> Config:
    cfg = Config()
    requested = path or os.environ.get("DYNESS_CAN_SPY_CONFIG") or DEFAULT_CONFIG
    parser = configparser.ConfigParser()
    if not Path(requested).exists():
        return cfg
    parser.read(requested, encoding="utf-8")
    get = parser.get
    getint = parser.getint
    getfloat = parser.getfloat
    getbool = parser.getboolean
    cfg.data_dir = Path(get("storage", "data_dir", fallback=str(cfg.data_dir)))
    cfg.max_session_mb = getint("storage", "max_session_mb", fallback=cfg.max_session_mb)
    cfg.min_free_mb = getint("storage", "min_free_mb", fallback=cfg.min_free_mb)
    cfg.interface = get("can", "interface", fallback=cfg.interface)
    cfg.dbus_service = get("dbus", "service", fallback=cfg.dbus_service)
    cfg.pack_voltage_path = get("dbus", "pack_voltage_path", fallback=cfg.pack_voltage_path)
    cfg.cell_path_template = get("dbus", "cell_path_template", fallback=cfg.cell_path_template)
    cfg.cell16_direct = getbool("dbus", "cell16_direct", fallback=cfg.cell16_direct)
    cfg.telemetry_json_path = Path(get("dbus", "telemetry_json_path", fallback=str(cfg.telemetry_json_path)))
    cfg.max_telemetry_age_s = getfloat("dbus", "max_telemetry_age_s", fallback=cfg.max_telemetry_age_s)
    cfg.telemetry_interval_s = getfloat("dbus", "poll_interval_s", fallback=cfg.telemetry_interval_s)
    cfg.host = get("web", "host", fallback=cfg.host)
    cfg.port = getint("web", "port", fallback=cfg.port)
    cfg.min_samples = getint("analysis", "min_samples", fallback=cfg.min_samples)
    cfg.min_movement_mv = getfloat("analysis", "min_movement_mv", fallback=cfg.min_movement_mv)
    cfg.max_lag_s = getfloat("analysis", "max_lag_s", fallback=cfg.max_lag_s)
    cfg.lag_step_s = getfloat("analysis", "lag_step_s", fallback=cfg.lag_step_s)
    cfg.max_hold_s = getfloat("analysis", "max_hold_s", fallback=cfg.max_hold_s)
    return cfg


def default_config_text() -> str:
    return """[storage]
data_dir = /data/dyness-can-spy
max_session_mb = 512
min_free_mb = 256

[can]
# auto selects the first active SocketCAN interface receiving traffic.
interface = auto

[dbus]
service = com.victronenergy.battery.rs485_dyness
pack_voltage_path = /Dc/0/Voltage
cell_path_template = /Voltages/Cell{index}
# Keep false when the service's Cell16 is reconstructed from pack voltage.
cell16_direct = false
telemetry_json_path = /data/home/nodered/cerbo-balancer-latest.json
max_telemetry_age_s = 30
poll_interval_s = 1.0

[web]
host = 0.0.0.0
port = 8765

[analysis]
min_samples = 20
min_movement_mv = 10
max_lag_s = 10
lag_step_s = 0.5
max_hold_s = 10
"""
