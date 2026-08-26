from __future__ import annotations

import inspect
from types import SimpleNamespace

from core import local_proxy, network_diagnostic_settings, remote_proxy
from ui.tabs.local_proxy_tab import LOCAL_YAML_NODE_ONLY_NOTICE, LocalProxyTab
from ui.tabs.ssh_tab import SSHTab
from ui.theme import COLORS
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

    def set(self, value) -> None:
        self.value = value


class _LabelStub:
    def __init__(self):
        self.values = {}

    def configure(self, **kwargs):
        self.values.update(kwargs)


class _GroupLabelPickerStub:
    def selected_group_label(self) -> str:
        return "香港组 3 个节点"

    def batch_scope_label(self) -> str:
        return "当前筛选 3 个节点"


class _SelectedPickerStub:
    def __init__(self, item):
        self._item = item

    def selected_item(self):
        return self._item


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


class _FailingStartThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        raise RuntimeError("thread start failed")


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


def _verified_stability(**overrides):
    values = {
        "stable": True,
        "short_stable": True,
        "deep_transport_ok": True,
        "deep_transport_successes": local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS,
        "deep_transport_attempts": local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS,
        "codex_compact_ok": True,
        "application_latency_ms": 90,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_local_quality_candidates_default_to_current_scope_when_nothing_checked():
    first = _node(1, "first")
    second = _node(2, "second")
    third = _node(3, "third")
    tab = object.__new__(LocalProxyTab)
    tab._subscription_picker = _PickerStub([first, second, third])
    tab._subscription_nodes = [first, second, third]
    tab._latency_results = {}

    assert tab._subscription_batch_nodes() == [first, second, third]
    assert tab._quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_local_quality_candidates_keep_entire_group_despite_partial_connectivity():
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

    assert tab._quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_local_quality_candidates_do_not_drop_failed_or_unmeasured_nodes():
    first = _node(1, "first")
    second = _node(2, "second")
    tab = object.__new__(LocalProxyTab)
    tab._subscription_picker = _PickerStub([first, second], checked_items=[first, second])
    tab._subscription_nodes = [first, second]
    tab._latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._quality_candidate_nodes([first, second]) == [first, second]


def test_ssh_quality_candidates_default_to_current_scope_when_nothing_checked():
    first = _node(1, "first")
    second = _node(2, "second")
    third = _node(3, "third")
    tab = object.__new__(SSHTab)
    tab._proxy_subscription_picker = _PickerStub([first, second, third])
    tab._proxy_subscription_nodes = [first, second, third]
    tab._proxy_latency_results = {}

    assert tab._proxy_subscription_batch_nodes() == [first, second, third]
    assert tab._proxy_quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_ssh_quality_candidates_keep_entire_group_despite_partial_connectivity():
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

    assert tab._proxy_quality_candidate_nodes([first, second, third]) == [first, second, third]


def test_ssh_quality_candidates_do_not_drop_failed_or_unmeasured_nodes():
    first = _node(1, "first")
    second = _node(2, "second")
    tab = object.__new__(SSHTab)
    tab._proxy_subscription_picker = _PickerStub([first, second], checked_items=[first, second])
    tab._proxy_subscription_nodes = [first, second]
    tab._proxy_latency_results = {
        remote_proxy.proxy_node_key(first.node): _latency(first, True),
        remote_proxy.proxy_node_key(second.node): _latency(second, False),
    }

    assert tab._proxy_quality_candidate_nodes([first, second]) == [first, second]


def test_proxy_tabs_quality_scope_uses_batch_selection_unless_group_is_explicit():
    first = _node(1, "香港-1")
    second = _node(2, "美国-1")
    filtered = [first, second]

    local = object.__new__(LocalProxyTab)
    local._subscription_picker = _PickerStub(filtered)
    local._subscription_nodes = [first, second]
    assert local._subscription_quality_scope() == (filtered, "stub 2")
    assert local._subscription_quality_scope("香港", [first]) == (
        [first],
        "香港组全部 1 个节点",
    )
    ssh = object.__new__(SSHTab)
    ssh._proxy_subscription_picker = _PickerStub(filtered)
    ssh._proxy_subscription_nodes = [first, second]
    assert ssh._proxy_subscription_quality_scope() == (filtered, "stub 2")
    assert ssh._proxy_subscription_quality_scope("美国", [second]) == (
        [second],
        "美国组全部 1 个节点",
    )


def _picker_with_nodes(nodes):
    picker = object.__new__(ProxyNodePicker)
    picker._nodes = list(nodes)
    picker._latency_results = {}
    picker._quality_results = {}
    picker._node_meta = {}
    picker._summary_counts = {}
    picker._metadata_version = 0
    picker._filter_cache_key = None
    picker._filter_cache_nodes = ()
    picker._selected_key = ""
    picker._on_group_quality = None
    picker._build_node_metadata()
    return picker


def test_proxy_node_picker_quality_batch_is_checked_first_then_current_filter():
    first_hong_kong = _node(1, "香港-1")
    united_states = _node(2, "美国-1")
    second_hong_kong = _node(3, "香港-2")
    picker = _picker_with_nodes([first_hong_kong, united_states, second_hong_kong])
    picker._checked_keys = set()
    picker._search_entry = _ValueStub("香港")
    picker._filter_combo = _ValueStub("全部")
    picker._region_combo = _ValueStub(ProxyNodePicker.REGION_ALL)
    picker._quality_combo = _ValueStub("全部质量")

    assert picker.batch_items() == [first_hong_kong, second_hong_kong]
    assert picker.batch_scope_label() == "当前筛选 2 个节点"

    picker._checked_keys.add(remote_proxy.proxy_subscription_node_key(united_states))

    assert picker.batch_items() == [united_states]
    assert picker.batch_scope_label() == "已勾选 1 个节点"

    picker._checked_keys.clear()
    picker._search_entry.value = ""

    assert picker.batch_items() == [first_hong_kong, united_states, second_hong_kong]
    assert picker.batch_scope_label() == "全部 3 个节点"


def test_proxy_node_picker_defaults_to_non_hong_kong_but_allows_manual_hong_kong():
    hong_kong = _node(1, "香港-手动")
    united_states = _node(2, "美国-自动")
    hong_kong_key = remote_proxy.proxy_subscription_node_key(hong_kong)
    united_states_key = remote_proxy.proxy_subscription_node_key(united_states)

    picker = _picker_with_nodes([])
    picker._checked_keys = set()
    picker._visible_node_rows = {}
    picker._update_region_options = lambda: None
    picker._render_nodes = lambda: None
    picker.set_nodes([hong_kong, united_states])

    assert picker.selected_key() == united_states_key

    picker.set_nodes([hong_kong, united_states], selected_key=hong_kong_key)
    assert picker.selected_key() == hong_kong_key

    picker.select_by_key(united_states_key)
    assert picker.select_by_key(hong_kong_key) is True
    assert picker.selected_key() == hong_kong_key

    hong_kong_only = _picker_with_nodes([])
    hong_kong_only._checked_keys = set()
    hong_kong_only._update_region_options = lambda: None
    hong_kong_only._render_nodes = lambda: None
    hong_kong_only.set_nodes([hong_kong])

    assert hong_kong_only.selected_key() == ""


def test_proxy_tabs_fastest_node_helpers_never_return_hong_kong():
    hong_kong = _node(1, "香港-5ms")
    united_states = _node(2, "美国-50ms")
    hong_kong_key = remote_proxy.proxy_subscription_node_key(hong_kong)
    united_states_key = remote_proxy.proxy_subscription_node_key(united_states)
    latencies = {
        hong_kong_key: remote_proxy.ProxyNodeLatencyResult(hong_kong_key, True, latency_ms=5),
        united_states_key: remote_proxy.ProxyNodeLatencyResult(united_states_key, True, latency_ms=50),
    }

    local = object.__new__(LocalProxyTab)
    local._subscription_nodes = [hong_kong, united_states]
    local._latency_results = latencies
    local._quality_results = {}
    assert local._fastest_subscription_node() is united_states
    assert local._fastest_subscription_node([hong_kong]) is None

    ssh = object.__new__(SSHTab)
    ssh._proxy_subscription_nodes = [hong_kong, united_states]
    ssh._proxy_latency_results = latencies
    ssh._proxy_quality_results = {}
    assert ssh._fastest_proxy_subscription_node() is united_states
    assert ssh._fastest_proxy_subscription_node([hong_kong]) is None


def test_proxy_node_picker_group_action_uses_every_node_not_visible_page():
    hong_kong = [_node(index, f"香港-{index}") for index in range(1, 26)]
    other = [_node(100, "美国-1")]
    picker = _picker_with_nodes([*hong_kong, *other])
    emitted = []
    picker._on_group_quality = lambda region, items: emitted.append((region, list(items)))

    assert len(picker.group_items("香港")) == 25
    picker._emit_group_quality("香港")

    assert emitted[0][0] == "香港"
    assert emitted[0][1] == hong_kong


def test_proxy_node_picker_default_render_includes_every_match_without_more(monkeypatch):
    class _ListFrameStub:
        def winfo_children(self):
            return []

    matches = [_node(index, f"香港-{index}") for index in range(1, 26)]
    excluded = _node(100, "美国-已过滤")
    picker = _picker_with_nodes([*matches, excluded])
    picker._render_after_id = None
    picker._render_batch_after_id = None
    picker._render_generation = 0
    picker._render_plan_pending = False
    picker._render_deferred = False
    picker._last_match_count = 0
    picker._last_visible_count = 0
    picker._checked_keys = set()
    picker._summary_label = None
    picker._scope_label = None
    picker._list_frame = _ListFrameStub()
    picker._visible_group_headers = []
    picker._visible_checkboxes = {}
    picker._visible_node_rows = {}
    picker._on_scope_change = None
    picker._filtered_nodes = lambda: list(matches)
    scheduled_plans = []
    build_render_plan = picker._build_render_plan

    def capture_render_plan(items):
        plan = build_render_plan(items)
        scheduled_plans.append(plan)
        return plan

    picker._build_render_plan = capture_render_plan
    picker.after = lambda _delay, _callback: "render-batch"
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)

    picker._render_nodes()

    assert picker._last_match_count == len(matches)
    assert picker._last_visible_count == len(matches)
    assert len(scheduled_plans) == 1
    plan = scheduled_plans[0]
    assert [payload for kind, payload, _extra in plan if kind == "row"] == matches
    assert {kind for kind, _payload, _extra in plan} <= {"header", "row"}
    assert not hasattr(ProxyNodePicker, "_render_more_footer")
    assert not hasattr(ProxyNodePicker, "_show_more_nodes")
    assert "显示更多" not in inspect.getsource(ProxyNodePicker)


def test_proxy_subscription_ui_has_no_pagination_or_show_more_source():
    picker_source = inspect.getsource(ProxyNodePicker)
    local_source = inspect.getsource(LocalProxyTab)
    ssh_source = inspect.getsource(SSHTab)
    forbidden_markers = (
        "MAX_VISIBLE_ROWS",
        "VISIBLE_ROWS_STEP",
        "_visible_limit",
        "_render_more_footer",
        "_show_more_nodes",
        "显示更多",
    )

    for marker in forbidden_markers:
        assert marker not in picker_source
    for source in (picker_source, local_source, ssh_source):
        assert "显示更多" not in source
    assert 'text="刷新并热更新"' in local_source
    assert "command=self._run_manual_hot_update" in local_source
    assert 'text="刷新并热更新"' in ssh_source
    assert "command=self._run_proxy_manual_hot_update" in ssh_source


