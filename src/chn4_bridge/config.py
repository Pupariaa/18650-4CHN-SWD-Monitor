"""YAML configuration loading and validation."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .decoder import AdcConversion
from .derived import EstimatorSettings
from .layout import ADC_FULL_SCALE_COUNTS, CHANNEL_COUNT

DEFAULT_CONFIG_NAME = "config.yaml"


class ConfigError(ValueError):
    """Raised when the configuration file is missing keys or has bad values."""


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    unique_id: str | None = None
    frequency_hz: int = 100_000


@dataclass(frozen=True, slots=True)
class AcquisitionConfig:
    interval_s: float = 1.0
    consistency_reads: int = 2
    consistency_retries: int = 2
    read_timeout_s: float = 5.0
    reconnect_attempts: int | None = 5
    reconnect_delay_s: float = 3.0
    stale_after_s: float = 5.0


@dataclass(frozen=True, slots=True)
class AdcConfig:
    full_scale_v: float = 4.4
    full_scale_counts: int = ADC_FULL_SCALE_COUNTS
    min_valid_v: float = 2.0
    max_valid_v: float = 4.4

    def conversion(self) -> AdcConversion:
        return AdcConversion(
            volts_per_count=self.full_scale_v / self.full_scale_counts,
            min_valid_v=self.min_valid_v,
            max_valid_v=self.max_valid_v,
        )


@dataclass(frozen=True, slots=True)
class ResistanceConfig:
    offsets_mohm: dict[int, float | None] = field(
        default_factory=lambda: {channel: None for channel in range(1, CHANNEL_COUNT + 1)}
    )


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    allow_remote: bool = False
    broadcast_interval_s: float = 1.0
    client_queue_size: int = 8
    send_timeout_s: float = 5.0
    heartbeat_interval_s: float = 20.0
    heartbeat_timeout_s: float = 20.0
    max_clients: int = 16
    allowed_origins: tuple[str, ...] | None = None
    include_experimental: bool = False

    @property
    def is_loopback(self) -> bool:
        return is_loopback_host(self.host)


@dataclass(frozen=True, slots=True)
class Config:
    """Complete bridge configuration. Every section has safe defaults."""

    probe: ProbeConfig = field(default_factory=ProbeConfig)
    acquisition: AcquisitionConfig = field(default_factory=AcquisitionConfig)
    estimation: EstimatorSettings = field(default_factory=EstimatorSettings)
    adc: AdcConfig = field(default_factory=AdcConfig)
    resistance: ResistanceConfig = field(default_factory=ResistanceConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    path: Path | None = None


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(path: str | Path | None = None) -> Config:
    """Load a configuration file.

    With ``path=None`` the loader uses ``config.yaml`` from the current
    directory when it exists and falls back to the built-in defaults
    otherwise. An explicit path that does not exist is an error.
    """
    if path is None:
        candidate = Path(DEFAULT_CONFIG_NAME)
        if not candidate.is_file():
            return Config()
        path = candidate
    resolved = Path(path)
    if not resolved.is_file():
        raise ConfigError(f"configuration file not found: {resolved}")
    try:
        document = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved}: invalid YAML: {exc}") from exc
    return parse_config(document or {}, path=resolved)


def parse_config(document: Any, path: Path | None = None) -> Config:
    """Build a :class:`Config` from an already parsed mapping."""
    root = _mapping(document, "configuration")
    _reject_unknown(root, {"probe", "acquisition", "estimation", "adc", "resistance", "server"}, "")
    return Config(
        probe=_parse_probe(_section(root, "probe")),
        acquisition=_parse_acquisition(_section(root, "acquisition")),
        estimation=_parse_estimation(_section(root, "estimation")),
        adc=_parse_adc(_section(root, "adc")),
        resistance=_parse_resistance(_section(root, "resistance")),
        server=_parse_server(_section(root, "server")),
        path=path,
    )


def write_resistance_offsets(path: str | Path, offsets: Mapping[int, float | None]) -> None:
    """Store per-channel resistance offsets in a configuration file.

    Other sections are preserved. The file is created if it does not exist.
    """
    target = Path(path)
    document: dict[str, Any] = {}
    if target.is_file():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        document = dict(_mapping(loaded, "configuration"))
    resistance = dict(_mapping(document.get("resistance") or {}, "resistance"))
    current = dict(_mapping(resistance.get("offsets_mohm") or {}, "resistance.offsets_mohm"))
    for channel, value in offsets.items():
        _channel_number(channel)
        current[f"ch{channel}"] = None if value is None else round(float(value), 3)
    resistance["offsets_mohm"] = current
    document["resistance"] = resistance
    parse_config(document)
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _parse_probe(section: Mapping[str, Any]) -> ProbeConfig:
    _reject_unknown(section, {"unique_id", "frequency_hz"}, "probe")
    defaults = ProbeConfig()
    unique_id = section.get("unique_id", defaults.unique_id)
    if unique_id is not None:
        unique_id = str(unique_id)
    return ProbeConfig(
        unique_id=unique_id,
        frequency_hz=_int(section, "frequency_hz", defaults.frequency_hz, "probe", 10_000, 4_000_000),
    )


def _parse_acquisition(section: Mapping[str, Any]) -> AcquisitionConfig:
    name = "acquisition"
    _reject_unknown(section, set(AcquisitionConfig.__dataclass_fields__), name)
    d = AcquisitionConfig()
    attempts: int | None = None
    if section.get("reconnect_attempts", d.reconnect_attempts) is not None:
        attempts = _int(section, "reconnect_attempts", d.reconnect_attempts or 0, name, 0, 1_000_000)
    config = AcquisitionConfig(
        interval_s=_float(section, "interval_s", d.interval_s, name, 0.2, 3600),
        consistency_reads=_int(section, "consistency_reads", d.consistency_reads, name, 1, 2),
        consistency_retries=_int(section, "consistency_retries", d.consistency_retries, name, 0, 10),
        read_timeout_s=_float(section, "read_timeout_s", d.read_timeout_s, name, 0.5, 60),
        reconnect_attempts=attempts,
        reconnect_delay_s=_float(section, "reconnect_delay_s", d.reconnect_delay_s, name, 0.1, 600),
        stale_after_s=_float(section, "stale_after_s", d.stale_after_s, name, 0.5, 3600),
    )
    if config.stale_after_s < config.interval_s:
        raise ConfigError("acquisition.stale_after_s must be greater than or equal to interval_s")
    return config


def _parse_estimation(section: Mapping[str, Any]) -> EstimatorSettings:
    name = "estimation"
    _reject_unknown(section, {"window_s", "idle_after_s", "reset_tolerance_mah"}, name)
    d = EstimatorSettings()
    return EstimatorSettings(
        window_s=_float(section, "window_s", d.window_s, name, 1, 3600),
        idle_after_s=_float(section, "idle_after_s", d.idle_after_s, name, 1, 3600),
        reset_tolerance_mah=_float(section, "reset_tolerance_mah", d.reset_tolerance_mah, name, 0, 100),
    )


def _parse_adc(section: Mapping[str, Any]) -> AdcConfig:
    name = "adc"
    _reject_unknown(section, set(AdcConfig.__dataclass_fields__), name)
    d = AdcConfig()
    config = AdcConfig(
        full_scale_v=_float(section, "full_scale_v", d.full_scale_v, name, 0.1, 100),
        full_scale_counts=_int(section, "full_scale_counts", d.full_scale_counts, name, 1, 65535),
        min_valid_v=_float(section, "min_valid_v", d.min_valid_v, name, 0, 100),
        max_valid_v=_float(section, "max_valid_v", d.max_valid_v, name, 0, 100),
    )
    if config.min_valid_v >= config.max_valid_v:
        raise ConfigError("adc.min_valid_v must be lower than adc.max_valid_v")
    return config


def _parse_resistance(section: Mapping[str, Any]) -> ResistanceConfig:
    _reject_unknown(section, {"offsets_mohm"}, "resistance")
    raw = _mapping(section.get("offsets_mohm") or {}, "resistance.offsets_mohm")
    offsets: dict[int, float | None] = {channel: None for channel in range(1, CHANNEL_COUNT + 1)}
    for key, value in raw.items():
        channel = _channel_key(key)
        if value is None:
            offsets[channel] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"resistance.offsets_mohm.{key} must be a number or null")
        if not -1000 <= float(value) <= 1000:
            raise ConfigError(f"resistance.offsets_mohm.{key} must be between -1000 and 1000")
        offsets[channel] = float(value)
    return ResistanceConfig(offsets_mohm=offsets)


def _parse_server(section: Mapping[str, Any]) -> ServerConfig:
    name = "server"
    _reject_unknown(section, set(ServerConfig.__dataclass_fields__), name)
    d = ServerConfig()
    host = section.get("host", d.host)
    if not isinstance(host, str) or not host:
        raise ConfigError("server.host must be a non-empty string")
    origins = section.get("allowed_origins", d.allowed_origins)
    if origins is not None:
        if not isinstance(origins, list) or not all(isinstance(item, str) for item in origins):
            raise ConfigError("server.allowed_origins must be a list of strings or null")
        origins = tuple(origins)
    config = ServerConfig(
        host=host,
        port=_int(section, "port", d.port, name, 0, 65535),
        allow_remote=_bool(section, "allow_remote", d.allow_remote, name),
        broadcast_interval_s=_float(section, "broadcast_interval_s", d.broadcast_interval_s, name, 0.1, 3600),
        client_queue_size=_int(section, "client_queue_size", d.client_queue_size, name, 1, 1024),
        send_timeout_s=_float(section, "send_timeout_s", d.send_timeout_s, name, 0.5, 120),
        heartbeat_interval_s=_float(section, "heartbeat_interval_s", d.heartbeat_interval_s, name, 1, 3600),
        heartbeat_timeout_s=_float(section, "heartbeat_timeout_s", d.heartbeat_timeout_s, name, 1, 3600),
        max_clients=_int(section, "max_clients", d.max_clients, name, 1, 1024),
        allowed_origins=origins,
        include_experimental=_bool(section, "include_experimental", d.include_experimental, name),
    )
    check_server_exposure(config)
    return config


def check_server_exposure(config: ServerConfig) -> None:
    """Refuse a non-loopback bind address unless it was explicitly allowed."""
    if not config.is_loopback and not config.allow_remote:
        raise ConfigError(
            f"server.host {config.host!r} is not a loopback address; "
            "set server.allow_remote: true (or pass --allow-remote) to expose the stream on the network"
        )


def _section(root: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = root.get(key)
    if value is None:
        return {}
    return _mapping(value, key)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _reject_unknown(section: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(str(key) for key in section if key not in allowed)
    if unknown:
        prefix = f"{name}." if name else ""
        raise ConfigError(f"unknown configuration key(s): {', '.join(prefix + key for key in unknown)}")


def _float(section: Mapping[str, Any], key: str, default: float, name: str, low: float, high: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name}.{key} must be a number")
    if not low <= float(value) <= high:
        raise ConfigError(f"{name}.{key} must be between {low} and {high}, got {value}")
    return float(value)


def _int(section: Mapping[str, Any], key: str, default: int, name: str, low: int, high: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}.{key} must be an integer")
    if not low <= value <= high:
        raise ConfigError(f"{name}.{key} must be between {low} and {high}, got {value}")
    return value


def _bool(section: Mapping[str, Any], key: str, default: bool, name: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name}.{key} must be true or false")
    return value


def _channel_key(key: Any) -> int:
    text = str(key).lower()
    if text.startswith("ch"):
        text = text[2:]
    try:
        return _channel_number(int(text))
    except ValueError:
        raise ConfigError(
            f"resistance.offsets_mohm: invalid channel key {key!r}, expected ch1 to ch{CHANNEL_COUNT}"
        ) from None


def _channel_number(channel: int) -> int:
    if not 1 <= channel <= CHANNEL_COUNT:
        raise ConfigError(f"channel must be between 1 and {CHANNEL_COUNT}, got {channel}")
    return channel
