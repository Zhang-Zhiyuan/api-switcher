"""Account-only migration uses synthetic credentials and an in-memory keyring."""
import base64
import json
import zlib

import pytest

from config import paths
from core import account_transfer as transfer
from core import auth_parser, parser, portable_migration, profile_manager, security, toml_parser
from models.profile import ClaudeAccountProfile, CodexAccountProfile


PASSWORD = "synthetic-transfer-password"


@pytest.fixture()
def account_env(tmp_path, monkeypatch):
    secrets = {}
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    claude_dir, codex_dir = user_dir / ".claude", user_dir / ".codex"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "app" / "profiles.json")
    monkeypatch.setattr(paths, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(parser, "CLAUDE_CREDENTIALS", claude_dir / ".credentials.json")
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", claude_dir / "settings.json")
    # The real .claude.json may be directly under the home directory.
    monkeypatch.setattr(parser, "CLAUDE_CONFIG", user_dir / ".claude.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", codex_dir / "auth.json")
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", codex_dir / "config.toml")
    monkeypatch.setattr(paths, "CODEX_ENV", codex_dir / ".env")
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secrets.get(key))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secrets[key]) if key in secrets else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, value: secrets.__setitem__(key, json.dumps(value)))
    monkeypatch.setattr(security, "set_secret", lambda key, value: secrets.__setitem__(key, value))
    monkeypatch.setattr(security, "delete_secret", lambda key: secrets.pop(key, None))
    for clear in (profile_manager.clear_profile_store_cache, parser.clear_claude_file_cache,
                  auth_parser.clear_codex_auth_cache, toml_parser.clear_codex_config_cache):
        clear()
    yield tmp_path, secrets
    for clear in (profile_manager.clear_profile_store_cache, parser.clear_claude_file_cache,
                  auth_parser.clear_codex_auth_cache, toml_parser.clear_codex_config_cache):
        clear()


def _credentials(kind, *, token="synthetic-access", email="account@example.test"):
    if kind == "codex":
        return {"auth_mode": "chatgpt", "tokens": {
            "access_token": token, "refresh_token": "synthetic-refresh", "account_id": email,
        }, "email": email, "last_refresh": "2026-09-21T00:00:00Z"}
    return {"claudeAiOauth": {
        "accessToken": token, "refreshToken": "synthetic-refresh", "expiresAt": 1900000000000,
        "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro",
    }, "email": email}


def _write_current(kind, credentials):
    if kind == "codex":
        toml_parser.write_codex_config({"cli_auth_credentials_store": "file"})
        auth_parser.write_codex_auth(credentials)
    else:
        parser.write_claude_credentials(credentials)


def _save(kind, name, credentials):
    identity = profile_manager._identity_from_json(credentials, f"{kind}-login")
    if kind == "codex":
        profile = CodexAccountProfile(name, f"codex-account:{name}:auth_json", identity)
        profile_manager.save_codex_account_profile_with_auth(profile, credentials)
    else:
        profile = ClaudeAccountProfile(name, f"claude-account:{name}:credentials", identity)
        profile_manager.save_claude_account_profile_with_credentials(profile, credentials)
    return profile