def test_proxy_node_picker_coalesces_non_contiguous_region_fragments():
    first_hk = _node(1, "香港-高质")
    united_states = _node(2, "美国-中等")
    second_hk = _node(3, "香港-未测")
    picker = _picker_with_nodes([first_hk, united_states, second_hk])

    groups = picker._group_visible_nodes([first_hk, united_states, second_hk])

    assert groups == [("香港", [first_hk, second_hk]), ("美国", [united_states])]


def test_proxy_node_picker_explicit_region_filter_defines_current_quality_group():
    hong_kong = _node(1, "香港-1")
    united_states = _node(2, "美国-1")
    picker = _picker_with_nodes([hong_kong, united_states])
    picker._selected_key = remote_proxy.proxy_subscription_node_key(united_states)
    picker._region_combo = _ValueStub("香港")

    assert picker.selected_group_items() == [hong_kong]
    assert picker.selected_group_label() == "香港组 1 个节点"


def test_local_and_ssh_group_quality_detection_have_no_implicit_proxy_reload():
    local_source = inspect.getsource(LocalProxyTab._measure_subscription_qualities)
    ssh_source = inspect.getsource(SSHTab._measure_proxy_subscription_qualities)

    assert "reload_local_ai_proxy_verified" not in local_source
    assert "_use_selected_subscription_node" not in local_source
    assert "reload_ai_proxy_verified" not in ssh_source
    assert "_use_selected_proxy_subscription_node" not in ssh_source
    for source in (local_source, ssh_source):
        assert "completed_results" in source
        assert "proxy_node_quality_cancelled" in source
        assert "proxy_node_quality_measured(value)" in source
        assert "merge_proxy_quality_refresh_results" in source
        assert "selection_results" in source
        assert "for key in completed_results" in source
        assert "merge_proxy_subscription_qualities(\n                        persisted_results" in source


def test_local_current_node_hot_update_uses_verified_reload_without_fetch_or_start(monkeypatch):
    first = _node(1, "香港-1")
    second = _node(2, "美国-2")
    profile_id = "profile-local-current"
    quality_results = {"quality": {"score": 88}}
    reload_calls = []
    calls = {"lock": [], "busy": []}
    statuses = []
    toasts = []

    def reload_verified(proxy_text, candidate_nodes, **kwargs):
        reload_calls.append((proxy_text, candidate_nodes, kwargs))
        return "本机 AI 代理已热更新节点；验证通过"

    def unexpected(*_args, **_kwargs):
        raise AssertionError("current-node hot update must not fetch or install")

    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy_verified", reload_verified)
    monkeypatch.setattr(local_proxy, "install_local_ai_proxy_verified", unexpected)
    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", unexpected)
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {
            "active_profile_id": profile_id,
            "profiles": {profile_id: {"id": profile_id}},
        },
    )
    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: calls["lock"].append("acquire") or True,
    )
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: calls["lock"].append("release"),
    )
    monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: "")
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    tab = object.__new__(LocalProxyTab)
    tab._periodic_update_running = False
    tab._busy = False
    tab._subscription_picker = _SelectedPickerStub(first)
    tab._subscription_nodes = [first, second]
    tab._quality_results = quality_results
    tab._subscription_profile_combo = _ValueStub("current")
    tab._subscription_profile_options = {"current": profile_id}
    tab._saved_subscription_load_generation = 7
    tab._set_busy = lambda value: calls["busy"].append(value)
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._hot_update_selected_subscription_node()

    assert reload_calls == [
        (
            remote_proxy.format_proxy_node(first.node),
            (first, second),
            {"quality_results": quality_results, "profile_id": profile_id},
        )
    ]
    assert calls["lock"] == ["acquire", "release"]
    assert calls["busy"] == [True, False]
    assert "不会自动启动或部署" in statuses[0][0]
    assert statuses[-1] == ("本机 AI 代理已热更新节点；验证通过", "success")
    assert toasts[-1][1]["is_error"] is False


def test_local_current_node_hot_update_does_not_overlap_periodic_update(monkeypatch):
    statuses = []
    toasts = []
    tab = object.__new__(LocalProxyTab)
    tab._periodic_update_running = True
    tab._subscription_picker = _SelectedPickerStub(_node(1, "香港-1"))
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab.winfo_toplevel = lambda: object()
    tab._run_local_task = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("periodic and manual updates must not overlap")
    )
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    tab._hot_update_selected_subscription_node()

    assert statuses[-1][1] == "warning"
    assert "定时热更新正在进行" in statuses[-1][0]
    assert toasts[-1][1]["is_error"] is True


def test_local_proxy_tab_imports_local_yaml_into_subscription_picker(monkeypatch, tmp_path):
    path = tmp_path / "fallback.yaml"
    path.write_text("proxies: []\n", encoding="utf-8")
    imported = _node(1, "美国-本地")
    result = SimpleNamespace(nodes=(imported,))
    state = {
        "active_profile_id": "file-profile",
        "profiles": {"file-profile": {"id": "file-profile", "source_path": str(path)}},
        "saved_path": str(tmp_path / "managed.yaml"),
        "source_path": str(path),
        "selected_node_key": "",
    }
    calls = {"busy": [], "cache": [], "status": [], "nodes": [], "release": 0, "use": 0}

    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.filedialog.askopenfilename",
        lambda **_kwargs: str(path),
    )
    monkeypatch.setattr(
        remote_proxy,
        "import_proxy_subscription_file",
        lambda selected_path, **kwargs: (
            result
            if selected_path == str(path) and kwargs == {"activate": True}
            else (_ for _ in ()).throw(AssertionError("unexpected import arguments"))
        ),
    )
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_latencies", lambda _state=None: {})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _state=None: {})
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: calls.__setitem__("release", calls["release"] + 1),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._periodic_update_running = False
    tab._saved_subscription_load_generation = 0
    tab._begin_subscription_profile_mutation = lambda _action: (True, True)
    tab._set_busy = lambda value: calls["busy"].append(value)
    tab._set_cache_status = lambda message, severity="info": calls["cache"].append((message, severity))
    tab._set_status = lambda message, severity="info": calls["status"].append((message, severity))
    tab._refresh_subscription_profile_options = lambda loaded_state: calls.setdefault("state", loaded_state)
    tab._apply_subscription_profile_inputs = lambda _state: None
    tab._set_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._select_subscription_node_by_key = lambda _key: False
    tab._use_selected_subscription_node = lambda **_kwargs: calls.__setitem__("use", calls["use"] + 1)
    tab._subscription_picker = _SelectedPickerStub(imported)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._import_subscription_yaml()

    assert calls["release"] == 1
    assert calls["busy"] == [True, False]
    assert calls["nodes"] == [((imported,), "")]
    assert calls["use"] == 1
    assert "导入 1 个节点" in calls["cache"][-1][0]
    assert calls["cache"][-1][1] == "success"
    assert calls["status"][-1][1] == "success"


def test_local_proxy_tab_restores_local_file_profile_with_empty_url(monkeypatch, tmp_path):
    cached = SimpleNamespace(nodes=(_node(1, "美国-缓存"),))
    state = {
        "url": "",
        "source_path": str(tmp_path / "fallback.yaml"),
        "saved_path": str(tmp_path / "managed.yaml"),
        "last_fetched_at": "2026-08-13T00:00:00+00:00",
        "selected_node_key": "selected",
    }
    calls = {"nodes": [], "cache": [], "status": []}
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda snapshot: cached)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_latencies", lambda snapshot: {})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda snapshot: {})
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)

    tab = object.__new__(LocalProxyTab)
    tab._latency_results = {}
    tab._quality_results = {}
    tab._prefer_quality_sort = False
    tab._saved_subscription_load_generation = 3
    tab._set_cache_status = lambda message, severity="info": calls["cache"].append((message, severity))
    tab._set_status = lambda message, severity="info": calls["status"].append((message, severity))
    tab._set_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._select_subscription_node_by_key = lambda _key: True
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True

    tab._load_subscription_cache_for_state(state, 3)

    assert calls["nodes"] == [(cached.nodes, "selected")]
    assert "本地 YAML" in calls["cache"][-1][0]
    assert calls["cache"][-1][1] == "success"


def test_ssh_proxy_tab_can_use_win11_imported_yaml_cache(monkeypatch, tmp_path):
    cached = SimpleNamespace(nodes=(_node(1, "美国-本地后备"),))
    state = {
        "url": "",
        "source_path": str(tmp_path / "fallback.yaml"),
        "saved_path": str(tmp_path / "managed.yaml"),
        "last_fetched_at": "2026-08-13T00:00:00+00:00",
        "selected_node_key": "selected",
    }
    calls = {"nodes": [], "cache": [], "status": [], "use": 0}
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda snapshot: cached)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda snapshot: {})
    monkeypatch.setattr("ui.tabs.ssh_tab.threading.Thread", _ImmediateThread)

    tab = object.__new__(SSHTab)
    tab._proxy_latency_results = {}
    tab._proxy_latency_server_count = 0
    tab._proxy_quality_results = {}
    tab._proxy_prefer_quality_sort = False
    tab._proxy_saved_subscription_load_generation = 4
    tab._set_proxy_cache_status = lambda message, severity="info": calls["cache"].append(
        (message, severity)
    )
    tab._set_proxy_status = lambda message, severity="info": calls["status"].append(
        (message, severity)
    )
    tab._set_proxy_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._select_proxy_subscription_node_by_key = lambda _key: True
    tab._use_selected_proxy_subscription_node = lambda **_kwargs: calls.__setitem__(
        "use", calls["use"] + 1
    )
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True

    tab._load_proxy_subscription_cache_for_state(state, 4)

    assert calls["nodes"] == [(cached.nodes, "selected")]
    assert calls["use"] == 1
    assert "本地 YAML" in calls["cache"][-1][0]
    assert calls["cache"][-1][1] == "success"


def test_ssh_saved_url_without_cache_still_schedules_startup_refresh(monkeypatch):
    state = {
        "active_profile_id": "new-profile",
        "url": "https://example.com/sub",
        "saved_path": "",
        "ssh_periodic_update_enabled": False,
        "ssh_periodic_update_interval_minutes": 60,
    }
    calls = {"startup": 0, "periodic": 0}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(remote_proxy, "proxy_subscription_auto_refresh_enabled", lambda _scope: True)
    monkeypatch.setattr("ui.tabs.ssh_tab.is_active_tab", lambda _tab: True)

    tab = object.__new__(SSHTab)
    tab._proxy_saved_subscription_after_id = None
    tab._deferred_proxy_saved_subscription_pending = False
    tab._proxy_saved_subscription_loaded = False
    tab._proxy_saved_subscription_load_generation = 0
    tab._proxy_periodic_update_entry = None
    tab._proxy_auto_refresh_var = SimpleNamespace(set=lambda _value: None)
    tab._proxy_periodic_update_var = SimpleNamespace(set=lambda _value: None)
    tab._refresh_proxy_subscription_profile_options = lambda _state: None
    tab._apply_proxy_subscription_profile_inputs = lambda _state: None
    tab._schedule_proxy_startup_refresh = lambda: calls.__setitem__(
        "startup", calls["startup"] + 1
    )
    tab._schedule_proxy_periodic_update = lambda **_kwargs: calls.__setitem__(
        "periodic", calls["periodic"] + 1
    )

    tab._load_saved_proxy_subscription_ui()

    assert calls == {"startup": 1, "periodic": 1}


