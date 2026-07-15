from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from core.api_tester import APITester
from ui.dialogs import profile_editor as profile_editor_module
from ui.dialogs.profile_editor import ProfileEditorDialog
from ui.dialogs.ssh_editor import SSHEditorDialog


class _Control:
    def __init__(self):
        self.configurations = []

    def configure(self, **kwargs):
        self.configurations.append(kwargs)


class _ModelControl:
    def __init__(self, values):
        self.values = values

    def cget(self, key):
        assert key == "values"
        return self.values


class _FailingThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("thread unavailable")


@pytest.mark.parametrize("dialog_class", [ProfileEditorDialog, SSHEditorDialog])
def test_editor_safe_after_uses_captured_dispatcher_without_worker_tk_calls(dialog_class):
    callbacks = []
    results = []
    dialog = object.__new__(dialog_class)
    dialog._destroyed = False
    dialog._ui_dispatch = lambda callback: callbacks.append(callback) or True
    dialog.winfo_toplevel = lambda: (_ for _ in ()).throw(
        AssertionError("worker must not call Tk.winfo_toplevel")
    )
    dialog.winfo_exists = lambda: (_ for _ in ()).throw(
        AssertionError("worker must not call Tk.winfo_exists")
    )
    dialog.after = lambda *_args: (_ for _ in ()).throw(
        AssertionError("worker must not call Tk.after")
    )

    worker_result = []
    thread = threading.Thread(
        target=lambda: worker_result.append(dialog_class._safe_after(dialog, lambda: results.append("done")))
    )
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert worker_result == [True]
    assert len(callbacks) == 1
    assert results == []
    callbacks[0]()
    assert results == ["done"]


def test_profile_test_thread_start_failure_restores_busy_state(monkeypatch):
    monkeypatch.setattr(threading, "Thread", _FailingThread)
    dialog = object.__new__(ProfileEditorDialog)
    dialog._test_busy = False
    dialog._profile_type = "claude"
    dialog._profile = None
    dialog._test_btn = _Control()
    dialog._error_label = _Control()
    dialog._collect_data = lambda: {
        "name": "test",
        "base_url": "https://example.invalid",
        "model": "model",
    }
    dialog._current_claude_provider = lambda: None
    dialog._get_secret_value = lambda *_args: "secret"

    ProfileEditorDialog._test_connection(dialog)

    assert dialog._test_busy is False
    assert dialog._test_btn.configurations[-1] == {"state": "normal", "text": "测试连接"}
    assert dialog._error_label.configurations[-1]["text"] == "无法启动连接测试: thread unavailable"


def test_profile_model_refresh_thread_start_failure_restores_busy_state(monkeypatch):
    monkeypatch.setattr(threading, "Thread", _FailingThread)
    dialog = object.__new__(ProfileEditorDialog)
    dialog._refresh_busy = False
    dialog._profile_type = "claude"
    dialog._profile = None
    dialog._refresh_buttons = [_Control()]
    dialog._error_label = _Control()
    dialog._collect_data = lambda: {"base_url": "https://example.invalid"}
    dialog._current_claude_provider = lambda: None
    dialog._get_secret_value = lambda *_args: "secret"

    ProfileEditorDialog._refresh_models(dialog)

    assert dialog._refresh_busy is False
    assert dialog._refresh_buttons[0].configurations[-1] == {"state": "normal", "text": "刷新最佳"}
    assert dialog._error_label.configurations[-1]["text"] == "无法启动模型刷新: thread unavailable"


def test_ssh_test_thread_start_failure_restores_busy_state(monkeypatch):
    monkeypatch.setattr(threading, "Thread", _FailingThread)
    dialog = object.__new__(SSHEditorDialog)
    dialog._test_busy = False
    dialog._test_btn = _Control()
    dialog._test_result = _Control()
    dialog._collect_data = lambda: {}
    dialog._build_profile = lambda _data: SimpleNamespace(name="server")

    SSHEditorDialog._test_connection(dialog)

    assert dialog._test_busy is False
    assert dialog._test_btn.configurations[-1] == {"state": "normal", "text": "测试连接"}
    assert dialog._test_result.configurations[-1]["text"] == "无法启动连接测试: thread unavailable"


def test_profile_model_for_save_uses_loaded_and_bundled_values_deterministically():
    dialog = object.__new__(ProfileEditorDialog)
    dialog._fields = {"model": (_ModelControl([None, "remote-first", "bundled-default"]), "combo")}
    provider = SimpleNamespace(default_model="bundled-default", supported_models=["bundled-default"])

    assert ProfileEditorDialog._model_for_save(dialog, provider, is_codex=False) == "bundled-default"

    dialog._fields = {"model": (_ModelControl(["remote-first", "remote-second"]), "combo")}
    assert ProfileEditorDialog._model_for_save(dialog, provider, is_codex=False) == "remote-first"

    dialog._fields = {"model": (_ModelControl([""]), "combo")}
    fallback_provider = SimpleNamespace(default_model="", supported_models=[None, "supported-first"])
    assert ProfileEditorDialog._model_for_save(dialog, fallback_provider, is_codex=False) == "supported-first"
    assert ProfileEditorDialog._model_for_save(dialog, None, is_codex=False) == "claude-sonnet-4"
    assert ProfileEditorDialog._model_for_save(dialog, None, is_codex=True) == "gpt-5.5"


@pytest.mark.parametrize("profile_type", ["claude", "codex"])
def test_profile_save_with_empty_model_never_fetches_models_on_tk_thread(monkeypatch, profile_type):
    provider = SimpleNamespace(
        name="mock-provider",
        display_name="Mock Provider",
        default_model="bundled-default",
        supported_models=["bundled-default"],
        codex_env_key="MOCK_API_KEY",
        requires_openai_auth=False,
        base_url_for_codex=lambda: "https://mock.invalid/v1",
    )
    monkeypatch.setattr(
        profile_editor_module.ProviderRegistry,
        "get_provider_by_display_name",
        lambda _display_name: provider,
    )
    monkeypatch.setattr(
        profile_editor_module.ProviderRegistry,
        "get_codex_wire_api",
        lambda *_args: "responses",
    )
    monkeypatch.setattr(
        APITester,
        "fetch_claude_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("save must not use network")),
    )
    monkeypatch.setattr(
        APITester,
        "fetch_openai_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("save must not use network")),
    )

    data = (
        {
            "name": "demo",
            "provider": "Mock Provider",
            "base_url": "https://mock.invalid",
            "model": "",
        }
        if profile_type == "claude"
        else {
            "name": "demo",
            "codex_provider": "Mock Provider",
            "custom_base_url": "https://mock.invalid/v1",
            "custom_name": "",
            "custom_env_key": "",
            "model": "",
        }
    )
    saved = []
    destroyed = []
    errors = []
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = profile_type
    dialog._profile = None
    dialog._fields = {"model": (_ModelControl(["remote-loaded", "remote-second"]), "combo")}
    dialog._collect_data = lambda: dict(data)
    dialog._get_secret_value = lambda *_args: "secret"
    dialog._on_save = lambda payload, profile: saved.append((payload, profile))
    dialog._show_error = errors.append
    dialog.update_idletasks = lambda: (_ for _ in ()).throw(AssertionError("save must not spin Tk"))
    dialog.destroy = lambda: destroyed.append(True)

    ProfileEditorDialog._save(dialog)

    assert errors == []
    assert destroyed == [True]
    assert saved[0][0]["model"] == "remote-loaded"
