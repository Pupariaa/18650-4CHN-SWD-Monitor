"""Plain-text rendering of snapshots for the command line."""

from __future__ import annotations

import os
import sys
from typing import TextIO

from ._version import __version__
from .acquisition import LinkState, StatusEvent
from .export import iso_utc
from .models import ChannelReading, Snapshot

COLUMNS: tuple[tuple[str, int], ...] = (
    ("CH", 4),
    ("STATE", 14),
    ("ACTIVITY", 9),
    ("V ADC", 9),
    ("V DISP", 9),
    ("I RECENT~", 11),
    ("I AVG~", 9),
    ("CAPACITY", 12),
    ("ENERGY", 12),
    ("R RAW", 10),
    ("R COMP", 10),
    ("ELAPSED", 11),
)

LEGEND = (
    "V ADC: continuous ADC voltage, experimental scale. V DISP: cached display value, refreshed for the selected channel.",
    "I RECENT~ / I AVG~: estimated from the capacity counter, not a direct current measurement.",
    "R COMP: raw resistance minus the configured per-channel offset (empty when no offset is set).",
)


def render_snapshot(snapshot: Snapshot, *, source: str, status: StatusEvent | None = None, legend: bool = True) -> str:
    lines = []
    if snapshot.simulated:
        lines.append("*** SIMULATED DATA - synthetic values, not measurements ***")
    header = (
        f"chn4-bridge {__version__} | {snapshot.source.value}: {source} | #{snapshot.sequence} "
        f"| {iso_utc(snapshot.captured_at)} | {snapshot.consistency.value}"
    )
    if snapshot.recorded_at is not None:
        header += f" | recorded {iso_utc(snapshot.recorded_at)}"
    lines.append(header)
    if status is not None and status.state is not LinkState.CONNECTED:
        lines.append(render_status(status))
    width = sum(size for _, size in COLUMNS)
    lines.append("-" * width)
    lines.append("".join(title.ljust(size) for title, size in COLUMNS))
    lines.append("-" * width)
    for reading in snapshot.channels:
        lines.append("".join(f"{cell} ".ljust(size) for cell, (_, size) in zip(_cells(reading), COLUMNS, strict=True)))
    lines.append("-" * width)
    issues = [f"CH{reading.channel}: {', '.join(reading.issues)}" for reading in snapshot.channels if reading.issues]
    if issues:
        lines.append("Issues: " + " | ".join(issues))
    if legend:
        lines.extend(LEGEND)
    return "\n".join(lines)


def render_status(status: StatusEvent) -> str:
    text = f"[{status.state.value}] {status.description}"
    if status.attempt:
        text += f" (attempt {status.attempt})"
    if status.message:
        text += f": {status.message}"
    return text


def _cells(reading: ChannelReading) -> tuple[str, ...]:
    state = f"{reading.state.value}({reading.state_code})"
    return (
        f"CH{reading.channel}",
        state if reading.present else "-",
        reading.activity.value,
        _number(reading.voltage_v, 3, " V"),
        _number(reading.display_voltage_v, 3, " V"),
        _number(reading.current_recent_a, 3, " A"),
        _number(reading.current_average_a, 3, " A"),
        _number(reading.capacity_mah, 1, " mAh"),
        _number(reading.energy_mwh, 1, " mWh"),
        _number(reading.resistance_raw_mohm, 0, " mOhm"),
        _number(reading.resistance_compensated_mohm, 0, " mOhm"),
        _duration(reading.elapsed_s),
    )


def _number(value: float | None, decimals: int, unit: str) -> str:
    if value is None:
        return "---"
    return f"{value:.{decimals}f}{unit}"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "---"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


class Screen:
    """Redraws output in place on interactive terminals, appends otherwise."""

    def __init__(self, stream: TextIO | None = None, *, clear: bool = True) -> None:
        self.stream = stream or sys.stdout
        self.clear = clear and self.stream.isatty() and _enable_ansi()

    def show(self, text: str) -> None:
        if self.clear:
            self.stream.write("\x1b[H\x1b[J")
        else:
            self.stream.write("\n")
        self.stream.write(text + "\n")
        self.stream.flush()


def _enable_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False
