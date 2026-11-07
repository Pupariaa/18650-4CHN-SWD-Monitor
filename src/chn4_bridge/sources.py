"""Frame sources: live hardware, recorded captures and synthetic demo data.

Every source returns :class:`chn4_bridge.models.RawFrame` objects, so the
decoder, the derived measurements and every consumer behave identically
whatever the origin of the data.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .capture import Capture, load_captures
from .config import Config
from .decoder import stable_fields_equal
from .layout import ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE, CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE
from .models import Consistency, RawFrame, SourceKind
from .synthetic import DemoSource
from .transport import SwdReader


class EndOfReplay(Exception):
    """Raised by :class:`ReplaySource` once every frame has been delivered."""


@runtime_checkable
class FrameSource(Protocol):
    """Blocking producer of raw frames. Methods are called from one thread."""

    kind: SourceKind
    description: str

    def open(self) -> None: ...

    def read_frame(self) -> RawFrame: ...

    def close(self) -> None: ...


class HardwareSource:
    """Reads the channel block and ADC block from the charger over SWD.

    With ``consistency_reads=2`` the channel block is read twice and the
    counter fields are compared. A mismatch means the firmware updated the
    structures during the read; the capture is retried up to
    ``consistency_retries`` times and published as ``inconsistent`` if it
    never stabilises.
    """

    kind = SourceKind.HARDWARE

    def __init__(
        self,
        reader: SwdReader,
        *,
        consistency_reads: int = 2,
        consistency_retries: int = 2,
    ) -> None:
        self.reader = reader
        self.consistency_reads = consistency_reads
        self.consistency_retries = consistency_retries
        self.description = "SWD probe"

    def open(self) -> None:
        self.reader.open()
        if self.reader.probe is not None:
            self.description = f"{self.reader.probe.description} ({self.reader.probe.unique_id})"

    def close(self) -> None:
        self.reader.close()

    def read_frame(self) -> RawFrame:
        started = time.monotonic()
        consistency = Consistency.UNVERIFIED
        channels = b""
        adc = b""
        for _ in range(self.consistency_retries + 1):
            channels = self.reader.read(CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE)
            adc = self.reader.read(ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE)
            if self.consistency_reads < 2:
                break
            second = self.reader.read(CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE)
            if stable_fields_equal(channels, second):
                consistency = Consistency.CONSISTENT
                channels = second
                break
            consistency = Consistency.INCONSISTENT
        self.reader.ensure_running()
        finished = time.monotonic()
        return RawFrame(
            channels=channels,
            adc=adc,
            captured_at=datetime.now(UTC),
            monotonic_s=finished,
            consistency=consistency,
            read_duration_ms=round((finished - started) * 1000, 2),
        )


class ReplaySource:
    """Replays a recorded capture through the normal processing pipeline.

    ``speed`` scales the recorded timing (2.0 replays twice as fast, 0 replays
    without delay). With ``loop`` the capture restarts after the last frame.
    """

    kind = SourceKind.REPLAY

    def __init__(self, capture: Capture, *, speed: float = 1.0, loop: bool = False, name: str = "capture") -> None:
        if speed < 0:
            raise ValueError("speed must be zero or positive")
        if not capture.frames:
            raise ValueError("capture contains no frames")
        self.capture = capture
        self.speed = speed
        self.loop = loop
        self.description = f"replay of {name}"
        self._index = 0

    @classmethod
    def from_paths(cls, paths: Sequence[str | Path], *, speed: float = 1.0, loop: bool = False) -> ReplaySource:
        names = ", ".join(Path(path).name for path in paths)
        return cls(load_captures(paths), speed=speed, loop=loop, name=names)

    def open(self) -> None:
        self._index = 0

    def close(self) -> None:
        return None

    def read_frame(self) -> RawFrame:
        frames = self.capture.frames
        if self._index >= len(frames):
            if not self.loop:
                raise EndOfReplay()
            self._index = 0
        frame = frames[self._index]
        self._index += 1
        return RawFrame(
            channels=frame.channels,
            adc=frame.adc,
            captured_at=datetime.now(UTC),
            monotonic_s=time.monotonic(),
            consistency=frame.consistency,
            recorded_at=frame.captured_at,
        )

    def next_delay_s(self) -> float | None:
        """Delay before the next frame, derived from the recorded timing."""
        frames = self.capture.frames
        if self.speed == 0:
            return 0.0
        if self._index == 0 or self._index >= len(frames):
            return None
        gap = frames[self._index].t_s - frames[self._index - 1].t_s
        return max(0.0, gap / self.speed)


def open_source(
    config: Config,
    *,
    replay: Sequence[str | Path] | None = None,
    demo: bool = False,
    speed: float = 1.0,
    loop: bool = False,
) -> FrameSource:
    """Build the frame source selected by the command line or the caller."""
    if replay and demo:
        raise ValueError("replay and demo are mutually exclusive")
    if replay:
        return ReplaySource.from_paths(replay, speed=speed, loop=loop)
    if demo:
        return DemoSource(time_scale=speed if speed > 0 else 1.0)
    reader = SwdReader(unique_id=config.probe.unique_id, frequency_hz=config.probe.frequency_hz)
    return HardwareSource(
        reader,
        consistency_reads=config.acquisition.consistency_reads,
        consistency_retries=config.acquisition.consistency_retries,
    )
