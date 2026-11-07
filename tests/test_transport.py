from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chn4_bridge import transport
from chn4_bridge.layout import (
    ADC_BLOCK_ADDRESS,
    ADC_BLOCK_SIZE,
    CHANNEL_BLOCK_ADDRESS,
    CHANNEL_BLOCK_SIZE,
    CPUID_ADDRESS,
)
from chn4_bridge.models import Consistency
from chn4_bridge.sources import HardwareSource
from chn4_bridge.synthetic import ChannelImage
from chn4_bridge.transport import (
    ProbeBusyError,
    ProbeNotFoundError,
    ProbeSelectionError,
    ReadNotAllowedError,
    SwdReader,
    TargetNotRunningError,
    TransportError,
    UnsafeEnvironmentError,
    check_allowed,
    pyocd_session_options,
    translate_error,
)

from helpers import EMPTY, FakeReader, make_frame

ALLOWED_TARGET_METHODS = {"read_memory_block8", "get_state"}


class RecordingTarget:
    """Fake pyOCD target that fails loudly on anything but reads and state queries."""

    def __init__(self, state: str = "RUNNING") -> None:
        self.state = state
        self.calls: list[str] = []
        self.memory = {
            CHANNEL_BLOCK_ADDRESS: b"\x11" * CHANNEL_BLOCK_SIZE,
            ADC_BLOCK_ADDRESS: b"\x22" * ADC_BLOCK_SIZE,
            CPUID_ADDRESS: (0x410CC601).to_bytes(4, "little"),
        }

    def read_memory_block8(self, address: int, size: int) -> list[int]:
        self.calls.append("read_memory_block8")
        return list(self.memory[address][:size])

    def get_state(self) -> Any:
        self.calls.append("get_state")
        return SimpleNamespace(name=self.state)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"transport accessed forbidden target attribute {name!r}")


class FakeProbe:
    def __init__(self, unique_id: str, description: str = "WCH CMSIS-DAP") -> None:
        self.unique_id = unique_id
        self.description = description
        self.vendor_name = "WCH"
        self.product_name = "WCH-Link"


@pytest.fixture
def fake_pyocd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(probes=[FakeProbe("0001A0000000")], target=RecordingTarget(), sessions=[], open_error=None)

    class Helper:
        @staticmethod
        def get_all_connected_probes(blocking: bool, print_wait_message: bool) -> list[FakeProbe]:
            assert blocking is False
            return list(state.probes)

    class Session:
        def __init__(self, probe: FakeProbe, auto_open: bool, options: dict[str, Any]) -> None:
            self.probe = probe
            self.options = options
            self.board = SimpleNamespace(target=state.target)
            self.closed = False
            state.sessions.append(self)

        def open(self) -> None:
            if state.open_error is not None:
                raise state.open_error

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(transport, "_import_pyocd", lambda: (Helper, Session))
    monkeypatch.chdir(tmp_path)
    return state


def test_session_options_keep_pyocd_passive() -> None:
    options = pyocd_session_options(100_000)
    assert options["connect_mode"] == "attach"
    assert options["auto_unlock"] is False
    assert options["target_override"] == "cortex_m"
    assert options["dap_protocol"] == "swd"
    assert options["cache.enable_memory"] is False
    assert options["resume_on_disconnect"] is False
    assert options["no_config"] is True
    assert options["frequency"] == 100_000


@pytest.mark.parametrize(
    ("address", "size"),
    [
        (CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE),
        (CHANNEL_BLOCK_ADDRESS + 0x38, 0x38),
        (ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE),
        (CPUID_ADDRESS, 4),
    ],
)
def test_allowlisted_reads(address: int, size: int) -> None:
    check_allowed(address, size)


@pytest.mark.parametrize(
    ("address", "size"),
    [
        (0x20000000, 0x4000),
        (CHANNEL_BLOCK_ADDRESS - 1, 4),
        (CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE + 1),
        (0x08000000, 4),
        (0x1FFF0000, 4),
        (ADC_BLOCK_ADDRESS, 0),
    ],
)
def test_reads_outside_the_allowlist_are_refused(address: int, size: int) -> None:
    with pytest.raises(ReadNotAllowedError):
        check_allowed(address, size)


