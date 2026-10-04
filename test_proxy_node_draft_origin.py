"""Subscription changes cannot silently deploy a previous or overwrite a manual node."""
from types import MethodType, SimpleNamespace

import pytest

from core import remote_proxy
from test_subscription_profile_ui import (
    _isolated_subscription_storage, _local_tab_stub, _seed_two_profiles, _ssh_tab_stub,
)
from ui.tabs import local_proxy_tab, ssh_tab


class Text:
    def __init__(self, value=""):
        self.value = value

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = value


class Picker:
    def __init__(self):
        self.items = []
        self.item = None

    def selected_item(self):
        return self.item

    def selected_key(self):
        return self.item.name if self.item else ""

    def select_by_key(self, key):
        match = next((item for item in self.items if item.name == key), None)
        if match:
            self.item = match
        return bool(match)


@pytest.fixture(params=["win", "ssh"])
def setup(request, monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    win = request.param == "win"
    tab = _local_tab_stub() if win else _ssh_tab_stub()
    module = local_proxy_tab if win else ssh_tab
    # Patch the real module, not LazyModule's resolved attributes: restoring
    # those would otherwise leave shadows that defeat later tests' patches.
    monkeypatch.setattr(module, "remote_proxy", remote_proxy)
    cls = module.LocalProxyTab if win else module.SSHTab
    prefix = "" if win else "_proxy"
    callbacks, summaries, messages, saves = [], [], [], []
    picker, editor = Picker(), Text()
    setattr(tab, prefix + "_subscription_picker", picker)
    setattr(tab, prefix + "_node_text", editor)
    setattr(tab, "_set_selected_summary" if win else "_set_proxy_selected_summary", lambda text, *_args: summaries.append(text))
    setattr(tab, "_set_status" if win else "_set_proxy_status", lambda text, *_args: messages.append(text))
    generation_attr = "_saved_subscription_load_generation" if win else "_proxy_saved_subscription_load_generation"
    setattr(tab, generation_attr, 1)
    tab._destroyed = False
    tab.winfo_exists = lambda: True
    tab._run_on_ui_thread = lambda callback: callbacks.append(callback)
    tab._schedule_startup_refresh = lambda: None
    tab._schedule_periodic_update = lambda **_kwargs: None
    tab._schedule_proxy_startup_refresh = lambda: None
    tab._schedule_proxy_periodic_update = lambda **_kwargs: None

    def set_nodes(nodes, preserve_key=""):
        picker.items = list(nodes)
        picker.item = next((item for item in nodes if item.name == preserve_key), next(iter(nodes), None))
        setattr(tab, prefix + "_subscription_options", {item.name: item for item in nodes})

    setattr(tab, "_set_subscription_nodes" if win else "_set_proxy_subscription_nodes", set_nodes)
    prepare = tab._use_selected_subscription_node if win else tab._use_selected_proxy_subscription_node
    apply_inputs = tab._apply_subscription_profile_inputs if win else tab._apply_proxy_subscription_profile_inputs
    options = tab._refresh_subscription_profile_options if win else tab._refresh_proxy_subscription_profile_options
    restore_method = cls._load_subscription_cache_for_state if win else cls._load_proxy_subscription_cache_for_state
    restore = MethodType(restore_method, tab)
    actual_input = tab._node_input if win else tab._proxy_node_input
    new_profile = tab._enter_new_subscription_profile if win else tab._enter_new_proxy_subscription_profile
    a, b = [SimpleNamespace(name=name, node={"name": name}) for name in ("node-a", "node-b")]
    monkeypatch.setattr(module.remote_proxy, "format_proxy_node", lambda node: f"yaml:{node['name']}")
    monkeypatch.setattr(module.remote_proxy, "describe_proxy_node", lambda node: node["name"])
    monkeypatch.setattr(module.remote_proxy, "set_proxy_subscription_selected_node", lambda *args, **kwargs: saves.append((args, kwargs)))
    monkeypatch.setattr(module.remote_proxy, "load_cached_proxy_subscription", lambda _state: SimpleNamespace(nodes=[b]))
    monkeypatch.setattr(module.remote_proxy, "load_proxy_subscription_latencies", lambda _state: {})
    monkeypatch.setattr(module.remote_proxy, "load_proxy_subscription_qualities", lambda _state: {})
    monkeypatch.setattr(module, "show_toast", lambda *_args, **_kwargs: None)

    class Thread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(module.threading, "Thread", Thread)
    state_a = {"active_profile_id": first["id"], "profiles": {first["id"]: first, second["id"]: second}}
    state_b = {**state_a, "active_profile_id": second["id"], "saved_path": "synthetic-b.yaml", "selected_node_key": b.name}
    options(state_a)
    apply_inputs(state_a)
    set_nodes([a])
    prepare(show_message=False, persist_selection=False)
    assert editor.value == "yaml:node-a"

    def switch(state=None):
        state = state_b if state is None else state
        options(state)
        apply_inputs(state)
        set_nodes([])

    def finish():
        while callbacks:
            callbacks.pop(0)()

    return SimpleNamespace(**locals())


def test_switch_subscription_restores_matching_node_without_persisting_or_live_apply(setup):
    s = setup
    s.switch()
    assert s.editor.value == ""
    s.restore(s.state_b, 1)
    s.finish()
    assert s.picker.selected_item().name == "node-b"
    assert s.actual_input() == "yaml:node-b"
    assert "node-b" in s.summaries[-1]
    assert not s.saves


@pytest.mark.parametrize("condition", ["no-cache", "empty", "error"])
def test_switch_without_usable_cache_does_not_reuse_old_node(setup, monkeypatch, condition):
    s = setup
    s.switch()
    state = dict(s.state_b)
    if condition == "no-cache":
        state["saved_path"] = ""
    elif condition == "empty":
        monkeypatch.setattr(s.module.remote_proxy, "load_cached_proxy_subscription", lambda _state: None)
    else:
        def fail(_state):
            raise OSError("synthetic cache failure")
        monkeypatch.setattr(s.module.remote_proxy, "load_cached_proxy_subscription", fail)
    s.restore(state, 1)
    s.finish()
    assert s.actual_input() == ""
    assert not s.picker.selected_item()
    assert "node-a" not in s.summaries[-1]
    assert not s.saves


def test_manual_edit_survives_switch_and_cache_completion_with_clear_priority(setup):
    s = setup
    s.editor.value = "manual: keep this YAML"
    s.switch()
    s.restore(s.state_b, 1)
    s.finish()
    assert s.actual_input() == "manual: keep this YAML"
    assert "手工" in s.summaries[-1] and "优先" in s.summaries[-1]
    assert not s.saves


def test_manual_edit_during_cache_restore_is_not_overwritten(setup):
    s = setup
    s.switch()
    s.restore(s.state_b, 1)
    s.editor.value = "manual: written while reading cache"
    s.finish()
    assert s.actual_input() == "manual: written while reading cache"
    assert "手工" in s.summaries[-1]
    assert not s.saves


def test_stale_cache_generation_cannot_replace_draft_or_picker(setup):
    s = setup
    s.switch()
    s.restore(s.state_b, 1)
    setattr(s.tab, s.generation_attr, 2)
    s.editor.value = "manual: newer context"
    s.finish()
    assert s.actual_input() == "manual: newer context"
    assert not s.picker.selected_item()
    assert not s.saves


@pytest.mark.parametrize("manual", [False, True])
def test_new_profile_only_clears_owned_programmatic_draft(setup, manual):
    s = setup
    if manual:
        s.editor.value = "manual: keep"
    s.new_profile()
    assert s.actual_input() == ("manual: keep" if manual else "")
    assert not s.saves


def test_file_import_identical_to_programmatic_yaml_is_still_manual(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(s.module.filedialog, "askopenfilename", lambda **_kwargs: "synthetic.yaml")
    monkeypatch.setattr(s.module.remote_proxy, "read_proxy_node_text_file", lambda _path: "yaml:node-a")
    monkeypatch.setattr(s.module.remote_proxy, "parse_proxy_node", lambda _text: {"name": "node-a"})
    load_file = s.tab._load_node_file if s.win else s.tab._load_proxy_node_file
    load_file()
    s.switch()
    s.restore(s.state_b, 1)
    s.finish()
    assert s.actual_input() == "yaml:node-a"
    assert "手工" in s.summaries[-1]
    assert not s.saves


@pytest.mark.parametrize("new", [False, True])
def test_lazy_editor_selection_origin_clears_old_summary_on_context_change(setup, new):
    s = setup
    setattr(s.tab, s.prefix + "_node_text", None)
    s.prepare(show_message=False, persist_selection=False)
    assert s.actual_input() == "yaml:node-a"
    if new:
        s.new_profile()
    else:
        s.switch()
    assert s.actual_input() == ""
    assert "node-a" not in s.summaries[-1]
    assert "未选择" in s.summaries[-1]
    assert not s.saves


@pytest.mark.parametrize("missing_picker", [False, True])
def test_restoration_without_selected_node_cannot_keep_successful_old_draft(setup, missing_picker):
    s = setup
    s.picker.item = None
    if missing_picker:
        setattr(s.tab, s.prefix + "_subscription_picker", None)
    s.prepare(show_message=False, persist_selection=False, preserve_manual=True)
    assert s.actual_input() == ""
    assert "未选择" in s.summaries[-1]
    assert not s.saves


@pytest.mark.parametrize("entry", ["selector", "shared", "delete"])
@pytest.mark.parametrize("manual", [False, True])
def test_real_context_entrypoints_preserve_only_manual_drafts(setup, monkeypatch, entry, manual):
    s = setup
    if manual:
        s.editor.value = "manual: independent node"
    if entry == "selector":
        select = s.tab._select_subscription_profile if s.win else s.tab._select_proxy_subscription_profile
        mapping = getattr(s.tab, s.prefix + "_subscription_profile_options")
        label = next(label for label, profile_id in mapping.items() if profile_id == s.second["id"])
        select(label, s.second["id"])
    elif entry == "shared":
        remote_proxy.set_active_proxy_subscription_profile(s.second["id"])
        setattr(s.tab, "_saved_subscription_loaded" if s.win else "_proxy_saved_subscription_loaded", True)
        sync = s.tab._sync_shared_subscription_profile if s.win else s.tab._sync_shared_proxy_subscription_profile
        assert sync()
    else:
        monkeypatch.setattr(s.module, "local_proxy", SimpleNamespace(local_proxy_service_bindings_for_profile=lambda _id: []))
        monkeypatch.setattr(remote_proxy.proxy_routing, "ssh_bindings_for_profile", lambda _id: [])
        monkeypatch.setattr(s.module, "ConfirmDialog", lambda *_args, **kwargs: kwargs["on_confirm"]())
        delete = s.tab._delete_subscription_profile if s.win else s.tab._delete_proxy_subscription_profile
        delete()
    assert s.actual_input() == ("manual: independent node" if manual else "")
    assert ("手工" if manual else "未选择") in s.summaries[-1]
    assert not s.saves
