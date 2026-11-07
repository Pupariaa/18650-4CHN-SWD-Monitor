"""Periodic acquisition, supervision and fan-out to consumers.

One :class:`Acquisition` owns the frame source. Blocking source calls run
on a dedicated worker thread, so the asyncio loop is never blocked by SWD
traffic. Consumers subscribe to a :class:`Broadcaster`; each subscriber has
its own bounded queue that drops the oldest events when full, so a slow
consumer can never stall the acquisition or the other consumers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from .config import Config
from .decoder import DecodeError
from .derived import SnapshotProcessor
from .models import RawFrame, Snapshot, SourceKind
from .sources import EndOfReplay, FrameSource, open_source
from .transport import (
    BridgeError,
    ProbeBusyError,
    ProbeNotFoundError,
    ProbeSelectionError,
    PyocdUnavailableError,
    TargetNotRunningError,
    TransportError,
    UnsafeEnvironmentError,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

OPEN_TIMEOUT_S = 20.0
CLOSE_TIMEOUT_S = 5.0
NON_RETRYABLE = (ProbeSelectionError, PyocdUnavailableError, UnsafeEnvironmentError)


class LinkState(StrEnum):
    """Connection state of the frame source."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    FINISHED = "finished"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class StatusEvent:
    state: LinkState
    source: SourceKind
    description: str
    message: str | None = None
    attempt: int = 0
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: str
    message: str
    fatal: bool = False
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


Event = Snapshot | StatusEvent | ErrorEvent


def error_code(exc: BaseException) -> str:
    """Stable machine-readable code for an exception."""
    for cls, code in (
        (ProbeSelectionError, "probe_selection"),
        (ProbeNotFoundError, "probe_not_found"),
        (ProbeBusyError, "probe_busy"),
        (TargetNotRunningError, "target_not_running"),
        (UnsafeEnvironmentError, "unsafe_environment"),
        (PyocdUnavailableError, "pyocd_unavailable"),
        (TransportError, "transport_error"),
        (DecodeError, "decode_error"),
    ):
        if isinstance(exc, cls):
            return code
    return "internal_error"


class Subscription:
    """A bounded, drop-oldest event queue attached to a :class:`Broadcaster`."""

    def __init__(self, broadcaster: Broadcaster, maxsize: int) -> None:
        self._broadcaster = broadcaster
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def offer(self, event: Event | None) -> None:
        if self.closed and event is not None:
            return
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                self._queue.get_nowait()
                self.dropped += 1

    async def get(self) -> Event | None:
        """Next event, or ``None`` once the subscription is closed."""
        if self.closed and self._queue.empty():
            return None
        return await self._queue.get()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._broadcaster.unsubscribe(self)
        self.offer(None)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        event = await self.get()
        if event is None:
            raise StopAsyncIteration
        return event


class Broadcaster:
    """Fans events out to any number of in-process consumers."""

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, maxsize: int = 16) -> Subscription:
        subscription = Subscription(self, maxsize)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)

    def publish(self, event: Event) -> None:
        for subscription in tuple(self._subscribers):
            subscription.offer(event)

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            subscription.close()


def processor_from_config(config: Config) -> SnapshotProcessor:
    return SnapshotProcessor(
        adc=config.adc.conversion(),
        estimator=config.estimation,
        resistance_offsets_mohm=config.resistance.offsets_mohm,
    )


