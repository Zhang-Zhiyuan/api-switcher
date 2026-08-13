import pytest

from core import backup_manager, profile_manager, security, switch_preview, switcher, toml_parser
from core.env_validation import validate_codex_env_key
from models.profile import CodexProfile
from ui.dialogs.profile_editor import ProfileEditorDialog
import ui.dialogs.profile_editor as profile_editor_module


@pytest.mark.parametrize(
    "name",
    ("OPENAI_API_KEY", "RELAY_API_KEY", "CUSTOM_KEY", "MY_PROVIDER_TOKEN"),
)
def test_validate_codex_env_key_accepts_dedicated_secret_names(name):
    assert validate_codex_env_key(name) == name


@pytest.mark.parametrize(
    "name",
    (
        "PATH",
        "HOME",
        "APPDATA",
        "CODEX_HOME",
        "HTTP_PROXY",
        "PYTHONPATH",
        "lowercase_api_key",
        "NOT-A-NAME",
        "ORDINARY_SETTING",
    ),
)
def test_validate_codex_env_key_rejects_system_or_non_secret_names(name):
    with pytest.raises(ValueError):
        validate_codex_env_key(name)


def _unsafe_profile() -> CodexProfile:
    return CodexProfile(
        name="unsafe",
        api_key_ref="codex:unsafe:api_key",
        model="relay-model",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_env_key="PATH",
    )


def test_codex_profile_model_exposes_fail_closed_env_validation():
    with pytest.raises(ValueError, match="PATH"):
        _unsafe_profile().validated_env_key()


def test_save_codex_profile_rejects_unsafe_env_key_before_store_write(monkeypatch, tmp_path):
    profiles_path = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_path)
    profile_manager.clear_profile_store_cache()

    with pytest.raises(ValueError, match="PATH"):
        profile_manager.save_codex_profile(_unsafe_profile())

    assert not profiles_path.exists()


def test_save_codex_profile_with_secrets_validates_before_secret_mutation(monkeypatch):
    writes = []
    monkeypatch.setattr(security, "set_secret", lambda *args: writes.append(args))

    with pytest.raises(ValueError, match="PATH"):
        profile_manager.save_codex_profile_with_secrets(
            _unsafe_profile(),
            {"codex:unsafe:api_key": "secret"},
        )

    assert writes == []


def test_preview_reports_unsafe_env_key_as_blocking_error(monkeypatch):
    profile = _unsafe_profile()
    monkeypatch.setattr(security, "get_secret", lambda _ref: "secret")

    checks = switch_preview._validate_codex_api_target(profile)

    assert any(
        check.item == "环境变量名" and check.status == "error" and "PATH" in check.message
        for check in checks
    )


def test_switch_rejects_unsafe_env_key_before_backup_or_environment_mutation(monkeypatch):
    profile = _unsafe_profile()
    original_path = "safe-path-sentinel"
    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [profile])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _profile: True)
    monkeypatch.setattr(security, "get_secret", lambda _ref: "secret")
    monkeypatch.setattr(
        switcher,
        "_ensure_switch_target_healthy",
        lambda *_args: (_ for _ in ()).throw(AssertionError("validation must happen first")),
    )
    monkeypatch.setattr(
        backup_manager,
        "create_backup",
        lambda *_args: (_ for _ in ()).throw(AssertionError("backup must not run")),
    )

    with pytest.raises(ValueError, match="PATH"):
        switcher.switch_codex_profile(profile.name)

    assert switcher.os.environ["PATH"] == original_path


def test_cleanup_skips_unsafe_legacy_profile_and_config_env_keys(monkeypatch):
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [_unsafe_profile()])

    names = switcher._codex_api_env_names_to_clear(
        {
            "model_provider": "custom",
            "model_providers": {"custom": {"env_key": "PATH"}},
        }
    )

    assert "PATH" not in names
    assert "OPENAI_API_KEY" in names


def test_apply_codex_profile_rejects_unsafe_env_key():
    with pytest.raises(ValueError, match="PATH"):
        toml_parser.apply_codex_profile({}, _unsafe_profile())


def test_codex_editor_rejects_unsafe_env_key_before_callback(monkeypatch):
    dialog = object.__new__(ProfileEditorDialog)
    dialog._profile_type = "codex"
    dialog._profile = None
    dialog._fields = {}
    dialog._collect_data = lambda: {
        "name": "unsafe",
        "api_key": "secret",
        "codex_provider": "Custom",
        "custom_base_url": "https://relay.example.test/v1",
        "custom_name": "Relay",
        "custom_env_key": "PATH",
        "model": "relay-model",
    }
    dialog._get_secret_value = lambda *_args: "secret"
    errors = []
    saves = []
    destroyed = []
    dialog._show_error = errors.append
    dialog._on_save = lambda *args: saves.append(args)
    dialog.destroy = lambda: destroyed.append(True)
    monkeypatch.setattr(
        profile_editor_module.ProviderRegistry,
        "get_provider_by_display_name",
        lambda _name: None,
    )
    monkeypatch.setattr(
        profile_editor_module.ProviderRegistry,
        "get_codex_wire_api",
        lambda *_args: "responses",
    )

    ProfileEditorDialog._save(dialog)

    assert errors and "PATH" in errors[0]
    assert saves == []
    assert destroyed == []
