from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import session_migration


def _jsonl(*items: dict) -> bytes:
    return b"".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for item in items
    )


def test_compact_codex_output_preserves_event_identity(monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 32)
    raw = _jsonl({
        "timestamp": "2026-07-14T00:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "x" * 200,
        },
    })

    compacted, omitted_count, omitted_bytes = session_migration._compact_session_jsonl_line(raw, "codex")
    item = json.loads(compacted)

    assert item["type"] == "response_item"
    assert item["payload"]["type"] == "function_call_output"
    assert item["payload"]["call_id"] == "call-1"
    assert item["payload"]["output"] == session_migration.COMPACT_TOOL_OUTPUT_MARKER
    assert omitted_count == 1
    assert omitted_bytes > 0
    assert len(compacted) < len(raw)


def test_compact_structured_outputs_keep_provider_valid_content_shape(monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 32)
    codex_raw = _jsonl({
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "call-2",
            "output": [{"type": "input_image", "image_url": "z" * 200}],
        },
    })
    claude_raw = _jsonl({
        "type": "user",
        "sessionId": "session-1",
        "toolUseResult": {"preserved": "y" * 200},
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "is_error": False,
                "content": [{"type": "image", "source": {"data": "z" * 200}}],
            }],
        },
    })

    codex_item = json.loads(session_migration._compact_session_jsonl_line(codex_raw, "codex")[0])
    claude_item = json.loads(session_migration._compact_session_jsonl_line(claude_raw, "claude")[0])

    assert codex_item["payload"]["output"] == [
        {"type": "input_text", "text": session_migration.COMPACT_TOOL_OUTPUT_MARKER}
    ]
    claude_block = claude_item["message"]["content"][0]
    assert claude_block["tool_use_id"] == "tool-1"
    assert claude_block["is_error"] is False
    assert claude_block["content"] == [
        {"type": "text", "text": session_migration.COMPACT_TOOL_OUTPUT_MARKER}
    ]
    assert claude_item["toolUseResult"] == session_migration.COMPACT_TOOL_OUTPUT_MARKER


def test_compact_mode_leaves_small_and_invalid_lines_byte_exact(monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 64)
    small = _jsonl({
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "small", "output": "ok"},
    })
    invalid = b"not-json\r\n"

    assert session_migration._compact_session_jsonl_line(small, "codex") == (small, 0, 0)
    assert session_migration._compact_session_jsonl_line(invalid, "codex") == (invalid, 0, 0)


