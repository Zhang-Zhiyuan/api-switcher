"""Queued API results must not overwrite fields edited while requests run."""
from types import SimpleNamespace

import pytest

from core.api_tester import APITester
from ui.dialogs.profile_editor import ProfileEditorDialog


@pytest.fixture(params=["claude", "codex"])
def request_editor(request, monkeypatch):
    import threading

    callbacks, applied, statuses, model_changes = [], [], [], []
    workers = []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(threading, "Thread", DeferredThread)
    kind = request.param
    data = {
        "name": "synthetic", "model": "chosen-model",
        "provider" if kind == "claude" else "codex_provider": "Custom",
        "base_url" if kind == "claude" else "custom_base_url": "https://synthetic.invalid/v1",
        "auth_token" if kind == "claude" else "api_key": "synthetic-secret",
        "auth_scheme" if kind == "claude" else "custom_requires_openai_auth": "auth_token" if kind == "claude" else False,
        "effort_level" if kind == "claude" else "model_reasoning_effort": "high",
    }
    result = SimpleNamespace(success=True, message="synthetic success", models=["remote-model"],
                             recommended_model="remote-model", selected_model="remote-model", error_details="")
    for name in ("fetch_claude_models", "fetch_openai_models", "test_claude_api", "benchmark_openai_wire_apis"):
        monkeypatch.setattr(APITester, name, lambda *_args, **_kwargs: result)
    dialog = object.__new__(ProfileEditorDialog)
    dialog._destroyed = False
    dialog._profile_type = kind
    dialog._profile = None
    dialog._refresh_busy = dialog._test_busy = False
    dialog._refresh_buttons = []
    dialog._test_btn = SimpleNamespace(configure=lambda **_kw: None)
    dialog._fields = {"model": (SimpleNamespace(set=model_changes.append), "combo")}
    dialog._collect_data = lambda: dict(data)
    dialog._get_secret_value = lambda *_args: "synthetic-secret"
    dialog._current_claude_provider = dialog._current_codex_provider = lambda: None
    dialog._codex_requires_openai_auth = lambda *_args: False
    dialog._safe_after = lambda callback: callbacks.append(callback) or True
    dialog.winfo_exists = lambda: True
    dialog._show_status = lambda text, *_args: statuses.append(text)
    dialog._show_error = lambda text: statuses.append(text)
    dialog._show_test_result = lambda *args: applied.append(args)
    dialog._apply_model_list = lambda *args, **kwargs: applied.append((args, kwargs))
    dialog._on_model_change = lambda: None
    return SimpleNamespace(dialog=dialog, data=data, result=result, workers=workers,
                           callbacks=callbacks, applied=applied, statuses=statuses, models=model_changes)


def finish_request(state, operation):
    getattr(state.dialog, "_refresh_models" if operation == "models" else "_test_connection")()
    assert len(state.workers) == 1
    state.workers.pop()()
    assert len(state.callbacks) == 1


@pytest.mark.parametrize("operation", ["models", "test"])
@pytest.mark.parametrize("changed", ["provider", "url", "secret", "model", "auth", "effort", "name", None])
def test_requests_only_apply_to_unchanged_form(request_editor, operation, changed):
    state = request_editor
    finish_request(state, operation)
    # Request completed, but the UI callback has not yet been consumed.
    fields = list(state.data)
    keys = {"provider": fields[2], "url": fields[3], "secret": fields[4],
            "auth": fields[5], "effort": fields[6], "name": "name", "model": "model"}
    if changed is not None:
        state.data[keys[changed]] = "edited-value"
    before = dict(state.data)
    state.callbacks.pop()()
    assert not state.dialog._refresh_busy and not state.dialog._test_busy
    assert state.data == before
    if changed is None:
        assert len(state.applied) == 1
    else:
        assert not state.applied and not state.models
        assert "已忽略" in state.statuses[-1]
        assert "synthetic-secret" not in " ".join(state.statuses)


@pytest.mark.parametrize("operation", ["models", "test"])
def test_queued_result_after_editor_destroy_never_touches_tk(request_editor, operation):
    state = request_editor
    finish_request(state, operation)
    state.dialog._destroyed = True
    def unavailable():
        raise AssertionError("closed window must not query Tcl")
    state.dialog.winfo_exists = unavailable
    state.callbacks.pop()()
    assert not state.applied and not state.models


@pytest.mark.parametrize("operation", ["models", "test"])
def test_failed_request_for_old_fields_is_not_shown_as_current_failure(request_editor, operation):
    state = request_editor
    state.result.success = False
    state.result.models = []
    state.result.selected_model = None
    state.result.message = "synthetic old failure"
    finish_request(state, operation)
    state.data["model"] = "new-model"
    state.callbacks.pop()()
    assert not state.applied and not state.models
    assert "已忽略" in state.statuses[-1]
    assert "synthetic old failure" not in state.statuses[-1]
