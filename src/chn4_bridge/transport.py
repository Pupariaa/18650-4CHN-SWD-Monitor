"""Read-only SWD access to the charger through pyOCD.

The only target operations issued by this module are memory reads inside
:data:`chn4_bridge.layout.READABLE_RANGES` and core state queries. There is
no code path that writes memory, halts, resets, unlocks, erases or programs
the target.

pyOCD itself performs the standard debug port power-up and identification
sequence when it attaches. That sequence touches debug infrastructure
registers only; it does not touch SRAM, flash or option bytes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .layout import READABLE_RANGES, MemoryRange

log = logging.getLogger(__name__)

RUNNING_STATES = frozenset({"RUNNING", "SLEEPING"})
PYOCD_USER_FILES = ("pyocd_user.py", ".pyocd_user.py")


class BridgeError(Exception):
    """Base class for errors reported to the user. ``exit_code`` is used by the CLI."""

    exit_code = 1


class TransportError(BridgeError):
    """Communication with the probe or the target failed."""

    exit_code = 5


class ProbeNotFoundError(TransportError):
    exit_code = 3


class ProbeSelectionError(TransportError):
    exit_code = 3


class ProbeBusyError(TransportError):
    exit_code = 3


class TargetNotRunningError(TransportError):
    exit_code = 4


class PyocdUnavailableError(BridgeError):
    exit_code = 1


class UnsafeEnvironmentError(BridgeError):
    exit_code = 1


class ReadNotAllowedError(ValueError):
    """Raised when code attempts to read outside the allowlisted ranges."""


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    unique_id: str
    description: str
    vendor: str
    product: str


def pyocd_session_options(frequency_hz: int) -> dict[str, Any]:
    """Session options that keep pyOCD passive.

    ``project_dir`` is pinned to the current directory because pyOCD changes
    the process working directory to it.
    """
    return {
        "target_override": "cortex_m",
        "connect_mode": "attach",
        "auto_unlock": False,
        "dap_protocol": "swd",
        "frequency": frequency_hz,
        "cache.enable_memory": False,
        "resume_on_disconnect": False,
        "no_config": True,
        "project_dir": os.getcwd(),
        "warning.cortex_m_default": False,
    }


def check_allowed(address: int, size: int, ranges: tuple[MemoryRange, ...] = READABLE_RANGES) -> MemoryRange:
    """Return the allowlisted range covering ``[address, address + size)``."""
    for memory_range in ranges:
        if memory_range.contains(address, size):
            return memory_range
    raise ReadNotAllowedError(f"read of {size} bytes at 0x{address:08X} is outside the allowlisted ranges")


def check_no_user_script(directory: Path | None = None) -> None:
    """Refuse to attach when pyOCD would execute a user script.

    pyOCD automatically runs ``pyocd_user.py`` from its project directory.
    Such a script could perform arbitrary target operations, which would
    break the read-only guarantee of this tool.
    """
    base = directory or Path.cwd()
    for name in PYOCD_USER_FILES:
        if (base / name).is_file():
            raise UnsafeEnvironmentError(
                f"{base / name} exists and would be executed by pyOCD; "
                "run chn4-bridge from another directory or remove the file"
            )


def list_probes() -> list[ProbeInfo]:
    """Return the CMSIS-DAP (and other pyOCD supported) probes currently connected."""
    return [_probe_info(probe) for probe in _connected_probes()]


class SwdReader:
    """A pyOCD session restricted to allowlisted memory reads.

    Methods are blocking and must be called from a single thread.
    """

    def __init__(self, unique_id: str | None = None, frequency_hz: int = 100_000) -> None:
        self.unique_id = unique_id
        self.frequency_hz = frequency_hz
        self.probe: ProbeInfo | None = None
        self._session: Any = None
        self._target: Any = None

    @property
    def is_open(self) -> bool:
        return self._target is not None

    def open(self) -> None:
        if self.is_open:
            return
        check_no_user_script()
        session_class = _import_pyocd()[1]
        probe = self._choose_probe()
        info = _probe_info(probe)
        session = session_class(probe, auto_open=False, options=pyocd_session_options(self.frequency_hz))
        try:
            session.open()
        except Exception as exc:
            _close_quietly(session)
            raise translate_error(exc, "opening the debug session") from exc
        self._session = session
        self._target = session.board.target
        self.probe = info
        try:
            self.ensure_running()
        except Exception:
            self.close()
            raise
        log.info("Attached to target through %s (%s) at %d Hz", info.description, info.unique_id, self.frequency_hz)

    def close(self) -> None:
        session = self._session
        self._session = None
        self._target = None
        if session is not None:
            _close_quietly(session)

    def read(self, address: int, size: int) -> bytes:
        """Read ``size`` bytes. Only allowlisted ranges are accepted."""
        check_allowed(address, size)
        target = self._require_target()
        try:
            data = target.read_memory_block8(address, size)
        except Exception as exc:
            raise translate_error(exc, f"reading 0x{address:08X}") from exc
        if len(data) != size:
            raise TransportError(f"short read at 0x{address:08X}: {len(data)} of {size} bytes")
        return bytes(data)

    def target_state(self) -> str:
        target = self._require_target()
        try:
            return str(target.get_state().name)
        except Exception as exc:
            raise translate_error(exc, "querying the core state") from exc

    def ensure_running(self) -> None:
        state = self.target_state()
        if state not in RUNNING_STATES:
            raise TargetNotRunningError(
                f"target core is {state}, expected RUNNING; the bridge never resumes or resets the core. "
                "Power-cycle the charger with the probe disconnected, then retry"
            )

    def _require_target(self) -> Any:
        if self._target is None:
            raise TransportError("SWD session is not open")
        return self._target

    def _choose_probe(self) -> Any:
        probes = _connected_probes()
        if self.unique_id:
            matches = [probe for probe in probes if self.unique_id in probe.unique_id]
            if not matches:
                raise ProbeNotFoundError(f"no connected probe matches unique ID {self.unique_id!r}")
            if len(matches) > 1:
                raise ProbeSelectionError(f"unique ID {self.unique_id!r} matches {len(matches)} probes")
            return matches[0]
        if not probes:
            raise ProbeNotFoundError(
                "no CMSIS-DAP probe found; check the USB connection, the WCH-Link mode (ARM / DAP-WINUSB) "
                "and that no other debugger software holds the probe"
            )
        if len(probes) > 1:
            listing = ", ".join(f"{probe.unique_id} ({probe.description})" for probe in probes)
            raise ProbeSelectionError(f"several probes are connected, select one with --probe: {listing}")
        return probes[0]


def translate_error(exc: BaseException, action: str) -> TransportError:
    """Map pyOCD and USB exceptions to bridge errors with actionable messages."""
    if isinstance(exc, TransportError):
        return exc
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    name = exc.__class__.__name__
    if name == "ProbeDisconnected" or "no such device" in lowered or "disconnected" in lowered:
        return TransportError(f"probe disconnected while {action}: {text}")
    if any(
        marker in lowered
        for marker in ("busy", "access denied", "in use", "libusb_error_access", "could not open", "unable to open")
    ):
        return ProbeBusyError(f"probe is unavailable while {action} (already used by another program?): {text}")
    if name == "TransferTimeoutError" or "timeout" in lowered or "timed out" in lowered:
        return TransportError(f"SWD timeout while {action}: {text}")
    if name == "TransferFaultError":
        return TransportError(f"SWD memory access fault while {action}: {text}")
    return TransportError(f"SWD error while {action}: {name}: {text}")


def _import_pyocd() -> tuple[Any, Any]:
    try:
        from pyocd.core.helpers import ConnectHelper
        from pyocd.core.session import Session
    except ImportError as exc:
        raise PyocdUnavailableError("pyOCD is not installed; install it with: pip install pyocd") from exc
    return ConnectHelper, Session


def _connected_probes() -> list[Any]:
    connect_helper = _import_pyocd()[0]
    try:
        probes: list[Any] = connect_helper.get_all_connected_probes(blocking=False, print_wait_message=False)
    except Exception as exc:
        raise translate_error(exc, "enumerating probes") from exc
    return probes


def _probe_info(probe: Any) -> ProbeInfo:
    return ProbeInfo(
        unique_id=str(probe.unique_id),
        description=str(probe.description),
        vendor=str(getattr(probe, "vendor_name", "")),
        product=str(getattr(probe, "product_name", "")),
    )


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:
        log.debug("Ignoring error while closing the debug session", exc_info=True)
