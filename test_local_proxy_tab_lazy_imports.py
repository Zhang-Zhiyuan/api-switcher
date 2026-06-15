import subprocess
import sys


def _run_import_check(module_name: str, forbidden: tuple[str, ...]):
    checks = "\n".join(
        f'if {name!r} in sys.modules: raise SystemExit("eager import: {name}")'
        for name in forbidden
    )
    code = f"""
import sys
import {module_name}  # noqa: F401

{checks}
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_proxy_node_picker_import_does_not_eagerly_load_remote_proxy():
    _run_import_check(
        "ui.widgets.proxy_node_picker",
        (
            "core.remote_proxy",
            "core.network_diagnostics",
            "core.ssh_manager",
            "paramiko",
        ),
    )


def test_local_proxy_tab_import_does_not_eagerly_load_proxy_cores():
    _run_import_check(
        "ui.tabs.local_proxy_tab",
        (
            "core.local_proxy",
            "core.remote_proxy",
            "core.network_diagnostic_settings",
            "core.startup_manager",
            "core.ssh_manager",
            "paramiko",
        ),
    )


def test_ssh_tab_import_does_not_eagerly_load_remote_cores():
    _run_import_check(
        "ui.tabs.ssh_tab",
        (
            "core.remote_proxy",
            "core.remote_auto_continue",
            "core.remote_git_login",
            "core.sync_manager",
            "core.ssh_manager",
            "core.network_diagnostic_settings",
            "paramiko",
            "keyring",
        ),
    )
