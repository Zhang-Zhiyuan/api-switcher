"""Credential-free probe semantics, using synthetic responses only."""
import ast
from datetime import datetime, timezone
import json

import pytest

from core import local_proxy, remote_proxy
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES
from core.proxy_health import parse_proxy_health, proxy_health_summary


@pytest.mark.parametrize("service_id,expected", [
    ("openai", "200/401"), ("claude", "200/400/401"), ("google_ai", "200/400/401/403"),
])
def test_local_and_remote_groups_share_explicit_health_contract(service_id, expected):
    service = next(item for item in LOCAL_PROXY_AI_SERVICES if item["id"] == service_id)
    url, statuses = local_proxy._service_route_health_contract((service_id,))
    assert statuses == service["health_check_expected_status"] == expected
    group = remote_proxy._managed_proxy_group(
        "SUB-123456789ABC-PROXY", ["synthetic"], health_checked=True,
        health_check_url=url, expected_status=statuses,
    )
    assert group["expected-status"] == expected
    accepted = {int(value) for value in statuses.split("/")}
    assert not accepted.intersection({301, 302, 402, 404, 405, 407, 418, 429, 500, 503})
    if service_id == "claude":
        assert 403 not in accepted


def test_gemini_controller_success_explains_status_only_limit():
    service = next(item for item in LOCAL_PROXY_AI_SERVICES if item["id"] == "google_ai")
    url = service["health_check_url"]
    now = datetime.now(timezone.utc)
    node = {"extra": {url: {"alive": True, "history": [{"time": now.isoformat(), "delay": 24}]}}}
    health = parse_proxy_health({"testUrl": url}, node, now)
    assert health.target_healthy is True  # HTTP observation, not an account verdict.
    assert health.response_validation_required
    assert "仍需业务验证" in proxy_health_summary(health)
    node["extra"][url]["alive"] = False
    assert "失败" in proxy_health_summary(parse_proxy_health({"testUrl": url}, node, now))


@pytest.mark.parametrize("body", [
    '<html>blocked: {"error":{"message":"x-api-key is required"}}</html>',
    '{"error":"authentication_error"}',
    '{"other":{"error":{"message":"x-api-key is required"}}}',
    '{"type":"error","error":{"message":"x-api-key blocked by region policy"}}',
    '{"error":{"message":"unrelated"},"documentation":"authentication x-api-key"}',
])
def test_anthropic_challenge_requires_real_top_level_error(body):
    assert not local_proxy._classify_ai_probe_response("Claude/Anthropic", 401, body)[0]


@pytest.mark.parametrize("status", [400, 401])
def test_anthropic_valid_unauthenticated_challenge_is_preserved(status):
    body = '{"type":"error","error":{"type":"authentication_error","message":"x-api-key header is required"}}'
    assert local_proxy._classify_ai_probe_response("Claude/Anthropic", status, body)[0]


@pytest.mark.parametrize("body,expected", [
    ('{"error":{"code":403,"message":"Please use API Key for Google Generative Language"}}', True),
    ('{"error":{"code":403,"message":"API key blocked by region policy"}}', False),
    ('<html>403 API key error</html>', False),
])
def test_google_403_authentication_and_policy_are_not_conflated(body, expected):
    assert local_proxy._classify_ai_probe_response("Gemini/Google AI", 403, body)[0] is expected


@pytest.mark.parametrize("label,status,error,expected", [
    ("Claude/Anthropic", 401, {"type": "authentication_error", "message": "x-api-key is required"}, True),
    ("Claude/Anthropic", 400, {"message": "anthropic-version is required"}, True),
    ("Claude/Anthropic", 401, {"message": "x-api-key blocked by policy"}, False),
    ("Claude/Anthropic", 403, {"type": "authentication_error"}, False),
    ("Gemini/Google AI", 403, {"message": "API key required", "status": "PERMISSION_DENIED"}, True),
    ("Gemini/Google AI", 403, {"message": "API key location not supported"}, False),
    ("Gemini/Google AI", 403, {"message": "API key disabled by policy"}, False),
    ("Gemini/Google AI", 403, {"message": "API key required", "status": "RESOURCE_EXHAUSTED"}, False),
    ("Gemini/Google AI", 429, {"message": "API key required"}, False),
    ("OpenAI API", 401, {"message": "Incorrect API key", "type": "invalid_request_error"}, True),
    ("OpenAI API", 401, {"message": "unrecognized upstream error"}, False),
    ("OpenAI API", 403, {"message": "Incorrect API key"}, False),
])
def test_windows_and_generated_ssh_strict_probe_classifiers_agree(label, status, error, expected):
    command = remote_proxy._build_probe_command(7890, strict=True)
    program = command.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    tree = ast.parse(program)
    # Execute only pure definitions; never execute the generated network probes.
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"json": json, "strict": True}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<synthetic-classifiers>", "exec"), namespace)
    body = json.dumps({"error": error})
    assert namespace["classify"](label, status, body)[0] is expected
    assert local_proxy._classify_ai_probe_response(label, status, body)[0] is expected


@pytest.mark.parametrize("service_id", ["claude", "google_ai"])
def test_old_probe_contract_is_recognized_but_never_generated(service_id):
    service = next(item for item in LOCAL_PROXY_AI_SERVICES if item["id"] == service_id)
    node = {"name": "synthetic", "type": "http", "server": "proxy.example.invalid", "port": 8080}
    spec = {"name": "SUB-123456789ABC-PROXY", "proxy_node": node,
            "health_checked": True, "health_check_url": service["health_check_url"],
            "health_check_expected_status": service["health_check_expected_status"]}
    current = remote_proxy.build_mihomo_config(
        node, strict_privacy=True, additional_proxy_groups=[spec],
        proxy_domain_routes={service["targets"][0]: spec["name"]},
    )
    assert remote_proxy._managed_config_strict_privacy_enabled(current)
    legacy = current.replace(spec["health_check_expected_status"], "200-499")
    assert legacy != current
    assert remote_proxy._managed_config_strict_privacy_enabled(legacy)
    assert not remote_proxy._managed_config_strict_privacy_enabled(legacy.replace("200-499", "200-599"))
    with pytest.raises(ValueError, match="健康检查契约"):
        remote_proxy.build_mihomo_config(node, additional_proxy_groups=[{**spec, "health_check_expected_status": "200-499"}])
