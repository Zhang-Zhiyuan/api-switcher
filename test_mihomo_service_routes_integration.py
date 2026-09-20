"""Exercise real mihomo routing against loopback-only synthetic upstreams."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib import request

import pytest
import yaml

from core import local_proxy, proxy_route_diagnostics, proxy_routing, remote_proxy
from core.subscription_routing_policy import suggest_tagged_routes
from test_local_proxy_service_routing import _patch_profiles


def _upstream(label, stack):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

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


def _reserve_exclusively(sock):
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


def _port_pair():
    failures = []
    for _attempt in range(30):
        with socket.socket() as listener, socket.socket() as controller, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM,
        ) as mixed_udp:
            for reserved in (listener, controller, mixed_udp):
                _reserve_exclusively(reserved)
            # With a narrowed Windows ephemeral range, sequential port-0
            # choices can repeatedly collide with UDP or derived controller
            # ports. After one failed automatic choice, sample independent
            # legal pairs instead of exhausting adjacent candidates.
            port = 0 if _attempt == 0 else 1024 + secrets.randbelow(64535 - 1024 + 1)
            try:
                listener.bind(("127.0.0.1", port))
                port = listener.getsockname()[1]
                # A mixed mihomo listener needs both protocols. On Windows an
                # available ephemeral TCP port can be occupied by UDP (e.g.
                # SSDP/1900); that failure leaves the controller alive but the
                # mixed listener unavailable after briefly opening its TCP port.
                mixed_udp.bind(("127.0.0.1", port))
                controller.bind(("127.0.0.1", remote_proxy.mihomo_controller_port(port)))
            except OSError as exc:
                failures.append(f"{port}: {type(exc).__name__}: {exc}")
                continue
            return port
    raise RuntimeError("No free mixed TCP/UDP and controller TCP ports for isolated mihomo; "
                       + "; ".join(failures[-3:]))


def _wait_for_mihomo_ready(process, port, *, timeout_seconds=10):
    controller_port = remote_proxy.mihomo_controller_port(port)
    deadline = time.monotonic() + timeout_seconds
    last_error = "not yet checked"
    while time.monotonic() < deadline:
        assert process.poll() is None, f"isolated mihomo exited before readiness: {process.returncode}"
        try:
            with socket.create_connection(("127.0.0.1", controller_port), timeout=0.2):
                pass
            with socket.create_connection(("127.0.0.1", port), timeout=0.2) as mixed:
                # Check the SOCKS protocol, not just a briefly bound TCP socket:
                # serving starts only after mihomo's UDP bind has succeeded.
                mixed.sendall(b"\x05\x01\x00")
                greeting = b""
                while len(greeting) < 2:
                    part = mixed.recv(2 - len(greeting))
                    if not part:
                        break
                    greeting += part
                if greeting != b"\x05\x00":
                    raise OSError(f"invalid SOCKS readiness greeting: {greeting!r}")
            return
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.05)
    raise AssertionError(
        f"isolated mihomo endpoints were not ready: mixed={port}, controller={controller_port}, "
        f"exit_code={process.poll()}, last_error={last_error}"
    )


def test_real_mihomo_dispatches_service_and_custom_requests_to_pinned_nodes(monkeypatch, tmp_path):
    binary = Path(os.environ.get("API_SWITCHER_MIHOMO_TEST_CORE") or local_proxy.LOCAL_PROXY_BIN_DIR / "mihomo.exe")
    if not binary.is_file():
        pytest.skip("Local mihomo core is not installed")
    with ExitStack() as stack:
        first, second, datacenter = [_upstream(label, stack) for label in ("home-one", "home-two", "datacenter")]
        _patch_profiles(monkeypatch, {"home": (first, second), "dc": (datacenter,)})
        preferences = {
            "builtin_sites": {"github": True, "huggingface": True},
            "custom_targets": [
                {"id": "api", "kind": "domain", "value": "api.openai.com", "enabled": True},
                {"id": "network", "kind": "ip-cidr", "value": "203.0.113.0/24", "enabled": True},
                {"id": "host", "kind": "ip-cidr", "value": "203.0.113.9/32", "enabled": True},
                {"id": "github", "kind": "domain", "value": "github.com", "enabled": True},
                {"id": "hf", "kind": "domain", "value": "huggingface.co", "enabled": True},
                {"id": "hfshort", "kind": "domain", "value": "hf.co", "enabled": True},
                {"id": "default", "kind": "domain", "value": "ytimg.com", "enabled": True},
                {"id": "inherited", "kind": "domain", "value": "inherited.example.test", "enabled": True},
            ],
            "service_profile_bindings": {
                "claude": "home",
                "custom": "home",
                "custom:api": "dc", "custom:network": "home", "custom:host": "dc",
                "github": "home", "huggingface": "home",
                "custom:github": "dc", "custom:hf": "dc", "custom:hfshort": "dc",
            },
            "service_node_bindings": {
                "claude": remote_proxy.proxy_node_key(second), "custom:api": remote_proxy.proxy_node_key(datacenter),
                "custom": remote_proxy.proxy_node_key(first),
                "custom:network": remote_proxy.proxy_node_key(first), "custom:host": remote_proxy.proxy_node_key(datacenter),
                "github": remote_proxy.proxy_node_key(first),
                "huggingface": remote_proxy.proxy_node_key(second), "custom:github": remote_proxy.proxy_node_key(datacenter),
                "custom:hf": remote_proxy.proxy_node_key(datacenter), "custom:hfshort": remote_proxy.proxy_node_key(datacenter),
            },
            "service_route_modes": {"custom:default": "default"},
        }
        catalog = [
            {"id": profile_id, "network_type": network_type, "auto_route_usable": True,
             "selected_node_key": remote_proxy.proxy_node_key(nodes[0]),
             "nodes": [{"key": remote_proxy.proxy_node_key(node), "label": node["name"]} for node in nodes]}
            for profile_id, network_type, nodes in (
                ("home", "residential", (first, second)), ("dc", "datacenter", (datacenter,)),
            )
        ]
        preferences, _notices = suggest_tagged_routes(preferences, catalog)
        assert {service: preferences["service_profile_bindings"][service]
                for service in ("openai", "claude", "google_ai", "youtube", "google")} == {
                    "openai": "home", "claude": "home", "google_ai": "home", "youtube": "dc", "google": "dc",
                }
        assert preferences["builtin_sites"]["youtube"] and preferences["builtin_sites"]["google"]
        # The opt-in policy must retain Claude's deliberately different exit.
        assert preferences["service_node_bindings"]["claude"] == remote_proxy.proxy_node_key(second)
        port = _port_pair()
        # The same routing options are used by Win11 and SSH deployments.
        options = proxy_routing.config_options(preferences)
        for group in options["additional_proxy_groups"]:
            group["health_checked"] = False
        config = remote_proxy.build_mihomo_config(second, port, log_level="silent", **options)
        # Exercise literal controller payloads too. Both outcomes remain on
        # loopback-only upstreams; a negative match must never contact the web.
        home_route = local_proxy._subscription_route_group_name("home", "openai")
        dc_route = local_proxy._subscription_route_group_name("dc", "youtube")
        parsed = yaml.safe_load(config)
        # Multi-node groups remain health-checked even when a caller requests
        # health_checked=False. Sanitize the *generated* groups, not merely the
        # input options, so no AI/public background probes escape this test.
        for group in parsed["proxy-groups"]:
            if "url" in group:
                group["url"] = f"http://127.0.0.1:{first['port']}/health"
                group["expected-status"] = "200"
        parsed["dns"] = {"enable": False}
        parsed["ipv6"] = False
        parsed["allow-lan"] = False
        parsed["bind-address"] = "127.0.0.1"
        # Fail closed on any unexpected test destination. All asserted rules
        # below must still match their generated managed subscription group.
        parsed["rules"] = ["MATCH,REJECT" if rule.startswith("MATCH,") else rule
                           for rule in parsed["rules"]]
        assert all(node["server"] == "127.0.0.1" for node in parsed["proxies"])
        assert not parsed.get("proxy-providers") and not parsed.get("rule-providers")
        parsed["rules"][:0] = [
            f"DOMAIN-KEYWORD,.keyword-only.test,{home_route}",
            f"DOMAIN-SUFFIX,keyword-only.test,{dc_route}",
            f"DOMAIN-SUFFIX,.dot-suffix.test,{home_route}",
            f"DOMAIN-SUFFIX,dot-suffix.test,{dc_route}",
        ]
        config = yaml.safe_dump(parsed)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(config, encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        checked = subprocess.run([str(binary), "-t", "-d", str(tmp_path), "-f", str(config_path)],
                                 capture_output=True, timeout=15, creationflags=flags)
        assert checked.returncode == 0, checked.stdout.decode("utf-8", errors="replace")
        process = subprocess.Popen([str(binary), "-d", str(tmp_path), "-f", str(config_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            _wait_for_mihomo_ready(process, port)
            opener = request.build_opener(remote_proxy._NoBypassProxyHandler({"http": f"http://127.0.0.1:{port}"}))
            runtime = proxy_route_diagnostics._read_controller(remote_proxy.mihomo_controller_port(port), expected_mixed_port=port)
            saved = proxy_route_diagnostics.saved_rules(preferences)
            for host, expected in (
                ("chatgpt.com", "home-one"), ("api.anthropic.com", "home-two"),
                ("www.youtube.com", "datacenter"), ("api.openai.com", "datacenter"),
                ("youtube.com", "datacenter"), ("google.com", "datacenter"), ("assets.gstatic.com", "datacenter"),
                ("gemini.google.com", "home-one"), ("generativelanguage.googleapis.com", "home-one"),
                ("203.0.113.8", "home-one"), ("203.0.113.9", "datacenter"),
                ("github.com", "datacenter"), ("raw.githubusercontent.com", "home-one"),
                ("huggingface.co", "datacenter"), ("hf.co", "datacenter"), ("i.ytimg.com", "home-two"),
                ("inherited.example.test", "home-one"),
                ("x.com", "home-one"), ("reddit.com", "home-one"),
                ("discord.com", "datacenter"), ("telegram.org", "datacenter"),
            ):
                actual_rule = proxy_route_diagnostics.match_rules(host, runtime["rules"], runtime["mode"])
                saved_rule = proxy_route_diagnostics.match_rules(host, saved)
                assert actual_rule.certain and saved_rule.certain
                assert actual_rule.route == saved_rule.route
                with opener.open(f"http://{host}/routing-test", timeout=5) as response:
                    assert response.read().decode("ascii") == expected
            for host, expected_route, expected_upstream in (
                ("keyword-only.test", dc_route, "datacenter"),
                ("sub.keyword-only.test", home_route, "home-one"),
                ("dot-suffix.test", dc_route, "datacenter"),
            ):
                result = proxy_route_diagnostics.match_rules(host, runtime["rules"], runtime["mode"])
                assert result.certain and result.route == expected_route
                with opener.open(f"http://{host}/literal-rule-test", timeout=5) as response:
                    assert response.read().decode("ascii") == expected_upstream
            with pytest.raises(ValueError, match="port"):
                proxy_route_diagnostics._read_controller(
                    remote_proxy.mihomo_controller_port(port), expected_mixed_port=port % 65535 + 1,
                )
            # Inspection must not alter mode, rules or selected nodes.
            after = proxy_route_diagnostics._read_controller(remote_proxy.mihomo_controller_port(port), expected_mixed_port=port)
            assert after["mode"] == runtime["mode"]
            assert after["rules"] == runtime["rules"]
            assert {key: value.get("now") for key, value in after["proxies"].items()} == {
                key: value.get("now") for key, value in runtime["proxies"].items()
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
