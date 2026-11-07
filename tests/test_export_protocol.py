from __future__ import annotations

import csv
import io
import json
import re

import pytest

from chn4_bridge import protocol
from chn4_bridge.acquisition import ErrorEvent, LinkState, StatusEvent
from chn4_bridge.capture import load_capture
from chn4_bridge.derived import SnapshotProcessor
from chn4_bridge.export import CSV_COLUMNS, CsvWriter, JsonlWriter, dumps, snapshot_to_dict
from chn4_bridge.models import FIELD_QUALITY, UNITS, RawFrame, Snapshot, SourceKind
from chn4_bridge.synthetic import ChannelImage

from helpers import EMPTY, REAL_CAPTURE, make_frame

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _real_snapshot() -> Snapshot:
    processor = SnapshotProcessor(resistance_offsets_mohm={1: 34.0})
    snapshot = None
    for frame in load_capture(REAL_CAPTURE).frames:
        snapshot = processor.process(
            RawFrame(channels=frame.channels, adc=frame.adc, captured_at=frame.captured_at, monotonic_s=frame.t_s),
            SourceKind.REPLAY,
        )
    assert snapshot is not None
    return snapshot


def test_snapshot_dict_uses_explicit_unit_keys() -> None:
    data = snapshot_to_dict(_real_snapshot(), now_monotonic=10.0, stale_after_s=5.0)
    assert ISO_UTC.match(data["captured_at"])
    assert data["source"] == "replay"
    assert data["simulated"] is False
    assert data["age_s"] == 1.0
    assert data["stale"] is False
    channel = data["channels"][0]
    for key in ("voltage_v", "display_voltage_v", "capacity_mah", "energy_mwh", "elapsed_s", "current_recent_a"):
        assert key in channel
    assert channel["state"] == "charge"
    assert channel["resistance_compensated_mohm"] == 14.0
    assert "experimental" not in channel
    json.loads(dumps(data))


def test_absent_values_serialise_as_null() -> None:
    snapshot = SnapshotProcessor().process(make_frame(), SourceKind.SIMULATION)
    data = snapshot_to_dict(snapshot)
    assert data["simulated"] is True
    channel = data["channels"][0]
    assert channel["present"] is False
    assert channel["voltage_v"] is None
    assert channel["capacity_mah"] is None
    assert '"voltage_v":null' in dumps(data)


def test_experimental_block_is_opt_in() -> None:
    channel = snapshot_to_dict(_real_snapshot(), include_experimental=True)["channels"][0]
    assert set(channel["experimental"]) >= {"voltage_candidate_a_v", "word_0x20", "raw_hex"}


def test_dumps_rejects_nan() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        dumps({"value": float("nan")})


def test_csv_has_one_row_per_channel_and_empty_cells_for_missing_values() -> None:
    stream = io.StringIO()
    writer = CsvWriter(stream)
    writer.write(
        SnapshotProcessor().process(
            make_frame((ChannelImage(), EMPTY, EMPTY, EMPTY), adc=(0, 0, 0, 0)), SourceKind.REPLAY
        )
    )
    rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
    assert len(rows) == 4
    assert list(rows[0]) == list(CSV_COLUMNS)
    assert rows[0]["voltage_v"] == ""
    assert rows[0]["issues"] == "adc_sample_invalid"
    assert rows[1]["present"] == "False"


def test_jsonl_writer_includes_experimental_fields() -> None:
    stream = io.StringIO()
    JsonlWriter(stream).write(_real_snapshot())
    record = json.loads(stream.getvalue())
    assert "experimental" in record["channels"][0]


def _check_envelope(message: dict[str, object], kind: str) -> dict[str, object]:
    assert message["schema_version"] == protocol.SCHEMA_VERSION == 1
    assert message["type"] == kind
    assert isinstance(message["timestamp_utc"], str) and ISO_UTC.match(message["timestamp_utc"])
    data = message["data"]
    assert isinstance(data, dict)
    json.loads(dumps(message))
    return data


def test_hello_message_documents_units_and_quality() -> None:
    data = _check_envelope(
        protocol.hello(
            source=SourceKind.SIMULATION,
            source_description="demo",
            broadcast_interval_s=1.0,
            stale_after_s=5.0,
            include_experimental=False,
        ),
        "hello",
    )
    assert data["read_only"] is True
    assert data["protocol"] == "chn4-bridge"
    assert data["source"] == {"kind": "simulation", "description": "demo", "simulated": True}
    assert data["units"] == UNITS
    assert data["field_quality"]["current_recent_a"] == "estimated"
    assert data["field_quality"]["voltage_v"] == "experimental"
    assert set(data["field_quality"]) == set(FIELD_QUALITY)


def test_snapshot_status_error_and_shutdown_messages() -> None:
    data = _check_envelope(protocol.snapshot(_real_snapshot(), now_monotonic=9.0, stale_after_s=5), "snapshot")
    assert len(data["channels"]) == 4
    status = _check_envelope(
        protocol.probe_status(StatusEvent(LinkState.DISCONNECTED, SourceKind.HARDWARE, "WCH-Link", "unplugged", 2)),
        "probe_status",
    )
    assert status["state"] == "disconnected"
    assert status["attempt"] == 2
    error = _check_envelope(protocol.error_from_event(ErrorEvent("probe_busy", "busy", fatal=False)), "error")
    assert error == {"code": "probe_busy", "message": "busy", "fatal": False}
    assert _check_envelope(protocol.shutdown("server_stopping"), "shutdown") == {"reason": "server_stopping"}


def test_unknown_message_type_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown message type"):
        protocol.envelope("start_charge", {})
