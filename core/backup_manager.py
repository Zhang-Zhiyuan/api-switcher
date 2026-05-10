import shutil
import logging
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
from models.profile import BackupEntry

logger = logging.getLogger(__name__)

BACKUP_FILES = {
    "claude_settings.json": CLAUDE_SETTINGS,
    "claude_config.json": CLAUDE_CONFIG,
    "claude_credentials.json": CLAUDE_CREDENTIALS,
    "codex_config.toml": CODEX_CONFIG,
    "codex_auth.json": CODEX_AUTH,
    "vscode_settings.json": VSCODE_SETTINGS,
}

BACKUP_META_FILE = "backup_meta.json"


def _allocate_backup_dir(timestamp: str) -> Path:
    """Return a unique backup directory for the timestamp."""
    base = BACKUPS_DIR / timestamp
    if not base.exists():
        return base

    for index in range(2, 1000):
        candidate = BACKUPS_DIR / f"{timestamp}-{index:02d}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("无法创建唯一备份目录，请稍后重试")


def create_backup(description: str = "") -> BackupEntry:
    """Create a backup of all config files. Returns the backup entry."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_dir = _allocate_backup_dir(ts)
    backup_dir.mkdir(parents=True, exist_ok=True)

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
    import json
    meta_path = backup_dir / BACKUP_META_FILE
    meta_path.write_text(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    return entry


def list_backups() -> list[BackupEntry]:
    """List all backups, most recent first."""
    if not BACKUPS_DIR.exists():
        return []

    backups = []
    for d in sorted(BACKUPS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / BACKUP_META_FILE
        if meta_path.exists():
            try:
                import json
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                backups.append(BackupEntry.from_dict(data))
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

    return backups


def get_latest_backup() -> BackupEntry | None:
    """Return the most recent backup entry, if any."""
    backups = list_backups()
    return backups[0] if backups else None


def restore_backup(entry: BackupEntry) -> list[str]:
    """Restore config files from a backup. Returns list of restored file names."""
    # Create a safety backup first
    create_backup("回滚前自动备份")

    restored = []
    backup_dir = Path(entry.directory)

    for name, dst in BACKUP_FILES.items():
        src = backup_dir / name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
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
    for entry in to_remove:
        try:
            shutil.rmtree(entry.directory)
            removed += 1
        except Exception as e:
            logger.warning(f"Failed to remove backup {entry.directory}: {e}")

    return removed
