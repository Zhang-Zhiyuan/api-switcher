import json
import os
import posixpath
import stat
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import session_migration


def _write_session_package(
    path: Path,
    *,
    provider: str,
    relative_path: str,
    session_id: str = "imported-session",
    content: bytes = b"{}\n",
) -> None:
    manifest = {
        "format": session_migration.PACKAGE_FORMAT,
        "version": session_migration.PACKAGE_VERSION,
        "sessions": [{
            "provider": provider,
            "session_id": session_id,
            "relative_path": relative_path,
            "title": "Imported",
            "files": [{
                "relative_path": relative_path,
                "archive_path": "files/0/session.jsonl",
                "main": True,
            }],
        }],
    }
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("files/0/session.jsonl", content)


def test_local_export_skips_reparse_source_and_enforces_provider_root(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    project = claude_home / "projects" / "demo"
    project.mkdir(parents=True)
    outside = tmp_path / "secret.jsonl"
    outside.write_text("secret outside session root", encoding="utf-8")
    source = project / "linked.jsonl"

    real_is_unsafe = session_migration._path_is_link_or_reparse
    try:
        source.symlink_to(outside)
    except OSError:
        source.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(
            session_migration,
            "_path_is_link_or_reparse",
            lambda path, info=None: path == source or real_is_unsafe(path, info),
        )

    record = session_migration.SessionRecord(
        key="claude:projects/demo/linked.jsonl",
        provider="claude",
        session_id="linked",
        title="Linked",
        summary="Linked",
        source_path=source,
        relative_path="projects/demo/linked.jsonl",
    )
    monkeypatch.setattr(session_migration, "list_sessions", lambda *_args, **_kwargs: [record])

    package = tmp_path / "linked.asxsession"
    result = session_migration.export_sessions(
        package,
        {record.key},
        claude_home=claude_home,
        codex_home=codex_home,
    )

    assert result.session_count == 0
    assert result.file_count == 0
    assert record.key in result.skipped_keys
    with zipfile.ZipFile(package) as bundle:
        assert b"secret outside session root" not in bundle.read("manifest.json")
    with pytest.raises(ValueError, match="越界"):
        session_migration._local_export_file_info(outside, claude_home, "claude")


def test_local_full_export_aborts_cleanly_if_source_grows_during_copy(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    source = claude_home / "projects" / "demo" / "session.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"{}\n")
    record = session_migration.SessionRecord(
        key="claude:projects/demo/session.jsonl",
        provider="claude",
        session_id="session",
        title="Session",
        summary="Session",
        source_path=source,
        relative_path="projects/demo/session.jsonl",
    )
    monkeypatch.setattr(session_migration, "list_sessions", lambda *_args, **_kwargs: [record])
    monkeypatch.setattr(
        session_migration,
        "_copy_binary_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            session_migration.SessionSourceChangedError("source grew")
        ),
    )
    package = tmp_path / "changed.asxsession"

    with pytest.raises(session_migration.SessionSourceChangedError, match="source grew"):
        session_migration.export_sessions(
            package,
            {record.key},
            claude_home=claude_home,
            codex_home=codex_home,
            content_mode=session_migration.CONTENT_MODE_FULL,
        )

    assert not package.exists()
    assert not list(tmp_path.glob("*.tmp"))


class _WalkSFTP:
    def __init__(self):
        self.root = "/home/test/.codex/sessions"
        self.attrs = {
            self.root: self._attr("sessions", stat.S_IFDIR | 0o700, inode=1),
            f"{self.root}/real": self._attr("real", stat.S_IFDIR | 0o700, inode=2),
            f"{self.root}/real/good.jsonl": self._attr("good.jsonl", stat.S_IFREG | 0o600),
            f"{self.root}/linked": self._attr("linked", stat.S_IFLNK | 0o777),
            f"{self.root}/same-inode": self._attr("same-inode", stat.S_IFDIR | 0o700, inode=1),
        }

    @staticmethod
    def _attr(filename, mode, inode=None):
        return SimpleNamespace(filename=filename, st_mode=mode, st_size=2, st_dev=7, st_ino=inode)

    def lstat(self, path):
        try:
            return self.attrs[posixpath.normpath(path)]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    def listdir_attr(self, path):
        path = posixpath.normpath(path)
        if path == self.root:
            return [
                self.attrs[f"{self.root}/real"],
                self.attrs[f"{self.root}/linked"],
                self.attrs[f"{self.root}/same-inode"],
                self._attr("../escape", stat.S_IFDIR | 0o700, inode=99),
            ]
        if path == f"{self.root}/real":
            return [self.attrs[f"{self.root}/real/good.jsonl"]]
        raise AssertionError(f"unsafe directory followed: {path}")


def test_remote_walk_uses_lstat_skips_links_and_inode_loops():
    sftp = _WalkSFTP()

    files = list(session_migration._remote_walk_files(sftp, sftp.root, suffix=".jsonl"))

    assert [(path, attr.filename) for path, attr in files] == [
        (f"{sftp.root}/real/good.jsonl", "good.jsonl")
    ]


@pytest.mark.parametrize("relative_path", [
    r"projects\demo\evil.jsonl",
    "C:/projects/demo/evil.jsonl",
    "projects/demo/evil.jsonl:stream",
    "projects/demo/NUL.jsonl",
    "projects/demo/evil.jsonl ",
    "projects/demo/../evil.jsonl",
])
def test_local_import_rejects_windows_path_variants(tmp_path, relative_path):
    package = tmp_path / "unsafe.asxsession"
    _write_session_package(package, provider="claude", relative_path=relative_path)
    claude_home = tmp_path / "claude"

    result = session_migration.import_sessions(
        package,
        claude_home=claude_home,
        codex_home=tmp_path / "codex",
    )

    assert result.file_count == 0
    assert result.session_count == 0
    assert result.skipped_invalid == 1
    assert not (claude_home / "projects").exists()


class _RemoteWriter(BytesIO):
    def __init__(self, sftp, path, mode):
        initial = sftp.files.get(path, b"") if "a" in mode else b""
        super().__init__(initial)
        self.seek(0, 2)
        self.sftp = sftp
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.sftp.files[self.path] = self.getvalue()
        self.close()


class _ImportSFTP:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs = {"/", "/home", "/home/test"}
        self.links: set[str] = set()
        self.race_destination: str | None = None

    def lstat(self, path):
        path = posixpath.normpath(path)
        if path in self.links:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=0)
        if path in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=len(self.files[path]))
        if path in self.dirs:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_size=0)
        raise FileNotFoundError(path)

    stat = lstat

    def mkdir(self, path):
        self.dirs.add(posixpath.normpath(path))

    def chmod(self, _path, _mode):
        pass

    def open(self, path, mode):
        return _RemoteWriter(self, posixpath.normpath(path), mode)

    def rename(self, source, destination):
        source = posixpath.normpath(source)
        destination = posixpath.normpath(destination)
        if destination == self.race_destination:
            self.files[destination] = b"concurrent winner"
            raise FileExistsError(destination)
        if destination in self.files or destination in self.links:
            raise FileExistsError(destination)
        self.files[destination] = self.files.pop(source)

    def posix_rename(self, source, destination):
        source = posixpath.normpath(source)
        destination = posixpath.normpath(destination)
        self.files[destination] = self.files.pop(source)

    def remove(self, path):
        self.files.pop(posixpath.normpath(path), None)

    def close(self):
        pass


