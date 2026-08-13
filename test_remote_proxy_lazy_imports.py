import subprocess
import sys


def test_remote_proxy_import_does_not_eagerly_load_ssh_or_keyring():
    code = r"""
import sys
import core.remote_proxy  # noqa: F401

for name in (
    "core.ssh_manager",
    "core.remote_config",
    "core.profile_manager",
    "core.network_diagnostics",
    "paramiko",
    "keyring",
):
    if name in sys.modules:
        raise SystemExit(f"eager import: {name}")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout
