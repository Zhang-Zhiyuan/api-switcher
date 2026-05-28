from core import backup_manager
from models.profile import BackupEntry


def test_backup_dirs_are_unique_and_latest_can_restore(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    source.write_text("one", encoding="utf-8")
    backups_dir = tmp_path / "backups"

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"config.json": source})

    first = backup_manager.create_backup("first")
    source.write_text("two", encoding="utf-8")
    second = backup_manager.create_backup("second")

    assert first.directory != second.directory
    assert first.directory.exists()
    assert second.directory.exists()
    assert backup_manager.get_latest_backup().description == "second"

    source.write_text("dirty", encoding="utf-8")
    restored_entry, restored_files = backup_manager.restore_latest_backup()

    assert restored_entry.directory == second.directory
    assert restored_files == ["config.json"]
    assert source.read_text(encoding="utf-8") == "two"
    assert backup_manager.get_latest_backup().description == "回滚前自动备份"


def test_backup_allocator_reserves_unique_directory(tmp_path, monkeypatch):
    backups_dir = tmp_path / "backups"
    timestamp = "2026-01-01T00-00-00"
    (backups_dir / timestamp).mkdir(parents=True)
    (backups_dir / f"{timestamp}-02").mkdir()

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)

    allocated = backup_manager._allocate_backup_dir(timestamp)

    assert allocated == backups_dir / f"{timestamp}-03"
    assert allocated.exists()


def test_backup_prune_ignores_directory_from_metadata(tmp_path, monkeypatch):
    backups_dir = tmp_path / "backups"
    backup_dir = backups_dir / "2026-01-01T00-00-00"
    backup_dir.mkdir(parents=True)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "keep.txt").write_text("do not delete", encoding="utf-8")
    (backup_dir / backup_manager.BACKUP_META_FILE).write_text(
        (
            "{"
            f'"timestamp": "2026-01-01T00:00:00", '
            f'"directory": "{external_dir.as_posix()}", '
            '"description": "malicious", '
            '"files": []'
            "}"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)

    [entry] = backup_manager.list_backups()
    assert entry.directory == backup_dir
    assert backup_manager.prune_backups(keep_count=0) == 1
    assert not backup_dir.exists()
    assert (external_dir / "keep.txt").exists()


def test_restore_backup_rejects_unmanaged_directory_before_safety_backup(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    source.write_text("current", encoding="utf-8")
    backups_dir = tmp_path / "backups"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "config.json").write_text("external", encoding="utf-8")

    safety_backups: list[str] = []
    original_create_backup = backup_manager.create_backup

    def create_backup(description: str = ""):
        safety_backups.append(description)
        return original_create_backup(description)

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"config.json": source})
    monkeypatch.setattr(backup_manager, "create_backup", create_backup)

    entry = BackupEntry(
        timestamp="2026-01-01T00:00:00",
        directory=external_dir,
        description="external",
        files=["config.json"],
    )
    try:
        backup_manager.restore_backup(entry)
    except ValueError as e:
        assert "受管备份目录" in str(e)
    else:
        raise AssertionError("restore_backup should reject unmanaged directories")

    assert source.read_text(encoding="utf-8") == "current"
    assert safety_backups == []
