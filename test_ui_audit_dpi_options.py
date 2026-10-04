import argparse

import pytest

from tools.ui_visual_audit import audit_client_size


@pytest.mark.parametrize("value,expected", [
    ("1280x900", (1280, 900)), ("320x240", (320, 240)), ("7680x4320", (7680, 4320)),
])
def test_physical_client_size_is_explicit(value, expected):
    assert audit_client_size(value) == expected


@pytest.mark.parametrize("value", ["", "1280", "1280X900", "0x0", "319x240", "320x239", "7681x4320", "1280x4321"])
def test_invalid_client_size_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        audit_client_size(value)
