from __future__ import annotations

import math
import struct

import pytest

from chn4_bridge.capture import Capture
from chn4_bridge.decoder import (
    AdcConversion,
    DecodeError,
    decode_cpuid,
    decode_frame,
    split_sram_image,
    stable_fields_equal,
)
from chn4_bridge.layout import (
    ADC_BLOCK_ADDRESS,
    CHANNEL_BLOCK_SIZE,
    CHANNEL_STRIDE,
    SRAM_BASE,
    Offset,
    adc_address,
    channel_address,
)
from chn4_bridge.models import Activity, ChannelState, RawFrame, SourceKind
from chn4_bridge.synthetic import ChannelImage, pack_channel

from helpers import EMPTY, make_frame


def _decode(frame: RawFrame):
    return decode_frame(frame, source=SourceKind.REPLAY, sequence=1)


def _real(capture: Capture, index: int) -> RawFrame:
    frame = capture.frames[index]
    return RawFrame(channels=frame.channels, adc=frame.adc, captured_at=frame.captured_at, monotonic_s=frame.t_s)


def test_channel_addresses_match_the_documented_map() -> None:
    assert [hex(channel_address(n)) for n in range(1, 5)] == [
        "0x20000048",
        "0x20000080",
        "0x200000b8",
        "0x200000f0",
    ]
    assert [hex(adc_address(n)) for n in range(1, 5)] == ["0x2000041a", "0x2000041c", "0x2000041e", "0x20000420"]
    assert CHANNEL_BLOCK_SIZE == 0xE0
    with pytest.raises(ValueError, match="channel"):
        channel_address(5)


def test_real_capture_decodes_all_four_channels(real_capture: Capture) -> None:
    snapshot = _decode(_real(real_capture, 1))
    assert [reading.channel for reading in snapshot.channels] == [1, 2, 3, 4]
    assert all(reading.present for reading in snapshot.channels)
    assert all(reading.state is ChannelState.CHARGE for reading in snapshot.channels)
    assert [reading.elapsed_s for reading in snapshot.channels] == [48, 47, 47, 46]
    assert [reading.resistance_raw_mohm for reading in snapshot.channels] == [48.0, 43.0, 58.0, 56.0]
    assert [reading.adc_raw for reading in snapshot.channels] == [3224, 3228, 3800, 3803]
    ch1 = snapshot.channel(1)
    assert ch1.capacity_mah == pytest.approx(11.777, abs=1e-3)
    assert ch1.energy_mwh == pytest.approx(40.683, abs=1e-3)
    assert ch1.stop_threshold_v == pytest.approx(2.8)
    assert ch1.loop_count == 1
    assert ch1.issues == ()


def test_adc_voltage_matches_display_voltage_of_selected_channel(real_capture: Capture) -> None:
    snapshot = _decode(_real(real_capture, 1))
    ch1 = snapshot.channel(1)
    assert ch1.voltage_v == pytest.approx(3224 * 4.4 / 4095, abs=1e-4)
    assert ch1.display_voltage_v == pytest.approx(ch1.voltage_v, abs=0.002)


def test_cached_display_voltage_of_unselected_channels_does_not_follow_adc(real_capture: Capture) -> None:
    first = _decode(_real(real_capture, 0))
    second = _decode(_real(real_capture, 1))
    assert first.channel(1).display_voltage_v != second.channel(1).display_voltage_v
    for number in (2, 3, 4):
        assert first.channel(number).display_voltage_v == second.channel(number).display_voltage_v
    assert first.channel(2).adc_raw != second.channel(2).adc_raw


