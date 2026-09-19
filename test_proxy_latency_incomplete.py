"""Detector failures are terminal UI results, not failed endpoint verdicts."""

from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import errno
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy
from ui.theme import COLORS
from ui.widgets import proxy_node_picker


def _node(index=1, node_type="http"):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": f"US-synthetic-{index}", "type": node_type,
        "server": f"node-{index}.example.test", "port": 443,
    })


def _assert_incomplete(result):
    assert remote_proxy.proxy_node_latency_incomplete(result)
    assert not remote_proxy.proxy_node_latency_ok(result)
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(result)
    assert remote_proxy.proxy_node_latency_label(result) == "未完成"


@pytest.mark.parametrize("as_dict", [False, True])
def test_incomplete_has_distinct_verdict_and_cannot_reuse_a_latency(as_dict):
    values = dict(node_key="synthetic", ok=True, latency_ms=10, incomplete=True)
    result = values if as_dict else remote_proxy.ProxyNodeLatencyResult(**values)
    _assert_incomplete(result)
    assert remote_proxy.proxy_node_latency_ms(result) is None
    assert not remote_proxy.proxy_node_latency_invalid(result)


@pytest.mark.parametrize("flag", ["true", "false", 1, None, []])
def test_incomplete_requires_real_boolean_in_cache(flag):
    result = {"ok": False, "incomplete": flag}
    assert remote_proxy.proxy_node_latency_invalid(result)
    assert remote_proxy.proxy_node_latency_label(result) == "结果无效"


def test_cancelled_takes_precedence_without_double_counting():
    result = remote_proxy.ProxyNodeLatencyResult("synthetic", False, cancelled=True, incomplete=True)
    assert remote_proxy.proxy_node_latency_cancelled(result)
    assert not remote_proxy.proxy_node_latency_incomplete(result)
    assert result.label() == "已取消"
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(result)


def test_legacy_failed_cache_with_zero_attempts_is_not_reinterpreted():
    record = {"ok": False, "attempts": 0, "measured_at": datetime.now(timezone.utc).isoformat()}
    assert not remote_proxy.proxy_node_latency_incomplete(record)
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(record)
    assert remote_proxy.proxy_node_latency_label(record) == "不可连"


def test_incomplete_json_cache_roundtrip_preserves_state(monkeypatch):
    node = _node()
    key = remote_proxy.proxy_subscription_node_key(node)
    result = remote_proxy.ProxyNodeLatencyResult(key, False, incomplete=True, detail="synthetic detector unavailable")

    def save(profile_id, **values):
        return {"id": profile_id, **values}

    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_profile_state", save)
    saved = remote_proxy.save_proxy_subscription_latencies({key: result}, profile_id="synthetic-profile")
    restored = remote_proxy.load_proxy_subscription_latencies(json.loads(json.dumps(saved)))

    _assert_incomplete(restored[key])
    assert restored[key]["measured_at"] == result.measured_at
    assert restored[key]["detail"] == result.detail


@pytest.mark.parametrize("quick", [False, True])
def test_tcp_executor_failure_is_not_a_node_failure(monkeypatch, quick):
    nodes = [_node(1), _node(2)]

    def failed_executor(**_kwargs):
        raise RuntimeError("synthetic thread creation failure")

    monkeypatch.setattr(remote_proxy, "ThreadPoolExecutor", failed_executor)
    results = remote_proxy.measure_proxy_node_latencies(nodes, quick=quick)

    assert len(results) == 2
    for result in results.values():
        _assert_incomplete(result)
        assert result.attempts == 0


