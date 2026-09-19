"""Listener preflight tests: only owned loopback sockets, no real mihomo."""
from contextlib import contextmanager
import os
import secrets
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import local_proxy, remote_proxy


def _exclusive(sock):
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    return sock


def _unused_managed_port():
    failures = []
    for _attempt in range(30):
        # Do not stay trapped in a small Windows dynamic TCP range whose UDP
        # or derived controller ports are occupied after the first OS choice.
        port = 0 if _attempt == 0 else 1024 + secrets.randbelow(64535 - 1024 + 1)
        try:
            if _attempt == 0:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    _exclusive(probe)
                    probe.bind(("127.0.0.1", port))
                    port = probe.getsockname()[1]
            with local_proxy._reserve_local_mihomo_ports(port, remote_proxy.mihomo_controller_port(port), mixed_host="0.0.0.0"):
                return port
        except OSError as exc:
            failures.append(f"{port}: {type(exc).__name__}: {exc}")
    pytest.fail("No free mixed TCP/UDP and controller TCP ports after 30 attempts; " + "; ".join(failures[-3:]))


def _not_running(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)


def test_udp_occupied_port_is_rejected_even_when_tcp_is_free():
    with _exclusive(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        assert not local_proxy._is_port_listening(port)
        with pytest.raises(OSError):
            with local_proxy._reserve_local_mihomo_ports(port):
                pytest.fail("UDP ownership must be checked")
        # The failed second bind must not leak the first TCP socket.
        with _exclusive(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as tcp:
            tcp.bind(("127.0.0.1", port))


def test_local_selection_skips_udp_collision_and_releases_preflight(monkeypatch):
    _not_running(monkeypatch)
    candidate = _unused_managed_port()
    with _exclusive(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as occupied:
        occupied.bind(("127.0.0.1", 0))
        preferred = occupied.getsockname()[1]
        if candidate == preferred:
            candidate = _unused_managed_port()
        monkeypatch.setattr(local_proxy, "LOCAL_PORT_CANDIDATES", (candidate,))
        assert local_proxy._select_local_mixed_port(preferred) == candidate
        with local_proxy._reserve_local_mihomo_ports(candidate, remote_proxy.mihomo_controller_port(candidate)):
            pass


def test_bound_but_not_listening_controller_is_unavailable():
    with _exclusive(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as occupied:
        occupied.bind(("127.0.0.1", 0))
        controller = occupied.getsockname()[1]
        assert not local_proxy._is_port_listening(controller)
        with pytest.raises(OSError):
            with local_proxy._reserve_local_mihomo_ports(controller_port=controller):
                pytest.fail("A reserved controller is not a free controller")


def test_managed_port_selection_detects_udp_on_another_local_address(monkeypatch):
    _not_running(monkeypatch)
    candidate = _unused_managed_port()
    with _exclusive(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as occupied:
        try:
            occupied.bind(("127.0.0.2", 0))
        except OSError:
            pytest.skip("Alternative loopback address is unavailable")
        preferred = occupied.getsockname()[1]
        if candidate == preferred:
            candidate = _unused_managed_port()
        monkeypatch.setattr(local_proxy, "LOCAL_PORT_CANDIDATES", (candidate,))
        assert not local_proxy._is_port_listening(preferred)
        assert local_proxy._select_local_mixed_port(preferred) == candidate


def test_verified_running_instance_reuses_original_port_without_any_probing(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17001})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 777)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *_args, **_kwargs: True)
    probe = Mock(side_effect=AssertionError("must not bind/probe a verified running instance"))
    monkeypatch.setattr(local_proxy, "_is_port_listening", probe)
    monkeypatch.setattr(local_proxy, "_reserve_local_mihomo_ports", probe)
    assert local_proxy._select_local_mixed_port(17002) == 17001
    probe.assert_not_called()


def test_local_port_exhaustion_is_bounded_and_does_not_claim_tcp_only(monkeypatch):
    _not_running(monkeypatch)
    monkeypatch.setattr(local_proxy, "LOCAL_PORT_CANDIDATES", (17001, 17002))
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: False)
    probe = Mock(side_effect=OSError("access denied"))
    monkeypatch.setattr(local_proxy, "_reserve_local_mihomo_ports", probe)
    with pytest.raises(RuntimeError, match="TCP/UDP"):
        local_proxy._select_local_mixed_port(17000)
    assert probe.call_count == 3


def _fake_sockets(monkeypatch, fail_stage="", fail_index=-1):
    created = []
    attempts = []
    monkeypatch.setattr(local_proxy, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(local_proxy.socket, "SO_EXCLUSIVEADDRUSE", 999, raising=False)

    class FakeSocket:
        def __init__(self, index, kind):
            self.index, self.kind, self.closed = index, kind, False
            self.options = []
            self.bound = None

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))
            if fail_stage == "setsockopt" and self.index == fail_index:
                raise OSError("exclusive option failed")

        def bind(self, address):
            self.bound = address
            if fail_stage == "bind" and self.index == fail_index:
                raise OSError("address already in use")

        def getsockname(self):
            if fail_stage == "getsockname" and self.index == fail_index:
                raise OSError("cannot read socket")
            return self.bound[0], self.bound[1] or 37000 + self.index

        def close(self):
            self.closed = True

    def factory(_family, kind):
        index = len(attempts)
        attempts.append(kind)
        if fail_stage == "create" and index == fail_index:
            raise OSError("socket limit reached")
        sock = FakeSocket(index, kind)
        created.append(sock)
        return sock

    monkeypatch.setattr(local_proxy.socket, "socket", factory)
    return created, attempts


