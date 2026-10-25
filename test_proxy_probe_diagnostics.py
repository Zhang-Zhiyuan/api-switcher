"""Regression cases for the repeated 0/13 remote deployment failure."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core import remote_proxy
from core.proxy_probe_diagnostics import CORE_DIAGNOSTIC_READER, candidate_failure_details


def failed_output():
    return "\n".join([
        *(f"probe\t{label}\t0\tTLS EOF\t5000"
          for _ in range(3) for label, _url in remote_proxy.REMOTE_AI_STABILITY_TARGETS),
        f"probe\t{remote_proxy.REMOTE_CODEX_COMPACT_PROBE_LABEL}\t0\tTLS EOF\t5000",
        'candidate_diagnostic\t{"api.openai.com":"dns","api.anthropic.com":"timeout"}',
    ])


def test_remote_core_reader_reports_causes_without_forwarding_log_secrets(tmp_path):
    log = tmp_path / "mihomo.log"
    log.write_text(
        "Old ignored output\n" * 10000
        + 'level=warning msg="[TCP] dial SUB-123456789ABC-PROXY 127.0.0.1:1234 --> api.openai.com:443 '
          'error: old.example:443 dns resolve failed: couldn\'t find ip token=secret-test-token"\n'
        + 'level=warning msg="dial --> api.anthropic.com:443 error: i/o timeout password=secret-test-password"\n'
        + 'level=warning msg="dial --> chatgpt.com:443 error: x509: unknown authority"\n',
        encoding="utf-8",
    )
    run = subprocess.run([sys.executable, "-c", CORE_DIAGNOSTIC_READER, str(log)],
                         capture_output=True, text=True, timeout=5, check=True)
    assert run.stdout.startswith("candidate_diagnostic\t")
    assert json.loads(run.stdout.split("\t", 1)[1]) == {
        "api.openai.com": "dns", "api.anthropic.com": "timeout", "chatgpt.com": "certificate",
    }
    assert "secret-test" not in run.stdout and "old.example" not in run.stdout
    assert not run.stderr


def test_diagnostic_parser_ignores_unknown_categories_and_redacts_probe_details():
    result = remote_proxy.RemoteAIProxyProbeResult("OpenAI API", False, "failed token=secret-diagnostic-token")
    detail = candidate_failure_details([result], 'candidate_diagnostic\t{"api.openai.com":"untrusted content"}',
                                       remote_proxy.REMOTE_AI_STABILITY_TARGETS)
    assert "untrusted content" not in detail and "secret-diagnostic-token" not in detail


@pytest.mark.parametrize("route", ["default", "bound", "direct", "depends_on_default"])
def test_isolated_failure_uses_actual_rules_to_decide_whether_retry_can_help(monkeypatch, route):
    node = {"name": "US candidate", "type": "http", "server": "example.invalid", "port": 8080}
    group = "SUB-123456789ABC-PROXY"
    options = {}
    if route in {"bound", "depends_on_default"}:
        options = {"additional_proxy_groups": [{"name": group, "proxy_node": node}],
                   "proxy_domain_routes": {"openai.com": group}}
    if route == "direct":
        options = {"proxy_domain_routes": {"openai.com": "DIRECT"}}
    config = remote_proxy.build_mihomo_config(node, **options)
    if route == "depends_on_default":
        parsed = remote_proxy.yaml.safe_load(config)
        parsed["proxy-groups"][1]["proxies"] = ["AI-PROXY"]
        config = remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + remote_proxy.yaml.safe_dump(parsed)
    monkeypatch.setattr(remote_proxy, "_build_isolated_candidate_config", lambda *_a, **_k: config)
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", lambda *_a, **_k: (0, failed_output(), ""))
    with pytest.raises(RuntimeError) as error:
        remote_proxy._probe_ai_proxy_candidate_isolated(object(), "/home/test", node)
    message = str(error.value)
    assert "0/13 可达" in message and "节点或上游域名解析失败" in message and "节点或上游连接超时" in message
    assert message.count("OpenAI API：") == 1  # Deduplicate repeated rounds.
    assert isinstance(error.value, remote_proxy.RemoteProxyIndependentRouteError) == (route in {"bound", "direct"})


def test_incomplete_results_cannot_be_mistaken_for_a_route_failure(monkeypatch):
    node = {"name": "US", "type": "http", "server": "example.invalid", "port": 8080}
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status",
                        lambda *_a, **_k: (0, failed_output().splitlines()[0], ""))
    with pytest.raises(RuntimeError, match="探测结果不完整") as error:
        remote_proxy._probe_ai_proxy_candidate_isolated(object(), "/home/test", node)
    assert not isinstance(error.value, remote_proxy.RemoteProxyIndependentRouteError)


def test_independent_route_failure_stops_install_without_testing_unrelated_nodes(monkeypatch):
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_a, **_k: SimpleNamespace(running=False))
    calls = []

    def probe(*_a, **_k):
        calls.append("isolated")
        raise remote_proxy.RemoteProxyIndependentRouteError("独立分流不可用：OpenAI API，域名解析失败")

    monkeypatch.setattr(remote_proxy, "_probe_ai_proxy_candidate_with_core_bootstrap", probe)
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", lambda *_a, **_k: pytest.fail("Unrelated node scan"))
    monkeypatch.setattr(remote_proxy, "install_ai_proxy", lambda *_a, **_k: pytest.fail("Must preserve real proxy"))
    result = remote_proxy.install_ai_proxy_verified(
        "synthetic", "{name: US candidate, type: http, server: example.invalid, port: 8080}",
    )
    assert calls == ["isolated"]
    assert result.outcome == "failed" and result.warning and not result.retryable
    assert "未修改正式代理" in result and "独立分流" in result
