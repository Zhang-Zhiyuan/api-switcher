"""Synthetic, no-Tk/no-network contracts for runtime route overview publication."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import proxy_route_diagnostics as diagnostics
from ui.dialogs.route_diagnostics_dialog import RouteDiagnosticsDialog, _prepare_report
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab
from ui.widgets.service_route_overview import ServiceRouteOverview, _RuntimeOverview


def synthetic_snapshot(scope="Win11 本机（含共享 WSL）", *, mismatch=False):
    now = datetime.now(timezone.utc)
    prefs = {"builtin_sites": {"youtube": True}, "service_route_modes": {"youtube": "direct"}}
    snapshot = diagnostics.RouteSnapshot(scope, prefs, [], captured_at=now)
    snapshot.saved = diagnostics.saved_rules(prefs)
    snapshot.runtime = {"mode": "direct" if mismatch else "rule", "rules": snapshot.saved,
                        "proxies": {"AI-PROXY": {"all": ["n"], "now": "n"}, "n": {
                            "alive": True, "history": [{"time": now.isoformat(), "delay": 30}]}}}
    snapshot.labels = {"n": "合成日本节点"}
    return snapshot


class Label:
    def __init__(self):
        self.options = {}
        self.visible = False

    def cget(self, key):
        return self.options.get(key)

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **_options):
        self.visible = True

    def pack_forget(self):
        self.visible = False

    def winfo_manager(self):
        return "pack" if self.visible else ""


class RuntimeView(_RuntimeOverview):
    def __init__(self):
        self._init_runtime()
        self._runtime_status = Label()
        self._runtime_preferences_fingerprint = None
        self.labels = {key: Label() for key in ("openai", "claude", "google_ai", "youtube")}
        self.timers = {}
        self.next_timer = 0

    def _runtime_labels(self):
        return self.labels

    def after(self, delay, callback):
        self.next_timer += 1
        self.timers[self.next_timer] = (delay, callback)
        return self.next_timer

    def after_cancel(self, token):
        self.timers.pop(token, None)


def test_summary_compact_mapping_preserves_mismatch_and_direct_semantics():
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    rows = {item.service: item for item in summary.rows}
    assert rows["openai"].current == "合成日本节点"
    assert "探针" not in rows["openai"].current
    assert rows["youtube"].current == "直连（未测试连通性）"
    assert not rows["openai"].warning
    mismatched = diagnostics.snapshot_overview(synthetic_snapshot(mismatch=True))
    row = next(item for item in mismatched.rows if item.service == "openai")
    assert row.warning and "与已保存策略组不同" in row.current
    assert "api_key" not in repr(summary)


def test_missing_runtime_is_not_misrepresented_as_failed_node():
    snapshot = synthetic_snapshot()
    snapshot.runtime = {}
    snapshot.error = "受管代理未运行或无法确认归属"
    summary = diagnostics.snapshot_overview(snapshot)
    assert all("不是节点故障结论" in row.current for row in summary.rows)
    assert all(row.warning for row in summary.rows)


def test_unknown_conditional_rules_never_claim_final_current_node():
    snapshot = synthetic_snapshot()
    snapshot.runtime["rules"] = [{"type": "RULE-SET", "payload": "synthetic", "proxy": "AI-PROXY"}]
    summary = diagnostics.snapshot_overview(snapshot)
    assert all("无法确定" in row.current and row.warning for row in summary.rows)


def test_summary_only_retains_redacted_presentation_not_controller_payload():
    snapshot = synthetic_snapshot()
    snapshot.labels["n"] = "Authorization: Bearer sk-private-synthetic-credential"
    summary = diagnostics.snapshot_overview(snapshot)
    assert "sk-private-synthetic-credential" not in repr(summary)
    assert not hasattr(summary, "runtime") and not hasattr(summary, "preferences")


def test_fingerprint_ignores_unrelated_options_but_not_route_intent():
    fingerprint = diagnostics.routing_preferences_fingerprint
    assert fingerprint({}) == fingerprint({"theme": "dark", "api_key": "synthetic"})
    assert fingerprint({}) != fingerprint({"strict_privacy": True})
    assert fingerprint({}) != fingerprint({"service_node_pools": {"openai": ["a", "b"]}})
    assert fingerprint({"service_node_pools": {"openai": ["a", "b"]}}) != fingerprint(
        {"service_node_pools": {"openai": ["b", "a"]}})


def test_summary_publish_schedules_only_local_expiry_and_marks_history():
    view = RuntimeView()
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    token = view.begin_runtime_read()
    assert view.set_runtime_summary(summary, token)
    assert "内核当前选择（读取时）" in view.labels["openai"].cget("text")
    assert len(view.timers) == 1
    delay, expire = view.timers.pop(view._runtime_after_id)
    assert 0 < delay <= 60001
    expire()
    assert "已过期" in view._runtime_status.cget("text")
    assert "历史内核选择" in view.labels["openai"].cget("text")
    assert not view.timers, "expiry must never fetch or schedule recurring reads"


@pytest.mark.parametrize("seconds", [-5, 60, 120])
def test_old_or_future_snapshot_is_immediately_historical(seconds):
    view = RuntimeView()
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    summary = replace(summary, captured_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
    assert view.set_runtime_summary(summary, view.begin_runtime_read())
    assert "已过期" in view._runtime_status.cget("text")
    assert not view.timers


def test_old_generation_and_mismatched_preferences_are_rejected():
    view = RuntimeView()
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    first, second = view.begin_runtime_read(), view.begin_runtime_read()
    assert not view.set_runtime_summary(summary, first)
    view._runtime_preferences_fingerprint = "changed"
    assert not view.set_runtime_summary(summary, second)
    assert "发生变化" in view._runtime_status.cget("text")
    assert not any(label.visible for label in view.labels.values())


def test_invalidation_cancels_expiry_and_disposal_rejects_late_result():
    view = RuntimeView()
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    view.set_runtime_summary(summary, view.begin_runtime_read())
    view.invalidate_runtime("操作可能已改变运行态")
    assert not view.timers and view._runtime_snapshot is None
    token = view.begin_runtime_read()
    view._dispose_runtime()
    assert not view.set_runtime_summary(summary, token)


def test_route_change_and_catalog_change_invalidate_current_snapshot():
    view = RuntimeView()
    view._signature, view._narrow, view._rows = None, False, {}
    view._summary = Label()
    view._build_row = lambda key: {name: Label() for name in ("target", "state", "profile", "node", "hint", "runtime")}
    view._layout = lambda *_args: None
    view._filter = lambda: None
    snapshot = synthetic_snapshot()
    summary = diagnostics.snapshot_overview(snapshot)
    ServiceRouteOverview.set_routes(view, snapshot.preferences, [])
    view.set_runtime_summary(summary, view.begin_runtime_read())
    generation = view._runtime_generation
    ServiceRouteOverview.set_routes(view, snapshot.preferences, [])
    assert view._runtime_generation == generation and view._runtime_snapshot is summary
    changed = {**snapshot.preferences, "service_route_modes": {"youtube": "default"}}
    ServiceRouteOverview.set_routes(view, changed, [])
    assert view._runtime_snapshot is None
    assert view._runtime_generation > generation


def local_tab(view):
    return SimpleNamespace(_route_overview=view, _destroyed=False, _busy=False)


@pytest.mark.parametrize("case", ["destroyed", "busy", "wrong_scope", "foreign_summary", "missing_token"])
def test_local_tab_rejects_inapplicable_result(case):
    view = RuntimeView()
    tab = local_tab(view)
    summary = diagnostics.snapshot_overview(synthetic_snapshot())
    token = LocalProxyTab._begin_route_runtime_read(tab, None)
    scope = None
    if case == "destroyed":
        tab._destroyed = True
    elif case == "busy":
        tab._busy = True
    elif case == "wrong_scope":
        scope = "ssh-a"
    elif case == "foreign_summary":
        summary = replace(summary, scope="ssh-a")
    else:
        token = None
    LocalProxyTab._accept_route_runtime_summary(tab, scope, summary, token)
    assert view._runtime_snapshot is None


def ssh_tab(view):
    tab = SimpleNamespace(_proxy_runtime_overview=view, _destroyed=False, _proxy_busy=False, _ssh_busy=False,
                          selected=["ssh-a", "ssh-b"], revision=1)
    tab._selected_sync_server_names = lambda: tab.selected
    tab._proxy_latency_source_signature = lambda selected: (tuple(selected), tab.revision)
    return tab


@pytest.mark.parametrize("case", ["selection", "endpoint", "scope", "summary", "destroyed", "busy", "new_read"])
def test_ssh_tab_never_maps_old_or_other_scope_snapshot_into_current_selection(case):
    view, summary = RuntimeView(), diagnostics.snapshot_overview(synthetic_snapshot("ssh-a"))
    tab = ssh_tab(view)
    context = SSHTab._begin_proxy_runtime_read(tab, "ssh-a")
    scope = "ssh-a"
    if case == "selection":
        tab.selected = ["ssh-b"]
    elif case == "endpoint":
        tab.revision += 1
    elif case == "scope":
        scope = "ssh-b"
    elif case == "summary":
        summary = replace(summary, scope="ssh-b")
    elif case == "destroyed":
        tab._destroyed = True
    elif case == "busy":
        tab._proxy_busy = True
    else:
        SSHTab._begin_proxy_runtime_read(tab, "ssh-b")
    SSHTab._accept_proxy_runtime_summary(tab, scope, summary, context)
    assert view._runtime_snapshot is None


def test_ssh_requires_explicit_selected_scope_and_accepts_only_that_result():
    view, tab = RuntimeView(), None
    tab = ssh_tab(view)
    assert SSHTab._begin_proxy_runtime_read(tab, "ssh-c") is None
    assert view._runtime_generation == 0
    context = SSHTab._begin_proxy_runtime_read(tab, "ssh-b")
    summary = diagnostics.snapshot_overview(synthetic_snapshot("ssh-b"))
    SSHTab._accept_proxy_runtime_summary(tab, "ssh-b", summary, context)
    assert view._runtime_snapshot is summary


def test_dialog_only_publishes_explicit_read_once_and_overview_failure_is_nonfatal():
    calls = []
    dialog = SimpleNamespace(_reading=True, _on_loaded=lambda *args: calls.append(args),
                             _scopes={"A": "ssh-a"}, _scope="A", _read_context=42, _status=Label())
    summary = _prepare_report(synthetic_snapshot("ssh-a"), "").overview
    RouteDiagnosticsDialog._publish_overview(dialog, summary)
    RouteDiagnosticsDialog._publish_overview(dialog, summary)
    assert calls == [("ssh-a", summary, 42)]
    def fail(*_args):
        raise RuntimeError("view disappeared")
    dialog._reading, dialog._on_loaded = True, fail
    RouteDiagnosticsDialog._publish_overview(dialog, summary)
    assert "诊断已完成" in dialog._status.cget("text")
