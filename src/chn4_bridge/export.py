"""Serialisation of snapshots to JSON-compatible dictionaries, CSV and JSONL."""

from __future__ import annotations

import csv
import dataclasses
import json
import math
from datetime import UTC, datetime
from enum import Enum
from typing import IO, Any

from .models import ChannelReading, Snapshot

CHANNEL_FIELDS: tuple[str, ...] = (
    "channel",
    "present",
    "state_code",
    "state",
    "activity",
    "voltage_v",
    "adc_raw",
    "display_voltage_v",
    "capacity_mah",
    "energy_mwh",
    "elapsed_s",
    "resistance_raw_mohm",
    "resistance_compensated_mohm",
    "current_recent_a",
    "current_average_a",
    "stop_threshold_v",
    "loop_count",
)

CSV_COLUMNS: tuple[str, ...] = (
    "captured_at",
    "sequence",
    "source",
    "consistency",
    *CHANNEL_FIELDS,
    "issues",
)


def iso_utc(moment: datetime) -> str:
    """ISO 8601 UTC timestamp with millisecond precision and a ``Z`` suffix."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def channel_to_dict(reading: ChannelReading, *, include_experimental: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {name: _plain(getattr(reading, name)) for name in CHANNEL_FIELDS}
    data["issues"] = list(reading.issues)
    if include_experimental:
        data["experimental"] = {key: _plain(value) for key, value in dataclasses.asdict(reading.experimental).items()}
    return data


def snapshot_to_dict(
    snapshot: Snapshot,
    *,
    include_experimental: bool = False,
    now_monotonic: float | None = None,
    stale_after_s: float | None = None,
) -> dict[str, Any]:
    """Convert a snapshot to plain JSON types.

    When ``now_monotonic`` is given the result includes ``age_s``, and
    ``stale`` when ``stale_after_s`` is given as well.
    """
    data: dict[str, Any] = {
        "sequence": snapshot.sequence,
        "captured_at": iso_utc(snapshot.captured_at),
        "recorded_at": iso_utc(snapshot.recorded_at) if snapshot.recorded_at else None,
        "source": snapshot.source.value,
        "simulated": snapshot.simulated,
        "consistency": snapshot.consistency.value,
        "read_duration_ms": snapshot.read_duration_ms,
    }
    if now_monotonic is not None:
        age = round(snapshot.age_s(now_monotonic), 3)
        data["age_s"] = age
        if stale_after_s is not None:
            data["stale"] = age > stale_after_s
    data["channels"] = [
        channel_to_dict(reading, include_experimental=include_experimental) for reading in snapshot.channels
    ]
    return data


def dumps(value: Any) -> str:
    """Compact JSON encoding that rejects NaN and infinity."""
    return json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


class JsonlWriter:
    """Writes one snapshot per line, including the experimental fields."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, snapshot: Snapshot) -> None:
        self._stream.write(dumps(snapshot_to_dict(snapshot, include_experimental=True)) + "\n")
        self._stream.flush()


class CsvWriter:
    """Writes one row per channel and snapshot. Missing values are empty cells."""

    def __init__(self, stream: IO[str], *, write_header: bool = True) -> None:
        self._stream = stream
        self._writer = csv.DictWriter(stream, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        if write_header:
            self._writer.writeheader()
            stream.flush()

    def write(self, snapshot: Snapshot) -> None:
        for row in snapshot_rows(snapshot):
            self._writer.writerow({key: "" if value is None else value for key, value in row.items()})
        self._stream.flush()


def snapshot_rows(snapshot: Snapshot) -> list[dict[str, Any]]:
    rows = []
    for reading in snapshot.channels:
        row: dict[str, Any] = {
            "captured_at": iso_utc(snapshot.captured_at),
            "sequence": snapshot.sequence,
            "source": snapshot.source.value,
            "consistency": snapshot.consistency.value,
        }
        row.update({name: _plain(getattr(reading, name)) for name in CHANNEL_FIELDS})
        row["issues"] = ";".join(reading.issues)
        rows.append(row)
    return rows


def _plain(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    return value
