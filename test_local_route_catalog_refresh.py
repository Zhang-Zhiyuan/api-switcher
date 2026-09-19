"""Headless event-loop stubs for read-only Win11 routing-overview refreshes."""
import copy
import queue
import threading
from types import MethodType, SimpleNamespace

import pytest

from core import local_proxy, proxy_routing
from ui.tabs.local_proxy_tab import LocalProxyTab


def _catalog(count, name="线路 A", tag="datacenter"):
    return [{"id": "a", "name": name, "network_type": tag,
             "auto_route_candidate_count": count, "auto_route_usable": count > 0,
             "nodes": [{"key": f"node-{index}", "label": f"节点 {index}"} for index in range(count)]}]


class _Tab:
    def __init__(self):
        self._destroyed = False
        self._route_catalog_generation = 0
        self._route_catalog_refresh_after_id = None
        self._route_catalog_refresh_running = False
        self._route_catalog_refresh_pending = False
        self._service_route_catalog = _catalog(1)
        self._preferences_load_generation = 0
        self._subscription_profiles_snapshot = [{"id": "a", "name": "线路 A", "network_type": "datacenter"}]
        self._routing_preferences_snapshot = {"builtin_sites": {"youtube": True}, "service_profile_bindings": {"youtube": "a"}}
        self._timers = {}
        self._timer_counter = 0
        self._ui_queue = queue.Queue()
        self._renders = []
        self._statuses = []
        self._loaded_preferences = []
        self.form = {"name": "尚未保存的改名", "url": "https://draft.example.test/changed", "selection": "manual-node"}
        self._route_overview = SimpleNamespace(set_routes=lambda prefs, catalog: self._renders.append(copy.deepcopy((prefs, catalog))))
        self._set_routing_status = lambda *args: self._statuses.append(args)
        self._apply_proxy_preferences_ui = lambda prefs: self._loaded_preferences.append(prefs)
        self.winfo_exists = lambda: not self._destroyed
        for name in ("_request_route_catalog_refresh", "_start_route_catalog_refresh",
                     "_refresh_service_route_profile_options", "_load_proxy_preferences_ui"):
            setattr(self, name, MethodType(getattr(LocalProxyTab, name), self))

    def after(self, _delay, callback):
        self._timer_counter += 1
        token = str(self._timer_counter)
        self._timers[token] = callback
        return token

    def start_scheduled_refresh(self):
        assert len(self._timers) == 1
        token = next(iter(self._timers))
        self._timers.pop(token)()

    def _run_on_ui_thread(self, callback):
        self._ui_queue.put(callback)

    def finish_one(self):
        self._ui_queue.get(timeout=4)()


@pytest.fixture
def tab(monkeypatch):
    view = _Tab()
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running",
                        lambda *_args, **_kwargs: pytest.fail("catalog refresh must not apply routes"))
    monkeypatch.setattr(local_proxy, "save_local_proxy_preferences",
                        lambda *_args, **_kwargs: pytest.fail("catalog refresh must not save preferences"))
    return view


def test_overview_refresh_is_async_coalesced_and_preserves_unsaved_form(tab, monkeypatch):
    threads = []

    def load():
        threads.append(threading.get_ident())
        return _catalog(3)

    monkeypatch.setattr(proxy_routing, "load_route_catalog", load)
    original = copy.deepcopy((tab.form, tab._routing_preferences_snapshot))
    for _ in range(4):
        tab._request_route_catalog_refresh()
    assert not threads and len(tab._timers) == 1
    tab.start_scheduled_refresh()
    tab.finish_one()
    assert len(threads) == 1 and threads[0] != threading.get_ident()
    assert tab._service_route_catalog[0]["auto_route_candidate_count"] == 3
    assert len(tab._renders) == 1
    assert (tab.form, tab._routing_preferences_snapshot) == original
    assert not tab._route_catalog_refresh_running and not tab._timers