def _write_bundle(tmp_path, profile_type, credentials=None, **payload_changes):
    payload = {
        "payload_version": 1, "kind": transfer.ACCOUNT_FORMAT, "profile_type": profile_type,
        "account_name": "portable-account", "credentials": credentials or _credentials(profile_type),
    }
    payload.update(payload_changes)
    bundle = portable_migration._encrypt_payload(payload, PASSWORD)
    bundle["format"] = transfer.ACCOUNT_FORMAT
    target = tmp_path / "input.asxaccount"
    target.write_text(json.dumps(bundle), encoding="utf-8")
    return target


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_current_round_trip_only_one_account_and_never_activates(account_env, kind):
    tmp_path, secrets = account_env
    original = _credentials(kind)
    original["OPENAI_API_KEY"] = "unrelated-api-secret"
    original["mcpOAuth"] = {"accessToken": "unrelated-mcp-secret"}
    _write_current(kind, original)
    target = tmp_path / "login.asxaccount"

    exported = transfer.export_account_login(target, PASSWORD, kind)

    assert exported.used_current_login
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()
    encrypted = target.read_text(encoding="utf-8")
    assert "synthetic-access" not in encrypted
    assert "account@example.test" not in encrypted
    assert "unrelated" not in encrypted
    payload = transfer._read_payload(target, PASSWORD)
    assert set(payload) == {"payload_version", "kind", "profile_type", "account_name", "credentials"}
    assert "OPENAI_API_KEY" not in payload["credentials"]
    assert "mcpOAuth" not in payload["credentials"]

    result = transfer.import_account_login(target, PASSWORD, expected_type=kind)

    assert result.created_new
    assert result.profile_type == kind
    assert len(transfer._profiles(kind)) == 1
    assert len(secrets) == 1
    assert transfer._saved_credentials(kind, transfer._profiles(kind)[0]) == transfer._clean_credentials(kind, original)
    assert profile_manager.get_active_codex_account_name() is None
    assert profile_manager.get_active_claude_account_name() is None
    current = auth_parser.read_codex_auth() if kind == "codex" else parser.read_claude_credentials()
    assert current == original


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_named_export_prefers_verified_current_but_does_not_update_saved(account_env, kind):
    tmp_path, secrets = account_env
    stale, fresh = _credentials(kind), _credentials(kind, token="rotated-token")
    _save(kind, "chosen", stale)
    before = dict(secrets)
    _write_current(kind, fresh)

    result = transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, kind, "chosen")

    assert result.used_current_login
    assert transfer._read_payload(result.path, PASSWORD)["credentials"] == fresh
    assert secrets == before


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_named_export_never_uses_different_live_identity(account_env, kind):
    tmp_path, _ = account_env
    saved = _credentials(kind)
    _save(kind, "chosen", saved)
    _write_current(kind, _credentials(kind, email="other@example.test", token="other-token"))
    result = transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, kind, "chosen")
    assert not result.used_current_login
    assert transfer._read_payload(result.path, PASSWORD)["credentials"] == saved


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_named_export_does_not_match_display_name_alone(account_env, kind):
    tmp_path, _ = account_env
    saved = _credentials(kind)
    saved.pop("email")
    if kind == "codex":
        saved["tokens"].pop("account_id")
    saved["name"] = "same-display-name"
    fresh = json.loads(json.dumps(saved))
    if kind == "codex":
        fresh["tokens"]["access_token"] = "other-token"
    else:
        fresh["claudeAiOauth"]["accessToken"] = "other-token"
    _save(kind, "chosen", saved)
    _write_current(kind, fresh)
    result = transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, kind, "chosen")
    assert not result.used_current_login
    assert transfer._read_payload(result.path, PASSWORD)["credentials"] == saved


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_current_api_mode_rejected_but_saved_snapshot_exportable(account_env, kind):
    tmp_path, _ = account_env
    credentials = _credentials(kind)
    _write_current(kind, credentials)
    _save(kind, "chosen", credentials)
    if kind == "codex":
        toml_parser.write_codex_config({"cli_auth_credentials_store": "file", "model_provider": "custom"})
    else:
        parser.write_claude_settings({"env": {"ANTHROPIC_API_KEY": "unrelated-key"}})
    with pytest.raises(ValueError, match="API"):
        transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, kind)
    result = transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, kind, "chosen")
    assert not result.used_current_login


@pytest.mark.parametrize("store", [None, "auto", "keyring"])
def test_current_codex_non_file_rejected_saved_allowed(account_env, store):
    tmp_path, _ = account_env
    _save("codex", "chosen", _credentials("codex"))
    auth_parser.write_codex_auth(_credentials("codex", token="stale-on-disk"))
    toml_parser.write_codex_config({"cli_auth_credentials_store": store} if store else {})
    with pytest.raises(ValueError, match="auto/keyring"):
        transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, "codex")
    result = transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, "codex", "chosen")
    assert not result.used_current_login


def test_home_level_claude_config_does_not_block_desktop_export(account_env):
    tmp_path, _ = account_env
    _write_current("claude", _credentials("claude"))
    output = tmp_path / "user" / "Desktop" / "login.asxaccount"
    assert transfer.export_account_login(output, PASSWORD, "claude").path == output


