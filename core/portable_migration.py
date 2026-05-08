"""Password-protected portable profile migration.

This module exports profile metadata plus app-managed secrets into a portable
file. Secrets are decrypted from the current machine and re-encrypted with a
user-provided migration password so they can be restored on another computer.
"""
from __future__ import annotations

import base64
import json
import os
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core import profile_manager, security


BUNDLE_FORMAT = "api-switcher-portable-profiles"
BUNDLE_VERSION = 1
KDF_ITERATIONS = 390_000
MAX_BUNDLE_FILE_BYTES = 3 * 1024 * 1024 * 1024


@dataclass
class PortableExportResult:
    path: Path
    profile_count: int
    secret_count: int
    missing_secret_refs: list[str]


@dataclass
class PortableImportResult:
    profile_count: int
    secret_count: int
    skipped_secret_refs: list[str]


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _derive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("迁移密码不能为空")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _encrypt_payload(payload: dict[str, Any], password: str) -> dict[str, Any]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(plaintext, level=6)
    ciphertext = AESGCM(key).encrypt(nonce, compressed, None)

    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kdf": {
            "name": "PBKDF2HMAC-SHA256",
            "iterations": KDF_ITERATIONS,
            "salt": _b64encode(salt),
        },
        "cipher": {
            "name": "AES-256-GCM",
            "nonce": _b64encode(nonce),
        },
        "compression": "zlib",
        "payload": _b64encode(ciphertext),
    }


def _decrypt_bundle(bundle: dict[str, Any], password: str) -> dict[str, Any]:
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError("不是 API切换器 Profile 迁移包")
    if bundle.get("version") != BUNDLE_VERSION:
        raise ValueError(f"不支持的迁移包版本: {bundle.get('version')}")

    kdf = bundle.get("kdf")
    cipher = bundle.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise ValueError("迁移包格式不完整")
    if kdf.get("name") != "PBKDF2HMAC-SHA256" or cipher.get("name") != "AES-256-GCM":
        raise ValueError("迁移包加密算法不受支持")

    try:
        iterations = int(kdf.get("iterations") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("迁移包 KDF 参数异常") from e
    if iterations < 100_000:
        raise ValueError("迁移包 KDF 参数异常")

    try:
        salt = _b64decode(str(kdf["salt"]))
        nonce = _b64decode(str(cipher["nonce"]))
        ciphertext = _b64decode(str(bundle["payload"]))
    except Exception as e:
        raise ValueError("迁移包编码损坏") from e

    try:
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        ).derive(password.encode("utf-8"))
        decrypted = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        raise ValueError("迁移密码错误，或迁移包已损坏") from e

    compression = bundle.get("compression")
    try:
        if compression == "zlib":
            plaintext = zlib.decompress(decrypted)
        elif compression in {None, "none"}:
            plaintext = decrypted
        else:
            raise ValueError(f"不支持的迁移包压缩方式: {compression}")
    except zlib.error as e:
        raise ValueError("迁移包压缩数据损坏") from e

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("迁移包内容损坏") from e
    if not isinstance(payload, dict) or payload.get("payload_version") != 1:
        raise ValueError("迁移包内容版本不受支持")
    return payload


