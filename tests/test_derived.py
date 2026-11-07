from __future__ import annotations

import pytest

from chn4_bridge.capture import Capture
from chn4_bridge.derived import EstimatorSettings, SnapshotProcessor, compensate_resistance
from chn4_bridge.models import Activity, RawFrame, Snapshot, SourceKind
from chn4_bridge.synthetic import ChannelImage

from helpers import EMPTY, make_frame


def _process(processor: SnapshotProcessor, image: ChannelImage, at: float) -> Snapshot:
    return processor.process(
        make_frame((image, EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0), monotonic_s=at), SourceKind.REPLAY
    )


def _charging(elapsed: int, capacity: float) -> ChannelImage:
    return ChannelImage(state_code=1, elapsed_s=elapsed, capacity_mah=capacity)


def test_current_between_the_two_real_captures(real_capture: Capture) -> None:
    processor = SnapshotProcessor()
    snapshots = [
        processor.process(
            RawFrame(channels=frame.channels, adc=frame.adc, captured_at=frame.captured_at, monotonic_s=frame.t_s),
            SourceKind.REPLAY,
        )
        for frame in real_capture.frames
    ]
    first, second = snapshots
    assert first.channel(1).current_recent_a is None
    assert first.channel(1).activity is Activity.UNKNOWN
    ch1 = second.channel(1)
    expected = 3.6 * (ch1.capacity_mah - first.channel(1).capacity_mah) / (48 - 39)
    assert ch1.activity is Activity.ACTIVE
    assert ch1.current_recent_a == pytest.approx(expected, abs=1e-3)
    assert ch1.current_recent_a == pytest.approx(0.876, abs=0.005)
    assert ch1.current_average_a == pytest.approx(3.6 * ch1.capacity_mah / 48, abs=1e-3)


def test_recent_current_uses_a_sliding_window() -> None:
    processor = SnapshotProcessor(estimator=EstimatorSettings(window_s=5, idle_after_s=10))
    for second in range(0, 10):
        snapshot = _process(processor, _charging(100 + second, 50 + second * 0.25), float(second))
    assert snapshot.channel(1).current_recent_a == pytest.approx(0.9, abs=1e-6)
    for second in range(10, 20):
        snapshot = _process(processor, _charging(100 + second, 52.5 + (second - 10) * 0.5), float(second))
    assert snapshot.channel(1).current_recent_a == pytest.approx(1.8, abs=1e-6)


def test_counter_reset_clears_history() -> None:
    processor = SnapshotProcessor()
    _process(processor, _charging(500, 300.0), 0.0)
    _process(processor, _charging(501, 300.3), 1.0)
    after_reset = _process(processor, _charging(0, 0.0), 2.0).channel(1)
    assert "counter_reset" in after_reset.issues
    assert after_reset.current_recent_a is None
    assert after_reset.current_average_a is None
    resumed = _process(processor, _charging(1, 0.25), 3.0).channel(1)
    assert resumed.current_recent_a == pytest.approx(0.9)


def test_counters_that_stop_progressing_become_idle_with_zero_estimate() -> None:
    processor = SnapshotProcessor(estimator=EstimatorSettings(idle_after_s=4))
    _process(processor, _charging(10, 5.0), 0.0)
    _process(processor, _charging(11, 5.25), 1.0)
    idle = _process(processor, _charging(11, 5.25), 6.0).channel(1)
    assert idle.activity is Activity.IDLE
    assert idle.current_recent_a == 0.0
    assert idle.current_average_a == pytest.approx(3.6 * 5.25 / 11, abs=1e-4)


def test_battery_removal_and_insertion_restart_estimation() -> None:
    processor = SnapshotProcessor()
    _process(processor, _charging(10, 5.0), 0.0)
    _process(processor, _charging(11, 5.25), 1.0)
    removed = _process(processor, EMPTY, 2.0).channel(1)
    assert removed.activity is Activity.ABSENT
    assert removed.current_recent_a is None
    inserted = _process(processor, _charging(0, 0.0), 3.0).channel(1)
    assert inserted.activity is Activity.UNKNOWN
    assert inserted.current_recent_a is None
    assert "counter_reset" not in inserted.issues


def test_missing_capacity_gives_unknown_activity() -> None:
    processor = SnapshotProcessor()
    reading = _process(processor, ChannelImage(capacity_mah=float("nan"), elapsed_s=10), 0.0).channel(1)
    assert reading.activity is Activity.UNKNOWN
    assert reading.current_average_a is None


def test_host_clock_going_backwards_is_not_turned_into_a_current() -> None:
    processor = SnapshotProcessor()
    _process(processor, _charging(10, 5.0), 100.0)
    reading = _process(processor, _charging(11, 5.25), 50.0).channel(1)
    assert "host_clock_discontinuity" in reading.issues
    assert reading.current_recent_a is None


def test_processor_reset_forgets_history_but_keeps_sequence() -> None:
    processor = SnapshotProcessor()
    _process(processor, _charging(10, 5.0), 0.0)
    processor.reset()
    snapshot = _process(processor, _charging(11, 5.25), 1.0)
    assert snapshot.sequence == 2
    assert snapshot.channel(1).current_recent_a is None


def test_resistance_compensation_is_optional_and_per_channel() -> None:
    images = tuple(ChannelImage(resistance_mohm=value) for value in (148.0, 148.0, 58.0, 30.0))
    frame = make_frame(images, adc=(3500, 3500, 3500, 3500))
    plain = SnapshotProcessor().process(frame, SourceKind.REPLAY)
    assert [reading.resistance_compensated_mohm for reading in plain.channels] == [None] * 4
    calibrated = SnapshotProcessor(resistance_offsets_mohm={1: 34.0, 2: 30.5, 3: 40.0, 4: 40.0}).process(
        frame, SourceKind.REPLAY
    )
    assert [reading.resistance_compensated_mohm for reading in calibrated.channels] == [114.0, 117.5, 18.0, None]
    assert "resistance_compensation_negative" in calibrated.channel(4).issues
    assert [reading.resistance_raw_mohm for reading in calibrated.channels] == [148.0, 148.0, 58.0, 30.0]


@pytest.mark.parametrize(
    ("raw", "offset", "expected"),
    [(None, 10.0, None), (50.0, None, None), (50.0, 10.0, 40.0), (50.0, -5.0, 55.0), (5.0, 10.0, None)],
)
def test_compensate_resistance(raw: float | None, offset: float | None, expected: float | None) -> None:
    assert compensate_resistance(raw, offset) == expected
