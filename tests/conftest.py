from __future__ import annotations

import pytest

from chn4_bridge.capture import Capture, load_capture

from helpers import REAL_CAPTURE


@pytest.fixture
def real_capture() -> Capture:
    return load_capture(REAL_CAPTURE)
