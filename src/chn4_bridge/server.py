"""Read-only WebSocket bridge.

A single :class:`~chn4_bridge.acquisition.Acquisition` feeds every client.
Each client has a bounded outgoing queue that drops the oldest messages,
and sends are bounded by a timeout, so a slow or dead client is
disconnected instead of slowing anything else down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import protocol
from .acquisition import Acquisition, ErrorEvent, LinkState, StatusEvent, Subscription
from .config import Config, ServerConfig, check_server_exposure
from .export import dumps
from .models import Snapshot
from .sources import FrameSource

log = logging.getLogger(__name__)

MAX_INBOUND_BYTES = 4096
CLOSE_GRACE_S = 2.0


class _Client:
    def __init__(self, connection: ServerConnection, queue_size: int) -> None:
        self.connection = connection
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0
        self.last_sequence = 0
        self.sender: asyncio.Task[None] | None = None

    def offer(self, message: str | None) -> None:
        while True:
            try:
                self.queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                self.queue.get_nowait()
                self.dropped += 1

    @property
    def label(self) -> str:
        address = self.connection.remote_address
        if isinstance(address, tuple) and len(address) >= 2:
            return f"{address[0]}:{address[1]}"
        return str(address)


class BridgeServer:
    """Serves the snapshots of one acquisition to WebSocket clients."""

    def __init__(self, acquisition: Acquisition, config: ServerConfig, *, stale_after_s: float) -> None:
        check_server_exposure(config)
        self.acquisition = acquisition
        self.config = config
        self.stale_after_s = stale_after_s
        self._clients: set[_Client] = set()
        self._server: Server | None = None
        self._subscription: Subscription | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._pending: Snapshot | None = None
        self._stopped = False

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not started")
        for sock in self._server.sockets:
            port: int = sock.getsockname()[1]
            return port
        raise RuntimeError("server has no listening socket")

    async def start(self) -> None:
        if not self.config.is_loopback:
            log.warning(
                "Listening on %s, which is reachable from other machines. The stream is unauthenticated "
                "and unencrypted; anyone on this network can read the measurements.",
                self.config.host,
            )
        origins: list[Any] | None = None
        if self.config.allowed_origins is not None:
            origins = [*self.config.allowed_origins, None]
        self._subscription = self.acquisition.broadcaster.subscribe(maxsize=64)
        self._server = await serve(
            self._handle,
            self.config.host,
            self.config.port,
            origins=origins,
            ping_interval=self.config.heartbeat_interval_s,
            ping_timeout=self.config.heartbeat_timeout_s,
            close_timeout=CLOSE_GRACE_S,
            max_size=MAX_INBOUND_BYTES,
            max_queue=4,
        )
        self._tasks = [
            asyncio.create_task(self._pump(self._subscription), name="chn4-ws-pump"),
            asyncio.create_task(self._ticker(), name="chn4-ws-ticker"),
        ]
        log.info("WebSocket server listening on ws://%s:%d", self.config.host, self.port)

    async def stop(self, reason: str = "server_stopping") -> None:
        """Notify clients with a ``shutdown`` message, then close everything."""
        if self._stopped:
            return
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._subscription is not None:
            self._subscription.close()
        message = dumps(protocol.shutdown(reason))
        senders = []
        for client in tuple(self._clients):
            client.offer(message)
            client.offer(None)
            if client.sender is not None:
                senders.append(client.sender)
        if senders:
            await asyncio.wait(senders, timeout=CLOSE_GRACE_S)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        log.info("WebSocket server stopped (%s)", reason)

    async def __aenter__(self) -> BridgeServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    def broadcast(self, message: dict[str, Any]) -> None:
        text = dumps(message)
        for client in tuple(self._clients):
            client.offer(text)

    async def _handle(self, connection: ServerConnection) -> None:
        if self._stopped or len(self._clients) >= self.config.max_clients:
            await connection.close(1013, "server busy")
            return
        client = _Client(connection, self.config.client_queue_size)
        self._clients.add(client)
        client.sender = asyncio.create_task(self._sender(client))
        log.info("Client connected: %s (%d connected)", client.label, len(self._clients))
        acquisition = self.acquisition
        client.offer(
            dumps(
                protocol.hello(
                    source=acquisition.source.kind,
                    source_description=acquisition.source.description,
                    broadcast_interval_s=self.config.broadcast_interval_s,
                    stale_after_s=self.stale_after_s,
                    include_experimental=self.config.include_experimental,
                )
            )
        )
        client.offer(dumps(protocol.probe_status(acquisition.status)))
        if acquisition.latest is not None:
            client.offer(dumps(self._snapshot_message(acquisition.latest)))
            client.last_sequence = acquisition.latest.sequence
        try:
            async for _ in connection:
                client.offer(dumps(protocol.error("read_only", "this server is read-only and ignores client messages")))
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(client)
            if not self._stopped:
                client.offer(None)
            if client.dropped:
                log.info("Client %s missed %d messages because it was too slow", client.label, client.dropped)
            log.info("Client disconnected: %s (%d connected)", client.label, len(self._clients))

    async def _sender(self, client: _Client) -> None:
        try:
            while True:
                message = await client.queue.get()
                if message is None:
                    return
                try:
                    await asyncio.wait_for(client.connection.send(message), self.config.send_timeout_s)
                except TimeoutError:
                    log.warning("Disconnecting %s: send timed out", client.label)
                    await client.connection.close(1011, "send timeout")
                    return
        except ConnectionClosed:
            return

    async def _pump(self, subscription: Subscription) -> None:
        async for event in subscription:
            if isinstance(event, Snapshot):
                self._pending = event
            elif isinstance(event, StatusEvent):
                self.broadcast(protocol.probe_status(event))
            elif isinstance(event, ErrorEvent):
                self.broadcast(protocol.error_from_event(event))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.config.broadcast_interval_s)
            pending, self._pending = self._pending, None
            if pending is None:
                continue
            recipients = [client for client in self._clients if client.last_sequence < pending.sequence]
            if not recipients:
                continue
            text = dumps(self._snapshot_message(pending))
            for client in recipients:
                client.offer(text)
                client.last_sequence = pending.sequence

    def _snapshot_message(self, snapshot: Snapshot) -> dict[str, Any]:
        return protocol.snapshot(
            snapshot,
            now_monotonic=time.monotonic(),
            stale_after_s=self.stale_after_s,
            include_experimental=self.config.include_experimental,
        )


async def run_bridge(
    source: FrameSource,
    config: Config,
    *,
    stop: asyncio.Event | None = None,
    on_started: Callable[[BridgeServer], None] | None = None,
) -> Acquisition:
    """Run the acquisition and the WebSocket server until ``stop`` is set.

    The server keeps running after a replay finishes so clients can still
    read the last snapshot. It shuts down when the acquisition fails
    permanently. Returns the acquisition so callers can inspect
    :attr:`Acquisition.failure`.
    """
    acquisition = Acquisition(source, config)
    server = BridgeServer(acquisition, config.server, stale_after_s=config.acquisition.stale_after_s)
    await server.start()
    if on_started is not None:
        on_started(server)
    acquisition_task = asyncio.create_task(acquisition.run(), name="chn4-acquisition")
    stop_event = stop or asyncio.Event()
    stop_task = asyncio.create_task(stop_event.wait(), name="chn4-stop")
    reason = "server_stopping"
    try:
        done, _ = await asyncio.wait({acquisition_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            if acquisition.status.state is LinkState.FAILED:
                reason = "acquisition_failed"
            else:
                acquisition_task.result()
                await stop_task
    finally:
        acquisition.stop()
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        try:
            await asyncio.wait_for(asyncio.shield(acquisition_task), timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            acquisition_task.cancel()
        await server.stop(reason)
    return acquisition