@pytest.mark.parametrize("location", ["claude", "codex", "secrets", "profile"])
def test_live_and_secret_output_paths_rejected(account_env, monkeypatch, location):
    tmp_path, _ = account_env
    _write_current("claude", _credentials("claude"))
    candidates = {
        "claude": parser.CLAUDE_CREDENTIALS.parent / "login.asxaccount",
        "codex": auth_parser.CODEX_AUTH.parent / "login.asxaccount",
        "secrets": paths.SECRETS_DIR / "login.asxaccount",
        "profile": tmp_path / "protected.asxaccount",
    }
    if location == "profile":
        monkeypatch.setattr(profile_manager, "PROFILES_FILE", candidates[location])
    with pytest.raises(ValueError, match="不能覆盖"):
        transfer.export_account_login(candidates[location], PASSWORD, "claude")
    assert not candidates[location].exists()


@pytest.mark.parametrize("password", ["1", "1234567", "       "])
def test_export_password_strength_enforced_before_reads(account_env, password):
    tmp_path, _ = account_env
    with pytest.raises(ValueError, match="8"):
        transfer.export_account_login(tmp_path / "out.asxaccount", password, "codex")


@pytest.mark.parametrize("password", [None, 0, False, b"", []])
def test_non_string_password_rejected_for_import_and_export(account_env, password):
    tmp_path, secrets = account_env
    with pytest.raises(ValueError, match="文本"):
        transfer.export_account_login(tmp_path / "out.asxaccount", password, "codex")
    with pytest.raises(ValueError, match="文本"):
        transfer.import_account_login(tmp_path / "input.asxaccount", password)
    assert secrets == {}


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_empty_password_export_is_explicitly_unencrypted_and_imports(account_env, kind):
    tmp_path, secrets = account_env
    credentials = _credentials(kind)
    _write_current(kind, credentials)
    target = tmp_path / "unencrypted.asxaccount"

    result = transfer.export_account_login(target, "", kind)

    assert result.path == target
    bundle = json.loads(target.read_text(encoding="utf-8"))
    assert bundle["version"] == 2
    assert bundle["cipher"] == {"name": "none"}
    assert "kdf" not in bundle
    assert bundle["compression"] == "zlib"
    # Base64 plus compression is readily reversible, not password protection.
    raw = zlib.decompress(base64.b64decode(bundle["payload"]))
    assert b"synthetic-access" in raw
    payload = json.loads(raw)
    assert payload["payload_version"] == 1
    assert payload["kind"] == transfer.ACCOUNT_FORMAT
    assert payload["credentials"] == credentials
    assert secrets == {}

    imported = transfer.import_account_login(target, "", expected_type=kind)
    assert imported.created_new
    assert transfer._saved_credentials(kind, transfer._profiles(kind)[0]) == credentials


@pytest.mark.parametrize("password", ["", "wrong-password"])
def test_encrypted_account_never_falls_back_to_plaintext(account_env, password):
    tmp_path, secrets = account_env
    target = _write_bundle(tmp_path, "codex")
    with pytest.raises(ValueError):
        transfer.import_account_login(target, password)
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


@pytest.mark.parametrize("changes", [
    {"kdf": None}, {"kdf": {"name": "PBKDF2HMAC-SHA256"}},
    {"nonce": "unexpected"}, {"salt": "unexpected"},
    {"cipher": {"name": "none", "nonce": "unexpected"}},
    {"cipher": {"name": "AES-256-GCM"}}, {"cipher": {}},
    {"compression": "none"}, {"version": True}, {"version": 3},
])
def test_plain_account_mixed_or_unknown_markers_rejected(account_env, changes):
    tmp_path, secrets = account_env
    _write_current("claude", _credentials("claude"))
    target = tmp_path / "unencrypted.asxaccount"
    transfer.export_account_login(target, "", "claude")
    bundle = json.loads(target.read_text(encoding="utf-8"))
    bundle.update(changes)
    target.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError):
        transfer.import_account_login(target, "")
    assert secrets == {}