@pytest.mark.parametrize("stage,index", [
    ("create", 0), ("create", 1), ("create", 2),
    ("bind", 0), ("bind", 1), ("bind", 2),
    ("setsockopt", 0), ("setsockopt", 1), ("setsockopt", 2),
    ("getsockname", 0), ("getsockname", 1), ("getsockname", 2),
])
def test_socket_failure_at_each_acquisition_stage_closes_every_created_socket(monkeypatch, stage, index):
    created, _attempts = _fake_sockets(monkeypatch, stage, index)
    with pytest.raises(OSError):
        with local_proxy._reserve_local_mihomo_ports(controller_port=0):
            pytest.fail("expected acquisition failure")
    assert all(sock.closed for sock in created)


def test_reservation_holds_tcp_udp_same_port_and_distinct_controller_until_exit(monkeypatch):
    created, attempts = _fake_sockets(monkeypatch)
    with local_proxy._reserve_local_mihomo_ports(controller_port=0) as ports:
        assert ports == (37000, 37002)
        assert attempts == [socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_STREAM]
        assert created[1].bound == ("127.0.0.1", 37000)
        assert all(sock.options == [(socket.SOL_SOCKET, 999, 1)] for sock in created)
        assert not any(sock.closed for sock in created)
    assert all(sock.closed for sock in created)


def test_wildcard_mixed_probe_keeps_controller_loopback_only(monkeypatch):
    created, _attempts = _fake_sockets(monkeypatch)
    with local_proxy._reserve_local_mihomo_ports(37000, 38000, mixed_host="0.0.0.0") as ports:
        assert ports == (37000, 38000)
        assert [sock.bound for sock in created] == [("0.0.0.0", 37000), ("0.0.0.0", 37000), ("127.0.0.1", 38000)]
    assert all(sock.closed for sock in created)


def test_isolated_selection_retries_udp_collision_with_released_sockets(monkeypatch):
    created, attempts = _fake_sockets(monkeypatch, "bind", 1)
    ports = local_proxy._select_isolated_mihomo_ports(with_controller=True)
    assert ports == (37002, 37004)
    assert len(attempts) == 5 and all(sock.closed for sock in created)


def test_isolated_selection_releases_successful_ports_even_without_controller(monkeypatch):
    created, attempts = _fake_sockets(monkeypatch)
    assert local_proxy._select_isolated_mihomo_ports() == (37000, None)
    assert attempts == [socket.SOCK_STREAM, socket.SOCK_DGRAM]
    assert all(sock.closed for sock in created)


def test_isolated_selection_has_finite_retry_limit(monkeypatch):
    @contextmanager
    def unavailable(*_args, **_kwargs):
        raise OSError("all ephemeral UDP ports unavailable")
        yield  # pragma: no cover

    probe = Mock(side_effect=unavailable)
    monkeypatch.setattr(local_proxy, "_reserve_local_mihomo_ports", probe)
    with pytest.raises(RuntimeError, match="TCP/UDP"):
        local_proxy._select_isolated_mihomo_ports(with_controller=True)
    assert probe.call_count == 30
