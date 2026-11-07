from __future__ import annotations

import asyncio
import dataclasses

import pytest

from chn4_bridge.acquisition import (
    Acquisition,
    Broadcaster,
    ErrorEvent,
    Event,
    LinkState,
    Monitor,
    StatusEvent,
)
from chn4_bridge.capture import load_capture
from chn4_bridge.config import AcquisitionConfig, Config
from chn4_bridge.models import Snapshot, SourceKind
from chn4_bridge.sources import ReplaySource
from chn4_bridge.synthetic import ChannelImage, DemoSource
from chn4_bridge.transport import ProbeNotFoundError, TransportError, UnsafeEnvironmentError

from helpers import EMPTY, REAL_CAPTURE, ScriptedSource, make_frame


def _config(**acquisition: object) -> Config:
    base = AcquisitionConfig(interval_s=0.2, reconnect_delay_s=0.1, read_timeout_s=1.0)
    return Config(acquisition=dataclasses.replace(base, **acquisition))


def _frame(elapsed: int) -> object:
    return make_frame(
        (ChannelImage(elapsed_s=elapsed, capacity_mah=elapsed / 4), EMPTY, EMPTY, EMPTY), adc=(3500, 0, 0, 0)
    )


async def _collect(acquisition: Acquisition, until: int, kind: type = Snapshot) -> list[Event]:
    subscription = acquisition.broadcaster.subscribe(maxsize=256)
    task = asyncio.create_task(acquisition.run())
    task.add_done_callback(lambda _: subscription.close())
    events: list[Event] = []
    try:
        async with asyncio.timeout(5):
            async for event in subscription:
                events.append(event)
                if sum(isinstance(item, kind) for item in events) >= until:
                    break
    finally:
        acquisition.stop()
        await task
    return events


async def test_broadcaster_fans_out_to_every_subscriber() -> None:
    broadcaster = Broadcaster()
    first = broadcaster.subscribe()
    second = broadcaster.subscribe()
    event = ErrorEvent("x", "y")
    broadcaster.publish(event)
    assert await first.get() is event
    assert await second.get() is event
    assert broadcaster.subscriber_count == 2
    first.close()
    assert broadcaster.subscriber_count == 1
    assert await first.get() is None


async def test_slow_subscriber_drops_oldest_events_without_blocking_others() -> None:
    broadcaster = Broadcaster()
    slow = broadcaster.subscribe(maxsize=2)
    fast = broadcaster.subscribe(maxsize=100)
    events = [ErrorEvent("n", str(index)) for index in range(10)]
    for event in events:
        broadcaster.publish(event)
    assert slow.dropped == 8
    assert [await slow.get(), await slow.get()] == events[-2:]
    assert [await fast.get() for _ in range(10)] == events


async def test_one_acquisition_feeds_several_consumers() -> None:
    source = ScriptedSource([_frame(n) for n in range(1, 50)])
    acquisition = Acquisition(source, _config())
    other = acquisition.broadcaster.subscribe(maxsize=256)
    events = await _collect(acquisition, until=3)
    snapshots = [event for event in events if isinstance(event, Snapshot)]
    assert [snapshot.sequence for snapshot in snapshots] == [1, 2, 3]
    received = []
    while True:
        event = await other.get()
        if isinstance(event, Snapshot):
            received.append(event.sequence)
        if isinstance(event, StatusEvent) and event.state is LinkState.STOPPED:
            break
    assert received[:3] == [1, 2, 3]
    assert source.opens == 1
    assert source.closes == 1


async def test_lost_connection_is_retried_and_history_reset() -> None:
    source = ScriptedSource([_frame(10), TransportError("probe disconnected"), _frame(11), _frame(12)])
    acquisition = Acquisition(source, _config())
    events = await _collect(acquisition, until=3)
    states = [event.state for event in events if isinstance(event, StatusEvent)]
    assert states[:2] == [LinkState.CONNECTING, LinkState.CONNECTED]
    assert LinkState.DISCONNECTED in states
    assert states.count(LinkState.CONNECTED) == 2
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors and errors[0].code == "transport_error" and errors[0].fatal is False
    snapshots = [event for event in events if isinstance(event, Snapshot)]
    assert snapshots[1].channel(1).current_recent_a is None
    assert source.opens == 2


