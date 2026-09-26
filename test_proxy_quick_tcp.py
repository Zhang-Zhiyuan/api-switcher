"""Fast TCP coverage, budgets and cancellation using synthetic/loopback endpoints."""

from contextlib import contextmanager, redirect_stdout
import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from core import remote_proxy


def _node(index, *, host="synthetic.example.invalid", port=443):
    return {"name": f"US-synthetic-{index}", "type": "vless", "server": host,
            "port": port, "uuid": f"00000000-0000-0000-0000-{index:012d}"}


def _addresses(*hosts):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0)) for host in hosts]


def test_quick_deduplicates_endpoints_and_rebinds_every_key(monkeypatch):
    nodes = [_node(index, port=10000 + index % 8) for index in range(96)]
    calls = {"legacy": 0, "quick": 0, "dns": 0}
    lock = threading.Lock()

    @contextmanager
    def connect(*_args, **_kwargs):
        with lock:
            calls["legacy"] += 1
        time.sleep(0.002)
        yield object()

    def resolve(*_args):
        with lock:
            calls["dns"] += 1
        return _addresses("127.0.0.1")

    def quick_connect(*_args):
        with lock:
            calls["quick"] += 1
        time.sleep(0.002)

    monkeypatch.setattr(remote_proxy.socket, "create_connection", connect)
    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(remote_proxy, "_quick_tcp_connect", quick_connect)
    started = time.perf_counter()
    legacy = remote_proxy.measure_proxy_node_latencies(nodes, attempts=3, max_workers=4, require_all=True)
    legacy_time = time.perf_counter() - started
    progress = []
    started = time.perf_counter()
    quick = remote_proxy.measure_proxy_node_latencies(
        nodes, attempts=3, max_workers=4, quick=True, progress_callback=lambda *args: progress.append(args),
    )
    quick_time = time.perf_counter() - started
    assert len(legacy) == len(quick) == len(progress) == 96
    assert calls == {"legacy": 288, "quick": 8, "dns": 1}
    assert {item.node_key for _done, _total, item in progress} == set(quick)
    assert [done for done, _total, _item in progress] == list(range(1, 97))
    assert all(item.ok and item.attempts == 1 and "同端点复用" in item.detail for item in quick.values())
    print(f"synthetic 96 nodes / 8 endpoints: legacy={legacy_time:.3f}s, quick={quick_time:.3f}s, connections=288->8")


def test_quick_real_loopback_connection_and_duplicate_names():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        results = remote_proxy.measure_proxy_node_latencies(
            [_node(index, host="127.0.0.1", port=port) for index in range(4)], quick=True,
        )
    assert len(results) == 4
    assert all(result.ok and result.attempts == 1 for result in results.values())


def test_hung_dns_is_deadline_bounded_and_global_slots_do_not_grow(monkeypatch):
    release = threading.Event()
    returned = threading.Event()
    calls = []
    monkeypatch.setattr(remote_proxy, "_PROXY_LATENCY_DNS_SLOTS", threading.BoundedSemaphore(2))

    def resolve(host, *_args):
        calls.append(host)
        release.wait(3)
        if len(calls) == 2:
            returned.set()
        return _addresses("127.0.0.1")

    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(remote_proxy, "_quick_tcp_connect", lambda *_args: pytest.fail("DNS has not returned"))
    try:
        started = time.monotonic()
        for batch in range(2):
            results = remote_proxy.measure_proxy_node_latencies(
                [_node(index, host=f"dns-{batch}-{index}.example.invalid") for index in range(20)],
                quick=True, timeout=0.2, max_workers=20,
            )
            assert len(results) == 20 and all(not value.ok for value in results.values())
        assert time.monotonic() - started < 1.2
        assert len(calls) == 2  # Repeated batches cannot leak another resolver pool.
    finally:
        release.set()
        assert returned.wait(1)