def test_ssh_delete_profile_restores_next_local_yaml_cache(monkeypatch):
    state = {
        "active_profile_id": "local-file",
        "url": "",
        "source_path": "fallback.yaml",
        "saved_path": "managed.yaml",
    }
    calls = {"loaded": [], "release": 0}
    monkeypatch.setattr(remote_proxy, "try_acquire_proxy_subscription_hot_update", lambda: True)
    monkeypatch.setattr(remote_proxy, "delete_proxy_subscription_profile", lambda _profile_id: {})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: calls.__setitem__("release", calls["release"] + 1),
    )
    monkeypatch.setattr(
        "ui.tabs.ssh_tab.ConfirmDialog",
        lambda *_args, **kwargs: kwargs["on_confirm"](),
    )
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_subscription_profile_combo = _ValueStub("old")
    tab._proxy_subscription_profile_options = {"old": "old-profile"}
    tab._proxy_saved_subscription_load_generation = 2
    tab._refresh_proxy_subscription_profile_options = lambda _state: None
    tab._apply_proxy_subscription_profile_inputs = lambda _state: None
    tab._set_proxy_subscription_nodes = lambda _nodes: None
    tab._set_proxy_status = lambda *_args, **_kwargs: None
    tab._load_proxy_subscription_cache_for_state = lambda snapshot, generation: calls[
        "loaded"
    ].append((snapshot, generation))
    tab.winfo_toplevel = lambda: object()

    tab._delete_proxy_subscription_profile()

    assert calls["release"] == 1
    assert calls["loaded"] == [(state, 3)]


def test_local_manual_subscription_fetch_failure_keeps_existing_nodes(monkeypatch):
    old_node = _node(1, "美国-旧缓存")
    old_key = remote_proxy.proxy_subscription_node_key(old_node)
    state = {
        "active_profile_id": "offline-profile",
        "profiles": {"offline-profile": {"id": "offline-profile"}},
    }
    calls = {"busy": [], "cache": [], "status": [], "toasts": [], "fetch": []}

    def fetch(url, **kwargs):
        calls["fetch"].append((url, kwargs))
        raise OSError("subscription offline")

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda _state: None)
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_subscription_direct_fallback_allowed",
        lambda: False,
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.show_toast",
        lambda _top, message, **kwargs: calls["toasts"].append((message, kwargs)),
    )

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._periodic_update_running = False
    # The UI mirror can still be at its default while persisted strict privacy
    # is already enabled.  The worker must use the authoritative preference.
    tab._strict_privacy_var = _ValueStub(False)
    tab._saved_subscription_load_generation = 0
    tab._subscription_options = {old_key: old_node}
    tab._subscription_url_input = lambda: "https://offline.example/sub"
    tab._save_subscription_profile = lambda show_message=False: {"id": "offline-profile"}
    tab._set_busy = lambda value: calls["busy"].append(value)
    tab._set_cache_status = lambda message, severity="info": calls["cache"].append((message, severity))
    tab._set_status = lambda message, severity="info": calls["status"].append((message, severity))
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._fetch_subscription()

    assert tab._subscription_options == {old_key: old_node}
    assert calls["busy"] == [True, False]
    assert "继续使用已有节点" in calls["cache"][-1][0]
    assert "已保留本机缓存" in calls["status"][-1][0]
    assert calls["status"][-1][1] == "warning"
    assert calls["toasts"][-1][1]["is_error"] is True
    assert calls["fetch"] == [
        (
            "https://offline.example/sub",
            {
                    "profile_id": "offline-profile",
                    "activate": False,
                    "allow_direct_fallback": False,
                    "recovery_proxy_provider": (
                        local_proxy.local_proxy_subscription_recovery_session
                    ),
            },
        )
    ]


def test_local_strict_privacy_toggle_confirms_persists_and_hot_applies(monkeypatch):
    calls = {"transactions": [], "tasks": [], "dialogs": [], "reloads": 0}
    monkeypatch.setattr(
        local_proxy,
        "set_local_proxy_strict_privacy_and_apply",
        lambda enabled: calls["transactions"].append(enabled) or "applied",
    )

    def confirm_dialog(*_args, **kwargs):
        calls["dialogs"].append(kwargs)
        kwargs["on_confirm"]()

    monkeypatch.setattr("ui.tabs.local_proxy_tab.ConfirmDialog", confirm_dialog)

    tab = object.__new__(LocalProxyTab)
    tab._strict_privacy_var = _ValueStub(True)
    tab._load_proxy_preferences_ui = lambda: calls.__setitem__("reloads", calls["reloads"] + 1)

    def run_task(message, worker, prefix, **kwargs):
        calls["tasks"].append((message, prefix))
        result = worker()
        kwargs["on_success"](result)

    tab._run_local_task = run_task
    tab.winfo_toplevel = lambda: object()

    tab._on_strict_privacy_toggle()

    assert calls["transactions"] == [True]
    assert tab._strict_privacy_var.get() is True
    assert "不是 VPN/TUN" in calls["dialogs"][0]["message"]
    assert "事务化更新" in calls["tasks"][0][0]
    assert calls["reloads"] == 1


def test_local_strict_privacy_toggle_restores_visible_state_after_transaction_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        local_proxy,
        "set_local_proxy_strict_privacy_and_apply",
        lambda _enabled: (_ for _ in ()).throw(RuntimeError("reload failed")),
    )

    def confirm_dialog(*_args, **kwargs):
        kwargs["on_confirm"]()

    monkeypatch.setattr("ui.tabs.local_proxy_tab.ConfirmDialog", confirm_dialog)

    tab = object.__new__(LocalProxyTab)
    tab._strict_privacy_var = _ValueStub(True)
    reloads = []
    tab._load_proxy_preferences_ui = lambda: reloads.append(True)

    def run_task(_message, worker, _prefix, **kwargs):
        try:
            worker()
        except Exception as exc:
            kwargs["on_error"](str(exc))

    tab._run_local_task = run_task
    tab.winfo_toplevel = lambda: object()

    tab._on_strict_privacy_toggle()

    assert tab._strict_privacy_var.get() is False
    assert reloads == [True]


def test_local_proxy_privacy_ui_states_application_layer_boundary_and_yaml_scope():
    source = inspect.getsource(LocalProxyTab)

    assert "严格隐私（应用层" in source
    assert "不是 VPN/TUN" in source
    assert "WebRTC/UDP" in source
    assert "系统 DNS/IPv6 绕过" in source
    assert "allow_direct_fallback=allow_direct_fallback" in source
    assert LOCAL_YAML_NODE_ONLY_NOTICE == "本地 YAML 只导入 proxies 节点，不继承顶层 dns/tun。"


def test_local_subscription_fetch_failure_restores_target_cache_when_ui_is_empty(monkeypatch):
    cached_node = _node(1, "美国-磁盘缓存")
    profile_state = {
        "id": "offline-profile",
        "url": "https://offline.example/sub",
        "saved_path": "managed.yaml",
        "selected_node_key": "",
    }
    state = {
        "active_profile_id": "offline-profile",
        "profiles": {"offline-profile": profile_state},
        **profile_state,
    }
    cached = SimpleNamespace(nodes=(cached_node,))
    calls = {"nodes": [], "used": 0, "cache": []}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_subscription_direct_fallback_allowed",
        lambda: False,
    )
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda snapshot: cached)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_latencies", lambda _state=None: {})
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _state=None: {})
    monkeypatch.setattr(
        remote_proxy,
        "fetch_proxy_subscription",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("subscription offline")),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._periodic_update_running = False
    tab._saved_subscription_load_generation = 0
    tab._subscription_options = {}
    tab._subscription_url_input = lambda: profile_state["url"]
    tab._save_subscription_profile = lambda show_message=False: profile_state
    tab._set_busy = lambda _value: None
    tab._set_cache_status = lambda message, severity="info": calls["cache"].append((message, severity))
    tab._set_status = lambda *_args, **_kwargs: None

    def set_nodes(nodes, preserve_key=""):
        calls["nodes"].append((tuple(nodes), preserve_key))
        tab._subscription_options = {
            remote_proxy.proxy_subscription_node_key(item): item for item in nodes
        }

    tab._set_subscription_nodes = set_nodes
    tab._select_subscription_node_by_key = lambda _key: False
    tab._use_selected_subscription_node = lambda **_kwargs: calls.__setitem__("used", calls["used"] + 1)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._fetch_subscription()

    assert calls["nodes"] == [((cached_node,), "")]
    assert calls["used"] == 1
    assert "继续使用已有节点" in calls["cache"][-1][0]


