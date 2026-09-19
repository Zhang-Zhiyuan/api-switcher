"""Deterministic local/generated-SSH address races, with no real network."""

import ast
import errno
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from core import remote_proxy


def _address(host):
    if ":" in host:
        return socket.AF_INET6, socket.SOCK_STREAM, 6, "", (host, 0, 0, 0)
    return socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0)


@pytest.fixture(params=["local", "remote"])
def race(request, monkeypatch):
    namespace = None
    if request.param == "remote":
        command = remote_proxy._build_remote_latency_command(3.0, quick=True)
        program = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        program = program.split("\nworkers = max(", 1)[0]
        parsed = ast.parse(program)
        # Define the exact shipped helper but omit its stdin/file loading.
        parsed.body = [node for node in parsed.body if isinstance(
            node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef),
        )]
        namespace = {}
        exec(compile(parsed, "<isolated-remote-address-race>", "exec"), namespace)

    state = SimpleNamespace(
        backend=request.param, clock=0.0, specs={}, created=[], peak=0,
        on_select=None, namespace=namespace,
    )

    class ProbeSocket:
        def __init__(self, family, *_args):
            self.family = family
            self.target = None
            self.started = state.clock
            self.close_calls = 0
            self.closed_at = None
            state.created.append(self)
            state.peak = max(state.peak, sum(sock.close_calls == 0 for sock in state.created))

        def setblocking(self, flag):
            assert flag is False

        def connect_ex(self, target):
            self.target = target
            return state.specs.get(target[0], {}).get("immediate", errno.EINPROGRESS)

        def getsockopt(self, *_args):
            return state.specs.get(self.target[0], {}).get("error", 0)

        def close(self):
            self.close_calls += 1
            self.closed_at = state.clock
            if state.specs.get(self.target[0], {}).get("close_raises"):
                raise OSError("synthetic close exception")

    def select_ready(_readers, writers, exceptions, timeout):
        assert set(writers) == set(exceptions)
        assert len(writers) <= remote_proxy.PROXY_LATENCY_MAX_ACTIVE_ADDRESSES
        state.clock += timeout
        if state.on_select:
            state.on_select()
        ready = [sock for sock in writers if state.clock - sock.started + 1e-10 >=
                 state.specs.get(sock.target[0], {}).get("delay", float("inf"))]
        # Duplicated readiness must not double-close a failed socket.
        return [], ready, ready

    monkeypatch.setattr(remote_proxy.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(remote_proxy.socket, "socket", ProbeSocket)
    monkeypatch.setattr(remote_proxy.select, "select", select_ready)

    def run(hosts, budget=3.0, cancel_event=None):
        addresses = [_address(host) for host in hosts]
        deadline = state.clock + budget
        if namespace is None:
            return remote_proxy._quick_tcp_connect(addresses, 443, deadline, cancel_event)
        return namespace["quick_connect"](addresses, 443, deadline)

    state.run = run
    return state


def _assert_closed_once(state):
    assert state.created
    assert all(sock.close_calls == 1 for sock in state.created)
    assert state.peak <= 4


@pytest.mark.parametrize("count", [1, 4])
def test_slow_but_in_budget_connections_do_not_become_false_failures(race, count):
    hosts = [f"192.0.2.{index}" for index in range(1, count + 1)]
    race.specs = {host: {"delay": 0.8} for host in hosts}
    race.run(hosts, budget=3.0)
    assert 0.79 <= race.clock < 1.0
    assert len(race.created) == count
    _assert_closed_once(race)


def test_ipv6_blackholes_do_not_crowd_ipv4_out_of_active_window(race):
    hosts = ["2001:db8::1", "2001:db8::2", "2001:db8::3", "2001:db8::4", "192.0.2.1"]
    race.specs = {"192.0.2.1": {"delay": 0.1}}
    race.run(hosts)
    assert [sock.target[0] for sock in race.created[:2]] == ["2001:db8::1", "192.0.2.1"]
    assert race.clock < 0.2
    _assert_closed_once(race)


def test_only_second_address_succeeds_without_waiting_for_first_timeout(race):
    race.specs = {"192.0.2.2": {"delay": 0.2}}
    race.run(["192.0.2.1", "192.0.2.2"])
    assert 0.19 <= race.clock < 0.3
    _assert_closed_once(race)


def test_failed_active_addresses_give_queued_candidates_a_slot(race):
    hosts = [f"192.0.2.{index}" for index in range(1, 8)]
    race.specs = {host: {"delay": 0.1, "error": errno.ECONNREFUSED} for host in hosts[:4]}
    race.specs[hosts[-1]] = {"delay": 0.2}
    race.run(hosts)
    assert len(race.created) == 7
    assert 0.29 <= race.clock < 0.4
    assert all(sock.closed_at <= 0.11 for sock in race.created[:4])
    _assert_closed_once(race)


def test_total_deadline_and_socket_cap_hold_even_with_many_blackholes(race):
    with pytest.raises(TimeoutError, match="总预算"):
        race.run([f"192.0.2.{index}" for index in range(1, 20)], budget=0.4)
    assert 0.39 <= race.clock <= 0.401
    assert len(race.created) == 19
    _assert_closed_once(race)


@pytest.mark.parametrize("count", [5, 8])
def test_blackholed_front_addresses_do_not_block_immediate_tail_winner(race, count):
    hosts = [f"192.0.2.{index}" for index in range(1, count + 1)]
    race.specs = {hosts[-1]: {"immediate": 0}}
    race.run(hosts)
    assert len(race.created) == count
    assert race.clock < 3.0
    # The primary pair was not timed out/rotated to reach the tail candidate.
    assert all(sock.closed_at == race.clock for sock in race.created[:2])
    _assert_closed_once(race)


def test_two_blackholes_leave_third_slow_connection_enough_budget(race):
    hosts = [f"192.0.2.{index}" for index in range(1, 6)]
    race.specs = {hosts[2]: {"delay": 0.8}}
    race.run(hosts)
    assert 0.79 <= race.clock < 1.0
    assert len(race.created) == 4
    _assert_closed_once(race)


def test_select_exception_closes_every_active_socket(race):
    def fail():
        raise OSError("synthetic selector failure")

    race.on_select = fail
    with pytest.raises(OSError, match="selector failure"):
        race.run([f"192.0.2.{index}" for index in range(1, 5)])
    assert len(race.created) == 4
    _assert_closed_once(race)


def test_immediate_winner_closes_other_pending_connections(race):
    race.specs = {"192.0.2.3": {"immediate": 0}}
    race.run([f"192.0.2.{index}" for index in range(1, 5)])
    assert len(race.created) == 3
    assert race.clock == 0
    _assert_closed_once(race)


def test_close_exception_does_not_skip_other_socket_cleanup(race):
    race.specs = {"192.0.2.1": {"delay": 0.1, "close_raises": True}}
    race.run([f"192.0.2.{index}" for index in range(1, 5)])
    _assert_closed_once(race)


def test_repeated_dns_addresses_do_not_consume_extra_sockets(race):
    race.specs = {"192.0.2.1": {"delay": 0.1}}
    race.run(["192.0.2.1"] * 10)
    assert len(race.created) == 1
    _assert_closed_once(race)


def test_cancel_or_remote_abort_closes_every_active_socket(race):
    cancel = threading.Event()
    if race.namespace is not None:
        # SSH cancellation normally stops between batches; emulate an abrupt
        # helper interruption here to verify the generated cleanup guarantee.
        remaining = race.namespace["remaining"]

        def interrupted(deadline):
            if cancel.is_set():
                raise InterruptedError("synthetic remote abort")
            return remaining(deadline)

        race.namespace["remaining"] = interrupted
    race.on_select = cancel.set
    with pytest.raises(InterruptedError):
        race.run([f"192.0.2.{index}" for index in range(1, 5)], cancel_event=cancel)
    assert len(race.created) == 4
    assert race.clock < 0.1
    _assert_closed_once(race)


def test_real_loopback_ipv4_ipv6_race_closes_probe_sockets(monkeypatch):
    created = []
    socket_factory = socket.socket
    with socket_factory() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]

        def tracked_socket(*args):
            connection = socket_factory(*args)
            created.append(connection)
            return connection

        monkeypatch.setattr(remote_proxy.socket, "socket", tracked_socket)
        remote_proxy._quick_tcp_connect(
            [_address("::1"), _address("127.0.0.1")], port, time.monotonic() + 2,
        )
    assert created
    assert all(connection.fileno() == -1 for connection in created)
