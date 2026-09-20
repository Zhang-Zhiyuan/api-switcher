"""Strict optional-password envelope tests; all stores and credentials are fake."""
import base64
import json
import zlib

import pytest

from config import paths
from core import portable_migration as migration
from core import profile_manager, security


PASSWORD = "synthetic-bundle-password"


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    secrets = {}
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "source" / "profiles.json")
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "SECRETS_DIR", tmp_path / "secret-files")
    monkeypatch.setattr(security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secrets.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secrets.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secrets.pop(ref, None))
    profile_manager.clear_profile_store_cache()
    yield tmp_path, secrets
    profile_manager.clear_profile_store_cache()


def _source_store(secrets):
    store = profile_manager._get_default_store()
    store["claude_profiles"] = [{"name": "portable-test", "provider": "custom", "auth_token_ref": "claude:portable-test:auth_token"}]
    profile_manager._save_store(store)
    secrets["claude:portable-test:auth_token"] = "synthetic-only-secret"


@pytest.mark.parametrize("password", ["", PASSWORD])
def test_optional_password_profile_roundtrip_to_other_store(isolated_store, monkeypatch, password):
    tmp_path, secrets = isolated_store
    _source_store(secrets)
    source_bytes = profile_manager.PROFILES_FILE.read_bytes()
    output = tmp_path / "profiles.asxprofile"

    exported = migration.export_portable_profiles(output, password, {"claude_profiles": {"portable-test"}})

    assert exported.profile_count == 1
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["version"] == (2 if password == "" else 1)
    assert bundle["cipher"]["name"] == ("none" if password == "" else "AES-256-GCM")
    assert ("kdf" in bundle) == bool(password)
    if password == "":
        decoded = json.loads(zlib.decompress(base64.b64decode(bundle["payload"])))
        assert decoded["secrets"] == secrets
    assert profile_manager.PROFILES_FILE.read_bytes() == source_bytes

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "destination" / "profiles.json")
    profile_manager.clear_profile_store_cache()
    secrets.clear()
    imported = migration.import_portable_profiles(output, password)

    assert imported.profile_count == 1
    assert imported.secret_count == 1
    assert secrets == {"claude:portable-test:auth_token": "synthetic-only-secret"}
    assert [item.name for item in profile_manager.list_claude_profiles()] == ["portable-test"]


@pytest.mark.parametrize("password", [None, False, 0, b"", []])
def test_invalid_password_types_are_not_no_password(isolated_store, password):
    tmp_path, secrets = isolated_store
    for operation in (migration.export_portable_profiles, migration.import_portable_profiles):
        with pytest.raises(ValueError, match="文本"):
            operation(tmp_path / "profiles.asxprofile", password)
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


@pytest.mark.parametrize("password", ["1", "1234567", "       "])
def test_nonempty_export_password_still_requires_eight_characters(isolated_store, password):
    tmp_path, _ = isolated_store
    with pytest.raises(ValueError, match="8"):
        migration.export_portable_profiles(tmp_path / "profiles.asxprofile", password)


@pytest.mark.parametrize("password", ["", "wrong-password"])
def test_aes_import_requires_correct_password_and_never_falls_back(isolated_store, password):
    tmp_path, secrets = isolated_store
    payload = {"payload_version": 1, "store": profile_manager._get_default_store()}
    bundle = migration._encrypt_payload(payload, PASSWORD)
    output = tmp_path / "encrypted.asxprofile"
    output.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError):
        migration.import_portable_profiles(output, password)
    assert secrets == {}
    assert not profile_manager.PROFILES_FILE.exists()


def test_plain_wrapper_is_explicit_and_never_calls_crypto(monkeypatch):
    monkeypatch.setattr(migration, "_encrypt_payload", lambda *args: pytest.fail("plaintext must not use empty-password encryption"))
    payload = {"payload_version": 1, "token": "synthetic-sensitive-login"}
    bundle = migration._encode_bundle_payload(payload, "")
    assert set(bundle) == {"format", "version", "created_at", "cipher", "compression", "payload"}
    assert bundle["cipher"] == {"name": "none"}
    assert migration._decode_bundle_payload(bundle, "") == payload
    # A supplied password is irrelevant to a clearly marked unencrypted file.
    assert migration._decode_bundle_payload(bundle, "unneeded-password") == payload


