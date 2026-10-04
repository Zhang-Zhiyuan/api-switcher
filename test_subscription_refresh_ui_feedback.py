"""Structured refresh feedback and timer hints without Tk, network, or user data."""
from types import SimpleNamespace

import pytest

from core import remote_proxy, subscription_auto_refresh as worker
from test_subscription_auto_refresh import InlineThread, timer as _timer_fixture


timer = _timer_fixture  # Reuse the synthetic local/SSH fixture, never a native Tk root.


def payload(**changes):
    return {"results": {}, "errors": [], "apply_messages": [], "steps": [],
            "retryable": False, "downloaded_count": 0, "reused_count": 0,
            "waiting_count": 0, **changes}


def returns(monkeypatch, result):
    monkeypatch.setattr(worker, "refresh_saved_subscriptions", lambda *_args, **_kwargs: result)


def test_saved_interval_is_sent_to_worker_without_reading_dirty_editor(timer):
    class Draft:
        def get(self):
            pytest.fail("background refresh must not read draft interval")

    timer.calls.dirty = True
    setattr(timer.tab, timer.prefix + "periodic_update_entry", Draft())
    setattr(timer.tab, timer.prefix + "periodic_update_interval_saved", 45)
    timer.start()
    assert timer.calls.worker == [(timer.kind, {
        "server_names": () if timer.kind == "local" else ("confirmed",), "interval_seconds": 2700,
    })]
    assert timer.calls.refresh == [{"preserve_editor": True}]
    assert not timer.calls.nodes


def test_deferred_worker_uses_interval_snapshot_but_preserves_latest_editor_draft(timer):
    pending = []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    timer.start(DeferredThread)
    setattr(timer.tab, timer.prefix + "periodic_update_interval_saved", 45)
    timer.calls.dirty = True
    pending.pop()()
    timer.calls.after.pop(next(iter(timer.calls.after)))()
    assert timer.calls.worker[0][1]["interval_seconds"] == 1800
    assert not timer.calls.nodes
    assert timer.calls.delays[-1] == 2700000


def test_old_worker_hint_does_not_override_newly_confirmed_interval(timer, monkeypatch):
    pending = []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    returns(monkeypatch, payload(next_delay_seconds=5, waiting_count=1))
    timer.start(DeferredThread)
    setattr(timer.tab, timer.prefix + "periodic_update_interval_saved", 45)
    pending.pop()()
    timer.calls.after.pop(next(iter(timer.calls.after)))()
    assert timer.calls.delays[-1] == 2700000


@pytest.mark.parametrize("hint, expected_ms", [(0.25, 1000), (1.5, 1500), (123.4, 123400), (999999, 86400000)])
def test_valid_hint_uses_seconds_and_replaces_existing_timer(timer, hint, expected_ms):
    timer.schedule()
    timer.schedule(retry=True, delay_seconds=hint)
    assert timer.calls.delays[-1] == expected_ms
    assert len(timer.calls.after) == 1


@pytest.mark.parametrize("hint", [None, 0, -1, float("nan"), float("inf"), "invalid", True, False, {}])
@pytest.mark.parametrize("options, expected_ms", [({}, 1800000), ({"initial": True}, 60000), ({"retry": True}, 60000)])
def test_invalid_hint_keeps_existing_initial_retry_and_saved_interval_defaults(timer, hint, options, expected_ms):
    timer.schedule(**options, delay_seconds=hint)
    assert timer.calls.delays == [expected_ms]


def test_retained_route_is_warning_from_structure_not_message_keywords(timer, monkeypatch):
    returns(monkeypatch, payload(
        steps=[{"stage": "apply", "outcome": "retained", "message": "沿用既有线路",
                "warning": True, "retryable": False}],
        reused_count=2, waiting_count=1, next_delay_seconds=95,
    ))
    timer.start()
    text, severity = timer.calls.status[-1]
    assert severity == "warning" and "沿用既有线路" in text
    assert "下载 0" in text and "复用缓存 2" in text and "等待 1" in text
    assert timer.calls.delays[-1] == 95000