def test_cancel_during_dns_returns_without_waiting_for_os_resolver(monkeypatch):
    release, entered, returned, cancel = (threading.Event() for _ in range(4))

    def resolve(*_args):
        entered.set()
        release.wait(3)
        returned.set()
        return _addresses("127.0.0.1")

    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", resolve)
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        results = remote_proxy.measure_proxy_node_latencies([_node(1)], quick=True, timeout=3, cancel_event=cancel)
        assert entered.is_set()
        assert time.monotonic() - started < 0.6
        assert all(value.cancelled and value.attempts == 0 for value in results.values())
    finally:
        release.set()
        timer.join(1)
        assert returned.wait(1)


def test_cancellation_preserves_completed_endpoint_and_stops_queued_work(monkeypatch):
    cancel = threading.Event()
    connected = []
    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", lambda *_args: _addresses("127.0.0.1"))
    monkeypatch.setattr(remote_proxy, "_quick_tcp_connect", lambda _addresses, port, *_args: connected.append(port))
    results = remote_proxy.measure_proxy_node_latencies(
        [_node(index, port=10000 + index) for index in range(10)], quick=True, max_workers=1,
        cancel_event=cancel, progress_callback=lambda *_args: cancel.set(),
    )
    assert connected == [10000]
    assert len(results) == 10
    assert sum(value.ok for value in results.values()) == 1
    assert sum(value.cancelled for value in results.values()) == 9


@pytest.mark.parametrize("remote", [False, True])
def test_precancel_starts_no_executor_or_ssh(monkeypatch, remote):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(remote_proxy, "ThreadPoolExecutor", lambda **_kwargs: pytest.fail("no thread pool needed"))
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda *_args: pytest.fail("must not connect"))
    if remote:
        results = remote_proxy.measure_proxy_node_latencies_on_server("synthetic", [_node(1)], quick=True, cancel_event=cancel)
    else:
        results = remote_proxy.measure_proxy_node_latencies([_node(1)], quick=True, cancel_event=cancel)
    assert all(value.cancelled for value in results.values())


class _PendingSocket:
    def __init__(self, *_args):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True

    def setblocking(self, _value):
        pass

    def connect_ex(self, target):
        self.target = target
        return 10035

    def getsockopt(self, *_args):
        return 0


def test_multiple_addresses_race_and_second_address_can_succeed(monkeypatch):
    clock = [10.0]
    sockets = []

    def make_socket(*args):
        sock = _PendingSocket(*args)
        sockets.append(sock)
        return sock

    def ready(_reads, writes, _errors, timeout):
        clock[0] += timeout
        return [], [sock for sock in writes if sock.target[0].endswith("2")], []

    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote_proxy.socket, "socket", make_socket)
    monkeypatch.setattr(remote_proxy.select, "select", ready)
    remote_proxy._quick_tcp_connect(_addresses("192.0.2.1", "192.0.2.2"), 443, 10.4)
    assert 10.0 < clock[0] < 10.2
    assert len(sockets) == 2 and all(sock.closed for sock in sockets)


def test_cancel_interrupts_nonblocking_connect_and_closes_socket(monkeypatch):
    cancel = threading.Event()
    sock = _PendingSocket()
    monkeypatch.setattr(remote_proxy.socket, "socket", lambda *_args: sock)

    def ready(*_args):
        cancel.set()
        return [], [], []

    monkeypatch.setattr(remote_proxy.select, "select", ready)
    with pytest.raises(InterruptedError):
        remote_proxy._quick_tcp_connect(_addresses("192.0.2.1"), 443, time.monotonic() + 3, cancel)
    assert sock.closed


def test_cancelled_cache_is_not_explicitly_unreachable(monkeypatch):
    captured = {}
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_state", lambda **kwargs: captured.update(kwargs) or kwargs)
    result = remote_proxy.ProxyNodeLatencyResult("synthetic-key", False, detail="已取消", cancelled=True)
    remote_proxy.save_proxy_subscription_latencies({"synthetic-key": result})
    monkeypatch.setattr(remote_proxy, "_normalize_proxy_subscription_state", lambda state: state)
    loaded = remote_proxy.load_proxy_subscription_latencies(captured)["synthetic-key"]
    assert loaded["cancelled"] is True
    for value in (result, loaded):
        assert remote_proxy.proxy_node_latency_label(value) == "已取消"
        assert not remote_proxy.proxy_node_latency_ok(value)
        assert not remote_proxy.proxy_node_latency_explicitly_unreachable(value)


