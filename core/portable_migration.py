"""Password-protected portable profile migration.

This module exports profile metadata plus app-managed secrets into a portable
file. Secrets are decrypted from the current machine and re-encrypted with a
user-provided migration password so they can be restored on another computer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core import profile_manager, security
from core.atomic_io import atomic_write_bytes, atomic_write_text, temp_path_for


BUNDLE_FORMAT = "api-switcher-portable-profiles"
BUNDLE_VERSION = 1
KDF_ITERATIONS = 390_000
MAX_KDF_ITERATIONS = 2_000_000
MAX_BUNDLE_FILE_BYTES = 3 * 1024 * 1024 * 1024
MAX_DECRYPTED_PAYLOAD_BYTES = 3 * 1024 * 1024 * 1024
MAX_BROWSER_FILE_BYTES = 512 * 1024 * 1024
MAX_BROWSER_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

BROWSER_SKIP_DIR_NAMES = {
    "browsermetrics",
    "cache",
    "cachestorage",
    "code cache",
    "crash reports",
    "crashpad",
    "gpucache",
    "gpu cache",
    "grshadercache",
    "dawncache",
    "dawngraphitecache",
    "dawnwebgpucache",
    "media cache",
    "optimizationhints",
    "pnacltranslationcache",
    "safebrowsing",
    "shadercache",
    "scriptcache",
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
    ".tmp",
}
BROWSER_ALLOWED_DEFAULT_FILES = {
    "account web data",
    "cookies",
    "login data",
    "login data for account",
    "preferences",
    "secure preferences",
    "trusted vault",
    "web data",
}
BROWSER_ALLOWED_DEFAULT_DIRS = {
    "indexeddb",
    "local storage",
    "network",
    "session storage",
    "storage",
    "webstorage",
}
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
PORTABLE_PROFILE_SELECTION_KEYS = (
    "claude_profiles",
    "codex_profiles",
    "ssh_profiles",
    "browser_profiles",
)
_PORTABLE_ACTIVE_PROFILE_LIST_KEYS = {
    "active_claude_profile": "claude_profiles",
    "active_codex_profile": "codex_profiles",
    "active_claude_account": "claude_account_profiles",
    "active_codex_account": "codex_account_profiles",
    "active_ssh_profile": "ssh_profiles",
    "active_browser_profile": "browser_profiles",
}
_PORTABLE_SECRET_REF_FIELDS = {
    "claude_profiles": ("auth_token_ref", "primary_api_key_ref"),
    "codex_profiles": ("api_key_ref",),
    "ssh_profiles": ("password_ref", "private_key_passphrase_ref"),
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


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    content: bytes = b""


@dataclass(frozen=True)
class _BrowserRestoreSnapshot:
    target: Path
    backup: Path
    had_original: bool


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _zlib_decompress_bounded(
    data: bytes,
    max_output_bytes: int,
    *,
    too_large_message: str,
    corrupt_message: str,
) -> bytes:
    """Decompress zlib data without allocating beyond the configured limit."""
    if max_output_bytes < 0:
        raise ValueError(too_large_message)

    try:
        decompressor = zlib.decompressobj()
        output = decompressor.decompress(data, max_output_bytes + 1)
        if len(output) > max_output_bytes or decompressor.unconsumed_tail:
            raise ValueError(too_large_message)
        output += decompressor.flush(max_output_bytes + 1 - len(output))
    except zlib.error as exc:
        raise ValueError(corrupt_message) from exc

    if len(output) > max_output_bytes:
        raise ValueError(too_large_message)
    if not decompressor.eof or decompressor.unused_data:
        raise ValueError(corrupt_message)
    return output


def _base64_encoded_length(raw_size: int) -> int:
    return 4 * ((raw_size + 2) // 3)


def _zlib_compress_bound(source_size: int) -> int:
    """Return zlib's documented upper bound for a compressed byte string."""
    return (
        source_size
        + (source_size >> 12)
        + (source_size >> 14)
        + (source_size >> 25)
        + 13
    )


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
    if iterations < 100_000 or iterations > MAX_KDF_ITERATIONS:
        raise ValueError("迁移包 KDF 参数异常")

    try:
        salt = _b64decode(str(kdf["salt"]))
        nonce = _b64decode(str(cipher["nonce"]))
        ciphertext = _b64decode(str(bundle["payload"]))
    except Exception as e:
        raise ValueError("迁移包编码损坏") from e
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError("迁移包加密参数异常")

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
    if compression == "zlib":
        plaintext = _zlib_decompress_bounded(
            decrypted,
            MAX_DECRYPTED_PAYLOAD_BYTES,
            too_large_message="迁移包解密后内容过大",
            corrupt_message="迁移包压缩数据损坏",
        )
    elif compression in {None, "none"}:
        if len(decrypted) > MAX_DECRYPTED_PAYLOAD_BYTES:
            raise ValueError("迁移包解密后内容过大")
        plaintext = decrypted
    else:
        raise ValueError(f"不支持的迁移包压缩方式: {compression}")

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("迁移包内容损坏") from e
    if not isinstance(payload, dict) or payload.get("payload_version") != 1:
        raise ValueError("迁移包内容版本不受支持")
    return payload


