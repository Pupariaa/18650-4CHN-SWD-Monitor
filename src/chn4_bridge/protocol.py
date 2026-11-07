"""Version 1 of the WebSocket message format.

Every message is a JSON object with the same envelope::

    {"schema_version": 1, "type": "...", "timestamp_utc": "...Z", "data": {...}}

The protocol is read-only: the server never acts on client messages. See
``docs/WEBSOCKET_PROTOCOL.md`` for the full specification and the
compatibility policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ._version import __version__
from .acquisition import ErrorEvent, StatusEvent
from .export import iso_utc, snapshot_to_dict
from .layout import CHANNEL_COUNT
from .models import FIELD_QUALITY, UNITS, Snapshot, SourceKind

PROTOCOL_NAME = "chn4-bridge"
SCHEMA_VERSION = 1

HELLO = "hello"
SNAPSHOT = "snapshot"
PROBE_STATUS = "probe_status"
ERROR = "error"
SHUTDOWN = "shutdown"

MESSAGE_TYPES = (HELLO, SNAPSHOT, PROBE_STATUS, ERROR, SHUTDOWN)


def envelope(message_type: str, data: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    if message_type not in MESSAGE_TYPES:
        raise ValueError(f"unknown message type {message_type!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "type": message_type,
        "timestamp_utc": iso_utc(now or datetime.now(UTC)),
        "data": data,
    }


def hello(
    *,
    source: SourceKind,
    source_description: str,
    broadcast_interval_s: float,
    stale_after_s: float,
    include_experimental: bool,
) -> dict[str, Any]:
    return envelope(
        HELLO,
        {
            "protocol": PROTOCOL_NAME,
            "schema_version": SCHEMA_VERSION,
            "server_version": __version__,
            "read_only": True,
            "source": {
                "kind": source.value,
                "description": source_description,
                "simulated": source is SourceKind.SIMULATION,
            },
            "channel_count": CHANNEL_COUNT,
            "broadcast_interval_s": broadcast_interval_s,
            "stale_after_s": stale_after_s,
            "include_experimental": include_experimental,
            "units": dict(UNITS),
            "field_quality": {name: quality.value for name, quality in FIELD_QUALITY.items()},
        },
    )


def snapshot(
    value: Snapshot,
    *,
    now_monotonic: float,
    stale_after_s: float,
    include_experimental: bool = False,
) -> dict[str, Any]:
    return envelope(
        SNAPSHOT,
        snapshot_to_dict(
            value,
            include_experimental=include_experimental,
            now_monotonic=now_monotonic,
            stale_after_s=stale_after_s,
        ),
    )


def probe_status(event: StatusEvent) -> dict[str, Any]:
    return envelope(
        PROBE_STATUS,
        {
            "state": event.state.value,
            "source": event.source.value,
            "description": event.description,
            "message": event.message,
            "attempt": event.attempt,
            "changed_at": iso_utc(event.at),
        },
    )


def error(code: str, message: str, *, fatal: bool = False) -> dict[str, Any]:
    return envelope(ERROR, {"code": code, "message": message, "fatal": fatal})


def error_from_event(event: ErrorEvent) -> dict[str, Any]:
    return error(event.code, event.message, fatal=event.fatal)


def shutdown(reason: str) -> dict[str, Any]:
    return envelope(SHUTDOWN, {"reason": reason})
