import os
import base64
import json
import zlib
from pathlib import Path

import pytest

from core import security


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_dpapi_fallback_round_trip_uses_active_atomic_file(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "SECRETS_DIR", tmp_path / "secrets")

    security._dpapi_set("roundtrip:ref", "roundtrip-secret")

    active_path = security._active_dpapi_key_path("roundtrip:ref")
    assert active_path.is_file()
    assert not security._dpapi_key_path("roundtrip:ref").exists()
    assert security._dpapi_get_active("roundtrip:ref") == "roundtrip-secret"

    security._dpapi_delete("roundtrip:ref")
    assert not active_path.exists()


def test_legacy_dpapi_path_cannot_escape_secrets_directory(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(security, "SECRETS_DIR", secrets_dir)

    legacy_path = security._legacy_dpapi_key_path(r"..\victim")

    assert legacy_path.parent == secrets_dir.resolve()
    assert legacy_path.name == ".._victim.bin"


def test_dpapi_get_does_not_read_legacy_path_outside_secrets_directory(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    outside = tmp_path / "victim.bin"
    outside.write_bytes(b"must not be read")
    monkeypatch.setattr(security, "SECRETS_DIR", secrets_dir)
    real_read_bytes = Path.read_bytes
    read_paths = []

    def track_read(path):
        read_paths.append(path.resolve())
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read)

    assert security._dpapi_get(r"..\victim") is None
    assert outside.resolve() not in read_paths
    assert outside.read_bytes() == b"must not be read"


def test_dpapi_delete_does_not_remove_legacy_path_outside_secrets_directory(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "secrets"
    outside = tmp_path / "victim.bin"
    outside.write_bytes(b"must not be deleted")
    monkeypatch.setattr(security, "SECRETS_DIR", secrets_dir)

    security._dpapi_delete(r"..\victim")

    assert outside.read_bytes() == b"must not be deleted"


def test_set_secret_reports_failure_when_both_backends_fail(monkeypatch):
    class BrokenKeyring:
        @staticmethod
        def set_password(*_args):
            raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(security, "_keyring", lambda: BrokenKeyring())
    monkeypatch.setattr(
        security,
        "_dpapi_set",
        lambda *_args: (_ for _ in ()).throw(OSError("dpapi unavailable")),
    )

    with pytest.raises(RuntimeError, match="无法保存密钥"):
        security.set_secret("test:ref", "secret")


def test_delete_secret_reports_partial_backend_failure(monkeypatch):
    class BrokenKeyring:
        @staticmethod
        def delete_password(*_args):
            raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(security, "_keyring", lambda: BrokenKeyring())
    monkeypatch.setattr(security, "_dpapi_delete", lambda *_args: None)

    with pytest.raises(RuntimeError, match="密钥删除不完整"):
        security.delete_secret("test:ref")


def test_delete_secret_reports_dpapi_cleanup_failure(monkeypatch):
    class WorkingKeyring:
        @staticmethod
        def delete_password(*_args):
            return None

    monkeypatch.setattr(security, "_keyring", lambda: WorkingKeyring())
    monkeypatch.setattr(
        security,
        "_dpapi_delete",
        lambda *_args: (_ for _ in ()).throw(PermissionError("file locked")),
    )

    with pytest.raises(RuntimeError, match="密钥删除不完整"):
        security.delete_secret("test:ref")


def test_active_dpapi_fallback_wins_over_stale_keyring_value(monkeypatch):
    class StaleKeyring:
        get_calls = 0

        @classmethod
        def get_password(cls, *_args):
            cls.get_calls += 1
            return "stale-keyring-value"

    monkeypatch.setattr(security, "_keyring", lambda: StaleKeyring())
    monkeypatch.setattr(security, "_dpapi_get_active", lambda _key: "fresh-fallback-value")

    assert security.get_secret("test:ref") == "fresh-fallback-value"
    assert StaleKeyring.get_calls == 0


def test_corrupt_active_fallback_never_returns_stale_keyring_value(tmp_path, monkeypatch):
    class StaleKeyring:
        get_calls = 0

        @classmethod
        def get_password(cls, *_args):
            cls.get_calls += 1
            return "stale-keyring-value"

    monkeypatch.setattr(security, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(security, "_keyring", lambda: StaleKeyring())
    security._active_dpapi_key_path("test:ref").write_bytes(b"not-dpapi")

    assert security.get_secret("test:ref") is None
    assert StaleKeyring.get_calls == 0
    with pytest.raises(security.SecretReadError, match="DPAPI 活动密钥无法读取"):
        security.get_secret_strict("test:ref")
    assert StaleKeyring.get_calls == 0


def test_strict_secret_read_reports_keyring_failure_when_no_fallback(tmp_path, monkeypatch):
    class BrokenKeyring:
        @staticmethod
        def get_password(*_args):
            raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(security, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(security, "_keyring", lambda: BrokenKeyring())

    with pytest.raises(security.SecretReadError, match="系统密钥环无法读取"):
        security.get_secret_strict("test:ref")


def test_successful_keyring_write_cleans_stale_dpapi_fallback(monkeypatch):
    class WorkingKeyring:
        @staticmethod
        def set_password(*_args):
            return None

    cleaned = []
    monkeypatch.setattr(security, "_keyring", lambda: WorkingKeyring())
    monkeypatch.setattr(security, "_dpapi_delete", lambda key: cleaned.append(key))

    security.set_secret("test:ref", "new-value")

    assert cleaned == ["test:ref"]


def test_successful_keyring_write_reports_stale_fallback_cleanup_failure(monkeypatch):
    class WorkingKeyring:
        @staticmethod
        def set_password(*_args):
            return None

    monkeypatch.setattr(security, "_keyring", lambda: WorkingKeyring())
    monkeypatch.setattr(
        security,
        "_dpapi_delete",
        lambda _key: (_ for _ in ()).throw(PermissionError("fallback locked")),
    )

    with pytest.raises(RuntimeError, match="旧 DPAPI 密钥清理失败"):
        security.set_secret("test:ref", "new-value")


def test_secret_json_rejects_oversized_decompressed_payload(monkeypatch):
    monkeypatch.setattr(security, "MAX_SECRET_JSON_BYTES", 64)
    encoded = base64.b64encode(zlib.compress(json.dumps({"value": "x" * 1000}).encode())).decode()
    monkeypatch.setattr(security, "get_secret", lambda _key: encoded)

    assert security.get_secret_json("test:json") is None


def test_secret_json_rejects_trailing_compressed_data_and_non_dict(monkeypatch):
    trailing = base64.b64encode(zlib.compress(b'{}') + b"trailing").decode()
    monkeypatch.setattr(security, "get_secret", lambda _key: trailing)
    assert security.get_secret_json("test:json") is None

    encoded_list = base64.b64encode(zlib.compress(b"[]")).decode()
    monkeypatch.setattr(security, "get_secret", lambda _key: encoded_list)
    assert security.get_secret_json("test:json") is None
