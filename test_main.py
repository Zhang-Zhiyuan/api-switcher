from __future__ import annotations

import io
import sys
from types import ModuleType

import main
from ui import app as app_module
from ui.startup_splash import (
    SPLASH_ARG,
    StartupSplash,
    _iter_stdin_lines_utf8,
    _splash_subprocess_env,
    splash_process_supported,
)


def test_parse_args_defaults_to_splash_enabled():
    args = main.parse_args([])

    assert args.start_minimized is False
    assert args.no_splash is False


def test_quick_switch_labels_identify_target_tools():
    assert app_module.PROXY_QUALITY_DIALOG_LABEL == "代理质量检测"
    assert app_module.QUICK_SWITCH_TITLE == "快速切换 API"
    assert app_module.CLAUDE_QUICK_SWITCH_LABEL == "Claude Code 使用"
    assert app_module.CODEX_QUICK_SWITCH_LABEL == "Codex CLI 使用"


def test_proxy_quality_is_not_a_primary_tab():
    labels = [label for label, *_spec in app_module.TAB_SPECS]

    assert "环境检测" not in labels
    assert "环境监测" not in labels
    assert app_module.PROXY_QUALITY_DIALOG_LABEL not in labels
    assert hasattr(app_module.App, "_show_proxy_quality_dialog")
    assert hasattr(app_module.App, "_on_proxy_quality_settings_saved")
    assert not hasattr(app_module.App, "_show_network_diagnostics_tab")


def test_only_first_primary_tab_loads_eagerly():
    specs = {label: eager for label, _attr, _module_name, _class_name, eager in app_module.TAB_SPECS}

    assert specs["Claude Code"] is True
    assert specs["Codex CLI"] is False
    assert all(eager is False for label, eager in specs.items() if label not in {"Claude Code"})


def test_proxy_quality_dialog_module_is_importable():
    from ui.dialogs.proxy_quality_dialog import ProxyQualityDialog
    from ui.widgets.proxy_quality_panel import ProxyQualityPanel

    assert ProxyQualityDialog.__name__ == "ProxyQualityDialog"
    assert ProxyQualityPanel.__name__ == "ProxyQualityPanel"


def test_parse_args_supports_no_splash_and_minimized_aliases():
    args = main.parse_args(["--tray", "--no-splash", "--ignored"])

    assert args.start_minimized is True
    assert args.no_splash is True
    assert args.splash_child is False


def test_parse_args_supports_hidden_splash_child_mode():
    args = main.parse_args([SPLASH_ARG])

    assert args.splash_child is True


def test_disabled_startup_splash_is_noop():
    splash = StartupSplash(enabled=False)

    assert splash.visible is False
    splash.pulse("ignored")
    splash.keep_visible_for(0)
    splash.close()
    assert splash.visible is False


def test_startup_splash_is_disabled_for_frozen_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    splash = StartupSplash()

    assert splash_process_supported() is False
    assert splash.visible is False
    splash.close()


def test_startup_splash_reads_status_pipe_as_utf8():
    stdin = ModuleType("stdin")
    stdin.buffer = io.BytesIO("STATUS\t正在准备配置...\nCLOSE\n".encode("utf-8"))

    assert list(_iter_stdin_lines_utf8(stdin)) == ["STATUS\t正在准备配置...", "CLOSE"]


def test_startup_splash_child_forces_utf8_environment():
    env = _splash_subprocess_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_flush_usage_session_ends_active_recorder(monkeypatch):
    calls = []
    module = ModuleType("core.usage_recorder")

    class FakeUsageRecorder:
        def end_session(self):
            calls.append("ended")

    module.usage_recorder = FakeUsageRecorder()
    monkeypatch.setitem(sys.modules, "core.usage_recorder", module)

    main.flush_usage_session()

    assert calls == ["ended"]