def test_ssh_manual_fetch_uses_persisted_strict_policy_without_overwriting_active_profile(
    monkeypatch,
):
    target_node = _node(1, "美国-旧分组")
    target_profile = {
        "id": "target-profile",
        "url": "https://target.example/sub",
        "saved_path": "target.yaml",
    }
    current_state = {
        "active_profile_id": "current-profile",
        "profiles": {
            "target-profile": target_profile,
            "current-profile": {
                "id": "current-profile",
                "url": "https://current.example/sub",
                "saved_path": "current.yaml",
            },
        },
        "url": "https://current.example/sub",
        "saved_path": "current.yaml",
    }
    state = {
        "value": {
            "active_profile_id": "target-profile",
            "profiles": {"target-profile": target_profile},
            **target_profile,
        }
    }
    calls = {"nodes": [], "loaded": [], "use": 0, "refreshed": [], "fetch": []}

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state["value"])
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_subscription_direct_fallback_allowed",
        lambda: False,
    )

    def fetch(url, **kwargs):
        calls["fetch"].append((url, kwargs))
        state["value"] = current_state
        return SimpleNamespace(
            nodes=(target_node,),
            saved_path="target.yaml",
            last_fetched_at="now",
        )

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    monkeypatch.setattr("ui.tabs.ssh_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_periodic_update_running = False
    tab._proxy_saved_subscription_load_generation = 0
    tab._proxy_subscription_options = {}
    tab._proxy_subscription_url_input = lambda: target_profile["url"]
    tab._save_proxy_subscription_profile = lambda show_message=False: target_profile
    tab._set_proxy_busy = lambda _value: None
    tab._set_proxy_cache_status = lambda *_args, **_kwargs: None
    tab._set_proxy_status = lambda *_args, **_kwargs: None
    tab._refresh_proxy_subscription_profile_options = lambda snapshot: calls["refreshed"].append(snapshot)
    tab._apply_proxy_subscription_profile_inputs = lambda _snapshot: None
    tab._set_proxy_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._use_selected_proxy_subscription_node = lambda **_kwargs: calls.__setitem__(
        "use", calls["use"] + 1
    )
    tab._load_proxy_subscription_cache_for_state = lambda snapshot, generation: calls[
        "loaded"
    ].append((snapshot, generation))
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._fetch_proxy_subscription()

    assert calls["nodes"] == [((), "")]
    assert calls["use"] == 0
    assert calls["refreshed"] == [current_state]
    assert calls["loaded"] == [(current_state, 1)]
    assert calls["fetch"] == [
        (
            "https://target.example/sub",
            {
                    "profile_id": "target-profile",
                    "activate": False,
                    "allow_direct_fallback": False,
                    "recovery_proxy_provider": (
                        local_proxy.local_proxy_subscription_recovery_session
                    ),
            },
        )
    ]


def test_ssh_subscription_fetch_failure_restores_target_cache_when_ui_is_empty(monkeypatch):
    cached_node = _node(1, "美国-SSH磁盘缓存")
    target_profile = {
        "id": "ssh-offline-profile",
        "url": "https://offline.example/sub",
        "saved_path": "managed.yaml",
        "selected_node_key": "",
    }
    state = {
        "active_profile_id": "ssh-offline-profile",
        "profiles": {"ssh-offline-profile": target_profile},
        **target_profile,
    }
    cached = SimpleNamespace(nodes=(cached_node,))
    calls = {"nodes": [], "used": [], "cache": []}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda snapshot: cached)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _state=None: {})
    monkeypatch.setattr(
        remote_proxy,
        "fetch_proxy_subscription",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("subscription offline")),
    )
    monkeypatch.setattr("ui.tabs.ssh_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_periodic_update_running = False
    tab._proxy_saved_subscription_load_generation = 0
    tab._proxy_subscription_options = {}
    tab._proxy_subscription_url_input = lambda: target_profile["url"]
    tab._save_proxy_subscription_profile = lambda show_message=False: target_profile
    tab._set_proxy_busy = lambda _value: None
    tab._set_proxy_cache_status = lambda message, severity="info": calls["cache"].append(
        (message, severity)
    )
    tab._set_proxy_status = lambda *_args, **_kwargs: None

    def set_nodes(nodes, preserve_key=""):
        calls["nodes"].append((tuple(nodes), preserve_key))
        tab._proxy_subscription_options = {
            remote_proxy.proxy_subscription_node_key(item): item for item in nodes
        }

    tab._set_proxy_subscription_nodes = set_nodes
    tab._select_proxy_subscription_node_by_key = lambda _key: False
    tab._use_selected_proxy_subscription_node = lambda **kwargs: calls["used"].append(kwargs)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._fetch_proxy_subscription()

    assert calls["nodes"] == [((cached_node,), "")]
    assert calls["used"] == [{"show_message": False, "persist_selection": False}]
    assert "继续使用已有节点" in calls["cache"][-1][0]


def test_ssh_selected_subscription_node_persists_to_captured_profile(monkeypatch):
    node = _node(1, "美国-目标分组")
    calls = []
    monkeypatch.setattr(
        remote_proxy,
        "set_proxy_subscription_selected_node",
        lambda selected, *, profile_id="": calls.append((selected, profile_id)),
    )

    tab = object.__new__(SSHTab)
    tab._proxy_subscription_picker = _SelectedPickerStub(node)
    tab._proxy_node_text = None
    tab._set_proxy_selected_summary = lambda *_args, **_kwargs: None
    tab._set_proxy_status = lambda *_args, **_kwargs: None

    tab._use_selected_proxy_subscription_node(
        show_message=False,
        profile_id="captured-profile",
    )

    assert calls == [(node.node, "captured-profile")]


def test_local_saved_url_without_cache_still_schedules_startup_refresh(monkeypatch):
    state = {
        "active_profile_id": "new-profile",
        "profiles": {
            "new-profile": {
                "id": "new-profile",
                "url": "https://example.com/sub",
                "saved_path": "",
            }
        },
        "url": "https://example.com/sub",
        "saved_path": "",
        "local_periodic_update_enabled": False,
        "local_periodic_update_interval_minutes": 60,
    }
    calls = {"startup": 0, "periodic": 0}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(remote_proxy, "proxy_subscription_auto_refresh_enabled", lambda _scope: True)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.is_active_tab", lambda _tab: True)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)

    tab = object.__new__(LocalProxyTab)
    tab._saved_subscription_after_id = None
    tab._deferred_saved_subscription_pending = False
    tab._saved_subscription_loaded = False
    tab._saved_subscription_load_generation = 0
    tab._periodic_update_entry = None
    tab._auto_refresh_var = SimpleNamespace(set=lambda _value: None)
    tab._periodic_update_var = SimpleNamespace(set=lambda _value: None)
    tab._set_cache_status = lambda *_args, **_kwargs: None
    tab._set_status = lambda *_args, **_kwargs: None
    tab._refresh_subscription_profile_options = lambda _state: None
    tab._apply_subscription_profile_inputs = lambda _state: None
    tab._schedule_startup_refresh = lambda: calls.__setitem__("startup", calls["startup"] + 1)
    tab._schedule_periodic_update = lambda **_kwargs: calls.__setitem__("periodic", calls["periodic"] + 1)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True

    tab._load_saved_subscription_ui()

    assert calls == {"startup": 1, "periodic": 1}


def test_ssh_current_node_hot_update_batches_selected_targets(monkeypatch):
    first = _node(1, "香港-1")
    second = _node(2, "美国-2")
    profile_id = "profile-ssh-current"
    quality_results = {"quality": {"score": 88}}
    reload_calls = []
    lock_calls = []
    statuses = []
    toasts = []

    def reload_verified(server_name, proxy_text, candidate_nodes, **kwargs):
        reload_calls.append((server_name, proxy_text, candidate_nodes, kwargs))
        if server_name == "alpha":
            return "AI 代理已无重启切换到香港-1；验证通过"
        return "AI 代理未运行，已跳过热更新"

    def unexpected(*_args, **_kwargs):
        raise AssertionError("current-node hot update must not fetch or deploy")

    monkeypatch.setattr(remote_proxy, "reload_ai_proxy_verified", reload_verified)
    monkeypatch.setattr(remote_proxy, "install_ai_proxy", unexpected)
    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", unexpected)
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {
            "active_profile_id": profile_id,
            "profiles": {profile_id: {"id": profile_id}},
        },
    )
    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: lock_calls.append("acquire") or True,
    )
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: lock_calls.append("release"),
    )
    monkeypatch.setattr(
        "ui.tabs.ssh_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    tab = object.__new__(SSHTab)
    tab._proxy_periodic_update_running = False
    tab._proxy_busy = False
    tab._ssh_busy = False
    tab._proxy_subscription_picker = _SelectedPickerStub(first)
    tab._proxy_subscription_nodes = [first, second]
    tab._proxy_quality_results = quality_results
    tab._proxy_subscription_profile_combo = _ValueStub("current")
    tab._proxy_subscription_profile_options = {"current": profile_id}
    tab._require_selected_servers = lambda status_setter=None: ["alpha", "beta"]
    tab._format_server_target = lambda names: f"{len(names)} targets"
    tab._set_proxy_status = lambda message, severity="info": statuses.append((message, severity))
    tab.winfo_toplevel = lambda: object()
    tab._run_server_batch = lambda names, action: SSHTab._run_server_batch(tab, names, action)

    def run_proxy_task(_message, worker, on_done=None):
        result = worker()
        on_done({"ok": True, "result": result, "error": None})
        return True

    tab._run_proxy_ssh_task = run_proxy_task

    tab._hot_update_selected_proxy_subscription_node()

    expected_proxy = remote_proxy.format_proxy_node(first.node)
    assert reload_calls == [
        (
            "alpha",
            expected_proxy,
            (first, second),
            {
                "quality_results": quality_results,
                "persist_selection": False,
                "profile_id": profile_id,
            },
        ),
        (
            "beta",
            expected_proxy,
            (first, second),
            {
                "quality_results": quality_results,
                "persist_selection": False,
                "profile_id": profile_id,
            },
        ),
    ]
    assert lock_calls == ["acquire", "release"]


def test_ssh_current_node_hot_update_passes_explicit_strict_privacy(monkeypatch):
    node = _node(1, "美国-1")
    profile_id = "profile-ssh-strict"
    captured = []
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {
            "active_profile_id": profile_id,
            "profiles": {profile_id: {"id": profile_id}},
        },
    )
    monkeypatch.setattr(remote_proxy, "try_acquire_proxy_subscription_hot_update", lambda: True)
    monkeypatch.setattr(remote_proxy, "release_proxy_subscription_hot_update", lambda: None)
    monkeypatch.setattr(
        remote_proxy,
        "reload_ai_proxy_verified",
        lambda *_args, **kwargs: captured.append(kwargs) or "验证通过",
    )
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(SSHTab)
    tab._proxy_periodic_update_running = False
    tab._proxy_busy = False
    tab._ssh_busy = False
    tab._proxy_subscription_picker = _SelectedPickerStub(node)
    tab._proxy_subscription_nodes = [node]
    tab._proxy_quality_results = {}
    tab._proxy_subscription_profile_combo = _ValueStub("current")
    tab._proxy_subscription_profile_options = {"current": profile_id}
    tab._proxy_strict_privacy_var = _ValueStub(True)
    tab._proxy_strict_privacy_explicit = True
    tab._require_selected_servers = lambda status_setter=None: ["alpha"]
    tab._format_server_target = lambda names: "alpha"
    tab._set_proxy_status = lambda *_args, **_kwargs: None
    tab.winfo_toplevel = lambda: object()
    tab._run_server_batch = lambda names, action: SSHTab._run_server_batch(tab, names, action)

    def run_task(_message, worker, on_done=None):
        result = worker()
        on_done({"ok": True, "result": result, "error": None})
        return True

    tab._run_proxy_ssh_task = run_task

    tab._hot_update_selected_proxy_subscription_node()

    assert captured[0]["strict_privacy"] is True
    assert SSHTab._proxy_hot_update_result_successful("验证通过") is True
    assert SSHTab._proxy_hot_update_result_successful("未运行，已跳过") is False
    assert (
        SSHTab._proxy_hot_update_result_successful(
            "alpha: 新节点验证失败；已恢复更新前节点 old；验证通过"
        )
        is False
    )
    assert (
        SSHTab._proxy_hot_update_result_successful(
            "alpha: 原热更新节点验证失败，已无重启切换到 fallback；验证通过"
        )
        is True
    )


def test_manual_and_periodic_hot_updates_share_the_same_orchestrator():
    cases = (
        (
            LocalProxyTab,
            "_run_manual_hot_update",
            "_run_periodic_update",
            "_start_subscription_hot_update",
            "_periodic_update_after_id",
            "_periodic_update_var",
        ),
        (
            SSHTab,
            "_run_proxy_manual_hot_update",
            "_run_proxy_periodic_update",
            "_start_proxy_subscription_hot_update",
            "_proxy_periodic_update_after_id",
            "_proxy_periodic_update_var",
        ),
    )

    for tab_class, manual_name, periodic_name, start_name, after_name, enabled_name in cases:
        tab = object.__new__(tab_class)
        calls = []
        setattr(tab, after_name, "scheduled-callback")
        setattr(tab, enabled_name, _ValueStub(True))
        setattr(tab, start_name, lambda *, manual, calls=calls: calls.append(manual))

        getattr(tab, manual_name)()
        getattr(tab, periodic_name)()

        assert calls == [True, False]
        assert getattr(tab, after_name) is None