class _ImportClient:
    def __init__(self, sftp):
        self.sftp = sftp

    def open_sftp(self):
        return self.sftp


def _patch_remote_import(monkeypatch, sftp):
    client = _ImportClient(sftp)
    monkeypatch.setattr(session_migration, "_connect_ssh", lambda _name: (object(), client))
    monkeypatch.setattr(
        session_migration,
        "_remote_provider_home",
        lambda _client, _profile, provider: f"/home/test/.{provider}",
    )


def test_remote_import_rejects_symlink_parent(monkeypatch, tmp_path):
    package = tmp_path / "remote-link.asxsession"
    relative = "projects/demo/session.jsonl"
    _write_session_package(package, provider="claude", relative_path=relative)
    sftp = _ImportSFTP()
    sftp.dirs.add("/home/test/.claude")
    sftp.links.add("/home/test/.claude/projects")
    _patch_remote_import(monkeypatch, sftp)

    result = session_migration.import_sessions_to_ssh("gpu", package, overwrite=True)

    assert result.file_count == 0
    assert result.skipped_invalid == 1
    assert "/home/test/.claude/projects/demo/session.jsonl" not in sftp.files


def test_local_import_no_overwrite_commit_loses_race_safely(monkeypatch, tmp_path):
    package = tmp_path / "local-race.asxsession"
    relative = "projects/demo/session.jsonl"
    _write_session_package(package, provider="claude", relative_path=relative, content=b"package data")
    claude_home = tmp_path / "claude"
    destination = claude_home / "projects" / "demo" / "session.jsonl"
    real_link = os.link

    def racing_link(source, target, *args, **kwargs):
        Path(target).write_bytes(b"concurrent winner")
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(session_migration.os, "link", racing_link)

    result = session_migration.import_sessions(
        package,
        claude_home=claude_home,
        codex_home=tmp_path / "codex",
        overwrite=False,
    )

    assert result.file_count == 0
    assert result.session_count == 0
    assert result.skipped_existing == 1
    assert destination.read_bytes() == b"concurrent winner"


