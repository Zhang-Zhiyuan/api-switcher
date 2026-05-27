from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


APP_NAME = "API切换器"
ENV_DATA_DIR = "API_SWITCHER_DATA_DIR"
ENV_PORTABLE = "API_SWITCHER_PORTABLE"
DATA_DIR_POINTER_FILE = "data_dir.txt"
PORTABLE_MARKER_FILE = "portable.flag"
PORTABLE_DATA_DIR_NAME = "data"
_STORAGE_DIR_SOURCE = "default"
_STORAGE_DIR_WARNINGS: list[str] = []


def _get_app_dir() -> Path:
    """Return the directory that contains the app code or frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _expand_configured_path(value: str, base_dir: Path | None = None) -> Path:
    stripped = value.strip()
    if not stripped:
        raise ValueError("数据目录不能为空")
    path = Path(os.path.expandvars(os.path.expanduser(stripped)))
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def _read_data_dir_pointer() -> Path | None:
    for pointer, base_dir in [(DATA_DIR_POINTER, APP_DIR), (USER_DATA_DIR_POINTER, None)]:
        if not pointer.exists() or not pointer.is_file():
            continue
        try:
            value = pointer.read_text(encoding="utf-8").strip()
        except OSError as e:
            _STORAGE_DIR_WARNINGS.append(f"无法读取数据目录指针 {pointer}: {e}")
            continue
        if not value:
            continue
        try:
            return _expand_configured_path(value, base_dir)
        except OSError as e:
            _STORAGE_DIR_WARNINGS.append(f"数据目录指针无效 {pointer}: {e}")
            continue
    return None


def _portable_requested() -> bool:
    value = os.environ.get(ENV_PORTABLE, "")
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return PORTABLE_MARKER.exists()


def _platform_default_storage_dirs() -> list[tuple[Path, str]]:
    if os.name == "nt":
        candidates: list[tuple[Path, str]] = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append((Path(appdata) / APP_NAME, "%APPDATA%"))
        candidates.append((Path.home() / "AppData" / "Roaming" / APP_NAME, "home-roaming"))
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append((Path(local_appdata) / APP_NAME, "%LOCALAPPDATA%"))
        candidates.append((Path.home() / "AppData" / "Local" / APP_NAME, "home-local"))
        return candidates

    if sys.platform == "darwin":
        return [(Path.home() / "Library" / "Application Support" / APP_NAME, "application-support")]

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return [(Path(base) / "api-switcher", "xdg-config")]
    return [(Path.home() / ".config" / "api-switcher", "home-config")]


def _get_user_data_dir_pointer() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME / DATA_DIR_POINTER_FILE
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / DATA_DIR_POINTER_FILE
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "api-switcher" / DATA_DIR_POINTER_FILE


def _can_use_storage_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_path = path / f".write_test_{os.getpid()}_{uuid.uuid4().hex}.tmp"
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink(missing_ok=True)
        return True, ""
    except OSError as e:
        return False, str(e)


def _select_storage_dir() -> tuple[Path, str]:
    """Pick the best writable storage directory."""
    candidates: list[tuple[Path, str]] = []

    override = os.environ.get(ENV_DATA_DIR)
    if override:
        try:
            candidates.append((_expand_configured_path(override, APP_DIR), ENV_DATA_DIR))
        except OSError as e:
            _STORAGE_DIR_WARNINGS.append(f"{ENV_DATA_DIR} 无效: {e}")

    pointer = _read_data_dir_pointer()
    if pointer is not None:
        candidates.append((pointer, DATA_DIR_POINTER_FILE))

    if _portable_requested():
        candidates.append((APP_DIR / PORTABLE_DATA_DIR_NAME, "portable"))

    candidates.extend(_platform_default_storage_dirs())
    candidates.append((Path(tempfile.gettempdir()) / "api-switcher", "temp-fallback"))

    seen: set[str] = set()
    for candidate, source in candidates:
        key = str(candidate).lower() if os.name == "nt" else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        ok, error = _can_use_storage_dir(candidate)
        if ok:
            return candidate, source
        _STORAGE_DIR_WARNINGS.append(f"数据目录不可写，已跳过 {candidate}: {error}")

    # This should be unreachable because the temp fallback should normally work.
    fallback = Path.cwd() / "api-switcher-data"
    return fallback, "cwd-fallback"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _is_path_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _copy_missing(source: Path, target: Path, copied: list[str], root: Path) -> None:
    """Copy legacy storage content without overwriting existing user data."""
    if source.is_symlink():
        return

    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        try:
            children = list(source.iterdir())
        except OSError:
            return
        for child in children:
            try:
                _copy_missing(child, target / child.name, copied, root)
            except OSError:
                continue
        return

    if source.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(source.relative_to(root)))


def migrate_legacy_storage() -> list[str]:
    """Move old project-local storage into the stable user data directory.

    Existing files in the new directory are preserved. The old directory is kept
    in place as a safety backup, which matters for large browser profiles.
    """
    copied: list[str] = []
    for legacy_storage_dir in LEGACY_STORAGE_DIRS:
        if _same_path(legacy_storage_dir, STORAGE_DIR) or not legacy_storage_dir.exists():
            continue

        for name in [
            "profiles.json",
            "profiles.backup",
            "usage_stats.json",
            "daily_stats.json",
            "backups",
            "browser_profiles",
            "secrets",
            "logs",
        ]:
            source = legacy_storage_dir / name
            if source.exists():
                try:
                    _copy_missing(source, STORAGE_DIR / name, copied, legacy_storage_dir)
                except OSError:
                    # Best-effort migration: a locked browser DB or protected file
                    # should not prevent the app from starting.
                    continue
    return copied


def ensure_storage_dirs(migrate_legacy: bool = True) -> list[str]:
    """Create all app-owned data directories and optionally migrate old data."""
    migrated = migrate_legacy_storage() if migrate_legacy else []
    for directory in [
        STORAGE_DIR,
        BACKUPS_DIR,
        SECRETS_DIR,
        STORAGE_DIR / "logs",
        STORAGE_DIR / "browser_profiles",
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    return migrated


def copy_storage_to(target_dir: str | Path, overwrite: bool = False) -> list[str]:
    """Copy current app-owned data to another directory."""
    target = _expand_configured_path(str(target_dir))
    if _same_path(target, STORAGE_DIR):
        return []
    if _is_path_inside(target, STORAGE_DIR):
        raise ValueError("目标目录不能位于当前数据目录内部，请选择一个独立目录")
    if _is_path_inside(STORAGE_DIR, target):
        raise ValueError("目标目录不能是当前数据目录的上级目录，请选择一个独立目录")

    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    if overwrite:
        for child in (STORAGE_DIR.iterdir() if STORAGE_DIR.exists() else []):
            target_child = target / child.name
            if target_child.exists() or target_child.is_symlink():
                _remove_path(target_child)
            _copy_missing(child, target_child, copied, STORAGE_DIR)
    elif STORAGE_DIR.exists():
        _copy_missing(STORAGE_DIR, target, copied, STORAGE_DIR)
    return copied


def write_data_dir_pointer(target_dir: str | Path, copy_current: bool = True) -> list[str]:
    """Persist a custom data directory next to the app. Takes effect after restart."""
    target = _expand_configured_path(str(target_dir))
    copied = copy_storage_to(target) if copy_current else []
    try:
        _atomic_write_text(DATA_DIR_POINTER, str(target))
        if USER_DATA_DIR_POINTER.exists():
            USER_DATA_DIR_POINTER.unlink()
    except OSError:
        USER_DATA_DIR_POINTER.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(USER_DATA_DIR_POINTER, str(target))
    return copied


def clear_data_dir_pointer() -> bool:
    changed = False
    if DATA_DIR_POINTER.exists():
        DATA_DIR_POINTER.unlink()
        changed = True
    if USER_DATA_DIR_POINTER.exists():
        USER_DATA_DIR_POINTER.unlink()
        changed = True
    return changed


def enable_portable_storage(copy_current: bool = True) -> list[str]:
    """Use APP_DIR/data as storage after restart."""
    target = APP_DIR / PORTABLE_DATA_DIR_NAME
    copied = copy_storage_to(target) if copy_current else []
    _atomic_write_text(
        PORTABLE_MARKER,
        "API_SWITCHER_PORTABLE=1\nData is stored in the sibling data directory.\n",
    )
    return copied


def disable_portable_storage() -> bool:
    changed = False
    if PORTABLE_MARKER.exists():
        PORTABLE_MARKER.unlink()
        changed = True
    changed = clear_data_dir_pointer() or changed
    return changed


def get_storage_info() -> dict:
    ok, error = _can_use_storage_dir(STORAGE_DIR)
    return {
        "app_dir": APP_DIR,
        "storage_dir": STORAGE_DIR,
        "legacy_storage_dirs": LEGACY_STORAGE_DIRS,
        "source": STORAGE_DIR_SOURCE,
        "env_var": ENV_DATA_DIR,
        "env_override": os.environ.get(ENV_DATA_DIR),
        "portable_env_var": ENV_PORTABLE,
        "portable": _portable_requested(),
        "data_dir_pointer": DATA_DIR_POINTER,
        "data_dir_pointer_exists": DATA_DIR_POINTER.exists(),
        "user_data_dir_pointer": USER_DATA_DIR_POINTER,
        "user_data_dir_pointer_exists": USER_DATA_DIR_POINTER.exists(),
        "portable_marker": PORTABLE_MARKER,
        "portable_marker_exists": PORTABLE_MARKER.exists(),
        "writable": ok,
        "write_error": error,
        "warnings": list(STORAGE_DIR_WARNINGS),
    }

# Target config files
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
CLAUDE_SETTINGS = CLAUDE_HOME / "settings.json"
CLAUDE_CONFIG = CLAUDE_HOME / "config.json"
CLAUDE_CREDENTIALS = CLAUDE_HOME / ".credentials.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CODEX_CONFIG = CODEX_HOME / "config.toml"
CODEX_AUTH = CODEX_HOME / "auth.json"
VSCODE_SETTINGS = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json"

# Local storage
APP_DIR = _get_app_dir()
DATA_DIR_POINTER = APP_DIR / DATA_DIR_POINTER_FILE
USER_DATA_DIR_POINTER = _get_user_data_dir_pointer()
PORTABLE_MARKER = APP_DIR / PORTABLE_MARKER_FILE
LEGACY_STORAGE_DIR = APP_DIR / "storage"
LEGACY_STORAGE_DIRS = [
    path for path in [
        LEGACY_STORAGE_DIR,
        APP_DIR.parent / "storage" if getattr(sys, "frozen", False) else None,
    ]
    if path is not None
]
STORAGE_DIR, STORAGE_DIR_SOURCE = _select_storage_dir()
STORAGE_DIR_WARNINGS = _STORAGE_DIR_WARNINGS
PROFILES_FILE = STORAGE_DIR / "profiles.json"
BACKUPS_DIR = STORAGE_DIR / "backups"
SECRETS_DIR = STORAGE_DIR / "secrets"

# Keyring service name
KEYRING_SERVICE = "api-switcher"
