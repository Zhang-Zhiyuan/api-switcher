import sqlite3
import shutil
from pathlib import Path

import pytest

import core.browser_data_manager as browser_data_module
import core.browser_profile_manager as browser_profile_module
from core.browser_data_manager import BrowserDataManager
from models.profile import BrowserProfile


def test_clear_cookies_db_uses_unique_temps_and_cleans_target_domains(tmp_path):
    default_dir = tmp_path / "Default"
    network_dir = default_dir / "Network"
    network_dir.mkdir(parents=True)
    cookies_path = network_dir / "Cookies"

    conn = sqlite3.connect(cookies_path)
    try:
        conn.execute("CREATE TABLE cookies (host_key TEXT)")
        conn.executemany(
            "INSERT INTO cookies (host_key) VALUES (?)",
            [
                ("chatgpt.com",),
                (".chatgpt.com",),
                ("example.com",),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    BrowserDataManager()._clear_cookies_db(default_dir, ["chatgpt.com"])

    conn = sqlite3.connect(cookies_path)
    try:
        rows = [row[0] for row in conn.execute("SELECT host_key FROM cookies ORDER BY host_key")]
    finally:
        conn.close()

    assert rows == ["example.com"]
    assert not list(network_dir.glob("Cookies.*"))


def test_clear_cookies_db_absorbs_committed_wal_and_removes_old_sidecars(tmp_path):
    default_dir = tmp_path / "Default"
    network_dir = default_dir / "Network"
    network_dir.mkdir(parents=True)
    cookies_path = network_dir / "Cookies"
    staging_path = tmp_path / "staging-cookies"

    conn = sqlite3.connect(staging_path)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE cookies (host_key TEXT)")
        conn.execute("INSERT INTO cookies (host_key) VALUES ('example.com')")
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

        # These rows are committed but deliberately remain outside the main DB.
        conn.executemany(
            "INSERT INTO cookies (host_key) VALUES (?)",
            [("chatgpt.com",), (".chatgpt.com",), ("wal.example",)],
        )
        conn.commit()
        staging_wal = Path(f"{staging_path}-wal")
        staging_shm = Path(f"{staging_path}-shm")
        assert staging_wal.stat().st_size > 0

        shutil.copy2(staging_path, cookies_path)
        shutil.copy2(staging_wal, Path(f"{cookies_path}-wal"))
        shutil.copy2(staging_shm, Path(f"{cookies_path}-shm"))
    finally:
        conn.close()

    # An immutable connection ignores WAL, proving the target rows are not in
    # the copied main file and must be captured from the committed WAL.
    immutable_uri = cookies_path.resolve().as_uri() + "?immutable=1"
    immutable_conn = sqlite3.connect(immutable_uri, uri=True)
    try:
        assert list(immutable_conn.execute("SELECT host_key FROM cookies")) == [("example.com",)]
    finally:
        immutable_conn.close()

    BrowserDataManager()._clear_cookies_db(default_dir, ["chatgpt.com"])

    result_conn = sqlite3.connect(cookies_path)
    try:
        rows = [row[0] for row in result_conn.execute("SELECT host_key FROM cookies ORDER BY host_key")]
    finally:
        result_conn.close()
    assert rows == ["example.com", "wal.example"]
    for suffix in ("-journal", "-shm", "-wal"):
        assert not Path(f"{cookies_path}{suffix}").exists()
    assert not list(network_dir.glob("Cookies.*"))


def test_cookie_copy_on_write_failure_preserves_live_database(tmp_path, monkeypatch):
    default_dir = tmp_path / "Default"
    network_dir = default_dir / "Network"
    network_dir.mkdir(parents=True)
    cookies_path = network_dir / "Cookies"
    conn = sqlite3.connect(cookies_path)
    try:
        conn.execute("CREATE TABLE cookies (host_key TEXT)")
        conn.execute("INSERT INTO cookies (host_key) VALUES ('chatgpt.com')")
        conn.commit()
    finally:
        conn.close()

    def fail_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(browser_data_module, "replace_with_retry", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        BrowserDataManager()._clear_cookies_db(default_dir, ["chatgpt.com"])

    conn = sqlite3.connect(cookies_path)
    try:
        rows = list(conn.execute("SELECT host_key FROM cookies"))
    finally:
        conn.close()
    assert rows == [("chatgpt.com",)]
    assert not list(network_dir.glob("Cookies.*"))


def test_file_lock_probe_never_renames_live_browser_file(tmp_path, monkeypatch):
    candidate = tmp_path / "Preferences"
    candidate.write_text("keep", encoding="utf-8")

    def unexpected_rename(*_args, **_kwargs):
        raise AssertionError("lock probe must not rename browser data")

    monkeypatch.setattr(Path, "replace", unexpected_rename)
    assert BrowserDataManager()._is_file_locked(candidate) is False
    assert candidate.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "Preferences.lockprobe").exists()


def test_full_reset_requires_opt_in_and_never_accepts_managed_root(tmp_path, monkeypatch):
    managed_root = tmp_path / "browser_profiles"
    child = managed_root / "chrome_child"
    sibling = managed_root / "chrome_sibling"
    child.mkdir(parents=True)
    sibling.mkdir()
    sentinel = sibling / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(browser_data_module, "MANAGED_BROWSER_PROFILES_DIR", managed_root)
    monkeypatch.setattr(browser_profile_module, "MANAGED_BROWSER_PROFILES_DIR", managed_root)

    disabled = BrowserProfile(
        name="Disabled",
        browser_type="chrome",
        profile_mode="managed",
        user_data_dir=str(child),
        allow_full_reset=False,
        created_by_app=True,
    )
    allowed, reason = BrowserDataManager().can_full_reset(disabled)
    assert allowed is False
    assert "未开启" in reason

    root_profile = BrowserProfile(
        name="Root",
        browser_type="chrome",
        profile_mode="managed",
        user_data_dir=str(managed_root),
        allow_full_reset=True,
        created_by_app=True,
    )
    profile_manager = browser_profile_module.BrowserProfileManager()
    assert profile_manager.validate_profile(root_profile)[0] is False
    assert BrowserDataManager().can_full_reset(root_profile)[0] is False

    with pytest.raises(RuntimeError, match="不允许整目录清理"):
        BrowserDataManager().full_reset(root_profile)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_site_cleanup_clears_shared_leveldb_and_only_target_origin_directories(tmp_path):
    default_dir = tmp_path / "Default"
    shared_files = [
        default_dir / "Local Storage" / "leveldb" / "000003.log",
        default_dir / "Session Storage" / "000004.log",
        default_dir / "Service Worker" / "CacheStorage" / "opaque-cache" / "data",
        default_dir / "Service Worker" / "Database" / "000005.log",
    ]
    for path in shared_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("shared data for chatgpt.com and example.com", encoding="utf-8")

    indexed_target = default_dir / "IndexedDB" / "https_chatgpt.com_0.indexeddb.leveldb"
    indexed_other = default_dir / "IndexedDB" / "https_example.com_0.indexeddb.leveldb"
    indexed_false_positive = default_dir / "IndexedDB" / "https_notchatgpt.com_0.indexeddb.leveldb"
    for path in (indexed_target, indexed_other, indexed_false_positive):
        path.mkdir(parents=True)
        (path / "data").write_text("value", encoding="utf-8")

    legacy_target = default_dir / "Local Storage" / "https_chatgpt.com_0.localstorage"
    legacy_other = default_dir / "Local Storage" / "https_example.com_0.localstorage"
    legacy_target.write_text("target", encoding="utf-8")
    legacy_other.write_text("other", encoding="utf-8")

    BrowserDataManager()._clear_storage_for_domains(
        default_dir,
        ["chatgpt.com"],
        clear_shared_storage=True,
    )

    assert not (default_dir / "Local Storage" / "leveldb").exists()
    assert not (default_dir / "Session Storage").exists()
    assert not (default_dir / "Service Worker" / "CacheStorage").exists()
    assert not (default_dir / "Service Worker" / "Database").exists()
    assert not indexed_target.exists()
    assert indexed_other.exists()
    assert indexed_false_positive.exists()
    assert not legacy_target.exists()
    assert legacy_other.exists()


def test_external_profile_site_cleanup_preserves_shared_storage_and_cache(tmp_path, monkeypatch):
    profile_dir = tmp_path / "external"
    default_dir = profile_dir / "Default"
    shared = default_dir / "Local Storage" / "leveldb" / "000003.log"
    cache = default_dir / "Cache" / "cache.data"
    indexed_target = default_dir / "IndexedDB" / "https_chatgpt.com_0.indexeddb.leveldb" / "data"
    for path in (shared, cache, indexed_target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep or clear", encoding="utf-8")

    profile = BrowserProfile(
        name="External",
        browser_type="chrome",
        profile_mode="external",
        user_data_dir=str(profile_dir),
        created_by_app=False,
    )
    manager = BrowserDataManager()
    monkeypatch.setattr(manager, "is_browser_running", lambda _profile: False)

    shared_cleared = manager.clear_site_data(profile, "chatgpt")

    assert shared_cleared is False
    assert shared.exists()
    assert cache.exists()
    assert not indexed_target.parent.exists()


def _managed_reset_profile(path: Path) -> BrowserProfile:
    return BrowserProfile(
        name="Reset",
        browser_type="chrome",
        profile_mode="managed",
        user_data_dir=str(path),
        allow_full_reset=True,
        created_by_app=True,
    )


def test_full_reset_uses_unique_backup_and_preserves_backup_named_sibling(tmp_path, monkeypatch):
    managed_root = tmp_path / "browser_profiles"
    profile_dir = managed_root / "chrome_reset"
    profile_dir.mkdir(parents=True)
    (profile_dir / "old.txt").write_text("old", encoding="utf-8")
    backup_named_sibling = managed_root / "chrome_reset.backup"
    backup_named_sibling.mkdir()
    sibling_sentinel = backup_named_sibling / "keep.txt"
    sibling_sentinel.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(browser_data_module, "MANAGED_BROWSER_PROFILES_DIR", managed_root)
    manager = BrowserDataManager()
    monkeypatch.setattr(manager, "is_browser_running", lambda _profile: False)

    manager.full_reset(_managed_reset_profile(profile_dir))

    assert profile_dir.is_dir()
    assert list(profile_dir.iterdir()) == []
    assert sibling_sentinel.read_text(encoding="utf-8") == "keep"
    assert not list(managed_root.glob("*.reset_backup"))


def test_full_reset_restores_original_if_empty_profile_creation_fails(tmp_path, monkeypatch):
    managed_root = tmp_path / "browser_profiles"
    profile_dir = managed_root / "chrome_reset"
    profile_dir.mkdir(parents=True)
    sentinel = profile_dir / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")

    monkeypatch.setattr(browser_data_module, "MANAGED_BROWSER_PROFILES_DIR", managed_root)
    manager = BrowserDataManager()
    monkeypatch.setattr(manager, "is_browser_running", lambda _profile: False)
    original_mkdir = Path.mkdir

    def fail_recreate(path, *args, **kwargs):
        if path == profile_dir and not path.exists():
            raise OSError("forced recreate failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_recreate)

    with pytest.raises(RuntimeError, match="forced recreate failure"):
        manager.full_reset(_managed_reset_profile(profile_dir))

    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not list(managed_root.glob("*.reset_backup"))


def test_full_reset_keeps_recovery_copy_when_final_cleanup_partially_fails(tmp_path, monkeypatch):
    managed_root = tmp_path / "browser_profiles"
    profile_dir = managed_root / "chrome_reset"
    profile_dir.mkdir(parents=True)
    (profile_dir / "first.txt").write_text("first", encoding="utf-8")
    (profile_dir / "second.txt").write_text("second", encoding="utf-8")

    monkeypatch.setattr(browser_data_module, "MANAGED_BROWSER_PROFILES_DIR", managed_root)
    manager = BrowserDataManager()
    monkeypatch.setattr(manager, "is_browser_running", lambda _profile: False)
    original_rmtree = browser_data_module.shutil.rmtree

    def partially_fail_backup_cleanup(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.endswith(".reset_backup"):
            (candidate / "first.txt").unlink()
            raise OSError("forced partial cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(browser_data_module.shutil, "rmtree", partially_fail_backup_cleanup)

    with pytest.raises(RuntimeError, match="已清空.*备份清理失败"):
        manager.full_reset(_managed_reset_profile(profile_dir))

    assert profile_dir.is_dir()
    assert list(profile_dir.iterdir()) == []
    [recovery_dir] = list(managed_root.glob("*.reset_backup"))
    assert (recovery_dir / "second.txt").read_text(encoding="utf-8") == "second"