def test_local_refresh_and_hot_update_fetches_then_applies_running_proxy(monkeypatch):
    first = _node(1, "香港-1")
    second = _node(2, "美国-2")
    profile_id = "profile-local"
    selected_key = remote_proxy.proxy_subscription_node_key(first)
    quality_results = {selected_key: {"quality_score": 90}}
    state = {
        "active_profile_id": profile_id,
        "profiles": {
            profile_id: {
                "id": profile_id,
                "selected_node_key": selected_key,
                "node_latencies": {},
                "node_qualities": quality_results,
            }
        },
    }
    calls = {
        "fetch": [],
        "apply": [],
        "release": 0,
        "busy": [],
        "nodes": [],
        "schedule": 0,
        "order": [],
    }
    statuses = []
    toasts = []

    monkeypatch.setattr(
        remote_proxy,
        "fetch_proxy_subscription",
        lambda url, **kwargs: calls["fetch"].append((url, kwargs))
        or SimpleNamespace(nodes=(first, second)),
    )
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_subscription_direct_fallback_allowed",
        lambda: False,
    )
    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: calls["order"].append("acquire") or True,
    )

    def release_hot_update():
        calls["release"] += 1

    monkeypatch.setattr(remote_proxy, "release_proxy_subscription_hot_update", release_hot_update)

    def refresh_running(nodes, **kwargs):
        calls["apply"].append((tuple(nodes), kwargs))
        return "本机 AI 代理已热更新节点；验证通过"

    monkeypatch.setattr(local_proxy, "refresh_running_local_ai_proxy_from_subscription", refresh_running)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    tab = object.__new__(LocalProxyTab)
    tab._periodic_update_running = False
    tab._busy = False
    tab._saved_subscription_load_generation = 0
    tab._subscription_url_input = lambda: "https://subscription.example/local"
    def save_profile(show_message=False):
        calls["order"].append("save")
        return {
            "id": profile_id,
            "selected_node_key": selected_key,
        }

    tab._save_subscription_profile = save_profile
    tab._cancel_periodic_update = lambda: None
    tab._schedule_periodic_update = lambda: calls.__setitem__("schedule", calls["schedule"] + 1)
    tab._set_busy = lambda value: calls["busy"].append(value)
    tab._set_cache_status = lambda *_args, **_kwargs: None
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab._selected_subscription_node_key = lambda: selected_key
    tab._set_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._start_subscription_hot_update(manual=True)

    assert calls["fetch"] == [
        (
            "https://subscription.example/local",
            {
                    "profile_id": profile_id,
                    "activate": False,
                    "allow_direct_fallback": False,
                    "recovery_proxy_provider": (
                        local_proxy.local_proxy_subscription_recovery_session
                    ),
            },
        )
    ]
    assert calls["apply"] == [
        (
            (first, second),
            {"quality_results": quality_results, "profile_id": profile_id},
        )
    ]
    assert calls["release"] == 1
    assert calls["order"] == ["acquire", "save"]
    assert calls["busy"] == [True, False]
    assert calls["nodes"] == [((first, second), selected_key)]
    assert calls["schedule"] == 1
    assert statuses[-1][1] == "success"
    assert "验证通过" in statuses[-1][0]
    assert toasts[-1][1]["is_error"] is False


def test_ssh_periodic_refresh_uses_persisted_strict_download_policy(monkeypatch):
    first = _node(1, "香港-1")
    second = _node(2, "美国-2")
    profile_id = "profile-ssh"
    selected_key = remote_proxy.proxy_subscription_node_key(first)
    quality_results = {selected_key: {"quality_score": 90}}
    state = {
        "active_profile_id": profile_id,
        "profiles": {
            profile_id: {
                "id": profile_id,
                "selected_node_key": selected_key,
                "node_latencies": {},
                "node_qualities": quality_results,
            }
        },
    }
    calls = {
        "fetch": [],
        "apply": [],
        "release": 0,
        "busy": [],
        "nodes": [],
        "schedule": 0,
        "order": [],
    }
    statuses = []
    toasts = []

    monkeypatch.setattr(
        remote_proxy,
        "fetch_proxy_subscription",
        lambda url, **kwargs: calls["fetch"].append((url, kwargs))
        or SimpleNamespace(nodes=(first, second)),
    )
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_subscription_direct_fallback_allowed",
        lambda: False,
    )
    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: calls["order"].append("acquire") or True,
    )

    def release_hot_update():
        calls["release"] += 1

    monkeypatch.setattr(remote_proxy, "release_proxy_subscription_hot_update", release_hot_update)

    def refresh_running(server_name, nodes, **kwargs):
        calls["apply"].append((server_name, tuple(nodes), kwargs))
        if server_name == "alpha":
            return "alpha: 已无重启切换；验证通过"
        return "beta: AI 代理未运行，已跳过订阅热更新"

    monkeypatch.setattr(remote_proxy, "refresh_running_ai_proxy_from_subscription", refresh_running)
    monkeypatch.setattr("ui.tabs.ssh_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "ui.tabs.ssh_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    tab = object.__new__(SSHTab)
    tab._proxy_periodic_update_running = False
    tab._proxy_busy = False
    tab._ssh_busy = False
    tab._proxy_saved_subscription_load_generation = 0
    tab._proxy_subscription_url_input = lambda: "https://subscription.example/ssh"
    tab._selected_sync_server_names = lambda: ["alpha", "beta"]
    def save_profile(show_message=False):
        calls["order"].append("save")
        assert tab._proxy_subscription_hot_update_lock_owned is True
        return {
            "id": profile_id,
            "selected_node_key": selected_key,
        }

    tab._save_proxy_subscription_profile = save_profile
    tab._cancel_proxy_periodic_update = lambda: None
    tab._schedule_proxy_periodic_update = lambda: calls.__setitem__("schedule", calls["schedule"] + 1)
    tab._set_proxy_busy = lambda value: calls["busy"].append(value)
    tab._set_proxy_cache_status = lambda *_args, **_kwargs: None
    tab._set_proxy_status = lambda message, severity="info": statuses.append((message, severity))
    tab._format_server_target = lambda names: f"{len(names)} targets"
    tab._run_server_batch = lambda names, action: SSHTab._run_server_batch(tab, names, action)
    tab._selected_proxy_subscription_node_key = lambda: selected_key
    tab._set_proxy_subscription_nodes = lambda nodes, preserve_key="": calls["nodes"].append(
        (tuple(nodes), preserve_key)
    )
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._start_proxy_subscription_hot_update(manual=False)

    assert calls["fetch"] == [
        (
            "https://subscription.example/ssh",
            {
                    "profile_id": profile_id,
                    "activate": False,
                    "allow_direct_fallback": False,
                    "recovery_proxy_provider": (
                        local_proxy.local_proxy_subscription_recovery_session
                    ),
            },
        )
    ]
    assert calls["apply"] == [
        (
            "alpha",
            (first, second),
            {
                "quality_results": quality_results,
                "profile_id": profile_id,
                "persist_selection": False,
            },
        ),
        (
            "beta",
            (first, second),
            {
                "quality_results": quality_results,
                "profile_id": profile_id,
                "persist_selection": False,
            },
        ),
    ]
    assert calls["release"] == 1
    assert calls["order"] == ["acquire", "save"]
    assert calls["busy"] == [True, False]
    assert calls["nodes"] == [((first, second), selected_key)]
    assert calls["schedule"] == 1
    assert statuses[-1][1] == "warning"
    assert "已跳过" in statuses[-1][0]
    assert toasts == []


def test_refresh_hot_updates_must_acquire_global_lock_before_saving_profile(monkeypatch):
    events = []
    local_statuses = []
    ssh_statuses = []
    schedules = {"local": 0, "ssh": 0}

    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: events.append("acquire") or False,
    )
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: (_ for _ in ()).throw(AssertionError("unowned lock must not be released")),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    local = object.__new__(LocalProxyTab)
    local._periodic_update_running = False
    local._busy = False
    local._subscription_url_input = lambda: "https://subscription.example/local"
    local._save_subscription_profile = lambda **_kwargs: events.append("local-save")
    local._set_status = lambda message, severity="info": local_statuses.append((message, severity))
    local._schedule_periodic_update = lambda: schedules.__setitem__(
        "local", schedules["local"] + 1
    )
    local.winfo_toplevel = lambda: object()

    local._start_subscription_hot_update(manual=True)

    ssh = object.__new__(SSHTab)
    ssh._proxy_periodic_update_running = False
    ssh._proxy_busy = False
    ssh._ssh_busy = False
    ssh._proxy_subscription_url_input = lambda: "https://subscription.example/ssh"
    ssh._selected_sync_server_names = lambda: ["alpha"]
    ssh._save_proxy_subscription_profile = lambda **_kwargs: events.append("ssh-save")
    ssh._set_proxy_status = lambda message, severity="info": ssh_statuses.append(
        (message, severity)
    )
    ssh._schedule_proxy_periodic_update = lambda: schedules.__setitem__(
        "ssh", schedules["ssh"] + 1
    )
    ssh.winfo_toplevel = lambda: object()

    ssh._start_proxy_subscription_hot_update(manual=True)

    assert events == ["acquire", "acquire"]
    assert schedules == {"local": 1, "ssh": 1}
    assert "另一个订阅热更新正在进行" in local_statuses[-1][0]
    assert "另一个订阅热更新正在进行" in ssh_statuses[-1][0]


def test_profile_save_and_switch_cannot_bypass_cross_tab_hot_update_lock(monkeypatch):
    mutations = []
    local_statuses = []
    ssh_statuses = []

    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: False,
    )
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_profile",
        lambda *_args, **_kwargs: mutations.append("save"),
    )
    monkeypatch.setattr(
        remote_proxy,
        "set_active_proxy_subscription_profile",
        lambda *_args, **_kwargs: mutations.append("switch"),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    local = object.__new__(LocalProxyTab)
    local._busy = False
    local._subscription_hot_update_lock_owned = False
    local._subscription_url_input = lambda: "https://subscription.example/local"
    local._subscription_name_entry = _ValueStub("local")
    local._subscription_profile_loading = False
    local._subscription_profile_options = {"profile": "profile-local"}
    local._set_status = lambda message, severity="info": local_statuses.append(
        (message, severity)
    )
    local._refresh_subscription_profile_options = lambda *_args, **_kwargs: None
    local.winfo_toplevel = lambda: object()

    assert local._save_subscription_profile(show_message=False) is None
    local._on_subscription_profile_selected("profile")

    ssh = object.__new__(SSHTab)
    ssh._proxy_busy = False
    ssh._proxy_subscription_hot_update_lock_owned = False
    ssh._proxy_subscription_url_input = lambda: "https://subscription.example/ssh"
    ssh._proxy_subscription_name_entry = _ValueStub("ssh")
    ssh._proxy_subscription_profile_loading = False
    ssh._proxy_subscription_profile_options = {"profile": "profile-ssh"}
    ssh._set_proxy_status = lambda message, severity="info": ssh_statuses.append(
        (message, severity)
    )
    ssh._refresh_proxy_subscription_profile_options = lambda *_args, **_kwargs: None
    ssh.winfo_toplevel = lambda: object()

    assert ssh._save_proxy_subscription_profile(show_message=False) is None
    ssh._on_proxy_subscription_profile_selected("profile")

    assert mutations == []
    assert any("另一个订阅热更新正在进行" in message for message, _severity in local_statuses)
    assert any("另一个标签页正在热更新订阅" in message for message, _severity in ssh_statuses)


def test_local_task_thread_start_failure_restores_busy(monkeypatch):
    busy = []
    statuses = []
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _FailingStartThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    local = object.__new__(LocalProxyTab)
    local._busy = False

    def set_busy(value):
        local._busy = value
        busy.append(value)

    local._set_busy = set_busy
    local._set_status = lambda message, severity="info": statuses.append((message, severity))
    local.winfo_toplevel = lambda: object()

    local._run_local_task("running", lambda: "done", "本机任务")

    assert busy == [True, False]
    assert local._busy is False
    assert statuses[-1][0].startswith("本机任务启动失败:")
    assert statuses[-1][1] == "error"


def test_local_task_feedback_marks_skips_as_warning_and_keeps_failure_safety_hint(monkeypatch):
    statuses = []
    toasts = []
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.show_toast",
        lambda _top, message, **kwargs: toasts.append((message, kwargs)),
    )

    local = object.__new__(LocalProxyTab)
    local._busy = False
    local._set_busy = lambda value: setattr(local, "_busy", value)
    local._set_status = lambda message, severity="info": statuses.append((message, severity))
    local._run_on_ui_thread = lambda callback: callback()
    local.winfo_exists = lambda: True
    local.winfo_toplevel = lambda: object()

    local._run_local_task(
        "正在检查...",
        lambda: "本机 AI 代理未运行，已跳过连通性探测",
        "检查本机 AI 代理",
    )

    assert statuses[-1][1] == "warning"
    assert toasts[-1][1]["is_error"] is True

    def fail_read_only_check():
        raise RuntimeError("controller unavailable")

    local._run_local_task(
        "正在检查...",
        fail_read_only_check,
        "检查本机 AI 代理",
        failure_hint="本次仅执行状态检查，未修改本机代理设置",
    )

    assert statuses[-1][1] == "error"
    assert "未修改本机代理设置" in statuses[-1][0]
    assert "未修改本机代理设置" in toasts[-1][0]


