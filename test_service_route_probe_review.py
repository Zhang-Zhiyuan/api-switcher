"""Independent anonymous-response contract; no accounts, sockets or live state."""
import copy
import json
import pickle

import pytest

from core import service_route_verification as verification
from core.proxy_update_result import update_result, with_update_message


def target(service):
    return verification.bound_route_targets({
        "service_profile_bindings": {service: "synthetic-subscription"},
        "builtin_sites": {service: True},
    })[0]


@pytest.mark.parametrize("service,status,payload", [
    ("openai", 401, {"error": {"message": "You did not provide an API key."}}),
    ("claude", 400, {"error": {"message": "anthropic-version header is required"}}),
    ("google_ai", 403, {"error": {"message": "Method doesn't allow unregistered callers", "status": "PERMISSION_DENIED"}}),
    ("openai", 200, {"object": "list", "data": [{"id": "synthetic-model"}]}),
    ("claude", 200, {"data": [], "has_more": False}),
    ("google_ai", 200, {"models": []}),
    ("discord", 200, {"url": "wss://gateway.discord.gg"}),
])
def test_valid_anonymous_shapes_are_not_mistaken_for_network_failure(service, status, payload):
    outcome, _ = verification.classify_response(target(service), status, json.dumps(payload))
    assert outcome == "passed"


@pytest.mark.parametrize("service,status", [
    ("openai", 401), ("claude", 400), ("google_ai", 403),
])
def test_country_rejection_is_not_accepted_as_a_normal_auth_challenge(service, status):
    body = json.dumps({"error": {"message": "API key request blocked in this country"}})
    assert verification.classify_response(target(service), status, body)[0] == "failed"


@pytest.mark.parametrize("service", ["openai", "claude", "google_ai", "github", "huggingface", "x_twitter", "reddit", "discord"])
def test_interception_html_cannot_pass_api_or_robots_probe(service):
    body = "<!DOCTYPE html><html><title>Sign in to this Wi-Fi</title></html>"
    assert verification.classify_response(target(service), 200, body)[0] == "failed"


@pytest.mark.parametrize("service", ["openai", "claude", "google_ai", "discord"])
@pytest.mark.parametrize("body", ["{}", "[]", '{"error":"synthetic block"}', '{"ok":true}'])
def test_arbitrary_http_200_json_is_not_service_evidence(service, body):
    assert verification.classify_response(target(service), 200, body)[0] == "failed"


@pytest.mark.parametrize("service,payload", [
    ("openai", {"object": "list", "data": []}),
    ("claude", {"data": [], "has_more": False}),
    ("google_ai", {"models": []}),
    ("discord", {"url": "wss://gateway.discord.gg"}),
])
def test_explicit_error_is_not_hidden_by_success_shaped_fields(service, payload):
    payload["error"] = {"message": "request blocked in this country"}
    assert verification.classify_response(target(service), 200, json.dumps(payload))[0] == "failed"


def test_discord_public_gateway_is_not_an_ai_auth_challenge():
    body = json.dumps({"error": {"message": "unregistered caller needs API key"}})
    assert verification.classify_response(target("discord"), 403, body)[0] == "failed"


def test_public_html_page_and_no_content_probe_have_different_contracts():
    telegram = '<!doctype html><html><head><title>Telegram</title></head><body>Telegram</body></html>'
    assert verification.classify_response(target("telegram"), 200, telegram)[0] == "passed"
    assert verification.classify_response(target("youtube"), 204, "")[0] == "passed"
    assert verification.classify_response(target("youtube"), 204, "unexpected content")[0] == "failed"


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))])
def test_route_observations_survive_wrapping_and_existing_copy_contract(clone):
    observed = verification.attach_verification(update_result("synthetic config loaded", "applied"), (
        verification.record(target("claude"), "failed", "synthetic timeout"),
    ))
    wrapped = clone(with_update_message(observed, "scope: " + str(observed)))
    assert wrapped.applied and wrapped.warning and not wrapped.retryable
    assert len(wrapped.route_verification) == 1
    assert wrapped.route_verification[0]["service"] == "claude"
    assert wrapped.route_verification[0]["status"] == "failed"
    assert "账号" in str(wrapped)