@pytest.mark.parametrize("error,expected_incomplete", [
    (ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refused"), False),
    (TimeoutError("synthetic timed out"), False),
    (RuntimeError("synthetic detector failure"), True),
    (OSError(errno.EMFILE, "synthetic descriptor exhaustion"), True),
])
@pytest.mark.parametrize("quick", [False, True])
def test_real_tcp_refusal_timeout_stay_unreachable_but_resource_errors_do_not(monkeypatch, error, expected_incomplete, quick):
    def fail(*_args, **_kwargs):
        raise error

    if quick:
        monkeypatch.setattr(remote_proxy._QuickTCPResolver, "resolve", lambda *_args: [])
        monkeypatch.setattr(remote_proxy, "_quick_tcp_connect", fail)
    else:
        monkeypatch.setattr(remote_proxy.socket, "create_connection", fail)
    result = remote_proxy.measure_proxy_node_latency(_node().node, attempts=1, quick=quick)

    assert remote_proxy.proxy_node_latency_incomplete(result) is expected_incomplete
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(result) is not expected_incomplete


def test_ssh_unavailable_returns_incomplete_partial_results_for_every_node(monkeypatch):
    nodes = [_node(1), _node(2)]

    def disconnected(*_args, **_kwargs):
        raise OSError("synthetic SSH connection unavailable")

    monkeypatch.setattr(remote_proxy, "_connect_ssh", disconnected)
    reports = []
    with pytest.raises(RuntimeError) as caught:
        remote_proxy.measure_proxy_node_latencies_on_server(
            "synthetic", nodes, quick=True, progress_callback=lambda *_args: reports.append(_args[-1]),
        )

    assert len(caught.value.partial_results) == len(reports) == 2
    for result in reports:
        _assert_incomplete(result)
        assert result.attempts == 0


@pytest.mark.parametrize("quick", [False, True])
def test_ssh_missing_output_is_incomplete_and_keeps_real_success(monkeypatch, quick):
    first, missing = _node(1), _node(2)
    key = remote_proxy.proxy_subscription_node_key(first)
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda *_args, **_kwargs: (None, object()))
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", lambda *_args, **_kwargs:
                        (0, f"latency\t{key}\t1\t23\t\t1\n", ""))

    result = remote_proxy.measure_proxy_node_latencies_on_server("synthetic", [first, missing], quick=quick)

    assert remote_proxy.proxy_node_latency_ok(result[key])
    _assert_incomplete(result[remote_proxy.proxy_subscription_node_key(missing)])


@pytest.mark.parametrize("quick", [False, True])
def test_missing_mihomo_is_incomplete_for_all_nodes(monkeypatch, quick):
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)

    def unavailable():
        raise RuntimeError("synthetic missing core")

    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", unavailable)
    results = local_proxy.measure_proxy_node_data_plane_latencies([_node(1), _node(2)], quick=quick)

    assert len(results) == 2
    for result in results.values():
        _assert_incomplete(result)
        assert result.attempts == 0


@pytest.mark.parametrize("error,expected_incomplete", [
    (RuntimeError("synthetic invalid controller record"), True),
    (ConnectionRefusedError("synthetic controller not listening"), True),
    (local_proxy._IsolatedMihomoProbeFailed("synthetic probe returned non-204"), False),
])
def test_mihomo_controller_failure_and_actual_probe_failure_are_distinct(monkeypatch, error, expected_incomplete):
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic.exe"))
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())

    @contextmanager
    def session(*_args, **_kwargs):
        yield SimpleNamespace(route_names=("synthetic",))

    def probe(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_batch_session", session)
    monkeypatch.setattr(local_proxy, "_probe_isolated_mihomo_batch_delay", probe)
    result = next(iter(local_proxy.measure_proxy_node_data_plane_latencies([_node()], quick=True).values()))

    assert remote_proxy.proxy_node_latency_incomplete(result) is expected_incomplete
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(result) is not expected_incomplete


def test_incomplete_sorts_as_unknown_ahead_of_confirmed_failure():
    failed, unfinished = _node(1), _node(2)
    values = {
        remote_proxy.proxy_subscription_node_key(failed): remote_proxy.ProxyNodeLatencyResult("failed", False),
        remote_proxy.proxy_subscription_node_key(unfinished): remote_proxy.ProxyNodeLatencyResult("unfinished", False, incomplete=True),
    }

    assert remote_proxy.sort_proxy_subscription_nodes([failed, unfinished], values) == (unfinished, failed)
    assert remote_proxy.best_proxy_subscription_node_by_latency([unfinished], values) is None


def test_picker_incomplete_filter_color_and_expiry_are_distinct():
    from test_proxy_picker_expiry import matches, picker

    unfinished = {"ok": False, "incomplete": True, "measured_at": "2000-01-01T00:00:00Z"}
    failed = {"ok": False, "measured_at": datetime.now(timezone.utc).isoformat()}
    obj = picker([unfinished, failed, None])

    assert "未完成" in proxy_node_picker.ProxyNodePicker.FILTER_OPTIONS
    assert matches(obj, "未完成") == obj._nodes[:1]
    assert matches(obj, "不可连") == obj._nodes[1:2]
    assert matches(obj, "未测速") == obj._nodes[2:]
    assert matches(obj, "已过期") == []
    assert obj._summary_counts["incomplete"] == 1
    assert obj._metadata_for(obj._nodes[0])["latency_label"] == "未完成"
    assert obj._latency_color(unfinished) == COLORS["warning"]
    assert proxy_node_picker._latency_transition_at(unfinished, 0) is None


@pytest.mark.parametrize("row", [
    "latency\tsynthetic\t1\tNaN\tdetail\t1",
    "latency\tsynthetic\t1\tinf\tdetail\t1",
    "latency\tsynthetic\t1\t0\tdetail\t1",
    "latency\tsynthetic\tmaybe\t\tdetail\t1",
    "latency\tsynthetic\t0\tNaN\tdetail\t1",
    "latency\tsynthetic\t0\t\tdetail",
    "latency\tsynthetic\t0\t\tdetail\tNaN",
    "latency\tsynthetic\t0\t\tdetail\t-1",
    "latency\tsynthetic\t0\t\tdetail\t1\tmaybe",
    "latency\tsynthetic\t0\t\tdetail\t1\t0\textra",
])
def test_bad_remote_rows_are_ignored_and_cannot_replace_valid_result(row):
    assert remote_proxy._parse_remote_latency_output(row) == {}
    good = "latency\tsynthetic\t1\t23\tcomplete\t1"
    for text in (good + "\n" + row, row + "\n" + good):
        parsed = remote_proxy._parse_remote_latency_output(text)
        assert parsed["synthetic"].ok
        assert parsed["synthetic"].latency_ms == 23


def test_remote_protocol_supports_legacy_failure_and_explicit_incomplete():
    parsed = remote_proxy._parse_remote_latency_output(
        "latency\tlegacy\t0\t\trefused\t0\n"
        "latency\tnew\t0\t\tresource failure\t0\t1\n"
    )
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(parsed["legacy"])
    _assert_incomplete(parsed["new"])


@pytest.mark.parametrize("quick", [False, True])
@pytest.mark.parametrize("error,incomplete", [
    (OSError(errno.EMFILE, "synthetic exhausted descriptors"), True),
    (MemoryError("synthetic memory unavailable"), True),
    (ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refused"), False),
    (TimeoutError("synthetic timeout"), False),
])
def test_generated_remote_script_emits_incomplete_for_resource_failure(monkeypatch, quick, error, incomplete):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(remote_proxy.socket, "socket", fail)
    monkeypatch.setattr(remote_proxy.socket, "create_connection", fail)
    monkeypatch.setattr("sys.argv", ["synthetic.py", "synthetic.json"])
    command = remote_proxy._build_remote_latency_command(0.2, 1, 1, quick=quick)
    program = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    items = [{"key": "synthetic", "server": "127.0.0.1", "port": 9}]
    namespace = {"open": lambda *_args, **_kwargs: io.StringIO(json.dumps(items))}
    output = io.StringIO()
    with redirect_stdout(output):
        exec(compile(program, "<isolated-remote-latency-protocol>", "exec"), namespace)

    assert len(output.getvalue().strip().split("\t")) == 7
    result = remote_proxy._parse_remote_latency_output(output.getvalue())["synthetic"]
    assert result.incomplete is incomplete
    assert remote_proxy.proxy_node_latency_explicitly_unreachable(result) is not incomplete
    assert result.attempts == (0 if incomplete else 1)