def test_ssh_quick_streams_unique_endpoints_progress_and_cancel(monkeypatch):
    cancel = threading.Event()
    sent, progress = [], []
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name, **_kwargs: (None, object()))

    def execute(_client, command, **kwargs):
        batch = json.loads(kwargs["input_data"])
        sent.append(batch)
        assert "QUICK = True" in command and "ATTEMPTS = 1" in command
        assert kwargs["timeout"] <= 15 and kwargs["log_command"] is False
        assert kwargs["cancel_event"] is cancel
        kwargs["stdout_callback"]("".join(
            f"latency\t{item['key']}\t1\t20\tTCP 快速检查\t1\n" for item in batch[:2]
        ))
        assert len(progress) == 6  # Results arrived before the command returns.
        cancel.set()
        raise InterruptedError("synthetic cancellation")

    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    results = remote_proxy.measure_proxy_node_latencies_on_server(
        "synthetic", [_node(index, port=10000 + index % 4) for index in range(12)],
        quick=True, max_workers=2, cancel_event=cancel, progress_callback=lambda *args: progress.append(args),
    )
    assert len(sent) == 1 and len(sent[0]) == 4
    assert len(results) == len(progress) == 12
    assert sum(value.ok for value in results.values()) == 6
    assert sum(value.cancelled for value in results.values()) == 6


@pytest.mark.parametrize("failure", ["command", "missing"])
def test_ssh_quick_keeps_streamed_results_and_terminalizes_every_remaining_key(monkeypatch, failure):
    sent = []
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _name, **_kwargs: (None, object()))

    def execute(_client, _command, **kwargs):
        batch = json.loads(kwargs["input_data"])
        sent.append(batch)
        kwargs["stdout_callback"]("".join(
            f"latency\t{item['key']}\t1\t20\t\t1\n" for item in batch[:2]
        ))
        if failure == "command":
            raise TimeoutError()
        return 0, "", ""

    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    nodes = [_node(index, port=10000 + index) for index in range(5)]
    progress = []
    if failure == "command":
        with pytest.raises(RuntimeError, match="TimeoutError") as captured:
            remote_proxy.measure_proxy_node_latencies_on_server(
                "synthetic", nodes, quick=True, max_workers=2, progress_callback=lambda *args: progress.append(args),
            )
        results = captured.value.partial_results
        assert len(progress) == 5
    else:
        results = remote_proxy.measure_proxy_node_latencies_on_server("synthetic", nodes, quick=True, max_workers=2)
    assert len(results) == 5
    assert len(sent) == 1 and len(sent[0]) == 5
    assert sum(value.ok for value in results.values()) == 2
    assert all(value.ok or value.detail for value in results.values())


def _execute_remote_program(items, timeout=0.2):
    command = remote_proxy._build_remote_latency_command(timeout, 7, 8, quick=True)
    program = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {"__name__": "synthetic_remote_probe", "open": lambda *_args, **_kwargs: io.StringIO(json.dumps(items))}
    output = io.StringIO()
    with redirect_stdout(output):
        exec(compile(program, "<synthetic-remote-latency>", "exec"), namespace)
    return remote_proxy._parse_remote_latency_output(output.getvalue())


def test_generated_quick_script_shares_dns_and_measures_every_input(monkeypatch):
    calls = []

    def resolve(host, *_args):
        calls.append(host)
        return _addresses("127.0.0.1")

    class ConnectedSocket(_PendingSocket):
        def connect_ex(self, target):
            return 0

    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(remote_proxy.socket, "socket", ConnectedSocket)
    monkeypatch.setattr("sys.argv", ["synthetic.py", "input.json"])
    results = _execute_remote_program([
        {"key": str(index), "server": "same.example.invalid", "port": 10000 + index}
        for index in range(8)
    ])
    assert calls == ["same.example.invalid"]
    assert len(results) == 8 and all(result.ok and result.attempts == 1 for result in results.values())


