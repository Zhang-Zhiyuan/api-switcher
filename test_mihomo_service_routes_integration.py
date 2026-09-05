"""Exercise real mihomo routing against loopback-only synthetic upstreams."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib import request

import pytest

from core import local_proxy, proxy_routing, remote_proxy
from test_local_proxy_service_routing import _patch_profiles


def _upstream(label, stack):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = label.encode("ascii")
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_CONNECT(self):
            # Mihomo tunnels plain HTTP through its HTTP outbound too. Complete
            # that tunnel locally and return our identity for the inner request.
            self.connection.settimeout(3)
            self.send_response(200, "Connection established")
            self.end_headers()
            self.close_connection = True
            self.handle_one_request()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    stack.callback(server.server_close)
    stack.callback(server.shutdown)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return {"name": label, "type": "http", "server": "127.0.0.1", "port": server.server_port}


def _port_pair():
    for _attempt in range(30):
        with socket.socket() as listener, socket.socket() as controller:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if port >= 65535:
                continue
            try:
                controller.bind(("127.0.0.1", remote_proxy.mihomo_controller_port(port)))
            except OSError:
                continue
            return port
    raise RuntimeError("No free port pair for isolated mihomo")


def test_real_mihomo_dispatches_service_and_custom_requests_to_pinned_nodes(monkeypatch, tmp_path):
    binary = Path(os.environ.get("API_SWITCHER_MIHOMO_TEST_CORE") or local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe")
    if not binary.is_file():
        pytest.skip("Local mihomo core is not installed")
    with ExitStack() as stack:
        first, second, datacenter = [_upstream(label, stack) for label in ("home-one", "home-two", "datacenter")]
        _patch_profiles(monkeypatch, {"home": (first, second), "dc": (datacenter,)})
        preferences = {
            "builtin_sites": {"youtube": True},
            "custom_targets": [
                {"id": "api", "kind": "domain", "value": "api.openai.com", "enabled": True},
                {"id": "network", "kind": "ip-cidr", "value": "203.0.113.0/24", "enabled": True},
                {"id": "host", "kind": "ip-cidr", "value": "203.0.113.9/32", "enabled": True},
            ],
            "service_profile_bindings": {
                "openai": "home", "claude": "home", "youtube": "dc",
                "custom:api": "dc", "custom:network": "home", "custom:host": "dc",
            },
            "service_node_bindings": {
                "openai": remote_proxy.proxy_node_key(first), "claude": remote_proxy.proxy_node_key(second),
                "youtube": remote_proxy.proxy_node_key(datacenter), "custom:api": remote_proxy.proxy_node_key(datacenter),
                "custom:network": remote_proxy.proxy_node_key(first), "custom:host": remote_proxy.proxy_node_key(datacenter),
            },
        }
        port = _port_pair()
        # The same routing options are used by Win11 and SSH deployments.
        options = proxy_routing.config_options(preferences)
        for group in options["additional_proxy_groups"]:
            group["health_checked"] = False
        config = remote_proxy.build_mihomo_config(datacenter, port, log_level="silent", **options)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config, encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        checked = subprocess.run([str(binary), "-t", "-d", str(tmp_path), "-f", str(config_path)],
                                 capture_output=True, timeout=15, creationflags=flags)
        assert checked.returncode == 0, checked.stdout.decode("utf-8", errors="replace")
        process = subprocess.Popen([str(binary), "-d", str(tmp_path), "-f", str(config_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, "isolated mihomo exited before listening"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            opener = request.build_opener(remote_proxy._NoBypassProxyHandler({"http": f"http://127.0.0.1:{port}"}))
            for host, expected in (
                ("chatgpt.com", "home-one"), ("api.anthropic.com", "home-two"),
                ("www.youtube.com", "datacenter"), ("api.openai.com", "datacenter"),
                ("203.0.113.8", "home-one"), ("203.0.113.9", "datacenter"),
            ):
                with opener.open(f"http://{host}/routing-test", timeout=5) as response:
                    assert response.read().decode("ascii") == expected
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
