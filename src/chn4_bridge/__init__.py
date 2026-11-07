"""Read-only SWD bridge for the 18650-4CHN four-channel Li-ion charger/tester."""

from ._version import __version__
from .acquisition import Acquisition, Broadcaster, ErrorEvent, LinkState, Monitor, StatusEvent
from .config import Config, ConfigError, load_config
from .decoder import AdcConversion, DecodeError, decode_frame, split_sram_image
from .derived import SnapshotProcessor
from .models import (
    FIELD_QUALITY,
    UNITS,
    Activity,
    ChannelReading,
    ChannelState,
    Consistency,
    ExperimentalFields,
    Quality,
    RawFrame,
    Snapshot,
    SourceKind,
)
from .sources import EndOfReplay, FrameSource, HardwareSource, ReplaySource, open_source
from .synthetic import DemoSource
from .transport import BridgeError, TransportError

__all__ = [
    "FIELD_QUALITY",
    "UNITS",
    "Acquisition",
    "Activity",
    "AdcConversion",
    "BridgeError",
    "Broadcaster",
    "ChannelReading",
    "ChannelState",
    "Config",
    "ConfigError",
    "Consistency",
    "DecodeError",
    "DemoSource",
    "EndOfReplay",
    "ErrorEvent",
    "ExperimentalFields",
    "FrameSource",
    "HardwareSource",
    "LinkState",
    "Monitor",
    "Quality",
    "RawFrame",
    "ReplaySource",
    "Snapshot",
    "SnapshotProcessor",
    "SourceKind",
    "StatusEvent",
    "TransportError",
    "__version__",
    "decode_frame",
    "load_config",
    "open_source",
    "split_sram_image",
]