def test_plain_account_keeps_inner_kind_and_decompression_guards(account_env, monkeypatch):
    tmp_path, secrets = account_env
    target = tmp_path / "input.asxaccount"
    payload = {"payload_version": 1, "kind": "not-account", "profile_type": "codex", "credentials": _credentials("codex")}
    bundle = portable_migration._encode_bundle_payload(payload, "")
    bundle["format"] = transfer.ACCOUNT_FORMAT
    target.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="内容类型"):
        transfer.import_account_login(target, "")
    payload["kind"] = transfer.ACCOUNT_FORMAT
    payload["extra"] = "z" * 10000
    bundle = portable_migration._encode_bundle_payload(payload, "")
    bundle["format"] = transfer.ACCOUNT_FORMAT
    target.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(transfer, "MAX_ACCOUNT_PAYLOAD_BYTES", 5000)
    with pytest.raises(ValueError, match="安全限制"):
        transfer.import_account_login(target, "")
    assert secrets == {}


def test_account_password_whitespace_is_not_trimmed(account_env):
    tmp_path, _ = account_env
    _write_current("codex", _credentials("codex"))
    target = tmp_path / "spaces.asxaccount"
    password = "  surrounded-by-spaces  "
    transfer.export_account_login(target, password, "codex")
    assert transfer._read_payload(target, password)["profile_type"] == "codex"
    with pytest.raises(ValueError):
        transfer._read_payload(target, password.strip())


def test_encrypted_account_blank_password_skips_kdf_and_gives_clear_message(account_env, monkeypatch):
    tmp_path, secrets = account_env
    target = _write_bundle(tmp_path, "codex")
    monkeypatch.setattr(portable_migration, "PBKDF2HMAC", lambda **kwargs: pytest.fail("empty password must not derive"))
    with pytest.raises(ValueError, match="已加密.*输入"):
        transfer.import_account_login(target, "")
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_wrong_password_wrong_type_tampering_make_no_changes(account_env, kind):
    tmp_path, secrets = account_env
    target = _write_bundle(tmp_path, kind)
    for password, expected in [("wrong-password", kind), (PASSWORD, "claude" if kind == "codex" else "codex")]:
        with pytest.raises(ValueError):
            transfer.import_account_login(target, password, expected)
    bundle = json.loads(target.read_text())
    bundle["payload"] = ("B" if bundle["payload"][0] == "A" else "A") + bundle["payload"][1:]
    target.write_text(json.dumps(bundle))
    with pytest.raises(ValueError):
        transfer.import_account_login(target, PASSWORD)
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


@pytest.mark.parametrize("kind,bad_credentials", [
    ("claude", {"env": {"ANTHROPIC_AUTH_TOKEN": "api-only"}}),
    ("claude", {"claudeAiOauth": {"accessToken": {"secret": "nested"}}}),
    ("claude", {"claudeAiOauth": {"accessToken": "  "}}),
    ("codex", {"OPENAI_API_KEY": "api-only"}),
    ("codex", {"tokens": {"access_token": ["token"]}}),
    ("codex", {"tokens": {"id_token": "id-only"}}),
    ("codex", {"tokens": {"access_token": " "}}),
])
def test_invalid_official_credentials_rejected(account_env, kind, bad_credentials):
    tmp_path, secrets = account_env
    target = _write_bundle(tmp_path, kind, bad_credentials)
    with pytest.raises(ValueError):
        transfer.import_account_login(target, PASSWORD)
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_collision_and_repeat_import_preserve_existing(account_env, kind):
    tmp_path, secrets = account_env
    existing = _save(kind, "portable-account", _credentials(kind, email="existing@example.test"))
    original = dict(secrets)
    target = _write_bundle(tmp_path, kind)
    result = transfer.import_account_login(target, PASSWORD)
    assert result.created_new and result.account_name == "portable-account-2"
    assert all(secrets[key] == value for key, value in original.items())
    assert existing in transfer._profiles(kind)
    repeated = transfer.import_account_login(target, PASSWORD)
    assert not repeated.created_new
    assert repeated.account_name == result.account_name
    assert len(transfer._profiles(kind)) == 2


