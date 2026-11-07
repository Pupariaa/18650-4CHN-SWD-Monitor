# Contributing

Thanks for your interest. The most valuable contributions right now are:

- captures from other units, hardware revisions or firmware versions,
- identification of the unknown fields of the channel structure,
- an instantaneous current variable, if the firmware keeps one,
- calibration data for the ADC voltage scale (ADC counts next to a multimeter reading),
- testing with other CMSIS-DAP probes and operating systems.

## Ground rules

- **The project is read-only and stays that way.** Pull requests that write to the target (SRAM, flash, option bytes), unlock, erase, reset or halt it, or control charging and discharging will not be accepted. The charger firmware must remain the only thing in charge of the cells.
- **Separate observations from hypotheses.** A new field interpretation goes in as `experimental` with the evidence in [docs/REVERSE_ENGINEERING.md](./docs/REVERSE_ENGINEERING.md). It can be promoted once it has been validated against the device in several situations.
- **No firmware or ROM content.** Do not attach flash dumps, system ROM dumps or full SRAM images to issues or pull requests. Raw captures (below) contain only the two blocks the bridge reads and are the preferred way to share data.

## Development setup

```bash
git clone https://github.com/Pupariaa/18650-4CHN-SWD-Monitor.git
cd 18650-4CHN-SWD-Monitor
python -m venv .venv
```

Activate the environment with `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on Linux and macOS, then:

```bash
python -m pip install -e ".[dev]"
```

Checks run by the CI, all without hardware:

```bash
ruff check src tests examples
ruff format --check src tests examples
mypy
pytest
```

The tests use a fake pyOCD target that fails on any call other than a memory read or a state query, real localhost sockets for the WebSocket server, and the reference capture in `tests/fixtures/`. Tests that need a real charger are not part of the suite; validate hardware changes manually and describe what you checked in the pull request.

## Sharing a capture

```bash
chn4-bridge record --raw my-unit.capture.jsonl --duration 120 --note "CH1 discharge to 3.0 V, CH2 empty, firmware shown at boot: ..."
```

Write down what the screen shows during the capture (selected slot, states, values, settings) in the note or in the issue. Check the file before sharing it: it is plain JSON, with each frame holding the 232 bytes read from the channel and ADC blocks in hexadecimal.

When reporting a different hardware revision, include the output of `chn4-bridge diagnose` and photos of the board if possible.

## Code style

- Python 3.11+, type hints everywhere, `mypy --strict` clean, formatted with `ruff format`.
- The decoder (`decoder.py`) stays pure: no I/O, no pyOCD, no clock.
- Values that cannot be read reliably are `None`, with an issue code explaining why, never a placeholder zero.
- Public functions and classes have docstrings; the code itself should read without inline comments.
- Keep modules focused; new abstractions need a concrete use.

## Commits and pull requests

- One topic per pull request, with a short description of what changed and how it was tested.
- Update `CHANGELOG.md` under the unreleased version.
- Protocol changes must follow the compatibility policy in [docs/WEBSOCKET_PROTOCOL.md](./docs/WEBSOCKET_PROTOCOL.md).
