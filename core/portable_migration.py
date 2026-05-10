"""Password-protected portable profile migration.

This module exports profile metadata plus app-managed secrets into a portable
file. Secrets are decrypted from the current machine and re-encrypted with a
user-provided migration password so they can be restored on another computer.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
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
MAX_BROWSER_FILE_BYTES = 512 * 1024 * 1024
MAX_BROWSER_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

BROWSER_SKIP_DIR_NAMES = {
    "browsermetrics",
    "cache",
    "code cache",
    "crash reports",
    "crashpad",
    "gpucache",
    "grshadercache",
    "optimizationhints",
    "pnacltranslationcache",
    "safebrowsing",
    "shadercache",
    "swreporter",
}
BROWSER_SKIP_FILE_NAMES = {
    "debug.log",
    "lockfile",
    "singletoncookie",
    "singletonlock",
    "singletonsock",
    "singletonsock.lock",
    "singletonsocket",
}
BROWSER_SKIP_SUFFIXES = {
    ".crdownload",
    ".lock",
    ".log",
    ".tmp",
}


@dataclass
class PortableExportResult:
    path: Path
    profile_count: int
    secret_count: int
    missing_secret_refs: list[str]
    browser_file_count: int = 0
    browser_bytes: int = 0
    skipped_browser_files: list[str] | None = None


@dataclass
class PortableImportResult:
    profile_count: int
    secret_count: int
    skipped_secret_refs: list[str]
    browser_file_count: int = 0
    browser_bytes: int = 0
    skipped_browser_files: list[str] | None = None


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


def _browser_profile_basename(name: str, browser_type: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "profile"
    browser = browser_type if browser_type in {"chrome", "edge"} else "chrome"
    return f"{browser}_{safe}"


def _managed_browser_profile_path(name: str, browser_type: str) -> Path:
    from config import paths

    return paths.STORAGE_DIR / "browser_profiles" / _browser_profile_basename(name, browser_type)


def _browser_profile_for_portable(profile: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(profile)
    name = str(cleaned.get("name") or "BrowserProfile")
    browser_type = str(cleaned.get("browser_type") or "chrome")
    cleaned["browser_type"] = browser_type if browser_type in {"chrome", "edge"} else "chrome"
    cleaned["profile_mode"] = "managed"
    cleaned["user_data_dir"] = str(_managed_browser_profile_path(name, cleaned["browser_type"]))
    cleaned["allow_full_reset"] = True
    cleaned["created_by_app"] = True
    cleaned["browser_executable"] = None
    notes = str(cleaned.get("notes") or "").strip()
    suffix = "跨机器迁移导入"
    cleaned["notes"] = f"{notes} | {suffix}" if notes else suffix
    return cleaned


def _should_skip_browser_relative_path(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if any(part in BROWSER_SKIP_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in BROWSER_SKIP_FILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in BROWSER_SKIP_SUFFIXES):
        return True
    if name.startswith("singleton"):
        return True
    return False


def _portable_relative_path(relative: Path) -> str:
    return PurePosixPath(*relative.parts).as_posix()


def _safe_browser_relative_path(value: str) -> Path:
    rel = PurePosixPath(str(value).replace("\\", "/"))
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"迁移包包含非法浏览器文件路径: {value}")
    return Path(*rel.parts)


def _collect_browser_profile_data(store: dict[str, Any]) -> tuple[dict[str, Any], list[str], int, int]:
    browser_data: dict[str, Any] = {}
    skipped: list[str] = []
    total_bytes = 0
    file_count = 0
    portable_profiles: list[dict[str, Any]] = []

    for profile in store.get("browser_profiles", []):
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        name = profile["name"]
        source_dir = Path(str(profile.get("user_data_dir") or "")).expanduser()
        if not source_dir.exists() or not source_dir.is_dir():
            skipped.append(f"{name}: Profile 目录不存在")
            continue
        if source_dir.is_symlink():
            skipped.append(f"{name}: 跳过符号链接目录")
            continue

        files: list[dict[str, Any]] = []
        for path in source_dir.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(source_dir)
                relative_text = _portable_relative_path(relative)
                if _should_skip_browser_relative_path(relative):
                    continue
                size = path.stat().st_size
                if size > MAX_BROWSER_FILE_BYTES:
                    skipped.append(f"{name}/{relative_text}: 文件过大，已跳过")
                    continue
                if total_bytes + size > MAX_BROWSER_TOTAL_BYTES:
                    skipped.append(f"{name}/{relative_text}: 浏览器数据超过迁移包上限，已跳过")
                    continue
                content = path.read_bytes()
                total_bytes += len(content)
                file_count += 1
                files.append({
                    "path": relative_text,
                    "size": len(content),
                    "compression": "zlib",
                    "data": _b64encode(zlib.compress(content, level=6)),
                })
            except OSError as e:
                skipped.append(f"{name}/{path.name}: 无法读取，已跳过 ({e})")

        if files:
            portable_profile = _browser_profile_for_portable(profile)
            portable_profiles.append(portable_profile)
            browser_data[name] = {
                "profile": portable_profile,
                "source_path": str(source_dir),
                "file_count": len(files),
                "files": files,
            }
        else:
            skipped.append(f"{name}: 没有可迁移的浏览器文件")

    store["browser_profiles"] = portable_profiles
    return browser_data, skipped, file_count, total_bytes


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
    stripped["claude_account_profiles"] = []
    stripped["codex_account_profiles"] = []
    stripped["browser_profiles"] = [
        dict(profile)
        for profile in store.get("browser_profiles", [])
        if isinstance(profile, dict) and isinstance(profile.get("name"), str)
    ]

    active_claude = stripped.get("active_claude_profile")
    if active_claude not in {profile.get("name") for profile in claude_profiles}:
        stripped["active_claude_profile"] = None

    active_codex = stripped.get("active_codex_profile")
    if active_codex not in {profile.get("name") for profile in codex_profiles}:
        stripped["active_codex_profile"] = None

    active_browser = stripped.get("active_browser_profile")
    if active_browser not in {profile.get("name") for profile in stripped["browser_profiles"]}:
        stripped["active_browser_profile"] = None
    stripped["active_claude_account"] = None
    stripped["active_codex_account"] = None
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


def _restore_browser_data(browser_data: dict[str, Any]) -> tuple[int, int, list[str]]:
    restored_files = 0
    restored_bytes = 0
    skipped: list[str] = []

    if not isinstance(browser_data, dict):
        return restored_files, restored_bytes, ["浏览器数据格式异常，已跳过"]

    from config import paths

    managed_root = (paths.STORAGE_DIR / "browser_profiles").resolve()
    managed_root.mkdir(parents=True, exist_ok=True)

    for name, item in browser_data.items():
        if not isinstance(item, dict):
            skipped.append(f"{name}: 浏览器数据格式异常")
            continue
        profile = item.get("profile")
        if not isinstance(profile, dict):
            skipped.append(f"{name}: 缺少浏览器 Profile 元数据")
            continue

        target = Path(str(profile.get("user_data_dir") or "")).expanduser()
        try:
            target = target.resolve()
        except OSError:
            target = target.absolute()
        if target != managed_root and managed_root not in target.parents:
            skipped.append(f"{name}: 目标目录不在托管目录内，已跳过")
            continue

        backup = target.with_name(f"{target.name}.import_backup")
        try:
            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                target.replace(backup)
            target.mkdir(parents=True, exist_ok=True)

            files = item.get("files", [])
            if not isinstance(files, list):
                raise ValueError("浏览器文件列表格式异常")

            for file_entry in files:
                if not isinstance(file_entry, dict):
                    skipped.append(f"{name}: 跳过异常文件条目")
                    continue
                rel_path = _safe_browser_relative_path(str(file_entry.get("path") or ""))
                encoded = file_entry.get("data")
                if not isinstance(encoded, str):
                    skipped.append(f"{name}/{rel_path}: 文件数据缺失")
                    continue
                try:
                    raw = _b64decode(encoded)
                    if file_entry.get("compression") == "zlib":
                        content = zlib.decompress(raw)
                    elif file_entry.get("compression") in {None, "none"}:
                        content = raw
                    else:
                        raise ValueError("不支持的文件压缩方式")
                except Exception as e:
                    skipped.append(f"{name}/{rel_path}: 解码失败 ({e})")
                    continue
                dest = target / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
                restored_files += 1
                restored_bytes += len(content)

            if backup.exists():
                shutil.rmtree(backup)
        except Exception as e:
            skipped.append(f"{name}: 恢复失败 ({e})")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                try:
                    backup.replace(target)
                except OSError:
                    skipped.append(f"{name}: 原目录恢复失败")

    return restored_files, restored_bytes, skipped


def _prepare_imported_browser_data(browser_data: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(browser_data, dict):
        return {}, []

    prepared: dict[str, Any] = {}
    profiles: list[dict[str, Any]] = []
    for name, item in browser_data.items():
        if not isinstance(item, dict):
            continue
        profile = item.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        portable_profile = _browser_profile_for_portable(profile)
        prepared[name] = dict(item)
        prepared[name]["profile"] = portable_profile
        profiles.append(portable_profile)
    return prepared, profiles


def export_portable_profiles(output_path: str | Path, password: str) -> PortableExportResult:
    """Export all profiles and available app-managed secrets."""
    if len(password) < 8:
        raise ValueError("迁移密码至少需要 8 个字符")

    store = _sanitize_portable_store(profile_manager._load_store())
    browser_data, skipped_browser_files, browser_file_count, browser_bytes = _collect_browser_profile_data(store)
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
        "browser_data": browser_data,
        "skipped_browser_files": skipped_browser_files,
        "notes": [
            "Includes app-managed Claude/Codex/SSH profile metadata and secrets.",
            "Browser profile directories are included best-effort for cross-machine migration.",
            "Chromium cookies may still be bound to the source OS account by the browser itself.",
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
        browser_file_count=browser_file_count,
        browser_bytes=browser_bytes,
        skipped_browser_files=skipped_browser_files,
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

    browser_data, browser_profiles = _prepare_imported_browser_data(payload.get("browser_data", {}))
    imported_store["browser_profiles"] = browser_profiles
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

    browser_file_count, browser_bytes, skipped_browser_files = _restore_browser_data(browser_data)
    skipped_browser_files.extend(
        item for item in payload.get("skipped_browser_files", [])
        if isinstance(item, str)
    )

    return PortableImportResult(
        profile_count=_count_profiles(imported_store),
        secret_count=restored,
        skipped_secret_refs=skipped,
        browser_file_count=browser_file_count,
        browser_bytes=browser_bytes,
        skipped_browser_files=skipped_browser_files,
    )
