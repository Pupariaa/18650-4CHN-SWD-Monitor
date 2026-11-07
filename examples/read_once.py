"""Read one snapshot of the four channels and print it.

python examples/read_once.py
python examples/read_once.py --demo
python examples/read_once.py --replay tests/fixtures/charge_4ch_real.jsonl
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chn4_bridge import FIELD_QUALITY, Monitor, load_config, open_source


def describe(value: float | None, unit: str, decimals: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{decimals}f} {unit}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--demo", action="store_true", help="synthetic data")
    group.add_argument("--replay", type=Path, help="raw capture or SRAM dump")
    args = parser.parse_args()

    config = load_config(args.config)
    source = open_source(config, replay=[args.replay] if args.replay else None, demo=args.demo, speed=0)
    with Monitor(source, config) as monitor:
        snapshot = monitor.read_once()

    print(
        f"Snapshot #{snapshot.sequence} from {snapshot.source.value}, captured {snapshot.captured_at:%Y-%m-%d %H:%M:%S} UTC"
    )
    if snapshot.simulated:
        print("SIMULATED DATA: these values are synthetic, not measurements.")
    for channel in snapshot.channels:
        if not channel.present:
            print(f"CH{channel.channel}: empty")
            continue
        print(
            f"CH{channel.channel}: {channel.state.value:<9} "
            f"V={describe(channel.voltage_v, 'V')} ({FIELD_QUALITY['voltage_v'].value}) "
            f"C={describe(channel.capacity_mah, 'mAh', 1)} "
            f"E={describe(channel.energy_mwh, 'mWh', 1)} "
            f"I~={describe(channel.current_average_a, 'A')} ({FIELD_QUALITY['current_average_a'].value}) "
            f"R={describe(channel.resistance_raw_mohm, 'mOhm', 0)}"
        )
        if channel.issues:
            print(f"     issues: {', '.join(channel.issues)}")


if __name__ == "__main__":
    main()