def test_remote_import_no_overwrite_commit_loses_race_safely(monkeypatch, tmp_path):
    package = tmp_path / "remote-race.asxsession"
    relative = "projects/demo/session.jsonl"
    _write_session_package(package, provider="claude", relative_path=relative, content=b"package data")
    sftp = _ImportSFTP()
    destination = "/home/test/.claude/projects/demo/session.jsonl"
    sftp.race_destination = destination
    _patch_remote_import(monkeypatch, sftp)

    result = session_migration.import_sessions_to_ssh("gpu", package, overwrite=False)

    assert result.file_count == 0
    assert result.session_count == 0
    assert result.skipped_existing == 1
    assert sftp.files[destination] == b"concurrent winner"


def test_codex_index_append_preserves_raw_and_concurrent_lines(monkeypatch, tmp_path):
    package = tmp_path / "codex-index.asxsession"
    relative = "sessions/2026/07/14/session.jsonl"
    _write_session_package(
        package,
        provider="codex",
        relative_path=relative,
        session_id="imported",
    )
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    index_path = codex_home / "session_index.jsonl"
    original = b"not-json\n{\"id\":\"existing\"}"
    index_path.write_bytes(original)
    real_open = os.open
    injected = False

    def concurrent_open(path, flags, mode=0o777, *args, **kwargs):
        nonlocal injected
        if Path(path) == index_path and flags & os.O_APPEND and not injected:
            with index_path.open("ab") as handle:
                handle.write(b"\n{\"id\":\"concurrent\"}\n")
            injected = True
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(session_migration.os, "open", concurrent_open)

    result = session_migration.import_sessions(
        package,
        claude_home=tmp_path / "claude",
        codex_home=codex_home,
    )

    data = index_path.read_bytes()
    assert result.session_count == 1
    assert data.startswith(original)
    assert b'"id":"concurrent"' in data
    assert b'"id":"imported"' in data


def test_remote_codex_index_is_appended_instead_of_replaced():
    sftp = _ImportSFTP()
    sftp.dirs.add("/home/test/.codex")
    index_path = "/home/test/.codex/session_index.jsonl"
    original = b"invalid original line\n"
    sftp.files[index_path] = original

    session_migration._update_remote_codex_index(
        sftp,
        "/home/test/.codex",
        [{"session_id": "imported", "title": "Imported"}],
    )

    assert sftp.files[index_path].startswith(original)
    assert b'"id":"imported"' in sftp.files[index_path]
