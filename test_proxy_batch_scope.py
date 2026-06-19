from __future__ import annotations

from core import remote_proxy
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab
from ui.widgets.proxy_node_picker import ProxyNodePicker


class _PickerStub:
    def __init__(self, batch_items, checked_items=()):
        self._batch_items = list(batch_items)
        self._checked_items = list(checked_items)

    def batch_items(self):
        return list(self._batch_items)

    def batch_scope_label(self) -> str:
        return f"stub {len(self._batch_items)}"

    def checked_items(self):
        return list(self._checked_items)


class _ValueStub:
    def __init__(self, value: str):
        self.value = value

    def get(self) -> str:
        return self.value


def _node(index: int, name: str) -> remote_proxy.ProxySubscriptionNode:
    return remote_proxy.ProxySubscriptionNode(
        index=index,
        node={
            "name": name,
            "type": "vless",
            "server": f"{name}.example.com",
            "port": 443,
            "uuid": "00000000-0000-0000-0000-000000000000",
        },
    )


def _latency(node: remote_proxy.ProxySubscriptionNode, ok: bool) -> remote_proxy.ProxyNodeLatencyResult:
    return remote_proxy.ProxyNodeLatencyResult(
        node_key=remote_proxy.proxy_node_key(node.node),
        ok=ok,
        latency_ms=80 if ok else None,
        detail="" if ok else "TCP 连接失败",
        attempts=2,
    )


def test_local_quality_candidates_default_to_current_scope_when_nothing_checked():
    first = _node(1, "first")
    second = _node(2, "second")
    third = _node(3, "third")
    tab = object.__new__(LocalProxyTab)
    tab._subscription_picker = _PickerStub([first, second, third])
    tab._subscription_nodes = [first, second, third]
    tab._latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._subscription_batch_nodes() == [first, second, third]
    assert tab._quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_local_quality_candidates_filter_checked_scope_by_connectivity():
    first = _node(1, "first")
    second = _node(2, "second")
    tab = object.__new__(LocalProxyTab)
    tab._subscription_picker = _PickerStub([first, second], checked_items=[first, second])
    tab._subscription_nodes = [first, second]
    tab._latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._quality_candidate_nodes([first, second]) == [first]


def test_ssh_quality_candidates_default_to_current_scope_when_nothing_checked():
    first = _node(1, "first")
    second = _node(2, "second")
    third = _node(3, "third")
    tab = object.__new__(SSHTab)
    tab._proxy_subscription_picker = _PickerStub([first, second, third])
    tab._proxy_subscription_nodes = [first, second, third]
    tab._proxy_latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._proxy_subscription_batch_nodes() == [first, second, third]
    assert tab._proxy_quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_ssh_quality_candidates_filter_checked_scope_by_connectivity():
    first = _node(1, "first")
    second = _node(2, "second")
    tab = object.__new__(SSHTab)
    tab._proxy_subscription_picker = _PickerStub([first, second], checked_items=[first, second])
    tab._proxy_subscription_nodes = [first, second]
    tab._proxy_latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._proxy_quality_candidate_nodes([first, second]) == [first]


def test_proxy_node_picker_reuses_filtered_nodes_until_filter_changes():
    first = _node(1, "first")
    second = _node(2, "second")
    picker = object.__new__(ProxyNodePicker)
    picker._nodes = [first, second]
    picker._latency_results = {}
    picker._quality_results = {}
    picker._node_meta = {}
    picker._summary_counts = {}
    picker._metadata_version = 0
    picker._filter_cache_key = None
    picker._filter_cache_nodes = ()
    picker._search_entry = _ValueStub("")
    picker._filter_combo = _ValueStub("全部")
    picker._region_combo = _ValueStub(ProxyNodePicker.REGION_ALL)
    picker._quality_combo = _ValueStub("全部质量")

    picker._build_node_metadata()
    original_metadata_for = picker._metadata_for
    calls = {"count": 0}

    def counting_metadata_for(item):
        calls["count"] += 1
        return original_metadata_for(item)

    picker._metadata_for = counting_metadata_for

    assert picker._filtered_nodes() == [first, second]
    assert calls["count"] == 2
    assert picker._filtered_nodes() == [first, second]
    assert calls["count"] == 2

    picker._search_entry.value = "second"
    assert picker._filtered_nodes() == [second]
    assert calls["count"] > 2


def test_proxy_node_picker_set_enabled_updates_visible_controls_without_rerender():
    picker = object.__new__(ProxyNodePicker)
    picker._enabled = True
    picker._search_entry = None
    picker._filter_combo = None
    picker._region_combo = None
    picker._quality_combo = None
    picker._filter_reset_button = None
    picker._batch_buttons = []
    calls = {"render": 0, "enabled": []}

    def render_nodes():
        calls["render"] += 1

    def set_visible_rows_enabled(enabled):
        calls["enabled"].append(enabled)

    picker._render_nodes = render_nodes
    picker._set_visible_rows_enabled = set_visible_rows_enabled

    picker.set_enabled(True)
    assert calls["render"] == 0

    picker.set_enabled(False)
    assert picker._enabled is False
    assert calls["render"] == 0
    assert calls["enabled"] == [False]


def test_proxy_node_picker_suspend_cancels_pending_render_work():
    picker = object.__new__(ProxyNodePicker)
    picker._render_after_id = "render"
    picker._render_batch_after_id = "batch"
    picker._render_plan_pending = True
    picker._render_deferred = False
    picker._summary_label = None
    picker._last_match_count = 0
    picker._nodes = []
    picker._summary_counts = {}
    picker._checked_keys = set()
    cancelled = []
    picker.after_cancel = lambda after_id: cancelled.append(after_id)

    ProxyNodePicker._suspend_background_work(picker)

    assert cancelled == ["render", "batch"]
    assert picker._render_after_id is None
    assert picker._render_batch_after_id is None
    assert picker._render_plan_pending is False
    assert picker._render_deferred is True


def test_proxy_node_picker_defers_hidden_render_without_clearing_rows(monkeypatch):
    class _Child:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    class _ListFrame:
        def __init__(self, children):
            self.children = children

        def winfo_children(self):
            return list(self.children)

    child = _Child()
    picker = object.__new__(ProxyNodePicker)
    picker._render_after_id = None
    picker._render_batch_after_id = None
    picker._render_generation = 3
    picker._render_plan_pending = True
    picker._render_deferred = False
    picker._summary_label = None
    picker._list_frame = _ListFrame([child])
    picker._last_match_count = 0
    picker._nodes = []
    picker._summary_counts = {}
    picker._checked_keys = set()

    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: False)

    ProxyNodePicker._render_nodes(picker)

    assert child.destroyed is False
    assert picker._render_generation == 3
    assert picker._render_plan_pending is False
    assert picker._render_deferred is True


def test_proxy_tabs_dispatch_worker_callbacks_through_top_level_queue():
    calls = []
    for tab_class in (LocalProxyTab, SSHTab):
        tab = object.__new__(tab_class)
        tab._destroyed = False
        tab._ui_dispatch = lambda callback, tab_class=tab_class: calls.append(tab_class.__name__) or callback()
        tab.after = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct after should not be used"))

        tab._run_on_ui_thread(lambda tab_class=tab_class: calls.append(f"{tab_class.__name__}:callback"))

    assert calls == [
        "LocalProxyTab",
        "LocalProxyTab:callback",
        "SSHTab",
        "SSHTab:callback",
    ]
