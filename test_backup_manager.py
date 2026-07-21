import json

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


def test_create_backup_copy_failure_never_exposes_partial_legacy_backup(tmp_path, monkeypatch):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    backups_dir = tmp_path / "backups"

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(
        backup_manager,
        "BACKUP_FILES",
        {"first.json": first, "second.json": second},
    )
    original_copy = backup_manager.shutil.copy2
    calls = {"count": 0}

    def fail_second_copy(source, target):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("forced copy failure")
        return original_copy(source, target)

    monkeypatch.setattr(backup_manager.shutil, "copy2", fail_second_copy)

    try:
        backup_manager.create_backup("must remain invisible")
    except OSError as exc:
        assert "forced copy failure" in str(exc)
    else:
        raise AssertionError("create_backup should surface a failed copy")

    assert backup_manager.list_backups() == []
    assert list(backups_dir.iterdir()) == []


def test_list_backups_ignores_incomplete_pending_directory(tmp_path, monkeypatch):
    backups_dir = tmp_path / "backups"
    pending = backups_dir / ".pending-interrupted"
    pending.mkdir(parents=True)
    (pending / "config.json").write_text("partial", encoding="utf-8")

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    backup_manager.clear_backup_list_cache()

    assert backup_manager.list_backups() == []

    entry = BackupEntry(
        timestamp="incomplete",
        directory=pending,
        description="partial",
        files=["config.json"],
    )
    try:
        backup_manager.restore_backup(entry)
    except ValueError as exc:
        assert "尚未创建完成" in str(exc)
    else:
        raise AssertionError("pending backup directories must not be restorable")


def test_list_backups_cache_reuses_meta_reads_and_detects_external_change(tmp_path, monkeypatch):
    backups_dir = tmp_path / "backups"
    backup_dir = backups_dir / "2026-01-01T00-00-00"
    backup_dir.mkdir(parents=True)
    meta_path = backup_dir / backup_manager.BACKUP_META_FILE
    meta_path.write_text(
        json.dumps({
            "timestamp": "2026-01-01T00:00:00",
            "directory": str(backup_dir),
            "description": "first",
            "files": ["config.json"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    backup_manager.clear_backup_list_cache()

    original_loads = backup_manager.json.loads
    calls = {"count": 0}

    def counting_loads(value, *args, **kwargs):
        calls["count"] += 1
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(backup_manager.json, "loads", counting_loads)

    first = backup_manager.list_backups()
    first[0].description = "mutated"
    second = backup_manager.list_backups()

    assert calls["count"] == 1
    assert second[0].description == "first"

    meta_path.write_text(
        json.dumps({
            "timestamp": "2026-01-01T00:00:00",
            "directory": str(backup_dir),
            "description": "external-change",
            "files": ["config.json", "settings.json"],
        }),
        encoding="utf-8",
    )

    third = backup_manager.list_backups()

    assert calls["count"] == 2
    assert third[0].description == "external-change"


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


def test_backup_prune_rejects_invalid_keep_count(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", tmp_path / "backups")

    for value in (-1, True, 1.5, "2"):
        try:
            backup_manager.prune_backups(keep_count=value)
        except ValueError as exc:
            assert "非负整数" in str(exc)
        else:
            raise AssertionError(f"invalid keep_count must be rejected: {value!r}")


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


def test_restore_backup_removes_files_absent_from_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    backups_dir = tmp_path / "backups"

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"config.json": source})

    entry = backup_manager.create_backup("before config existed")
    source.write_text("created later", encoding="utf-8")

    restored = backup_manager.restore_backup(entry)

    assert restored == ["config.json"]
    assert not source.exists()
    safety = backup_manager.get_latest_backup()
    assert safety is not None
    assert safety.description == "回滚前自动备份"
    assert (safety.directory / "config.json").read_text(encoding="utf-8") == "created later"


def test_restore_backup_rejects_declared_file_missing_from_backup(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    source.write_text("current", encoding="utf-8")
    backups_dir = tmp_path / "backups"
    backup_dir = backups_dir / "broken"
    backup_dir.mkdir(parents=True)

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"config.json": source})

    entry = BackupEntry(
        timestamp="2026-01-01T00:00:00",
        directory=backup_dir,
        description="broken",
        files=["config.json"],
    )

    try:
        backup_manager.restore_backup(entry)
    except ValueError as exc:
        assert "缺失或损坏" in str(exc)
    else:
        raise AssertionError("restore_backup should reject an incomplete backup")

    assert source.read_text(encoding="utf-8") == "current"
    assert not any(path.name != "broken" for path in backups_dir.iterdir())


def test_restore_legacy_backup_does_not_delete_new_managed_file(tmp_path, monkeypatch):
    source = tmp_path / "new_config.json"
    source.write_text("current config", encoding="utf-8")
    backups_dir = tmp_path / "backups"
    backup_dir = backups_dir / "legacy"
    backup_dir.mkdir(parents=True)
    (backup_dir / backup_manager.BACKUP_META_FILE).write_text(
        json.dumps({
            "timestamp": "2025-01-01T00:00:00",
            "directory": str(backup_dir),
            "description": "before profiles were managed",
            "files": [],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"new_config.json": source})

    [entry] = backup_manager.list_backups()
    restored = backup_manager.restore_backup(entry)

    assert restored == []
    assert source.read_text(encoding="utf-8") == "current config"


def test_v2_restore_ignores_stray_file_not_declared_in_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"config.json": source})

    entry = backup_manager.create_backup("absent")
    source.write_text("current", encoding="utf-8")
    (entry.directory / "config.json").write_text("stray", encoding="utf-8")

    restored = backup_manager.restore_backup(entry)

    assert restored == ["config.json"]
    assert not source.exists()


def test_restore_failure_rolls_back_all_targets_to_current_state(tmp_path, monkeypatch):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text("old-a", encoding="utf-8")
    second.write_text("old-b", encoding="utf-8")
    backups_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_manager, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_manager, "BACKUP_FILES", {"a.json": first, "b.json": second})

    entry = backup_manager.create_backup("old")
    first.write_text("current-a", encoding="utf-8")
    second.write_text("current-b", encoding="utf-8")
    original_write = backup_manager.atomic_write_bytes
    calls = {"count": 0}

    def fail_second_write(path, content):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("forced second write failure")
        return original_write(path, content)

    monkeypatch.setattr(backup_manager, "atomic_write_bytes", fail_second_write)

    try:
        backup_manager.restore_backup(entry)
    except OSError as exc:
        assert "forced second write failure" in str(exc)
    else:
        raise AssertionError("restore should surface the injected write failure")

    assert first.read_text(encoding="utf-8") == "current-a"
    assert second.read_text(encoding="utf-8") == "current-b"
