"""SRAM layout of the 18650-4CHN firmware, as identified by reverse engineering.

Every address in this module was observed on a single unit. See
``docs/REVERSE_ENGINEERING.md`` for the confidence level of each field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

SRAM_BASE = 0x20000000

CHANNEL_COUNT = 4
CHANNEL_BLOCK_ADDRESS = 0x20000048
CHANNEL_STRIDE = 0x38
CHANNEL_BLOCK_SIZE = CHANNEL_STRIDE * CHANNEL_COUNT

ADC_BLOCK_ADDRESS = 0x2000041A
ADC_SAMPLE_SIZE = 2
ADC_BLOCK_SIZE = ADC_SAMPLE_SIZE * CHANNEL_COUNT
ADC_FULL_SCALE_COUNTS = 4095

CPUID_ADDRESS = 0xE000ED00
CPUID_SIZE = 4

SRAM_IMAGE_MIN_SIZE = ADC_BLOCK_ADDRESS + ADC_BLOCK_SIZE - SRAM_BASE


class Offset(IntEnum):
    """Byte offsets inside one 56-byte channel structure."""

    STATE = 0x00
    STOP_THRESHOLD = 0x01
    UNKNOWN_BYTE_02 = 0x02
    LOOP_COUNT = 0x03
    ELAPSED_S = 0x04
    UNKNOWN_WORD_08 = 0x08
    CAPACITY_MAH = 0x0C
    ENERGY_MWH = 0x10
    UNKNOWN_WORD_14 = 0x14
    VOLTAGE_CANDIDATE_A = 0x18
    VOLTAGE_CANDIDATE_B = 0x1C
    UNKNOWN_WORD_20 = 0x20
    UNKNOWN_WORD_24 = 0x24
    DISPLAY_VOLTAGE_V = 0x28
    UNKNOWN_WORD_2C = 0x2C
    RESISTANCE_MOHM = 0x30
    PRESENCE = 0x34


CONSISTENCY_FIELDS: tuple[tuple[int, int], ...] = (
    (Offset.STATE, 0x18),
    (Offset.RESISTANCE_MOHM, 0x08),
)
"""Byte ranges (offset, length) compared between two consecutive reads.

They hold the state, counters, capacity, energy, resistance and presence,
which only change when the firmware updates its accounting. Voltage fields
are excluded because they may legitimately change between two reads.
"""


@dataclass(frozen=True, slots=True)
class MemoryRange:
    """A contiguous region of target memory that the bridge may read."""

    name: str
    address: int
    size: int

    @property
    def end(self) -> int:
        return self.address + self.size

    def contains(self, address: int, size: int) -> bool:
        return size > 0 and self.address <= address and address + size <= self.end


CHANNEL_RANGE = MemoryRange("channels", CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE)
ADC_RANGE = MemoryRange("adc", ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE)
CPUID_RANGE = MemoryRange("cpuid", CPUID_ADDRESS, CPUID_SIZE)

READABLE_RANGES: tuple[MemoryRange, ...] = (CHANNEL_RANGE, ADC_RANGE, CPUID_RANGE)


def channel_address(channel: int) -> int:
    """Return the SRAM address of the structure for ``channel`` (1 to 4)."""
    if not 1 <= channel <= CHANNEL_COUNT:
        raise ValueError(f"channel must be between 1 and {CHANNEL_COUNT}, got {channel}")
    return CHANNEL_BLOCK_ADDRESS + (channel - 1) * CHANNEL_STRIDE


def adc_address(channel: int) -> int:
    """Return the SRAM address of the ADC sample for ``channel`` (1 to 4)."""
    if not 1 <= channel <= CHANNEL_COUNT:
        raise ValueError(f"channel must be between 1 and {CHANNEL_COUNT}, got {channel}")
    return ADC_BLOCK_ADDRESS + (channel - 1) * ADC_SAMPLE_SIZE