class Acquisition:
    """Single acquisition loop feeding a :class:`Broadcaster`.

    Run it with ``await acquisition.run()``; call :meth:`stop` (or cancel the
    task) to end it. Lost connections are retried with
    ``acquisition.reconnect_delay_s`` between attempts, up to
    ``acquisition.reconnect_attempts`` consecutive failures.
    """

    def __init__(
        self,
        source: FrameSource,
        config: Config | None = None,
        broadcaster: Broadcaster | None = None,
        processor: SnapshotProcessor | None = None,
        on_frame: Callable[[RawFrame], None] | None = None,
    ) -> None:
        self.source = source
        self.config = config or Config()
        self.broadcaster = broadcaster or Broadcaster()
        self.processor = processor or processor_from_config(self.config)
        self.on_frame = on_frame
        self.latest: Snapshot | None = None
        self.failure: BaseException | None = None
        self.status = StatusEvent(LinkState.STOPPED, source.kind, source.description)
        self._stop = asyncio.Event()
        self._executor: ThreadPoolExecutor | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def is_stale(self, now_monotonic: float | None = None) -> bool:
        """True when no snapshot was produced within ``acquisition.stale_after_s``."""
        if self.latest is None:
            return True
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return self.latest.age_s(now) > self.config.acquisition.stale_after_s

    async def run(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chn4-source")
        settings = self.config.acquisition
        call = self._call
        failures = 0
        opened = False
        try:
            while not self._stop.is_set():
                if not opened:
                    self._set_status(LinkState.CONNECTING, attempt=failures)
                    try:
                        await call(self.source.open, OPEN_TIMEOUT_S)
                    except BridgeError as exc:
                        failures += 1
                        if not await self._handle_failure(exc, failures):
                            return
                        continue
                    opened = True
                    failures = 0
                    self.processor.reset()
                    self.status = StatusEvent(LinkState.CONNECTED, self.source.kind, self.source.description)
                    self.broadcaster.publish(self.status)
                    log.info("Source connected: %s", self.source.description)

                cycle_started = time.monotonic()
                try:
                    frame = await call(self.source.read_frame, settings.read_timeout_s)
                except EndOfReplay:
                    self._set_status(LinkState.FINISHED, message="end of capture")
                    return
                except BridgeError as exc:
                    opened = False
                    await self._close_source()
                    self.processor.reset()
                    failures += 1
                    if not await self._handle_failure(exc, failures):
                        return
                    continue

                if self.on_frame is not None:
                    self.on_frame(frame)
                try:
                    snapshot = self.processor.process(frame, self.source.kind)
                except DecodeError as exc:
                    self._publish_error(exc, fatal=False)
                else:
                    self.latest = snapshot
                    self.broadcaster.publish(snapshot)

                delay = self._next_delay(settings.interval_s - (time.monotonic() - cycle_started))
                await self._sleep(delay)
        finally:
            if opened:
                await self._close_source()
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            if self.status.state not in (LinkState.FAILED, LinkState.FINISHED):
                self._set_status(LinkState.STOPPED)

    async def _call(self, function: Callable[[], T], limit_s: float) -> T:
        if self._executor is None:
            raise RuntimeError("acquisition is not running")
        future = asyncio.get_running_loop().run_in_executor(self._executor, function)
        try:
            return await asyncio.wait_for(future, limit_s)
        except TimeoutError:
            raise TransportError(f"no answer from the source within {limit_s:.1f} s") from None

    async def _handle_failure(self, exc: BridgeError, failures: int) -> bool:
        limit = self.config.acquisition.reconnect_attempts
        retryable = not isinstance(exc, NON_RETRYABLE)
        if not retryable or (limit is not None and failures > limit):
            self.failure = exc
            self._publish_error(exc, fatal=True)
            self._set_status(LinkState.FAILED, message=str(exc), attempt=failures)
            log.error("Acquisition stopped: %s", exc)
            return False
        self._publish_error(exc, fatal=False)
        self._set_status(LinkState.DISCONNECTED, message=str(exc), attempt=failures)
        log.warning("Source unavailable (attempt %d): %s", failures, exc)
        await self._sleep(self.config.acquisition.reconnect_delay_s)
        return not self._stop.is_set()

    async def _close_source(self) -> None:
        try:
            await self._call(self.source.close, CLOSE_TIMEOUT_S)
        except BridgeError as exc:
            log.warning("Closing the source failed: %s", exc)

    def _next_delay(self, default: float) -> float:
        hint = getattr(self.source, "next_delay_s", None)
        if callable(hint):
            value = hint()
            if isinstance(value, int | float):
                return max(0.0, float(value))
        return max(0.0, default)

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _set_status(self, state: LinkState, *, message: str | None = None, attempt: int = 0) -> None:
        self.status = StatusEvent(state, self.source.kind, self.source.description, message, attempt)
        self.broadcaster.publish(self.status)

    def _publish_error(self, exc: BaseException, *, fatal: bool) -> None:
        self.broadcaster.publish(ErrorEvent(error_code(exc), str(exc), fatal))


class Monitor:
    """Synchronous access to the charger for scripts and notebooks.

    >>> with Monitor() as monitor:
    ...     snapshot = monitor.read_once()
    ...     print(snapshot.channel(1).voltage_v)

    ``Monitor`` reads frames on demand in the calling thread. For several
    concurrent consumers, use :class:`Acquisition` with a
    :class:`Broadcaster` instead.
    """

    def __init__(self, source: FrameSource | None = None, config: Config | None = None) -> None:
        self.config = config or Config()
        self.source = source if source is not None else open_source(self.config)
        self.processor = processor_from_config(self.config)
        self._opened = False

    def open(self) -> None:
        if not self._opened:
            self.source.open()
            self._opened = True
            self.processor.reset()

    def close(self) -> None:
        if self._opened:
            self._opened = False
            self.source.close()

    def __enter__(self) -> Monitor:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def read_once(self) -> Snapshot:
        """Capture and decode one snapshot, opening the source if needed."""
        self.open()
        return self.processor.process(self.source.read_frame(), self.source.kind)

    def stream(self, count: int | None = None, interval_s: float | None = None) -> Iterator[Snapshot]:
        """Yield snapshots periodically until ``count`` is reached or a replay ends."""
        period = self.config.acquisition.interval_s if interval_s is None else interval_s
        produced = 0
        while count is None or produced < count:
            started = time.monotonic()
            try:
                snapshot = self.read_once()
            except EndOfReplay:
                return
            produced += 1
            yield snapshot
            if count is not None and produced >= count:
                return
            hint = getattr(self.source, "next_delay_s", None)
            delay = hint() if callable(hint) else None
            if delay is None:
                delay = period - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
