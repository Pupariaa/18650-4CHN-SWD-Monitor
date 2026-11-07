"""Follow the four channels and log the estimated charge or discharge current.

Stops after --count snapshots, at the end of a replay, or with Ctrl+C.

    python examples/stream.py --count 30
    python examples/stream.py --demo --speed 30
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chn4_bridge import Activity, Monitor, load_config, open_source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--count", type=int, help="number of snapshots (default: unlimited)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between snapshots")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="synthetic data")
    group.add_argument("--replay", type=Path, help="raw capture or SRAM dump")
    parser.add_argument("--speed", type=float, default=1.0, help="demo / replay time factor")
    args = parser.parse_args()

    config = load_config(args.config)
    source = open_source(config, replay=[args.replay] if args.replay else None, demo=args.demo, speed=args.speed)
    print("time                     " + "".join(f"CH{n:<17}" for n in range(1, 5)))
    try:
        with Monitor(source, config) as monitor:
            for snapshot in monitor.stream(count=args.count, interval_s=args.interval):
                cells = []
                for channel in snapshot.channels:
                    if channel.activity is Activity.ABSENT:
                        cells.append("empty")
                    elif channel.voltage_v is None:
                        cells.append("V n/a")
                    elif channel.current_recent_a is None:
                        cells.append(f"{channel.voltage_v:.3f} V")
                    else:
                        cells.append(f"{channel.voltage_v:.3f} V {channel.current_recent_a:.3f} A")
                marker = " SIMULATED" if snapshot.simulated else ""
                print(
                    f"{snapshot.captured_at:%Y-%m-%d %H:%M:%S}      "
                    + "".join(f"{cell:<19}" for cell in cells)
                    + marker
                )
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
