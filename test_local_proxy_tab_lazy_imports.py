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


def test_env_tab_import_does_not_eagerly_load_ssh_core():
    _run_import_check(
        "ui.tabs.env_tab",
        (
            "core.profile_manager",
            "core.ssh_manager",
            "paramiko",
            "keyring",
        ),
    )


def test_session_migration_tab_import_does_not_eagerly_load_session_core():
    _run_import_check(
        "ui.tabs.session_migration_tab",
        (
            "core.session_migration",
            "core.remote_config",
            "core.ssh_manager",
            "paramiko",
            "keyring",
        ),
    )


def test_common_tab_import_does_not_eagerly_load_config_cores():
    _run_import_check(
        "ui.tabs.common_tab",
        (
            "core.parser",
            "core.toml_parser",
            "core.auth_parser",
            "core.startup_manager",
            "core.vscode_parser",
            "core.switcher",
            "core.profile_manager",
            "keyring",
        ),
    )


def test_browser_tab_import_does_not_eagerly_load_browser_cores():
    _run_import_check(
        "ui.tabs.browser_tab",
        (
            "core.profile_manager",
            "core.browser_data_manager",
            "core.browser_launcher",
            "core.browser_profile_manager",
            "ui.dialogs.browser_profile_editor",
            "ui.dialogs.bulk_operation_result_dialog",
            "core.security",
            "keyring",
        ),
    )


def test_backup_tab_import_does_not_eagerly_load_backup_cores():
    _run_import_check(
        "ui.tabs.backup_tab",
        (
            "core.backup_manager",
            "core.local_config_bundle",
            "core.portable_migration",
            "core.profile_manager",
            "core.security",
            "ui.dialogs.password_dialog",
            "keyring",
        ),
    )


def test_claude_tab_import_does_not_eagerly_load_profile_cores():
    _run_import_check(
        "ui.tabs.claude_tab",
        (
            "core.profile_manager",
            "core.providers",
            "ui.dialogs.profile_editor",
            "models.profile",
            "core.security",
            "keyring",
        ),
    )


def test_codex_tab_import_does_not_eagerly_load_profile_cores():
    _run_import_check(
        "ui.tabs.codex_tab",
        (
            "core.profile_manager",
            "core.providers",
            "ui.dialogs.profile_editor",
            "models.profile",
            "core.security",
            "keyring",
        ),
    )


def test_auto_continue_control_import_does_not_eagerly_load_manager():
    _run_import_check(
        "ui.widgets.auto_continue_control",
        (
            "core.auto_continue.manager",
            "core.auto_continue.claude_provider",
            "core.auto_continue.codex_provider",
            "models.auto_continue",
            "core.git_manager",
            "keyring",
        ),
    )


def test_proxy_quality_panel_import_does_not_eagerly_load_network_cores():
    _run_import_check(
        "ui.widgets.proxy_quality_panel",
        (
            "core.network_diagnostic_settings",
            "core.network_diagnostics",
            "core.security",
            "urllib.request",
            "keyring",
        ),
    )


def test_proxy_quality_dialog_import_does_not_eagerly_load_network_cores():
    _run_import_check(
        "ui.dialogs.proxy_quality_dialog",
        (
            "core.network_diagnostic_settings",
            "core.network_diagnostics",
            "core.security",
            "urllib.request",
            "keyring",
        ),
    )


def test_usage_stats_tab_import_does_not_eagerly_load_stats_store():
    _run_import_check(
        "ui.tabs.usage_stats_tab",
        (
            "core.usage_stats",
            "core.usage_recorder",
        ),
    )


def test_app_import_does_not_eagerly_load_tray_core():
    _run_import_check(
        "ui.app",
        (
            "core.tray_manager",
            "pystray",
        ),
    )