async def test_reconnection_gives_up_after_the_configured_attempts() -> None:
    source = ScriptedSource([_frame(1)], open_errors=[ProbeNotFoundError("none")] * 10)
    acquisition = Acquisition(source, _config(reconnect_attempts=2))
    events = await _collect(acquisition, until=1, kind=type(None))
    assert acquisition.status.state is LinkState.FAILED
    assert isinstance(acquisition.failure, ProbeNotFoundError)
    assert source.opens == 3
    fatal = [event for event in events if isinstance(event, ErrorEvent) and event.fatal]
    assert len(fatal) == 1 and fatal[0].code == "probe_not_found"


async def test_non_retryable_errors_stop_immediately() -> None:
    source = ScriptedSource([_frame(1)], open_errors=[UnsafeEnvironmentError("pyocd_user.py present")])
    acquisition = Acquisition(source, _config(reconnect_attempts=None))
    await acquisition.run()
    assert acquisition.status.state is LinkState.FAILED
    assert source.opens == 1


async def test_replay_end_finishes_the_acquisition() -> None:
    source = ReplaySource(load_capture(REAL_CAPTURE), speed=0)
    acquisition = Acquisition(source, _config())
    subscription = acquisition.broadcaster.subscribe(maxsize=64)
    await acquisition.run()
    subscription.close()
    events = [event async for event in subscription]
    snapshots = [event for event in events if isinstance(event, Snapshot)]
    assert len(snapshots) == 2
    assert all(snapshot.source is SourceKind.REPLAY for snapshot in snapshots)
    assert acquisition.status.state is LinkState.FINISHED
    assert acquisition.latest is snapshots[-1]


async def test_read_timeout_is_reported_as_transport_error() -> None:
    class Stuck(ScriptedSource):
        def read_frame(self):
            import time

            time.sleep(0.6)
            return _frame(1)

    acquisition = Acquisition(Stuck([]), _config(read_timeout_s=0.5, reconnect_attempts=0))
    await asyncio.wait_for(acquisition.run(), 5)
    assert isinstance(acquisition.failure, TransportError)
    assert "no answer" in str(acquisition.failure)


async def test_stop_is_prompt() -> None:
    acquisition = Acquisition(DemoSource(), _config(interval_s=30))
    task = asyncio.create_task(acquisition.run())
    while acquisition.latest is None:
        await asyncio.sleep(0.01)
    acquisition.stop()
    await asyncio.wait_for(task, 2)
    assert acquisition.status.state is LinkState.STOPPED
    assert acquisition.latest.simulated


async def test_staleness() -> None:
    acquisition = Acquisition(ScriptedSource([_frame(1)]), _config(stale_after_s=1.0))
    assert acquisition.is_stale()
    await _collect(acquisition, until=1)
    assert acquisition.latest is not None
    assert not acquisition.is_stale(acquisition.latest.monotonic_s + 0.5)
    assert acquisition.is_stale(acquisition.latest.monotonic_s + 2)


def test_sync_monitor_reads_and_streams() -> None:
    with Monitor(ReplaySource(load_capture(REAL_CAPTURE), speed=0)) as monitor:
        snapshots = list(monitor.stream())
    assert [snapshot.sequence for snapshot in snapshots] == [1, 2]
    assert snapshots[1].channel(1).current_recent_a == pytest.approx(0.876, abs=0.005)
    with Monitor(DemoSource()) as monitor:
        snapshot = monitor.read_once()
    assert snapshot.simulated
    assert snapshot.channel(4).present is False
