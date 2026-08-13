import hashlib
import json
import urllib.error

from core.api_tester import APITester
from ui.tabs.common_tab import _build_overview_text, _short_secret


def test_error_body_redacts_request_secret_and_common_token_shapes():
    secret = "sk-ant-api03-super-secret-value"
    body = json.dumps(
        {
            "error": {
                "message": (
                    f"invalid x-api-key: {secret}; "
                    "Authorization: Bearer eyJabcdefghijk.abcdefghijk.abcdefghi"
                )
            }
        }
    )

    detail = APITester._parse_error_body(body, (secret, f"Bearer {secret}"))

    assert secret not in detail
    assert "eyJabcdefghijk" not in detail
    assert "[REDACTED]" in detail


def test_error_body_redacts_short_request_secret_exactly():
    detail = APITester._parse_error_body(
        json.dumps({"error": {"message": "invalid key: abc"}}),
        ("abc",),
    )

    assert "abc" not in detail
    assert detail == "invalid key: [REDACTED]"


def test_request_errors_redact_header_secret(monkeypatch):
    secret = "short-secret"

    def raise_network_error(*_args, **_kwargs):
        raise urllib.error.URLError(f"upstream echoed {secret}")

    monkeypatch.setattr("core.api_tester.urllib.request.urlopen", raise_network_error)

    ok, _data, result = APITester._request_json(
        "https://example.test/v1/models",
        {"Authorization": f"Bearer {secret}"},
    )

    assert ok is False
    assert secret not in (result.error_details or "")
    assert "[REDACTED]" in (result.error_details or "")


def test_stream_unknown_errors_redact_header_secret(monkeypatch, caplog):
    secret = "another-short-secret"

    def raise_unknown_error(*_args, **_kwargs):
        raise RuntimeError(f"adapter echoed {secret}")

    monkeypatch.setattr("core.api_tester.urllib.request.urlopen", raise_unknown_error)

    result = APITester._request_event_stream(
        "https://example.test/v1/responses",
        {"x-api-key": secret},
        {"model": "test"},
    )

    assert secret not in (result.error_details or "")
    assert secret not in caplog.text
    assert "[REDACTED]" in (result.error_details or "")


def test_short_secret_never_reveals_even_short_credentials():
    for secret in ("a", "abc", "short-key", "sk-longer-secret-value"):
        rendered = _short_secret(secret)
        assert rendered.startswith("sha256:")
        assert rendered != secret
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:8]
        if len(secret) < 4:
            assert rendered == f"sha256:{fingerprint}"
        else:
            assert rendered == f"sha256:{fingerprint}…{secret[-4:]}"


def test_overview_never_contains_full_claude_or_codex_secret(monkeypatch):
    claude_secret = "claude-secret-value"
    codex_secret = "codex-secret-value"
    monkeypatch.setattr("ui.tabs.common_tab._codex_env_value", lambda _key: codex_secret)

    text = _build_overview_text(
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://example.test",
                "ANTHROPIC_API_KEY": claude_secret,
            }
        },
        {
            "model_provider": "custom",
            "model_providers": {"custom": {"env_key": "CUSTOM_API_KEY"}},
        },
        {},
        {},
    )

    assert claude_secret not in text
    assert codex_secret not in text
    assert text.count("sha256:") == 2
