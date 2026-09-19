"""Execute the generated SSH port allocator against synthetic sockets only."""
import sys
import secrets
from types import SimpleNamespace

import pytest

from core import remote_proxy


def _allocator():
    command = remote_proxy._build_isolated_candidate_probe_command("/home/synthetic", timeout=7)
    code = command.split('PORTS="$(python3 - <<\'PY\'\n', 1)[1].split('\nPY\n)"', 1)[0]
    return compile(code, "<isolated-ssh-port-allocator>", "exec")


def _sockets(monkeypatch, failure):
    opened, closed, bound, options = [], [], [], []
    state = {"attempt": 0, "created": 0}

    class Reservation:
        def __init__(self, family, kind):
            state["created"] += 1
            if failure == "create" and state["created"] == 2:
                raise OSError("synthetic socket allocation failure")
            self.kind = kind
            self.role = "mixed" if not any(sock not in closed for sock in opened) else ("udp" if kind == "UDP" else "controller")
            self.port = None
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(self)

        def setsockopt(self, *args):
            options.append(args)

        def bind(self, endpoint):
            assert endpoint[0] == "127.0.0.1"
            if self.role == "mixed":
                state["attempt"] += 1
                self.port = endpoint[1] or 18000 + state["attempt"]
            else:
                self.port = endpoint[1]
            bound.append((self.kind, self.port))
            if self.kind == "UDP" and (failure == "all" or (failure == "udp" and state["attempt"] == 1)):
                raise OSError("synthetic UDP in use")
            if self.role == "controller" and failure == "controller" and state["attempt"] == 1:
                raise OSError("synthetic controller in use")

        def getsockname(self):
            return "127.0.0.1", self.port

    monkeypatch.setitem(sys.modules, "socket", SimpleNamespace(
        socket=Reservation, AF_INET="IPv4", SOCK_STREAM="TCP", SOCK_DGRAM="UDP",
        SOL_SOCKET="socket", SO_EXCLUSIVEADDRUSE="exclusive",
    ))
    monkeypatch.setattr(secrets, "randbelow", lambda _bound: 18000 + state["attempt"] + 1 - 1024)
    return opened, closed, bound, options, state


@pytest.mark.parametrize("failure", ["none", "udp", "controller", "create"])
def test_ssh_allocator_checks_udp_and_releases_every_created_socket(monkeypatch, capsys, failure):
    code = _allocator()
    opened, closed, bound, options, state = _sockets(monkeypatch, failure)
    exec(code, {})
    port, controller = map(int, capsys.readouterr().out.split())
    assert controller == port + 1000
    assert ("TCP", port) in bound and ("UDP", port) in bound and ("TCP", controller) in bound
    assert port == (18002 if failure in {"udp", "controller"} else 18001)
    assert set(closed) == set(opened) and len(closed) == len(opened)
    assert options and all(option == ("socket", "exclusive", 1) for option in options)
    assert state["attempt"] <= 2


def test_ssh_allocator_failure_is_bounded_and_never_emits_unusable_ports(monkeypatch, capsys):
    code = _allocator()
    opened, closed, _bound, _options, state = _sockets(monkeypatch, "all")
    with pytest.raises(SystemExit, match="TCP/UDP"):
        exec(code, {})
    assert state["attempt"] == 64
    assert len(closed) == len(opened)
    assert capsys.readouterr().out == ""
