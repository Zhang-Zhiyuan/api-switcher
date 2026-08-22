"""Security and consistency checks for configurable API endpoint URLs."""
from __future__ import annotations

import json
import urllib.request

import pytest

from core import security, switch_preview
from core.api_tester import APITester
from core.providers import PROVIDERS
from core.url_validation import APIBaseURLValidationError, canonicalize_api_base_url
from models.profile import CodexProfile
from ui.dialogs.profile_editor import ProfileEditorDialog


class _JSONResponse:
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"data": [{"id": "test-model"}]}).encode("utf-8")

    def getcode(self):
        return 200


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api.example.test/v1?region=cn", "https://api.example.test/v1?region=cn"),
        ("HTTPS://API.EXAMPLE.TEST/v1/", "https://api.example.test/v1"),
        ("http://localhost:8080/v1/", "http://localhost:8080/v1"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
    ],
)
def test_canonicalize_api_base_url_accepts_safe_endpoints(raw, expected):
    assert canonicalize_api_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:password@api.example.test/v1",
        "https://api.example.test/v1#credentials",
        "https:///v1",
        "ftp://api.example.test/v1",
        "http://api.example.test/v1",
        "http://localhost.example.test/v1",
        "http://localhost./v1",
        "https://api.example.test\\@evil.example/v1",
        "https://api.example.test:invalid/v1",
        "\nhttps://api.example.test/v1",
        "https://api.example.test/v1\r\nX-Injected: yes",
    ],
)
def test_canonicalize_api_base_url_rejects_unsafe_endpoints(raw):
    with pytest.raises(APIBaseURLValidationError):
        canonicalize_api_base_url(raw)


def test_all_nonempty_provider_presets_pass_shared_url_validation():
    for provider in PROVIDERS.values():
        candidates = {
            provider.default_base_url,
            provider.base_url_for_claude(),
            provider.base_url_for_codex(),
        }
        for candidate in candidates:
            if candidate:
                assert canonicalize_api_base_url(candidate).startswith("https://"), (
                    provider.name,
                    candidate,
                )


@pytest.mark.parametrize(
    "call",
    [
        lambda: APITester.fetch_openai_models(
            "sk-test", "https://leaked:password@api.example.test/v1"
        ),
        lambda: APITester.fetch_claude_models(
            "sk-test", "https://leaked:password@api.example.test"
        ),
        lambda: APITester.test_openai_api(
            "sk-test", "http://api.example.test/v1", "gpt-test"
        ),
        lambda: APITester.test_claude_api(
            "sk-test", "https://api.example.test/#bad", "claude-test"
        ),
        lambda: APITester.benchmark_openai_wire_apis(
            "sk-test", "https://api.example.test:bad/v1", "gpt-test"
        ),
        lambda: APITester.test_url_reachable("https://user:secret@api.example.test"),
    ],
)
def test_api_tester_rejects_unsafe_urls_before_network(monkeypatch, call):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("unsafe URL must not reach the network"),
    )

    result = call()

    assert result.success is False
    assert result.message == "API 端点无效"
    assert "password" not in str(result.error_details)
    assert "secret" not in str(result.error_details)


def test_api_tester_canonicalizes_missing_scheme_and_preserves_query(monkeypatch):
    seen_urls: list[str] = []

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        return _JSONResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = APITester.fetch_openai_models(
        "sk-test",
        "API.EXAMPLE.TEST/v1/?region=cn",
    )

    assert result.success is True
    assert seen_urls == ["https://api.example.test/v1/models?region=cn"]


def test_switch_preview_uses_same_validation_and_redacts_userinfo(monkeypatch):
    monkeypatch.setattr(security, "get_secret", lambda _ref: "sk-test")
    bad = CodexProfile(
        name="Bad URL",
        api_key_ref="codex:bad:api_key",
        model="gpt-test",
        model_provider="custom",
        custom_name="Custom",
        custom_base_url="https://name:supersecret@api.example.test/v1",
        custom_env_key="CUSTOM_API_KEY",
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )

    checks = switch_preview._validate_codex_api_target(bad)
    url_check = next(check for check in checks if check.item == "Base URL")

    assert url_check.status == "error"
    assert "用户名或密码" in url_check.message
    assert "supersecret" not in url_check.message


