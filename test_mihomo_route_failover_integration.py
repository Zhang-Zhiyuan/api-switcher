"""Real-core same-subscription failover using only loopback destinations.

The temporary core has no DNS service/providers and all probe URLs, upstreams,
and direct-route sentinels are loopback. Never reload or inspect the live core.
"""
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib import error, request

import pytest
import yaml

from core import local_proxy, proxy_routing, remote_proxy
from core.subscription_routing_policy import suggest_tagged_routes
from test_local_proxy_service_routing import _patch_profiles
from test_mihomo_service_routes_integration import _port_pair, _wait_for_mihomo_ready


class _LoopbackOutbound:
    """An HTTP proxy stub that returns its identity, never forwards anywhere."""

    def __init__(self, name, stack):
        self.name = name
        self.hits = Counter()
        self.stopped = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                owner.hits[self.path] += 1
                health = self.path.endswith("/health")
                content = b"" if health else owner.name.encode("ascii")
                self.send_response(204 if health else 200)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                if content:
                    self.wfile.write(content)
                self.close_connection = True

            def do_CONNECT(self):
                # Acknowledge CONNECT then process the nested plain HTTP request
                # locally; no socket is ever opened to its requested target.
                self.connection.settimeout(2)
                self.send_response(200, "Connection established")
                self.end_headers()
                self.close_connection = True
                self.handle_one_request()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.node = {"name": name, "type": "http", "server": "127.0.0.1", "port": self.server.server_port}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        stack.callback(self.stop)

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _proxies(port):
    # http.client never consults the real process/system proxy environment.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/proxies")
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read(1024 * 1024))["proxies"]
    finally:
        connection.close()