def test_reader_only_issues_reads_and_state_queries(fake_pyocd: SimpleNamespace) -> None:
    reader = SwdReader(frequency_hz=200_000)
    reader.open()
    assert reader.probe is not None and reader.probe.unique_id == "0001A0000000"
    assert fake_pyocd.sessions[0].options["frequency"] == 200_000
    assert reader.read(CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE) == b"\x11" * CHANNEL_BLOCK_SIZE
    assert reader.read(CPUID_ADDRESS, 4) == (0x410CC601).to_bytes(4, "little")
    with pytest.raises(ReadNotAllowedError):
        reader.read(0x20000000, 16)
    reader.ensure_running()
    reader.close()
    assert fake_pyocd.sessions[0].closed
    assert set(fake_pyocd.target.calls) <= ALLOWED_TARGET_METHODS


def test_reader_refuses_a_halted_core_without_touching_it(fake_pyocd: SimpleNamespace) -> None:
    fake_pyocd.target.state = "HALTED"
    reader = SwdReader()
    with pytest.raises(TargetNotRunningError, match="HALTED"):
        reader.open()
    assert fake_pyocd.sessions[0].closed
    assert fake_pyocd.target.calls == ["get_state"]


def test_sleeping_core_is_accepted(fake_pyocd: SimpleNamespace) -> None:
    fake_pyocd.target.state = "SLEEPING"
    reader = SwdReader()
    reader.open()
    reader.close()


def test_no_probe(fake_pyocd: SimpleNamespace) -> None:
    fake_pyocd.probes = []
    with pytest.raises(ProbeNotFoundError) as info:
        SwdReader().open()
    assert info.value.exit_code == 3


def test_several_probes_require_a_selection(fake_pyocd: SimpleNamespace) -> None:
    fake_pyocd.probes = [FakeProbe("AAA111"), FakeProbe("BBB222")]
    with pytest.raises(ProbeSelectionError, match="--probe"):
        SwdReader().open()
    reader = SwdReader(unique_id="BBB")
    reader.open()
    assert reader.probe is not None and reader.probe.unique_id == "BBB222"


def test_busy_probe_is_reported(fake_pyocd: SimpleNamespace) -> None:
    fake_pyocd.open_error = RuntimeError("LIBUSB_ERROR_ACCESS: Access denied")
    with pytest.raises(ProbeBusyError):
        SwdReader().open()
    assert fake_pyocd.sessions[0].closed


def test_pyocd_user_script_blocks_attach(fake_pyocd: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "pyocd_user.py").write_text("")
    with pytest.raises(UnsafeEnvironmentError):
        SwdReader().open()
    assert fake_pyocd.sessions == []


@pytest.mark.parametrize(
    ("name", "message", "expected"),
    [
        ("ProbeDisconnected", "device gone", "disconnected"),
        ("TransferTimeoutError", "", "timeout"),
        ("TransferFaultError", "fault", "fault"),
        ("ProbeError", "Unable to open device", "unavailable"),
        ("ValueError", "strange", "SWD error"),
    ],
)
def test_error_translation(name: str, message: str, expected: str) -> None:
    exc = type(name, (Exception,), {})(message)
    error = translate_error(exc, "reading")
    assert isinstance(error, TransportError)
    assert expected in str(error)


def _block(elapsed: int) -> bytes:
    return make_frame((ChannelImage(elapsed_s=elapsed), EMPTY, EMPTY, EMPTY)).channels


def test_hardware_source_confirms_consistent_reads() -> None:
    reader = FakeReader([_block(10)])
    frame = HardwareSource(reader, consistency_reads=2).read_frame()
    assert frame.consistency is Consistency.CONSISTENT
    assert [call for call in reader.calls if call[0] == "read"] == [
        ("read", CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE),
        ("read", ADC_BLOCK_ADDRESS, ADC_BLOCK_SIZE),
        ("read", CHANNEL_BLOCK_ADDRESS, CHANNEL_BLOCK_SIZE),
    ]
    assert reader.calls[-1][0] == "ensure_running"


def test_hardware_source_retries_a_torn_read() -> None:
    reader = FakeReader([_block(10), _block(11), _block(11), _block(11)])
    frame = HardwareSource(reader, consistency_reads=2, consistency_retries=2).read_frame()
    assert frame.consistency is Consistency.CONSISTENT
    assert frame.channels == _block(11)


def test_hardware_source_flags_a_capture_that_never_stabilises() -> None:
    reader = FakeReader([_block(n) for n in range(20)])
    frame = HardwareSource(reader, consistency_reads=2, consistency_retries=1).read_frame()
    assert frame.consistency is Consistency.INCONSISTENT


def test_single_read_mode_is_unverified() -> None:
    reader = FakeReader([_block(10)])
    frame = HardwareSource(reader, consistency_reads=1).read_frame()
    assert frame.consistency is Consistency.UNVERIFIED
    assert len([call for call in reader.calls if call[0] == "read"]) == 2