def test_fields_are_little_endian_at_documented_offsets() -> None:
    block = bytearray(CHANNEL_STRIDE)
    block[Offset.STATE] = 2
    block[Offset.STOP_THRESHOLD] = 30
    block[Offset.LOOP_COUNT] = 3
    block[Offset.ELAPSED_S : Offset.ELAPSED_S + 4] = bytes([0x10, 0x0E, 0x00, 0x00])
    struct.pack_into("<f", block, Offset.CAPACITY_MAH, 1234.5)
    struct.pack_into("<f", block, Offset.ENERGY_MWH, 4567.25)
    struct.pack_into("<f", block, Offset.DISPLAY_VOLTAGE_V, 3.75)
    struct.pack_into("<f", block, Offset.RESISTANCE_MOHM, 61.0)
    struct.pack_into("<I", block, Offset.PRESENCE, 1)
    images = bytes(block) + pack_channel(EMPTY) * 3
    adc = bytes([0xD8, 0x0E, 0, 0, 0, 0, 0, 0])
    snapshot = _decode(RawFrame(channels=images, adc=adc, captured_at=make_frame().captured_at, monotonic_s=0))
    ch1 = snapshot.channel(1)
    assert ch1.state is ChannelState.DISCHARGE
    assert ch1.elapsed_s == 3600
    assert ch1.capacity_mah == 1234.5
    assert ch1.energy_mwh == 4567.25
    assert ch1.display_voltage_v == 3.75
    assert ch1.resistance_raw_mohm == 61.0
    assert ch1.stop_threshold_v == 3.0
    assert ch1.loop_count == 3
    assert ch1.adc_raw == 0x0ED8


def test_absent_channel_has_no_measurements() -> None:
    images = (
        ChannelImage(present=False, capacity_mah=500.0, display_voltage_v=3.9, resistance_mohm=50.0, elapsed_s=99),
        EMPTY,
        EMPTY,
        EMPTY,
    )
    reading = _decode(make_frame(images, adc=(3700, 0, 0, 0))).channel(1)
    assert reading.present is False
    assert reading.activity is Activity.ABSENT
    for value in (
        reading.voltage_v,
        reading.display_voltage_v,
        reading.capacity_mah,
        reading.energy_mwh,
        reading.elapsed_s,
        reading.resistance_raw_mohm,
    ):
        assert value is None
    assert reading.adc_raw == 3700


@pytest.mark.parametrize(
    ("code", "state", "issue"),
    [
        (0, ChannelState.STOP, None),
        (1, ChannelState.CHARGE, None),
        (2, ChannelState.DISCHARGE, None),
        (7, ChannelState.UNKNOWN, "state_code_unknown"),
    ],
)
def test_state_codes(code: int, state: ChannelState, issue: str | None) -> None:
    reading = _decode(make_frame((ChannelImage(state_code=code), EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0))).channel(1)
    assert reading.state is state
    assert reading.state_code == code
    assert (issue in reading.issues) if issue else reading.issues == ()


@pytest.mark.parametrize("raw", [0, 4095, 1000])
def test_implausible_adc_samples_are_unavailable_not_zero(raw: int) -> None:
    reading = _decode(make_frame((ChannelImage(), EMPTY, EMPTY, EMPTY), adc=(raw, 0, 0, 0))).channel(1)
    assert reading.voltage_v is None
    assert "adc_sample_invalid" in reading.issues
    assert reading.adc_raw == raw


def test_missing_adc_block_is_reported() -> None:
    reading = _decode(make_frame((ChannelImage(), EMPTY, EMPTY, EMPTY), adc=None)).channel(1)
    assert reading.voltage_v is None
    assert reading.adc_raw is None
    assert "adc_unavailable" in reading.issues


def test_adc_conversion_is_configurable() -> None:
    conversion = AdcConversion(volts_per_count=5.0 / 4095, min_valid_v=1.0, max_valid_v=5.0)
    frame = make_frame((ChannelImage(), EMPTY, EMPTY, EMPTY), adc=(4000, 0, 0, 0))
    reading = decode_frame(frame, source=SourceKind.REPLAY, sequence=1, adc=conversion).channel(1)
    assert reading.voltage_v == pytest.approx(4000 * 5.0 / 4095, abs=1e-4)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_become_none(value: float) -> None:
    image = ChannelImage(capacity_mah=value, energy_mwh=value, display_voltage_v=value, resistance_mohm=value)
    reading = _decode(make_frame((image, EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0))).channel(1)
    assert reading.capacity_mah is None
    assert reading.energy_mwh is None
    assert reading.display_voltage_v is None
    assert reading.resistance_raw_mohm is None
    assert {"capacity_mah_not_finite", "energy_mwh_not_finite", "resistance_raw_mohm_not_finite"} <= set(reading.issues)