@pytest.mark.parametrize("kind,suffix", [("claude", "credentials"), ("codex", "auth_json")])
def test_orphan_secret_ref_is_not_overwritten(account_env, kind, suffix):
    tmp_path, secrets = account_env
    ref = f"{kind}-account:portable-account:{suffix}"
    secrets[ref] = "orphan-secret-preserve"
    result = transfer.import_account_login(_write_bundle(tmp_path, kind), PASSWORD)
    assert result.account_name == "portable-account-2"
    assert secrets[ref] == "orphan-secret-preserve"


def test_import_rebuilds_local_identity_and_ignores_injected_secret_refs(account_env):
    tmp_path, secrets = account_env
    path = _write_bundle(tmp_path, "codex", identity="attacker", auth_json_ref="claude:existing:token")
    result = transfer.import_account_login(path, PASSWORD)
    profile = transfer._profiles("codex")[0]
    assert profile.name == result.account_name
    assert profile.identity == "account@example.test"
    assert profile.auth_json_ref == "codex-account:portable-account:auth_json"
    assert "claude:existing:token" not in secrets


def test_corrupt_inner_marker_and_payload_version_rejected(account_env):
    tmp_path, secrets = account_env
    for changes in [{"kind": "other-format"}, {"payload_version": True}, {"payload_version": 2}]:
        path = _write_bundle(tmp_path, "claude", **changes)
        with pytest.raises(ValueError):
            transfer.import_account_login(path, PASSWORD)
    assert secrets == {}


def test_input_size_and_decompression_limit_before_save(account_env, monkeypatch):
    tmp_path, secrets = account_env
    path = _write_bundle(tmp_path, "claude", extra="A" * 20_000)
    monkeypatch.setattr(transfer, "MAX_ACCOUNT_PAYLOAD_BYTES", 10_000)
    with pytest.raises(ValueError, match="安全限制"):
        transfer.import_account_login(path, PASSWORD)
    monkeypatch.setattr(transfer, "MAX_ACCOUNT_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="过大"):
        transfer.import_account_login(path, PASSWORD)
    assert secrets == {}


def test_kdf_work_limit_rejected_before_derivation(account_env, monkeypatch):
    tmp_path, secrets = account_env
    path = _write_bundle(tmp_path, "codex")
    bundle = json.loads(path.read_text())
    bundle["kdf"]["iterations"] = portable_migration.MAX_KDF_ITERATIONS + 1
    path.write_text(json.dumps(bundle))
    monkeypatch.setattr(portable_migration, "PBKDF2HMAC", lambda **kwargs: pytest.fail("must not derive"))
    with pytest.raises(ValueError, match="安全限制"):
        transfer.import_account_login(path, PASSWORD)
    assert secrets == {}


def test_untrusted_format_fields_never_reflected_in_error(account_env):
    tmp_path, _ = account_env
    path = _write_bundle(tmp_path, "codex")
    bundle = json.loads(path.read_text())
    bundle["compression"] = "untrusted-secret-value"
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError) as error:
        transfer.import_account_login(path, PASSWORD)
    assert "untrusted-secret-value" not in str(error.value)


def test_export_write_failure_does_not_modify_live_or_saved(account_env, monkeypatch):
    tmp_path, secrets = account_env
    saved, current = _credentials("claude"), _credentials("claude", token="fresh")
    _save("claude", "chosen", saved)
    _write_current("claude", current)
    original_store = profile_manager.PROFILES_FILE.read_bytes()
    original_secrets = dict(secrets)
    monkeypatch.setattr(transfer, "atomic_write_text", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        transfer.export_account_login(tmp_path / "out.asxaccount", PASSWORD, "claude", "chosen")
    assert profile_manager.PROFILES_FILE.read_bytes() == original_store
    assert secrets == original_secrets
    assert parser.read_claude_credentials() == current


def test_portable_decrypt_optional_limit_keeps_default_behavior():
    payload = {"payload_version": 1, "large": "a" * 2000}
    bundle = portable_migration._encrypt_payload(payload, PASSWORD)
    assert portable_migration._decrypt_bundle(bundle, PASSWORD) == payload
    with pytest.raises(ValueError, match="过大"):
        portable_migration._decrypt_bundle(bundle, PASSWORD, max_payload_bytes=100)
    with pytest.raises(ValueError, match="限制无效"):
        portable_migration._decrypt_bundle(bundle, PASSWORD, max_payload_bytes=-1)
