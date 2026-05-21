"""Password-protected ZIP export/import for local API Switcher configuration."""
from __future__ import annotations

import base64
import json
import os
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core import backup_manager, profile_manager, security
from core.atomic_io import atomic_write_text, replace_with_retry, temp_path_for


PACKAGE_FORMAT = "api-switcher-local-config-zip"
PACKAGE_VERSION = 1
PAYLOAD_FORMAT = "api-switcher-local-config-payload"
PAYLOAD_VERSION = 1
KDF_ITERATIONS = 390_000
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 64 * 1024 * 1024
MANIFEST_NAME = "manifest.json"
PAYLOAD_NAME = "payload.enc.json"

ACTIVE_TO_LIST_KEY = {
    "active_claude_profile": "claude_profiles",
    "active_codex_profile": "codex_profiles",
    "active_claude_account": "claude_account_profiles",
    "active_codex_account": "codex_account_profiles",
    "active_ssh_profile": "ssh_profiles",
    "active_browser_profile": "browser_profiles",
}


@dataclass(frozen=True)
class LocalConfigExportResult:
    path: Path
    profile_count: int
    secret_count: int
    missing_secret_refs: list[str]
    zip_bytes: int


@dataclass(frozen=True)
class LocalConfigImportResult:
    profile_count: int
    secret_count: int
    skipped_secret_refs: list[str]
    backup_description: str


