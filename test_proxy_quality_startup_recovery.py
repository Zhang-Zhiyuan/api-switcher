"""Resource exhaustion must not leave proxy UI controls permanently busy."""

from types import SimpleNamespace

import pytest

from core import network_diagnostic_settings, remote_proxy
from ui.tabs import local_proxy_tab, ssh_tab


class InlineThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def _broken_thread(fail_at):
    class BrokenThread(InlineThread):
        def __init__(self, target, **kwargs):
            if fail_at == "constructor":
                raise RuntimeError("synthetic thread capacity exhausted")
            super().__init__(target, **kwargs)

        def start(self):
            raise RuntimeError("synthetic thread capacity exhausted")

    return BrokenThread


@pytest.mark.parametrize("kind", ["local", "ssh"])
@pytest.mark.parametrize("fail_at", ["constructor", "start"])
def test_quality_worker_start_failure_restores_controls_and_preserves_results(monkeypatch, kind, fail_at):
    module, cls = (local_proxy_tab, local_proxy_tab.LocalProxyTab) if kind == "local" else (ssh_tab, ssh_tab.SSHTab)
    prefix = "_" if kind == "local" else "_proxy_"
    tab = object.__new__(cls)
    nodes = [SimpleNamespace(node={"name": "synthetic"})]
    quality = {"old-node": {"result": "previous measurement"}}
    calls = SimpleNamespace(busy=[], status=[], toast=[])
    setattr(tab, prefix + "busy", False)
    setattr(tab, prefix + "subscription_nodes", nodes)
    setattr(tab, prefix + "quality_results", quality)
    setattr(tab, prefix + "latency_results", {"old-node": 20})
    setattr(tab, prefix + "quality_cancel_event", None)
    setattr(tab, prefix + "subscription_quality_scope", lambda *_args: (nodes, "合成范围"))
    setattr(tab, "_current_subscription_profile_id" if kind == "local" else "_current_proxy_subscription_profile_id", lambda: "synthetic-profile")

    def set_busy(value):
        setattr(tab, prefix + "busy", value)
        calls.busy.append(value)

    setattr(tab, "_set_busy" if kind == "local" else "_set_proxy_busy", set_busy)
    setattr(tab, "_set_status" if kind == "local" else "_set_proxy_status", lambda *args: calls.status.append(args))
    tab.winfo_toplevel = lambda: None
    monkeypatch.setattr(module, "show_toast", lambda *_args, **kwargs: calls.toast.append(kwargs))
    monkeypatch.setattr(network_diagnostic_settings, "load_settings", lambda: SimpleNamespace(enabled_services=lambda: ["netcoffee"]))
    monkeypatch.setattr(remote_proxy, "proxy_quality_effective_services", lambda *_args: ["netcoffee"])
    monkeypatch.setattr(remote_proxy, "quality_source_label_from_settings", lambda *_args: "合成源")
    monkeypatch.setattr(remote_proxy, "assess_proxy_node_qualities", lambda *_args, **_kwargs: pytest.fail("failed startup must never test a real node"))
    monkeypatch.setattr(module.threading, "Thread", _broken_thread(fail_at))
    method = tab._measure_subscription_qualities if kind == "local" else tab._measure_proxy_subscription_qualities
    method()
    assert calls.busy == [True, False]
    assert getattr(tab, prefix + "busy") is False
    assert getattr(tab, prefix + "quality_cancel_event") is None
    assert getattr(tab, prefix + "quality_results") is quality
    assert getattr(tab, prefix + "subscription_nodes") is nodes
    assert "未启动" in calls.status[-1][0]
    assert calls.status[-1][1] == "error"

    # Once thread capacity is available the same action must enter its worker,
    # even if the diagnostic service itself reports a separate failure.
    def failed_diagnostic(*_args, **_kwargs):
        raise OSError("synthetic diagnostic unavailable")

    tab.winfo_exists = lambda: True
    tab._run_on_ui_thread = lambda callback: callback()
    monkeypatch.setattr(remote_proxy, "assess_proxy_node_qualities", failed_diagnostic)
    monkeypatch.setattr(module.threading, "Thread", InlineThread)
    method()
    assert calls.busy == [True, False, True, False]
    assert getattr(tab, prefix + "quality_cancel_event") is None
    assert getattr(tab, prefix + "quality_results") is quality
    assert "synthetic diagnostic unavailable" in calls.status[-1][0]


@pytest.mark.parametrize("fail_at", ["constructor", "start"])
def test_local_saved_state_worker_start_failure_preserves_editor_and_can_retry(monkeypatch, fail_at):
    tab = object.__new__(local_proxy_tab.LocalProxyTab)
    tab._saved_subscription_after_id = None
    tab._saved_subscription_loaded = False
    tab._saved_subscription_load_generation = 0
    tab._deferred_saved_subscription_pending = False
    tab._auto_refresh_var = SimpleNamespace(set=lambda _value: None)
    tab._periodic_update_var = SimpleNamespace(set=lambda _value: None)
    tab._periodic_update_entry = None
    tab._schedule_periodic_update = lambda **_kwargs: None
    tab._subscription_profile_blocks_automatic_refresh = lambda: True
    tab._refresh_subscription_profile_options = lambda *_args, **_kwargs: None
    tab._apply_subscription_profile_inputs = lambda *_args: pytest.fail("must retain unsaved editor draft")
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    statuses, reads = [], []
    tab._set_status = lambda *args: statuses.append(args)
    tab._set_cache_status = lambda *_args: None
    monkeypatch.setattr(local_proxy_tab, "is_active_tab", lambda _tab: True)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: reads.append(True) or {})
    monkeypatch.setattr(remote_proxy, "proxy_subscription_auto_refresh_enabled", lambda _scope: False)
    monkeypatch.setattr(local_proxy_tab.threading, "Thread", _broken_thread(fail_at))
    tab._load_saved_subscription_ui()
    assert tab._saved_subscription_loaded is False
    assert tab._deferred_saved_subscription_pending is True
    assert "未启动" in statuses[-1][0]
    assert not reads
    monkeypatch.setattr(local_proxy_tab.threading, "Thread", InlineThread)
    tab._load_saved_subscription_ui()
    assert tab._saved_subscription_loaded is True
    assert reads == [True]
    assert "保留当前草稿" in statuses[-1][0]