def test_compact_export_only_contains_selected_session_and_does_not_modify_source(tmp_path, monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 32)
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "07" / "14"
    sessions_dir.mkdir(parents=True)

    selected_file = sessions_dir / "rollout-selected.jsonl"
    selected_bytes = _jsonl(
        {
            "timestamp": "2026-07-14T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "selected", "cwd": "C:\\Project"},
        },
        {
            "timestamp": "2026-07-14T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"text": "保留对话"}]},
        },
        {
            "timestamp": "2026-07-14T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "large-output",
                "output": "tool-output-secret" * 100,
            },
        },
    )
    selected_file.write_bytes(selected_bytes)
    # Compact mode must be judged by its compacted output, not rejected from
    # the much larger source-file size before streaming.
    monkeypatch.setattr(session_migration, "MAX_PACKAGE_FILE_BYTES", len(selected_bytes) - 1)
    other_file = sessions_dir / "rollout-other.jsonl"
    other_file.write_bytes(_jsonl({
        "type": "session_meta",
        "payload": {"id": "other", "cwd": "C:\\Other"},
    }))

    session_migration.clear_local_session_cache()
    records = session_migration.list_sessions(
        "codex",
        claude_home=claude_home,
        codex_home=codex_home,
    )
    selected_record = next(record for record in records if record.session_id == "selected")
    package = tmp_path / "selected.asxsession"

    result = session_migration.export_sessions(
        package,
        {selected_record.key},
        claude_home=claude_home,
        codex_home=codex_home,
        content_mode=session_migration.CONTENT_MODE_COMPACT,
    )

    assert selected_file.read_bytes() == selected_bytes
    assert result.session_count == 1
    assert result.content_mode == session_migration.CONTENT_MODE_COMPACT
    assert result.omitted_output_count == 1
    assert result.omitted_bytes > 0

    with zipfile.ZipFile(package, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["content_mode"] == session_migration.CONTENT_MODE_COMPACT
        assert len(manifest["sessions"]) == 1
        assert manifest["sessions"][0]["session_id"] == "selected"
        [file_entry] = manifest["sessions"][0]["files"]
        compacted_data = bundle.read(file_entry["archive_path"])
        assert b"tool-output-secret" not in compacted_data
        assert file_entry["compacted"] is True
        assert file_entry["size"] == len(compacted_data)

    summary = session_migration.inspect_package(package)
    assert summary.session_count == 1
    assert summary.content_mode == session_migration.CONTENT_MODE_COMPACT
    assert summary.omitted_output_count == 1

    imported_codex_home = tmp_path / "imported-codex"
    imported = session_migration.import_sessions(
        package,
        claude_home=tmp_path / "imported-claude",
        codex_home=imported_codex_home,
    )
    assert imported.session_count == 1
    imported_file = imported_codex_home / selected_record.relative_path
    assert imported_file.exists()
    imported_items = [json.loads(line) for line in imported_file.read_text(encoding="utf-8").splitlines()]
    output_item = next(item for item in imported_items if item.get("payload", {}).get("call_id") == "large-output")
    assert output_item["payload"]["output"] == session_migration.COMPACT_TOOL_OUTPUT_MARKER


def test_full_export_preserves_selected_main_file_byte_exact(tmp_path):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    session_file = codex_home / "sessions" / "rollout-full.jsonl"
    session_file.parent.mkdir(parents=True)
    source = _jsonl(
        {"type": "session_meta", "payload": {"id": "full", "cwd": "C:\\Project"}},
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call", "output": "x" * 1000},
        },
    )
    session_file.write_bytes(source)
    session_migration.clear_local_session_cache()
    [record] = session_migration.list_sessions(
        "codex",
        claude_home=claude_home,
        codex_home=codex_home,
    )
    package = tmp_path / "full.asxsession"

    result = session_migration.export_sessions(
        package,
        {record.key},
        claude_home=claude_home,
        codex_home=codex_home,
    )

    with zipfile.ZipFile(package, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        [file_entry] = manifest["sessions"][0]["files"]
        assert bundle.read(file_entry["archive_path"]) == source
    assert result.content_mode == session_migration.CONTENT_MODE_FULL
    assert result.omitted_output_count == 0


def test_remote_compact_export_uses_same_tool_output_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 32)
    remote_path = "/home/test/.codex/sessions/rollout-remote.jsonl"
    source = _jsonl(
        {"type": "session_meta", "payload": {"id": "remote", "cwd": "/workspace"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "remote-call",
                "output": "remote-tool-output" * 100,
            },
        },
    )
    record = session_migration.SessionRecord(
        key="ssh:gpu:codex:remote",
        provider="codex",
        session_id="remote",
        title="Remote",
        summary="Remote",
        source_path=Path(remote_path),
        relative_path="sessions/rollout-remote.jsonl",
        origin="ssh",
        ssh_name="gpu",
        remote_path=remote_path,
        size_bytes=len(source),
    )

    class RemoteFile(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class SFTP:
        def stat(self, _path):
            return SimpleNamespace(st_size=len(source))

        def open(self, _path, _mode):
            return RemoteFile(source)

        def close(self):
            pass

    class Client:
        def open_sftp(self):
            return SFTP()

    monkeypatch.setattr(session_migration, "_connect_ssh", lambda _name: (object(), Client()))
    monkeypatch.setattr(
        session_migration,
        "_remote_provider_home",
        lambda _client, _profile, provider: f"/home/test/.{provider}",
    )
    monkeypatch.setattr(session_migration, "list_remote_sessions", lambda _name, _provider: [record])
    monkeypatch.setattr(
        session_migration,
        "_remote_record_files",
        lambda _sftp, _record, **_kwargs: [remote_path],
    )

    package = tmp_path / "remote-compact.asxsession"
    result = session_migration.export_remote_sessions(
        "gpu",
        package,
        {record.key},
        provider="codex",
        content_mode=session_migration.CONTENT_MODE_COMPACT,
    )

    assert result.session_count == 1
    assert result.omitted_output_count == 1
    with zipfile.ZipFile(package, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        [file_entry] = manifest["sessions"][0]["files"]
        migrated = bundle.read(file_entry["archive_path"])
    assert b"remote-tool-output" not in migrated
    migrated_items = [json.loads(line) for line in migrated.splitlines()]
    assert migrated_items[1]["payload"]["call_id"] == "remote-call"
    assert migrated_items[1]["payload"]["output"] == session_migration.COMPACT_TOOL_OUTPUT_MARKER


def test_export_rejects_unknown_content_mode(tmp_path):
    with pytest.raises(ValueError, match="内容模式"):
        session_migration.export_sessions(tmp_path / "bad.asxsession", {"key"}, content_mode="unknown")


def test_compact_claude_export_excludes_support_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(session_migration, "COMPACT_TOOL_OUTPUT_LIMIT_BYTES", 32)
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    session_file = claude_home / "projects" / "project" / "session-1.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(_jsonl({
        "type": "user",
        "sessionId": "session-1",
        "cwd": "C:\\Project",
        "message": {"content": "保留对话"},
    }))
    support_file = session_file.with_suffix("") / "tool-results" / "large.txt"
    support_file.parent.mkdir(parents=True)
    support_file.write_text("sensitive-support-output" * 100, encoding="utf-8")
    session_migration.clear_local_session_cache()
    [record] = session_migration.list_sessions(
        "claude",
        claude_home=claude_home,
        codex_home=codex_home,
    )

    compact_package = tmp_path / "compact.asxsession"
    compact = session_migration.export_sessions(
        compact_package,
        {record.key},
        claude_home=claude_home,
        codex_home=codex_home,
        content_mode=session_migration.CONTENT_MODE_COMPACT,
    )
    full_package = tmp_path / "full.asxsession"
    full = session_migration.export_sessions(
        full_package,
        {record.key},
        claude_home=claude_home,
        codex_home=codex_home,
    )

    assert compact.file_count == 1
    assert full.file_count == 2
    with zipfile.ZipFile(compact_package, "r") as bundle:
        assert all("tool-results" not in name for name in bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["compact_policy"]["include_claude_support_files"] is False