@dataclass(frozen=True)
class LocalConfigPackageSummary:
    path: Path
    profile_count: int
    profile_counts: dict[str, int]
    secret_count: int
    missing_secret_count: int
    created_at: str


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
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(plaintext, level=6)
    ciphertext = AESGCM(_derive_key(password, salt)).encrypt(nonce, compressed, None)
    return {
        "format": PAYLOAD_FORMAT,
        "version": PAYLOAD_VERSION,
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


def _decrypt_payload(bundle: dict[str, Any], password: str) -> dict[str, Any]:
    if bundle.get("format") != PAYLOAD_FORMAT:
        raise ValueError("不是 API切换器完整配置 ZIP 的加密数据")
    if bundle.get("version") != PAYLOAD_VERSION:
        raise ValueError(f"不支持的完整配置 ZIP 数据版本: {bundle.get('version')}")

    kdf = bundle.get("kdf")
    cipher = bundle.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise ValueError("完整配置 ZIP 加密参数不完整")
    if kdf.get("name") != "PBKDF2HMAC-SHA256" or cipher.get("name") != "AES-256-GCM":
        raise ValueError("完整配置 ZIP 加密算法不受支持")

    try:
        iterations = int(kdf.get("iterations") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("完整配置 ZIP KDF 参数异常") from e
    if iterations < 100_000:
        raise ValueError("完整配置 ZIP KDF 参数异常")

    try:
        salt = _b64decode(str(kdf["salt"]))
        nonce = _b64decode(str(cipher["nonce"]))
        ciphertext = _b64decode(str(bundle["payload"]))
    except Exception as e:
        raise ValueError("完整配置 ZIP 编码损坏") from e

    try:
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        ).derive(password.encode("utf-8"))
        decrypted = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        raise ValueError("迁移密码错误，或完整配置 ZIP 已损坏") from e

    try:
        if bundle.get("compression") == "zlib":
            plaintext = zlib.decompress(decrypted)
        elif bundle.get("compression") in {None, "none"}:
            plaintext = decrypted
        else:
            raise ValueError(f"不支持的完整配置 ZIP 压缩方式: {bundle.get('compression')}")
    except zlib.error as e:
        raise ValueError("完整配置 ZIP 压缩数据损坏") from e

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("完整配置 ZIP 内容损坏") from e
    if not isinstance(payload, dict) or payload.get("payload_version") != PAYLOAD_VERSION:
        raise ValueError("完整配置 ZIP 内容版本不受支持")
    return payload


def _profile_count(store: dict[str, Any]) -> int:
    total = 0
    for key in profile_manager.PROFILE_LIST_KEYS:
        items = store.get(key, [])
        if isinstance(items, list):
            total += sum(1 for item in items if isinstance(item, dict) and isinstance(item.get("name"), str))
    return total


def _profile_counts_by_type(store: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in profile_manager.PROFILE_LIST_KEYS:
        items = store.get(key, [])
        counts[key] = sum(1 for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)) if isinstance(items, list) else 0
    return counts


def _collect_secret_refs(store: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in profile_manager.PROFILE_LIST_KEYS:
        items = store.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field_name, value in item.items():
                if field_name.endswith("_ref") and isinstance(value, str) and value:
                    refs.add(value)
    return refs


def _normalized_store_for_import(store: dict[str, Any]) -> dict[str, Any]:
    normalized = profile_manager._get_default_store()
    if isinstance(store, dict):
        for key, value in store.items():
            normalized[key] = value
    profile_manager._normalize_store(normalized)
    return normalized


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


def _apply_imported_active_profiles(target_store: dict[str, Any], imported_store: dict[str, Any]) -> None:
    for active_key, list_key in ACTIVE_TO_LIST_KEY.items():
        imported_active = imported_store.get(active_key)
        if not isinstance(imported_active, str) or not imported_active:
            continue
        names = {
            item.get("name")
            for item in target_store.get(list_key, [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if imported_active in names:
            target_store[active_key] = imported_active


def _delete_unreferenced_replaced_secrets(existing_store: dict[str, Any], new_store: dict[str, Any]) -> list[str]:
    skipped: list[str] = []
    for ref in sorted(_collect_secret_refs(existing_store) - _collect_secret_refs(new_store)):
        try:
            security.delete_secret(ref)
        except Exception as e:
            skipped.append(f"{ref} (旧密钥清理失败: {e})")
    return skipped


def _disconnect_imported_ssh_profiles(imported_store: dict[str, Any]) -> None:
    names = {
        item.get("name")
        for item in imported_store.get("ssh_profiles", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if not names:
        return
    try:
        from core.ssh_manager import ssh_manager

        for name in names:
            ssh_manager.disconnect(name)
    except Exception:
        return


def _payload_name_from_manifest(manifest: dict[str, Any]) -> str:
    payload_name = str(manifest.get("payload") or PAYLOAD_NAME)
    if payload_name != PAYLOAD_NAME:
        raise ValueError("完整配置 ZIP payload 路径异常")
    return payload_name


def _validate_zip_file_entry(bundle: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    try:
        info = bundle.getinfo(name)
    except KeyError as e:
        raise ValueError(f"完整配置 ZIP 缺少 {name}") from e
    if info.is_dir():
        raise ValueError(f"完整配置 ZIP 条目不是文件: {name}")
    if info.file_size > MAX_ZIP_ENTRY_BYTES:
        raise ValueError(f"完整配置 ZIP 条目过大: {name}")
    return info


def _read_json_zip_entry(bundle: zipfile.ZipFile, name: str) -> dict[str, Any]:
    info = _validate_zip_file_entry(bundle, name)
    try:
        data = json.loads(bundle.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"完整配置 ZIP 的 {name} 已损坏") from e
    if not isinstance(data, dict):
        raise ValueError(f"完整配置 ZIP 的 {name} 格式异常")
    return data


def _write_profiles_safety_backup(backup_entry: Any, store: dict[str, Any]) -> None:
    directory = getattr(backup_entry, "directory", None)
    if not directory:
        return
    try:
        target = Path(directory) / "profiles.json"
        atomic_write_text(target, json.dumps(store, ensure_ascii=False, indent=2))
    except Exception:
        # Import can still rely on profile_manager's own profiles.backup file.
        return


def inspect_local_config_zip(input_path: str | Path) -> LocalConfigPackageSummary:
    """Read the unencrypted manifest from a local config ZIP without importing secrets."""
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise ValueError("完整配置 ZIP 不存在")
    if not path.is_file():
        raise ValueError("请选择完整配置 ZIP 文件")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("完整配置 ZIP 过大，请确认是否选择了正确文件")
    try:
        with zipfile.ZipFile(path, "r") as bundle:
            manifest = _read_json_zip_entry(bundle, MANIFEST_NAME)
            _validate_zip_file_entry(bundle, _payload_name_from_manifest(manifest))
    except zipfile.BadZipFile as e:
        raise ValueError("完整配置 ZIP 文件损坏") from e

    if manifest.get("format") != PACKAGE_FORMAT:
        raise ValueError("不是 API切换器完整配置 ZIP")
    if manifest.get("version") != PACKAGE_VERSION:
        raise ValueError(f"不支持的完整配置 ZIP 版本: {manifest.get('version')}")

    profile_counts = manifest.get("profile_counts", {})
    if not isinstance(profile_counts, dict):
        profile_counts = {}
    try:
        profile_count = int(manifest.get("profile_count") or 0)
        secret_count = int(manifest.get("secret_count") or 0)
        missing_secret_count = int(manifest.get("missing_secret_count") or 0)
    except (TypeError, ValueError) as e:
        raise ValueError("完整配置 ZIP manifest 统计字段异常") from e

    return LocalConfigPackageSummary(
        path=path,
        profile_count=profile_count,
        profile_counts={
            str(key): int(value)
            for key, value in profile_counts.items()
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
        },
        secret_count=secret_count,
        missing_secret_count=missing_secret_count,
        created_at=str(manifest.get("created_at") or ""),
    )


def export_local_config_zip(output_path: str | Path, password: str) -> LocalConfigExportResult:
    """Export all local API/SSH/browser profile metadata plus referenced secrets to a ZIP."""
    if len(password) < 8:
        raise ValueError("迁移密码至少需要 8 个字符")

    store = _normalized_store_for_import(profile_manager._load_store())
    secret_refs = sorted(_collect_secret_refs(store))
    secrets: dict[str, str] = {}
    missing: list[str] = []
    for ref in secret_refs:
        value = security.get_secret(ref)
        if value is None:
            missing.append(ref)
        else:
            secrets[ref] = value

    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "payload_version": PAYLOAD_VERSION,
        "exported_at": created_at,
        "store": store,
        "secrets": secrets,
        "missing_secret_refs": missing,
        "notes": [
            "完整本地配置 ZIP：包含 Profile 元数据、活动选择和被 Profile 引用的密钥。",
            "密钥已用迁移密码加密；ZIP 本身仅用于打包，不依赖 ZipCrypto。",
            "私钥认证的 SSH Profile 会保存私钥文件路径和私钥口令；不会复制私钥文件本体。",
        ],
    }
    manifest = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "created_at": created_at,
        "profile_count": _profile_count(store),
        "profile_counts": _profile_counts_by_type(store),
        "secret_count": len(secrets),
        "missing_secret_count": len(missing),
        "payload": PAYLOAD_NAME,
    }

    path = Path(output_path).expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError("导出路径不能是目录")
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_output = temp_path_for(path)
    try:
        with zipfile.ZipFile(tmp_output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            bundle.writestr(PAYLOAD_NAME, json.dumps(_encrypt_payload(payload, password), ensure_ascii=False, indent=2))
        replace_with_retry(tmp_output, path)
    except Exception:
        tmp_output.unlink(missing_ok=True)
        raise

    return LocalConfigExportResult(
        path=path,
        profile_count=int(manifest["profile_count"]),
        secret_count=len(secrets),
        missing_secret_refs=missing,
        zip_bytes=path.stat().st_size,
    )


def import_local_config_zip(input_path: str | Path, password: str) -> LocalConfigImportResult:
    """Import API/SSH/browser profile metadata and secrets from a local config ZIP.

    Same-name profiles are replaced. Profiles with different names are preserved.
    """
    if not password:
        raise ValueError("请输入迁移密码")

    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        raise ValueError("完整配置 ZIP 不存在")
    if not path.is_file():
        raise ValueError("请选择完整配置 ZIP 文件")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("完整配置 ZIP 过大，请确认是否选择了正确文件")

    try:
        with zipfile.ZipFile(path, "r") as bundle:
            manifest = _read_json_zip_entry(bundle, MANIFEST_NAME)
            if manifest.get("format") != PACKAGE_FORMAT:
                raise ValueError("不是 API切换器完整配置 ZIP")
            if manifest.get("version") != PACKAGE_VERSION:
                raise ValueError(f"不支持的完整配置 ZIP 版本: {manifest.get('version')}")
            payload_name = _payload_name_from_manifest(manifest)
            payload = _decrypt_payload(_read_json_zip_entry(bundle, payload_name), password)
    except zipfile.BadZipFile as e:
        raise ValueError("完整配置 ZIP 文件损坏") from e

    imported_store = payload.get("store")
    if not isinstance(imported_store, dict):
        raise ValueError("完整配置 ZIP 中没有有效配置数据")
    imported_store = _normalized_store_for_import(imported_store)

    secrets = payload.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("完整配置 ZIP 中的密钥数据无效")
    missing_from_source = [
        ref for ref in payload.get("missing_secret_refs", [])
        if isinstance(ref, str) and ref
    ]

    skipped: list[str] = []
    valid_secrets: list[tuple[str, str]] = []
    for ref, value in secrets.items():
        if not isinstance(ref, str) or not isinstance(value, str):
            skipped.append(str(ref))
            continue
        valid_secrets.append((ref, value))

    existing_store = _normalized_store_for_import(profile_manager._load_store())
    backup_entry = backup_manager.create_backup("导入完整配置 ZIP 前自动备份")
    _write_profiles_safety_backup(backup_entry, existing_store)
    new_store = dict(existing_store)
    for key in profile_manager.PROFILE_LIST_KEYS:
        new_store[key] = _merge_profile_lists(existing_store.get(key, []), imported_store.get(key, []))
    _apply_imported_active_profiles(new_store, imported_store)
    try:
        new_store["version"] = max(int(existing_store.get("version", 1)), int(imported_store.get("version", 1)))
    except (TypeError, ValueError):
        new_store["version"] = existing_store.get("version", 1)

    restored = 0
    for ref, value in valid_secrets:
        try:
            security.set_secret(ref, value)
        except Exception as e:
            skipped.append(f"{ref} ({e})")
            continue
        restored += 1

    profile_manager._normalize_store(new_store)
    profile_manager._save_store(new_store)
    _disconnect_imported_ssh_profiles(imported_store)
    skipped.extend(_delete_unreferenced_replaced_secrets(existing_store, new_store))
    skipped.extend(f"{ref} (源包缺少密钥)" for ref in missing_from_source)

    return LocalConfigImportResult(
        profile_count=_profile_count(imported_store),
        secret_count=restored,
        skipped_secret_refs=sorted(set(skipped)),
        backup_description=backup_entry.description,
    )