def test_generated_quick_script_hung_dns_does_not_hold_process_batch(monkeypatch):
    release, returned = threading.Event(), threading.Event()

    def resolve(*_args):
        release.wait(3)
        returned.set()
        return _addresses("127.0.0.1")

    monkeypatch.setattr(remote_proxy.socket, "getaddrinfo", resolve)
    monkeypatch.setattr("sys.argv", ["synthetic.py", "input.json"])
    try:
        started = time.monotonic()
        results = _execute_remote_program([{"key": "one", "server": "slow.example.invalid", "port": 443}])
        assert time.monotonic() - started < 0.8
        assert not results["one"].ok and results["one"].detail
    finally:
        release.set()
        assert returned.wait(1)


def test_quick_timeout_handles_nonfinite_values():
    assert remote_proxy._quick_tcp_timeout(float("nan")) == 3.0
    assert remote_proxy._quick_tcp_timeout(float("inf")) == 15.0
    assert remote_proxy._quick_tcp_timeout(-1) == 0.2


def test_quick_endpoint_preserves_case_sensitive_ipv6_zone():
    assert remote_proxy._quick_tcp_endpoint(_node(1, host="[FE80::A%MyNIC]")) == ("fe80::a%MyNIC", 443)
    assert remote_proxy._quick_tcp_endpoint(_node(1, host="Example.INVALID")) == ("example.invalid", 443)


def test_resolver_thread_start_failure_releases_global_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(remote_proxy, "_PROXY_LATENCY_DNS_SLOTS", slots)

    def fail_thread(**_kwargs):
        raise RuntimeError("synthetic thread allocation failure")

    monkeypatch.setattr(remote_proxy.threading, "Thread", fail_thread)
    result = remote_proxy.measure_proxy_node_latency(_node(1), quick=True)
    assert not result.ok and "synthetic thread allocation failure" in result.detail
    assert slots.acquire(blocking=False)
    slots.release()


def test_multi_server_ui_retains_source_failure_and_successful_peer(monkeypatch):
    from ui.tabs.ssh_tab import SSHTab

    def connect(name, **kwargs):
        assert kwargs == {"timeout": 5, "max_retries": 1}
        if name == "unavailable-server":
            raise OSError("synthetic SSH transport failure")
        return None, object()

    def execute(_client, _command, **kwargs):
        items = json.loads(kwargs["input_data"])
        return 0, "".join(f"latency\t{item['key']}\t1\t20\t\t1\n" for item in items), ""

    monkeypatch.setattr(remote_proxy, "_connect_ssh", connect)
    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    nodes = [remote_proxy.ProxySubscriptionNode(index, _node(index)) for index in range(2)]
    tab = object.__new__(SSHTab)
    outcome = tab._measure_proxy_nodes_for_servers(["available-server", "unavailable-server"], nodes, quick=True)
    assert len(outcome["failures"]) == 1
    assert "unavailable-server" in outcome["failures"][0]
    assert all(value.ok for value in outcome["results"]["available-server"].values())
    assert all(not value.ok for value in outcome["results"]["unavailable-server"].values())
    aggregate = tab._aggregate_proxy_latency_results(outcome["results"], 2, nodes)
    assert len(aggregate) == 2 and all(value.ok and "1/2" in value.detail for value in aggregate.values())


def test_quick_ssh_connection_options_do_not_change_legacy_defaults(monkeypatch):
    profile = SimpleNamespace(name="synthetic")
    calls = []

    def connect(value, **kwargs):
        assert value is profile
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(remote_proxy, "profile_manager", SimpleNamespace(list_ssh_profiles=lambda: [profile]))
    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(
        connect=connect, execute_command_with_status=lambda *_args, **_kwargs: (0, "", ""),
    ))
    remote_proxy._connect_ssh("synthetic")
    remote_proxy.measure_proxy_node_latencies_on_server("synthetic", [_node(1)], quick=True)
    assert calls == [{}, {"timeout": 5, "max_retries": 1}]