def _wait_until(predicate, message, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(message() if callable(message) else message)


@pytest.mark.parametrize("primary_dead_at_start", [False, True])
@pytest.mark.parametrize("route_mode", ["tagged_datacenter", "selected_ai"])
def test_real_mihomo_pool_fails_over_only_within_authorized_candidates(
    monkeypatch, tmp_path, primary_dead_at_start, route_mode,
):
    binary = Path(os.environ.get("API_SWITCHER_MIHOMO_TEST_CORE") or local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe")
    if not binary.is_file():
        pytest.skip("Local mihomo core is not installed")
    with ExitStack() as stack:
        home, primary, backup, direct, excluded = [
            _LoopbackOutbound(name, stack)
            for name in ("residential-home", "dc-primary", "dc-backup", "direct-sentinel", "unselected")
        ]
        _patch_profiles(monkeypatch, {"home": (home.node,), "dc": (primary.node, backup.node)})
        catalog = [
            {"id": profile_id, "network_type": network_type, "auto_route_usable": True,
             "nodes": [{"key": remote_proxy.proxy_node_key(node), "label": node["name"]} for node in nodes]}
            for profile_id, network_type, nodes in (
                ("home", "residential", (home.node,)),
                ("dc", "datacenter", (primary.node, backup.node)),
            )
        ]
        preferences, _notices = suggest_tagged_routes({}, catalog)
        assert "youtube" not in preferences.get("service_node_bindings", {})
        assert "google" not in preferences.get("service_node_bindings", {})
        service_hosts = {"youtube": "youtube.com", "google": "google.com"}
        service_groups = [local_proxy._subscription_route_group_name("dc", service)
                          for service in service_hosts]
        if route_mode == "selected_ai":
            # The cache deliberately has an unselected healthy node first and
            # reverses the selected nodes: only explicit membership/order count.
            _patch_profiles(monkeypatch, {"pool": (excluded.node, backup.node, primary.node)})
            service_hosts = {"openai": "api.openai.com", "claude": "api.anthropic.com",
                             "google_ai": "generativelanguage.googleapis.com"}
            keys = [remote_proxy.proxy_node_key(node) for node in (primary.node, backup.node)]
            preferences = {
                "service_profile_bindings": {service: "pool" for service in service_hosts},
                "service_node_pools": {service: list(keys) for service in service_hosts},
            }
            service_groups = [local_proxy._subscription_route_group_name("pool", service, node_keys=keys)
                              for service in service_hosts]
        port = _port_pair()
        config = yaml.safe_load(remote_proxy.build_mihomo_config(
            home.node, port, log_level="silent", **proxy_routing.config_options(preferences),
        ))
        groups = {group["name"]: group for group in config["proxy-groups"]}
        nodes = {node["name"]: node for node in config["proxies"]}
        aliases = {}
        for group_name in service_groups:
            group = groups[group_name]
            assert group["type"] == "fallback"
            assert len(group["proxies"]) == 2
            assert [nodes[name]["port"] for name in group["proxies"]] == [primary.node["port"], backup.node["port"]]
            assert all(name not in {"DIRECT", "REJECT", "AI-PROXY"} for name in group["proxies"])
            aliases[group_name] = tuple(group["proxies"])

        # Change only the test environment's health endpoint/timing. The actual
        # generated rules, outbound membership and fallback types stay intact.
        for group in groups.values():
            if "url" in group:
                group.update({"url": f"http://127.0.0.1:{direct.node['port']}/health",
                              "expected-status": "204", "interval": 1,
                              "timeout": 500, "lazy": False})
        config["dns"] = {"enable": False}
        config["hosts"] = {host: "127.0.0.1" for host in service_hosts.values()}
        config["ipv6"] = False
        config["allow-lan"] = False
        config["bind-address"] = "127.0.0.1"
        assert not config.get("proxy-providers") and not config.get("rule-providers")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        if primary_dead_at_start:
            primary.stop()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        checked = subprocess.run([str(binary), "-t", "-d", str(tmp_path), "-f", str(config_path)],
                                 capture_output=True, timeout=15, creationflags=flags)
        assert checked.returncode == 0, checked.stdout.decode("utf-8", errors="replace")
        process = subprocess.Popen([str(binary), "-d", str(tmp_path), "-f", str(config_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            _wait_for_mihomo_ready(process, port)
            opener = request.build_opener(remote_proxy._NoBypassProxyHandler({"http": f"http://127.0.0.1:{port}"}))
            controller = remote_proxy.mihomo_controller_port(port)

            def fetch(host):
                with opener.open(f"http://{host}:{direct.node['port']}/traffic", timeout=2) as response:
                    return response.read(1024).decode("ascii")

            if not primary_dead_at_start:
                for host in service_hosts.values():
                    assert fetch(host) == "dc-primary"
                primary.stop()

            def using_backup():
                current = _proxies(controller)
                return all(
                    current[group]["now"] == secondary
                    and current[secondary].get("alive") is True
                    and current[first].get("alive") is False
                    for group, (first, secondary) in aliases.items()
                )

            _wait_until(using_backup, lambda: "authorized backup was not selected: " + repr({
                "proxies": {key: value for key, value in _proxies(controller).items()
                            if key in aliases or any(key in names for names in aliases.values())},
                "backup_hits": backup.hits,
            }))
            for host in service_hosts.values():
                assert fetch(host) == "dc-backup"

            backup.stop()

            def all_dead():
                current = _proxies(controller)
                return all(current[name].get("alive") is False
                           for group_aliases in aliases.values() for name in group_aliases)

            _wait_until(all_dead, "failed subscription nodes were not marked unavailable")
            for host in service_hosts.values():
                with pytest.raises((error.URLError, ConnectionError, TimeoutError)):
                    fetch(host)
            # If routing accidentally leaked to the residential/default route
            # or DIRECT, one of these loopback sentinels would receive traffic.
            assert home.hits["/traffic"] == 0
            assert direct.hits["/traffic"] == 0
            assert not any("/traffic" in path for path in home.hits)
            assert not any("/traffic" in path for path in direct.hits)
            assert not excluded.hits
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
