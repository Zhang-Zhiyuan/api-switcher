"""Regression checks for password-protected portable profile migration."""
import json
import posixpath
import stat
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

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
