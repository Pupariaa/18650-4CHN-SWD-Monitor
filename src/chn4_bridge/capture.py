"""Raw capture files: recording and loading of undecoded SRAM frames.

Two inputs are supported:

* JSON Lines captures written by ``chn4-bridge record --raw``. The first
  line is a header, every following line holds one frame as hex strings.
* Binary SRAM images (for example a 16 KiB dump starting at 0x20000000)
  or bare 224-byte channel blocks.

Raw captures are the preferred way to share data: they only contain the
two memory ranges the bridge reads, never firmware or unrelated SRAM.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .decoder import DecodeError, split_sram_image
from .export import iso_utc as _iso
from .layout import ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE, CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE
from .models import Consistency, RawFrame

CAPTURE_FORMAT = "chn4-bridge-raw-capture"
CAPTURE_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One frame of a capture file. ``t_s`` is relative to the first frame."""

    t_s: float
    channels: bytes
    adc: bytes | None
    captured_at: datetime | None = None
    consistency: Consistency = Consistency.UNVERIFIED


@dataclass(frozen=True, slots=True)
class Capture:
    header: dict[str, Any]
    frames: tuple[CapturedFrame, ...] = field(default=())


class CaptureWriter:
    """Writes raw frames to a JSON Lines capture file."""

    def __init__(self, stream: IO[str], *, source: str, note: str | None = None) -> None:
        self._stream = stream
        self._origin: float | None = None
        header: dict[str, Any] = {
            "format": CAPTURE_FORMAT,
            "version": CAPTURE_VERSION,
            "created_at": _iso(datetime.now(UTC)),
            "source": source,
            "channel_block_address": f"0x{CHANNEL_BLOCK_ADDRESS:08X}",
            "channel_block_size": CHANNEL_BLOCK_SIZE,
            "adc_block_address": f"0x{ADC_BLOCK_ADDRESS:08X}",
            "adc_block_size": ADC_BLOCK_SIZE,
        }
        if note:
            header["note"] = note
        self._write(header)

    def write(self, frame: RawFrame) -> None:
        if self._origin is None:
            self._origin = frame.monotonic_s
        self._write(
            {
                "t_s": round(frame.monotonic_s - self._origin, 3),
                "captured_at": _iso(frame.captured_at),
                "consistency": frame.consistency.value,
                "channels": frame.channels.hex(),
                "adc": frame.adc.hex() if frame.adc is not None else None,
            }
        )

    def _write(self, record: dict[str, Any]) -> None:
        self._stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._stream.flush()


def write_capture(
    path: str | Path,
    frames: Iterable[CapturedFrame],
    *,
    source: str,
    note: str | None = None,
) -> None:
    """Write already captured frames to a JSON Lines capture file."""
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        writer = CaptureWriter(stream, source=source, note=note)
        for frame in frames:
            writer.write(
                RawFrame(
                    channels=frame.channels,
                    adc=frame.adc,
                    captured_at=frame.captured_at or datetime.now(UTC),
                    monotonic_s=frame.t_s,
                    consistency=frame.consistency,
                )
            )


def load_capture(path: str | Path) -> Capture:
    """Load a JSON Lines capture or a binary SRAM image."""
    target = Path(path)
    data = target.read_bytes()
    if data[:1] == b"{":
        return parse_capture(data.decode("utf-8"), name=str(target))
    channels, adc = split_sram_image(data)
    header = {"format": "sram-image", "file": target.name, "size": len(data)}
    return Capture(header=header, frames=(CapturedFrame(t_s=0.0, channels=channels, adc=adc),))


def load_captures(paths: Iterable[str | Path]) -> Capture:
    """Concatenate several capture files in the given order.

    Binary images carry no timing information; consecutive images are
    spaced one second apart.
    """
    frames: list[CapturedFrame] = []
    headers: list[dict[str, Any]] = []
    offset = 0.0
    for path in paths:
        capture = load_capture(path)
        headers.append(capture.header)
        for frame in capture.frames:
            frames.append(
                CapturedFrame(
                    t_s=offset + frame.t_s,
                    channels=frame.channels,
                    adc=frame.adc,
                    captured_at=frame.captured_at,
                    consistency=frame.consistency,
                )
            )
        if frames:
            offset = frames[-1].t_s + 1.0
    if not frames:
        raise DecodeError("no frames found in the given capture files")
    header = headers[0] if len(headers) == 1 else {"format": "multiple", "files": headers}
    return Capture(header=header, frames=tuple(frames))


def parse_capture(text: str, name: str = "<capture>") -> Capture:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise DecodeError(f"{name}: empty capture")
    header = _json(lines[0], name, 1)
    if header.get("format") != CAPTURE_FORMAT:
        raise DecodeError(f"{name}: not a {CAPTURE_FORMAT} file")
    if header.get("version") != CAPTURE_VERSION:
        raise DecodeError(f"{name}: unsupported capture version {header.get('version')!r}")
    frames = tuple(_frame(_json(line, name, number), name, number) for number, line in enumerate(lines[1:], start=2))
    if not frames:
        raise DecodeError(f"{name}: capture has a header but no frames")
    return Capture(header=header, frames=frames)


def _frame(record: dict[str, Any], name: str, line: int) -> CapturedFrame:
    try:
        channels = bytes.fromhex(record["channels"])
        adc_hex = record.get("adc")
        adc = bytes.fromhex(adc_hex) if adc_hex is not None else None
        captured = record.get("captured_at")
        return CapturedFrame(
            t_s=float(record.get("t_s", 0.0)),
            channels=channels,
            adc=adc,
            captured_at=datetime.fromisoformat(captured) if captured else None,
            consistency=Consistency(record.get("consistency", Consistency.UNVERIFIED.value)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DecodeError(f"{name}:{line}: invalid frame record: {exc}") from exc


def _json(line: str, name: str, number: int) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise DecodeError(f"{name}:{number}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DecodeError(f"{name}:{number}: expected a JSON object")
    return value