def test_current_node_thread_start_failure_releases_lock_and_restores_busy(monkeypatch):
    node = _node(1, "香港-1")
    local_profile_id = "profile-local-thread-failure"
    ssh_profile_id = "profile-ssh-thread-failure"
    release_calls = []
    local_busy = []
    ssh_busy = []
    local_statuses = []
    ssh_statuses = []
    active_profile_id = local_profile_id

    monkeypatch.setattr(
        remote_proxy,
        "try_acquire_proxy_subscription_hot_update",
        lambda: True,
    )
    monkeypatch.setattr(
        remote_proxy,
        "release_proxy_subscription_hot_update",
        lambda: release_calls.append("release"),
    )
    monkeypatch.setattr(
        remote_proxy,
        "load_proxy_subscription_state",
        lambda: {
            "active_profile_id": active_profile_id,
            "profiles": {active_profile_id: {"id": active_profile_id}},
        },
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _FailingStartThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *_args, **_kwargs: None)

    local = object.__new__(LocalProxyTab)
    local._periodic_update_running = False
    local._busy = False
    local._subscription_picker = _SelectedPickerStub(node)
    local._subscription_nodes = [node]
    local._quality_results = {}
    local._subscription_profile_combo = _ValueStub("current")
    local._subscription_profile_options = {"current": local_profile_id}
    local._saved_subscription_load_generation = 1

    def set_local_busy(value):
        local._busy = value
        local_busy.append(value)

    local._set_busy = set_local_busy
    local._set_status = lambda message, severity="info": local_statuses.append(
        (message, severity)
    )
    local.winfo_toplevel = lambda: object()

    local._hot_update_selected_subscription_node()

    active_profile_id = ssh_profile_id
    ssh = object.__new__(SSHTab)
    ssh._proxy_periodic_update_running = False
    ssh._proxy_busy = False
    ssh._ssh_busy = False
    ssh._proxy_subscription_picker = _SelectedPickerStub(node)
    ssh._proxy_subscription_nodes = [node]
    ssh._proxy_quality_results = {}
    ssh._proxy_subscription_profile_combo = _ValueStub("current")
    ssh._proxy_subscription_profile_options = {"current": ssh_profile_id}
    ssh._require_selected_servers = lambda status_setter=None: ["alpha"]
    ssh._format_server_target = lambda names: "alpha"
    ssh._set_proxy_status = lambda message, severity="info": ssh_statuses.append(
        (message, severity)
    )
    ssh._set_sync_status = lambda *_args, **_kwargs: None
    ssh._remote_inspect_button = None
    ssh._remote_pull_button = None
    ssh._remote_pull_options = {}

    def set_ssh_busy(value):
        ssh._proxy_busy = value
        ssh_busy.append(value)

    ssh._set_proxy_busy = set_ssh_busy
    ssh.winfo_toplevel = lambda: object()

    ssh._hot_update_selected_proxy_subscription_node()

    assert release_calls == ["release", "release"]
    assert local_busy == [True, False]
    assert local._busy is False
    assert ssh_busy == [True, False]
    assert ssh._proxy_busy is False
    assert ssh._ssh_busy is False
    assert "启动失败" in local_statuses[-1][0]
    assert any("线程未启动" in message for message, _severity in ssh_statuses)


def test_proxy_subscription_hot_update_lock_is_non_blocking_and_reusable():
    assert remote_proxy.try_acquire_proxy_subscription_hot_update() is True
    try:
        assert remote_proxy.try_acquire_proxy_subscription_hot_update() is False
    finally:
        remote_proxy.release_proxy_subscription_hot_update()
    assert remote_proxy.try_acquire_proxy_subscription_hot_update() is True
    remote_proxy.release_proxy_subscription_hot_update()


def test_current_node_hot_update_buttons_follow_busy_and_node_availability():
    local = object.__new__(LocalProxyTab)
    local_button = _LabelStub()
    local_manual_button = _LabelStub()
    local_attrs = (
        "_fetch_button",
        "_manual_hot_update_button",
        "_latency_button",
        "_quality_button",
        "_use_node_button",
        "_quality_settings_button",
        "_ping0_button",
        "_load_file_button",
        "_start_button",
        "_inspect_button",
        "_test_button",
        "_stop_button",
        "_apply_routing_button",
        "_subscription_profile_save_button",
        "_subscription_profile_delete_button",
        "_quality_cancel_button",
        "_auto_refresh_check",
        "_periodic_update_check",
        "_subscription_picker",
        "_subscription_profile_combo",
        "_subscription_name_entry",
        "_subscription_entry",
    )
    for name in local_attrs:
        setattr(local, name, None)
    local._manual_hot_update_button = local_manual_button
    local._hot_update_node_button = local_button
    local._subscription_options = {}

    local._set_busy(False)
    assert local_manual_button.values["state"] == "normal"
    assert local_button.values["state"] == "disabled"
    local._subscription_options = {"node": object()}
    local._set_busy(False)
    assert local_button.values["state"] == "normal"
    local._set_busy(True)
    assert local_manual_button.values["state"] == "disabled"
    assert local_button.values["state"] == "disabled"

    ssh = object.__new__(SSHTab)
    ssh_button = _LabelStub()
    ssh_manual_button = _LabelStub()
    ssh_attrs = (
        "_proxy_fetch_button",
        "_proxy_manual_hot_update_button",
        "_proxy_latency_button",
        "_proxy_quality_button",
        "_proxy_use_node_button",
        "_proxy_quality_settings_button",
        "_proxy_ping0_button",
        "_proxy_load_file_button",
        "_proxy_deploy_button",
        "_proxy_inspect_button",
        "_proxy_remote_test_button",
        "_proxy_remote_cleanup_button",
        "_proxy_subscription_profile_save_button",
        "_proxy_subscription_profile_delete_button",
        "_proxy_quality_cancel_button",
        "_proxy_auto_refresh_check",
        "_proxy_periodic_update_check",
        "_proxy_subscription_picker",
        "_proxy_subscription_profile_combo",
        "_proxy_subscription_name_entry",
        "_proxy_subscription_entry",
    )
    for name in ssh_attrs:
        setattr(ssh, name, None)
    ssh._proxy_manual_hot_update_button = ssh_manual_button
    ssh._proxy_hot_update_button = ssh_button
    ssh._proxy_subscription_options = {}

    ssh._set_proxy_busy(False)
    assert ssh_manual_button.values["state"] == "normal"
    assert ssh_button.values["state"] == "disabled"
    ssh._proxy_subscription_options = {"node": object()}
    ssh._set_proxy_busy(False)
    assert ssh_button.values["state"] == "normal"
    ssh._set_proxy_busy(True)
    assert ssh_manual_button.values["state"] == "disabled"
    assert ssh_button.values["state"] == "disabled"


def test_proxy_tabs_action_hint_lists_only_effective_quality_sources(monkeypatch):
    settings = network_diagnostic_settings.settings_from_values(
        {
            network_diagnostic_settings.SERVICE_NETCOFFEE,
            network_diagnostic_settings.SERVICE_PING0,
        },
        {},
    )
    monkeypatch.setattr(network_diagnostic_settings, "load_settings", lambda: settings)

    cases = (
        (LocalProxyTab, "_subscription_action_hint_label", "_subscription_picker", "_refresh_subscription_action_hint"),
        (SSHTab, "_proxy_subscription_action_hint_label", "_proxy_subscription_picker", "_refresh_proxy_subscription_action_hint"),
    )
    for tab_class, label_attr, picker_attr, method_name in cases:
        tab = object.__new__(tab_class)
        label = _LabelStub()
        setattr(tab, label_attr, label)
        setattr(tab, picker_attr, _GroupLabelPickerStub())

        getattr(tab, method_name)()

        text = label.values["text"]
        assert "可执行质量源: Net.Coffee AI" in text
        assert "缺 Key 已跳过: Ping0" in text
        assert "可执行质量源: Net.Coffee AI + Ping0" not in text
        assert label.values["text_color"] == COLORS["warning"]


def test_proxy_tabs_action_hint_warns_when_no_quality_source_is_executable(monkeypatch):
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_PING0},
        {},
    )
    monkeypatch.setattr(network_diagnostic_settings, "load_settings", lambda: settings)

    cases = (
        (LocalProxyTab, "_subscription_action_hint_label", "_subscription_picker", "_refresh_subscription_action_hint"),
        (SSHTab, "_proxy_subscription_action_hint_label", "_proxy_subscription_picker", "_refresh_proxy_subscription_action_hint"),
    )
    for tab_class, label_attr, picker_attr, method_name in cases:
        tab = object.__new__(tab_class)
        label = _LabelStub()
        setattr(tab, label_attr, label)
        setattr(tab, picker_attr, _GroupLabelPickerStub())

        getattr(tab, method_name)()

        assert "可执行质量源: 无" in label.values["text"]
        assert "已启用但缺 Key: Ping0" in label.values["text"]
        assert label.values["text_color"] == COLORS["warning"]


def test_proxy_tabs_cancel_quality_status_covers_dns_and_http_work():
    local_source = inspect.getsource(LocalProxyTab._cancel_subscription_quality)
    ssh_source = inspect.getsource(SSHTab._cancel_proxy_subscription_quality)

    for source in (local_source, ssh_source):
        assert "DNS 解析/网络请求" in source
        assert "已发出的网络请求将在当前超时内结束" not in source


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


def test_proxy_node_picker_updates_registered_controls_without_walking_full_widget_tree():
    class _ListFrame:
        def winfo_children(self):
            raise AssertionError("enabled-state updates must not traverse every rendered label")

    picker = object.__new__(ProxyNodePicker)
    checkbox = _LabelStub()
    select_button = _LabelStub()
    quality_button = _LabelStub()
    toggle_button = _LabelStub()
    picker._list_frame = _ListFrame()
    picker._visible_checkboxes = {"node": (checkbox, object())}
    picker._visible_node_rows = {"node": (object(), select_button)}
    picker._visible_group_headers = [
        {"quality": quality_button, "toggle": toggle_button}
    ]

    picker._set_visible_rows_enabled(False)

    for control in (checkbox, select_button, quality_button, toggle_button):
        assert control.values["state"] == "disabled"


def test_proxy_node_picker_cancel_invalidates_stale_batch_without_clobbering_new_state():
    picker = object.__new__(ProxyNodePicker)
    picker._render_batch_after_id = "old-batch"
    picker._render_plan_pending = True
    picker._render_generation = 7
    picker._list_frame = object()
    picker.after_cancel = lambda _after_id: (_ for _ in ()).throw(RuntimeError("already queued"))
    picker._render_row = lambda _item: (_ for _ in ()).throw(
        AssertionError("stale batch must not render")
    )

    picker._cancel_incremental_render()

    assert picker._render_generation == 8
    assert picker._render_batch_after_id is None
    picker._render_plan_batch(7, [("row", object(), None)], 0)
    assert picker._render_plan_pending is True


