"""Pure, deterministic decoding of captured SRAM bytes.

Nothing in this module performs I/O or depends on pyOCD.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .layout import (
    ADC_BLOCK_ADDRESS,
    ADC_BLOCK_SIZE,
    ADC_FULL_SCALE_COUNTS,
    ADC_SAMPLE_SIZE,
    CHANNEL_BLOCK_ADDRESS,
    CHANNEL_BLOCK_SIZE,
    CHANNEL_COUNT,
    CHANNEL_STRIDE,
    CONSISTENCY_FIELDS,
    SRAM_BASE,
    SRAM_IMAGE_MIN_SIZE,
    Offset,
)
from .models import (
    STATE_CODES,
    Activity,
    ChannelReading,
    ChannelState,
    ExperimentalFields,
    RawFrame,
    Snapshot,
    SourceKind,
)


class DecodeError(ValueError):
    """Raised when captured bytes do not match the expected layout."""


@dataclass(frozen=True, slots=True)
class AdcConversion:
    """Conversion of raw ADC counts to a battery voltage.

    The default scale of 4.4 V full scale over 4095 counts was derived
    experimentally and is not calibrated against an external instrument.
    Samples outside ``[min_valid_v, max_valid_v]``, zero samples and
    saturated samples are reported as unavailable.
    """

    volts_per_count: float = 4.4 / ADC_FULL_SCALE_COUNTS
    min_valid_v: float = 2.0
    max_valid_v: float = 4.4

    def to_volts(self, raw: int) -> float | None:
        if raw <= 0 or raw >= ADC_FULL_SCALE_COUNTS:
            return None
        volts = raw * self.volts_per_count
        if not self.min_valid_v <= volts <= self.max_valid_v:
            return None
        return round(volts, 4)


@dataclass(frozen=True, slots=True)
class CpuId:
    """Decoded ARM CPUID register (``0xE000ED00``)."""

    value: int
    implementer: int
    variant: int
    architecture: int
    part_number: int
    revision: int

    @property
    def implementer_name(self) -> str:
        return "ARM" if self.implementer == 0x41 else f"0x{self.implementer:02X}"

    @property
    def core_name(self) -> str:
        return _CORE_NAMES.get(self.part_number, f"unknown part 0x{self.part_number:03X}")

    @property
    def revision_name(self) -> str:
        return f"r{self.variant}p{self.revision}"


_CORE_NAMES = {
    0xC20: "Cortex-M0",
    0xC60: "Cortex-M0+",
    0xC21: "Cortex-M1",
    0xC23: "Cortex-M3",
    0xC24: "Cortex-M4",
    0xC27: "Cortex-M7",
    0xD20: "Cortex-M23",
    0xD21: "Cortex-M33",
}


def decode_cpuid(value: int) -> CpuId:
    """Split a CPUID register value into its architectural fields."""
    return CpuId(
        value=value,
        implementer=(value >> 24) & 0xFF,
        variant=(value >> 20) & 0x0F,
        architecture=(value >> 16) & 0x0F,
        part_number=(value >> 4) & 0xFFF,
        revision=value & 0x0F,
    )


def split_sram_image(image: bytes, base_address: int = SRAM_BASE) -> tuple[bytes, bytes | None]:
    """Extract the channel block and ADC block from a raw SRAM image.

    ``image`` may be a full SRAM dump starting at ``base_address`` or a bare
    224-byte channel block, in which case no ADC data is returned.
    """
    if len(image) == CHANNEL_BLOCK_SIZE:
        return bytes(image), None
    if base_address != SRAM_BASE:
        raise DecodeError(f"unsupported SRAM base address 0x{base_address:08X}")
    if len(image) < SRAM_IMAGE_MIN_SIZE:
        raise DecodeError(
            f"image of {len(image)} bytes is neither a {CHANNEL_BLOCK_SIZE}-byte channel block "
            f"nor an SRAM image of at least {SRAM_IMAGE_MIN_SIZE} bytes"
        )
    channel_start = CHANNEL_BLOCK_ADDRESS - base_address
    adc_start = ADC_BLOCK_ADDRESS - base_address
    return (
        bytes(image[channel_start : channel_start + CHANNEL_BLOCK_SIZE]),
        bytes(image[adc_start : adc_start + ADC_BLOCK_SIZE]),
    )


def stable_fields_equal(first: bytes, second: bytes) -> bool:
    """Compare the counter and state fields of two channel block reads."""
    if len(first) != CHANNEL_BLOCK_SIZE or len(second) != CHANNEL_BLOCK_SIZE:
        return False
    for index in range(CHANNEL_COUNT):
        base = index * CHANNEL_STRIDE
        for offset, length in CONSISTENCY_FIELDS:
            start = base + offset
            if first[start : start + length] != second[start : start + length]:
                return False
    return True


def decode_frame(
    frame: RawFrame,
    *,
    source: SourceKind,
    sequence: int,
    adc: AdcConversion | None = None,
) -> Snapshot:
    """Decode a raw capture into a snapshot.

    Derived values (currents, activity, compensated resistance) are left
    empty; :class:`chn4_bridge.derived.SnapshotProcessor` fills them.
    """
    if len(frame.channels) != CHANNEL_BLOCK_SIZE:
        raise DecodeError(f"channel block must be {CHANNEL_BLOCK_SIZE} bytes, got {len(frame.channels)}")
    if frame.adc is not None and len(frame.adc) != ADC_BLOCK_SIZE:
        raise DecodeError(f"ADC block must be {ADC_BLOCK_SIZE} bytes, got {len(frame.adc)}")
    conversion = adc or AdcConversion()
    channels = tuple(
        decode_channel(
            index + 1,
            frame.channels[index * CHANNEL_STRIDE : (index + 1) * CHANNEL_STRIDE],
            _adc_sample(frame.adc, index),
            conversion,
        )
        for index in range(CHANNEL_COUNT)
    )
    return Snapshot(
        sequence=sequence,
        captured_at=frame.captured_at,
        monotonic_s=frame.monotonic_s,
        source=source,
        consistency=frame.consistency,
        channels=channels,
        read_duration_ms=frame.read_duration_ms,
        recorded_at=frame.recorded_at,
    )


def decode_channel(
    channel: int,
    block: bytes,
    adc_raw: int | None,
    adc: AdcConversion,
) -> ChannelReading:
    """Decode one 56-byte channel structure."""
    if len(block) != CHANNEL_STRIDE:
        raise DecodeError(f"channel structure must be {CHANNEL_STRIDE} bytes, got {len(block)}")
    issues: list[str] = []
    presence_word = _u32(block, Offset.PRESENCE)
    if presence_word not in (0, 1):
        issues.append("presence_word_unexpected")
    present = presence_word != 0
    state_code = block[Offset.STATE]
    state = STATE_CODES.get(state_code, ChannelState.UNKNOWN)
    if state is ChannelState.UNKNOWN:
        issues.append("state_code_unknown")

    experimental = ExperimentalFields(
        voltage_candidate_a_v=_f32(block, Offset.VOLTAGE_CANDIDATE_A),
        voltage_candidate_b_v=_f32(block, Offset.VOLTAGE_CANDIDATE_B),
        byte_0x02=block[Offset.UNKNOWN_BYTE_02],
        word_0x08=_u32(block, Offset.UNKNOWN_WORD_08),
        word_0x14=_u32(block, Offset.UNKNOWN_WORD_14),
        word_0x20=_u32(block, Offset.UNKNOWN_WORD_20),
        word_0x24=_u32(block, Offset.UNKNOWN_WORD_24),
        word_0x2c=_u32(block, Offset.UNKNOWN_WORD_2C),
        presence_word=presence_word,
        raw_hex=bytes(block).hex(),
    )

    if not present:
        return ChannelReading(
            channel=channel,
            present=False,
            state_code=state_code,
            state=state,
            activity=Activity.ABSENT,
            voltage_v=None,
            adc_raw=adc_raw,
            display_voltage_v=None,
            capacity_mah=None,
            energy_mwh=None,
            elapsed_s=None,
            resistance_raw_mohm=None,
            resistance_compensated_mohm=None,
            current_recent_a=None,
            current_average_a=None,
            stop_threshold_v=None,
            loop_count=None,
            experimental=experimental,
            issues=tuple(issues),
        )

    voltage_v: float | None = None
    if adc_raw is None:
        issues.append("adc_unavailable")
    else:
        voltage_v = adc.to_volts(adc_raw)
        if voltage_v is None:
            issues.append("adc_sample_invalid")

    return ChannelReading(
        channel=channel,
        present=True,
        state_code=state_code,
        state=state,
        activity=Activity.UNKNOWN,
        voltage_v=voltage_v,
        adc_raw=adc_raw,
        display_voltage_v=_positive(block, Offset.DISPLAY_VOLTAGE_V, "display_voltage_v", issues),
        capacity_mah=_non_negative(block, Offset.CAPACITY_MAH, "capacity_mah", issues),
        energy_mwh=_non_negative(block, Offset.ENERGY_MWH, "energy_mwh", issues),
        elapsed_s=_u32(block, Offset.ELAPSED_S),
        resistance_raw_mohm=_non_negative(block, Offset.RESISTANCE_MOHM, "resistance_raw_mohm", issues),
        resistance_compensated_mohm=None,
        current_recent_a=None,
        current_average_a=None,
        stop_threshold_v=block[Offset.STOP_THRESHOLD] / 10,
        loop_count=block[Offset.LOOP_COUNT],
        experimental=experimental,
        issues=tuple(issues),
    )


def _adc_sample(adc: bytes | None, index: int) -> int | None:
    if adc is None:
        return None
    value: int = struct.unpack_from("<H", adc, index * ADC_SAMPLE_SIZE)[0]
    return value


def _u32(block: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<I", block, offset)[0]
    return value


def _f32(block: bytes, offset: int) -> float | None:
    value: float = struct.unpack_from("<f", block, offset)[0]
    if not math.isfinite(value):
        return None
    return float(f"{value:.7g}")


def _non_negative(block: bytes, offset: int, name: str, issues: list[str]) -> float | None:
    value = _f32(block, offset)
    if value is None:
        issues.append(f"{name}_not_finite")
        return None
    if value < 0:
        issues.append(f"{name}_negative")
        return None
    return value


def _positive(block: bytes, offset: int, name: str, issues: list[str]) -> float | None:
    value = _f32(block, offset)
    if value is None:
        issues.append(f"{name}_not_finite")
        return None
    if value <= 0:
        return None
    return value
