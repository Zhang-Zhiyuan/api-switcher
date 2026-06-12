import copy
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from config.paths import (
    CLAUDE_SETTINGS,
    CLAUDE_CONFIG,
    CLAUDE_CREDENTIALS,
    CODEX_CONFIG,
    CODEX_AUTH,
    VSCODE_SETTINGS,
    BACKUPS_DIR,
)
from core.atomic_io import atomic_write_bytes, atomic_write_text
from models.profile import BackupEntry

logger = logging.getLogger(__name__)
_BACKUP_LIST_CACHE_LOCK = threading.RLock()
_BACKUP_LIST_CACHE: list[BackupEntry] | None = None
_BACKUP_LIST_CACHE_SIGNATURE: tuple | None = None

BACKUP_FILES = {
    "claude_settings.json": CLAUDE_SETTINGS,
    "claude_config.json": CLAUDE_CONFIG,
    "claude_credentials.json": CLAUDE_CREDENTIALS,
    "codex_config.toml": CODEX_CONFIG,
    "codex_auth.json": CODEX_AUTH,
    "vscode_settings.json": VSCODE_SETTINGS,
}

BACKUP_META_FILE = "backup_meta.json"


def clear_backup_list_cache() -> None:
    """Clear cached backup listing."""
    global _BACKUP_LIST_CACHE, _BACKUP_LIST_CACHE_SIGNATURE
    with _BACKUP_LIST_CACHE_LOCK:
        _BACKUP_LIST_CACHE = None
        _BACKUP_LIST_CACHE_SIGNATURE = None


def _clone_backup_entries(entries: list[BackupEntry]) -> list[BackupEntry]:
    return copy.deepcopy(entries)


def _backup_list_signature() -> tuple:
    root_key = str(BACKUPS_DIR.resolve(strict=False))
    if not BACKUPS_DIR.exists():
        return (root_key, None)
    try:
        root_stat = BACKUPS_DIR.stat()
        children = []
        for child in BACKUPS_DIR.iterdir():
            try:
                child_stat = child.stat()
            except OSError:
                continue
            if not child.is_dir():
                continue
            meta_path = child / BACKUP_META_FILE
            try:
                meta_stat = meta_path.stat()
                meta_signature = (int(meta_stat.st_mtime_ns), int(meta_stat.st_size))
            except OSError:
                meta_signature = None
            children.append((
                child.name,
                int(child_stat.st_mtime_ns),
                int(child_stat.st_size),
                meta_signature,
            ))
        children.sort()
        return (root_key, int(root_stat.st_mtime_ns), int(root_stat.st_size), tuple(children))
    except OSError:
        return (root_key, None)


def _cache_backup_list(entries: list[BackupEntry], signature: tuple | None = None) -> None:
    global _BACKUP_LIST_CACHE, _BACKUP_LIST_CACHE_SIGNATURE
    _BACKUP_LIST_CACHE = _clone_backup_entries(entries)
    _BACKUP_LIST_CACHE_SIGNATURE = signature or _backup_list_signature()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_managed_backup_dir(directory: Path | str) -> Path:
    backup_root = BACKUPS_DIR.resolve()
    backup_dir = Path(directory).resolve()
    if backup_dir == backup_root or not _is_relative_to(backup_dir, backup_root):
        raise ValueError("备份目录不在受管备份目录内")
    if not backup_dir.is_dir():
        raise ValueError("备份目录不存在或不可访问")
    return backup_dir


def _allocate_backup_dir(timestamp: str) -> Path:
    """Create and return a unique backup directory for the timestamp."""
    base = BACKUPS_DIR / timestamp
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        base.mkdir(parents=True, exist_ok=False)
        return base
    except FileExistsError:
        pass

    for index in range(2, 1000):
        candidate = BACKUPS_DIR / f"{timestamp}-{index:02d}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue

    raise RuntimeError("无法创建唯一备份目录，请稍后重试")


def create_backup(description: str = "") -> BackupEntry:
    """Create a backup of all config files. Returns the backup entry."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_dir = _allocate_backup_dir(ts)

    backed_up = []
    for name, src in BACKUP_FILES.items():
        if src.exists():
            shutil.copy2(src, backup_dir / name)
            backed_up.append(name)
            logger.debug(f"Backed up {src} -> {backup_dir / name}")

    entry = BackupEntry(
        timestamp=datetime.now().isoformat(),
        directory=backup_dir,
        description=description,
        files=backed_up,
    )

    # Save metadata
    meta_path = backup_dir / BACKUP_META_FILE
    atomic_write_text(meta_path, json.dumps(entry.to_dict(), indent=2, ensure_ascii=False))
    clear_backup_list_cache()

    return entry


def list_backups() -> list[BackupEntry]:
    """List all backups, most recent first."""
    signature = _backup_list_signature()
    with _BACKUP_LIST_CACHE_LOCK:
        if _BACKUP_LIST_CACHE is not None and _BACKUP_LIST_CACHE_SIGNATURE == signature:
            return _clone_backup_entries(_BACKUP_LIST_CACHE)

        if not BACKUPS_DIR.exists():
            _cache_backup_list([], signature)
            return []

        backups = []
        for d in sorted(BACKUPS_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta_path = d / BACKUP_META_FILE
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    entry = BackupEntry.from_dict(data)
                    # Trust the directory we scanned, not editable metadata inside it.
                    entry.directory = d
                    backups.append(entry)
                except Exception as e:
                    logger.warning(f"Failed to read backup meta in {d}: {e}")
            else:
                # Legacy backup without meta
                files = [f.name for f in d.iterdir() if f.is_file()]
                backups.append(BackupEntry(
                    timestamp=d.name,
                    directory=d,
                    description="(legacy backup)",
                    files=files,
                ))

        _cache_backup_list(backups, signature)
        return _clone_backup_entries(backups)


def get_latest_backup() -> BackupEntry | None:
    """Return the most recent backup entry, if any."""
    backups = list_backups()
    return backups[0] if backups else None


def restore_backup(entry: BackupEntry) -> list[str]:
    """Restore config files from a backup. Returns list of restored file names."""
    backup_dir = _resolve_managed_backup_dir(entry.directory)

    # Create a safety backup first
    create_backup("回滚前自动备份")

    restored = []

    for name, dst in BACKUP_FILES.items():
        src = backup_dir / name
        if src.exists():
            atomic_write_bytes(dst, src.read_bytes())
            restored.append(name)
            logger.info(f"Restored {src} -> {dst}")

    return restored


def restore_latest_backup() -> tuple[BackupEntry, list[str]]:
    """Restore the most recent backup and return the entry plus restored files."""
    entry = get_latest_backup()
    if not entry:
        raise ValueError("暂无可回滚的备份")
    return entry, restore_backup(entry)


def prune_backups(keep_count: int = 20) -> int:
    """Remove old backups, keeping the most recent ones. Returns count removed."""
    backups = list_backups()
    if len(backups) <= keep_count:
        return 0

    to_remove = backups[keep_count:]
    removed = 0
    backup_root = BACKUPS_DIR.resolve()
    for entry in to_remove:
        try:
            target = Path(entry.directory).resolve()
            if target == backup_root or not _is_relative_to(target, backup_root):
                logger.warning(f"Skipped suspicious backup directory: {entry.directory}")
                continue
            shutil.rmtree(target)
            removed += 1
        except Exception as e:
            logger.warning(f"Failed to remove backup {entry.directory}: {e}")

    if removed:
        clear_backup_list_cache()
    return removed
