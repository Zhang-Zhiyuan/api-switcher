import copy
import json
import logging
import shutil
import threading
import uuid
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
BACKUP_FORMAT_VERSION = 2


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
            if child.name.startswith(".pending-"):
                continue
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
    if backup_dir.name.startswith(".pending-"):
        raise ValueError("备份尚未创建完成")
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
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    staging_dir = BACKUPS_DIR / f".pending-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=False, exist_ok=False)

    try:
        backed_up = []
        for name, src in BACKUP_FILES.items():
            if src.exists():
                shutil.copy2(src, staging_dir / name)
                backed_up.append(name)
                logger.debug(f"Backed up {src} -> {staging_dir / name}")

        entry = BackupEntry(
            timestamp=datetime.now().isoformat(),
            directory=staging_dir,
            description=description,
            files=backed_up,
        )

        # A backup becomes visible only after every file and its metadata have
        # been written.  This prevents a failed copy from being mistaken for a
        # valid metadata-less legacy backup.
        for index in range(1, 1000):
            suffix = "" if index == 1 else f"-{index:02d}"
            backup_dir = BACKUPS_DIR / f"{ts}{suffix}"
            if backup_dir.exists():
                continue

            entry.directory = backup_dir
            metadata = entry.to_dict()
            metadata["backup_format_version"] = BACKUP_FORMAT_VERSION
            # Record every file managed by this application, including files
            # absent at snapshot time. Restore uses this only for new-format
            # backups so upgrades cannot delete newer file types.
            metadata["managed_files"] = sorted(BACKUP_FILES)
            atomic_write_text(
                staging_dir / BACKUP_META_FILE,
                json.dumps(metadata, indent=2, ensure_ascii=False),
            )
            try:
                staging_dir.rename(backup_dir)
                break
            except OSError:
                # Another process may have committed the same timestamp after
                # our existence check. Retry only when that candidate exists.
                if backup_dir.exists():
                    continue
                raise
        else:
            raise RuntimeError("无法创建唯一备份目录，请稍后重试")
    except Exception:
        try:
            shutil.rmtree(staging_dir)
        except OSError as cleanup_error:
            # Pending directories are deliberately ignored by listing/pruning,
            # so even an undeletable partial copy can never be restored.
            logger.warning(f"Failed to clean incomplete backup {staging_dir}: {cleanup_error}")
        raise

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
            if not d.is_dir() or d.name.startswith(".pending-"):
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


def _snapshot_file_state(backup_dir: Path) -> tuple[set[str] | None, set[str] | None]:
    """Return (managed files, existing files), or (None, None) for legacy."""
    meta_path = backup_dir / BACKUP_META_FILE
    if not meta_path.exists():
        return None, None
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if int(metadata.get("backup_format_version", 0)) < BACKUP_FORMAT_VERSION:
            return None, None
        managed_files = metadata.get("managed_files")
        existing_files = metadata.get("files")
        if not isinstance(managed_files, list) or not isinstance(existing_files, list):
            raise ValueError("备份元数据缺少文件状态")
        managed = {str(name) for name in managed_files if str(name).strip()}
        existing = {str(name) for name in existing_files if str(name).strip()}
        if not existing.issubset(managed):
            raise ValueError("备份元数据的文件状态不一致")
        return managed, existing
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"备份元数据损坏: {exc}") from exc


def _capture_target_state() -> dict[str, tuple[bool, bytes]]:
    state: dict[str, tuple[bool, bytes]] = {}
    for name, path in BACKUP_FILES.items():
        if not path.exists():
            state[name] = (False, b"")
            continue
        if not path.is_file():
            raise ValueError(f"配置目标不是文件，无法回滚: {path}")
        state[name] = (True, path.read_bytes())
    return state


def _restore_target_state(state: dict[str, tuple[bool, bytes]]) -> list[str]:
    errors: list[str] = []
    for name, path in BACKUP_FILES.items():
        existed, content = state[name]
        try:
            if existed:
                atomic_write_bytes(path, content)
            elif path.exists():
                if not path.is_file():
                    raise ValueError(f"目标不是文件: {path}")
                path.unlink()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


def restore_backup(entry: BackupEntry) -> list[str]:
    """Restore the exact managed-file state captured by a backup."""
    backup_dir = _resolve_managed_backup_dir(entry.directory)
    snapshot_managed_files, snapshot_existing_files = _snapshot_file_state(backup_dir)

    declared_files = snapshot_existing_files if snapshot_existing_files is not None else set(entry.files)
    missing_files = [
        name
        for name in BACKUP_FILES
        if name in declared_files and not (backup_dir / name).is_file()
    ]
    if missing_files:
        raise ValueError(f"备份文件缺失或损坏: {', '.join(missing_files)}")

    target_state = _capture_target_state()

    # Create a safety backup first
    create_backup("回滚前自动备份")

    restored = []
    try:
        for name, dst in BACKUP_FILES.items():
            src = backup_dir / name
            should_restore = (
                name in snapshot_existing_files
                if snapshot_existing_files is not None
                else src.is_file()
            )
            if should_restore:
                atomic_write_bytes(dst, src.read_bytes())
                restored.append(name)
                logger.info(f"Restored {src} -> {dst}")
            elif snapshot_managed_files is not None and name in snapshot_managed_files and dst.exists():
                # Absence is part of a v2 snapshot. Files introduced by newer
                # app versions are preserved because they are not in the old
                # snapshot's explicit managed set.
                dst.unlink()
                restored.append(name)
                logger.info(f"Removed {dst}; it was absent from the backup")
    except Exception as restore_error:
        rollback_errors = _restore_target_state(target_state)
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise RuntimeError(f"备份回滚失败，且自动恢复当前配置不完整: {details}") from restore_error
        raise

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