def test_legacy_encrypt_primitive_cannot_encrypt_empty_password():
    with pytest.raises(ValueError, match="不能为空"):
        migration._encrypt_payload({"payload_version": 1}, "")


def test_encrypted_blank_password_rejected_before_kdf(monkeypatch):
    bundle = migration._encrypt_payload({"payload_version": 1}, PASSWORD)
    monkeypatch.setattr(migration, "PBKDF2HMAC", lambda **kwargs: pytest.fail("empty password must not derive"))
    with pytest.raises(ValueError, match="加密.*密码"):
        migration._decode_bundle_payload(bundle, "")


@pytest.mark.parametrize("changes", [
    {"kdf": None}, {"kdf": {}}, {"kdf": {"name": "PBKDF2HMAC-SHA256"}},
    {"nonce": "unexpected"}, {"salt": "unexpected"}, {"encrypted": False},
    {"cipher": {"name": "none", "nonce": "unexpected"}}, {"cipher": {}},
    {"cipher": {"name": "AES-256-GCM"}}, {"compression": None},
    {"compression": "none"}, {"payload": {"token": "not-a-string"}},
    {"created_at": None}, {"version": True}, {"version": 1.0},
    {"version": "2"}, {"version": 0}, {"version": 3},
])
def test_mixed_or_unknown_plaintext_markers_never_accepted(changes):
    bundle = migration._encode_bundle_payload({"payload_version": 1}, "")
    bundle.update(changes)
    with pytest.raises(ValueError):
        migration._decode_bundle_payload(bundle, "")


@pytest.mark.parametrize("removed", ["format", "version", "cipher", "compression", "payload", "created_at"])
def test_missing_plaintext_markers_rejected(removed):
    bundle = migration._encode_bundle_payload({"payload_version": 1}, "")
    bundle.pop(removed)
    with pytest.raises(ValueError):
        migration._decode_bundle_payload(bundle, "")


@pytest.mark.parametrize("version,cipher", [(1, "none"), (2, "AES-256-GCM")])
def test_version_and_cipher_must_agree(version, cipher):
    bundle = migration._encrypt_payload({"payload_version": 1}, PASSWORD)
    bundle["version"] = version
    bundle["cipher"]["name"] = cipher
    with pytest.raises(ValueError):
        migration._decode_bundle_payload(bundle, PASSWORD)


def test_plaintext_decode_remains_bounded_and_requires_complete_zlib():
    bundle = migration._encode_bundle_payload({"payload_version": 1, "large": "x" * 20_000}, "")
    with pytest.raises(ValueError, match="过大"):
        migration._decode_bundle_payload(bundle, "", max_payload_bytes=10_000)
    original = base64.b64decode(bundle["payload"])
    for compressed in (original[:-2], original + b"trailing"):
        bundle["payload"] = base64.b64encode(compressed).decode()
        with pytest.raises(ValueError, match="损坏"):
            migration._decode_bundle_payload(bundle, "")


def test_public_plaintext_read_size_limit(isolated_store, monkeypatch):
    tmp_path, secrets = isolated_store
    output = tmp_path / "oversized.asxprofile"
    bundle = migration._encode_bundle_payload({"payload_version": 1, "store": {}}, "")
    output.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(migration, "MAX_BUNDLE_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="过大"):
        migration.import_portable_profiles(output, "")
    assert secrets == {}


def test_plaintext_decode_rejects_invalid_inner_payload():
    for payload in ({"payload_version": True}, {"payload_version": 2}, {"no_version": 1}):
        bundle = migration._encode_bundle_payload(payload, "")
        with pytest.raises(ValueError, match="版本"):
            migration._decode_bundle_payload(bundle, "")


@pytest.mark.parametrize("password", ["        ", "  surrounded-password  "])
def test_whitespace_password_is_encrypted_without_trimming(password):
    payload = {"payload_version": 1}
    bundle = migration._encode_bundle_payload(payload, password)
    assert bundle["version"] == 1
    assert migration._decode_bundle_payload(bundle, password) == payload
    with pytest.raises(ValueError):
        migration._decode_bundle_payload(bundle, password.strip())
