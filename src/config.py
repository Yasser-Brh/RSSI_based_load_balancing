"""Configuration loader.

All settings are read from environment variables (or a local .env file).
No credentials should ever be hard-coded here; use .env.example as a template.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Optional


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT_DIR / ".env"


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    """Minimal .env loader: sets environment variables that are not already defined.

    Lines starting with '#' and blank lines are ignored.
    Values may optionally be wrapped in single or double quotes.
    Already-set environment variables are never overwritten.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class RadioConfig:
    band: str            # "2G", "5G", "6G"
    interface_name: str  # e.g. phy0.0-ap0
    uci_radio: str       # e.g. radio0


@dataclass(frozen=True)
class APConfig:
    name: str
    device_id: str
    device_key: str
    control_device_id: str
    radios: tuple        # tuple[RadioConfig, ...]


@dataclass(frozen=True)
class AppConfig:
    auth_url: str
    static_token: str          # OPENWISP_TOKEN (prioritaire sur username/password)
    base_url: str
    username: str
    password: str
    verify_ssl: bool
    db_path: Path
    poll_interval: float
    client_diff_threshold: int
    txpower_step: int
    min_txpower: int
    max_txpower: int
    control_cooldown: int
    target_ssid: str
    aps: tuple[APConfig, APConfig]


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_optional(name: str, default: str) -> str:
    return os.getenv(name, default)


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _load_ap(prefix: str) -> APConfig:
    """Build an APConfig from environment variables sharing the given prefix (e.g. 'AP1').

    At least one radio band must be configured via {PREFIX}_RADIO_{2G|5G|6G}_IFACE
    and {PREFIX}_RADIO_{2G|5G|6G}_UCI.
    """
    device_id = _get_required(f"{prefix}_DEVICE_ID")
    radios = []
    for band, suffix in [("2G", "2G"), ("5G", "5G"), ("6G", "6G")]:
        iface = os.getenv(f"{prefix}_RADIO_{suffix}_IFACE", "").strip()
        uci   = os.getenv(f"{prefix}_RADIO_{suffix}_UCI",   "").strip()
        if iface and uci:
            radios.append(RadioConfig(band=band, interface_name=iface, uci_radio=uci))
    if not radios:
        raise ValueError(
            f"No radios configured for {prefix}. "
            f"Set {prefix}_RADIO_2G_IFACE / _UCI, {prefix}_RADIO_5G_IFACE / _UCI, "
            f"{prefix}_RADIO_6G_IFACE / _UCI in your .env."
        )
    return APConfig(
        name=_get_optional(f"{prefix}_NAME", prefix),
        device_id=device_id,
        device_key=_get_required(f"{prefix}_DEVICE_KEY"),
        control_device_id=_get_optional(f"{prefix}_CONTROL_DEVICE_ID", device_id) or device_id,
        radios=tuple(radios),
    )


def load_config() -> AppConfig:
    """Build an AppConfig from environment variables (or a .env file).

    Raises ValueError for any missing required variable.
    See .env.example for the full list of supported variables.
    """
    load_dotenv()
    return AppConfig(
        auth_url=_get_optional("OPENWISP_AUTH_URL", "https://openwisp.example.com/api/v1/users/token/"),
        static_token=_get_optional("OPENWISP_TOKEN", ""),
        base_url=_get_optional("OPENWISP_BASE_URL", "https://openwisp.example.com"),
        username=_get_optional("OPENWISP_USERNAME", ""),
        password=_get_optional("OPENWISP_PASSWORD", ""),
        verify_ssl=_get_bool("OPENWISP_VERIFY_SSL", False),
        db_path=Path(_get_optional("OPENWISP_DB_PATH", str(ROOT_DIR / "rssi_lb.sqlite3"))),
        poll_interval=_get_float("OPENWISP_POLL_INTERVAL", 5.0),
        client_diff_threshold=_get_int("OPENWISP_CLIENT_DIFF_THRESHOLD", 2),
        txpower_step=_get_int("OPENWISP_TXPOWER_STEP", 3),
        min_txpower=_get_int("OPENWISP_MIN_TXPOWER", 8),
        max_txpower=_get_int("OPENWISP_MAX_TXPOWER", 23),
        control_cooldown=_get_int("OPENWISP_CONTROL_COOLDOWN", 30),
        target_ssid=_get_optional("OPENWISP_TARGET_SSID", ""),
        aps=(_load_ap("AP1"), _load_ap("AP2")),
    )