def _collect_secret_refs_from_store(store: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key, ref_fields in _PORTABLE_SECRET_REF_FIELDS.items():
        profiles = store.get(key, [])
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            for field_name in ref_fields:
                value = profile.get(field_name)
                if isinstance(value, str) and value:
                    refs.add(value)
    return refs


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
    return _collect_secret_refs_from_store(retained_store)


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


def _unique_browser_profile_basename(
    name: str,
    browser_type: str,
    used_basenames: set[str] | None = None,
) -> str:
    basename = _browser_profile_basename(name, browser_type)
    if used_basenames is None:
        return basename

    key = basename.casefold()
    if key not in used_basenames:
        used_basenames.add(key)
        return basename

    digest = hashlib.sha256(f"{browser_type}\0{name}".encode("utf-8")).hexdigest()[:8]
    candidate = f"{basename}_{digest}"
    index = 2
    while candidate.casefold() in used_basenames:
        candidate = f"{basename}_{digest}_{index}"
        index += 1
    used_basenames.add(candidate.casefold())
    return candidate


def _browser_profile_for_portable(
    profile: dict[str, Any],
    used_basenames: set[str] | None = None,
) -> dict[str, Any]:
    cleaned = profile_manager.BrowserProfile.from_dict(profile).to_dict()
    name = str(cleaned.get("name") or "BrowserProfile")
    cleaned["name"] = name
    browser_type = str(cleaned.get("browser_type") or "chrome")
    cleaned["browser_type"] = browser_type if browser_type in {"chrome", "edge"} else "chrome"
    cleaned["profile_mode"] = "managed"
    basename = _unique_browser_profile_basename(name, cleaned["browser_type"], used_basenames)
    from config import paths

    cleaned["user_data_dir"] = str(paths.STORAGE_DIR / "browser_profiles" / basename)
    cleaned["allow_full_reset"] = True
    cleaned["created_by_app"] = True
    cleaned["browser_executable"] = None
    notes = str(cleaned.get("notes") or "").strip()
    suffix = "跨机器迁移导入"
    cleaned["notes"] = f"{notes} | {suffix}" if notes else suffix
    return cleaned


def _browser_default_file_is_allowed(name: str) -> bool:
    lowered = name.lower()
    if lowered in BROWSER_ALLOWED_DEFAULT_FILES:
        return True
    return any(
        lowered == f"{base}{suffix}"
        for base in BROWSER_ALLOWED_DEFAULT_FILES
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    )


def _browser_relative_path_is_allowlisted(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if parts == ["local state"]:
        return True
    if len(parts) < 2 or parts[0] != "default":
        return False
    if len(parts) == 2:
        return _browser_default_file_is_allowed(parts[1])

    category = parts[1]
    if category in BROWSER_ALLOWED_DEFAULT_DIRS:
        return True
    return category == "service worker" and len(parts) >= 4 and parts[2] == "database"


def _browser_directory_may_contain_allowed_files(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if parts == ["default"]:
        return True
    if len(parts) < 2 or parts[0] != "default":
        return False
    category = parts[1]
    if category in BROWSER_ALLOWED_DEFAULT_DIRS:
        return True
    if category != "service worker":
        return False
    return len(parts) == 2 or parts[2] == "database"


def _should_skip_browser_relative_path(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if not _browser_relative_path_is_allowlisted(relative):
        return True
    if any(part in BROWSER_SKIP_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in BROWSER_SKIP_FILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in BROWSER_SKIP_SUFFIXES):
        return True
    if name.endswith(".log"):
        # Chromium LevelDB .log files contain live Local Storage,
        # Session Storage and IndexedDB records.  Other logs are runtime noise.
        storage_parts = parts[:-1]
        is_storage_log = any(
            part == "leveldb" or part.endswith(".leveldb") or part == "session storage"
            for part in storage_parts
        ) or ("service worker" in storage_parts and "database" in storage_parts)
        if not is_storage_log:
            return True
    if name.startswith("singleton"):
        return True
    return False


def _path_is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _resolved_export_path(path: Path, source_root: Path) -> Path | None:
    if _path_is_reparse_point(path):
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if resolved == source_root or source_root not in resolved.parents:
        return None
    return resolved


def _iter_portable_browser_paths(source_dir: Path):
    """Yield allowlisted files without traversing links, junctions or caches."""
    local_state = source_dir / "Local State"
    resolved_local_state = _resolved_export_path(local_state, source_dir)
    if resolved_local_state is not None and resolved_local_state.is_file():
        yield local_state

    default_dir = source_dir / "Default"
    try:
        resolved_default = _resolved_export_path(default_dir, source_dir)
        if resolved_default is None or not resolved_default.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(
            default_dir,
            topdown=True,
            followlinks=False,
        ):
            current = Path(dirpath)
            safe_directories: list[str] = []
            for dirname in dirnames:
                child = current / dirname
                relative = child.relative_to(source_dir)
                lowered_parts = [part.lower() for part in relative.parts]
                if any(part in BROWSER_SKIP_DIR_NAMES for part in lowered_parts):
                    continue
                if not _browser_directory_may_contain_allowed_files(relative):
                    continue
                resolved_child = _resolved_export_path(child, source_dir)
                if resolved_child is None or not resolved_child.is_dir():
                    continue
                safe_directories.append(dirname)
            dirnames[:] = safe_directories

            for filename in filenames:
                path = current / filename
                relative = path.relative_to(source_dir)
                if _should_skip_browser_relative_path(relative):
                    continue
                resolved_path = _resolved_export_path(path, source_dir)
                if resolved_path is not None and resolved_path.is_file():
                    yield path
    except OSError:
        return


def _portable_relative_path(relative: Path) -> str:
    return PurePosixPath(*relative.parts).as_posix()


def _safe_browser_relative_path(value: str) -> Path:
    normalized = str(value).replace("\\", "/")
    rel = PurePosixPath(normalized)
    windows_rel = PureWindowsPath(normalized)
    if (
        rel.is_absolute()
        or windows_rel.drive
        or windows_rel.root
        or any(part in {"", ".", ".."} or ":" in part for part in rel.parts)
    ):
        raise ValueError(f"迁移包包含非法浏览器文件路径: {value}")
    return Path(*rel.parts)


def _collect_browser_profile_data(store: dict[str, Any]) -> tuple[dict[str, Any], list[str], int, int]:
    browser_data: dict[str, Any] = {}
    skipped: list[str] = []
    total_bytes = 0
    file_count = 0
    portable_profiles: list[dict[str, Any]] = []
    used_basenames: set[str] = set()

    for profile in store.get("browser_profiles", []):
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        name = profile["name"]
        source_text = str(profile.get("user_data_dir") or "").strip()
        if not source_text:
            skipped.append(f"{name}: Profile 路径为空")
            continue
        source_path = Path(source_text).expanduser()
        if not source_path.exists() or not source_path.is_dir():
            skipped.append(f"{name}: Profile 目录不存在")
            continue
        if _path_is_reparse_point(source_path):
            skipped.append(f"{name}: 跳过符号链接或重解析 Profile 目录")
            continue
        try:
            source_dir = source_path.resolve(strict=True)
        except OSError as exc:
            skipped.append(f"{name}: Profile 目录无法解析 ({exc})")
            continue
        unresolved_absolute = Path(os.path.abspath(source_path))
        if os.path.normcase(str(unresolved_absolute)) != os.path.normcase(str(source_dir)):
            skipped.append(f"{name}: Profile 路径包含符号链接或重解析父目录")
            continue

        files: list[dict[str, Any]] = []
        for path in _iter_portable_browser_paths(source_dir):
            try:
                resolved_path = _resolved_export_path(path, source_dir)
                if resolved_path is None or not resolved_path.is_file():
                    continue
                relative = path.relative_to(source_dir)
                relative_text = _portable_relative_path(relative)
                if _should_skip_browser_relative_path(relative):
                    continue
                size = resolved_path.stat().st_size
                if size > MAX_BROWSER_FILE_BYTES:
                    skipped.append(f"{name}/{relative_text}: 文件过大，已跳过")
                    continue
                if total_bytes + size > MAX_BROWSER_TOTAL_BYTES:
                    skipped.append(f"{name}/{relative_text}: 浏览器数据超过迁移包上限，已跳过")
                    continue
                remaining_total = MAX_BROWSER_TOTAL_BYTES - total_bytes
                read_limit = min(MAX_BROWSER_FILE_BYTES, remaining_total)
                with resolved_path.open("rb") as handle:
                    content = handle.read(read_limit + 1)
                actual_size = len(content)
                if actual_size > MAX_BROWSER_FILE_BYTES:
                    skipped.append(f"{name}/{relative_text}: 读取后文件过大，已跳过")
                    continue
                if total_bytes + actual_size > MAX_BROWSER_TOTAL_BYTES:
                    skipped.append(f"{name}/{relative_text}: 读取后浏览器数据超过迁移包上限，已跳过")
                    continue
                if path.resolve(strict=True) != resolved_path:
                    skipped.append(f"{name}/{relative_text}: 读取期间路径发生变化，已跳过")
                    continue
                total_bytes += actual_size
                file_count += 1
                files.append({
                    "path": relative_text,
                    "size": actual_size,
                    "compression": "zlib",
                    "data": _b64encode(zlib.compress(content, level=6)),
                })
            except OSError as e:
                skipped.append(f"{name}/{path.name}: 无法读取，已跳过 ({e})")

        if files:
            portable_profile = _browser_profile_for_portable(profile, used_basenames)
            portable_profiles.append(portable_profile)
            browser_data[name] = {
                "profile": portable_profile,
                "file_count": len(files),
                "files": files,
            }
        else:
            skipped.append(f"{name}: 没有可迁移的浏览器文件")

    store["browser_profiles"] = portable_profiles
    return browser_data, skipped, file_count, total_bytes


def _validate_portable_export_path(output_path: Path, store: dict[str, Any]) -> None:
    """Keep a migration output from replacing one of its live source files."""
    from config import paths

    resolved_output = Path(output_path).expanduser().resolve(strict=False)
    profile_store = Path(profile_manager.PROFILES_FILE).expanduser().resolve(strict=False)
    protected_profile_files = {
        profile_store,
        profile_store.with_suffix(".backup"),
    }
    if resolved_output in protected_profile_files:
        raise ValueError("迁移包不能覆盖当前 Profile 配置或备份文件")

    secrets_dir = Path(paths.SECRETS_DIR).expanduser().resolve(strict=False)
    if resolved_output == secrets_dir or secrets_dir in resolved_output.parents:
        raise ValueError("迁移包不能保存在密钥存储目录内")

    for profile in store.get("browser_profiles", []):
        if not isinstance(profile, dict):
            continue
        source_text = str(profile.get("user_data_dir") or "").strip()
        if not source_text:
            continue
        source_dir = Path(source_text).expanduser().resolve(strict=False)
        if resolved_output == source_dir or source_dir in resolved_output.parents:
            raise ValueError("迁移包不能保存在正在导出的浏览器 Profile 目录内")


def _clear_unavailable_active_profiles(store: dict[str, Any]) -> None:
    for active_key, list_key in _PORTABLE_ACTIVE_PROFILE_LIST_KEYS.items():
        items = store.get(list_key, [])
        names = {
            item.get("name")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        } if isinstance(items, list) else set()
        if store.get(active_key) not in names:
            store[active_key] = None


def _normalize_portable_profile_selection(
    selection: Mapping[str, Iterable[str]] | None,
) -> dict[str, set[str]] | None:
    if selection is None:
        return None
    if not isinstance(selection, Mapping):
        raise ValueError("Profile 迁移选择格式无效")

    unknown_keys = set(selection) - set(PORTABLE_PROFILE_SELECTION_KEYS)
    if unknown_keys:
        details = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ValueError(f"Profile 迁移选择包含不支持的类型: {details}")

    normalized: dict[str, set[str]] = {}
    for key in PORTABLE_PROFILE_SELECTION_KEYS:
        raw_names = selection.get(key, ())
        if isinstance(raw_names, (str, bytes)) or not isinstance(raw_names, Iterable):
            raise ValueError(f"Profile 迁移选择 {key} 必须是名称列表")
        names: set[str] = set()
        for name in raw_names:
            if not isinstance(name, str) or not name:
                raise ValueError(f"Profile 迁移选择 {key} 包含无效名称")
            names.add(name)
        normalized[key] = names
    return normalized


def _select_portable_profiles(
    store: dict[str, Any],
    selection: Mapping[str, Iterable[str]] | None,
) -> dict[str, Any]:
    normalized = _normalize_portable_profile_selection(selection)
    if normalized is None:
        return store

    selected_store = dict(store)
    for key in PORTABLE_PROFILE_SELECTION_KEYS:
        selected_names = normalized[key]
        items = store.get(key, [])
        selected_store[key] = [
            dict(item)
            for item in items
            if isinstance(item, dict) and item.get("name") in selected_names
        ] if isinstance(items, list) else []
    _clear_unavailable_active_profiles(selected_store)
    return selected_store


def list_portable_profile_options() -> dict[str, list[str]]:
    """Return exportable third-party Profile names grouped by selection key."""
    store = _sanitize_portable_store(profile_manager._load_store())
    return {
        key: [
            item["name"]
            for item in store.get(key, [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        for key in PORTABLE_PROFILE_SELECTION_KEYS
    }


def _sanitize_portable_store(store: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(store, dict):
        store = {}

    def sanitized_profiles(key: str, model_type) -> list[dict[str, Any]]:
        items = store.get(key, [])
        if not isinstance(items, list):
            return []
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
        return cleaned_items

    claude_profiles = [
        profile
        for profile in sanitized_profiles("claude_profiles", profile_manager.ClaudeProfile)
        if str(profile.get("provider") or "anthropic").lower() != "anthropic"
    ]
    codex_profiles = [
        profile
        for profile in sanitized_profiles("codex_profiles", profile_manager.CodexProfile)
        if str(profile.get("model_provider") or "openai").lower() != "openai"
    ]
    stripped = {
        "version": _store_version(store),
        "claude_profiles": claude_profiles,
        "codex_profiles": codex_profiles,
        "claude_account_profiles": [],
        "codex_account_profiles": [],
        "ssh_profiles": sanitized_profiles("ssh_profiles", profile_manager.SSHProfile),
        "browser_profiles": sanitized_profiles("browser_profiles", profile_manager.BrowserProfile),
        "active_claude_profile": store.get("active_claude_profile"),
        "active_codex_profile": store.get("active_codex_profile"),
        "active_claude_account": None,
        "active_codex_account": None,
        "active_ssh_profile": store.get("active_ssh_profile"),
        "active_browser_profile": store.get("active_browser_profile"),
    }
    _clear_unavailable_active_profiles(stripped)
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


def _remove_browser_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _move_browser_path(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        _remove_browser_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


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


def _rollback_browser_restores(snapshots: list[_BrowserRestoreSnapshot]) -> list[str]:
    errors: list[str] = []
    for snapshot in reversed(snapshots):
        try:
            if snapshot.target.exists() or snapshot.target.is_symlink():
                _remove_browser_path(snapshot.target)
            if snapshot.had_original:
                if not (snapshot.backup.exists() or snapshot.backup.is_symlink()):
                    raise FileNotFoundError(f"浏览器回滚备份不存在: {snapshot.backup}")
                _move_browser_path(snapshot.backup, snapshot.target)
            elif snapshot.backup.exists() or snapshot.backup.is_symlink():
                _remove_browser_path(snapshot.backup)
        except Exception as exc:
            errors.append(f"浏览器目录 {snapshot.target}: {exc}")
    return errors


def _commit_browser_restores(snapshots: list[_BrowserRestoreSnapshot]) -> list[str]:
    errors: list[str] = []
    for snapshot in snapshots:
        try:
            if snapshot.backup.exists() or snapshot.backup.is_symlink():
                _remove_browser_path(snapshot.backup)
        except Exception as exc:
            errors.append(f"{snapshot.target.name}: 导入事务备份清理失败 ({exc})")
    return errors


def _rollback_portable_import(
    profile_snapshot: _FileSnapshot,
    secret_snapshot: dict[str, str | None],
    browser_snapshots: list[_BrowserRestoreSnapshot],
) -> list[str]:
    errors = _rollback_browser_restores(browser_snapshots)
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
    return errors


def _decode_browser_file_entry(
    name: str,
    file_entry: Any,
    remaining_total: int,
) -> tuple[Path, bytes]:
    if not isinstance(file_entry, dict):
        raise ValueError("异常文件条目")
    rel_path = _safe_browser_relative_path(str(file_entry.get("path") or ""))
    declared_size = file_entry.get("size")
    if declared_size is None:
        raise ValueError(f"{rel_path}: 文件大小元数据缺失")
    if not isinstance(declared_size, int) or isinstance(declared_size, bool):
        raise ValueError(f"{rel_path}: 文件大小元数据异常")
    declared_size_int = declared_size
    if declared_size_int < 0:
        raise ValueError(f"{rel_path}: 文件大小元数据异常")
    if declared_size_int > MAX_BROWSER_FILE_BYTES:
        raise ValueError(f"{rel_path}: 文件过大")
    if declared_size_int > remaining_total:
        raise ValueError(f"{rel_path}: 浏览器数据超过导入上限")

    encoded = file_entry.get("data")
    if not isinstance(encoded, str):
        raise ValueError(f"{rel_path}: 文件数据缺失")
    compression = file_entry.get("compression")
    if compression == "zlib":
        max_raw_size = _zlib_compress_bound(declared_size_int)
    elif compression in {None, "none"}:
        max_raw_size = declared_size_int
    else:
        raise ValueError(f"{rel_path}: 不支持的文件压缩方式")
    if len(encoded) > _base64_encoded_length(max_raw_size):
        raise ValueError(f"{rel_path}: 编码数据超过声明大小边界")
    try:
        raw = _b64decode(encoded)
        if len(raw) > max_raw_size:
            raise ValueError("编码数据超过声明大小边界")
        if compression == "zlib":
            content = _zlib_decompress_bounded(
                raw,
                declared_size_int,
                too_large_message="解压后内容超过声明大小",
                corrupt_message="压缩数据损坏",
            )
        else:
            content = raw
    except Exception as exc:
        raise ValueError(f"{rel_path}: 解码失败 ({exc})") from exc
    if declared_size_int != len(content):
        raise ValueError(f"{rel_path}: 文件大小校验失败")
    return rel_path, content


def _restore_browser_data(
    browser_data: dict[str, Any],
    *,
    transaction_snapshots: list[_BrowserRestoreSnapshot] | None = None,
) -> tuple[int, int, list[str], set[str]]:
    restored_files = 0
    restored_bytes = 0
    skipped: list[str] = []
    restored_profiles: set[str] = set()

    if not isinstance(browser_data, dict):
        return restored_files, restored_bytes, ["浏览器数据格式异常，已跳过"], restored_profiles

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
        profile_name = str(profile.get("name") or name)
        lexical_managed_root = Path(os.path.abspath(managed_root))
        lexical_target = Path(os.path.abspath(target))
        try:
            lexical_target.relative_to(lexical_managed_root)
        except ValueError:
            skipped.append(f"{name}: 目标目录不在托管目录内，已跳过")
            continue
        if _path_is_reparse_point(lexical_target):
            skipped.append(f"{name}: 目标目录是符号链接或重解析点，已跳过")
            continue
        try:
            target = target.resolve()
        except OSError:
            target = target.absolute()
        if os.path.normcase(str(lexical_target)) != os.path.normcase(str(target)):
            skipped.append(f"{name}: 目标目录包含符号链接或重解析父目录，已跳过")
            continue
        if target == managed_root or managed_root not in target.parents:
            skipped.append(f"{name}: 目标目录不在托管目录内，已跳过")
            continue

        staging = temp_path_for(target).with_suffix(".import_staging")
        backup = temp_path_for(target).with_suffix(".import_backup")
        had_original = target.exists() or target.is_symlink()
        original_moved = False
        install_started = False
        profile_file_count = 0
        profile_bytes = 0
        try:
            if staging.exists() or staging.is_symlink():
                raise FileExistsError(f"临时浏览器 staging 路径已存在: {staging}")
            if backup.exists() or backup.is_symlink():
                raise FileExistsError(f"临时浏览器备份路径已存在: {backup}")
            files = item.get("files", [])
            if not isinstance(files, list):
                raise ValueError("浏览器文件列表格式异常")
            if not files:
                raise ValueError("没有可恢复的浏览器文件")
            declared_file_count = item.get("file_count")
            if declared_file_count is not None:
                if not isinstance(declared_file_count, int) or isinstance(declared_file_count, bool):
                    raise ValueError("浏览器文件数量元数据异常")
                if declared_file_count != len(files):
                    raise ValueError("浏览器文件数量与声明不一致")

            staging.mkdir(parents=True)
            seen_paths: set[str] = set()
            for file_entry in files:
                remaining_total = MAX_BROWSER_TOTAL_BYTES - restored_bytes - profile_bytes
                rel_path, content = _decode_browser_file_entry(name, file_entry, remaining_total)
                path_key = rel_path.as_posix().casefold()
                if path_key in seen_paths:
                    raise ValueError(f"{rel_path}: 重复的浏览器文件路径")
                seen_paths.add(path_key)
                destination = staging / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(destination, content)
                profile_file_count += 1
                profile_bytes += len(content)

            if had_original:
                _move_browser_path(target, backup)
                original_moved = True
            install_started = True
            _move_browser_path(staging, target)

            snapshot = _BrowserRestoreSnapshot(
                target=target,
                backup=backup,
                had_original=had_original,
            )
            if transaction_snapshots is None:
                skipped.extend(_commit_browser_restores([snapshot]))
            else:
                transaction_snapshots.append(snapshot)
            restored_files += profile_file_count
            restored_bytes += profile_bytes
            restored_profiles.add(profile_name)
        except Exception as exc:
            skipped.append(f"{name}: 恢复失败 ({exc})")
            if staging.exists() or staging.is_symlink():
                try:
                    _remove_browser_path(staging)
                except Exception as cleanup_error:
                    skipped.append(f"{name}: 清理 staging 失败 ({cleanup_error})")

            cleanup_error: Exception | None = None
            if install_started and (target.exists() or target.is_symlink()):
                try:
                    _remove_browser_path(target)
                except Exception as remove_error:
                    cleanup_error = remove_error
                    skipped.append(f"{name}: 清理失败目录失败 ({remove_error})")
            if original_moved:
                try:
                    if not (backup.exists() or backup.is_symlink()):
                        raise FileNotFoundError(f"浏览器回滚备份不存在: {backup}")
                    _move_browser_path(backup, target)
                    cleanup_error = None
                except Exception as restore_error:
                    skipped.append(f"{name}: 原目录恢复失败 ({restore_error})")
                    raise RuntimeError(
                        f"{name}: 浏览器数据恢复失败，且原目录自动恢复失败；保留备份: {backup}"
                    ) from exc
            elif cleanup_error is not None:
                raise RuntimeError(
                    f"{name}: 浏览器数据恢复失败，且失败目录清理不完整: {cleanup_error}"
                ) from exc

    return restored_files, restored_bytes, skipped, restored_profiles


def _prepare_imported_browser_data(browser_data: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(browser_data, dict):
        return {}, []

    prepared: dict[str, Any] = {}
    profiles: list[dict[str, Any]] = []
    used_basenames: set[str] = set()
    for name, item in browser_data.items():
        if not isinstance(item, dict):
            continue
        profile = item.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        portable_profile = _browser_profile_for_portable(profile, used_basenames)
        prepared[name] = dict(item)
        prepared[name]["profile"] = portable_profile
        profiles.append(portable_profile)
    return prepared, profiles


def _validate_portable_import_path(input_path: Path, browser_data: dict[str, Any]) -> None:
    """Do not let replacing a browser target move or delete the input bundle."""
    resolved_input = Path(input_path).expanduser().resolve(strict=False)
    for item in browser_data.values():
        if not isinstance(item, dict):
            continue
        profile = item.get("profile")
        if not isinstance(profile, dict):
            continue
        target_text = str(profile.get("user_data_dir") or "").strip()
        if not target_text:
            continue
        target = Path(target_text).expanduser().resolve(strict=False)
        if resolved_input == target or target in resolved_input.parents:
            raise ValueError("迁移包不能位于待替换的浏览器 Profile 目录内")


def export_portable_profiles(
    output_path: str | Path,
    password: str,
    selection: Mapping[str, Iterable[str]] | None = None,
) -> PortableExportResult:
    """Export selected profiles and their secrets, or all when selection is None.

    Selection keys are the names in ``PORTABLE_PROFILE_SELECTION_KEYS``. An
    omitted key selects no profiles of that type; passing ``None`` preserves
    the legacy full-export behavior.
    """
    if len(password) < 8:
        raise ValueError("迁移密码至少需要 8 个字符")

    path = Path(output_path).expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError("导出路径不能是目录")

    # Keep the profile snapshot, referenced-secret reads, and package write in
    # one store transaction.  The profile manager uses an RLock, so its
    # internal _load_store() lock remains safe to acquire here.
    with profile_manager._STORE_CACHE_LOCK:
        return _export_portable_profiles_locked(path, password, selection)


def _export_portable_profiles_locked(
    path: Path,
    password: str,
    selection: Mapping[str, Iterable[str]] | None,
) -> PortableExportResult:
    full_store = _sanitize_portable_store(profile_manager._load_store())
    store = _select_portable_profiles(full_store, selection)
    if selection is not None and _count_profiles(store) == 0:
        raise ValueError("选择中没有可导出的 Profile")
    # Protect every live browser source even when that profile was not chosen.
    _validate_portable_export_path(path, full_store)
    browser_data, skipped_browser_files, browser_file_count, browser_bytes = _collect_browser_profile_data(store)
    _clear_unavailable_active_profiles(store)
    if selection is not None and _count_profiles(store) == 0:
        raise ValueError("所选 Profile 没有可迁移的数据")
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

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(bundle, ensure_ascii=False, indent=2))

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
    _validate_portable_import_path(path, browser_data)
    imported_store["browser_profiles"] = browser_profiles
    imported_store = _sanitize_portable_store(imported_store)

    secrets = payload.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("迁移包中的密钥数据无效")

    # Hold the same RLock used by the profile store from the existing-store
    # read until the transaction is committed or completely rolled back.
    with profile_manager._STORE_CACHE_LOCK:
        return _import_portable_profiles_locked(
            imported_store,
            browser_data,
            browser_profiles,
            secrets,
            payload.get("skipped_browser_files", []),
        )


def _import_portable_profiles_locked(
    imported_store: dict[str, Any],
    browser_data: dict[str, Any],
    browser_profiles: list[dict[str, Any]],
    secrets: dict[str, Any],
    source_browser_skips: Any,
) -> PortableImportResult:
    allowed_secret_refs = _collect_secret_refs_from_store(imported_store)
    existing_store = profile_manager._load_store()
    conflicting_refs = sorted(
        allowed_secret_refs
        & _collect_unreplaced_profile_secret_refs(existing_store, imported_store)
    )
    if conflicting_refs:
        details = ", ".join(conflicting_refs[:3])
        suffix = f" 等 {len(conflicting_refs)} 项" if len(conflicting_refs) > 3 else ""
        raise ValueError(
            f"迁移包的密钥引用与未替换的现有配置冲突: {details}{suffix}"
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

    profile_snapshot = _snapshot_file(profile_manager.PROFILES_FILE)
    secret_snapshot = {
        ref: security.get_secret(ref)
        for ref, _value in sorted(valid_secrets)
    }
    browser_snapshots: list[_BrowserRestoreSnapshot] = []
    browser_file_count = 0
    browser_bytes = 0
    skipped_browser_files: list[str] = []
    restored_browser_names: set[str] = set()
    restored = 0
    try:
        (
            browser_file_count,
            browser_bytes,
            skipped_browser_files,
            restored_browser_names,
        ) = _restore_browser_data(
            browser_data,
            transaction_snapshots=browser_snapshots,
        )
        if isinstance(source_browser_skips, list):
            skipped_browser_files.extend(
                item for item in source_browser_skips
                if isinstance(item, str)
            )

        if browser_profiles:
            imported_store["browser_profiles"] = [
                profile for profile in imported_store.get("browser_profiles", [])
                if isinstance(profile, dict) and profile.get("name") in restored_browser_names
            ]
            if imported_store.get("active_browser_profile") not in restored_browser_names:
                imported_store["active_browser_profile"] = None

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
                names = {
                    item.get("name")
                    for item in new_store.get(list_key, [])
                    if isinstance(item, dict)
                }
                if imported_active in names:
                    new_store[active_key] = imported_active

        new_store["version"] = max(_store_version(existing_store), _store_version(imported_store))

        for ref, value in valid_secrets:
            try:
                security.set_secret(ref, value)
            except Exception as e:
                skipped.append(f"{ref} ({e})")
                continue
            restored += 1

        profile_manager._normalize_store(new_store)
        profile_manager._save_store(new_store)
    except Exception as import_error:
        rollback_errors = _rollback_portable_import(
            profile_snapshot,
            secret_snapshot,
            browser_snapshots,
        )
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise RuntimeError(f"迁移包导入失败，且自动回滚不完整: {details}") from import_error
        raise

    skipped_browser_files.extend(_commit_browser_restores(browser_snapshots))

    return PortableImportResult(
        profile_count=_count_profiles(imported_store),
        secret_count=restored,
        skipped_secret_refs=skipped,
        browser_file_count=browser_file_count,
        browser_bytes=browser_bytes,
        skipped_browser_files=skipped_browser_files,
    )
