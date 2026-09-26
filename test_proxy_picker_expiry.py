"""Picker freshness transitions without GUI timers, user files or network."""
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import pytest

from core import remote_proxy
from ui.theme import COLORS
from ui.widgets import proxy_node_picker as module


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2030, 1, 1, tzinfo=timezone.utc), "reads": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"] if tz is not None else state["now"].replace(tzinfo=None)

    def read():
        state["reads"] += 1
        return state["now"].timestamp()

    monkeypatch.setattr(remote_proxy, "datetime", Clock)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=read, monotonic=time.monotonic))
    return state


def record(clock, *, ok=True, age=0, cancelled=False):
    return {"ok": ok and not cancelled, "latency_ms": 23 if ok and not cancelled else None,
            "measured_at": (clock["now"] - timedelta(seconds=age)).isoformat(),
            "attempts": 1, "detail": "synthetic", "cancelled": cancelled}


def picker(records):
    obj = object.__new__(module.ProxyNodePicker)
    obj._nodes = [remote_proxy.ProxySubscriptionNode(index, {
        "name": f"US-synthetic-{index}", "type": "http", "server": f"node-{index}.example.test", "port": 8080,
    }) for index in range(len(records))]
    obj._latency_results = {remote_proxy.proxy_subscription_node_key(item): value
                            for item, value in zip(obj._nodes, records) if value is not None}
    obj._quality_results = {}
    obj._node_meta = {}
    obj._metadata_version = 0
    obj._metadata_checked_at = None
    obj._metadata_next_expiry = None
    obj._filter_cache_key = None
    obj._filter_cache_nodes = ()
    obj._visible_checkboxes = {}
    obj._summary_label = None
    obj._search_entry = obj._region_combo = obj._quality_combo = None
    mode = {"value": "全部"}
    obj._filter_combo = SimpleNamespace(get=lambda: mode["value"])
    obj.mode = mode
    obj._build_node_metadata()
    return obj


def matches(obj, name):
    obj.mode["value"] = name
    return obj._filtered_nodes()


def test_fresh_failed_expired_cancelled_and_missing_have_distinct_filters(clock):
    obj = picker([record(clock), record(clock, ok=False), record(clock, age=1801),
                  record(clock, ok=False, age=1801), record(clock, cancelled=True), None])
    assert matches(obj, "可连") == obj._nodes[:1]
    assert matches(obj, "不可连") == obj._nodes[1:2]
    assert matches(obj, "已过期") == obj._nodes[2:4]
    assert matches(obj, "已取消") == obj._nodes[4:5]
    assert matches(obj, "未测速") == obj._nodes[5:]
    assert obj._summary_counts["ok"] == 1 and obj._summary_counts["expired"] == 2
    for item in obj._nodes[2:4]:
        meta = obj._metadata_for(item)
        assert meta["latency_label"] == "已过期" and not meta["latency_unreachable"]
        assert obj._row_presentation(item)[4] == COLORS["muted_soft"]


def test_expiry_invalidates_cached_filter_text_search_and_summary_on_interaction(clock):
    obj = picker([record(clock, age=1799)])
    assert matches(obj, "可连") == obj._nodes
    version = obj._metadata_version
    clock["now"] += timedelta(seconds=2)
    assert matches(obj, "可连") == []
    assert obj._metadata_version == version + 1
    assert matches(obj, "不可连") == []
    assert matches(obj, "已过期") == obj._nodes
    assert obj._metadata_for(obj._nodes[0])["latency_label"] == "已过期"
    assert obj._summary_counts["ok"] == 0 and obj._summary_counts["expired"] == 1
    obj._search_entry = SimpleNamespace(get=lambda: "已过期")
    assert matches(obj, "全部") == obj._nodes
    assert obj._metadata_version == version + 1  # no repeated full rebuild


def test_only_earliest_expiry_rebuilds_then_tracks_next_boundary(clock):
    obj = picker([record(clock, age=1799), record(clock, age=1200)])
    version = obj._metadata_version
    clock["now"] += timedelta(seconds=2)
    assert len(matches(obj, "可连")) == 1
    assert obj._metadata_version == version + 1
    for _ in range(30):
        matches(obj, "可连")
    assert obj._metadata_version == version + 1
    clock["now"] += timedelta(seconds=600)
    assert matches(obj, "可连") == []
    assert obj._metadata_version == version + 2
    assert obj._metadata_next_expiry is None


def test_cached_per_row_access_does_not_poll_clock_or_create_timers(clock):
    obj = picker([record(clock) for _ in range(1000)])
    reads = clock["reads"]
    for item in obj._nodes:
        obj._metadata_for(item)
        obj._row_presentation(item)
    assert clock["reads"] == reads
    version = obj._metadata_version
    assert len(matches(obj, "可连")) == 1000
    assert len(matches(obj, "可连")) == 1000
    assert clock["reads"] == reads + 2
    assert obj._metadata_version == version


def test_tab_activation_revalidates_completed_view_without_changing_selection(clock):
    obj = picker([record(clock, age=1799)])
    obj._render_deferred = False
    obj._selected_key = remote_proxy.proxy_subscription_node_key(obj._nodes[0])
    obj._checked_keys = {obj._selected_key}
    renders = []
    obj._request_render_nodes = renders.append
    obj._resume_background_work()
    assert not renders
    clock["now"] += timedelta(seconds=2)
    # Inactivity itself performs no work; no timer was installed.
    assert obj._summary_counts["ok"] == 1
    obj._resume_background_work()
    assert renders == [20] and obj._summary_counts["ok"] == 0
    assert obj._selected_key and obj._checked_keys == {obj._selected_key}
    obj._resume_background_work()
    assert renders == [20]


def test_backward_clock_adjustment_revalidates_expired_cache(clock):
    obj = picker([record(clock, age=1801)])
    assert matches(obj, "已过期") == obj._nodes
    clock["now"] -= timedelta(seconds=10)
    assert matches(obj, "可连") == obj._nodes
    assert matches(obj, "已过期") == []


def test_future_timestamp_transitions_into_core_tolerance_on_interaction(clock):
    obj = picker([record(clock, age=-600)])
    assert matches(obj, "可连") == []
    clock["now"] += timedelta(seconds=301)
    assert matches(obj, "可连") == obj._nodes


@pytest.mark.parametrize("broken", [float("inf"), -1, True, "not-a-number"])
def test_invalid_latency_is_unmeasured_not_failed_or_expired(clock, broken):
    value = record(clock)
    value["latency_ms"] = broken
    obj = picker([value])
    assert matches(obj, "不可连") == matches(obj, "可连") == matches(obj, "已过期") == []
    assert matches(obj, "未测速") == obj._nodes
    assert obj._metadata_for(obj._nodes[0])["latency_label"] == "结果无效"
    assert obj._row_presentation(obj._nodes[0])[4] == COLORS["muted_soft"]


def test_bad_or_absent_timestamp_never_enters_unreachable(clock):
    values = [record(clock, ok=False), record(clock, ok=False)]
    values[0]["measured_at"] = "invalid-date"
    values[1].pop("measured_at")
    obj = picker(values)
    assert matches(obj, "不可连") == matches(obj, "可连") == []
    assert obj._metadata_next_expiry is None
