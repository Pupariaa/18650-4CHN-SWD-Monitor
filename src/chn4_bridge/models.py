"""Typed data models shared by every part of the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Quality(StrEnum):
    """How much a published value can be trusted."""

    DIRECT = "direct"
    ESTIMATED = "estimated"
    EXPERIMENTAL = "experimental"
    UNKNOWN = "unknown"


class ChannelState(StrEnum):
    """Interpretation of the raw state byte at offset ``+0x00``."""

    STOP = "stop"
    CHARGE = "charge"
    DISCHARGE = "discharge"
    UNKNOWN = "unknown"


class Activity(StrEnum):
    """Activity inferred from the progression of the firmware counters."""

    ABSENT = "absent"
    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    """Origin of a snapshot."""

    HARDWARE = "hardware"
    REPLAY = "replay"
    SIMULATION = "simulation"


class Consistency(StrEnum):
    """Result of the torn-read check performed on a capture."""

    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    UNVERIFIED = "unverified"


STATE_CODES: dict[int, ChannelState] = {
    0: ChannelState.STOP,
    1: ChannelState.CHARGE,
    2: ChannelState.DISCHARGE,
}

FIELD_QUALITY: dict[str, Quality] = {
    "present": Quality.DIRECT,
    "state_code": Quality.DIRECT,
    "state": Quality.EXPERIMENTAL,
    "activity": Quality.ESTIMATED,
    "voltage_v": Quality.EXPERIMENTAL,
    "adc_raw": Quality.DIRECT,
    "display_voltage_v": Quality.DIRECT,
    "capacity_mah": Quality.DIRECT,
    "energy_mwh": Quality.DIRECT,
    "elapsed_s": Quality.DIRECT,
    "resistance_raw_mohm": Quality.DIRECT,
    "resistance_compensated_mohm": Quality.EXPERIMENTAL,
    "current_recent_a": Quality.ESTIMATED,
    "current_average_a": Quality.ESTIMATED,
    "stop_threshold_v": Quality.EXPERIMENTAL,
    "loop_count": Quality.EXPERIMENTAL,
}
"""Quality of each published channel field.

``direct`` means the value is read as-is from a firmware variable whose
meaning was correlated with the device display. It does not mean the value
is calibrated against an external instrument.
"""

UNITS: dict[str, str] = {
    "voltage_v": "V",
    "display_voltage_v": "V",
    "capacity_mah": "mAh",
    "energy_mwh": "mWh",
    "elapsed_s": "s",
    "resistance_raw_mohm": "mOhm",
    "resistance_compensated_mohm": "mOhm",
    "current_recent_a": "A",
    "current_average_a": "A",
    "stop_threshold_v": "V",
    "adc_raw": "counts",
}


@dataclass(frozen=True, slots=True)
class RawFrame:
    """Bytes captured from the target at one point in time.

    ``channels`` holds the four channel structures (224 bytes). ``adc`` holds
    the four ADC samples (8 bytes) or ``None`` when the source cannot provide
    them. ``monotonic_s`` is a host monotonic timestamp used for timing only.
    """

    channels: bytes
    adc: bytes | None
    captured_at: datetime
    monotonic_s: float
    consistency: Consistency = Consistency.UNVERIFIED
    read_duration_ms: float | None = None
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExperimentalFields:
    """Fields whose meaning is not established. Kept for research only."""

    voltage_candidate_a_v: float | None
    voltage_candidate_b_v: float | None
    byte_0x02: int
    word_0x08: int
    word_0x14: int
    word_0x20: int
    word_0x24: int
    word_0x2c: int
    presence_word: int
    raw_hex: str


@dataclass(frozen=True, slots=True)
class ChannelReading:
    """Decoded measurements of one battery slot.

    Measurements that are unavailable, implausible or meaningless for an
    empty slot are ``None``. ``issues`` lists the reasons a value was
    discarded or flagged.
    """

    channel: int
    present: bool
    state_code: int
    state: ChannelState
    activity: Activity
    voltage_v: float | None
    adc_raw: int | None
    display_voltage_v: float | None
    capacity_mah: float | None
    energy_mwh: float | None
    elapsed_s: int | None
    resistance_raw_mohm: float | None
    resistance_compensated_mohm: float | None
    current_recent_a: float | None
    current_average_a: float | None
    stop_threshold_v: float | None
    loop_count: int | None
    experimental: ExperimentalFields
    issues: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The four channels decoded from one capture."""

    sequence: int
    captured_at: datetime
    monotonic_s: float
    source: SourceKind
    consistency: Consistency
    channels: tuple[ChannelReading, ...]
    read_duration_ms: float | None = None
    recorded_at: datetime | None = None

    @property
    def simulated(self) -> bool:
        return self.source is SourceKind.SIMULATION

    def channel(self, number: int) -> ChannelReading:
        """Return the reading for channel ``number`` (1 to 4)."""
        for reading in self.channels:
            if reading.channel == number:
                return reading
        raise KeyError(f"no channel {number} in snapshot")

    def age_s(self, now_monotonic: float) -> float:
        """Seconds elapsed since the capture, using the host monotonic clock."""
        return max(0.0, now_monotonic - self.monotonic_s)