def test_switch_preview_canonicalizes_implicit_https(monkeypatch):
    monkeypatch.setattr(security, "get_secret", lambda _ref: "sk-test")
    profile = CodexProfile(
        name="Implicit HTTPS",
        api_key_ref="codex:implicit:api_key",
        model="gpt-test",
        model_provider="custom",
        custom_name="Custom",
        custom_base_url="api.example.test/v1/",
        custom_env_key="CUSTOM_API_KEY",
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )

    checks = switch_preview._validate_codex_api_target(profile)
    url_check = next(check for check in checks if check.item == "Base URL")

    assert url_check.status == "ok"
    assert url_check.message == "https://api.example.test/v1"


def _editor_save_result(profile_type: str, data: dict):
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = profile_type
    dialog._profile = None
    dialog._fields = {}
    dialog._collect_data = lambda: dict(data)
    dialog._get_secret_value = lambda *_args: "sk-test"
    errors: list[str] = []
    saves: list[dict] = []
    destroyed: list[bool] = []
    dialog._show_error = errors.append
    dialog._on_save = lambda payload, _profile: saves.append(payload)
    dialog.destroy = lambda: destroyed.append(True)
    ProfileEditorDialog._save(dialog)
    return errors, saves, destroyed


@pytest.mark.parametrize("profile_type", ["claude", "codex"])
def test_profile_editor_persists_the_same_canonical_url_used_by_preview(profile_type):
    custom = PROVIDERS["custom"]
    common = {
        "name": "Canonical URL",
        "model": "test-model",
    }
    if profile_type == "claude":
        data = {
            **common,
            "provider": custom.display_name,
            "base_url": "API.EXAMPLE.TEST/v1/",
        }
        expected_field = "base_url"
    else:
        data = {
            **common,
            "codex_provider": custom.display_name,
            "custom_base_url": "API.EXAMPLE.TEST/v1/",
            "custom_name": "Custom",
            "custom_env_key": "CUSTOM_API_KEY",
            "custom_requires_openai_auth": False,
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
        }
        expected_field = "custom_base_url"

    errors, saves, destroyed = _editor_save_result(profile_type, data)

    assert errors == []
    assert destroyed == [True]
    expected_url = "https://api.example.test" if profile_type == "claude" else "https://api.example.test/v1"
    assert saves[0][expected_field] == expected_url


@pytest.mark.parametrize("profile_type", ["claude", "codex"])
def test_profile_editor_blocks_userinfo_without_echoing_password(profile_type):
    custom = PROVIDERS["custom"]
    common = {
        "name": "Unsafe URL",
        "model": "test-model",
    }
    if profile_type == "claude":
        data = {
            **common,
            "provider": custom.display_name,
            "base_url": "https://user:supersecret@api.example.test/v1",
        }
    else:
        data = {
            **common,
            "codex_provider": custom.display_name,
            "custom_base_url": "https://user:supersecret@api.example.test/v1",
            "custom_name": "Custom",
            "custom_env_key": "CUSTOM_API_KEY",
            "custom_requires_openai_auth": False,
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
        }

    errors, saves, destroyed = _editor_save_result(profile_type, data)

    assert errors and "用户名或密码" in errors[0]
    assert "supersecret" not in errors[0]
    assert saves == []
    assert destroyed == []


def test_openai_auth_local_precheck_uses_shared_url_validation():
    result = ProfileEditorDialog._codex_openai_auth_precheck(
        "https://user:supersecret@api.example.test/v1",
        "gpt-test",
    )

    assert result.success is False
    assert "API 端点无效" in result.message
    assert "supersecret" not in result.message
