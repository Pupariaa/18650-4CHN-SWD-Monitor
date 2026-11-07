from __future__ import annotations

import io
from pathlib import Path

import pytest

from chn4_bridge.capture import CAPTURE_FORMAT, CaptureWriter, load_capture, load_captures, parse_capture
from chn4_bridge.decoder import DecodeError
from chn4_bridge.layout import CHANNEL_BLOCK_SIZE
from chn4_bridge.models import Consistency
from chn4_bridge.sources import EndOfReplay, ReplaySource
from chn4_bridge.synthetic import ChannelImage

from helpers import EMPTY, REAL_CAPTURE, make_frame


def test_real_fixture_only_contains_the_two_read_ranges() -> None:
    capture = load_capture(REAL_CAPTURE)
    assert capture.header["format"] == CAPTURE_FORMAT
    assert capture.header["source"] == "hardware"
    assert len(capture.frames) == 2
    for frame in capture.frames:
        assert len(frame.channels) == CHANNEL_BLOCK_SIZE
        assert frame.adc is not None and len(frame.adc) == 8
    assert capture.frames[1].t_s - capture.frames[0].t_s == 9.0


def test_writer_output_round_trips() -> None:
    stream = io.StringIO()
    writer = CaptureWriter(stream, source="simulation", note="unit test")
    frames = [
        make_frame((ChannelImage(elapsed_s=1), EMPTY, EMPTY, EMPTY), adc=(1, 2, 3, 4), monotonic_s=100.0),
        make_frame(
            (ChannelImage(elapsed_s=2), EMPTY, EMPTY, EMPTY),
            adc=None,
            monotonic_s=101.5,
            consistency=Consistency.CONSISTENT,
        ),
    ]
    for frame in frames:
        writer.write(frame)
    capture = parse_capture(stream.getvalue())
    assert capture.header["note"] == "unit test"
    assert [frame.t_s for frame in capture.frames] == [0.0, 1.5]
    assert capture.frames[0].channels == frames[0].channels
    assert capture.frames[0].adc == frames[0].adc
    assert capture.frames[1].adc is None
    assert capture.frames[1].consistency is Consistency.CONSISTENT
    assert capture.frames[0].captured_at == frames[0].captured_at


def test_binary_sram_images_are_loaded(tmp_path: Path) -> None:
    capture = load_capture(REAL_CAPTURE)
    image = bytearray(0x4000)
    image[0x48 : 0x48 + CHANNEL_BLOCK_SIZE] = capture.frames[0].channels
    assert capture.frames[0].adc is not None
    image[0x41A : 0x41A + 8] = capture.frames[0].adc
    first = tmp_path / "a.bin"
    first.write_bytes(bytes(image))
    second = tmp_path / "b.bin"
    second.write_bytes(bytes(image))
    combined = load_captures([first, second])
    assert [frame.t_s for frame in combined.frames] == [0.0, 1.0]
    assert combined.frames[0].channels == capture.frames[0].channels
    assert combined.frames[0].adc == capture.frames[0].adc


@pytest.mark.parametrize(
    "text",
    [
        "",
        '{"format": "something-else", "version": 1}\n{"channels": ""}',
        '{"format": "chn4-bridge-raw-capture", "version": 99}\n',
        '{"format": "chn4-bridge-raw-capture", "version": 1}\n',
        '{"format": "chn4-bridge-raw-capture", "version": 1}\n{"channels": "zz"}',
        '{"format": "chn4-bridge-raw-capture", "version": 1}\nnot json',
    ],
)
def test_invalid_captures_are_rejected(text: str) -> None:
    with pytest.raises(DecodeError):
        parse_capture(text)


def test_replay_source_paces_and_finishes() -> None:
    source = ReplaySource(load_capture(REAL_CAPTURE), speed=3.0)
    source.open()
    first = source.read_frame()
    assert source.next_delay_s() == pytest.approx(3.0)
    second = source.read_frame()
    assert first.recorded_at is not None and second.recorded_at is not None
    assert (second.recorded_at - first.recorded_at).total_seconds() == 9.0
    with pytest.raises(EndOfReplay):
        source.read_frame()


def test_replay_source_loops() -> None:
    source = ReplaySource(load_capture(REAL_CAPTURE), speed=0, loop=True)
    source.open()
    channels = [source.read_frame().channels for _ in range(5)]
    assert channels[0] == channels[2] == channels[4]
    assert source.next_delay_s() == 0.0
