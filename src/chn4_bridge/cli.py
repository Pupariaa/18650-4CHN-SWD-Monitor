"""Command line interface: ``chn4-bridge <command>``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import IO, Any

from ._version import __version__
from .acquisition import Acquisition, ErrorEvent, Event, LinkState, Monitor, StatusEvent, processor_from_config
from .capture import CaptureWriter, load_captures
from .config import (
    DEFAULT_CONFIG_NAME,
    Config,
    ConfigError,
    check_server_exposure,
    load_config,
    write_resistance_offsets,
)
from .decoder import DecodeError, decode_cpuid
from .export import CsvWriter, JsonlWriter, dumps, snapshot_to_dict
from .layout import (
    ADC_BLOCK_ADDRESS,
    ADC_BLOCK_SIZE,
    CHANNEL_BLOCK_ADDRESS,
    CHANNEL_BLOCK_SIZE,
    CHANNEL_COUNT,
    CPUID_ADDRESS,
)
from .models import RawFrame, Snapshot, SourceKind
from .server import BridgeServer, run_bridge
from .sources import FrameSource, open_source
from .terminal import Screen, render_snapshot, render_status
from .transport import BridgeError, SwdReader, list_probes

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_PROBE = 3
EXIT_INTERRUPTED = 130

EXPECTED_CPUID = 0x410CC601

log = logging.getLogger("chn4_bridge")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.log_level.upper() != "DEBUG":
        logging.getLogger("pyocd").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
        config = _apply_overrides(config, args)
        result: int = args.handler(args, config)
        return result
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (DecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chn4-bridge",
        description=(
            "Read-only SWD bridge for the 18650-4CHN four-channel Li-ion charger/tester. "
            "Hardware commands need a CMSIS-DAP probe; decode, --replay and --demo work offline."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help=f"configuration file (default: ./{DEFAULT_CONFIG_NAME} if present)")
    parser.add_argument("--log-level", default="warning", choices=["debug", "info", "warning", "error"])
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    probes = commands.add_parser("probes", help="list connected debug probes [hardware]")
    probes.add_argument("--json", action="store_true", help="print JSON")
    probes.set_defaults(handler=cmd_probes)

    diagnose = commands.add_parser("diagnose", help="check probe, target state, CPUID and read timing [hardware]")
    _probe_options(diagnose)
    diagnose.set_defaults(handler=cmd_diagnose)

    read = commands.add_parser("read", help="print one snapshot of the four channels")
    _source_options(read)
    read.add_argument("--json", action="store_true", help="print JSON")
    read.add_argument("--experimental", action="store_true", help="include unidentified fields in JSON output")
    read.set_defaults(handler=cmd_read)

    monitor = commands.add_parser("monitor", help="live table of the four channels")
    _source_options(monitor)
    monitor.add_argument("--no-clear", action="store_true", help="append output instead of redrawing")
    monitor.set_defaults(handler=cmd_monitor)

    serve = commands.add_parser("serve", help="start the read-only WebSocket bridge")
    _source_options(serve)
    serve.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="TCP port (default 8765)")
    serve.add_argument("--allow-remote", action="store_true", help="allow a non-loopback bind address")
    serve.add_argument("--broadcast-interval", type=float, help="seconds between snapshot broadcasts")
    serve.add_argument("--experimental", action="store_true", help="include unidentified fields in snapshots")
    serve.set_defaults(handler=cmd_serve)

    record = commands.add_parser("record", help="record snapshots to CSV / JSONL and raw frames to a capture")
    _source_options(record)
    record.add_argument("--csv", type=Path, help="write decoded rows to this CSV file")
    record.add_argument("--jsonl", type=Path, help="write decoded snapshots to this JSON Lines file")
    record.add_argument("--raw", type=Path, help="write undecoded frames to this capture file (replayable)")
    record.add_argument("--note", help="free text stored in the raw capture header")
    record.add_argument("--duration", type=float, help="stop after this many seconds")
    record.add_argument("--count", type=int, help="stop after this many snapshots")
    record.set_defaults(handler=cmd_record)

    decode = commands.add_parser("decode", help="decode SRAM dumps or raw captures [offline]")
    decode.add_argument("files", nargs="+", type=Path, help="binary SRAM image(s) or raw capture file(s)")
    decode.add_argument("--json", action="store_true", help="print one JSON snapshot per line")
    decode.add_argument("--experimental", action="store_true", help="include unidentified fields")
    decode.set_defaults(handler=cmd_decode)

    calibrate = commands.add_parser(
        "calibrate-resistance",
        help="compute per-channel resistance offsets from the values shown on the device",
        description=(
            "Average the raw resistance of the selected channels and compute offset = raw - displayed. "
            "Only the configuration file can be written; nothing is ever written to the device."
        ),
    )
    _source_options(calibrate)
    for channel in range(1, CHANNEL_COUNT + 1):
        calibrate.add_argument(f"--ch{channel}", type=float, metavar="MOHM", help=f"resistance shown for CH{channel}")
    calibrate.add_argument("--samples", type=int, default=5, help="snapshots to average (default 5)")
    calibrate.add_argument("--write", action="store_true", help="store the offsets in the configuration file")
    calibrate.set_defaults(handler=cmd_calibrate)

    return parser


def _probe_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--probe", metavar="UID", help="unique ID (or part of it) of the probe to use")
    parser.add_argument("--frequency", type=int, metavar="HZ", help="SWD clock in Hz (default 100000)")


def _source_options(parser: argparse.ArgumentParser) -> None:
    _probe_options(parser)
    group = parser.add_argument_group("offline sources")
    exclusive = group.add_mutually_exclusive_group()
    exclusive.add_argument("--replay", nargs="+", type=Path, metavar="FILE", help="replay raw captures or SRAM dumps")
    exclusive.add_argument("--demo", action="store_true", help="synthetic data, clearly marked as simulated")
    group.add_argument("--speed", type=float, default=1.0, help="replay / demo time factor (0 = no delay on replay)")
    group.add_argument("--loop", action="store_true", help="restart the replay after the last frame")
    parser.add_argument("--interval", type=float, metavar="S", help="seconds between captures (default 1.0)")


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    probe = config.probe
    if getattr(args, "probe", None):
        probe = dataclasses.replace(probe, unique_id=args.probe)
    if getattr(args, "frequency", None) is not None:
        if not 10_000 <= args.frequency <= 4_000_000:
            raise ConfigError("--frequency must be between 10000 and 4000000 Hz")
        probe = dataclasses.replace(probe, frequency_hz=args.frequency)
    acquisition = config.acquisition
    if getattr(args, "interval", None) is not None:
        if not 0.2 <= args.interval <= 3600:
            raise ConfigError("--interval must be between 0.2 and 3600 seconds")
        acquisition = dataclasses.replace(
            acquisition, interval_s=args.interval, stale_after_s=max(acquisition.stale_after_s, args.interval * 3)
        )
    server = config.server
    if getattr(args, "host", None):
        server = dataclasses.replace(server, host=args.host)
    if getattr(args, "port", None) is not None:
        server = dataclasses.replace(server, port=args.port)
    if getattr(args, "allow_remote", False):
        server = dataclasses.replace(server, allow_remote=True)
    if getattr(args, "broadcast_interval", None) is not None:
        server = dataclasses.replace(server, broadcast_interval_s=args.broadcast_interval)
    if args.command == "serve" and getattr(args, "experimental", False):
        server = dataclasses.replace(server, include_experimental=True)
    if args.command == "serve":
        check_server_exposure(server)
    if getattr(args, "speed", 1.0) < 0:
        raise ConfigError("--speed must be zero or positive")
    return dataclasses.replace(config, probe=probe, acquisition=acquisition, server=server)


def _make_source(args: argparse.Namespace, config: Config) -> FrameSource:
    return open_source(config, replay=args.replay, demo=args.demo, speed=args.speed, loop=args.loop)


def cmd_probes(args: argparse.Namespace, config: Config) -> int:
    probes = list_probes()
    if args.json:
        print(json.dumps([dataclasses.asdict(probe) for probe in probes], indent=2))
    elif probes:
        print(f"{'UNIQUE ID':<36}{'DESCRIPTION':<40}VENDOR / PRODUCT")
        for probe in probes:
            print(f"{probe.unique_id:<36}{probe.description:<40}{probe.vendor} / {probe.product}")
    else:
        print(
            "No debug probe found. Check the USB cable, the WCH-Link mode (ARM, DAP-WINUSB) and close other debug tools."
        )
    return EXIT_OK if probes else EXIT_NO_PROBE


def cmd_diagnose(args: argparse.Namespace, config: Config) -> int:
    def report(ok: bool, text: str) -> None:
        print(f"[{'ok' if ok else 'FAIL'}] {text}")

    try:
        import pyocd

        report(True, f"pyOCD {pyocd.__version__}")
    except ImportError:
        report(False, "pyOCD is not installed")
        return EXIT_ERROR
    probes = list_probes()
    report(bool(probes), f"{len(probes)} probe(s) detected")
    for probe in probes:
        print(f"       {probe.unique_id}  {probe.description}")
    reader = SwdReader(unique_id=config.probe.unique_id, frequency_hz=config.probe.frequency_hz)
    try:
        reader.open()
        report(True, f"attached at {config.probe.frequency_hz} Hz, core state {reader.target_state()}")
        cpuid = decode_cpuid(int.from_bytes(reader.read(CPUID_ADDRESS, 4), "little"))
        matches = cpuid.value == EXPECTED_CPUID
        report(
            matches,
            f"CPUID 0x{cpuid.value:08X}: {cpuid.implementer_name} {cpuid.core_name} {cpuid.revision_name}"
            + ("" if matches else f" (reference unit reports 0x{EXPECTED_CPUID:08X}; layout may differ)"),
        )
        timings = []
        blocks = []
        for _ in range(3):
            started = time.perf_counter()
            blocks.append(reader.read(CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE))
            reader.read(ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE)
            timings.append((time.perf_counter() - started) * 1000)
        report(True, f"channel + ADC read: {min(timings):.1f} to {max(timings):.1f} ms")
        reader.ensure_running()
        report(True, f"core still {reader.target_state()} after reads")
        processor = processor_from_config(config)
        frame = RawFrame(
            channels=blocks[-1],
            adc=reader.read(ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE),
            captured_at=_now(),
            monotonic_s=time.monotonic(),
        )
        snapshot = processor.process(frame, SourceKind.HARDWARE)
        present = [f"CH{reading.channel}" for reading in snapshot.channels if reading.present]
        report(True, f"batteries reported present: {', '.join(present) if present else 'none'}")
    except BridgeError as exc:
        report(False, str(exc))
        return exc.exit_code
    finally:
        reader.close()
    return EXIT_OK


def cmd_read(args: argparse.Namespace, config: Config) -> int:
    with Monitor(_make_source(args, config), config) as monitor:
        snapshot = monitor.read_once()
        if args.json:
            print(json.dumps(snapshot_to_dict(snapshot, include_experimental=args.experimental), indent=2))
        else:
            print(render_snapshot(snapshot, source=monitor.source.description))
    return EXIT_OK


def cmd_decode(args: argparse.Namespace, config: Config) -> int:
    capture = load_captures(args.files)
    processor = processor_from_config(config)
    for index, frame in enumerate(capture.frames):
        raw = RawFrame(
            channels=frame.channels,
            adc=frame.adc,
            captured_at=frame.captured_at or _now(),
            monotonic_s=frame.t_s,
            consistency=frame.consistency,
            recorded_at=frame.captured_at,
        )
        snapshot = processor.process(raw, SourceKind.REPLAY)
        if args.json:
            print(dumps(snapshot_to_dict(snapshot, include_experimental=args.experimental)))
        else:
            if index:
                print()
            print(
                render_snapshot(
                    snapshot, source=_frame_label(args.files, index), legend=index == len(capture.frames) - 1
                )
            )
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace, config: Config) -> int:
    source = _make_source(args, config)
    screen = Screen(clear=not args.no_clear)
    latest: list[Snapshot] = []
    status: list[StatusEvent] = []

    def on_event(event: Event) -> bool:
        if isinstance(event, Snapshot):
            latest[:] = [event]
        elif isinstance(event, StatusEvent):
            status[:] = [event]
            if event.state is not LinkState.CONNECTED and not latest:
                screen.show(render_status(event))
                return True
        elif isinstance(event, ErrorEvent):
            log.warning("%s: %s", event.code, event.message)
            return True
        if latest:
            screen.show(render_snapshot(latest[0], source=source.description, status=status[0] if status else None))
        return True

    acquisition = _run_async(lambda stop: _consume(source, config, stop, on_event))
    return _exit_code(acquisition)


def cmd_record(args: argparse.Namespace, config: Config) -> int:
    if not (args.csv or args.jsonl or args.raw):
        raise ConfigError("record needs at least one of --csv, --jsonl or --raw")
    if args.count is not None and args.count < 1:
        raise ConfigError("--count must be at least 1")
    source = _make_source(args, config)
    with contextlib.ExitStack() as stack:
        csv_writer = CsvWriter(_open(stack, args.csv)) if args.csv else None
        jsonl_writer = JsonlWriter(_open(stack, args.jsonl)) if args.jsonl else None
        raw_writer = (
            CaptureWriter(_open(stack, args.raw), source=source.kind.value, note=args.note) if args.raw else None
        )
        written = [0]

        def on_event(event: Event) -> bool:
            if isinstance(event, Snapshot):
                if csv_writer:
                    csv_writer.write(event)
                if jsonl_writer:
                    jsonl_writer.write(event)
                written[0] += 1
                print(f"\rrecorded {written[0]} snapshot(s)", end="", file=sys.stderr, flush=True)
                return args.count is None or written[0] < args.count
            if isinstance(event, StatusEvent) and event.state is not LinkState.CONNECTED:
                print(f"\n{render_status(event)}", file=sys.stderr)
            elif isinstance(event, ErrorEvent):
                print(f"\n{event.code}: {event.message}", file=sys.stderr)
            return True

        acquisition = _run_async(
            lambda stop: _consume(
                source,
                config,
                stop,
                on_event,
                on_frame=raw_writer.write if raw_writer else None,
                duration=args.duration,
            )
        )
        print(file=sys.stderr)
    return _exit_code(acquisition)


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    source = _make_source(args, config)

    def started(server: BridgeServer) -> None:
        print(f"chn4-bridge {__version__} serving {source.kind.value} data ({source.description})", file=sys.stderr)
        print(f"WebSocket: ws://{server.config.host}:{server.port}  (read-only, Ctrl+C to stop)", file=sys.stderr)
        if source.kind is SourceKind.SIMULATION:
            print("Data is SIMULATED; every snapshot is tagged source=simulation.", file=sys.stderr)

    acquisition = _run_async(lambda stop: run_bridge(source, config, stop=stop, on_started=started))
    return _exit_code(acquisition)


def cmd_calibrate(args: argparse.Namespace, config: Config) -> int:
    displayed = {
        channel: getattr(args, f"ch{channel}")
        for channel in range(1, CHANNEL_COUNT + 1)
        if getattr(args, f"ch{channel}") is not None
    }
    if not displayed:
        raise ConfigError("give the displayed resistance of at least one channel, for example --ch1 114")
    if args.samples < 1:
        raise ConfigError("--samples must be at least 1")
    samples: dict[int, list[float]] = {channel: [] for channel in displayed}
    with Monitor(_make_source(args, config), config) as monitor:
        for snapshot in monitor.stream(count=args.samples):
            for channel in displayed:
                raw = snapshot.channel(channel).resistance_raw_mohm
                if raw is not None:
                    samples[channel].append(raw)
    offsets: dict[int, float | None] = {}
    print(f"{'CH':<5}{'RAW MEAN':<12}{'SAMPLES':<9}{'DISPLAYED':<12}{'OFFSET':<10}PREVIOUS")
    for channel, shown in displayed.items():
        values = samples[channel]
        previous = config.resistance.offsets_mohm.get(channel)
        if not values:
            print(f"CH{channel:<3}{'---':<12}{0:<9}{shown:<12g}{'---':<10}{_fmt(previous)}  no battery or no data")
            continue
        raw_mean = mean(values)
        offset = round(raw_mean - shown, 3)
        offsets[channel] = offset
        print(f"CH{channel:<3}{raw_mean:<12.2f}{len(values):<9}{shown:<12g}{offset:<10g}{_fmt(previous)}")
    if not offsets:
        print("No offset computed.", file=sys.stderr)
        return EXIT_ERROR
    if args.write:
        target = config.path or Path(DEFAULT_CONFIG_NAME)
        write_resistance_offsets(target, offsets)
        print(f"Offsets written to {target}")
    else:
        print("Dry run. Add --write to store these offsets in the configuration file.")
    return EXIT_OK


async def _consume(
    source: FrameSource,
    config: Config,
    stop: asyncio.Event,
    on_event: Callable[[Event], bool],
    *,
    on_frame: Callable[[RawFrame], None] | None = None,
    duration: float | None = None,
) -> Acquisition:
    acquisition = Acquisition(source, config, on_frame=on_frame)
    subscription = acquisition.broadcaster.subscribe(maxsize=64)
    task = asyncio.create_task(acquisition.run())
    task.add_done_callback(lambda _: subscription.close())
    stopper = asyncio.create_task(stop.wait())
    stopper.add_done_callback(lambda _: acquisition.stop())
    timer = asyncio.get_running_loop().call_later(duration, acquisition.stop) if duration else None
    try:
        async for event in subscription:
            if not on_event(event):
                acquisition.stop()
    finally:
        acquisition.stop()
        if timer is not None:
            timer.cancel()
        stopper.cancel()
        await task
    return acquisition


def _run_async(main: Callable[[asyncio.Event], Awaitable[Acquisition]]) -> Acquisition:
    async def runner() -> Acquisition:
        stop = asyncio.Event()
        with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError, ValueError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        return await main(stop)

    return asyncio.run(runner())


def _exit_code(acquisition: Acquisition) -> int:
    failure = acquisition.failure
    if failure is None:
        return EXIT_OK
    if isinstance(failure, BridgeError):
        return failure.exit_code
    return EXIT_ERROR


def _open(stack: contextlib.ExitStack, path: Path) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return stack.enter_context(path.open("w", encoding="utf-8", newline=""))


def _frame_label(files: Sequence[Path], index: int) -> str:
    if len(files) == 1:
        return f"{files[0].name} frame {index + 1}"
    return f"frame {index + 1} of {', '.join(path.name for path in files)}"


def _fmt(value: Any) -> str:
    return "---" if value is None else f"{value:g}"


def _now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":
    sys.exit(main())
