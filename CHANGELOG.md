# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The WebSocket message format has its own `schema_version`, described in [docs/WEBSOCKET_PROTOCOL.md](./docs/WEBSOCKET_PROTOCOL.md).

## [0.1.0] - Unreleased

First public version.

### Added

- Read-only SWD transport on top of pyOCD: attach without halting, no auto-unlock, memory reads restricted to an allowlist (channel block, ADC block, CPUID), refusal to attach when a pyOCD user script is present, probe detection and selection, explicit errors for missing, busy or ambiguous probes and for a halted core.
- Decoder for the four 56-byte channel structures at `0x20000048` and the four ADC samples at `0x2000041A`, with `null` for unavailable or implausible values and machine-readable issue codes.
- Field quality labels (`direct`, `estimated`, `experimental`, `unknown`) and units published with the data.
- Current estimation from the capacity counter (recent window and step average), activity detection, handling of counter resets, removals, reconnections and clock discontinuities.
- Configurable ADC scale and per-channel resistance offsets, with the `calibrate-resistance` command.
- Torn-read detection by double reading of the channel block.
- Acquisition loop with bounded, configurable reconnection and a broadcaster feeding several consumers from one SWD session.
- `chn4-bridge` CLI: `probes`, `diagnose`, `read`, `monitor`, `serve`, `record`, `decode`, `calibrate-resistance`.
- Raw capture format (JSON Lines) for recording and replaying the exact bytes read from the device; decoding of binary SRAM images.
- Demo mode with synthetic data marked `simulation` everywhere it appears.
- Read-only WebSocket server, protocol schema version 1: `hello`, `snapshot`, `probe_status`, `error`, `shutdown`; per-client bounded queues, send timeout, heartbeat, client limit, loopback-only by default.
- CSV and JSON Lines export.
- Reverse engineering notes, protocol specification, Python, Node.js and browser examples.
- Test suite running without hardware, CI on Linux and Windows with Python 3.11 to 3.13.
