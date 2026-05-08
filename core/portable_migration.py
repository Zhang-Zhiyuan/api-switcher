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
from typing import Any, Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from config import paths
from core import profile_manager, security


BUNDLE_FORMAT = "api-switcher-portable-profiles"
BUNDLE_VERSION = 1
KDF_ITERATIONS = 390_000
MAX_BROWSER_FILE_BYTES = 256 * 1024 * 1024
MAX_BROWSER_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_BUNDLE_FILE_BYTES = 3 * 1024 * 1024 * 1024


@dataclass
class PortableExportResult:
    path: Path
    profile_count: int
    secret_count: int
    missing_secret_refs: list[str]
    browser_profile_count: int = 0
    browser_file_count: int = 0
    browser_bytes: int = 0
    skipped_browser_files: list[str] | None = None


@dataclass
class PortableImportResult:
    profile_count: int
    secret_count: int
    skipped_secret_refs: list[str]
    browser_profile_count: int = 0
    browser_file_count: int = 0
    skipped_browser_files: list[str] | None = None


BROWSER_SKIP_DIRS = {
    "cache",
    "code cache",
    "gpucache",
    "shadercache",
    "grshadercache",
    "crashpad",
    "browsermetrics",
    "optimization hints",
    "safe browsing",
    "segmentation platform",
}
BROWSER_SKIP_FILES = {
    "singletonlock",
    "singletoncookie",
    "singletonsocket",
    "lockfile",
}
BROWSER_SKIP_SUFFIXES = {
    ".log",
    ".tmp",
    ".lock",
    ".backup",
    ".crdownload",
}


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


def _store_version(store: dict[str, Any]) -> int:
    try:
        return int(store.get("version", 1))
    except (TypeError, ValueError):
        return 1


