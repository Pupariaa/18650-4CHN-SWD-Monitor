from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chn4_bridge.config import Config, ConfigError, load_config, parse_config, write_resistance_offsets

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_defaults_are_conservative() -> None:
    config = Config()
    assert config.probe.frequency_hz == 100_000
    assert config.server.host == "127.0.0.1"
    assert config.server.allow_remote is False
    assert config.acquisition.interval_s == 1.0
    assert config.resistance.offsets_mohm == {1: None, 2: None, 3: None, 4: None}


def test_missing_default_file_gives_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_config() == Config()


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_example_configuration_is_valid() -> None:
    config = load_config(EXAMPLE)
    assert config.resistance.offsets_mohm == {1: 34.0, 2: 34.0, 3: 40.0, 4: 40.0}
    assert config.adc.conversion().volts_per_count == pytest.approx(4.4 / 4095)


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match=r"acquisition\.intervall_s"):
        parse_config({"acquisition": {"intervall_s": 2}})


@pytest.mark.parametrize(
    "document",
    [
        {"probe": {"frequency_hz": 1000}},
        {"acquisition": {"interval_s": "fast"}},
        {"acquisition": {"interval_s": 10, "stale_after_s": 5}},
        {"adc": {"min_valid_v": 4.5}},
        {"resistance": {"offsets_mohm": {"ch5": 3}}},
        {"resistance": {"offsets_mohm": {"ch1": "a"}}},
        {"server": {"port": 70000}},
        {"server": {"allow_remote": "yes"}},
    ],
)
def test_invalid_values_are_rejected(document: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_config(document)


def test_network_exposure_requires_explicit_opt_in() -> None:
    with pytest.raises(ConfigError, match="allow_remote"):
        parse_config({"server": {"host": "0.0.0.0"}})
    config = parse_config({"server": {"host": "0.0.0.0", "allow_remote": True}})
    assert config.server.is_loopback is False
    assert parse_config({"server": {"host": "::1"}}).server.is_loopback


def test_reconnect_attempts_can_be_unlimited() -> None:
    assert parse_config({"acquisition": {"reconnect_attempts": None}}).acquisition.reconnect_attempts is None
    assert parse_config({"acquisition": {"reconnect_attempts": 0}}).acquisition.reconnect_attempts == 0
    assert parse_config({}).acquisition.reconnect_attempts == 5


def test_resistance_offsets_accept_numeric_and_prefixed_keys() -> None:
    config = parse_config({"resistance": {"offsets_mohm": {"ch1": 34, 3: 40.5}}})
    assert config.resistance.offsets_mohm == {1: 34.0, 2: None, 3: 40.5, 4: None}


def test_write_resistance_offsets_preserves_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"server": {"port": 9000}, "resistance": {"offsets_mohm": {"ch2": 30}}}))
    write_resistance_offsets(path, {1: 34.25, 4: 40})
    config = load_config(path)
    assert config.server.port == 9000
    assert config.resistance.offsets_mohm == {1: 34.25, 2: 30.0, 3: None, 4: 40.0}


def test_write_resistance_offsets_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "new.yaml"
    write_resistance_offsets(path, {3: 12.5})
    assert load_config(path).resistance.offsets_mohm[3] == 12.5