def test_change_during_refresh_discards_stale_result_and_coalesces_followup(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
            return _catalog(2)
        return _catalog(4)

    monkeypatch.setattr(proxy_routing, "load_route_catalog", load)
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    assert entered.wait(4)
    try:
        for _ in range(4):
            tab._request_route_catalog_refresh()
        assert not tab._timers
    finally:
        release.set()
    tab.finish_one()
    assert not tab._renders
    assert tab._service_route_catalog[0]["auto_route_candidate_count"] == 1
    tab.start_scheduled_refresh()
    tab.finish_one()
    assert calls == [1, 2]
    assert tab._service_route_catalog[0]["auto_route_candidate_count"] == 4
    assert len(tab._renders) == 1


def test_older_preference_load_cannot_overwrite_newer_subscription_catalog(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
            return _catalog(1, "旧名称")
        return _catalog(3, "新名称")

    monkeypatch.setattr(local_proxy, "load_local_proxy_preferences", lambda: {"keep_running_on_exit": True})
    monkeypatch.setattr(proxy_routing, "load_route_catalog", load)
    tab._load_proxy_preferences_ui()
    assert entered.wait(4)
    try:
        tab._request_route_catalog_refresh()
        tab.start_scheduled_refresh()
        tab.finish_one()
        assert tab._service_route_catalog == _catalog(3, "新名称")
    finally:
        release.set()
    tab.finish_one()
    assert tab._service_route_catalog == _catalog(3, "新名称")
    assert tab._loaded_preferences == [{"keep_running_on_exit": True}]


def test_rename_and_tag_change_during_read_is_not_undone_by_worker(tab, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
            return _catalog(2)
        return _catalog(2, "新名称", "residential")

    monkeypatch.setattr(proxy_routing, "load_route_catalog", load)
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    assert entered.wait(4)
    try:
        tab._refresh_service_route_profile_options([{"id": "a", "name": "新名称", "network_type": "residential"}])
        assert tab._service_route_catalog[0]["name"] == "新名称"
        assert tab._service_route_catalog[0]["auto_route_candidate_count"] == 1
    finally:
        release.set()
    tab.finish_one()
    assert tab._service_route_catalog[0]["name"] == "新名称"
    tab.start_scheduled_refresh()
    tab.finish_one()
    assert tab._service_route_catalog == _catalog(2, "新名称", "residential")


def test_metadata_only_refresh_preserves_count_without_starting_cache_io(tab):
    tab._service_route_catalog = _catalog(5)
    tab._refresh_service_route_profile_options([{"id": "a", "name": "改名", "network_type": "residential"}])
    assert tab._service_route_catalog[0]["auto_route_candidate_count"] == 5
    assert tab._service_route_catalog[0]["auto_route_usable"] is True
    assert not tab._timers and not tab._route_catalog_refresh_running


def test_failed_catalog_refresh_keeps_old_snapshot_and_allows_retry(tab, monkeypatch):
    def fail():
        raise TimeoutError()

    monkeypatch.setattr(proxy_routing, "load_route_catalog", fail)
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    tab.finish_one()
    assert not tab._route_catalog_refresh_running
    assert tab._service_route_catalog == _catalog(1)
    assert "TimeoutError" in tab._statuses[-1][0]
    monkeypatch.setattr(proxy_routing, "load_route_catalog", lambda: _catalog(2))
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    tab.finish_one()
    assert tab._service_route_catalog == _catalog(2)


def test_thread_start_failure_does_not_stick_refresh_state(tab, monkeypatch):
    def fail(_thread):
        raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail)
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    assert not tab._route_catalog_refresh_running
    assert tab._route_catalog_refresh_after_id is None
    assert "未启动" in tab._statuses[-1][0]


def test_destroyed_tab_discards_pending_refresh_without_rendering(tab, monkeypatch):
    monkeypatch.setattr(proxy_routing, "load_route_catalog", lambda: _catalog(3))
    tab._request_route_catalog_refresh()
    tab.start_scheduled_refresh()
    tab._destroyed = True
    tab.finish_one()
    assert not tab._renders
    assert tab._service_route_catalog == _catalog(1)
    tab._request_route_catalog_refresh()
    assert not tab._timers
