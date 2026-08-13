import pytest

from core import auth_parser, switch_preview, toml_parser
from core.api_tester import APITester
from core.providers import ProviderRegistry
from models.profile import (
    CODEX_APPROVAL_POLICIES,
    CODEX_SANDBOX_MODES,
    CodexProfile,
)
from ui.dialogs.profile_editor import ProfileEditorDialog


@pytest.mark.parametrize(
    ("approval", "sandbox", "expected_approval", "expected_sandbox"),
    [
        ("manual", "off", "on-request", "danger-full-access"),
        ("auto", "workspace-write", "on-request", "workspace-write"),
        ("UNTRUSTED", "READ-ONLY", "untrusted", "read-only"),
        ("future-value", "unknown", "untrusted", "read-only"),
        (None, None, "untrusted", "read-only"),
    ],
)
def test_codex_profile_migrates_legacy_runtime_options_fail_closed(
    approval,
    sandbox,
    expected_approval,
    expected_sandbox,
):
    profile = CodexProfile.from_dict(
        {
            "name": "legacy",
            "approval_policy": approval,
            "sandbox_mode": sandbox,
        }
    )

    assert profile.approval_policy == expected_approval
    assert profile.sandbox_mode == expected_sandbox


def test_codex_runtime_option_contract_matches_current_cli_values():
    assert CODEX_APPROVAL_POLICIES == ("untrusted", "on-request", "never")
    assert CODEX_SANDBOX_MODES == ("read-only", "workspace-write", "danger-full-access")


def test_apply_codex_profile_canonicalizes_runtime_option_case():
    profile = CodexProfile(
        name="case-normalized",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_env_key="RELAY_API_KEY",
        approval_policy="ON-REQUEST",
        sandbox_mode="WORKSPACE-WRITE",
    )

    updated = toml_parser.apply_codex_profile({}, profile)

    assert updated["approval_policy"] == "on-request"
    assert updated["sandbox_mode"] == "workspace-write"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("approval_policy", "manual", "审批策略"),
        ("sandbox_mode", "off", "沙盒模式"),
    ],
)
def test_apply_codex_profile_rejects_invalid_runtime_values_before_mutating(field, value, message):
    profile = CodexProfile(
        name="invalid",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_env_key="RELAY_API_KEY",
    )
    setattr(profile, field, value)
    original = {"sentinel": {"unchanged": True}}

    with pytest.raises(ValueError, match=message):
        toml_parser.apply_codex_profile(original, profile)

    assert original == {"sentinel": {"unchanged": True}}


def test_preview_reports_invalid_runtime_options_as_blocking_error(monkeypatch):
    profile = CodexProfile(
        name="invalid",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_requires_openai_auth=True,
        approval_policy="manual",
    )
    monkeypatch.setattr(switch_preview.profile_manager, "is_third_party_codex_profile", lambda _profile: True)
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: {"tokens": {"access_token": "present"}})

    checks = switch_preview._validate_codex_api_target(profile)

    assert any(check.item == "运行权限配置" and check.status == "error" for check in checks)


def test_codex_editor_excludes_official_openai_and_migrates_legacy_selection_to_custom():
    providers = ProfileEditorDialog._codex_editor_providers()

    assert providers
    assert "openai" not in {provider.name for provider in providers}
    assert "custom" in {provider.name for provider in providers}
    assert ProfileEditorDialog._codex_initial_provider_name(
        CodexProfile(name="legacy-openai", model_provider="openai"),
        providers,
    ) == ProviderRegistry.get_provider("custom").display_name


def _save_dialog(data: dict):
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = "codex"
    dialog._profile = None
    dialog._fields = {}
    dialog._collect_data = lambda: dict(data)
    dialog._get_secret_value = lambda *_args: ""
    errors = []
    saves = []
    destroyed = []
    dialog._show_error = errors.append
    dialog._on_save = lambda payload, profile: saves.append((payload, profile))
    dialog.destroy = lambda: destroyed.append(True)
    return dialog, errors, saves, destroyed


def test_codex_editor_rejects_forged_official_openai_provider():
    dialog, errors, saves, destroyed = _save_dialog(
        {
            "name": "official-does-not-belong-here",
            "codex_provider": ProviderRegistry.get_provider("openai").display_name,
            "model": "gpt-5.5",
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
        }
    )

    ProfileEditorDialog._save(dialog)

    assert errors and "官方账号" in errors[0]
    assert saves == []
    assert destroyed == []


def test_custom_openai_auth_save_requires_no_provider_key_and_writes_no_env_key():
    dialog, errors, saves, destroyed = _save_dialog(
        {
            "name": "gateway-login",
            "codex_provider": "Custom",
            "api_key": "must-not-be-saved",
            "custom_base_url": "https://gateway.example.test/v1",
            "custom_name": "Login Gateway",
            "custom_env_key": "RELAY_API_KEY",
            "custom_requires_openai_auth": True,
            "model": "gpt-5.5",
            "model_reasoning_effort": "high",
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
        }
    )
    dialog._get_secret_value = lambda *_args: (_ for _ in ()).throw(
        AssertionError("OpenAI-auth mode must not request a provider key")
    )

    ProfileEditorDialog._save(dialog)

    assert errors == []
    assert destroyed == [True]
    payload = saves[0][0]
    assert payload["model_provider"] == "custom"
    assert payload["custom_requires_openai_auth"] is True
    assert payload["api_key"] == ""
    assert payload["custom_env_key"] is None


