from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from chn4_bridge import __version__, transport
from chn4_bridge.capture import load_capture
from chn4_bridge.cli import main

from helpers import REAL_CAPTURE


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_missing_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_decode_prints_the_four_channels(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["decode", str(REAL_CAPTURE)]) == 0
    out = capsys.readouterr().out
    for channel in ("CH1", "CH2", "CH3", "CH4"):
        assert channel in out
    assert "charge(1)" in out


def test_decode_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["decode", str(REAL_CAPTURE), "--json", "--experimental"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    last = json.loads(lines[-1])
    assert last["channels"][0]["adc_raw"] == 3224
    assert "experimental" in last["channels"][0]


def test_decode_errors_have_a_clear_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.bin"
    broken.write_bytes(b"\x01\x02")
    assert main(["decode", str(broken)]) == 1
    assert "error:" in capsys.readouterr().err


def test_read_demo_json_is_marked_as_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["read", "--demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "simulation"
    assert data["simulated"] is True


def test_monitor_replay_runs_to_the_end(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["monitor", "--replay", str(REAL_CAPTURE), "--speed", "0", "--no-clear"]) == 0
    out = capsys.readouterr().out
    assert out.count("CH1 ") >= 2
    assert "0.87" in out


def test_record_csv_jsonl_and_raw(tmp_path: Path) -> None:
    csv_path, jsonl_path, raw_path = tmp_path / "out.csv", tmp_path / "out.jsonl", tmp_path / "raw.jsonl"
    code = main(
        [
            "record",
            "--replay",
            str(REAL_CAPTURE),
            "--speed",
            "0",
            "--csv",
            str(csv_path),
            "--jsonl",
            str(jsonl_path),
            "--raw",
            str(raw_path),
            "--note",
            "test run",
        ]
    )
    assert code == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 8
    assert {row["channel"] for row in rows} == {"1", "2", "3", "4"}
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 2
    raw = load_capture(raw_path)
    assert raw.header["note"] == "test run"
    assert raw.header["source"] == "replay"
    assert [frame.channels for frame in raw.frames] == [frame.channels for frame in load_capture(REAL_CAPTURE).frames]


def test_record_count_limit(tmp_path: Path) -> None:
    out = tmp_path / "demo.jsonl"
    assert main(["record", "--demo", "--interval", "0.2", "--count", "2", "--jsonl", str(out)]) == 0
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["simulated"] for record in records)


def test_record_requires_an_output() -> None:
    assert main(["record", "--demo"]) == 2


def test_calibrate_resistance_dry_run_and_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"server": {"port": 9100}}))
    base = ["--config", str(config_path), "calibrate-resistance", "--replay", str(REAL_CAPTURE), "--speed", "0"]
    assert main([*base, "--ch1", "14", "--ch3", "18", "--samples", "2"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert "resistance" not in yaml.safe_load(config_path.read_text())
    assert main([*base, "--ch1", "14", "--ch3", "18", "--samples", "2", "--write"]) == 0
    stored = yaml.safe_load(config_path.read_text())
    assert stored["resistance"]["offsets_mohm"] == {"ch1": 34.0, "ch3": 40.0}
    assert stored["server"]["port"] == 9100


def test_calibrate_requires_a_channel() -> None:
    assert main(["calibrate-resistance", "--demo"]) == 2


def test_invalid_configuration_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("acquisition:\n  interval_s: -1\n")
    assert main(["--config", str(bad), "read", "--demo"]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_serve_refuses_remote_host_without_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", "--demo", "--host", "0.0.0.0"]) == 2
    assert "allow-remote" in capsys.readouterr().err


def test_probes_without_hardware(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(transport, "_connected_probes", list)
    assert main(["probes"]) == 3
    assert "No debug probe found" in capsys.readouterr().out


def test_hardware_read_without_probe_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(transport, "_connected_probes", list)
    monkeypatch.setattr(transport, "_import_pyocd", lambda: (None, object))
    assert main(["read"]) == 3
