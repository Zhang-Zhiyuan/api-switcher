import subprocess
import sys


def test_security_import_does_not_eagerly_load_keyring():
    code = r"""
import sys
import core.security  # noqa: F401

if "keyring" in sys.modules:
    raise SystemExit("keyring imported eagerly")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_profile_manager_import_does_not_eagerly_load_keyring():
    code = r"""
import sys
import core.profile_manager  # noqa: F401

for name in ("core.security", "keyring"):
    if name in sys.modules:
        raise SystemExit(f"{name} imported eagerly")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout
