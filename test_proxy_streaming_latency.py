"""Single-command SSH quick tests, with synthetic streams and remote workers."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from core import remote_proxy


def nodes(count):
    return [{"name": f"synthetic-{index}", "type": "vless", "server": "nodes.example.invalid",
             "port": 20000 + index, "uuid": f"00000000-0000-0000-0000-{index:012d}"}
            for index in range(count)]


def row(item, delay=21):
    return f"latency\t{item['key']}\t1\t{delay}\t节点可连\t1\n"


@pytest.mark.parametrize("end", ["ok", "nonzero", "timeout"])
def test_partial_lines_unknown_keys_duplicates_and_completion(monkeypatch, end):
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda *_args, **_kwargs: (None, object()))
    progress, commands = [], []

    def execute(_client, _command, **kwargs):
        items = json.loads(kwargs["input_data"])
        commands.append(items)
        payload = row(items[1]) + row(items[1], 999) + row({"key": "foreign"})
        for char in payload:
            kwargs["stdout_callback"](char)
        assert len(progress) == 1 and progress[0][2].latency_ms == 21
        if end == "timeout":
            raise TimeoutError("synthetic timeout")
        kwargs["stdout_callback"](row(items[0]))
        return (9, "", "synthetic error") if end == "nonzero" else (0, "", "")

    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    kwargs = {"quick": True, "max_workers": 2, "progress_callback": lambda *args: progress.append(args)}
    if end == "ok":
        results = remote_proxy.measure_proxy_node_latencies_on_server("synthetic", nodes(5), **kwargs)
    else:
        with pytest.raises(RuntimeError) as exc:
            remote_proxy.measure_proxy_node_latencies_on_server("synthetic", nodes(5), **kwargs)
        results = exc.value.partial_results
    assert len(commands) == 1 and len(commands[0]) == 5
    assert len(results) == len(progress) == 5
    assert sum(result.ok for result in results.values()) == (1 if end == "timeout" else 2)
    assert all(result.ok or result.incomplete for result in results.values())
    assert [event[0] for event in progress] == list(range(1, 6))


def test_unterminated_oversized_record_preserves_prior_results(monkeypatch):
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda *_args, **_kwargs: (None, object()))

    def execute(_client, _command, **kwargs):
        items = json.loads(kwargs["input_data"])
        kwargs["stdout_callback"](row(items[0]))
        kwargs["stdout_callback"]("x" * 4097)

    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    with pytest.raises(RuntimeError, match="上限") as exc:
        remote_proxy.measure_proxy_node_latencies_on_server("synthetic", nodes(3), quick=True)
    assert sum(value.ok for value in exc.value.partial_results.values()) == 1
    assert len(exc.value.partial_results) == 3


def run_dispatch(items, measure, emit):
    command = remote_proxy._build_remote_latency_command(0.2, 1, 2, quick=True)
    program = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    marker = "workers = max(1, min(MAX_WORKERS, len(nodes) or 1))"
    assert program.count(marker) == 1
    program = program.replace(marker, "measure = synthetic_measure\n" + marker)
    namespace = {
        "__name__": "synthetic_remote_dispatch", "synthetic_measure": measure, "print": emit,
        "open": lambda *_args, **_kwargs: io.StringIO(json.dumps(items)),
    }
    exec(compile(program, "<synthetic-remote-dispatch>", "exec"), namespace)


def test_remote_free_slot_refills_before_slow_peer_finishes(monkeypatch):
    monkeypatch.setattr("sys.argv", ["synthetic", "input.json"])
    slow_started, third_started = threading.Event(), threading.Event()
    output = []

    def measure(item):
        if item["key"] == "0":
            slow_started.set()
            assert third_started.wait(2), "a slow node blocked the next worker slot"
        elif item["key"] == "1":
            assert slow_started.wait(2)
        else:
            third_started.set()
        return item["key"], 1, "20", "", 0

    run_dispatch([{"key": str(index)} for index in range(3)], measure, lambda text, **_: output.append(text))
    assert len(output) == 3 and output[0].startswith("latency\t1\t")
    assert third_started.is_set()


def test_remote_disconnect_does_not_launch_entire_remaining_queue(monkeypatch):
    monkeypatch.setattr("sys.argv", ["synthetic", "input.json"])
    measured = []

    def measure(item):
        measured.append(item["key"])
        return item["key"], 1, "20", "", 0

    def disconnected(*_args, **_kwargs):
        raise BrokenPipeError("synthetic SSH channel closed")

    with pytest.raises(BrokenPipeError):
        run_dispatch([{"key": str(index)} for index in range(1000)], measure, disconnected)
    assert 1 <= len(measured) <= 2