def _safe_profile_dir_name(name: str, browser_type: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "profile"
    browser = browser_type if browser_type in {"chrome", "edge"} else "browser"
    return f"{browser}_{safe}"


def _managed_browser_profiles_dir() -> Path:
    return paths.STORAGE_DIR / "browser_profiles"


def _is_path_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _should_skip_browser_path(path: Path, relative_parts: Iterable[str]) -> bool:
    if path.is_symlink():
        return True

    lowered_parts = [part.lower() for part in relative_parts]
    if any(part in BROWSER_SKIP_DIRS for part in lowered_parts[:-1]):
        return True

    name = path.name.lower()
    if name in BROWSER_SKIP_FILES:
        return True
    if any(name.endswith(suffix) for suffix in BROWSER_SKIP_SUFFIXES):
        return True
    return False


def _browser_profile_export_path(profile: dict[str, Any]) -> Path | None:
    if profile.get("profile_mode") != "managed":
        return None
    user_data_dir = profile.get("user_data_dir")
    if not isinstance(user_data_dir, str) or not user_data_dir.strip():
        return None

    try:
        source = Path(user_data_dir).expanduser().resolve()
    except OSError:
        return None

    if not source.exists() or not source.is_dir():
        return None
    if not _is_path_inside(source, _managed_browser_profiles_dir()) and not profile.get("created_by_app"):
        return None
    return source


def _collect_browser_profile_data(store: dict[str, Any]) -> dict[str, Any]:
    browser_profiles = store.get("browser_profiles", [])
    if not isinstance(browser_profiles, list):
        return {"profiles": {}, "skipped": []}

    exported_profiles: dict[str, Any] = {}
    skipped: list[str] = []
    for profile in browser_profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        source = _browser_profile_export_path(profile)
        if source is None:
            continue

        files: list[dict[str, Any]] = []
        total_bytes = 0
        for file_path in source.rglob("*"):
            try:
                relative = file_path.relative_to(source)
                relative_parts = relative.parts
                if _should_skip_browser_path(file_path, relative_parts):
                    skipped.append(f"{profile['name']}:{relative.as_posix()}")
                    continue
                if not file_path.is_file():
                    continue
                stat = file_path.stat()
                if stat.st_size > MAX_BROWSER_FILE_BYTES:
                    skipped.append(f"{profile['name']}:{relative.as_posix()} (文件过大)")
                    continue
                if total_bytes + stat.st_size > MAX_BROWSER_TOTAL_BYTES:
                    skipped.append(f"{profile['name']}:{relative.as_posix()} (浏览器数据总量超过上限)")
                    continue
                data = file_path.read_bytes()
                total_bytes += len(data)
                files.append({
                    "path": relative.as_posix(),
                    "data": _b64encode(data),
                    "mtime": stat.st_mtime,
                    "size": len(data),
                })
            except (OSError, ValueError) as e:
                try:
                    relative_text = file_path.relative_to(source).as_posix()
                except ValueError:
                    relative_text = str(file_path)
                skipped.append(f"{profile['name']}:{relative_text} ({e})")

        exported_profiles[profile["name"]] = {
            "browser_type": profile.get("browser_type"),
            "source_dir": str(source),
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        }

    return {"profiles": exported_profiles, "skipped": skipped}


def _normalize_imported_browser_profiles(imported_store: dict[str, Any], browser_data: dict[str, Any]) -> None:
    data_profiles = browser_data.get("profiles") if isinstance(browser_data, dict) else {}
    if not isinstance(data_profiles, dict):
        data_profiles = {}

    profiles = imported_store.get("browser_profiles", [])
    if not isinstance(profiles, list):
        return

    managed_root = _managed_browser_profiles_dir()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = profile.get("name")
        if name not in data_profiles:
            continue

        target = managed_root / _safe_profile_dir_name(str(name), str(profile.get("browser_type") or "browser"))
        profile["profile_mode"] = "managed"
        profile["created_by_app"] = True
        profile["allow_full_reset"] = True
        profile["user_data_dir"] = str(target)

        explicit_exe = profile.get("browser_executable")
        if isinstance(explicit_exe, str) and explicit_exe:
            try:
                if not Path(explicit_exe).exists():
                    profile["browser_executable"] = None
            except OSError:
                profile["browser_executable"] = None


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _restore_interrupted_browser_import(target_dir: Path, backup_dir: Path) -> None:
    if target_dir.exists():
        if backup_dir.exists():
            _remove_path(backup_dir)
        return
    if backup_dir.exists():
        shutil.move(str(backup_dir), str(target_dir))


def _commit_browser_profile_dir(staging_dir: Path, target_dir: Path, backup_dir: Path) -> None:
    target_moved_to_backup = False
    staging_moved_to_target = False
    try:
        if backup_dir.exists():
            _remove_path(backup_dir)
        if target_dir.exists():
            shutil.move(str(target_dir), str(backup_dir))
            target_moved_to_backup = True
        shutil.move(str(staging_dir), str(target_dir))
        staging_moved_to_target = True
    except Exception:
        if not staging_moved_to_target and staging_dir.exists():
            _remove_path(staging_dir)
        if staging_moved_to_target and target_dir.exists():
            _remove_path(target_dir)
        if target_moved_to_backup and backup_dir.exists():
            shutil.move(str(backup_dir), str(target_dir))
        raise
    else:
        if backup_dir.exists():
            _remove_path(backup_dir)


def _restore_browser_profile_data(browser_data: dict[str, Any]) -> tuple[int, int, list[str]]:
    data_profiles = browser_data.get("profiles") if isinstance(browser_data, dict) else {}
    if not isinstance(data_profiles, dict):
        return 0, 0, []

    restored_profiles = 0
    restored_files = 0
    skipped: list[str] = []
    managed_root = _managed_browser_profiles_dir()
    managed_root.mkdir(parents=True, exist_ok=True)

    for profile_name, entry in data_profiles.items():
        if not isinstance(entry, dict):
            skipped.append(str(profile_name))
            continue

        target_dir = managed_root / _safe_profile_dir_name(str(profile_name), str(entry.get("browser_type") or "browser"))
        staging_dir = target_dir.with_name(target_dir.name + ".importing")
        backup_dir = target_dir.with_name(target_dir.name + ".pre-import")
        try:
            _restore_interrupted_browser_import(target_dir, backup_dir)
            if staging_dir.exists():
                _remove_path(staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            skipped.append(f"{profile_name}:无法准备目录 ({e})")
            continue

        files = entry.get("files", [])
        if not isinstance(files, list):
            skipped.append(f"{profile_name}:files 字段无效")
            _remove_path(staging_dir)
            continue

        profile_restored_files = 0
        profile_total_bytes = 0
        profile_errors: list[str] = []
        for file_info in files:
            if not isinstance(file_info, dict):
                profile_errors.append(f"{profile_name}:无效文件记录")
                continue
            relative_text = file_info.get("path")
            data_text = file_info.get("data")
            if not isinstance(relative_text, str) or not isinstance(data_text, str):
                profile_errors.append(f"{profile_name}:无效文件字段")
                continue
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                profile_errors.append(f"{profile_name}:{relative_text}")
                continue

            target_file = staging_dir / relative
            if not _is_path_inside(target_file, staging_dir):
                profile_errors.append(f"{profile_name}:{relative_text}")
                continue

            declared_size = file_info.get("size")
            if isinstance(declared_size, int) and declared_size > MAX_BROWSER_FILE_BYTES:
                profile_errors.append(f"{profile_name}:{relative_text} (文件过大)")
                continue

            try:
                data = _b64decode(data_text)
                if len(data) > MAX_BROWSER_FILE_BYTES:
                    profile_errors.append(f"{profile_name}:{relative_text} (文件过大)")
                    continue
                if profile_total_bytes + len(data) > MAX_BROWSER_TOTAL_BYTES:
                    profile_errors.append(f"{profile_name}:{relative_text} (浏览器数据总量超过上限)")
                    continue
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(data)
                mtime = file_info.get("mtime")
                if isinstance(mtime, (int, float)):
                    os.utime(target_file, (mtime, mtime))
                profile_total_bytes += len(data)
                profile_restored_files += 1
            except Exception as e:
                profile_errors.append(f"{profile_name}:{relative_text} ({e})")

        if profile_errors:
            skipped.extend(profile_errors)
            skipped.append(f"{profile_name}:存在无效或未写入文件，已保留原目录")
            _remove_path(staging_dir)
            continue

        if profile_restored_files == 0 and target_dir.exists():
            skipped.append(f"{profile_name}:没有可恢复文件，已保留原目录")
            _remove_path(staging_dir)
            continue

        try:
            _commit_browser_profile_dir(staging_dir, target_dir, backup_dir)
        except Exception as e:
            skipped.append(f"{profile_name}:无法替换目录 ({e})")
            continue

        restored_profiles += 1
        restored_files += profile_restored_files

    return restored_profiles, restored_files, skipped


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

    store = profile_manager._load_store()
    secret_refs = sorted(_collect_secret_refs_from_store(store))
    secrets: dict[str, str] = {}
    missing: list[str] = []

    for ref in secret_refs:
        value = security.get_secret(ref)
        if value is None:
            missing.append(ref)
        else:
            secrets[ref] = value

    browser_data = _collect_browser_profile_data(store)

    payload = {
        "payload_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "store": store,
        "secrets": secrets,
        "browser_data": browser_data,
        "missing_secret_refs": missing,
        "notes": [
            "Includes app-managed profile metadata, secrets, and managed browser profile data.",
            "Browser cookie/session migration is best-effort and may fail if the browser encrypts data to a machine-specific key.",
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
        browser_profile_count=len(browser_data.get("profiles", {})),
        browser_file_count=sum(
            int(entry.get("file_count", 0))
            for entry in browser_data.get("profiles", {}).values()
            if isinstance(entry, dict)
        ),
        browser_bytes=sum(
            int(entry.get("total_bytes", 0))
            for entry in browser_data.get("profiles", {}).values()
            if isinstance(entry, dict)
        ),
        skipped_browser_files=browser_data.get("skipped", []),
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

    browser_data = payload.get("browser_data", {})
    if not isinstance(browser_data, dict):
        browser_data = {}
    _normalize_imported_browser_profiles(imported_store, browser_data)

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

    browser_profile_count, browser_file_count, skipped_browser_files = _restore_browser_profile_data(browser_data)

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
        browser_profile_count=browser_profile_count,
        browser_file_count=browser_file_count,
        skipped_browser_files=skipped_browser_files,
    )
