"""Measurements derived from consecutive snapshots.

The firmware does not expose an instantaneous current that has been
identified so far. Currents are estimated from the capacity counter:

    I_estimated_A = 3.6 * delta_capacity_mAh / delta_time_s

All values produced here are estimates and are labelled as such.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from .decoder import AdcConversion, decode_frame
from .layout import CHANNEL_COUNT
from .models import Activity, ChannelReading, RawFrame, Snapshot, SourceKind

MAH_PER_SECOND_TO_AMPERE = 3.6


@dataclass(frozen=True, slots=True)
class EstimatorSettings:
    """Tuning of the current estimator and activity detection.

    ``window_s`` is the span of device time used for the recent current.
    ``idle_after_s`` is the host time without counter progression after
    which a channel is reported as idle. ``reset_tolerance_mah`` absorbs
    float jitter before a capacity decrease is treated as a counter reset.
    """

    window_s: float = 5.0
    idle_after_s: float = 4.0
    reset_tolerance_mah: float = 0.05


@dataclass(frozen=True, slots=True)
class CurrentEstimate:
    activity: Activity
    recent_a: float | None
    average_a: float | None
    issues: tuple[str, ...] = ()


@dataclass(slots=True)
class _Point:
    elapsed_s: int
    capacity_mah: float
    progressed_at: float


class CurrentEstimator:
    """Estimates the current of a single channel from its counters."""

    def __init__(self, settings: EstimatorSettings | None = None) -> None:
        self.settings = settings or EstimatorSettings()
        self._history: deque[_Point] = deque()

    def reset(self) -> None:
        self._history.clear()

    def update(self, reading: ChannelReading, monotonic_s: float) -> CurrentEstimate:
        """Feed one reading and return the current estimate for it."""
        if not reading.present:
            self.reset()
            return CurrentEstimate(Activity.ABSENT, None, None)
        capacity = reading.capacity_mah
        elapsed = reading.elapsed_s
        if capacity is None or elapsed is None:
            self.reset()
            return CurrentEstimate(Activity.UNKNOWN, None, None)

        average = _current(capacity, elapsed) if elapsed > 0 else None
        issues: list[str] = []
        history = self._history
        if history:
            last = history[-1]
            counter_went_back = elapsed < last.elapsed_s
            capacity_went_back = capacity < last.capacity_mah - self.settings.reset_tolerance_mah
            if counter_went_back or capacity_went_back:
                history.clear()
                issues.append("counter_reset")
            elif monotonic_s < last.progressed_at:
                history.clear()
                issues.append("host_clock_discontinuity")

        if not history:
            history.append(_Point(elapsed, capacity, monotonic_s))
            return CurrentEstimate(Activity.UNKNOWN, None, average, tuple(issues))

        last = history[-1]
        if elapsed > last.elapsed_s or capacity > last.capacity_mah + 1e-4:
            history.append(_Point(elapsed, capacity, monotonic_s))
        while len(history) > 2 and history[-1].elapsed_s - history[1].elapsed_s >= self.settings.window_s:
            history.popleft()

        if monotonic_s - history[-1].progressed_at >= self.settings.idle_after_s:
            return CurrentEstimate(Activity.IDLE, 0.0, average, tuple(issues))
        if len(history) < 2:
            return CurrentEstimate(Activity.UNKNOWN, None, average, tuple(issues))

        first, newest = history[0], history[-1]
        delta_time = newest.elapsed_s - first.elapsed_s
        delta_capacity = newest.capacity_mah - first.capacity_mah
        recent = _current(delta_capacity, delta_time) if delta_time > 0 and delta_capacity >= 0 else None
        return CurrentEstimate(Activity.ACTIVE, recent, average, tuple(issues))


def compensate_resistance(raw_mohm: float | None, offset_mohm: float | None) -> float | None:
    """Subtract a per-channel offset from the raw resistance.

    Returns ``None`` when no offset is configured, when the raw value is
    unavailable, or when the result would be negative.
    """
    if raw_mohm is None or offset_mohm is None:
        return None
    value = raw_mohm - offset_mohm
    if value < 0:
        return None
    return round(value, 3)


class SnapshotProcessor:
    """Turns raw frames into fully populated snapshots.

    A processor keeps per-channel history, so the same instance must be fed
    frames in chronological order from a single source.
    """

    def __init__(
        self,
        *,
        adc: AdcConversion | None = None,
        estimator: EstimatorSettings | None = None,
        resistance_offsets_mohm: Mapping[int, float | None] | None = None,
    ) -> None:
        self.adc = adc or AdcConversion()
        self.resistance_offsets_mohm: dict[int, float | None] = dict(resistance_offsets_mohm or {})
        self._estimators = {channel: CurrentEstimator(estimator) for channel in range(1, CHANNEL_COUNT + 1)}
        self._sequence = 0

    def reset(self) -> None:
        """Forget the channel history, for example after a reconnection."""
        for estimator in self._estimators.values():
            estimator.reset()

    def process(self, frame: RawFrame, source: SourceKind) -> Snapshot:
        self._sequence += 1
        snapshot = decode_frame(frame, source=source, sequence=self._sequence, adc=self.adc)
        channels = tuple(self._derive(reading, frame.monotonic_s) for reading in snapshot.channels)
        return dataclasses.replace(snapshot, channels=channels)

    def _derive(self, reading: ChannelReading, monotonic_s: float) -> ChannelReading:
        estimate = self._estimators[reading.channel].update(reading, monotonic_s)
        offset = self.resistance_offsets_mohm.get(reading.channel)
        compensated = compensate_resistance(reading.resistance_raw_mohm, offset)
        issues = reading.issues + estimate.issues
        if offset is not None and reading.resistance_raw_mohm is not None and compensated is None:
            issues += ("resistance_compensation_negative",)
        return dataclasses.replace(
            reading,
            activity=estimate.activity,
            current_recent_a=estimate.recent_a,
            current_average_a=estimate.average_a,
            resistance_compensated_mohm=compensated,
            issues=issues,
        )


def _current(delta_capacity_mah: float, delta_time_s: float) -> float:
    return round(MAH_PER_SECOND_TO_AMPERE * delta_capacity_mah / delta_time_s, 4)
