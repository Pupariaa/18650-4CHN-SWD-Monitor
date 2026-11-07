"""Minimal WebSocket client for ``chn4-bridge serve``.

Start the bridge first (``chn4-bridge serve`` or ``chn4-bridge serve --demo``), then:

    python examples/ws_client.py
    python examples/ws_client.py --url ws://127.0.0.1:8765 --count 10

Only the ``websockets`` package is required; this script does not import chn4_bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

SUPPORTED_SCHEMA = 1


def channel_line(channel: dict[str, Any]) -> str:
    if not channel["present"]:
        return f"CH{channel['channel']}: empty"
    voltage = channel["voltage_v"]
    current = channel["current_recent_a"]
    capacity = channel["capacity_mah"]
    return (
        f"CH{channel['channel']}: {channel['state']:<9} "
        f"{'n/a' if voltage is None else f'{voltage:.3f} V':>8}  "
        f"{'n/a' if current is None else f'{current:.3f} A~':>9}  "
        f"{'n/a' if capacity is None else f'{capacity:.1f} mAh':>11}"
    )


async def run(url: str, count: int | None) -> None:
    received = 0
    async with connect(url) as websocket:
        async for raw in websocket:
            message = json.loads(raw)
            kind = message["type"]
            data = message["data"]
            if kind == "hello":
                if data["schema_version"] != SUPPORTED_SCHEMA:
                    raise SystemExit(f"unsupported schema_version {data['schema_version']}")
                source = data["source"]
                print(
                    f"Connected to chn4-bridge {data['server_version']}, source: {source['kind']} ({source['description']})"
                )
                if source["simulated"]:
                    print("SIMULATED DATA: values are synthetic, not measurements.")
            elif kind == "probe_status":
                detail = f": {data['message']}" if data["message"] else ""
                print(f"[status] {data['state']}{detail}")
            elif kind == "error":
                print(f"[error] {data['code']}: {data['message']}")
            elif kind == "shutdown":
                print(f"[shutdown] {data['reason']}")
                return
            elif kind == "snapshot":
                stale = " STALE" if data.get("stale") else ""
                print(f"#{data['sequence']} {data['captured_at']} {data['consistency']}{stale}")
                for channel in data["channels"]:
                    print("  " + channel_line(channel))
                received += 1
                if count is not None and received >= count:
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--count", type=int, help="exit after this many snapshots")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url, args.count))
    except KeyboardInterrupt:
        pass
    except (ConnectionClosed, OSError) as exc:
        raise SystemExit(f"connection to {args.url} failed or was lost: {exc}") from None


if __name__ == "__main__":
    main()