def test_custom_openai_auth_connection_test_is_local_precheck_only(monkeypatch):
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = "codex"
    dialog._profile = None
    dialog._test_busy = False
    dialog._fields = {}
    dialog._collect_data = lambda: {
        "name": "gateway-login",
        "custom_base_url": "https://gateway.example.test/v1",
        "custom_requires_openai_auth": True,
        "model": "gpt-5.5",
        "approval_policy": "on-request",
        "sandbox_mode": "workspace-write",
    }
    dialog._current_codex_provider = lambda: ProviderRegistry.get_provider("custom")
    dialog._get_secret_value = lambda *_args: (_ for _ in ()).throw(
        AssertionError("OpenAI-auth precheck must not read a provider key")
    )
    errors = []
    results = []
    dialog._show_error = errors.append
    dialog._apply_test_result = lambda result, name: results.append((result, name))
    monkeypatch.setattr(
        toml_parser,
        "read_codex_config",
        lambda: {"cli_auth_credentials_store": "file"},
    )
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: {"tokens": {"access_token": "present"}})
    monkeypatch.setattr(
        APITester,
        "benchmark_openai_wire_apis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network test must not run")),
    )

    ProfileEditorDialog._test_connection(dialog)

    assert errors == []
    assert results[0][0].success is True
    assert "未发起网络请求" in results[0][0].message
    assert results[0][1] == "gateway-login"


def test_codex_editor_save_rejects_invalid_runtime_option_before_callback():
    dialog, errors, saves, destroyed = _save_dialog(
        {
            "name": "invalid-runtime",
            "codex_provider": "Custom",
            "custom_base_url": "https://gateway.example.test/v1",
            "custom_requires_openai_auth": True,
            "model": "gpt-5.5",
            "approval_policy": "manual",
            "sandbox_mode": "workspace-write",
        }
    )

    ProfileEditorDialog._save(dialog)

    assert errors and "审批策略" in errors[0]
    assert saves == []
    assert destroyed == []


def test_codex_editor_connection_precheck_rejects_invalid_runtime_option():
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = "codex"
    dialog._profile = None
    dialog._test_busy = False
    dialog._fields = {}
    dialog._collect_data = lambda: {
        "name": "invalid-runtime",
        "custom_base_url": "https://gateway.example.test/v1",
        "custom_requires_openai_auth": True,
        "model": "gpt-5.5",
        "approval_policy": "manual",
        "sandbox_mode": "workspace-write",
    }
    dialog._current_codex_provider = lambda: ProviderRegistry.get_provider("custom")
    errors = []
    dialog._show_error = errors.append

    ProfileEditorDialog._test_connection(dialog)

    assert errors and "审批策略" in errors[0]


def test_custom_openai_auth_precheck_handles_unreadable_or_missing_login(monkeypatch):
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: {"cli_auth_credentials_store": "file"})
    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: {})
    missing = ProfileEditorDialog._codex_openai_auth_precheck(
        "https://gateway.example.test/v1",
        "gpt-5.5",
    )
    assert missing.success is False
    assert "未找到" in missing.message

    monkeypatch.setattr(auth_parser, "read_codex_auth", lambda: (_ for _ in ()).throw(ValueError("bad json")))
    unreadable = ProfileEditorDialog._codex_openai_auth_precheck(
        "https://gateway.example.test/v1",
        "gpt-5.5",
    )
    assert unreadable.success is False
    assert "无法读取" in unreadable.message
    assert "bad json" in unreadable.error_details


def test_custom_openai_auth_precheck_does_not_trust_stale_non_file_auth(monkeypatch):
    monkeypatch.setattr(toml_parser, "read_codex_config", lambda: {"cli_auth_credentials_store": "auto"})
    monkeypatch.setattr(
        auth_parser,
        "read_codex_auth",
        lambda: (_ for _ in ()).throw(AssertionError("stale auth.json must not be read")),
    )

    result = ProfileEditorDialog._codex_openai_auth_precheck(
        "https://gateway.example.test/v1",
        "gpt-5.5",
    )

    assert result.success is False
    assert "无法静态确认" in result.message
    assert "codex login status" in result.error_details


def test_openai_auth_toml_removes_stale_env_key():
    profile = CodexProfile(
        name="gateway-login",
        model_provider="custom",
        custom_base_url="https://gateway.example.test/v1",
        custom_name="Login Gateway",
        custom_env_key="RELAY_API_KEY",
        custom_requires_openai_auth=True,
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )
    config = {
        "model_providers": {
            "custom": {
                "base_url": "https://old.example.test/v1",
                "env_key": "OLD_API_KEY",
            }
        }
    }

    updated = toml_parser.apply_codex_profile(config, profile)

    provider = updated["model_providers"]["custom"]
    assert provider["requires_openai_auth"] is True
    assert "env_key" not in provider


def test_profile_validation_rejects_invalid_direct_instances_before_env_resolution(monkeypatch):
    profile = CodexProfile(
        name="invalid",
        model_provider="custom",
        custom_requires_openai_auth=True,
        approval_policy="auto",
    )
    monkeypatch.setattr(
        ProviderRegistry,
        "get_codex_env_key_for_profile",
        lambda _profile: (_ for _ in ()).throw(AssertionError("env resolution must not run")),
    )

    with pytest.raises(ValueError, match="审批策略"):
        profile.validated_env_key()