def test_proxy_node_picker_tears_down_old_rows_in_bounded_batches(monkeypatch):
    class _Root:
        def __init__(self, name):
            self.name = name
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    picker = object.__new__(ProxyNodePicker)
    picker._render_generation = 11
    picker._render_batch_after_id = None
    picker._render_plan_pending = True
    picker._render_deferred = False
    picker._list_frame = object()
    picker._last_match_count = 9
    picker._last_visible_count = 9
    picker.TEARDOWN_BATCH_SIZE = 3
    scheduled = []
    rendered = []
    picker.after = lambda _delay, callback: scheduled.append(callback) or f"after-{len(scheduled)}"
    picker._schedule_render_plan = lambda generation, plan, empty: rendered.append(
        (generation, plan, empty)
    )
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)
    roots = tuple(_Root(str(index)) for index in range(7))
    render_plan = [("row", object(), None)]

    picker._teardown_roots_batch(11, roots, render_plan, None, 0)

    assert [root.destroyed for root in roots] == [True, True, True, False, False, False, False]
    assert rendered == []
    assert len(scheduled) == 1

    scheduled.pop(0)()
    assert [root.destroyed for root in roots] == [True, True, True, True, True, True, False]
    assert rendered == []
    assert len(scheduled) == 1

    scheduled.pop(0)()
    assert all(root.destroyed for root in roots)
    assert rendered == [(11, render_plan, None)]


def test_proxy_node_picker_stale_teardown_cannot_destroy_new_generation_rows():
    class _Root:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    picker = object.__new__(ProxyNodePicker)
    picker._render_generation = 12
    picker._render_plan_pending = True
    picker._list_frame = object()
    root = _Root()
    picker._schedule_render_plan = lambda *_args: (_ for _ in ()).throw(
        AssertionError("stale teardown must not start a render plan")
    )

    picker._teardown_roots_batch(11, (root,), [], None, 0)

    assert root.destroyed is False
    assert picker._render_plan_pending is True


def test_proxy_node_picker_render_schedules_old_rows_instead_of_destroying_synchronously(monkeypatch):
    class _Root:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    class _ListFrame:
        def __init__(self, roots):
            self.roots = roots

        def winfo_children(self):
            return list(self.roots)

    root = _Root()
    checkbox = _LabelStub()
    select_button = _LabelStub()
    quality_button = _LabelStub()
    toggle_button = _LabelStub()
    picker = object.__new__(ProxyNodePicker)
    picker._render_after_id = None
    picker._render_batch_after_id = None
    picker._render_generation = 4
    picker._render_plan_pending = False
    picker._render_deferred = False
    picker._list_frame = _ListFrame([root])
    picker._visible_group_headers = [
        {"quality": quality_button, "toggle": toggle_button}
    ]
    picker._visible_checkboxes = {"old": (checkbox, object())}
    picker._visible_node_rows = {"old": (object(), select_button)}
    picker._nodes = []
    picker._summary_counts = {}
    picker._checked_keys = set()
    picker._last_match_count = 0
    picker._last_visible_count = 0
    picker._cancel_incremental_render = lambda: None
    picker._filtered_nodes = lambda: []
    picker._update_summary_label = lambda **_kwargs: None
    picker._update_scope_label = lambda: None
    picker._empty_message = lambda *_args: "empty"
    picker._emit_scope_change = lambda: None
    scheduled = []
    picker._schedule_teardown_batch = lambda generation, roots, plan, empty: scheduled.append(
        (generation, roots, plan, empty)
    )
    picker._schedule_render_plan = lambda *_args: (_ for _ in ()).throw(
        AssertionError("old roots must be torn down before the new plan starts")
    )
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)

    picker._render_nodes()

    assert root.destroyed is False
    assert scheduled == [(5, (root,), [], "empty")]
    assert picker._visible_group_headers
    assert picker._visible_checkboxes
    assert picker._visible_node_rows

    picker._set_visible_rows_enabled(False)
    for control in (checkbox, select_button, quality_button, toggle_button):
        assert control.values["state"] == "disabled"

    picker._begin_render_plan(5, [], None)
    assert picker._visible_group_headers == []
    assert picker._visible_checkboxes == {}
    assert picker._visible_node_rows == {}


def test_proxy_node_picker_scheduler_failures_finish_current_generation(monkeypatch):
    class _Root:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    picker = object.__new__(ProxyNodePicker)
    picker._render_generation = 6
    picker._render_batch_after_id = None
    picker._render_plan_pending = True
    picker._render_deferred = False
    picker._list_frame = object()
    picker._last_match_count = 3
    picker._last_visible_count = 3
    picker.RENDER_BATCH_SIZE = 1
    picker.after = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Tk scheduler unavailable"))
    picker._update_summary_label = lambda **_kwargs: None
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)
    monkeypatch.setattr("ui.widgets.proxy_node_picker.recent_user_scroll", lambda *_args, **_kwargs: False)

    root = _Root()
    begun = []
    picker._begin_render_plan = lambda generation, plan, empty: begun.append(
        (generation, plan, empty)
    )
    picker._schedule_teardown_batch(6, (root,), [("row", "new", None)], None)

    assert root.destroyed is True
    assert begun == [(6, [("row", "new", None)], None)]

    rendered = []
    picker._begin_render_plan = ProxyNodePicker._begin_render_plan.__get__(picker)
    picker._visible_group_headers = []
    picker._visible_checkboxes = {}
    picker._visible_node_rows = {}
    picker._render_plan_item = lambda _kind, payload, _extra: rendered.append(payload)
    picker._render_plan_pending = True
    picker._render_plan_batch(
        6,
        [("row", "first", None), ("row", "second", None), ("row", "third", None)],
        0,
    )

    assert rendered == ["first", "second", "third"]
    assert picker._render_plan_pending is False


def test_proxy_node_picker_render_item_failure_does_not_stall_generation(monkeypatch):
    picker = object.__new__(ProxyNodePicker)
    picker._render_generation = 9
    picker._render_batch_after_id = None
    picker._render_plan_pending = True
    picker._list_frame = object()
    picker._last_match_count = 2
    picker._last_visible_count = 2
    picker.RENDER_BATCH_SIZE = 3
    picker._update_summary_label = lambda **_kwargs: None
    rendered = []

    def render_item(_kind, payload, _extra):
        if payload == "broken":
            raise RuntimeError("single row failed")
        rendered.append(payload)

    picker._render_plan_item = render_item
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)
    monkeypatch.setattr("ui.widgets.proxy_node_picker.recent_user_scroll", lambda *_args, **_kwargs: False)

    picker._render_plan_batch(
        9,
        [("row", "broken", None), ("row", "healthy", None)],
        0,
    )

    assert rendered == ["healthy"]
    assert picker._render_plan_pending is False


def test_proxy_node_picker_scroll_retry_failure_falls_through_to_render(monkeypatch):
    picker = object.__new__(ProxyNodePicker)
    picker._render_generation = 10
    picker._render_batch_after_id = None
    picker._render_plan_pending = True
    picker._list_frame = object()
    picker._last_match_count = 1
    picker._last_visible_count = 1
    picker.RENDER_BATCH_SIZE = 3
    picker.after = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Tk scheduler unavailable"))
    picker._update_summary_label = lambda **_kwargs: None
    rendered = []
    picker._render_plan_item = lambda _kind, payload, _extra: rendered.append(payload)
    monkeypatch.setattr("ui.widgets.proxy_node_picker.is_active_tab", lambda _widget: True)
    monkeypatch.setattr("ui.widgets.proxy_node_picker.recent_user_scroll", lambda *_args, **_kwargs: True)

    picker._render_plan_batch(10, [("row", "node", None)], 0)

    assert rendered == ["node"]
    assert picker._render_plan_pending is False


def test_proxy_node_picker_select_by_key_updates_visible_rows_without_rerender():
    picker = object.__new__(ProxyNodePicker)
    picker._nodes = [{"key": "first"}, {"key": "second"}]
    picker._selected_key = "first"
    picker._node_key = lambda item: item["key"]
    calls = {"render": 0, "selection": []}

    def render_nodes():
        calls["render"] += 1

    picker._render_nodes = render_nodes
    picker._update_visible_selection = lambda previous, selected: calls["selection"].append((previous, selected))

    assert picker.select_by_key("first")
    assert calls["render"] == 0
    assert calls["selection"] == []

    assert picker.select_by_key("second")
    assert picker._selected_key == "second"
    assert calls["render"] == 0
    assert calls["selection"] == [("first", "second")]

    assert not picker.select_by_key("missing")
    assert calls["render"] == 0
    assert calls["selection"] == [("first", "second")]


def test_proxy_node_picker_select_button_updates_visible_rows_without_rerender():
    first = {"key": "first"}
    second = {"key": "second"}
    picker = object.__new__(ProxyNodePicker)
    picker._nodes = [first, second]
    picker._selected_key = "first"
    picker._node_key = lambda item: item["key"]
    selected_items = []
    selection_updates = []
    picker._on_select = selected_items.append
    picker._render_nodes = lambda: (_ for _ in ()).throw(AssertionError("选择节点不应整体重绘"))
    picker._update_visible_selection = lambda previous, selected: selection_updates.append((previous, selected))

    picker._select("second")

    assert picker._selected_key == "second"
    assert selection_updates == [("first", "second")]
    assert selected_items == [second]


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
    assert picker._render_generation == 4
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


def test_local_proxy_tab_resyncs_profile_activated_by_ssh_tab(monkeypatch):
    state = {
        "active_profile_id": "profile-b",
        "saved_path": "profile-b.yaml",
        "profiles": {
            "profile-b": {"id": "profile-b", "saved_path": "profile-b.yaml"},
        },
    }
    calls = {"options": [], "inputs": [], "nodes": [], "cache": []}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._periodic_update_running = False
    tab._saved_subscription_loaded = True
    tab._saved_subscription_load_generation = 4
    tab._subscription_profile_combo = _ValueStub("profile A")
    tab._subscription_profile_options = {"profile A": "profile-a"}
    tab._refresh_subscription_profile_options = lambda snapshot: calls["options"].append(snapshot)
    tab._apply_subscription_profile_inputs = lambda snapshot: calls["inputs"].append(snapshot)
    tab._set_subscription_nodes = lambda nodes: calls["nodes"].append(tuple(nodes))
    tab._load_subscription_cache_for_state = lambda snapshot, generation: calls["cache"].append(
        (snapshot, generation)
    )

    assert tab._sync_shared_subscription_profile() is True
    assert calls == {
        "options": [state],
        "inputs": [state],
        "nodes": [()],
        "cache": [(state, 5)],
    }


def test_ssh_proxy_tab_resyncs_profile_activated_by_local_tab(monkeypatch):
    state = {
        "active_profile_id": "profile-b",
        "saved_path": "profile-b.yaml",
        "profiles": {
            "profile-b": {"id": "profile-b", "saved_path": "profile-b.yaml"},
        },
    }
    calls = {"options": [], "inputs": [], "nodes": [], "cache": []}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)

    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._ssh_busy = False
    tab._proxy_periodic_update_running = False
    tab._proxy_saved_subscription_loaded = True
    tab._proxy_saved_subscription_load_generation = 8
    tab._proxy_subscription_profile_combo = _ValueStub("profile A")
    tab._proxy_subscription_profile_options = {"profile A": "profile-a"}
    tab._refresh_proxy_subscription_profile_options = lambda snapshot: calls["options"].append(snapshot)
    tab._apply_proxy_subscription_profile_inputs = lambda snapshot: calls["inputs"].append(snapshot)
    tab._set_proxy_subscription_nodes = lambda nodes: calls["nodes"].append(tuple(nodes))
    tab._load_proxy_subscription_cache_for_state = lambda snapshot, generation: calls["cache"].append(
        (snapshot, generation)
    )

    assert tab._sync_shared_proxy_subscription_profile() is True
    assert calls == {
        "options": [state],
        "inputs": [state],
        "nodes": [()],
        "cache": [(state, 9)],
    }


