"""Local Claude Code and Codex session discovery and migration."""
from __future__ import annotations

import json
import posixpath
import shutil
import stat
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from core.atomic_io import atomic_write_bytes, atomic_write_text, replace_with_retry, temp_path_for
from core import profile_manager, remote_config
from core.ssh_manager import ssh_manager


PACKAGE_FORMAT = "api-switcher-session-migration"
PACKAGE_VERSION = 1
PACKAGE_EXTENSION = ".asxsession"
MAX_SCAN_LINES = 2000
MAX_REMOTE_SESSION_FILES = 5000
MAX_REMOTE_PARSE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class SessionRecord:
    key: str
    provider: str
    session_id: str
    title: str
    summary: str
    source_path: Path
    relative_path: str
    project_key: str = ""
    project_path: str = ""
    created_at: str = ""
    updated_at: str = ""
    size_bytes: int = 0
    message_count: int = 0
    model: str = ""
    origin: str = "local"
    ssh_name: str = ""
    remote_path: str = ""

    def to_manifest(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_path"] = str(self.source_path)
        return data


@dataclass(frozen=True)
class SessionExportResult:
    path: Path
    session_count: int
    file_count: int
    total_bytes: int
    skipped_keys: list[str]


@dataclass(frozen=True)
class SessionImportResult:
    session_count: int
    file_count: int
    skipped_existing: int
    skipped_invalid: int


@dataclass(frozen=True)
class SessionPackageSummary:
    session_count: int
    file_count: int
    total_bytes: int
    providers: dict[str, int]
    project_paths: list[str]


def default_claude_home() -> Path:
    return Path.home() / ".claude"


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def list_sessions(
    provider: str = "all",
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    ssh_name: str | None = None,
) -> list[SessionRecord]:
    """Return Claude Code and/or Codex sessions sorted newest first."""
    if ssh_name:
        return list_remote_sessions(ssh_name, provider)

    provider = (provider or "all").lower()
    claude_home = claude_home or default_claude_home()
    codex_home = codex_home or default_codex_home()

    records: list[SessionRecord] = []
    if provider in {"all", "claude"}:
        records.extend(_list_claude_sessions(claude_home))
    if provider in {"all", "codex"}:
        records.extend(_list_codex_sessions(codex_home))

    return sorted(records, key=lambda item: item.updated_at or item.created_at, reverse=True)


def list_remote_sessions(ssh_name: str, provider: str = "all") -> list[SessionRecord]:
    """Return Claude Code and/or Codex sessions from an SSH server."""
    provider = (provider or "all").lower()
    profile, client = _connect_ssh(ssh_name)
    claude_home = _remote_provider_home(client, profile, "claude")
    codex_home = _remote_provider_home(client, profile, "codex")

    sftp = None
    try:
        sftp = client.open_sftp()
        records: list[SessionRecord] = []
        if provider in {"all", "claude"}:
            records.extend(_list_remote_claude_sessions(sftp, claude_home, ssh_name))
        if provider in {"all", "codex"}:
            records.extend(_list_remote_codex_sessions(sftp, codex_home, ssh_name))
        return sorted(records, key=lambda item: item.updated_at or item.created_at, reverse=True)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def export_sessions(
    output_path: str | Path,
    selected_keys: set[str] | list[str] | tuple[str, ...],
    claude_home: Path | None = None,
    codex_home: Path | None = None,
) -> SessionExportResult:
    """Export selected sessions to a portable .asxsession zip package."""
    keys = {str(key) for key in selected_keys}
    if not keys:
        raise ValueError("请选择要导出的会话")

    claude_home = claude_home or default_claude_home()
    codex_home = codex_home or default_codex_home()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = list_sessions("all", claude_home=claude_home, codex_home=codex_home)
    selected = [record for record in records if record.key in keys]
    skipped_keys = sorted(keys - {record.key for record in selected})
    if not selected:
        raise ValueError("没有找到可导出的会话")

    manifest_entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0

    tmp_output = temp_path_for(output_path)
    try:
        with zipfile.ZipFile(tmp_output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for index, record in enumerate(selected):
                home = _provider_home(record.provider, claude_home, codex_home)
                entry = record.to_manifest()
                entry["files"] = []

                for file_path in _record_files(record):
                    if not file_path.is_file():
                        continue
                    try:
                        relative_path = _relative_to_home(file_path, home)
                    except ValueError:
                        continue

                    archive_path = f"files/{index}/{relative_path}"
                    size = file_path.stat().st_size
                    bundle.write(file_path, archive_path)
                    entry["files"].append({
                        "relative_path": relative_path,
                        "archive_path": archive_path,
                        "size": size,
                        "main": file_path == record.source_path,
                    })
                    file_count += 1
                    total_bytes += size

                if entry["files"]:
                    manifest_entries.append(entry)

            manifest = {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sessions": manifest_entries,
            }
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        replace_with_retry(tmp_output, output_path)
    except Exception:
        tmp_output.unlink(missing_ok=True)
        raise

    return SessionExportResult(
        path=output_path,
        session_count=len(manifest_entries),
        file_count=file_count,
        total_bytes=total_bytes,
        skipped_keys=skipped_keys,
    )


def export_remote_sessions(
    ssh_name: str,
    output_path: str | Path,
    selected_keys: set[str] | list[str] | tuple[str, ...],
    provider: str = "all",
) -> SessionExportResult:
    """Export selected sessions from an SSH server to a local .asxsession package."""
    keys = {str(key) for key in selected_keys}
    if not keys:
        raise ValueError("请选择要导出的会话")

    profile, client = _connect_ssh(ssh_name)
    claude_home = _remote_provider_home(client, profile, "claude")
    codex_home = _remote_provider_home(client, profile, "codex")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = list_remote_sessions(ssh_name, provider)
    selected = [record for record in records if record.key in keys]
    skipped_keys = sorted(keys - {record.key for record in selected})
    if not selected:
        raise ValueError("没有找到可导出的会话")

    manifest_entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    tmp_output = temp_path_for(output_path)
    sftp = None
    try:
        sftp = client.open_sftp()
        with zipfile.ZipFile(tmp_output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for index, record in enumerate(selected):
                home = claude_home if record.provider == "claude" else codex_home
                entry = record.to_manifest()
                entry["source"] = "ssh"
                entry["ssh_name"] = ssh_name
                entry["files"] = []

                for remote_path in _remote_record_files(sftp, record):
                    try:
                        relative_path = _remote_relative_to_home(remote_path, home)
                        info = sftp.stat(remote_path)
                    except Exception:
                        continue
                    size = int(getattr(info, "st_size", 0) or 0)
                    if size > MAX_PACKAGE_FILE_BYTES or total_bytes + size > MAX_PACKAGE_TOTAL_BYTES:
                        continue

                    archive_path = f"files/{index}/{relative_path}"
                    with bundle.open(archive_path, "w") as target, sftp.open(remote_path, "rb") as source:
                        shutil.copyfileobj(source, target)
                    entry["files"].append({
                        "relative_path": relative_path,
                        "archive_path": archive_path,
                        "size": size,
                        "main": remote_path == record.remote_path,
                    })
                    file_count += 1
                    total_bytes += size

                if entry["files"]:
                    manifest_entries.append(entry)

            manifest = {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sessions": manifest_entries,
            }
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        replace_with_retry(tmp_output, output_path)
    except Exception:
        tmp_output.unlink(missing_ok=True)
        raise
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass

    return SessionExportResult(
        path=output_path,
        session_count=len(manifest_entries),
        file_count=file_count,
        total_bytes=total_bytes,
        skipped_keys=skipped_keys,
    )


def import_sessions(
    input_path: str | Path,
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    overwrite: bool = False,
    target_project_path: str | Path | None = None,
) -> SessionImportResult:
    """Import a .asxsession package into local Claude/Codex session folders."""
    claude_home = claude_home or default_claude_home()
    codex_home = codex_home or default_codex_home()
    target_project_text = _normalize_project_path(target_project_path)
    imported_codex: list[dict[str, Any]] = []
    imported_sessions: set[str] = set()
    file_count = 0
    skipped_existing = 0
    skipped_invalid = 0
    imported_bytes = 0

    with zipfile.ZipFile(input_path, "r") as bundle:
        manifest = _read_manifest(bundle)
        for session in manifest.get("sessions", []):
            if not isinstance(session, dict):
                skipped_invalid += 1
                continue
            provider = str(session.get("provider") or "").lower()
            if provider not in {"claude", "codex"}:
                skipped_invalid += 1
                continue

            home = _provider_home(provider, claude_home, codex_home)
            imported_main = False
            for file_entry in session.get("files", []):
                if not isinstance(file_entry, dict):
                    skipped_invalid += 1
                    continue
                archive_path = str(file_entry.get("archive_path") or "")
                relative_path = str(file_entry.get("relative_path") or "")
                if not archive_path or not relative_path:
                    skipped_invalid += 1
                    continue
                try:
                    info = _package_file_info(bundle, archive_path)
                    relative_path = _remap_relative_path(provider, relative_path, target_project_text)
                    destination = _safe_destination(home, relative_path)
                except ValueError:
                    skipped_invalid += 1
                    continue

                if destination.exists() and not overwrite:
                    skipped_existing += 1
                    continue
                if imported_bytes + info.file_size > MAX_PACKAGE_TOTAL_BYTES:
                    skipped_invalid += 1
                    continue

                if target_project_text and destination.suffix.lower() == ".jsonl":
                    data = bundle.read(info)
                    atomic_write_bytes(destination, _rewrite_jsonl_cwd(data, target_project_text))
                else:
                    _copy_package_file_atomic(bundle, info, destination)
                imported_bytes += info.file_size
                file_count += 1
                imported_main = imported_main or bool(file_entry.get("main"))

            if imported_main:
                key = f"{provider}:{session.get('relative_path', '')}"
                imported_sessions.add(key)
                if provider == "codex":
                    imported_codex.append(session)

    if imported_codex:
        _update_codex_index(codex_home, imported_codex)

    return SessionImportResult(
        session_count=len(imported_sessions),
        file_count=file_count,
        skipped_existing=skipped_existing,
        skipped_invalid=skipped_invalid,
    )


def import_sessions_to_ssh(
    ssh_name: str,
    input_path: str | Path,
    overwrite: bool = False,
    target_project_path: str | Path | None = None,
) -> SessionImportResult:
    """Import a local .asxsession package into an SSH server's Claude/Codex folders."""
    profile, client = _connect_ssh(ssh_name)
    claude_home = _remote_provider_home(client, profile, "claude")
    codex_home = _remote_provider_home(client, profile, "codex")
    target_project_text = _normalize_remote_project_path(client, target_project_path)
    imported_codex: list[dict[str, Any]] = []
    imported_sessions: set[str] = set()
    file_count = 0
    skipped_existing = 0
    skipped_invalid = 0
    imported_bytes = 0

    sftp = None
    try:
        sftp = client.open_sftp()
        with zipfile.ZipFile(input_path, "r") as bundle:
            manifest = _read_manifest(bundle)
            for session in manifest.get("sessions", []):
                if not isinstance(session, dict):
                    skipped_invalid += 1
                    continue
                provider = str(session.get("provider") or "").lower()
                if provider not in {"claude", "codex"}:
                    skipped_invalid += 1
                    continue

                home = claude_home if provider == "claude" else codex_home
                imported_main = False
                for file_entry in session.get("files", []):
                    if not isinstance(file_entry, dict):
                        skipped_invalid += 1
                        continue
                    archive_path = str(file_entry.get("archive_path") or "")
                    relative_path = str(file_entry.get("relative_path") or "")
                    if not archive_path or not relative_path:
                        skipped_invalid += 1
                        continue
                    try:
                        info = _package_file_info(bundle, archive_path)
                        relative_path = _remap_relative_path(provider, relative_path, target_project_text)
                        destination = _safe_remote_destination(home, relative_path)
                    except ValueError:
                        skipped_invalid += 1
                        continue

                    if _remote_file_exists(sftp, destination) and not overwrite:
                        skipped_existing += 1
                        continue
                    if imported_bytes + info.file_size > MAX_PACKAGE_TOTAL_BYTES:
                        skipped_invalid += 1
                        continue

                    if target_project_text and destination.lower().endswith(".jsonl"):
                        data = bundle.read(info)
                        _write_remote_bytes_atomic(
                            sftp,
                            destination,
                            _rewrite_jsonl_cwd(data, target_project_text),
                            file_mode=0o600,
                        )
                    else:
                        _copy_package_file_to_remote(sftp, bundle, info, destination, file_mode=0o600)
                    imported_bytes += info.file_size
                    file_count += 1
                    imported_main = imported_main or bool(file_entry.get("main"))

                if imported_main:
                    key = f"{provider}:{session.get('relative_path', '')}"
                    imported_sessions.add(key)
                    if provider == "codex":
                        imported_codex.append(session)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass

    if imported_codex:
        _update_remote_codex_index(client, codex_home, imported_codex)

    return SessionImportResult(
        session_count=len(imported_sessions),
        file_count=file_count,
        skipped_existing=skipped_existing,
        skipped_invalid=skipped_invalid,
    )


def inspect_package(input_path: str | Path) -> SessionPackageSummary:
    """Read a migration package manifest without importing it."""
    with zipfile.ZipFile(input_path, "r") as bundle:
        manifest = _read_manifest(bundle)
        providers: dict[str, int] = {}
        project_paths: list[str] = []
        seen_projects: set[str] = set()
        file_count = 0
        total_bytes = 0
        for session in manifest.get("sessions", []):
            if not isinstance(session, dict):
                continue
            provider = str(session.get("provider") or "unknown")
            providers[provider] = providers.get(provider, 0) + 1
            project_path = str(session.get("project_path") or "")
            if project_path and project_path not in seen_projects:
                project_paths.append(project_path)
                seen_projects.add(project_path)
            for file_entry in session.get("files", []):
                if isinstance(file_entry, dict):
                    try:
                        info = _package_file_info(bundle, str(file_entry.get("archive_path") or ""))
                    except ValueError:
                        continue
                    file_count += 1
                    total_bytes += info.file_size
        return SessionPackageSummary(
            session_count=len([item for item in manifest.get("sessions", []) if isinstance(item, dict)]),
            file_count=file_count,
            total_bytes=total_bytes,
            providers=providers,
            project_paths=project_paths,
        )


def format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size_bytes} B"


def _list_claude_sessions(claude_home: Path) -> list[SessionRecord]:
    projects_dir = claude_home / "projects"
    if not projects_dir.exists():
        return []

    records: list[SessionRecord] = []
    for path in projects_dir.glob("*/*.jsonl"):
        if path.is_file():
            try:
                records.append(_parse_claude_session(path, claude_home))
            except Exception:
                continue
    return records


def _list_codex_sessions(codex_home: Path) -> list[SessionRecord]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []

    index = _read_codex_index(codex_home)
    records: list[SessionRecord] = []
    for path in sessions_dir.rglob("*.jsonl"):
        if path.is_file():
            try:
                records.append(_parse_codex_session(path, codex_home, index))
            except Exception:
                continue
    return records


def _list_remote_claude_sessions(sftp, claude_home: str, ssh_name: str) -> list[SessionRecord]:
    projects_dir = posixpath.join(claude_home, "projects")
    records: list[SessionRecord] = []
    for project_attr in _remote_listdir_attr(sftp, projects_dir):
        project_dir = posixpath.join(projects_dir, project_attr.filename)
        if not _remote_attr_is_dir(project_attr) and not _remote_is_dir(sftp, project_dir):
            continue
        for file_attr in _remote_listdir_attr(sftp, project_dir):
            if _remote_attr_is_dir(file_attr) or not file_attr.filename.endswith(".jsonl"):
                continue
            path = posixpath.join(project_dir, file_attr.filename)
            try:
                records.append(_parse_remote_claude_session(path, claude_home, ssh_name, file_attr, _remote_read_text(sftp, path)))
            except Exception:
                continue
            if len(records) >= MAX_REMOTE_SESSION_FILES:
                return records
    return records


def _list_remote_codex_sessions(sftp, codex_home: str, ssh_name: str) -> list[SessionRecord]:
    sessions_dir = posixpath.join(codex_home, "sessions")
    index = _read_remote_codex_index(sftp, codex_home)
    records: list[SessionRecord] = []
    for path, attr in _remote_walk_files(sftp, sessions_dir, suffix=".jsonl", limit=MAX_REMOTE_SESSION_FILES):
        try:
            records.append(_parse_remote_codex_session(path, codex_home, ssh_name, attr, _remote_read_text(sftp, path), index))
        except Exception:
            continue
    return records


def _parse_claude_session(path: Path, claude_home: Path) -> SessionRecord:
    session_id = path.stem
    project_key = path.parent.name
    relative_path = _relative_to_home(path, claude_home)
    title = ""
    summary = ""
    project_path = ""
    created_at = ""
    updated_at = _file_time_iso(path)
    model = ""
    message_count = 0

    for item in _iter_jsonl(path):
        timestamp = str(item.get("timestamp") or "")
        created_at = created_at or timestamp
        updated_at = max(updated_at, timestamp) if timestamp else updated_at
        session_id = str(item.get("sessionId") or session_id)
        project_path = project_path or str(item.get("cwd") or "")

        if item.get("type") in {"user", "assistant"}:
            message_count += 1
        if item.get("type") == "user" and not summary:
            text = _extract_text(item.get("message", {}).get("content") if isinstance(item.get("message"), dict) else item)
            if not _is_context_message(text):
                summary = _clean_text(text)
                title = _title_from_summary(summary, f"Claude {session_id[:8]}")
        if item.get("type") == "assistant" and not model:
            message = item.get("message")
            if isinstance(message, dict):
                model = str(message.get("model") or "")

    title = title or f"Claude {session_id[:8]}"
    summary = summary or title
    return SessionRecord(
        key=f"claude:{relative_path}",
        provider="claude",
        session_id=session_id,
        title=title,
        summary=summary,
        source_path=path,
        relative_path=relative_path,
        project_key=project_key,
        project_path=project_path,
        created_at=created_at,
        updated_at=updated_at,
        size_bytes=path.stat().st_size,
        message_count=message_count,
        model=model,
    )


def _parse_codex_session(path: Path, codex_home: Path, index: dict[str, dict[str, Any]]) -> SessionRecord:
    relative_path = _relative_to_home(path, codex_home)
    session_id = path.stem
    title = ""
    summary = ""
    project_path = ""
    created_at = ""
    updated_at = _file_time_iso(path)
    model = ""
    message_count = 0

    for item in _iter_jsonl(path):
        timestamp = str(item.get("timestamp") or "")
        created_at = created_at or timestamp
        updated_at = max(updated_at, timestamp) if timestamp else updated_at

        item_type = item.get("type")
        payload = item.get("payload")
        if item_type == "session_meta" and isinstance(payload, dict):
            session_id = str(payload.get("id") or session_id)
            project_path = project_path or str(payload.get("cwd") or "")
            model = str(payload.get("model") or payload.get("model_provider") or model)
            created_at = str(payload.get("timestamp") or created_at)
        elif item_type == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "message":
                message_count += 1
                if payload.get("role") == "user" and not summary:
                    text = _extract_text(payload.get("content"))
                    if not _is_context_message(text):
                        summary = _clean_text(text)
                        title = _title_from_summary(summary, f"Codex {session_id[:8]}")

    indexed = index.get(session_id, {})
    indexed_title = _clean_text(str(indexed.get("thread_name") or ""))
    if indexed_title:
        title = indexed_title
        summary = summary or indexed_title
    indexed_updated_at = str(indexed.get("updated_at") or "")
    if indexed_updated_at:
        updated_at = indexed_updated_at

    title = title or f"Codex {session_id[:8]}"
    summary = summary or title
    return SessionRecord(
        key=f"codex:{relative_path}",
        provider="codex",
        session_id=session_id,
        title=title,
        summary=summary,
        source_path=path,
        relative_path=relative_path,
        project_path=project_path,
        created_at=created_at,
        updated_at=updated_at,
        size_bytes=path.stat().st_size,
        message_count=message_count,
        model=model,
    )


def _parse_remote_claude_session(path: str, claude_home: str, ssh_name: str, attr, text: str) -> SessionRecord:
    session_id = _posix_stem(path)
    project_key = posixpath.basename(posixpath.dirname(path))
    relative_path = _remote_relative_to_home(path, claude_home)
    title = ""
    summary = ""
    project_path = ""
    created_at = ""
    updated_at = _remote_file_time_iso(attr)
    model = ""
    message_count = 0

    for item in _iter_jsonl_text(text):
        timestamp = str(item.get("timestamp") or "")
        created_at = created_at or timestamp
        updated_at = max(updated_at, timestamp) if timestamp else updated_at
        session_id = str(item.get("sessionId") or session_id)
        project_path = project_path or str(item.get("cwd") or "")

        if item.get("type") in {"user", "assistant"}:
            message_count += 1
        if item.get("type") == "user" and not summary:
            text_value = _extract_text(item.get("message", {}).get("content") if isinstance(item.get("message"), dict) else item)
            if not _is_context_message(text_value):
                summary = _clean_text(text_value)
                title = _title_from_summary(summary, f"Claude {session_id[:8]}")
        if item.get("type") == "assistant" and not model:
            message = item.get("message")
            if isinstance(message, dict):
                model = str(message.get("model") or "")

    title = title or f"Claude {session_id[:8]}"
    summary = summary or title
    return SessionRecord(
        key=f"ssh:{ssh_name}:claude:{relative_path}",
        provider="claude",
        session_id=session_id,
        title=title,
        summary=summary,
        source_path=Path(relative_path),
        relative_path=relative_path,
        project_key=project_key,
        project_path=project_path,
        created_at=created_at,
        updated_at=updated_at,
        size_bytes=int(getattr(attr, "st_size", 0) or 0),
        message_count=message_count,
        model=model,
        origin="ssh",
        ssh_name=ssh_name,
        remote_path=path,
    )


def _parse_remote_codex_session(
    path: str,
    codex_home: str,
    ssh_name: str,
    attr,
    text: str,
    index: dict[str, dict[str, Any]],
) -> SessionRecord:
    relative_path = _remote_relative_to_home(path, codex_home)
    session_id = _posix_stem(path)
    title = ""
    summary = ""
    project_path = ""
    created_at = ""
    updated_at = _remote_file_time_iso(attr)
    model = ""
    message_count = 0

    for item in _iter_jsonl_text(text):
        timestamp = str(item.get("timestamp") or "")
        created_at = created_at or timestamp
        updated_at = max(updated_at, timestamp) if timestamp else updated_at

        item_type = item.get("type")
        payload = item.get("payload")
        if item_type == "session_meta" and isinstance(payload, dict):
            session_id = str(payload.get("id") or session_id)
            project_path = project_path or str(payload.get("cwd") or "")
            model = str(payload.get("model") or payload.get("model_provider") or model)
            created_at = str(payload.get("timestamp") or created_at)
        elif item_type == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "message":
                message_count += 1
                if payload.get("role") == "user" and not summary:
                    text_value = _extract_text(payload.get("content"))
                    if not _is_context_message(text_value):
                        summary = _clean_text(text_value)
                        title = _title_from_summary(summary, f"Codex {session_id[:8]}")

    indexed = index.get(session_id, {})
    indexed_title = _clean_text(str(indexed.get("thread_name") or ""))
    if indexed_title:
        title = indexed_title
        summary = summary or indexed_title
    indexed_updated_at = str(indexed.get("updated_at") or "")
    if indexed_updated_at:
        updated_at = indexed_updated_at

    title = title or f"Codex {session_id[:8]}"
    summary = summary or title
    return SessionRecord(
        key=f"ssh:{ssh_name}:codex:{relative_path}",
        provider="codex",
        session_id=session_id,
        title=title,
        summary=summary,
        source_path=Path(relative_path),
        relative_path=relative_path,
        project_path=project_path,
        created_at=created_at,
        updated_at=updated_at,
        size_bytes=int(getattr(attr, "st_size", 0) or 0),
        message_count=message_count,
        model=model,
        origin="ssh",
        ssh_name=ssh_name,
        remote_path=path,
    )


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield from _iter_jsonl_lines(handle)


def _iter_jsonl_text(text: str):
    yield from _iter_jsonl_lines(str(text or "").splitlines())


def _iter_jsonl_lines(lines):
    for index, line in enumerate(lines):
        if index >= MAX_SCAN_LINES:
            break
        line = str(line or "").strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_extract_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
    return ""


def _clean_text(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 1].rstrip() + "…"


def _is_context_message(text: str) -> bool:
    stripped = str(text or "").strip().lower()
    return stripped.startswith("<environment_context>") or stripped.startswith("<system_context>")


def _title_from_summary(summary: str, fallback: str) -> str:
    if not summary:
        return fallback
    title = summary.splitlines()[0].strip()
    return _clean_text(title, limit=64) or fallback


def _file_time_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _relative_to_home(path: Path, home: Path) -> str:
    return path.resolve().relative_to(home.resolve()).as_posix()


def _provider_home(provider: str, claude_home: Path, codex_home: Path) -> Path:
    if provider == "claude":
        return claude_home
    if provider == "codex":
        return codex_home
    raise ValueError(f"不支持的会话来源: {provider}")


def _connect_ssh(ssh_name: str):
    profiles = profile_manager.list_ssh_profiles()
    profile = next((item for item in profiles if item.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile, ssh_manager.connect(profile)


def _remote_provider_home(client, profile, provider: str) -> str:
    raw_path = remote_config._remote_dir(profile, provider)
    return remote_config._expand_remote_path(client, raw_path)


def _normalize_project_path(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return str(Path(text).expanduser().absolute())


def _normalize_remote_project_path(client, value: str | Path | None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    return remote_config._expand_remote_path(client, text)


def _claude_project_key_for_path(project_path: str) -> str:
    text = project_path.replace("/", "\\")
    if len(text) >= 2 and text[1] == ":":
        text = text[0].lower() + text[1:]
    return "".join(char if char.isascii() and char.isalnum() else "-" for char in text)


def _remap_relative_path(provider: str, relative_path: str, target_project_path: str) -> str:
    if provider != "claude" or not target_project_path:
        return relative_path
    pure = PurePosixPath(relative_path)
    parts = list(pure.parts)
    if len(parts) >= 3 and parts[0] == "projects":
        parts[1] = _claude_project_key_for_path(target_project_path)
        return PurePosixPath(*parts).as_posix()
    return relative_path


def _rewrite_jsonl_cwd(data: bytes, target_project_path: str) -> bytes:
    output: list[str] = []
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            output.append(raw_line)
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            output.append(raw_line)
            continue
        _rewrite_cwd_values(item, target_project_path)
        output.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(output) + ("\n" if output else "")).encode("utf-8")


def _rewrite_cwd_values(value: Any, target_project_path: str) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key == "cwd" and isinstance(item, str):
                value[key] = target_project_path
            else:
                _rewrite_cwd_values(item, target_project_path)
    elif isinstance(value, list):
        for item in value:
            _rewrite_cwd_values(item, target_project_path)


def _record_files(record: SessionRecord) -> list[Path]:
    files = [record.source_path]
    if record.provider == "claude":
        support_dir = record.source_path.with_suffix("")
        if support_dir.is_dir():
            files.extend(path for path in support_dir.rglob("*") if path.is_file())
    return files


def _remote_record_files(sftp, record: SessionRecord) -> list[str]:
    if not record.remote_path:
        return []
    files = [record.remote_path]
    if record.provider == "claude":
        support_dir = record.remote_path[:-6] if record.remote_path.endswith(".jsonl") else record.remote_path
        files.extend(path for path, _attr in _remote_walk_files(sftp, support_dir, suffix="", limit=MAX_REMOTE_SESSION_FILES))
    return files


def _safe_destination(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"不安全的相对路径: {relative_path}")
    target = (root / Path(*pure.parts)).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError(f"目标路径越界: {relative_path}")
    return target


def _safe_remote_destination(root: str, relative_path: str) -> str:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"不安全的相对路径: {relative_path}")
    target = posixpath.normpath(posixpath.join(root, *pure.parts))
    root_normalized = posixpath.normpath(root)
    if target != root_normalized and not target.startswith(root_normalized.rstrip("/") + "/"):
        raise ValueError(f"目标路径越界: {relative_path}")
    return target


def _remote_relative_to_home(path: str, home: str) -> str:
    normalized_path = posixpath.normpath(str(path or "").replace("\\", "/"))
    normalized_home = posixpath.normpath(str(home or "").replace("\\", "/"))
    if normalized_home == "/":
        return normalized_path.lstrip("/")
    prefix = normalized_home.rstrip("/") + "/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix):]
    raise ValueError(f"远端路径不在会话目录内: {path}")


def _safe_archive_path(archive_path: str) -> None:
    pure = PurePosixPath(archive_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"不安全的包内路径: {archive_path}")
    if not pure.parts or pure.parts[0] != "files":
        raise ValueError(f"不支持的包内路径: {archive_path}")


def _package_file_info(bundle: zipfile.ZipFile, archive_path: str) -> zipfile.ZipInfo:
    _safe_archive_path(archive_path)
    try:
        info = bundle.getinfo(archive_path)
    except KeyError as e:
        raise ValueError(f"会话迁移包缺少文件: {archive_path}") from e
    if info.is_dir():
        raise ValueError(f"会话迁移包条目不是文件: {archive_path}")
    if info.file_size > MAX_PACKAGE_FILE_BYTES:
        raise ValueError(f"会话迁移包文件过大: {archive_path}")
    return info


def _copy_package_file_atomic(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = temp_path_for(destination)
    try:
        with bundle.open(info, "r") as source, tmp_path.open("wb") as target:
            shutil.copyfileobj(source, target)
        replace_with_retry(tmp_path, destination)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_package_file_to_remote(sftp, bundle: zipfile.ZipFile, info: zipfile.ZipInfo, destination: str, file_mode: int | None = None) -> None:
    remote_dir = posixpath.dirname(destination)
    if remote_dir:
        ssh_manager._ensure_remote_dir(sftp, remote_dir)
    temp_path = f"{destination}.tmp.{uuid.uuid4().hex}"
    try:
        with bundle.open(info, "r") as source, sftp.open(temp_path, "wb") as target:
            shutil.copyfileobj(source, target)
        if file_mode is not None:
            try:
                sftp.chmod(temp_path, file_mode)
            except Exception:
                pass
        ssh_manager._replace_remote_file(sftp, temp_path, destination)
        if file_mode is not None:
            try:
                sftp.chmod(destination, file_mode)
            except Exception:
                pass
    except Exception:
        try:
            sftp.remove(temp_path)
        except Exception:
            pass
        raise


def _write_remote_bytes_atomic(sftp, destination: str, data: bytes, file_mode: int | None = None) -> None:
    remote_dir = posixpath.dirname(destination)
    if remote_dir:
        ssh_manager._ensure_remote_dir(sftp, remote_dir)
    temp_path = f"{destination}.tmp.{uuid.uuid4().hex}"
    try:
        with sftp.open(temp_path, "wb") as handle:
            handle.write(data)
        if file_mode is not None:
            try:
                sftp.chmod(temp_path, file_mode)
            except Exception:
                pass
        ssh_manager._replace_remote_file(sftp, temp_path, destination)
        if file_mode is not None:
            try:
                sftp.chmod(destination, file_mode)
            except Exception:
                pass
    except Exception:
        try:
            sftp.remove(temp_path)
        except Exception:
            pass
        raise


def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, Any]:
    try:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    except KeyError as e:
        raise ValueError("会话迁移包缺少 manifest.json") from e
    except json.JSONDecodeError as e:
        raise ValueError("会话迁移包 manifest 损坏") from e
    if manifest.get("format") != PACKAGE_FORMAT:
        raise ValueError("不是 API切换器会话迁移包")
    if manifest.get("version") != PACKAGE_VERSION:
        raise ValueError(f"不支持的会话迁移包版本: {manifest.get('version')}")
    if not isinstance(manifest.get("sessions"), list):
        raise ValueError("会话迁移包格式不完整")
    return manifest


def _read_codex_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with index_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = item
    return result


def _read_remote_codex_index(sftp, codex_home: str) -> dict[str, dict[str, Any]]:
    return _parse_codex_index_text(_remote_read_text(sftp, posixpath.join(codex_home, "session_index.jsonl"), missing_ok=True))


def _parse_codex_index_text(text: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line in str(text or "").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    return result


def _update_codex_index(codex_home: Path, sessions: list[dict[str, Any]]) -> None:
    index_path = codex_home / "session_index.jsonl"
    existing = _read_codex_index(codex_home)
    lines = _codex_index_lines(existing, sessions)
    if lines:
        atomic_write_text(index_path, "\n".join(lines) + "\n")


def _update_remote_codex_index(client, codex_home: str, sessions: list[dict[str, Any]]) -> None:
    index_path = posixpath.join(codex_home, "session_index.jsonl")
    existing = _parse_codex_index_text(ssh_manager.read_remote_file(client, index_path) or "")
    lines = _codex_index_lines(existing, sessions)
    if lines:
        ssh_manager.write_remote_file(client, index_path, "\n".join(lines) + "\n", file_mode=0o600)


def _codex_index_lines(existing: dict[str, dict[str, Any]], sessions: list[dict[str, Any]]) -> list[str]:
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        existing[session_id] = {
            "id": session_id,
            "thread_name": session.get("title") or session.get("summary") or session_id,
            "updated_at": session.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        }
    if not existing:
        return []
    return [
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in sorted(existing.values(), key=lambda value: str(value.get("updated_at") or ""))
    ]


def _remote_listdir_attr(sftp, path: str):
    try:
        return list(sftp.listdir_attr(path))
    except Exception:
        return []


def _remote_walk_files(sftp, root: str, suffix: str = "", limit: int = MAX_REMOTE_SESSION_FILES):
    if not _remote_is_dir(sftp, root):
        return
    pending = [posixpath.normpath(root)]
    yielded = 0
    while pending and yielded < limit:
        current = pending.pop(0)
        for attr in _remote_listdir_attr(sftp, current):
            path = posixpath.join(current, attr.filename)
            if _remote_attr_is_dir(attr) or (not _remote_attr_is_file(attr) and _remote_is_dir(sftp, path)):
                pending.append(path)
                continue
            if suffix and not path.endswith(suffix):
                continue
            yielded += 1
            yield path, attr
            if yielded >= limit:
                break


def _remote_attr_is_dir(attr) -> bool:
    mode = getattr(attr, "st_mode", None)
    return bool(mode is not None and stat.S_ISDIR(mode))


def _remote_attr_is_file(attr) -> bool:
    mode = getattr(attr, "st_mode", None)
    return bool(mode is not None and stat.S_ISREG(mode))


def _remote_is_dir(sftp, path: str) -> bool:
    try:
        return stat.S_ISDIR(getattr(sftp.stat(path), "st_mode", 0))
    except Exception:
        return False


def _remote_file_exists(sftp, path: str) -> bool:
    try:
        return stat.S_ISREG(getattr(sftp.stat(path), "st_mode", 0))
    except Exception:
        return False


def _remote_read_text(sftp, path: str, missing_ok: bool = False, max_bytes: int = MAX_REMOTE_PARSE_BYTES) -> str:
    try:
        with sftp.open(path, "rb") as handle:
            try:
                raw = handle.read(max_bytes + 1)
            except TypeError:
                raw = handle.read()
    except Exception:
        if missing_ok:
            return ""
        raise
    if isinstance(raw, str):
        return raw
    return bytes(raw[:max_bytes]).decode("utf-8-sig", errors="replace")


def _posix_stem(path: str) -> str:
    name = posixpath.basename(str(path or ""))
    return name.rsplit(".", 1)[0] if "." in name else name


def _remote_file_time_iso(attr) -> str:
    try:
        timestamp = float(getattr(attr, "st_mtime", 0) or 0)
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except Exception:
        pass
    return datetime.now(timezone.utc).isoformat()
