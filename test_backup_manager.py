from core import backup_manager


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
