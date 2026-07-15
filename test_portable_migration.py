"""Regression checks for password-protected portable profile migration."""
import base64
import json
import posixpath
import stat
import tempfile
import warnings
import zipfile
import zlib
from io import BytesIO
from pathlib import Path

import pytest

from config import paths
from core import portable_migration, profile_manager, security, session_migration
from models.profile import BrowserProfile, ClaudeProfile, CodexProfile, SSHProfile


def test_session_migration_round_trip(tmp_path):
    claude_home = tmp_path / "claude_a"
    codex_home = tmp_path / "codex_a"
    claude_project = claude_home / "projects" / "c--Users-Test-Project"
    claude_project.mkdir(parents=True)
    claude_file = claude_project / "claude-session-1.jsonl"
    claude_file.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:00Z",
                "sessionId": "claude-session-1",
                "cwd": "C:\\Users\\Test\\Project",
                "message": {"content": [{"type": "text", "text": "迁移 Claude 会话"}]},
            }, ensure_ascii=False),
            json.dumps({
                "type": "assistant",
                "timestamp": "2026-05-01T00:01:00Z",
                "sessionId": "claude-session-1",
                "message": {"model": "opus[1m]", "content": "ok"},
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    support_file = claude_project / "claude-session-1" / "tool-results" / "result.txt"
    support_file.parent.mkdir(parents=True)
    support_file.write_text("tool output", encoding="utf-8")

    codex_session_dir = codex_home / "sessions" / "2026" / "05" / "01"
    codex_session_dir.mkdir(parents=True)
    codex_file = codex_session_dir / "rollout-2026-05-01T00-00-00-codex-session-1.jsonl"
    codex_file.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-session-1",
                    "timestamp": "2026-05-01T00:00:00Z",
                    "cwd": "C:\\Users\\Test\\Project",
                    "model_provider": "openai",
                },
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "迁移 Codex 会话"}],
                },
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({
            "id": "codex-session-1",
            "thread_name": "Codex 迁移测试",
            "updated_at": "2026-05-01T00:02:00Z",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    records = session_migration.list_sessions(claude_home=claude_home, codex_home=codex_home)
    assert {record.provider for record in records} == {"claude", "codex"}
    assert any(record.title == "Codex 迁移测试" for record in records)
    assert any(record.summary == "迁移 Claude 会话" for record in records)

    bundle = tmp_path / "sessions.asxsession"
    exported = session_migration.export_sessions(
        bundle,
        {record.key for record in records},
        claude_home=claude_home,
        codex_home=codex_home,
    )
    assert exported.session_count == 2
    assert exported.file_count == 3

    imported_claude_home = tmp_path / "claude_b"
    imported_codex_home = tmp_path / "codex_b"
    imported = session_migration.import_sessions(
        bundle,
        claude_home=imported_claude_home,
        codex_home=imported_codex_home,
    )
    assert imported.session_count == 2
    assert imported.file_count == 3
    assert (imported_claude_home / "projects" / "c--Users-Test-Project" / "claude-session-1.jsonl").exists()
    assert (
        imported_claude_home
        / "projects"
        / "c--Users-Test-Project"
        / "claude-session-1"
        / "tool-results"
        / "result.txt"
    ).read_text(encoding="utf-8") == "tool output"
    assert (imported_codex_home / "sessions" / "2026" / "05" / "01" / codex_file.name).exists()
    assert "Codex 迁移测试" in (imported_codex_home / "session_index.jsonl").read_text(encoding="utf-8")

    imported_again = session_migration.import_sessions(
        bundle,
        claude_home=imported_claude_home,
        codex_home=imported_codex_home,
    )
    assert imported_again.session_count == 0
    assert imported_again.skipped_existing == 3

    remap_claude_home = tmp_path / "claude_c"
    remap_codex_home = tmp_path / "codex_c"
    target_project = tmp_path / "new_machine" / "Project中文"
    target_project.mkdir(parents=True)
    remapped = session_migration.import_sessions(
        bundle,
        claude_home=remap_claude_home,
        codex_home=remap_codex_home,
        target_project_path=target_project,
    )
    assert remapped.session_count == 2
    remapped_project_key = session_migration._claude_project_key_for_path(str(target_project.resolve()))
    remapped_claude_file = remap_claude_home / "projects" / remapped_project_key / "claude-session-1.jsonl"
    assert remapped_claude_file.exists()
    assert json.loads(remapped_claude_file.read_text(encoding="utf-8").splitlines()[0])["cwd"] == str(target_project.resolve())

    remapped_codex_file = remap_codex_home / "sessions" / "2026" / "05" / "01" / codex_file.name
    codex_meta = json.loads(remapped_codex_file.read_text(encoding="utf-8").splitlines()[0])
    assert codex_meta["payload"]["cwd"] == str(target_project.resolve())

    summary = session_migration.inspect_package(bundle)
    assert summary.session_count == 2
    assert summary.providers == {"claude": 1, "codex": 1}
    assert summary.file_count == 3


def test_session_migration_ignores_runtime_context_titles(tmp_path):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_project = claude_home / "projects" / "c--Users-Test-Project"
    claude_project.mkdir(parents=True)
    claude_file = claude_project / "claude-context.jsonl"
    claude_file.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:00Z",
                "sessionId": "claude-context",
                "cwd": "C:\\Users\\Test\\Project",
                "message": {
                    "content": "<local-command-caveat>Caveat: ignore generated command messages</local-command-caveat>"
                },
            }, ensure_ascii=False),
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:01Z",
                "sessionId": "claude-context",
                "message": {"content": "<command-name>/model</command-name>"},
            }, ensure_ascii=False),
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:02Z",
                "sessionId": "claude-context",
                "message": {"content": "<local-command-stdout>Set model</local-command-stdout>"},
            }, ensure_ascii=False),
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:03Z",
                "sessionId": "claude-context",
                "message": {"content": "真正的 Claude 迁移需求"},
            }, ensure_ascii=False),
            json.dumps({
                "type": "ai-title",
                "timestamp": "2026-05-01T00:00:04Z",
                "sessionId": "claude-context",
                "aiTitle": "Claude 真实标题",
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    codex_session_dir = codex_home / "sessions" / "2026" / "05" / "01"
    codex_session_dir.mkdir(parents=True)
    codex_file = codex_session_dir / "rollout-2026-05-01T00-00-00-codex-context.jsonl"
    codex_file.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-context",
                    "timestamp": "2026-05-01T00:00:00Z",
                    "cwd": "C:\\Users\\Test\\Project",
                    "model_provider": "openai",
                },
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions for C:\\Users\\Test\\Project"}],
                },
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<hook_prompt>continue</hook_prompt>"}],
                },
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "真正的 Codex 迁移需求"}],
                },
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({
            "id": "codex-context",
            "thread_name": "# AGENTS.md instructions for C:\\Users\\Test\\Project",
            "updated_at": "2026-05-01T00:00:04Z",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    records = session_migration.list_sessions(claude_home=claude_home, codex_home=codex_home)
    by_provider = {record.provider: record for record in records}

    assert by_provider["claude"].title == "Claude 真实标题"
    assert by_provider["claude"].summary == "真正的 Claude 迁移需求"
    assert by_provider["codex"].title == "真正的 Codex 迁移需求"
    assert by_provider["codex"].summary == "真正的 Codex 迁移需求"


def test_session_listing_reuses_cache_until_files_change(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_project = claude_home / "projects" / "c--Users-Test-Project"
    claude_project.mkdir(parents=True)
    claude_file = claude_project / "cached-session.jsonl"

    def write_message(message: str) -> None:
        claude_file.write_text(
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:00Z",
                "sessionId": "cached-session",
                "cwd": "C:\\Users\\Test\\Project",
                "message": {"content": message},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    write_message("第一次会话")
    session_migration.clear_local_session_cache()
    original_parse = session_migration._parse_claude_session
    calls = {"count": 0}

    def counted_parse(*args, **kwargs):
        calls["count"] += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(session_migration, "_parse_claude_session", counted_parse)

    first = session_migration.list_sessions("claude", claude_home=claude_home, codex_home=codex_home)
    second = session_migration.list_sessions("claude", claude_home=claude_home, codex_home=codex_home)

    assert calls["count"] == 1
    assert first[0].summary == "第一次会话"
    assert second[0].summary == "第一次会话"

    write_message("第二次会话，文件大小也变化")
    third = session_migration.list_sessions("claude", claude_home=claude_home, codex_home=codex_home)

    assert calls["count"] == 2
    assert third[0].summary == "第二次会话，文件大小也变化"


def test_session_export_skips_oversized_files(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_project = claude_home / "projects" / "c--Users-Test-Project"
    claude_project.mkdir(parents=True)
    claude_file = claude_project / "small-claude.jsonl"
    claude_file.write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-05-01T00:00:00Z",
            "sessionId": "small-claude",
            "message": {"content": "小会话"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    support_file = claude_project / "small-claude" / "tool-results" / "huge.txt"
    support_file.parent.mkdir(parents=True)

    codex_session_dir = codex_home / "sessions" / "2026" / "05" / "01"
    codex_session_dir.mkdir(parents=True)
    codex_file = codex_session_dir / "rollout-large-codex.jsonl"

    limit = claude_file.stat().st_size + 40
    support_file.write_text("s" * (limit + 20), encoding="utf-8")
    codex_file.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-01T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "large-codex", "cwd": "C:\\Users\\Test\\Project"},
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "大 Codex 会话 " + ("x" * limit)}],
                },
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_migration, "MAX_PACKAGE_FILE_BYTES", limit)

    records = session_migration.list_sessions(claude_home=claude_home, codex_home=codex_home)
    bundle = tmp_path / "oversized.asxsession"
    exported = session_migration.export_sessions(
        bundle,
        {record.key for record in records},
        claude_home=claude_home,
        codex_home=codex_home,
    )

    assert exported.session_count == 1
    assert exported.file_count == 1
    assert any(key.startswith("codex:") for key in exported.skipped_keys)
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["sessions"][0]["provider"] == "claude"
    assert len(manifest["sessions"][0]["files"]) == 1


def test_session_export_rejects_output_inside_live_session_home(tmp_path):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    session_file = claude_home / "projects" / "demo" / "session.jsonl"
    session_file.parent.mkdir(parents=True)
    original = json.dumps({
        "type": "user",
        "timestamp": "2026-05-01T00:00:00Z",
        "sessionId": "session",
        "message": {"content": "keep this session"},
    }) + "\n"
    session_file.write_text(original, encoding="utf-8")
    session_migration.clear_local_session_cache()
    [record] = session_migration.list_sessions(
        "claude",
        claude_home=claude_home,
        codex_home=codex_home,
    )

    with pytest.raises(ValueError, match="不能保存"):
        session_migration.export_sessions(
            session_file,
            {record.key},
            claude_home=claude_home,
            codex_home=codex_home,
        )

    assert session_file.read_text(encoding="utf-8") == original
    assert not zipfile.is_zipfile(session_file)


def test_session_migration_skips_invalid_package_entries(tmp_path):
    package = tmp_path / "invalid.asxsession"
    manifest = {
        "format": session_migration.PACKAGE_FORMAT,
        "version": session_migration.PACKAGE_VERSION,
        "sessions": [
            {
                "provider": "claude",
                "session_id": "bad-archive-path",
                "relative_path": "projects/demo/bad-archive-path.jsonl",
                "files": [
                    {
                        "relative_path": "projects/demo/bad-archive-path.jsonl",
                        "archive_path": "../bad.jsonl",
                        "main": True,
                    }
                ],
            },
            {
                "provider": "codex",
                "session_id": "missing-file",
                "relative_path": "sessions/2026/05/01/missing.jsonl",
                "files": [
                    {
                        "relative_path": "sessions/2026/05/01/missing.jsonl",
                        "archive_path": "files/1/missing.jsonl",
                        "main": True,
                    }
                ],
            },
            {
                "provider": "codex",
                "session_id": "good-file",
                "relative_path": "sessions/2026/05/01/good.jsonl",
                "title": "Good",
                "updated_at": "2026-05-01T00:00:00Z",
                "files": [
                    {
                        "relative_path": "sessions/2026/05/01/good.jsonl",
                        "archive_path": "files/2/good.jsonl",
                        "main": True,
                    }
                ],
            },
        ],
    }
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        bundle.writestr("../bad.jsonl", "{}\n")
        bundle.writestr("files/2/good.jsonl", "{}\n")

    imported = session_migration.import_sessions(
        package,
        claude_home=tmp_path / "claude",
        codex_home=tmp_path / "codex",
    )

    assert imported.session_count == 1
    assert imported.file_count == 1
    assert imported.skipped_invalid == 2
    assert (tmp_path / "codex" / "sessions" / "2026" / "05" / "01" / "good.jsonl").exists()
    assert not (tmp_path / "bad.jsonl").exists()


def test_session_import_rejects_provider_config_paths_locally_and_over_ssh(monkeypatch, tmp_path):
    package = tmp_path / "config-overwrite.asxsession"
    manifest = {
        "format": session_migration.PACKAGE_FORMAT,
        "version": session_migration.PACKAGE_VERSION,
        "sessions": [
            {
                "provider": "claude",
                "session_id": "malicious-claude",
                "relative_path": "settings.json",
                "files": [{
                    "relative_path": "settings.json",
                    "archive_path": "files/0/settings.json",
                    "main": True,
                }],
            },
            {
                "provider": "codex",
                "session_id": "malicious-codex",
                "relative_path": "auth.json",
                "files": [{
                    "relative_path": "auth.json",
                    "archive_path": "files/1/auth.json",
                    "main": True,
                }],
            },
        ],
    }
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("files/0/settings.json", "malicious")
        bundle.writestr("files/1/auth.json", "malicious")

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    claude_settings = claude_home / "settings.json"
    codex_auth = codex_home / "auth.json"
    claude_settings.parent.mkdir(parents=True)
    codex_auth.parent.mkdir(parents=True)
    claude_settings.write_text("keep-claude", encoding="utf-8")
    codex_auth.write_text("keep-codex", encoding="utf-8")

    local_result = session_migration.import_sessions(
        package,
        claude_home=claude_home,
        codex_home=codex_home,
        overwrite=True,
    )

    assert local_result.session_count == 0
    assert local_result.file_count == 0
    assert local_result.skipped_invalid == 2
    assert claude_settings.read_text(encoding="utf-8") == "keep-claude"
    assert codex_auth.read_text(encoding="utf-8") == "keep-codex"

    sftp = _SessionSFTP()
    _patch_session_ssh(monkeypatch, sftp)
    remote_result = session_migration.import_sessions_to_ssh("gpu", package, overwrite=True)

    assert remote_result.session_count == 0
    assert remote_result.file_count == 0
    assert remote_result.skipped_invalid == 2
    assert "/home/test/.claude/settings.json" not in sftp.files
    assert "/home/test/.codex/auth.json" not in sftp.files


def test_session_migration_rejects_duplicate_manifest(tmp_path):
    package = tmp_path / "duplicate-manifest.asxsession"
    manifest = json.dumps({
        "format": session_migration.PACKAGE_FORMAT,
        "version": session_migration.PACKAGE_VERSION,
        "sessions": [],
    })
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", manifest)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            bundle.writestr("manifest.json", manifest)

    with pytest.raises(ValueError, match="重复关键条目"):
        session_migration.inspect_package(package)


def test_session_migration_rejects_oversized_manifest(tmp_path, monkeypatch):
    package = tmp_path / "oversized-manifest.asxsession"
    monkeypatch.setattr(session_migration, "MAX_MANIFEST_BYTES", 32)
    with zipfile.ZipFile(package, "w") as bundle:
        bundle.writestr("manifest.json", " " * 64)

    with pytest.raises(ValueError, match="manifest 过大"):
        session_migration.inspect_package(package)


def test_browser_restore_rejects_size_mismatch_without_profile_leftover(tmp_path, monkeypatch):
    _set_data_dir(tmp_path)
    _reset_store()
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_BadImport"
    payload = zlib.compress(b'{"ok":true}')
    browser_data = {
        "BadImport": {
            "profile": {
                "name": "BadImport",
                "browser_type": "chrome",
                "profile_mode": "managed",
                "user_data_dir": str(target),
                "created_by_app": True,
            },
            "files": [{
                "path": "Default/Preferences",
                "size": 999,
                "compression": "zlib",
                "data": base64.b64encode(payload).decode("ascii"),
            }],
        }
    }

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(browser_data)

    assert restored_files == 0
    assert restored_bytes == 0
    assert restored_profiles == set()
    assert any("文件大小校验失败" in item for item in skipped)
    assert not target.exists()


def test_browser_restore_rejects_reparse_target_without_touching_link_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_Linked"
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")

    real_is_reparse = portable_migration._path_is_reparse_point
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(victim, target_is_directory=True)
    except OSError:
        target.mkdir()
        monkeypatch.setattr(
            portable_migration,
            "_path_is_reparse_point",
            lambda path: Path(path) == target or real_is_reparse(path),
        )

    content = b"new login state"
    browser_data = {
        "Linked": {
            "profile": {
                "name": "Linked",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "file_count": 1,
            "files": [{
                "path": "Local State",
                "size": len(content),
                "compression": "none",
                "data": base64.b64encode(content).decode("ascii"),
            }],
        },
    }

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(browser_data)

    assert restored_files == 0
    assert restored_bytes == 0
    assert not restored_profiles
    assert any("符号链接或重解析" in item for item in skipped)
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not (victim / "Local State").exists()


def test_browser_restore_invalid_entry_keeps_existing_profile_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_AllOrNothing"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    valid_content = b"valid-first-entry"
    browser_data = {
        "AllOrNothing": {
            "profile": {
                "name": "AllOrNothing",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "files": [
                {
                    "path": "Default/Network/Cookies",
                    "size": len(valid_content),
                    "compression": "none",
                    "data": base64.b64encode(valid_content).decode("ascii"),
                },
                {
                    "path": "Default/Local Storage/leveldb/000003.log",
                    "size": 10,
                    "compression": "none",
                    "data": "invalid-base64",
                },
            ],
        },
    }

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(browser_data)

    assert restored_files == 0
    assert restored_bytes == 0
    assert not restored_profiles
    assert any("解码失败" in item for item in skipped)
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not (target / "Default" / "Network" / "Cookies").exists()
    assert not list(target.parent.glob("*.import_staging"))
    assert not list(target.parent.glob("*.import_backup"))


def test_browser_restore_stages_profiles_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    root = paths.STORAGE_DIR / "browser_profiles"
    good_target = root / "chrome_Good"
    bad_target = root / "chrome_Bad"
    for target in (good_target, bad_target):
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("original", encoding="utf-8")

    good_content = b"good-cookie"
    browser_data = {
        "Good": {
            "profile": {"name": "Good", "browser_type": "chrome", "user_data_dir": str(good_target)},
            "files": [{
                "path": "Default/Network/Cookies",
                "size": len(good_content),
                "compression": "none",
                "data": base64.b64encode(good_content).decode("ascii"),
            }],
        },
        "Bad": {
            "profile": {"name": "Bad", "browser_type": "chrome", "user_data_dir": str(bad_target)},
            "file_count": 2,
            "files": [{
                "path": "Default/Network/Cookies",
                "size": 3,
                "compression": "none",
                "data": base64.b64encode(b"bad").decode("ascii"),
            }],
        },
    }

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(browser_data)

    assert restored_files == 1
    assert restored_bytes == len(good_content)
    assert restored_profiles == {"Good"}
    assert any("Bad: 恢复失败" in item for item in skipped)
    assert any("文件数量与声明不一致" in item for item in skipped)
    assert not (good_target / "keep.txt").exists()
    assert (good_target / "Default" / "Network" / "Cookies").read_bytes() == good_content
    assert (bad_target / "keep.txt").read_text(encoding="utf-8") == "original"
    assert not (bad_target / "Default" / "Network" / "Cookies").exists()


def test_browser_restore_rejects_duplicate_paths_before_replacing_original(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_Duplicate"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    encoded = base64.b64encode(b"one").decode("ascii")
    browser_data = {
        "Duplicate": {
            "profile": {"name": "Duplicate", "browser_type": "chrome", "user_data_dir": str(target)},
            "file_count": 2,
            "files": [
                {"path": "Default/Network/Cookies", "size": 3, "compression": "none", "data": encoded},
                {"path": "default/network/cookies", "size": 3, "compression": "none", "data": encoded},
            ],
        },
    }

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(browser_data)

    assert restored_files == 0
    assert restored_bytes == 0
    assert not restored_profiles
    assert any("重复的浏览器文件路径" in item for item in skipped)
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not list(target.parent.glob("*.import_staging"))
    assert not list(target.parent.glob("*.import_backup"))


def test_portable_browser_export_keeps_leveldb_log_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "source"
    leveldb_log = source / "Default" / "Local Storage" / "leveldb" / "000003.log"
    leveldb_log.parent.mkdir(parents=True)
    leveldb_log.write_bytes(b"live-local-storage")
    store = {
        "browser_profiles": [{
            "name": "LogOnly",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": str(source),
        }],
    }

    browser_data, skipped, file_count, total_bytes = portable_migration._collect_browser_profile_data(store)

    assert file_count == 1
    assert total_bytes == len(b"live-local-storage")
    assert len(store["browser_profiles"]) == 1
    assert not skipped
    assert browser_data["LogOnly"]["files"][0]["path"].endswith("000003.log")


def test_portable_browser_export_only_keeps_default_login_state(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "source"
    included = {
        "Local State": b"encryption-key",
        "Default/Network/Cookies": b"cookies",
        "Default/Local Storage/leveldb/data.ldb": b"local-storage",
        "Default/IndexedDB/site/index.data": b"indexed-db",
        "Default/Session Storage/session.ldb": b"session-storage",
        "Default/WebStorage/QuotaManager": b"web-storage",
        "Default/Storage/ext/state": b"extension-storage",
        "Default/Service Worker/Database/000003.log": b"service-worker-db",
        "Default/Preferences": b"preferences",
        "Default/Secure Preferences": b"secure-preferences",
        "Default/Login Data": b"login-data",
    }
    excluded = {
        "Default/Cache/cache.bin": b"cache",
        "Default/Storage/site/CacheStorage/cache/data": b"nested-cache-storage",
        "Default/WebStorage/site/DawnGraphiteCache/data": b"nested-graphite-cache",
        "Default/WebStorage/site/DawnWebGPUCache/data": b"nested-webgpu-cache",
        "Default/IndexedDB/site/ScriptCache/script": b"nested-script-cache",
        "Default/Local Storage/site/Media Cache/media": b"nested-media-cache",
        "Default/History": b"history",
        "Default/Extensions/demo/extension.js": b"extension",
        "Default/Media History": b"media",
        "Default/DawnCache/data.bin": b"dawn-cache",
        "Default/Service Worker/CacheStorage/cache/data": b"service-worker-cache",
        "Default/Service Worker/ScriptCache/script": b"service-worker-script",
        "component_crx_cache/component.crx": b"component-cache",
        "ProvenanceData/1/model.ort": b"rebuildable-model",
        "Profile 1/Network/Cookies": b"unused-profile",
        "Other Root File": b"runtime-data",
    }
    for relative_path, content in {**included, **excluded}.items():
        path = source / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    store = {
        "browser_profiles": [{
            "name": "Portable",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": str(source),
        }],
    }

    browser_data, skipped, file_count, total_bytes = portable_migration._collect_browser_profile_data(store)

    exported_paths = {
        entry["path"]
        for entry in browser_data["Portable"]["files"]
    }
    assert exported_paths == set(included)
    assert file_count == len(included)
    assert total_bytes == sum(len(content) for content in included.values())
    assert not skipped


@pytest.mark.parametrize("blocked_name", ["source", "Default", "Cookies"])
def test_portable_browser_export_rejects_reparse_components(tmp_path, monkeypatch, blocked_name):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    source = tmp_path / "source"
    cookie = source / "Default" / "Network" / "Cookies"
    cookie.parent.mkdir(parents=True)
    cookie.write_bytes(b"cookie")
    original_check = portable_migration._path_is_reparse_point

    def simulated_reparse(path):
        return Path(path).name == blocked_name or original_check(path)

    monkeypatch.setattr(portable_migration, "_path_is_reparse_point", simulated_reparse)
    store = {
        "browser_profiles": [{
            "name": "Linked",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": str(source),
        }],
    }

    browser_data, skipped, file_count, total_bytes = portable_migration._collect_browser_profile_data(store)

    assert browser_data == {}
    assert file_count == 0
    assert total_bytes == 0
    assert skipped
    assert store["browser_profiles"] == []


def test_portable_browser_export_uses_bounded_read_and_rechecks_actual_size(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(portable_migration, "MAX_BROWSER_FILE_BYTES", 8)
    source = tmp_path / "source"
    cookie = source / "Default" / "Network" / "Cookies"
    cookie.parent.mkdir(parents=True)
    cookie.write_bytes(b"x")
    original_open = Path.open
    read_sizes: list[int] = []

    class GrowingFile(BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return b"x" * 9

    def controlled_open(path, mode="r", *args, **kwargs):
        if Path(path) == cookie and mode == "rb":
            return GrowingFile()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", controlled_open)
    store = {
        "browser_profiles": [{
            "name": "Growing",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": str(source),
        }],
    }

    browser_data, skipped, file_count, total_bytes = portable_migration._collect_browser_profile_data(store)

    assert read_sizes == [portable_migration.MAX_BROWSER_FILE_BYTES + 1]
    assert browser_data == {}
    assert file_count == 0
    assert total_bytes == 0
    assert any("读取后文件过大" in item for item in skipped)


def test_portable_browser_export_does_not_treat_empty_path_as_working_directory():
    store = {
        "browser_profiles": [{
            "name": "Broken",
            "browser_type": "chrome",
            "profile_mode": "managed",
            "user_data_dir": "",
        }],
    }

    browser_data, skipped, file_count, total_bytes = portable_migration._collect_browser_profile_data(store)

    assert browser_data == {}
    assert file_count == 0
    assert total_bytes == 0
    assert skipped == ["Broken: Profile 路径为空"]
    assert store["browser_profiles"] == []


def test_portable_export_rejects_live_profile_store_and_browser_source_paths(tmp_path, monkeypatch):
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text("keep-profile-store", encoding="utf-8")
    profiles_backup = profiles_file.with_suffix(".backup")
    profiles_backup.write_text("keep-profile-backup", encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    browser_dir = tmp_path / "browser"
    browser_file = browser_dir / "Default" / "Preferences"
    browser_file.parent.mkdir(parents=True)
    browser_file.write_text("keep-browser-data", encoding="utf-8")
    store = profile_manager._get_default_store()
    store["browser_profiles"] = [{
        "name": "Browser",
        "browser_type": "chrome",
        "profile_mode": "managed",
        "user_data_dir": str(browser_dir),
    }]
    store["ssh_profiles"] = [{"name": "Server", "password_ref": "ssh:server"}]
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "_load_store", lambda: store)
    monkeypatch.setattr(paths, "SECRETS_DIR", secrets_dir)

    with pytest.raises(ValueError, match="不能覆盖当前 Profile"):
        portable_migration.export_portable_profiles(profiles_file, "strong-password")
    with pytest.raises(ValueError, match="不能覆盖当前 Profile"):
        portable_migration.export_portable_profiles(profiles_backup, "strong-password")
    with pytest.raises(ValueError, match="密钥存储目录"):
        portable_migration.export_portable_profiles(secrets_dir / "bundle.asxprofile", "strong-password")
    with pytest.raises(ValueError, match="浏览器 Profile"):
        portable_migration.export_portable_profiles(browser_file, "strong-password")
    with pytest.raises(ValueError, match="浏览器 Profile"):
        portable_migration.export_portable_profiles(
            browser_file,
            "strong-password",
            selection={"ssh_profiles": {"Server"}},
        )

    assert profiles_file.read_text(encoding="utf-8") == "keep-profile-store"
    assert profiles_backup.read_text(encoding="utf-8") == "keep-profile-backup"
    assert browser_file.read_text(encoding="utf-8") == "keep-browser-data"


def test_portable_browser_sanitized_name_collisions_get_distinct_directories(tmp_path, monkeypatch):
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    (source_a / "Default" / "Network").mkdir(parents=True)
    (source_b / "Default" / "Network").mkdir(parents=True)
    (source_a / "Default" / "Network" / "marker.txt").write_text("FIRST", encoding="utf-8")
    (source_b / "Default" / "Network" / "marker.txt").write_text("SECOND", encoding="utf-8")
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "export_storage")
    store = {
        "browser_profiles": [
            {
                "name": "A/B",
                "browser_type": "chrome",
                "profile_mode": "managed",
                "user_data_dir": str(source_a),
            },
            {
                "name": "A?B",
                "browser_type": "chrome",
                "profile_mode": "managed",
                "user_data_dir": str(source_b),
            },
        ],
    }
    browser_data, skipped, file_count, _total_bytes = portable_migration._collect_browser_profile_data(store)
    assert file_count == 2
    assert not skipped

    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "import_storage")
    prepared, profiles = portable_migration._prepare_imported_browser_data(browser_data)
    profile_paths = [Path(profile["user_data_dir"]) for profile in profiles]
    assert len(set(profile_paths)) == 2

    restored_files, _restored_bytes, restore_skips, restored_names = portable_migration._restore_browser_data(prepared)
    assert restored_files == 2
    assert not restore_skips
    assert restored_names == {"A/B", "A?B"}
    by_name = {profile["name"]: Path(profile["user_data_dir"]) for profile in profiles}
    assert (by_name["A/B"] / "Default" / "Network" / "marker.txt").read_text(encoding="utf-8") == "FIRST"
    assert (by_name["A?B"] / "Default" / "Network" / "marker.txt").read_text(encoding="utf-8") == "SECOND"


def test_portable_export_selection_filters_profiles_secrets_and_active_names(tmp_path, monkeypatch):
    browser_keep = tmp_path / "browser_keep"
    browser_drop = tmp_path / "browser_drop"
    for source, content in ((browser_keep, b"keep-cookie"), (browser_drop, b"drop-cookie")):
        cookie = source / "Default" / "Network" / "Cookies"
        cookie.parent.mkdir(parents=True)
        cookie.write_bytes(content)
        (source / "Local State").write_bytes(b"local-state")

    refs = {
        "claude:ClaudeKeep:auth_token": "claude-keep-secret",
        "claude:ClaudeDrop:auth_token": "claude-drop-secret",
        "codex:CodexKeep:api_key": "codex-keep-secret",
        "codex:CodexDrop:api_key": "codex-drop-secret",
        "ssh:SshKeep:password": "ssh-keep-secret",
        "ssh:SshDrop:password": "ssh-drop-secret",
        "unknown:claude": "must-not-export",
        "unknown:ssh": "must-not-export",
    }
    store = profile_manager._get_default_store()
    store["unexpected_top_level"] = {"private": "must-not-export"}
    store["claude_profiles"] = [
        {
            "name": "ClaudeKeep",
            "provider": "custom",
            "auth_token_ref": "claude:ClaudeKeep:auth_token",
            "surprise_ref": "unknown:claude",
            "unknown_profile_field": "must-not-export",
        },
        {"name": "ClaudeDrop", "provider": "custom", "auth_token_ref": "claude:ClaudeDrop:auth_token"},
    ]
    store["codex_profiles"] = [
        {"name": "CodexKeep", "model_provider": "custom", "api_key_ref": "codex:CodexKeep:api_key"},
        {"name": "CodexDrop", "model_provider": "custom", "api_key_ref": "codex:CodexDrop:api_key"},
    ]
    store["ssh_profiles"] = [
        {"name": "SshKeep", "password_ref": "ssh:SshKeep:password", "surprise_ref": "unknown:ssh"},
        {"name": "SshDrop", "password_ref": "ssh:SshDrop:password"},
    ]
    store["browser_profiles"] = [
        {"name": "BrowserKeep", "browser_type": "chrome", "profile_mode": "managed", "user_data_dir": str(browser_keep)},
        {"name": "BrowserDrop", "browser_type": "edge", "profile_mode": "managed", "user_data_dir": str(browser_drop)},
    ]
    store["active_claude_profile"] = "ClaudeKeep"
    store["active_codex_profile"] = "CodexDrop"
    store["active_ssh_profile"] = "SshKeep"
    store["active_browser_profile"] = "BrowserDrop"

    secret_reads: list[str] = []

    def get_secret(ref: str):
        secret_reads.append(ref)
        return refs.get(ref)

    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "export_storage")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profile_manager, "_load_store", lambda: store)
    monkeypatch.setattr(security, "get_secret", get_secret)
    selection = {
        "claude_profiles": {"ClaudeKeep"},
        "codex_profiles": {"CodexKeep"},
        "ssh_profiles": {"SshKeep"},
        "browser_profiles": {"BrowserKeep"},
    }

    selected_bundle = tmp_path / "selected.asxprofile"
    result = portable_migration.export_portable_profiles(
        selected_bundle,
        "strong-password",
        selection=selection,
    )
    selected_payload = portable_migration._decrypt_bundle(
        json.loads(selected_bundle.read_text(encoding="utf-8")),
        "strong-password",
    )
    selected_store = selected_payload["store"]

    assert result.profile_count == 4
    assert result.secret_count == 3
    selected_refs = {
        "claude:ClaudeKeep:auth_token",
        "codex:CodexKeep:api_key",
        "ssh:SshKeep:password",
    }
    assert secret_reads == sorted(selected_refs)
    assert set(selected_payload["secrets"]) == selected_refs
    assert set(selected_payload["browser_data"]) == {"BrowserKeep"}
    assert "source_path" not in selected_payload["browser_data"]["BrowserKeep"]
    assert set(selected_store) == {
        "version",
        *profile_manager.PROFILE_LIST_KEYS,
        *profile_manager.ACTIVE_PROFILE_KEYS,
    }
    assert "surprise_ref" not in selected_store["claude_profiles"][0]
    assert "unknown_profile_field" not in selected_store["claude_profiles"][0]
    assert "surprise_ref" not in selected_store["ssh_profiles"][0]
    assert [item["name"] for item in selected_store["claude_profiles"]] == ["ClaudeKeep"]
    assert [item["name"] for item in selected_store["codex_profiles"]] == ["CodexKeep"]
    assert [item["name"] for item in selected_store["ssh_profiles"]] == ["SshKeep"]
    assert [item["name"] for item in selected_store["browser_profiles"]] == ["BrowserKeep"]
    assert selected_store["active_claude_profile"] == "ClaudeKeep"
    assert selected_store["active_codex_profile"] is None
    assert selected_store["active_ssh_profile"] == "SshKeep"
    assert selected_store["active_browser_profile"] is None

    secret_reads.clear()
    full_bundle = tmp_path / "full.asxprofile"
    full_result = portable_migration.export_portable_profiles(full_bundle, "strong-password")
    full_payload = portable_migration._decrypt_bundle(
        json.loads(full_bundle.read_text(encoding="utf-8")),
        "strong-password",
    )

    assert full_result.profile_count == 8
    assert full_result.secret_count == 6
    assert set(secret_reads) == set(refs) - {"unknown:claude", "unknown:ssh"}
    assert set(full_payload["browser_data"]) == {"BrowserKeep", "BrowserDrop"}
    assert full_payload["store"]["active_codex_profile"] == "CodexDrop"
    assert full_payload["store"]["active_browser_profile"] == "BrowserDrop"


def test_portable_export_selection_rejects_invalid_or_empty_selection(tmp_path, monkeypatch):
    store = profile_manager._get_default_store()
    store["ssh_profiles"] = [{"name": "Server", "password_ref": "ssh:Server:password"}]
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profile_manager, "_load_store", lambda: store)

    with pytest.raises(ValueError, match="不支持的类型"):
        portable_migration.export_portable_profiles(
            tmp_path / "unknown.asxprofile",
            "strong-password",
            selection={"unknown": {"Server"}},
        )
    with pytest.raises(ValueError, match="没有可导出"):
        portable_migration.export_portable_profiles(
            tmp_path / "empty.asxprofile",
            "strong-password",
            selection={},
        )


def test_portable_profile_transactions_hold_store_lock_through_write_and_rollback(
    tmp_path,
    monkeypatch,
):
    class TrackingLock:
        def __init__(self):
            self.depth = 0
            self.enter_count = 0

        def __enter__(self):
            self.depth += 1
            self.enter_count += 1
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            self.depth -= 1

    lock = TrackingLock()
    store = profile_manager._get_default_store()
    store["ssh_profiles"] = [{"name": "Server", "password_ref": "ssh:Server:password"}]
    profiles_file = tmp_path / "profiles.json"
    bundle = tmp_path / "profiles.asxprofile"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "_STORE_CACHE_LOCK", lock)

    def load_store():
        assert lock.depth == 1
        return store

    def get_secret(_ref):
        assert lock.depth == 1
        return None

    original_atomic_write = portable_migration.atomic_write_text

    def locked_atomic_write(*args, **kwargs):
        assert lock.depth == 1
        return original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(profile_manager, "_load_store", load_store)
    monkeypatch.setattr(security, "get_secret", get_secret)
    monkeypatch.setattr(security, "get_secret_strict", get_secret)
    monkeypatch.setattr(portable_migration, "atomic_write_text", locked_atomic_write)

    portable_migration.export_portable_profiles(bundle, "strong-password")

    save_seen = False
    rollback_seen = False

    def save_then_fail(_store):
        nonlocal save_seen
        assert lock.depth == 1
        save_seen = True
        raise OSError("forced locked save failure")

    def locked_rollback(*_args, **_kwargs):
        nonlocal rollback_seen
        assert lock.depth == 1
        rollback_seen = True
        return []

    monkeypatch.setattr(profile_manager, "_save_store", save_then_fail)
    monkeypatch.setattr(portable_migration, "_rollback_portable_import", locked_rollback)

    with pytest.raises(OSError, match="forced locked save failure"):
        portable_migration.import_portable_profiles(bundle, "strong-password")

    assert save_seen
    assert rollback_seen
    assert lock.enter_count == 2
    assert lock.depth == 0


def test_portable_payload_decompression_is_bounded(monkeypatch):
    encrypted = portable_migration._encrypt_payload(
        {"payload_version": 1, "data": "x" * 1024},
        "strong-password",
    )
    monkeypatch.setattr(portable_migration, "MAX_DECRYPTED_PAYLOAD_BYTES", 128)

    with pytest.raises(ValueError, match="解密后内容过大"):
        portable_migration._decrypt_bundle(encrypted, "strong-password")


def test_portable_payload_rejects_excessive_kdf_and_bad_nonce_or_salt():
    encrypted = portable_migration._encrypt_payload(
        {"payload_version": 1},
        "strong-password",
    )

    excessive_kdf = json.loads(json.dumps(encrypted))
    excessive_kdf["kdf"]["iterations"] = portable_migration.MAX_KDF_ITERATIONS + 1
    with pytest.raises(ValueError, match="KDF 参数异常"):
        portable_migration._decrypt_bundle(excessive_kdf, "strong-password")

    short_salt = json.loads(json.dumps(encrypted))
    short_salt["kdf"]["salt"] = base64.b64encode(b"short").decode("ascii")
    with pytest.raises(ValueError, match="加密参数异常"):
        portable_migration._decrypt_bundle(short_salt, "strong-password")

    short_nonce = json.loads(json.dumps(encrypted))
    short_nonce["cipher"]["nonce"] = base64.b64encode(b"short").decode("ascii")
    with pytest.raises(ValueError, match="加密参数异常"):
        portable_migration._decrypt_bundle(short_nonce, "strong-password")


def test_portable_browser_restore_rejects_oversized_encoded_data_before_decode(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_Bounded"
    browser_data = {
        "Bounded": {
            "profile": {
                "name": "Bounded",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "files": [{
                "path": "Default/Local Storage/leveldb/000003.log",
                "size": 1,
                "compression": "zlib",
                "data": "A" * 1000,
            }],
        },
    }
    decode_calls = 0
    original_decode = portable_migration._b64decode

    def counted_decode(value):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(value)

    monkeypatch.setattr(portable_migration, "_b64decode", counted_decode)

    restored_files, restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(
        browser_data
    )

    assert decode_calls == 0
    assert restored_files == 0
    assert restored_bytes == 0
    assert not restored_profiles
    assert any("声明大小边界" in item for item in skipped)
    assert not target.exists()


def test_portable_browser_restore_does_not_delete_original_when_initial_move_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_MoveFailure"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    browser_data = {
        "MoveFailure": {
            "profile": {
                "name": "MoveFailure",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "files": [{
                "path": "Default/Preferences",
                "size": 3,
                "compression": "none",
                "data": base64.b64encode(b"new").decode("ascii"),
            }],
        },
    }
    monkeypatch.setattr(
        portable_migration,
        "_move_browser_path",
        lambda _source, _target: (_ for _ in ()).throw(OSError("forced initial move failure")),
    )

    restored_files, _restored_bytes, skipped, restored_profiles = portable_migration._restore_browser_data(
        browser_data
    )

    assert restored_files == 0
    assert not restored_profiles
    assert any("forced initial move failure" in item for item in skipped)
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not list(target.parent.glob("*.import_backup"))


def test_portable_browser_restore_surfaces_failed_original_directory_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_RollbackFailure"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("original", encoding="utf-8")
    browser_data = {
        "RollbackFailure": {
            "profile": {
                "name": "RollbackFailure",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "files": [{
                "path": "Default/Preferences",
                "size": 3,
                "compression": "none",
                "data": base64.b64encode(b"new").decode("ascii"),
            }],
        },
    }
    original_move = portable_migration._move_browser_path
    move_calls = 0

    def fail_restore_move(source, destination):
        nonlocal move_calls
        move_calls += 1
        if move_calls in {2, 3}:
            raise OSError("forced rollback move failure")
        return original_move(source, destination)

    monkeypatch.setattr(portable_migration, "_move_browser_path", fail_restore_move)

    with pytest.raises(RuntimeError, match="原目录自动恢复失败"):
        portable_migration._restore_browser_data(browser_data)

    assert not target.exists()
    [recovery] = list(target.parent.glob("*.import_backup"))
    assert (recovery / "keep.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "relative_path",
    [
        "C:/outside.txt",
        "C:\\outside.txt",
        "C:drive-relative.txt",
        "Default/file.txt:alternate-stream",
        "//server/share/outside.txt",
    ],
)
def test_portable_browser_relative_path_rejects_windows_escape_forms(relative_path):
    with pytest.raises(ValueError, match="非法浏览器文件路径"):
        portable_migration._safe_browser_relative_path(relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "Default/Cache/cache.bin",
        "Default/History",
        "Default/Extensions/attacker/manifest.json",
        "Default/Service Worker/CacheStorage/cache/data",
        "Default/WebStorage/site/DawnWebGPUCache/data",
        "Default/arbitrary-runtime.log",
    ],
)
def test_portable_browser_restore_rejects_paths_outside_export_allowlist(
    relative_path,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(paths, "STORAGE_DIR", tmp_path / "storage")
    target = paths.STORAGE_DIR / "browser_profiles" / "chrome_PathAudit"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    content = b"must-not-install"
    browser_data = {
        "PathAudit": {
            "profile": {
                "name": "PathAudit",
                "browser_type": "chrome",
                "user_data_dir": str(target),
            },
            "file_count": 1,
            "files": [{
                "path": relative_path,
                "size": len(content),
                "compression": "none",
                "data": base64.b64encode(content).decode("ascii"),
            }],
        },
    }

    restored_files, restored_bytes, skipped, restored_profiles = (
        portable_migration._restore_browser_data(browser_data)
    )

    assert restored_files == 0
    assert restored_bytes == 0
    assert not restored_profiles
    assert any("不在迁移白名单" in item for item in skipped)
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not list(target.parent.glob("*.import_staging"))
    assert not list(target.parent.glob("*.import_backup"))


def test_portable_import_rejects_package_inside_browser_target_before_mutation(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    target = storage_dir / "browser_profiles" / "chrome_InsideTarget"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    profile = BrowserProfile(
        name="InsideTarget",
        browser_type="chrome",
        profile_mode="managed",
        user_data_dir="ignored-on-import",
    ).to_dict()
    content = b"new-cookie"
    payload = {
        "payload_version": 1,
        "store": profile_manager._get_default_store(),
        "secrets": {},
        "browser_data": {
            "InsideTarget": {
                "profile": profile,
                "files": [{
                    "path": "Default/Network/Cookies",
                    "size": len(content),
                    "compression": "none",
                    "data": base64.b64encode(content).decode("ascii"),
                }],
            },
        },
    }
    package = target / "inside.asxprofile"
    package.write_text(
        json.dumps(portable_migration._encrypt_payload(payload, "strong-password")),
        encoding="utf-8",
    )

    def unexpected_mutation(*_args, **_kwargs):
        raise AssertionError("目标路径校验前不应发生任何导入变更")

    monkeypatch.setattr(portable_migration, "_restore_browser_data", unexpected_mutation)
    monkeypatch.setattr(profile_manager, "_save_store", unexpected_mutation)
    monkeypatch.setattr(security, "set_secret", unexpected_mutation)

    with pytest.raises(ValueError, match="待替换的浏览器 Profile"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert package.exists()
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not (target / "Default" / "Network" / "Cookies").exists()
    assert not list(target.parent.glob("*.import_staging"))
    assert not list(target.parent.glob("*.import_backup"))


def test_portable_import_rolls_back_browser_secret_and_profiles_after_save_failure(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    secret_values = {
        "claude:Imported:auth_token": "old-secret",
        "claude:Existing:auth_token": "existing-secret",
    }
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))

    profile_manager.save_claude_profile(ClaudeProfile(
        name="Existing",
        auth_token_ref="claude:Existing:auth_token",
        base_url="https://existing.example.test",
        model="existing-model",
        provider="custom",
    ))
    profiles_before = profiles_file.read_bytes()

    target = storage_dir / "browser_profiles" / "chrome_ImportedBrowser"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("original-browser-data", encoding="utf-8")

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Imported",
        auth_token_ref="claude:Imported:auth_token",
        base_url="https://imported.example.test",
        model="imported-model",
        provider="custom",
    ).to_dict()]
    browser_profile = BrowserProfile(
        name="ImportedBrowser",
        browser_type="chrome",
        profile_mode="managed",
        user_data_dir="ignored-on-import",
        start_target="chatgpt",
        allow_full_reset=True,
        created_by_app=True,
    ).to_dict()
    imported_store["browser_profiles"] = [browser_profile]
    imported_data = b"imported-browser-data"
    payload = {
        "payload_version": 1,
        "store": imported_store,
        "secrets": {"claude:Imported:auth_token": "new-secret"},
        "browser_data": {
            "ImportedBrowser": {
                "profile": browser_profile,
                "files": [{
                    "path": "Default/Preferences",
                    "size": len(imported_data),
                    "compression": "zlib",
                    "data": base64.b64encode(zlib.compress(imported_data)).decode("ascii"),
                }],
            },
        },
    }
    package = tmp_path / "rollback.asxprofile"
    package.write_text(
        json.dumps(portable_migration._encrypt_payload(payload, "strong-password")),
        encoding="utf-8",
    )

    original_save = profile_manager._save_store

    def save_then_fail(store, *args, **kwargs):
        original_save(store, *args, **kwargs)
        raise OSError("forced profile save failure")

    monkeypatch.setattr(profile_manager, "_save_store", save_then_fail)

    with pytest.raises(OSError, match="forced profile save failure"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert profiles_file.read_bytes() == profiles_before
    assert sentinel.read_text(encoding="utf-8") == "original-browser-data"
    assert not (target / "Default" / "Preferences").exists()
    assert not list(target.parent.glob("*.import_backup"))
    assert secret_values["claude:Imported:auth_token"] == "old-secret"
    assert {profile.name for profile in profile_manager.list_claude_profiles()} == {"Existing"}
    profile_manager.clear_profile_store_cache()


def test_portable_import_does_not_overwrite_unreferenced_secret(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    imported_ref = "claude:Imported:auth_token"
    unrelated_ref = "claude:Existing:auth_token"
    secret_values = {unrelated_ref: "keep-existing-secret"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))

    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_claude_profile(ClaudeProfile(
        name="Existing",
        auth_token_ref=unrelated_ref,
        base_url="https://existing.example.test",
        model="existing-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Imported",
        auth_token_ref=imported_ref,
        base_url="https://imported.example.test",
        model="imported-model",
        provider="custom",
    ).to_dict()]
    payload = {
        "payload_version": 1,
        "store": imported_store,
        "secrets": {
            imported_ref: "imported-secret",
            unrelated_ref: "attacker-controlled-secret",
        },
        "browser_data": {},
    }
    package = tmp_path / "unreferenced-secret.asxprofile"
    package.write_text(
        json.dumps(portable_migration._encrypt_payload(payload, "strong-password")),
        encoding="utf-8",
    )

    result = portable_migration.import_portable_profiles(package, "strong-password")

    assert result.secret_count == 1
    assert f"{unrelated_ref} (未被导入配置引用)" in result.skipped_secret_refs
    assert secret_values[imported_ref] == "imported-secret"
    assert secret_values[unrelated_ref] == "keep-existing-secret"
    assert {profile.name for profile in profile_manager.list_claude_profiles()} == {
        "Existing",
        "Imported",
    }
    profile_manager.clear_profile_store_cache()


def test_portable_import_rejects_ref_owned_by_unreplaced_profile(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    shared_ref = "claude:Existing:auth_token"
    secret_values = {shared_ref: "keep-existing-secret"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_claude_profile(ClaudeProfile(
        name="Existing",
        auth_token_ref=shared_ref,
        base_url="https://existing.example.test",
        model="existing-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Imported",
        auth_token_ref=shared_ref,
        base_url="https://imported.example.test",
        model="imported-model",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "conflicting-secret-ref.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {shared_ref: "attacker-controlled-secret"},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    with pytest.raises(ValueError, match="密钥引用与未替换的现有配置冲突"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert secret_values[shared_ref] == "keep-existing-secret"
    assert {profile.name for profile in profile_manager.list_claude_profiles()} == {"Existing"}
    profile_manager.clear_profile_store_cache()


def test_portable_import_allows_same_name_profile_to_replace_its_secret(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    shared_ref = "claude:Same:auth_token"
    secret_values = {shared_ref: "old-secret"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_claude_profile(ClaudeProfile(
        name="Same",
        auth_token_ref=shared_ref,
        base_url="https://old.example.test",
        model="old-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Same",
        auth_token_ref=shared_ref,
        base_url="https://new.example.test",
        model="new-model",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "same-name-secret-ref.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {shared_ref: "new-secret"},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    result = portable_migration.import_portable_profiles(package, "strong-password")

    assert result.secret_count == 1
    assert secret_values[shared_ref] == "new-secret"
    assert profile_manager.list_claude_profiles()[0].model == "new-model"
    profile_manager.clear_profile_store_cache()


def test_portable_import_clears_stale_same_name_secret_missing_from_source(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    shared_ref = "claude:SameMissing:auth_token"
    secret_values = {shared_ref: "old-secret-that-must-not-survive"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_claude_profile(ClaudeProfile(
        name="SameMissing",
        auth_token_ref=shared_ref,
        base_url="https://old.example.test",
        model="old-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="SameMissing",
        auth_token_ref=shared_ref,
        base_url="https://new.example.test",
        model="new-model",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "same-name-missing-secret.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {},
        "missing_secret_refs": [shared_ref],
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    result = portable_migration.import_portable_profiles(package, "strong-password")

    assert shared_ref not in secret_values
    assert result.secret_count == 0
    assert any(
        item == f"{shared_ref} (源包缺少密钥，已清除本机旧值)"
        for item in result.skipped_secret_refs
    )
    assert profile_manager.list_claude_profiles()[0].model == "new-model"
    profile_manager.clear_profile_store_cache()


def test_portable_missing_secret_clear_rolls_back_after_profile_save_failure(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    shared_ref = "claude:MissingRollback:auth_token"
    secret_values = {shared_ref: "old-secret"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_claude_profile(ClaudeProfile(
        name="MissingRollback",
        auth_token_ref=shared_ref,
        base_url="https://old.example.test",
        model="old-model",
        provider="custom",
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="MissingRollback",
        auth_token_ref=shared_ref,
        base_url="https://new.example.test",
        model="new-model",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "missing-secret-rollback.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    original_save = profile_manager._save_store

    def fail_imported_save(store, *args, **kwargs):
        imported_profiles = store.get("claude_profiles", [])
        if any(item.get("model") == "new-model" for item in imported_profiles):
            raise OSError("forced missing-secret save failure")
        return original_save(store, *args, **kwargs)

    monkeypatch.setattr(profile_manager, "_save_store", fail_imported_save)

    with pytest.raises(OSError, match="forced missing-secret save failure"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert secret_values[shared_ref] == "old-secret"
    profile_manager.clear_profile_store_cache()
    assert profile_manager.list_claude_profiles()[0].model == "old-model"


def test_portable_import_removes_obsolete_replaced_profile_secret(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    old_ref = "ssh:Same:password"
    secret_values = {old_ref: "old-password"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_ssh_profile(SSHProfile(
        name="Same",
        host="old.example.test",
        auth_type="password",
        password_ref=old_ref,
    ))

    imported_store = profile_manager._get_default_store()
    imported_store["ssh_profiles"] = [SSHProfile(
        name="Same",
        host="new.example.test",
        auth_type="key",
        private_key_path="C:/keys/id_ed25519",
    ).to_dict()]
    package = tmp_path / "obsolete-secret.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    portable_migration.import_portable_profiles(package, "strong-password")

    assert old_ref not in secret_values
    [profile] = profile_manager.list_ssh_profiles()
    assert profile.host == "new.example.test"
    assert profile.auth_type == "key"
    profile_manager.clear_profile_store_cache()


def test_portable_obsolete_secret_delete_rolls_back_after_profile_save_failure(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()

    old_ref = "ssh:Rollback:password"
    secret_values = {old_ref: "old-password"}
    deleted_refs: list[str] = []
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))

    def delete_secret(ref):
        deleted_refs.append(ref)
        secret_values.pop(ref, None)

    monkeypatch.setattr(security, "delete_secret", delete_secret)
    profile_manager._save_store(profile_manager._get_default_store())
    profile_manager.save_ssh_profile(SSHProfile(
        name="Rollback",
        host="old.example.test",
        auth_type="password",
        password_ref=old_ref,
    ))
    profiles_before = profiles_file.read_bytes()

    imported_store = profile_manager._get_default_store()
    imported_store["ssh_profiles"] = [SSHProfile(
        name="Rollback",
        host="new.example.test",
        auth_type="key",
        private_key_path="C:/keys/id_ed25519",
    ).to_dict()]
    package = tmp_path / "obsolete-secret-rollback.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    original_save = profile_manager._save_store

    def save_then_fail(store, *args, **kwargs):
        original_save(store, *args, **kwargs)
        if any(
            item.get("host") == "new.example.test"
            for item in store.get("ssh_profiles", [])
        ):
            raise OSError("forced obsolete-secret save failure")

    monkeypatch.setattr(profile_manager, "_save_store", save_then_fail)

    with pytest.raises(OSError, match="forced obsolete-secret save failure"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert old_ref in deleted_refs
    assert secret_values[old_ref] == "old-password"
    assert profiles_file.read_bytes() == profiles_before
    profile_manager.clear_profile_store_cache()
    assert profile_manager.list_ssh_profiles()[0].host == "old.example.test"


def test_portable_import_rejects_cross_namespace_ref_before_overwriting_unowned_secret(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "storage"
    profiles_file = storage_dir / "profiles.json"
    monkeypatch.setattr(paths, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(paths, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_file)
    profile_manager.clear_profile_store_cache()
    profile_manager._save_store(profile_manager._get_default_store())

    unowned_ref = "network-diagnostics:audit:0"
    secret_values = {unowned_ref: "unrelated-local-secret"}
    monkeypatch.setattr(security, "get_secret", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "get_secret_strict", lambda ref: secret_values.get(ref))
    monkeypatch.setattr(security, "set_secret", lambda ref, value: secret_values.__setitem__(ref, value))
    monkeypatch.setattr(security, "delete_secret", lambda ref: secret_values.pop(ref, None))

    imported_store = profile_manager._get_default_store()
    imported_store["claude_profiles"] = [ClaudeProfile(
        name="Collision",
        auth_token_ref=unowned_ref,
        base_url="https://collision.example.test",
        provider="custom",
    ).to_dict()]
    package = tmp_path / "unowned-missing-secret.asxprofile"
    package.write_text(json.dumps(portable_migration._encrypt_payload({
        "payload_version": 1,
        "store": imported_store,
        "secrets": {unowned_ref: "attacker-controlled-secret"},
        "browser_data": {},
    }, "strong-password")), encoding="utf-8")

    with pytest.raises(ValueError, match="密钥引用不属于字段 auth_token_ref"):
        portable_migration.import_portable_profiles(package, "strong-password")

    assert secret_values[unowned_ref] == "unrelated-local-secret"
    assert profile_manager.list_claude_profiles() == []
    profile_manager.clear_profile_store_cache()


@pytest.mark.parametrize(
    ("list_key", "profile"),
    [
        (
            "claude_profiles",
            {"name": "Claude", "auth_token_ref": "network-diagnostics:proxycheck:0"},
        ),
        (
            "claude_profiles",
            {"name": "Claude", "primary_api_key_ref": "claude:Owner:auth_token"},
        ),
        (
            "codex_profiles",
            {"name": "Codex", "api_key_ref": "claude:Owner:auth_token"},
        ),
        (
            "ssh_profiles",
            {"name": "SSH", "password_ref": "ssh:Owner:key_passphrase"},
        ),
        (
            "ssh_profiles",
            {"name": "SSH", "private_key_passphrase_ref": "ssh:Owner:password"},
        ),
    ],
)
def test_portable_secret_ref_fields_reject_cross_namespace_or_cross_field_refs(
    list_key,
    profile,
):
    store = profile_manager._get_default_store()
    store[list_key] = [profile]

    with pytest.raises(ValueError, match="密钥引用不属于字段"):
        portable_migration._validate_portable_secret_refs(store)


class _RemoteAttr:
    def __init__(self, filename: str, mode: int, size: int = 0, mtime: int = 1_779_000_000):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _RemoteReader(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _RemoteWriter:
    def __init__(self, sftp, path: str):
        self.sftp = sftp
        self.path = path
        self.buffer = bytearray()

    def write(self, data):
        self.buffer.extend(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.sftp.add_file(self.path, bytes(self.buffer))


class _SessionSFTP:
    def __init__(self, home: str = "/home/test"):
        self.home = home
        self.files: dict[str, bytes] = {}
        self.dirs = {"/"}
        self.mkdir_calls = []
        self.chmod_calls = []
        self.closed = False
        self._ensure_dir(home)

    def _ensure_dir(self, path: str):
        normalized = posixpath.normpath(path)
        parts = [part for part in normalized.split("/") if part]
        current = "/"
        self.dirs.add(current)
        for part in parts:
            current = posixpath.join(current, part)
            self.dirs.add(current)

    def add_file(self, path: str, data: bytes | str):
        path = posixpath.normpath(path)
        self._ensure_dir(posixpath.dirname(path))
        self.files[path] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def get_channel(self):
        class Channel:
            def settimeout(self, _timeout):
                pass

        return Channel()

    def normalize(self, path):
        return self.home if path == "." else posixpath.normpath(path)

    def listdir_attr(self, path: str):
        path = posixpath.normpath(path)
        prefix = path.rstrip("/") + "/"
        children = {}
        for directory in self.dirs:
            if directory == path or not directory.startswith(prefix):
                continue
            name = directory[len(prefix):].split("/", 1)[0]
            children[name] = _RemoteAttr(name, stat.S_IFDIR | 0o700)
        for file_path, data in self.files.items():
            if not file_path.startswith(prefix):
                continue
            name = file_path[len(prefix):].split("/", 1)[0]
            if "/" in file_path[len(prefix):]:
                continue
            children[name] = _RemoteAttr(name, stat.S_IFREG | 0o600, len(data))
        return list(children.values())

    def stat(self, path: str):
        path = posixpath.normpath(path)
        if path in self.files:
            return _RemoteAttr(posixpath.basename(path), stat.S_IFREG | 0o600, len(self.files[path]))
        if path in self.dirs:
            return _RemoteAttr(posixpath.basename(path), stat.S_IFDIR | 0o700)
        raise FileNotFoundError(path)

    def open(self, path: str, mode: str):
        path = posixpath.normpath(path)
        if "r" in mode:
            if path not in self.files:
                raise FileNotFoundError(path)
            return _RemoteReader(self.files[path])
        return _RemoteWriter(self, path)

    def mkdir(self, path: str):
        self._ensure_dir(path)
        self.mkdir_calls.append(posixpath.normpath(path))

    def chmod(self, path: str, mode: int):
        self.chmod_calls.append((posixpath.normpath(path), mode))

    def rename(self, source: str, target: str):
        self.files[posixpath.normpath(target)] = self.files.pop(posixpath.normpath(source))

    def posix_rename(self, source: str, target: str):
        self.rename(source, target)

    def remove(self, path: str):
        self.files.pop(posixpath.normpath(path), None)

    def close(self):
        self.closed = True


class _BrokenReadSessionSFTP(_SessionSFTP):
    def __init__(self, home: str = "/home/test"):
        super().__init__(home)
        self.broken_reads: set[str] = set()

    def open(self, path: str, mode: str):
        normalized = posixpath.normpath(path)
        if "r" in mode and normalized in self.broken_reads:
            raise OSError("permission denied")
        return super().open(path, mode)


class _SessionSSHClient:
    def __init__(self, sftp: _SessionSFTP):
        self.sftp = sftp

    def open_sftp(self):
        return self.sftp

    def exec_command(self, _command, timeout=None):
        return None, _RemoteReader(self.sftp.home.encode("utf-8")), _RemoteReader(b"")


def _patch_session_ssh(monkeypatch, sftp: _SessionSFTP, name: str = "gpu"):
    profile = SSHProfile(name=name, host="gpu.example.com", auth_type="password", password_ref="ssh:gpu:password")
    client = _SessionSSHClient(sftp)
    monkeypatch.setattr(session_migration.profile_manager, "list_ssh_profiles", lambda: [profile])
    monkeypatch.setattr(session_migration.ssh_manager, "connect", lambda _profile: client)
    return client


def test_session_migration_supports_ssh_export_and_import(monkeypatch, tmp_path):
    source_sftp = _SessionSFTP()
    claude_file = "/home/test/.claude/projects/-home-test-proj/claude-session-remote.jsonl"
    source_sftp.add_file(
        claude_file,
        "\n".join([
            json.dumps({
                "type": "user",
                "timestamp": "2026-05-01T00:00:00Z",
                "sessionId": "claude-session-remote",
                "cwd": "/home/test/proj",
                "message": {"content": [{"type": "text", "text": "远端 Claude 会话"}]},
            }, ensure_ascii=False),
            json.dumps({
                "type": "assistant",
                "timestamp": "2026-05-01T00:01:00Z",
                "sessionId": "claude-session-remote",
                "message": {"model": "opus", "content": "ok"},
            }, ensure_ascii=False),
        ]) + "\n",
    )
    source_sftp.add_file("/home/test/.claude/projects/-home-test-proj/claude-session-remote/tool-results/result.txt", "remote tool")
    codex_file = "/home/test/.codex/sessions/2026/05/01/rollout-remote.jsonl"
    source_sftp.add_file(
        codex_file,
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-remote",
                    "timestamp": "2026-05-01T00:00:00Z",
                    "cwd": "/home/test/proj",
                    "model_provider": "openai",
                },
            }, ensure_ascii=False),
            json.dumps({
                "timestamp": "2026-05-01T00:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "远端 Codex 会话"}],
                },
            }, ensure_ascii=False),
        ]) + "\n",
    )
    source_sftp.add_file(
        "/home/test/.codex/session_index.jsonl",
        json.dumps({"id": "codex-remote", "thread_name": "远端 Codex 标题", "updated_at": "2026-05-01T00:02:00Z"}, ensure_ascii=False) + "\n",
    )
    _patch_session_ssh(monkeypatch, source_sftp)

    records = session_migration.list_sessions("all", ssh_name="gpu")

    assert {record.provider for record in records} == {"claude", "codex"}
    assert all(record.origin == "ssh" and record.ssh_name == "gpu" for record in records)
    assert any(record.summary == "远端 Claude 会话" for record in records)
    assert any(record.title == "远端 Codex 标题" for record in records)

    bundle = tmp_path / "remote.asxsession"
    exported = session_migration.export_remote_sessions("gpu", bundle, {record.key for record in records})

    assert exported.session_count == 2
    assert exported.file_count == 3

    imported = session_migration.import_sessions(
        bundle,
        claude_home=tmp_path / "local_claude",
        codex_home=tmp_path / "local_codex",
    )
    assert imported.session_count == 2
    assert (tmp_path / "local_claude" / "projects" / "-home-test-proj" / "claude-session-remote.jsonl").exists()
    assert (
        tmp_path
        / "local_claude"
        / "projects"
        / "-home-test-proj"
        / "claude-session-remote"
        / "tool-results"
        / "result.txt"
    ).read_text(encoding="utf-8") == "remote tool"

    target_sftp = _SessionSFTP()
    _patch_session_ssh(monkeypatch, target_sftp)
    remote_import = session_migration.import_sessions_to_ssh("gpu", bundle, target_project_path="/workspace/new")

    assert remote_import.session_count == 2
    remapped_claude = "/home/test/.claude/projects/-workspace-new/claude-session-remote.jsonl"
    assert remapped_claude in target_sftp.files
    assert json.loads(target_sftp.files[remapped_claude].decode("utf-8").splitlines()[0])["cwd"] == "/workspace/new"
    assert "/home/test/.codex/sessions/2026/05/01/rollout-remote.jsonl" in target_sftp.files
    codex_meta = json.loads(target_sftp.files["/home/test/.codex/sessions/2026/05/01/rollout-remote.jsonl"].decode("utf-8").splitlines()[0])
    assert codex_meta["payload"]["cwd"] == "/workspace/new"
    assert "远端 Codex 标题" in target_sftp.files["/home/test/.codex/session_index.jsonl"].decode("utf-8")


def test_remote_session_export_skips_unreadable_support_file(monkeypatch, tmp_path):
    sftp = _BrokenReadSessionSFTP()
    claude_file = "/home/test/.claude/projects/-home-test-proj/claude-session-remote.jsonl"
    support_file = "/home/test/.claude/projects/-home-test-proj/claude-session-remote/tool-results/result.txt"
    sftp.add_file(
        claude_file,
        json.dumps({
            "type": "user",
            "timestamp": "2026-05-01T00:00:00Z",
            "sessionId": "claude-session-remote",
            "cwd": "/home/test/proj",
            "message": {"content": "远端 Claude 会话"},
        }, ensure_ascii=False) + "\n",
    )
    sftp.add_file(support_file, "unreadable tool result")
    sftp.broken_reads.add(support_file)
    _patch_session_ssh(monkeypatch, sftp)

    records = session_migration.list_sessions("claude", ssh_name="gpu")
    bundle = tmp_path / "remote-unreadable.asxsession"
    exported = session_migration.export_remote_sessions("gpu", bundle, {record.key for record in records}, provider="claude")

    assert exported.session_count == 1
    assert exported.file_count == 1
    assert exported.skipped_keys == []
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert len(manifest["sessions"]) == 1
    assert len(manifest["sessions"][0]["files"]) == 1
    assert manifest["sessions"][0]["files"][0]["main"] is True


def test_remote_session_export_uses_actual_size_when_source_shrinks(monkeypatch, tmp_path):
    remote_path = "/home/test/.codex/sessions/2026/05/01/rollout-shrunk.jsonl"
    content = json.dumps({
        "type": "session_meta",
        "payload": {"id": "shrunk", "cwd": "/workspace"},
    }).encode("utf-8") + b"\n"

    class ShrinkingSFTP(_SessionSFTP):
        def stat(self, path: str):
            attr = super().stat(path)
            if posixpath.normpath(path) == remote_path:
                attr.st_size = len(content) + 4096
            return attr

    sftp = ShrinkingSFTP()
    sftp.add_file(remote_path, content)
    _patch_session_ssh(monkeypatch, sftp)
    [record] = session_migration.list_sessions("codex", ssh_name="gpu")
    bundle_path = tmp_path / "remote-shrunk.asxsession"

    result = session_migration.export_remote_sessions(
        "gpu", bundle_path, {record.key}, provider="codex"
    )

    assert result.session_count == 1
    assert result.file_count == 1
    assert result.total_bytes == len(content)
    with zipfile.ZipFile(bundle_path, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        [file_entry] = manifest["sessions"][0]["files"]
        assert file_entry["size"] == len(content)
        assert bundle.read(file_entry["archive_path"]) == content


def test_remote_session_export_skips_grown_source_without_orphan_zip_entry(monkeypatch, tmp_path):
    remote_path = "/home/test/.codex/sessions/2026/05/01/rollout-grown.jsonl"
    content = json.dumps({
        "type": "session_meta",
        "payload": {"id": "grown", "cwd": "/workspace", "padding": "x" * 100},
    }).encode("utf-8") + b"\n"

    class GrowingSFTP(_SessionSFTP):
        def stat(self, path: str):
            attr = super().stat(path)
            if posixpath.normpath(path) == remote_path:
                attr.st_size = 8
            return attr

    sftp = GrowingSFTP()
    sftp.add_file(remote_path, content)
    _patch_session_ssh(monkeypatch, sftp)
    [record] = session_migration.list_sessions("codex", ssh_name="gpu")
    bundle_path = tmp_path / "remote-grown.asxsession"

    result = session_migration.export_remote_sessions(
        "gpu", bundle_path, {record.key}, provider="codex"
    )

    assert result.session_count == 0
    assert result.file_count == 0
    assert result.skipped_keys == [record.key]
    with zipfile.ZipFile(bundle_path, "r") as bundle:
        assert bundle.namelist() == ["manifest.json"]


def test_remote_session_export_rejects_local_output_inside_live_home(monkeypatch, tmp_path):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    output = claude_home / "projects" / "demo" / "session.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(session_migration, "default_claude_home", lambda: claude_home)
    monkeypatch.setattr(session_migration, "default_codex_home", lambda: codex_home)
    monkeypatch.setattr(
        session_migration,
        "_connect_ssh",
        lambda _name: (_ for _ in ()).throw(AssertionError("SSH must not connect for an unsafe output")),
    )

    with pytest.raises(ValueError, match="不能保存"):
        session_migration.export_remote_sessions("gpu", output, {"ssh:gpu:claude:demo"})

    assert output.read_text(encoding="utf-8") == "keep"


def _reset_store() -> None:
    profile_manager._save_store(profile_manager._get_default_store())


def _set_data_dir(data_dir: Path) -> None:
    paths.STORAGE_DIR = data_dir
    paths.PROFILES_FILE = paths.STORAGE_DIR / "profiles.json"
    paths.BACKUPS_DIR = paths.STORAGE_DIR / "backups"
    paths.SECRETS_DIR = paths.STORAGE_DIR / "secrets"
    profile_manager.PROFILES_FILE = paths.PROFILES_FILE
    security.SECRETS_DIR = paths.SECRETS_DIR
    paths.ensure_storage_dirs(migrate_legacy=False)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unique = root.name.replace("-", "_")
        machine_a = root / "machine_a"
        machine_b = root / "machine_b"
        _set_data_dir(machine_a)

        _reset_store()

        claude_ref = f"claude:MoveMe_{unique}:auth_token"
        codex_ref = f"codex:MoveMe_{unique}:api_key"
        ssh_ref = f"ssh:Server_{unique}:password"
        security.set_secret(claude_ref, "claude-secret")
        security.set_secret(codex_ref, "codex-secret")
        security.set_secret(ssh_ref, "ssh-secret")

        profile_manager.save_claude_profile(ClaudeProfile(
            name="MoveMe",
            auth_token_ref=claude_ref,
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-flash",
            provider="deepseek",
        ))
        profile_manager.save_codex_profile(CodexProfile(
            name="MoveMe",
            api_key_ref=codex_ref,
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_base_url="https://api.deepseek.com",
        ))
        profile_manager.save_ssh_profile(SSHProfile(
            name="Server",
            host="example.com",
            password_ref=ssh_ref,
        ))

        browser_dir = paths.STORAGE_DIR / "browser_profiles" / "chrome_BrowserMoveMe"
        (browser_dir / "Default" / "Network").mkdir(parents=True)
        (browser_dir / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
        (browser_dir / "Default" / "Cache").mkdir(parents=True)
        (browser_dir / "Local State").write_text('{"os_crypt":{"encrypted_key":"fake"}}', encoding="utf-8")
        (browser_dir / "Default" / "Network" / "Cookies").write_bytes(b"cookie-db")
        (browser_dir / "Default" / "Local Storage" / "leveldb" / "chatgpt.ldb").write_bytes(b"local-storage")
        (browser_dir / "Default" / "Cache" / "cache.bin").write_bytes(b"skip-cache")
        profile_manager.save_browser_profile(BrowserProfile(
            name="BrowserMoveMe",
            browser_type="chrome",
            profile_mode="managed",
            user_data_dir=str(browser_dir),
            start_target="chatgpt",
            allow_full_reset=True,
            created_by_app=True,
        ))
        profile_manager.set_active_claude("MoveMe")
        profile_manager.set_active_codex("MoveMe")
        profile_manager.set_active_browser("BrowserMoveMe")

        bundle = root / "profiles.asxprofile"
        result = portable_migration.export_portable_profiles(bundle, "strong-password")
        assert result.profile_count == 4, result
        assert result.secret_count == 3, result
        assert result.missing_secret_refs == [], result
        assert result.browser_file_count >= 3, result
        assert result.browser_bytes > 0, result

        raw = json.loads(bundle.read_text(encoding="utf-8"))
        assert raw["format"] == portable_migration.BUNDLE_FORMAT
        assert "claude-secret" not in bundle.read_text(encoding="utf-8")
        assert "cookie-db" not in bundle.read_text(encoding="utf-8")

        _set_data_dir(machine_b)
        _reset_store()
        for ref in [claude_ref, codex_ref, ssh_ref]:
            security.delete_secret(ref)
        preexisting_browser_dir = machine_b / "browser_profiles" / "chrome_BrowserMoveMe"
        preexisting_browser_dir.mkdir(parents=True)
        (preexisting_browser_dir / "old.txt").write_text("old-browser-data", encoding="utf-8")

        imported = portable_migration.import_portable_profiles(bundle, "strong-password")
        assert imported.profile_count == 4, imported
        assert imported.secret_count == 3, imported
        assert imported.browser_file_count >= 3, imported
        assert profile_manager.get_active_claude_name() == "MoveMe"
        assert profile_manager.get_active_codex_name() == "MoveMe"
        assert profile_manager.get_active_browser_name() == "BrowserMoveMe"
        assert security.get_secret(claude_ref) == "claude-secret"
        assert security.get_secret(codex_ref) == "codex-secret"
        assert security.get_secret(ssh_ref) == "ssh-secret"
        [browser_profile] = profile_manager.list_browser_profiles()
        imported_browser_dir = Path(browser_profile.user_data_dir)
        assert imported_browser_dir.exists()
        assert imported_browser_dir != browser_dir
        assert (imported_browser_dir / "Local State").read_text(encoding="utf-8") == '{"os_crypt":{"encrypted_key":"fake"}}'
        assert (imported_browser_dir / "Default" / "Network" / "Cookies").read_bytes() == b"cookie-db"
        assert (imported_browser_dir / "Default" / "Local Storage" / "leveldb" / "chatgpt.ldb").read_bytes() == b"local-storage"
        assert not (imported_browser_dir / "Default" / "Cache" / "cache.bin").exists()
        assert not (imported_browser_dir / "old.txt").exists()
        assert not (machine_b / "browser_profiles" / "chrome_BrowserMoveMe.import_backup").exists()

        bad_json_bundle = root / "bad-json.asxprofile"
        bad_json_bundle.write_text("{", encoding="utf-8")
        try:
            portable_migration.import_portable_profiles(bad_json_bundle, "strong-password")
        except ValueError as e:
            assert "JSON" in str(e)
        else:
            raise AssertionError("Broken JSON bundle should fail")

        try:
            portable_migration.import_portable_profiles(bundle, "wrong-password")
        except ValueError as e:
            assert "迁移密码错误" in str(e)
        else:
            raise AssertionError("Wrong password should fail")

        corrupt_payload = portable_migration._decrypt_bundle(raw, "strong-password")
        for item in corrupt_payload.get("browser_data", {}).values():
            for file_entry in item.get("files", []):
                file_entry["data"] = "@@not-base64@@"
        corrupt_browser_bundle = root / "corrupt-browser.asxprofile"
        corrupt_browser_bundle.write_text(
            json.dumps(portable_migration._encrypt_payload(corrupt_payload, "strong-password"), ensure_ascii=False),
            encoding="utf-8",
        )
        machine_c = root / "machine_c"
        _set_data_dir(machine_c)
        _reset_store()
        imported_corrupt = portable_migration.import_portable_profiles(corrupt_browser_bundle, "strong-password")
        assert imported_corrupt.browser_file_count == 0
        assert imported_corrupt.skipped_browser_files
        assert profile_manager.list_browser_profiles() == []
        assert profile_manager.get_active_browser_name() is None

        for ref in [claude_ref, codex_ref, ssh_ref]:
            security.delete_secret(ref)

    print("OK portable migration checks passed")


if __name__ == "__main__":
    main()