def test_proxy_tabs_default_selection_persists_to_displayed_profile(monkeypatch):
    node = _node(1, "美国-页面分组")
    calls = []
    monkeypatch.setattr(
        remote_proxy,
        "set_proxy_subscription_selected_node",
        lambda selected, *, profile_id="": calls.append((selected, profile_id)),
    )

    local = object.__new__(LocalProxyTab)
    local._subscription_picker = _SelectedPickerStub(node)
    local._subscription_profile_combo = _ValueStub("local profile")
    local._subscription_profile_options = {"local profile": "local-profile-id"}
    local._node_text = None
    local._set_selected_summary = lambda *_args, **_kwargs: None
    local._set_status = lambda *_args, **_kwargs: None
    local._use_selected_subscription_node(show_message=False)

    ssh = object.__new__(SSHTab)
    ssh._proxy_subscription_picker = _SelectedPickerStub(node)
    ssh._proxy_subscription_profile_combo = _ValueStub("ssh profile")
    ssh._proxy_subscription_profile_options = {"ssh profile": "ssh-profile-id"}
    ssh._proxy_node_text = None
    ssh._set_proxy_selected_summary = lambda *_args, **_kwargs: None
    ssh._set_proxy_status = lambda *_args, **_kwargs: None
    ssh._use_selected_proxy_subscription_node(show_message=False)

    assert calls == [
        (node.node, "local-profile-id"),
        (node.node, "ssh-profile-id"),
    ]


def test_local_latency_results_use_profile_captured_before_worker(monkeypatch):
    node = _node(1, "美国-测速")
    result = _latency(node, True)
    calls = {"saved": [], "used": []}
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies",
        lambda *_args, **_kwargs: {remote_proxy.proxy_subscription_node_key(node): result},
    )
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_latencies",
        lambda values, *, profile_id="": calls["saved"].append((dict(values), profile_id)),
    )
    monkeypatch.setattr(
        local_proxy,
        "select_stable_local_proxy_node",
        lambda nodes, *_args, **_kwargs: (
            list(nodes)[0],
            {
                remote_proxy.proxy_subscription_node_key(node): SimpleNamespace(
                    **vars(_verified_stability()),
                )
            },
        ),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = [node]
    tab._latency_results = {}
    tab._quality_results = {}
    tab._prefer_quality_sort = False
    tab._saved_subscription_load_generation = 4
    tab._selected_subscription_node_key = lambda: "original"
    tab._subscription_batch_nodes = lambda: [node]
    tab._subscription_batch_scope_label = lambda: "当前分组"
    tab._current_subscription_profile_id = lambda: "captured-profile"
    tab._set_busy = lambda _value: None
    tab._set_status = lambda *_args, **_kwargs: None
    tab._set_subscription_nodes = lambda *_args, **_kwargs: None
    tab._fastest_subscription_node = lambda _nodes=None: node
    tab._select_subscription_node_by_key = lambda _key: True
    tab._use_selected_subscription_node = lambda **kwargs: calls["used"].append(kwargs)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._measure_subscription_latencies()

    assert calls["saved"] == [
        ({remote_proxy.proxy_subscription_node_key(node): result}, "captured-profile")
    ]
    assert calls["used"] == [
        {"show_message": False, "profile_id": "captured-profile"}
    ]


def test_local_latency_gate_skips_first_unstable_and_selects_second_stable(monkeypatch):
    first = _node(1, "美国-低延迟不稳定")
    second = _node(2, "日本-稳定")
    first_result = remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(first), True, latency_ms=20, attempts=3
    )
    second_result = remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(second), True, latency_ms=45, attempts=3
    )
    selected_key = remote_proxy.proxy_subscription_node_key(second)
    calls = {"measure_kwargs": {}, "used": [], "selected": [], "saved": []}

    def fake_measure(_nodes, **kwargs):
        calls["measure_kwargs"] = kwargs
        return {
            remote_proxy.proxy_subscription_node_key(first): first_result,
            selected_key: second_result,
        }

    def fake_stability(nodes, latencies, qualities, **kwargs):
        assert tuple(nodes) == (first, second)
        assert latencies[selected_key] is second_result
        assert qualities == {"quality": "snapshot"}
        assert kwargs == {"rounds": 3}
        return second, {
            remote_proxy.proxy_subscription_node_key(first): SimpleNamespace(stable=False),
            selected_key: _verified_stability(),
        }

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", fake_measure)
    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", fake_stability)
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_latencies",
        lambda values, *, profile_id="": calls["saved"].append((dict(values), profile_id)),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = [first, second]
    tab._latency_results = {}
    tab._quality_results = {"quality": "snapshot"}
    tab._prefer_quality_sort = False
    tab._saved_subscription_load_generation = 7
    tab._subscription_batch_nodes = lambda: [first, second]
    tab._subscription_batch_scope_label = lambda: "当前分组"
    tab._current_subscription_profile_id = lambda: "profile-a"
    tab._selected_subscription_node_key = lambda: "original-key"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    statuses = []
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab._set_subscription_nodes = lambda *_args, **_kwargs: None
    tab._select_subscription_node_by_key = lambda key: calls["selected"].append(key) or True
    tab._use_selected_subscription_node = lambda **kwargs: calls["used"].append(kwargs)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._measure_subscription_latencies()

    assert calls["measure_kwargs"] == {
        "timeout": 3.0,
        "attempts": 3,
        "max_workers": 20,
        "require_all": True,
    }
    assert calls["selected"] == [selected_key]
    assert calls["used"] == [{"show_message": False, "profile_id": "profile-a"}]
    assert calls["saved"][0][1] == "profile-a"
    assert "3 轮 AI 短探针" in statuses[-1][0]
    assert "Codex 长会话网络近似 2/2 轮完整传输" in statuses[-1][0]
    assert "不是真实账号 compact" in statuses[-1][0]
    assert "未改动系统代理" in statuses[-1][0]


def test_local_latency_gate_all_unstable_keeps_original_selection(monkeypatch):
    node = _node(1, "美国-不稳定")
    key = remote_proxy.proxy_subscription_node_key(node)
    latency = remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=25, attempts=3)
    calls = {"selected": [], "used": [], "preserved": []}

    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies",
        lambda *_args, **_kwargs: {key: latency},
    )
    monkeypatch.setattr(
        local_proxy,
        "select_stable_local_proxy_node",
        lambda *_args, **_kwargs: (
            node,
            {
                key: _verified_stability(
                    stable=True,
                    deep_transport_ok=False,
                    deep_transport_successes=1,
                    codex_compact_ok=False,
                )
            },
        ),
    )
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = [node]
    tab._latency_results = {}
    tab._quality_results = {}
    tab._prefer_quality_sort = False
    tab._saved_subscription_load_generation = 1
    tab._subscription_batch_nodes = lambda: [node]
    tab._subscription_batch_scope_label = lambda: "当前分组"
    tab._current_subscription_profile_id = lambda: "profile-a"
    tab._selected_subscription_node_key = lambda: "original-key"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    statuses = []
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab._set_subscription_nodes = lambda *_args, **kwargs: calls["preserved"].append(kwargs)
    tab._select_subscription_node_by_key = lambda key_value: calls["selected"].append(key_value) or True
    tab._use_selected_subscription_node = lambda **kwargs: calls["used"].append(kwargs)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._measure_subscription_latencies()

    assert calls["selected"] == []
    assert calls["used"] == []
    assert calls["preserved"] == [{"preserve_key": "original-key"}]
    assert "已保留原选择" in statuses[-1][0]
    assert "Codex 长会话网络近似" in statuses[-1][0]
    assert statuses[-1][1] == "warning"


def test_local_latency_gate_profile_change_saves_captured_profile_without_selecting(monkeypatch):
    node = _node(1, "美国-跨分组")
    key = remote_proxy.proxy_subscription_node_key(node)
    latency = remote_proxy.ProxyNodeLatencyResult(key, True, latency_ms=30, attempts=3)
    calls = {"profile_reads": 0, "saved": [], "selected": [], "used": []}
    monkeypatch.setattr(
        remote_proxy,
        "measure_proxy_node_latencies",
        lambda *_args, **_kwargs: {key: latency},
    )
    monkeypatch.setattr(
        local_proxy,
        "select_stable_local_proxy_node",
        lambda *_args, **_kwargs: (node, {key: _verified_stability()}),
    )
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_latencies",
        lambda values, *, profile_id="": calls["saved"].append((dict(values), profile_id)),
    )
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = [node]
    tab._latency_results = {}
    tab._quality_results = {}
    tab._prefer_quality_sort = False
    tab._saved_subscription_load_generation = 2
    tab._subscription_batch_nodes = lambda: [node]
    tab._subscription_batch_scope_label = lambda: "原分组"

    def current_profile():
        calls["profile_reads"] += 1
        return "profile-a" if calls["profile_reads"] == 1 else "profile-b"

    tab._current_subscription_profile_id = current_profile
    tab._selected_subscription_node_key = lambda: "original-key"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    statuses = []
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab._set_subscription_nodes = lambda *_args, **_kwargs: None
    tab._select_subscription_node_by_key = lambda value: calls["selected"].append(value) or True
    tab._use_selected_subscription_node = lambda **kwargs: calls["used"].append(kwargs)
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    tab._measure_subscription_latencies()

    assert calls["saved"][0][1] == "profile-a"
    assert calls["selected"] == []
    assert calls["used"] == []
    assert "页面分组已变更" in statuses[-1][0]


def test_local_latency_gate_thread_start_failure_restores_busy(monkeypatch):
    node = _node(1, "美国-线程失败")
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _FailingStartThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = [node]
    tab._latency_results = {}
    tab._quality_results = {}
    tab._saved_subscription_load_generation = 1
    tab._subscription_batch_nodes = lambda: [node]
    tab._subscription_batch_scope_label = lambda: "当前分组"
    tab._current_subscription_profile_id = lambda: "profile-a"
    tab._selected_subscription_node_key = lambda: "original"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    statuses = []
    tab._set_status = lambda message, severity="info": statuses.append((message, severity))
    tab.winfo_toplevel = lambda: object()

    tab._measure_subscription_latencies()

    assert tab._busy is False
    assert "启动节点测速与稳定验证任务失败" in statuses[-1][0]


def test_latency_cache_with_explicit_profile_never_writes_global_active_profile(monkeypatch):
    node = _node(1, "美国-缓存")
    result = _latency(node, True)
    calls = []
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_profile_state",
        lambda profile_id, **updates: calls.append((profile_id, updates)) or {"id": profile_id},
    )
    monkeypatch.setattr(
        remote_proxy,
        "save_proxy_subscription_state",
        lambda **_updates: (_ for _ in ()).throw(
            AssertionError("explicit profile latency cache must not use global active profile")
        ),
    )

    saved = remote_proxy.save_proxy_subscription_latencies(
        {remote_proxy.proxy_subscription_node_key(node): result},
        profile_id="captured-profile",
    )

    assert saved == {"id": "captured-profile"}
    assert calls[0][0] == "captured-profile"
    assert calls[0][1]["node_latencies"]
