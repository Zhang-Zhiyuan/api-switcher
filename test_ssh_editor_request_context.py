"""SSH test results must describe the unchanged form, not an earlier draft."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ui.dialogs import ssh_editor
from ui.theme import COLORS


class _Control:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


@pytest.fixture
def pending_test(monkeypatch):
    import core.ssh_manager as ssh_core

    workers = []
    callbacks = []
    responses = [(True, "synthetic connection succeeded")]

    class _DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            workers.append(self.target)

    def test_connection(_profile, *, secret_overrides):
        assert secret_overrides == {}
        response = responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(ssh_editor.threading, "Thread", _DeferredThread)
    monkeypatch.setattr(ssh_core.ssh_manager, "test_connection", test_connection)
    form = {
        "name": "synthetic server",
        "host": "ssh.example.invalid",
        "port": "22",
        "username": "synthetic-user",
        "auth_type": "key",
        "private_key_path": "synthetic-key",
        "key_passphrase": "synthetic-passphrase",
        "password": "synthetic-password",
        "remote_claude_dir": "",
        "remote_codex_dir": "",
    }
    dialog = object.__new__(ssh_editor.SSHEditorDialog)
    dialog._destroyed = False
    dialog._test_busy = False
    dialog._test_btn = _Control()
    dialog._test_result = _Control()
    dialog._collect_data = lambda: dict(form)
    dialog._build_save_plan = lambda _data: SimpleNamespace(profile=object(), secret_updates={})
    dialog._safe_after = lambda callback: callbacks.append(callback) or True
    dialog.winfo_exists = lambda: True
    return SimpleNamespace(
        dialog=dialog, form=form, responses=responses, workers=workers, callbacks=callbacks,
    )


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("name", "different server"),
        ("host", "other.example.invalid"),
        ("port", "2222"),
        ("username", "different-user"),
        ("auth_type", "password"),
        ("private_key_path", "different-key"),
        ("key_passphrase", "changed-synthetic-passphrase"),
        ("password", "changed-synthetic-password"),
        ("remote_claude_dir", "~/other-claude"),
        ("remote_codex_dir", "~/other-codex"),
    ],
)
def test_late_ssh_result_is_discarded_after_form_changes(pending_test, field, replacement, success):
    state = pending_test
    state.responses[0] = success, "result belongs to the original server"
    state.dialog._test_connection()
    state.workers[0]()
    state.form[field] = replacement
    state.callbacks[0]()

    assert state.dialog._test_busy is False
    assert state.dialog._test_btn.options == {"state": "normal", "text": "测试连接"}
    assert "配置已变化" in state.dialog._test_result.options["text"]
    assert "重新测试" in state.dialog._test_result.options["text"]
    assert state.dialog._test_result.options["text_color"] == COLORS["warning"]
    assert replacement not in state.dialog._test_result.options["text"]
    assert "original server" not in state.dialog._test_result.options["text"]


@pytest.mark.parametrize("success", [True, False])
def test_unchanged_ssh_form_keeps_test_result(pending_test, success):
    state = pending_test
    state.responses[0] = success, "unchanged synthetic result"
    state.dialog._test_connection()
    state.workers[0]()
    state.callbacks[0]()

    assert state.dialog._test_busy is False
    assert state.dialog._test_result.options == {
        "text": "unchanged synthetic result",
        "text_color": COLORS["success"] if success else COLORS["danger"],
    }


def test_late_ssh_exception_does_not_describe_changed_form(pending_test):
    state = pending_test
    state.responses[0] = OSError("old server failure")
    state.dialog._test_connection()
    state.form["host"] = "changed.example.invalid"
    state.workers[0]()
    state.callbacks[0]()

    assert state.dialog._test_busy is False
    assert "配置已变化" in state.dialog._test_result.options["text"]
    assert "old server failure" not in state.dialog._test_result.options["text"]


def test_queued_ssh_result_after_destroy_never_calls_tk(pending_test):
    state = pending_test
    state.dialog._test_connection()
    state.workers[0]()
    state.dialog._destroyed = True

    def forbidden():
        pytest.fail("a queued callback must not query a destroyed Tk window")

    state.dialog.winfo_exists = forbidden
    state.dialog._collect_data = forbidden
    before = dict(state.dialog._test_result.options)
    state.callbacks[0]()
    assert state.dialog._test_result.options == before


def test_ssh_result_rejects_unreadable_form_without_leaving_test_busy(pending_test):
    state = pending_test
    state.dialog._test_connection()
    state.workers[0]()

    def unreadable():
        raise RuntimeError("synthetic form unavailable")

    state.dialog._collect_data = unreadable
    state.callbacks[0]()
    assert state.dialog._test_busy is False
    assert state.dialog._test_result.options["text_color"] == COLORS["warning"]


def test_ssh_test_start_failure_restores_button(pending_test, monkeypatch):
    state = pending_test

    class _FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread unavailable")

    monkeypatch.setattr(ssh_editor.threading, "Thread", _FailingThread)
    state.dialog._test_connection()
    assert state.dialog._test_busy is False
    assert state.dialog._test_btn.options == {"state": "normal", "text": "测试连接"}
    assert state.dialog._test_result.options["text_color"] == COLORS["danger"]
    assert "无法启动连接测试" in state.dialog._test_result.options["text"]
