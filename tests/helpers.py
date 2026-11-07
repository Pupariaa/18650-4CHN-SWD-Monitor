from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from chn4_bridge.capture import CapturedFrame
from chn4_bridge.layout import ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE, CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE
from chn4_bridge.models import Consistency, RawFrame, SourceKind
from chn4_bridge.synthetic import ChannelImage, pack_adc, pack_channels

FIXTURES = Path(__file__).parent / "fixtures"
REAL_CAPTURE = FIXTURES / "charge_4ch_real.jsonl"

EMPTY = ChannelImage(present=False, state_code=0)


def make_frame(
    images: Sequence[ChannelImage] = (EMPTY, EMPTY, EMPTY, EMPTY),
    adc: Sequence[int] | None = (0, 0, 0, 0),
    *,
    monotonic_s: float = 0.0,
    consistency: Consistency = Consistency.UNVERIFIED,
) -> RawFrame:
    return RawFrame(
        channels=pack_channels(images),
        adc=pack_adc(adc) if adc is not None else None,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        monotonic_s=monotonic_s,
        consistency=consistency,
    )


def to_captured(frame: RawFrame, t_s: float) -> CapturedFrame:
    return CapturedFrame(t_s=t_s, channels=frame.channels, adc=frame.adc, captured_at=frame.captured_at)


class FakeReader:
    """Stands in for SwdReader. ``script`` entries are bytes, exceptions or callables."""

    def __init__(self, channel_reads: Sequence[object] = (), adc: bytes = b"\x00" * ADC_BLOCK_SIZE) -> None:
        self.channel_reads = list(channel_reads)
        self.adc = adc
        self.calls: list[tuple[str, int, int]] = []
        self.opened = 0
        self.closed = 0
        self.running = True
        self.open_error: Exception | None = None
        self.probe = None

    def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def read(self, address: int, size: int) -> bytes:
        self.calls.append(("read", address, size))
        if address == CHANNEL_BLOCK_ADDRESS and size == CHANNEL_BLOCK_SIZE:
            item = self.channel_reads.pop(0) if len(self.channel_reads) > 1 else self.channel_reads[0]
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, bytes)
            return item
        if address == ADC_BLOCK_ADDRESS and size == ADC_BLOCK_SIZE:
            return self.adc
        raise AssertionError(f"unexpected read 0x{address:08X}+{size}")

    def ensure_running(self) -> None:
        self.calls.append(("ensure_running", 0, 0))


class ScriptedSource:
    """Frame source driven by a list of frames and exceptions."""

    kind = SourceKind.HARDWARE

    def __init__(self, script: Sequence[object], open_errors: Sequence[Exception] = ()) -> None:
        self.description = "scripted test source"
        self.script = list(script)
        self.open_errors = list(open_errors)
        self.opens = 0
        self.closes = 0

    def open(self) -> None:
        self.opens += 1
        if self.open_errors:
            raise self.open_errors.pop(0)

    def close(self) -> None:
        self.closes += 1

    def read_frame(self) -> RawFrame:
        if not self.script:
            raise AssertionError("script exhausted")
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, RawFrame)
        return item

    def next_delay_s(self) -> float:
        return 0.0
