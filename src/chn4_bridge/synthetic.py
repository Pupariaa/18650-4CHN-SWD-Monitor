"""Synthetic frames for the demo mode and for tests.

Everything produced here is fabricated from the identified memory layout.
Snapshots built from :class:`DemoSource` are always tagged
``source = "simulation"`` and must never be mistaken for measurements.
"""

from __future__ import annotations

import math
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .decoder import AdcConversion
from .layout import ADC_FULL_SCALE_COUNTS, CHANNEL_COUNT, CHANNEL_STRIDE, Offset
from .models import Consistency, RawFrame, SourceKind


@dataclass(frozen=True, slots=True)
class ChannelImage:
    """Field values used to build one 56-byte channel structure."""

    present: bool = True
    state_code: int = 1
    stop_threshold_tenths_v: int = 28
    loop_count: int = 1
    elapsed_s: int = 0
    capacity_mah: float = 0.0
    energy_mwh: float = 0.0
    voltage_candidate_a_v: float = 0.0
    voltage_candidate_b_v: float = 0.0
    display_voltage_v: float = 0.0
    resistance_mohm: float = 0.0
    presence_word: int | None = None


def pack_channel(image: ChannelImage) -> bytes:
    block = bytearray(CHANNEL_STRIDE)
    block[Offset.STATE] = image.state_code
    block[Offset.STOP_THRESHOLD] = image.stop_threshold_tenths_v
    block[Offset.LOOP_COUNT] = image.loop_count
    struct.pack_into("<I", block, Offset.ELAPSED_S, image.elapsed_s)
    struct.pack_into("<f", block, Offset.CAPACITY_MAH, image.capacity_mah)
    struct.pack_into("<f", block, Offset.ENERGY_MWH, image.energy_mwh)
    struct.pack_into("<f", block, Offset.VOLTAGE_CANDIDATE_A, image.voltage_candidate_a_v)
    struct.pack_into("<f", block, Offset.VOLTAGE_CANDIDATE_B, image.voltage_candidate_b_v)
    struct.pack_into("<f", block, Offset.DISPLAY_VOLTAGE_V, image.display_voltage_v)
    struct.pack_into("<f", block, Offset.RESISTANCE_MOHM, image.resistance_mohm)
    presence = image.presence_word if image.presence_word is not None else int(image.present)
    struct.pack_into("<I", block, Offset.PRESENCE, presence)
    return bytes(block)


def pack_channels(images: Sequence[ChannelImage]) -> bytes:
    if len(images) != CHANNEL_COUNT:
        raise ValueError(f"expected {CHANNEL_COUNT} channel images, got {len(images)}")
    return b"".join(pack_channel(image) for image in images)


def pack_adc(samples: Sequence[int]) -> bytes:
    if len(samples) != CHANNEL_COUNT:
        raise ValueError(f"expected {CHANNEL_COUNT} ADC samples, got {len(samples)}")
    return b"".join(struct.pack("<H", sample) for sample in samples)


def volts_to_counts(volts: float, conversion: AdcConversion | None = None) -> int:
    scale = (conversion or AdcConversion()).volts_per_count
    return max(0, min(ADC_FULL_SCALE_COUNTS, round(volts / scale)))


class DemoSource:
    """Generates plausible but entirely synthetic four-channel frames.

    CH1 charges at about 0.8 A, CH2 holds a finished charge, CH3
    discharges at about 1.0 A and CH4 is empty. Like the real firmware,
    only the channel shown on the display (CH1) refreshes its cached
    display voltage. ``time_scale`` speeds up the simulated device clock.
    """

    kind = SourceKind.SIMULATION

    def __init__(self, time_scale: float = 1.0) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be positive")
        self.time_scale = time_scale
        self.description = "synthetic demo data"
        self._started: float | None = None

    def open(self) -> None:
        self._started = time.monotonic()

    def close(self) -> None:
        self._started = None

    def read_frame(self) -> RawFrame:
        if self._started is None:
            raise RuntimeError("demo source is not open")
        now = time.monotonic()
        t = (now - self._started) * self.time_scale
        charge_v = 3.62 + min(0.56, t * 0.0012) + 0.003 * math.sin(t * 0.9)
        charge_mah = 12.0 + t * 0.8 / 3.6
        discharge_v = 4.12 - min(1.2, t * 0.0009) + 0.002 * math.sin(t * 1.3)
        discharge_mah = 85.0 + t * 1.0 / 3.6
        idle_v = 4.176 + 0.001 * math.sin(t * 0.2)
        images = (
            ChannelImage(
                state_code=1,
                elapsed_s=48 + int(t),
                capacity_mah=charge_mah,
                energy_mwh=charge_mah * charge_v,
                voltage_candidate_a_v=3.40,
                voltage_candidate_b_v=3.36,
                display_voltage_v=charge_v,
                resistance_mohm=48.0,
            ),
            ChannelImage(
                state_code=0,
                elapsed_s=8123,
                capacity_mah=2405.7,
                energy_mwh=9213.4,
                voltage_candidate_a_v=3.41,
                voltage_candidate_b_v=3.37,
                display_voltage_v=4.181,
                resistance_mohm=43.0,
            ),
            ChannelImage(
                state_code=2,
                stop_threshold_tenths_v=30,
                elapsed_s=306 + int(t),
                capacity_mah=discharge_mah,
                energy_mwh=discharge_mah * discharge_v,
                voltage_candidate_a_v=4.06,
                voltage_candidate_b_v=4.00,
                display_voltage_v=4.083,
                resistance_mohm=58.0,
            ),
            ChannelImage(present=False, state_code=0),
        )
        adc = pack_adc(
            (
                volts_to_counts(charge_v),
                volts_to_counts(idle_v),
                volts_to_counts(discharge_v),
                0,
            )
        )
        return RawFrame(
            channels=pack_channels(images),
            adc=adc,
            captured_at=datetime.now(UTC),
            monotonic_s=now,
            consistency=Consistency.UNVERIFIED,
            read_duration_ms=None,
        )