def test_negative_counters_are_rejected() -> None:
    image = ChannelImage(capacity_mah=-3.0, energy_mwh=-1.0)
    reading = _decode(make_frame((image, EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0))).channel(1)
    assert reading.capacity_mah is None
    assert "capacity_mah_negative" in reading.issues


def test_unexpected_presence_word_is_flagged() -> None:
    image = ChannelImage(presence_word=0x00010001)
    reading = _decode(make_frame((image, EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0))).channel(1)
    assert reading.present is True
    assert "presence_word_unexpected" in reading.issues


def test_experimental_fields_are_kept_separately(real_capture: Capture) -> None:
    reading = _decode(_real(real_capture, 1)).channel(1)
    experimental = reading.experimental
    assert experimental.voltage_candidate_a_v == pytest.approx(3.4030, abs=1e-3)
    assert experimental.word_0x20 == 0x0004CD25
    assert experimental.presence_word == 1
    assert bytes.fromhex(experimental.raw_hex) == real_capture.frames[1].channels[:CHANNEL_STRIDE]


@pytest.mark.parametrize(("channels", "adc"), [(b"\x00" * 10, None), (b"\x00" * CHANNEL_BLOCK_SIZE, b"\x00" * 3)])
def test_wrong_block_lengths_raise(channels: bytes, adc: bytes | None) -> None:
    frame = RawFrame(channels=channels, adc=adc, captured_at=make_frame().captured_at, monotonic_s=0)
    with pytest.raises(DecodeError):
        _decode(frame)


def test_split_sram_image_extracts_both_blocks() -> None:
    image = bytearray(0x4000)
    image[0x48 : 0x48 + CHANNEL_BLOCK_SIZE] = bytes(range(CHANNEL_BLOCK_SIZE))
    image[ADC_BLOCK_ADDRESS - SRAM_BASE : ADC_BLOCK_ADDRESS - SRAM_BASE + 8] = b"ABCDEFGH"
    channels, adc = split_sram_image(bytes(image))
    assert channels == bytes(range(CHANNEL_BLOCK_SIZE))
    assert adc == b"ABCDEFGH"
    assert split_sram_image(bytes(CHANNEL_BLOCK_SIZE)) == (bytes(CHANNEL_BLOCK_SIZE), None)
    with pytest.raises(DecodeError):
        split_sram_image(b"\x00" * 100)


def test_stable_fields_ignore_voltage_jitter_but_catch_counter_changes() -> None:
    base = ChannelImage(elapsed_s=10, capacity_mah=5.0, display_voltage_v=3.70)
    first = make_frame((base, EMPTY, EMPTY, EMPTY)).channels
    voltage_moved = make_frame(
        (ChannelImage(elapsed_s=10, capacity_mah=5.0, display_voltage_v=3.71), EMPTY, EMPTY, EMPTY)
    )
    counter_moved = make_frame(
        (ChannelImage(elapsed_s=11, capacity_mah=5.0, display_voltage_v=3.70), EMPTY, EMPTY, EMPTY)
    )
    assert stable_fields_equal(first, voltage_moved.channels)
    assert not stable_fields_equal(first, counter_moved.channels)


def test_cpuid_of_reference_unit() -> None:
    cpuid = decode_cpuid(0x410CC601)
    assert cpuid.implementer_name == "ARM"
    assert cpuid.core_name == "Cortex-M0+"
    assert cpuid.architecture == 0xC
    assert cpuid.revision_name == "r0p1"