def _collect_secret_refs_from_store(store: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in profile_manager.PROFILE_LIST_KEYS:
        profiles = store.get(key, [])
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            for field_name, value in profile.items():
                if field_name.endswith("_ref") and isinstance(value, str) and value:
                    refs.add(value)
    return refs


def _count_profiles(store: dict[str, Any]) -> int:
    total = 0
    for key in profile_manager.PROFILE_LIST_KEYS:
        items = store.get(key, [])
        if isinstance(items, list):
            total += sum(1 for item in items if isinstance(item, dict) and item.get("name"))
    return total


def _sanitize_portable_store(store: dict[str, Any]) -> dict[str, Any]:
    stripped = dict(store)

    claude_profiles = []
    for profile in store.get("claude_profiles", []):
        if not isinstance(profile, dict):
            continue
        if profile.get("provider", "anthropic") == "anthropic":
            continue
        claude_profiles.append(dict(profile))

    codex_profiles = []
    for profile in store.get("codex_profiles", []):
        if not isinstance(profile, dict):
            continue
        if profile.get("model_provider", "openai") == "openai":
            continue
        cleaned = dict(profile)
        for legacy_key in ("auth_mode", "openai_auth_key_ref", "oauth_tokens_ref", "auth_data_ref", "last_refresh"):
            cleaned.pop(legacy_key, None)
        codex_profiles.append(cleaned)

    stripped["claude_profiles"] = claude_profiles
    stripped["codex_profiles"] = codex_profiles
    stripped["browser_profiles"] = []

    active_claude = stripped.get("active_claude_profile")
    if active_claude not in {profile.get("name") for profile in claude_profiles}:
        stripped["active_claude_profile"] = None

    active_codex = stripped.get("active_codex_profile")
    if active_codex not in {profile.get("name") for profile in codex_profiles}:
        stripped["active_codex_profile"] = None

    stripped["active_browser_profile"] = None
    return stripped


def _store_version(store: dict[str, Any]) -> int:
    try:
        return int(store.get("version", 1))
    except (TypeError, ValueError):
        return 1


def _merge_profile_lists(existing: list[Any], imported: list[Any]) -> list[dict[str, Any]]:
    existing_items = [
        dict(item) for item in existing
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    imported_items = [
        dict(item) for item in imported
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]

    imported_names = {item["name"] for item in imported_items}
    merged = [item for item in existing_items if item["name"] not in imported_names]
    merged.extend(imported_items)
    return merged


def export_portable_profiles(output_path: str | Path, password: str) -> PortableExportResult:
    """Export all profiles and available app-managed secrets."""
    if len(password) < 8:
        raise ValueError("迁移密码至少需要 8 个字符")

    store = _sanitize_portable_store(profile_manager._load_store())
    secret_refs = sorted(_collect_secret_refs_from_store(store))
    secrets: dict[str, str] = {}
    missing: list[str] = []

    for ref in secret_refs:
        value = security.get_secret(ref)
        if value is None:
            missing.append(ref)
        else:
            secrets[ref] = value

    payload = {
        "payload_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "store": store,
        "secrets": secrets,
        "missing_secret_refs": missing,
        "notes": [
            "Includes app-managed Claude/Codex/SSH profile metadata and secrets.",
            "Browser profiles and browser login/session data are intentionally local-only and are not exported.",
        ],
    }
    bundle = _encrypt_payload(payload, password)

    path = Path(output_path).expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError("导出路径不能是目录")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return PortableExportResult(
        path=path,
        profile_count=_count_profiles(store),
        secret_count=len(secrets),
        missing_secret_refs=missing,
    )


def import_portable_profiles(input_path: str | Path, password: str) -> PortableImportResult:
    """Import profiles and secrets from a portable migration file.

    Same-name profiles are replaced. Profiles with different names are kept.
    """
    if not password:
        raise ValueError("请输入迁移密码")

    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise ValueError("迁移包不存在")
    if not path.is_file():
        raise ValueError("请选择迁移包文件")
    if path.stat().st_size > MAX_BUNDLE_FILE_BYTES:
        raise ValueError("迁移包过大，请确认是否选择了正确文件")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError("迁移包 JSON 格式损坏") from e
    if not isinstance(bundle, dict):
        raise ValueError("迁移包 JSON 格式损坏")
    payload = _decrypt_bundle(bundle, password)

    imported_store = payload.get("store")
    if not isinstance(imported_store, dict):
        raise ValueError("迁移包中没有有效 Profile 数据")

    imported_store = _sanitize_portable_store(imported_store)

    existing_store = profile_manager._load_store()
    new_store = dict(existing_store)
    for key in profile_manager.PROFILE_LIST_KEYS:
        new_store[key] = _merge_profile_lists(
            existing_store.get(key, []),
            imported_store.get(key, []),
        )

    for active_key in profile_manager.ACTIVE_PROFILE_KEYS:
        imported_active = imported_store.get(active_key)
        if isinstance(imported_active, str):
            list_key = active_key.replace("active_", "") + "s"
            if list_key == "claude_profiles":
                list_key = "claude_profiles"
            names = {
                item.get("name")
                for item in new_store.get(list_key, [])
                if isinstance(item, dict)
            }
            if imported_active in names:
                new_store[active_key] = imported_active

    new_store["version"] = max(_store_version(existing_store), _store_version(imported_store))
    secrets = payload.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("迁移包中的密钥数据无效")

    skipped: list[str] = []
    valid_secrets: list[tuple[str, str]] = []
    for ref, value in secrets.items():
        if not isinstance(ref, str) or not isinstance(value, str):
            skipped.append(str(ref))
            continue
        valid_secrets.append((ref, value))

    profile_manager._normalize_store(new_store)
    profile_manager._save_store(new_store)

    restored = 0
    for ref, value in valid_secrets:
        try:
            security.set_secret(ref, value)
        except Exception as e:
            skipped.append(f"{ref} ({e})")
            continue
        restored += 1

    return PortableImportResult(
        profile_count=_count_profiles(imported_store),
        secret_count=restored,
        skipped_secret_refs=skipped,
    )
