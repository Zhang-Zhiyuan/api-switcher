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

from core import backup_manager, network_diagnostic_settings, profile_manager, security
from core.atomic_io import atomic_write_bytes, atomic_write_text, replace_with_retry, temp_path_for
from models.profile import (
    BrowserProfile,
    ClaudeAccountProfile,
    ClaudeProfile,
    CodexAccountProfile,
    CodexProfile,
    SSHProfile,
)


PACKAGE_FORMAT = "api-switcher-local-config-zip"
PACKAGE_VERSION = 1
PAYLOAD_FORMAT = "api-switcher-local-config-payload"
PAYLOAD_VERSION = 1
KDF_ITERATIONS = 390_000
MAX_KDF_ITERATIONS = 2_000_000
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 64 * 1024 * 1024
MAX_DECRYPTED_PAYLOAD_BYTES = 64 * 1024 * 1024
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

PROFILE_MODEL_TYPES = {
    "claude_profiles": ClaudeProfile,
    "codex_profiles": CodexProfile,
    "claude_account_profiles": ClaudeAccountProfile,
    "codex_account_profiles": CodexAccountProfile,
    "ssh_profiles": SSHProfile,
    "browser_profiles": BrowserProfile,
}

PROFILE_SECRET_REF_FIELDS = {
    "claude_profiles": ("auth_token_ref", "primary_api_key_ref"),
    "codex_profiles": ("api_key_ref",),
    "claude_account_profiles": ("credentials_ref",),
    "codex_account_profiles": ("auth_json_ref",),
    "ssh_profiles": ("password_ref", "private_key_passphrase_ref"),
    "browser_profiles": (),
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


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    content: bytes = b""


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
    if len(plaintext) > MAX_DECRYPTED_PAYLOAD_BYTES:
        raise ValueError("完整配置 ZIP 内容过大")
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
    if iterations < 100_000 or iterations > MAX_KDF_ITERATIONS:
        raise ValueError("完整配置 ZIP KDF 参数异常")

    try:
        salt = _b64decode(str(kdf["salt"]))
        nonce = _b64decode(str(cipher["nonce"]))
        ciphertext = _b64decode(str(bundle["payload"]))
    except Exception as e:
        raise ValueError("完整配置 ZIP 编码损坏") from e
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("完整配置 ZIP 加密参数异常")

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
            decompressor = zlib.decompressobj()
            plaintext = decompressor.decompress(decrypted, MAX_DECRYPTED_PAYLOAD_BYTES + 1)
            if len(plaintext) > MAX_DECRYPTED_PAYLOAD_BYTES or decompressor.unconsumed_tail:
                raise ValueError("完整配置 ZIP 解密后内容过大")
            plaintext += decompressor.flush(MAX_DECRYPTED_PAYLOAD_BYTES + 1 - len(plaintext))
            if len(plaintext) > MAX_DECRYPTED_PAYLOAD_BYTES:
                raise ValueError("完整配置 ZIP 解密后内容过大")
            if not decompressor.eof or decompressor.unused_data:
                raise ValueError("完整配置 ZIP 压缩数据损坏")
        elif bundle.get("compression") in {None, "none"}:
            plaintext = decrypted
            if len(plaintext) > MAX_DECRYPTED_PAYLOAD_BYTES:
                raise ValueError("完整配置 ZIP 解密后内容过大")
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
    for key, ref_fields in PROFILE_SECRET_REF_FIELDS.items():
        items = store.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field_name in ref_fields:
                value = item.get(field_name)
                if isinstance(value, str) and value:
                    refs.add(value)
    return refs


def _load_network_diagnostic_settings_payload() -> dict[str, Any]:
    path = network_diagnostic_settings.SETTINGS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _collect_network_diagnostic_secret_refs(settings_payload: dict[str, Any]) -> set[str]:
    services = settings_payload.get("services") if isinstance(settings_payload.get("services"), dict) else {}
    refs: set[str] = set()
    for service in network_diagnostic_settings.SERVICE_ORDER:
        raw = services.get(service)
        if not isinstance(raw, dict):
            continue
        key_refs = raw.get("key_refs", [])
        if not isinstance(key_refs, list):
            continue
        for index, item in enumerate(key_refs):
            expected = f"network-diagnostics:{service}:{index}"
            if isinstance(item, str) and item.strip() == expected:
                refs.add(expected)
    return refs


def _normalized_network_diagnostic_settings_payload(
    settings_payload: dict[str, Any],
    *,
    strict: bool,
) -> dict[str, Any]:
    """Return only supported services with their canonical secret references."""
    services = settings_payload.get("services")
    if services is None:
        return {}
    if not isinstance(services, dict):
        if strict:
            raise ValueError("完整配置 ZIP 中的环境检测设置无效")
        return {}

    known_services = set(network_diagnostic_settings.SERVICE_ORDER)
    unknown_services = sorted(str(service) for service in services if service not in known_services)
    if strict and unknown_services:
        raise ValueError(
            "完整配置 ZIP 的环境检测设置包含不支持的服务: "
            + ", ".join(unknown_services[:3])
        )

    normalized_services: dict[str, Any] = {}
    for service in network_diagnostic_settings.SERVICE_ORDER:
        if service not in services:
            continue
        raw = services.get(service)
        if not isinstance(raw, dict):
            if strict:
                raise ValueError(f"完整配置 ZIP 的环境检测服务配置无效: {service}")
            continue

        raw_refs = raw.get("key_refs", [])
        if not isinstance(raw_refs, list):
            if strict:
                raise ValueError(f"完整配置 ZIP 的环境检测密钥引用无效: {service}")
            raw_refs = []

        canonical_refs: list[str] = []
        invalid_ref = False
        for index, item in enumerate(raw_refs):
            expected = f"network-diagnostics:{service}:{index}"
            if not isinstance(item, str) or item.strip() != expected:
                invalid_ref = True
                continue
            canonical_refs.append(expected)
        if strict and invalid_ref:
            raise ValueError(f"完整配置 ZIP 的环境检测密钥引用不是规范路径: {service}")

        enabled = network_diagnostic_settings._coerce_bool(
            raw.get("enabled"),
            network_diagnostic_settings.DEFAULT_ENABLED.get(service, False),
        )
        normalized_services[service] = {
            "enabled": enabled,
            "key_refs": canonical_refs,
        }

    if not normalized_services:
        return {}
    return {"version": 1, "services": normalized_services}


def _has_network_diagnostic_settings(settings_payload: Any) -> bool:
    if not isinstance(settings_payload, dict):
        return False
    services = settings_payload.get("services")
    return isinstance(services, dict) and bool(services)


def _normalized_store_for_import(store: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a store through the explicit profile schemas used by the app."""
    normalized = profile_manager._get_default_store()
    if not isinstance(store, dict):
        return normalized

    version = store.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        normalized["version"] = version

    for key, model_type in PROFILE_MODEL_TYPES.items():
        items = store.get(key, [])
        if not isinstance(items, list):
            continue
        cleaned_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                cleaned = model_type.from_dict(item).to_dict()
            except Exception:
                continue
            if isinstance(cleaned.get("name"), str) and cleaned["name"]:
                cleaned_items.append(cleaned)
        normalized[key] = cleaned_items

    for active_key in profile_manager.ACTIVE_PROFILE_KEYS:
        active_name = store.get(active_key)
        normalized[active_key] = active_name if isinstance(active_name, str) else None

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


def _collect_unreplaced_profile_secret_refs(
    existing_store: dict[str, Any],
    imported_store: dict[str, Any],
) -> set[str]:
    """Return refs owned by existing profiles that will survive the merge."""
    retained_store: dict[str, list[dict[str, Any]]] = {}
    for key in profile_manager.PROFILE_LIST_KEYS:
        imported_items = imported_store.get(key, [])
        if not isinstance(imported_items, list):
            imported_items = []
        imported_names = {
            item.get("name")
            for item in imported_items
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
        }
        existing_items = existing_store.get(key, [])
        if not isinstance(existing_items, list):
            existing_items = []
        retained_store[key] = [
            item
            for item in existing_items
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("name") not in imported_names
        ]
    return _collect_secret_refs(retained_store)


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

        for name in sorted(names):
            try:
                ssh_manager.disconnect(name)
            except Exception:
                continue
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


def _ensure_unique_zip_entries(bundle: zipfile.ZipFile, names: set[str]) -> None:
    counts: dict[str, int] = {}
    for info in bundle.infolist():
        if info.filename in names:
            counts[info.filename] = counts.get(info.filename, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError("完整配置 ZIP 包含重复关键条目: " + ", ".join(duplicates))


def _read_json_zip_entry(bundle: zipfile.ZipFile, name: str) -> dict[str, Any]:
    info = _validate_zip_file_entry(bundle, name)
    try:
        data = json.loads(bundle.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"完整配置 ZIP 的 {name} 已损坏") from e
    if not isinstance(data, dict):
        raise ValueError(f"完整配置 ZIP 的 {name} 格式异常")
    return data


def _json_zip_entry_bytes(name: str, data: dict[str, Any]) -> bytes:
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > MAX_ZIP_ENTRY_BYTES:
        raise ValueError(f"完整配置 ZIP 条目过大: {name}")
    return encoded


def _validate_local_config_export_path(path: Path, store: dict[str, Any]) -> None:
    """Prevent an export archive from replacing live data it represents."""
    resolved = Path(path).expanduser().resolve(strict=False)
    profile_path = Path(profile_manager.PROFILES_FILE).expanduser().resolve(strict=False)
    protected_files = {
        profile_path,
        profile_path.with_suffix(".backup"),
        Path(network_diagnostic_settings.SETTINGS_FILE).expanduser().resolve(strict=False),
    }
    if resolved in protected_files:
        raise ValueError("完整配置 ZIP 不能覆盖当前应用配置文件")

    secrets_dir = Path(security.SECRETS_DIR).expanduser().resolve(strict=False)
    if resolved == secrets_dir or secrets_dir in resolved.parents:
        raise ValueError("完整配置 ZIP 不能保存在应用密钥目录内")

    for profile in store.get("browser_profiles", []):
        if not isinstance(profile, dict):
            continue
        source_text = str(profile.get("user_data_dir") or "").strip()
        if not source_text:
            continue
        source_dir = Path(source_text).expanduser().resolve(strict=False)
        if resolved == source_dir or source_dir in resolved.parents:
            raise ValueError("完整配置 ZIP 不能保存在浏览器 Profile 目录内")


def _snapshot_file(path: Path) -> _FileSnapshot:
    if not path.exists():
        return _FileSnapshot(existed=False)
    if not path.is_file():
        raise ValueError(f"配置路径不是文件: {path}")
    return _FileSnapshot(existed=True, content=path.read_bytes())


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    if snapshot.existed:
        atomic_write_bytes(path, snapshot.content)
    else:
        path.unlink(missing_ok=True)


def _snapshot_secrets(refs: set[str]) -> dict[str, str | None]:
    return {ref: security.get_secret_strict(ref) for ref in sorted(refs)}


def _rollback_import(
    profile_snapshot: _FileSnapshot,
    settings_snapshot: _FileSnapshot | None,
    secret_snapshot: dict[str, str | None],
) -> list[str]:
    errors: list[str] = []
    for ref, previous_value in secret_snapshot.items():
        try:
            if previous_value is None:
                security.delete_secret(ref)
            else:
                security.set_secret(ref, previous_value)
        except Exception as exc:
            errors.append(f"密钥 {ref}: {exc}")

    try:
        _restore_file(profile_manager.PROFILES_FILE, profile_snapshot)
        profile_manager.clear_profile_store_cache()
    except Exception as exc:
        errors.append(f"Profile: {exc}")

    if settings_snapshot is not None:
        try:
            _restore_file(network_diagnostic_settings.SETTINGS_FILE, settings_snapshot)
            network_diagnostic_settings.clear_settings_cache()
        except Exception as exc:
            errors.append(f"环境检测设置: {exc}")
    return errors


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
            _ensure_unique_zip_entries(bundle, {MANIFEST_NAME, PAYLOAD_NAME})
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

    # Use the same global order as import so the store, diagnostics settings,
    # and referenced secrets come from one coherent in-process snapshot.
    with profile_manager._STORE_CACHE_LOCK:
        with network_diagnostic_settings._SETTINGS_CACHE_LOCK:
            return _export_local_config_zip_locked(output_path, password)


def _export_local_config_zip_locked(
    output_path: str | Path,
    password: str,
) -> LocalConfigExportResult:
    store = _normalized_store_for_import(profile_manager._load_store())
    profile_manager.validate_profile_secret_refs(store)
    network_diagnostics = _normalized_network_diagnostic_settings_payload(
        _load_network_diagnostic_settings_payload(),
        strict=False,
    )
    secret_refs = sorted(
        _collect_secret_refs(store)
        | _collect_network_diagnostic_secret_refs(network_diagnostics)
    )
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
        "network_diagnostics": network_diagnostics,
        "secrets": secrets,
        "missing_secret_refs": missing,
        "notes": [
            "完整本地配置 ZIP：包含 Profile 元数据、活动选择、环境检测设置和被引用的密钥。",
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
        "network_diagnostics": _has_network_diagnostic_settings(network_diagnostics),
        "payload": PAYLOAD_NAME,
    }

    path = Path(output_path).expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError("导出路径不能是目录")
    _validate_local_config_export_path(path, store)
    manifest_entry = _json_zip_entry_bytes(MANIFEST_NAME, manifest)
    payload_entry = _json_zip_entry_bytes(
        PAYLOAD_NAME,
        _encrypt_payload(payload, password),
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_output = temp_path_for(path)
    try:
        with zipfile.ZipFile(tmp_output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(MANIFEST_NAME, manifest_entry)
            bundle.writestr(PAYLOAD_NAME, payload_entry)
        if tmp_output.stat().st_size > MAX_ZIP_BYTES:
            raise ValueError("完整配置 ZIP 过大")
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
            _ensure_unique_zip_entries(bundle, {MANIFEST_NAME, PAYLOAD_NAME})
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
    profile_manager.validate_profile_secret_refs(imported_store)
    imported_store = _normalized_store_for_import(imported_store)
    imported_network_diagnostics = payload.get("network_diagnostics")
    if imported_network_diagnostics is not None and not isinstance(imported_network_diagnostics, dict):
        raise ValueError("完整配置 ZIP 中的环境检测设置无效")
    imported_network_diagnostics = _normalized_network_diagnostic_settings_payload(
        imported_network_diagnostics or {},
        strict=True,
    )

    secrets = payload.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("完整配置 ZIP 中的密钥数据无效")
    # Every participant follows profile -> network-settings lock order.  Both
    # RLocks stay held from the existing-state read through commit or rollback.
    with profile_manager._STORE_CACHE_LOCK:
        with network_diagnostic_settings._SETTINGS_CACHE_LOCK:
            result = _import_local_config_zip_locked(
                imported_store,
                imported_network_diagnostics,
                secrets,
            )
    _disconnect_imported_ssh_profiles(imported_store)
    return result


def _import_local_config_zip_locked(
    imported_store: dict[str, Any],
    imported_network_diagnostics: dict[str, Any],
    secrets: dict[str, Any],
) -> LocalConfigImportResult:
    imported_profile_refs = _collect_secret_refs(imported_store)
    imported_network_refs = _collect_network_diagnostic_secret_refs(
        imported_network_diagnostics
    )
    allowed_secret_refs = imported_profile_refs | imported_network_refs

    existing_store = _normalized_store_for_import(profile_manager._load_store())
    existing_profile_refs = _collect_secret_refs(existing_store)
    retained_profile_refs = _collect_unreplaced_profile_secret_refs(
        existing_store,
        imported_store,
    )
    existing_network_diagnostics = _normalized_network_diagnostic_settings_payload(
        _load_network_diagnostic_settings_payload(),
        strict=False,
    )
    existing_network_refs = _collect_network_diagnostic_secret_refs(
        existing_network_diagnostics
    )
    replaces_network_settings = _has_network_diagnostic_settings(
        imported_network_diagnostics
    )

    protected_secret_refs = set(retained_profile_refs)
    if not replaces_network_settings:
        protected_secret_refs.update(existing_network_refs)
    conflicting_refs = sorted(allowed_secret_refs & protected_secret_refs)
    if conflicting_refs:
        details = ", ".join(conflicting_refs[:3])
        suffix = f" 等 {len(conflicting_refs)} 项" if len(conflicting_refs) > 3 else ""
        raise ValueError(
            f"完整配置 ZIP 的密钥引用与未替换的现有配置冲突: {details}{suffix}"
        )

    skipped: list[str] = []
    valid_secrets: list[tuple[str, str]] = []
    for ref, value in secrets.items():
        if not isinstance(ref, str) or not isinstance(value, str):
            skipped.append(str(ref))
            continue
        if ref not in allowed_secret_refs:
            skipped.append(f"{ref} (未被导入配置引用)")
            continue
        valid_secrets.append((ref, value))

    valid_secret_refs = {ref for ref, _value in valid_secrets}
    missing_secret_refs = allowed_secret_refs - valid_secret_refs
    skipped.extend(
        f"{ref} (源包缺少密钥，已清除本机旧值)"
        for ref in sorted(missing_secret_refs)
    )

    new_store = dict(existing_store)
    for key in profile_manager.PROFILE_LIST_KEYS:
        new_store[key] = _merge_profile_lists(existing_store.get(key, []), imported_store.get(key, []))
    _apply_imported_active_profiles(new_store, imported_store)
    try:
        new_store["version"] = max(int(existing_store.get("version", 1)), int(imported_store.get("version", 1)))
    except (TypeError, ValueError):
        new_store["version"] = existing_store.get("version", 1)

    final_profile_refs = _collect_secret_refs(new_store)
    final_network_refs = (
        imported_network_refs
        if replaces_network_settings
        else existing_network_refs
    )
    replaceable_secret_refs = existing_profile_refs - retained_profile_refs
    if replaces_network_settings:
        replaceable_secret_refs.update(existing_network_refs)
    obsolete_secret_refs = (
        existing_profile_refs
        | (existing_network_refs if replaces_network_settings else set())
    ) - (final_profile_refs | final_network_refs)
    secret_refs_to_delete = missing_secret_refs | obsolete_secret_refs
    secret_refs_to_change = valid_secret_refs | secret_refs_to_delete

    profile_snapshot = _snapshot_file(profile_manager.PROFILES_FILE)
    settings_snapshot = (
        _snapshot_file(network_diagnostic_settings.SETTINGS_FILE)
        if replaces_network_settings
        else None
    )
    secret_snapshot = _snapshot_secrets(secret_refs_to_change)
    unowned_local_collisions = sorted(
        ref
        for ref in missing_secret_refs
        if secret_snapshot.get(ref) is not None
        and ref not in replaceable_secret_refs
    )
    if unowned_local_collisions:
        details = ", ".join(unowned_local_collisions[:3])
        suffix = (
            f" 等 {len(unowned_local_collisions)} 项"
            if len(unowned_local_collisions) > 3
            else ""
        )
        raise ValueError(f"完整配置 ZIP 的密钥引用与本机未归属密钥冲突: {details}{suffix}")

    backup_entry = backup_manager.create_backup("导入 ZIP 前客户端运行配置备份")

    restored = 0
    try:
        for ref, value in valid_secrets:
            security.set_secret(ref, value)
            restored += 1
        for ref in sorted(secret_refs_to_delete):
            security.delete_secret(ref)

        profile_manager._normalize_store(new_store)
        profile_manager._save_store(new_store)
        if settings_snapshot is not None:
            atomic_write_text(
                network_diagnostic_settings.SETTINGS_FILE,
                json.dumps(imported_network_diagnostics, ensure_ascii=False, indent=2),
            )
            network_diagnostic_settings.clear_settings_cache()
    except Exception as import_error:
        rollback_errors = _rollback_import(profile_snapshot, settings_snapshot, secret_snapshot)
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise RuntimeError(f"完整配置 ZIP 导入失败，且自动回滚不完整: {details}") from import_error
        raise

    return LocalConfigImportResult(
        profile_count=_profile_count(imported_store),
        secret_count=restored,
        skipped_secret_refs=sorted(set(skipped)),
        backup_description=backup_entry.description,
    )
