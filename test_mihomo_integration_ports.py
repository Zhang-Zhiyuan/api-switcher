"""Loopback-only regressions for disposable mihomo test startup."""
import socket
from types import SimpleNamespace

import pytest

from core import remote_proxy
from test_mihomo_service_routes_integration import _port_pair, _reserve_exclusively, _wait_for_mihomo_ready


def test_port_pair_skips_tcp_port_already_occupied_by_udp(monkeypatch):
    native_socket = socket.socket
    blocked_port = _port_pair()
    with native_socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied_udp:
        _reserve_exclusively(occupied_udp)
        occupied_udp.bind(("127.0.0.1", blocked_port))
        safe_port = _port_pair()
        attempted = []

        class CandidateSocket(native_socket):
            def bind(self, address):
                if self.type == socket.SOCK_STREAM and address[1] == 0:
                    attempted.append(blocked_port)
                    address = (address[0], blocked_port)
                elif self.type == socket.SOCK_STREAM and address[1] == safe_port:
                    attempted.append(safe_port)
                return super().bind(address)

        monkeypatch.setattr(socket, "socket", CandidateSocket)
        monkeypatch.setattr("test_mihomo_service_routes_integration.secrets.randbelow", lambda _: safe_port - 1024)
        assert _port_pair() == safe_port
        assert attempted == [blocked_port, safe_port]
        assert occupied_udp.getsockname() == ("127.0.0.1", blocked_port)


@pytest.mark.parametrize("controller_listening", [False, True])
def test_readiness_deadline_fails_instead_of_continuing_to_web_requests(controller_listening):
    port = _port_pair()
    with socket.socket() as controller:
        _reserve_exclusively(controller)
        controller.bind(("127.0.0.1", remote_proxy.mihomo_controller_port(port)))
        if controller_listening:
            controller.listen(4)
        process = SimpleNamespace(poll=lambda: None, returncode=None)
        with pytest.raises(AssertionError, match="endpoints were not ready"):
            _wait_for_mihomo_ready(process, port, timeout_seconds=0.1)


def test_readiness_reports_owned_process_exit_immediately():
    process = SimpleNamespace(poll=lambda: 17, returncode=17)
    with pytest.raises(AssertionError, match="exited before readiness: 17"):
        _wait_for_mihomo_ready(process, 12345)
