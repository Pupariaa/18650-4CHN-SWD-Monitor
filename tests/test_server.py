from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from chn4_bridge.acquisition import Acquisition
from chn4_bridge.capture import load_capture
from chn4_bridge.config import AcquisitionConfig, Config, ConfigError, ServerConfig
from chn4_bridge.server import BridgeServer, run_bridge
from chn4_bridge.sources import ReplaySource
from chn4_bridge.synthetic import DemoSource

from helpers import REAL_CAPTURE


def _config(**server: Any) -> Config:
    defaults: dict[str, Any] = {
        "port": 0,
        "broadcast_interval_s": 0.1,
        "heartbeat_interval_s": 5,
        "heartbeat_timeout_s": 5,
    }
    defaults.update(server)
    return Config(
        acquisition=AcquisitionConfig(interval_s=0.2, reconnect_delay_s=0.1),
        server=ServerConfig(**defaults),
    )


@pytest.fixture
async def bridge() -> AsyncIterator[BridgeServer]:
    config = _config(max_clients=3, client_queue_size=4)
    acquisition = Acquisition(DemoSource(time_scale=10), config)
    server = BridgeServer(acquisition, config.server, stale_after_s=config.acquisition.stale_after_s)
    await server.start()
    task = asyncio.create_task(acquisition.run())
    try:
        yield server
    finally:
        acquisition.stop()
        await task
        await server.stop()


def _url(server: BridgeServer) -> str:
    return f"ws://127.0.0.1:{server.port}"


async def _next(connection: ClientConnection, kind: str, limit: float = 5.0) -> dict[str, Any]:
    async with asyncio.timeout(limit):
        while True:
            message = json.loads(await connection.recv())
            if message["type"] == kind:
                return message


async def test_clients_receive_hello_status_and_snapshots(bridge: BridgeServer) -> None:
    async with connect(_url(bridge)) as first, connect(_url(bridge)) as second:
        hello = json.loads(await first.recv())
        assert hello["type"] == "hello"
        assert hello["schema_version"] == 1
        assert hello["data"]["read_only"] is True
        assert hello["data"]["source"]["simulated"] is True
        status = json.loads(await first.recv())
        assert status["type"] == "probe_status"
        snapshot_a = await _next(first, "snapshot")
        snapshot_b = await _next(second, "snapshot")
        for snapshot in (snapshot_a, snapshot_b):
            data = snapshot["data"]
            assert data["source"] == "simulation"
            assert data["simulated"] is True
            assert len(data["channels"]) == 4
            assert data["stale"] is False
        later = await _next(first, "snapshot")
        assert later["data"]["sequence"] > snapshot_a["data"]["sequence"]
        assert bridge.client_count == 2


async def test_late_client_gets_latest_snapshot_immediately(bridge: BridgeServer) -> None:
    while bridge.acquisition.latest is None:
        await asyncio.sleep(0.02)
    async with connect(_url(bridge)) as client:
        kinds = [json.loads(await client.recv())["type"] for _ in range(3)]
    assert kinds == ["hello", "probe_status", "snapshot"]


async def test_client_messages_are_refused_without_side_effects(bridge: BridgeServer) -> None:
    async with connect(_url(bridge)) as client:
        await client.send(json.dumps({"type": "start_charge", "channel": 1}))
        error = await _next(client, "error")
        assert error["data"]["code"] == "read_only"
        assert error["data"]["fatal"] is False
        await _next(client, "snapshot")


async def test_stalled_client_does_not_block_others(bridge: BridgeServer) -> None:
    async with connect(_url(bridge), max_queue=1) as stalled, connect(_url(bridge)) as active:
        first = await _next(active, "snapshot")
        for _ in range(8):
            last = await _next(active, "snapshot")
        assert last["data"]["sequence"] > first["data"]["sequence"]
        assert stalled.state.name == "OPEN"


async def test_connection_limit(bridge: BridgeServer) -> None:
    async with connect(_url(bridge)), connect(_url(bridge)), connect(_url(bridge)):
        await asyncio.sleep(0.1)
        async with connect(_url(bridge)) as extra:
            with pytest.raises(ConnectionClosed) as info:
                await asyncio.wait_for(extra.recv(), 5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 1013


async def test_disconnected_clients_are_forgotten(bridge: BridgeServer) -> None:
    async with connect(_url(bridge)) as client:
        await client.recv()
        assert bridge.client_count == 1
    for _ in range(50):
        if bridge.client_count == 0:
            break
        await asyncio.sleep(0.02)
    assert bridge.client_count == 0


async def test_shutdown_message_is_sent_before_closing() -> None:
    config = _config()
    acquisition = Acquisition(DemoSource(), config)
    server = BridgeServer(acquisition, config.server, stale_after_s=5)
    await server.start()
    async with connect(_url(server)) as client:
        await client.recv()
        await server.stop("maintenance")
        shutdown = await _next(client, "shutdown")
        assert shutdown["data"] == {"reason": "maintenance"}
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(client.recv(), 5)


def test_remote_bind_requires_opt_in() -> None:
    with pytest.raises(ConfigError, match="allow_remote"):
        BridgeServer(Acquisition(DemoSource()), ServerConfig(host="0.0.0.0"), stale_after_s=5)


async def test_run_bridge_serves_a_replay_until_stopped() -> None:
    config = _config()
    stop = asyncio.Event()
    started: list[BridgeServer] = []
    source = ReplaySource(load_capture(REAL_CAPTURE), speed=0)
    task = asyncio.create_task(run_bridge(source, config, stop=stop, on_started=started.append))
    while not started:
        await asyncio.sleep(0.01)
    server = started[0]
    while server.acquisition.status.state.value != "finished":
        await asyncio.sleep(0.01)
    async with connect(_url(server)) as client:
        await _next(client, "hello")
        snapshot = await _next(client, "snapshot")
        assert snapshot["data"]["source"] == "replay"
        assert snapshot["data"]["sequence"] == 2
        assert snapshot["data"]["channels"][0]["current_recent_a"] == pytest.approx(0.876, abs=0.005)
        stop.set()
        shutdown = await _next(client, "shutdown")
        assert shutdown["data"]["reason"] == "server_stopping"
    acquisition = await asyncio.wait_for(task, 5)
    assert acquisition.failure is None
