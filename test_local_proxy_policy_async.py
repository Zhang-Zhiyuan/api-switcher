"""Policy controls use deferred fake workers, never Tk or machine settings."""
from types import SimpleNamespace

import pytest

from core import local_proxy
from ui.tabs import local_proxy_tab as module


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def policy_tab(monkeypatch):
    calls = SimpleNamespace(work=[], ui=[], status=[], toast=[], reload=[], writes=[], apply=[])

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            calls.work.append(self.target)

    tab = object.__new__(module.LocalProxyTab)
    tab._busy = False
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    tab._set_status = lambda *args: calls.status.append(args)
    tab._set_routing_status = lambda *args: calls.status.append(args)
    tab._run_on_ui_thread = calls.ui.append
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()
    tab._load_proxy_preferences_ui = lambda: calls.reload.append(True)
    tab._proxy_non_cn_var = Value(True)
    tab._strict_privacy_var = Value(True)
    tab._wsl_share_var = Value(True)
    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=DeferredThread))
    monkeypatch.setattr(module, "local_proxy", local_proxy)
    monkeypatch.setattr(module, "show_toast", lambda *args, **kw: calls.toast.append((args, kw)))
    monkeypatch.setattr(module, "ConfirmDialog", lambda *args, **kw: kw["on_confirm"]())
    monkeypatch.setattr(local_proxy, "set_local_proxy_non_cn_mode", calls.writes.append)
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: calls.apply.append(True) or "规则已应用")
    return tab, calls


def fail_to_start(monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread creation failure")

    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=BrokenThread))


@pytest.mark.parametrize("unavailable", ["busy", "thread"])
@pytest.mark.parametrize("policy", ["strict_privacy", "wsl_share"])
@pytest.mark.parametrize("enabled", [True, False])
def test_confirmed_policy_restores_checkbox_when_task_never_starts(policy_tab, monkeypatch, policy, unavailable, enabled):
    tab, calls = policy_tab
    getattr(tab, f"_{policy}_var").set(enabled)
    if unavailable == "busy":
        tab._busy = True
    else:
        fail_to_start(monkeypatch)
    getattr(tab, f"_on_{policy}_toggle")()
    assert getattr(tab, f"_{policy}_var").get() is not enabled
    assert calls.reload == [True]
    assert calls.work == []
    assert tab._busy is (unavailable == "busy")


@pytest.mark.parametrize("unavailable", ["busy", "thread"])
def test_unstarted_task_notifies_failure_once_and_keeps_status_if_recovery_raises(policy_tab, monkeypatch, unavailable):
    tab, calls = policy_tab
    errors = []

    def broken_recovery(error):
        errors.append(error)
        raise RuntimeError("synthetic UI recovery failure")

    if unavailable == "busy":
        tab._busy = True
    else:
        fail_to_start(monkeypatch)
    tab._run_local_task("checking", lambda: pytest.fail("must not run"), "检查", on_error=broken_recovery)
    assert len(errors) == 1
    assert calls.toast
    assert tab._busy is (unavailable == "busy")


@pytest.mark.parametrize("enabled", [True, False])
def test_non_cn_preference_write_and_apply_wait_for_background_worker(policy_tab, enabled):
    tab, calls = policy_tab
    tab._proxy_non_cn_var.set(enabled)
    tab._on_proxy_non_cn_toggle()
    assert calls.writes == []
    assert calls.apply == []
    assert tab._busy is True
    calls.work.pop(0)()
    assert calls.writes == [enabled]
    assert calls.apply == [True]
    assert calls.reload == []
    calls.ui.pop(0)()
    assert calls.reload == [True]
    assert tab._busy is False
    assert "规则已应用" in calls.status[-1][0]


@pytest.mark.parametrize("unavailable", ["busy", "thread"])
@pytest.mark.parametrize("enabled", [True, False])
def test_non_cn_rejected_change_restores_visible_preference_without_saving(policy_tab, monkeypatch, unavailable, enabled):
    tab, calls = policy_tab
    tab._proxy_non_cn_var.set(enabled)
    if unavailable == "busy":
        tab._busy = True
    else:
        fail_to_start(monkeypatch)
    tab._on_proxy_non_cn_toggle()
    assert tab._proxy_non_cn_var.get() is not enabled
    assert calls.writes == []
    assert calls.apply == []
    assert calls.reload == [True]


@pytest.mark.parametrize("stage", ["save", "apply"])
def test_non_cn_error_restores_authoritative_preferences_and_preserves_saved_intent(policy_tab, monkeypatch, stage):
    tab, calls = policy_tab

    def fail(*args):
        raise RuntimeError(f"synthetic {stage} failure")

    monkeypatch.setattr(local_proxy, "set_local_proxy_non_cn_mode" if stage == "save" else "apply_local_proxy_routing_to_running", fail)
    tab._on_proxy_non_cn_toggle()
    assert len(calls.work) == 1
    calls.work.pop(0)()
    calls.ui.pop(0)()
    assert tab._proxy_non_cn_var.get() is (stage == "apply")
    assert calls.writes == ([] if stage == "save" else [True])
    assert calls.reload == [True]
    assert calls.status[-1][1] == "error"
    if stage == "apply":
        assert "偏好已保存" in calls.status[-1][0]
    assert tab._busy is False


def test_non_cn_control_is_disabled_during_busy_and_restored():
    class Shell:
        _subscription_options = {}

        def __getattr__(self, _name):
            return None

        def _update_subscription_profile_form_controls(self):
            pass

    tab = Shell()
    states = []
    tab._proxy_non_cn_check = SimpleNamespace(configure=lambda **kw: states.append(kw["state"]))
    module.LocalProxyTab._set_busy(tab, True)
    module.LocalProxyTab._set_busy(tab, False)
    assert states == ["disabled", "normal"]


def test_task_worker_error_recovers_once_on_ui_thread(policy_tab):
    tab, calls = policy_tab
    errors = []

    def fail():
        raise RuntimeError("synthetic worker failure")

    tab._run_local_task("running", fail, "检查", on_error=errors.append)
    calls.work.pop(0)()
    assert errors == []
    calls.ui.pop(0)()
    assert errors == ["synthetic worker failure"]
    assert tab._busy is False


def test_late_worker_failure_does_not_touch_destroyed_ui(policy_tab):
    tab, calls = policy_tab
    errors = []

    def fail():
        raise RuntimeError("synthetic worker failure")

    tab._run_local_task("running", fail, "检查", on_error=errors.append)
    calls.work.pop(0)()
    tab.winfo_exists = lambda: False
    calls.ui.pop(0)()
    assert errors == []
    assert calls.toast == []