def test_structured_success_does_not_classify_chinese_substrings_as_failure(timer, monkeypatch):
    returns(monkeypatch, payload(
        steps=[{"stage": "apply", "outcome": "applied", "message": "已清除历史失败记录",
                "warning": False, "retryable": False}],
        apply_messages=["已清除历史失败记录"], downloaded_count=1,
    ))
    timer.start()
    assert timer.calls.status[-1][1] == "info"
    assert timer.calls.delays[-1] == 1800000


def test_nonretryable_structured_error_does_not_trigger_legacy_short_retry(timer, monkeypatch):
    returns(monkeypatch, payload(errors=["synthetic permanent error"], retryable=False))
    timer.start()
    assert timer.calls.status[-1][1] == "warning"
    assert timer.calls.delays[-1] == 1800000


def test_structured_retry_hint_controls_next_tick_even_without_errors(timer, monkeypatch):
    returns(monkeypatch, payload(retryable=True, waiting_count=1, next_delay_seconds=180))
    timer.start()
    assert timer.calls.delays[-1] == 180000


def test_legacy_payload_does_not_pass_new_hint_keyword_to_old_scheduler(timer, monkeypatch):
    scheduled = []

    def old_schedule(*, retry=False):
        scheduled.append(retry)

    name = "_schedule_periodic_update" if timer.kind == "local" else "_schedule_proxy_periodic_update"
    setattr(timer.tab, name, old_schedule)
    returns(monkeypatch, {"results": {}, "errors": ["synthetic error"], "apply_messages": []})
    timer.start()
    assert scheduled == [True]


def test_legacy_apply_message_is_shown_conservatively_without_parsing_text(timer, monkeypatch):
    returns(monkeypatch, {"results": {}, "errors": [], "apply_messages": ["沿用既有线路"]})
    timer.start()
    assert timer.calls.status[-1][1] == "warning"
    assert "沿用既有线路" in timer.calls.status[-1][0]
    assert timer.calls.delays[-1] == 1800000


@pytest.mark.parametrize("hint", [None, 0, -2, float("nan"), float("inf"), True])
def test_invalid_payload_hint_is_not_forwarded_to_scheduler(timer, monkeypatch, hint):
    scheduled = []
    name = "_schedule_periodic_update" if timer.kind == "local" else "_schedule_proxy_periodic_update"
    setattr(timer.tab, name, lambda **kwargs: scheduled.append(kwargs))
    returns(monkeypatch, payload(next_delay_seconds=hint))
    timer.start()
    assert scheduled == [{"retry": False}]


@pytest.mark.parametrize("stage", ["refresh", "payload"])
def test_ui_finish_failure_overrides_nonretryable_payload_and_drops_old_hint(timer, monkeypatch, stage):
    result = payload(results={"a": SimpleNamespace(nodes=("cached",))}, reused_count=1,
                     retryable=False, next_delay_seconds=86400)
    if stage == "payload":
        result = None
    else:
        name = "_refresh_subscription_profile_options" if timer.kind == "local" else "_refresh_proxy_subscription_profile_options"

        def fail(**_kwargs):
            raise RuntimeError("synthetic UI synchronization failure")

        setattr(timer.tab, name, fail)
    returns(monkeypatch, result)
    timer.start()
    assert timer.calls.busy[-1] is False
    assert timer.calls.delays[-1] == 60000
    assert timer.calls.status[-1][1] == "warning"


@pytest.mark.parametrize("result", [None, {"errors": object()}, payload(next_delay_seconds=86400)])
def test_reservation_release_failure_always_reaches_completion_and_short_retry(timer, monkeypatch, result):
    returns(monkeypatch, result)

    def fail_release():
        raise RuntimeError("synthetic reservation release failure")

    monkeypatch.setattr(remote_proxy, "release_proxy_subscription_hot_update", fail_release)
    timer.start()
    assert timer.calls.busy[-1] is False
    assert timer.calls.delays[-1] == 60000
    assert "synthetic reservation release failure" in timer.calls.status[-1][0]
