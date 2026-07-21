"""Local Claude Code and Codex session discovery and migration."""
from __future__ import annotations

import json
import os
import posixpath
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from core.atomic_io import replace_with_retry, temp_path_for
from core import profile_manager, remote_config
from core.ssh_manager import ssh_manager


PACKAGE_FORMAT = "api-switcher-session-migration"
PACKAGE_VERSION = 1
PACKAGE_EXTENSION = ".asxsession"
CONTENT_MODE_FULL = "full"
CONTENT_MODE_COMPACT = "compact"
COMPACT_TOOL_OUTPUT_LIMIT_BYTES = 256 * 1024
COMPACT_TOOL_OUTPUT_MARKER = "[API切换器精简迁移包：已省略超大工具输出]"
EXPORT_SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024
MAX_SCAN_LINES = 200
MAX_REMOTE_SESSION_FILES = 5000
MAX_REMOTE_PARSE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_REWRITE_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_SESSION_EXPORT_FILES = MAX_REMOTE_SESSION_FILES
LOCAL_SESSION_CACHE_MAX_ENTRIES = 32
_LOCAL_SESSION_CACHE_LOCK = threading.RLock()
_LOCAL_SESSION_CACHE: OrderedDict[
    tuple[str, str, str],
    tuple[tuple, tuple[Any, ...]],
] = OrderedDict()


class SessionSourceChangedError(ValueError):
    """A selected source changed after its export size was validated."""
_CODEX_INDEX_APPEND_LOCK = threading.RLock()


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
    content_mode: str = CONTENT_MODE_FULL
    omitted_output_count: int = 0
    omitted_bytes: int = 0


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
    content_mode: str = CONTENT_MODE_FULL
    omitted_output_count: int = 0
    omitted_bytes: int = 0


def default_claude_home() -> Path:
    return Path.home() / ".claude"


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def clear_local_session_cache() -> None:
    with _LOCAL_SESSION_CACHE_LOCK:
        _LOCAL_SESSION_CACHE.clear()


def _local_session_cache_key(provider: str, claude_home: Path, codex_home: Path) -> tuple[str, str, str]:
    return (
        str(provider or "all").lower(),
        str(Path(claude_home).expanduser().resolve(strict=False)),
        str(Path(codex_home).expanduser().resolve(strict=False)),
    )


def _session_file_signature(path: Path, root: Path, provider: str):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or _path_is_link_or_reparse(path, info):
            return None
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        return (provider, str(relative).replace("\\", "/"), int(info.st_size), int(info.st_mtime_ns))
    except OSError:
        return None


def _local_session_signature(provider: str, claude_home: Path, codex_home: Path) -> tuple:
    provider = str(provider or "all").lower()
    parts = []
    if provider in {"all", "claude"}:
        projects_dir = Path(claude_home) / "projects"
        if _local_directory_is_safe(projects_dir):
            for path in sorted(_iter_local_claude_session_files(projects_dir)):
                try:
                    _validate_local_source_file(path, projects_dir)
                except (OSError, ValueError):
                    continue
                signature = _session_file_signature(path, claude_home, "claude")
                if signature is not None:
                    parts.append(signature)
    if provider in {"all", "codex"}:
        codex_home = Path(codex_home)
        index_signature = _session_file_signature(codex_home / "session_index.jsonl", codex_home, "codex-index")
        if index_signature is not None:
            parts.append(index_signature)
        sessions_dir = codex_home / "sessions"
        if _local_directory_is_safe(sessions_dir):
            for path in sorted(path for path in _iter_local_files(sessions_dir) if path.suffix == ".jsonl"):
                try:
                    _validate_local_source_file(path, sessions_dir)
                except (OSError, ValueError):
                    continue
                signature = _session_file_signature(path, codex_home, "codex")
                if signature is not None:
                    parts.append(signature)
    return tuple(parts)


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
    cache_key = _local_session_cache_key(provider, claude_home, codex_home)
    signature = _local_session_signature(provider, claude_home, codex_home)
    with _LOCAL_SESSION_CACHE_LOCK:
        cached = _LOCAL_SESSION_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            _LOCAL_SESSION_CACHE.move_to_end(cache_key)
            return list(cached[1])

    records: list[SessionRecord] = []
    if provider in {"all", "claude"}:
        records.extend(_list_claude_sessions(claude_home))
    if provider in {"all", "codex"}:
        records.extend(_list_codex_sessions(codex_home))

    records = sorted(records, key=lambda item: item.updated_at or item.created_at, reverse=True)
    with _LOCAL_SESSION_CACHE_LOCK:
        _LOCAL_SESSION_CACHE.pop(cache_key, None)
        _LOCAL_SESSION_CACHE[cache_key] = (signature, tuple(records))
        while len(_LOCAL_SESSION_CACHE) > LOCAL_SESSION_CACHE_MAX_ENTRIES:
            _LOCAL_SESSION_CACHE.popitem(last=False)
    return list(records)


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
    content_mode: str = CONTENT_MODE_FULL,
) -> SessionExportResult:
    """Export selected sessions to a portable .asxsession zip package."""
    content_mode = _normalize_content_mode(content_mode)
    keys = {str(key) for key in selected_keys}
    if not keys:
        raise ValueError("请选择要导出的会话")

    claude_home = claude_home or default_claude_home()
    codex_home = codex_home or default_codex_home()
    output_path = Path(output_path)
    _validate_local_export_path(output_path, claude_home, codex_home)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = list_sessions("all", claude_home=claude_home, codex_home=codex_home)
    selected = [record for record in records if record.key in keys]
    skipped_keys = sorted(keys - {record.key for record in selected})
    if not selected:
        raise ValueError("没有找到可导出的会话")

    manifest_entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    omitted_output_count = 0
    omitted_bytes = 0

    tmp_output = temp_path_for(output_path)
    try:
        with zipfile.ZipFile(tmp_output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for index, record in enumerate(selected):
                home = _provider_home(record.provider, claude_home, codex_home)
                entry = record.to_manifest()
                entry["files"] = []

                for file_path in _record_files(
                    record,
                    include_support_files=content_mode == CONTENT_MODE_FULL,
                ):
                    if total_bytes >= MAX_PACKAGE_TOTAL_BYTES:
                        break
                    try:
                        relative_path, file_info = _local_export_file_info(
                            file_path,
                            home,
                            record.provider,
                        )
                        size = int(file_info.st_size)
                    except (OSError, ValueError):
                        continue
                    compact_main = content_mode == CONTENT_MODE_COMPACT and file_path == record.source_path
                    compact_output_limit = min(
                        MAX_PACKAGE_FILE_BYTES,
                        MAX_PACKAGE_TOTAL_BYTES - total_bytes,
                    )
                    if compact_main:
                        if compact_output_limit <= 0:
                            continue
                    elif not _package_size_allowed(size, total_bytes):
                        continue
                    if len(entry["files"]) >= MAX_SESSION_EXPORT_FILES:
                        raise ValueError(
                            f"单个会话导出文件数量超过安全上限（{MAX_SESSION_EXPORT_FILES} 个）"
                        )

                    archive_path = f"files/{index}/{relative_path}"
                    try:
                        source_root = _local_provider_session_root(home, record.provider)
                        with _open_validated_local_source(file_path, source_root, file_info) as source:
                            if compact_main:
                                written_size, file_omitted_count, file_omitted_bytes = _write_compact_stream_to_bundle(
                                    source,
                                    bundle,
                                    archive_path,
                                    record.provider,
                                    max_output_bytes=compact_output_limit,
                                )
                            else:
                                with bundle.open(archive_path, "w") as target:
                                    written_size = _copy_binary_stream(source, target, size)
                                file_omitted_count = 0
                                file_omitted_bytes = 0
                    except SessionSourceChangedError:
                        # The ZIP entry may already contain partial bytes. Abort
                        # the temporary package instead of returning a bloated
                        # archive whose manifest omits that orphan entry.
                        raise
                    except (OSError, ValueError):
                        continue
                    file_entry = {
                        "relative_path": relative_path,
                        "archive_path": archive_path,
                        "size": written_size,
                        "main": file_path == record.source_path,
                    }
                    if file_omitted_count:
                        file_entry.update({
                            "source_size": size,
                            "compacted": True,
                            "omitted_output_count": file_omitted_count,
                            "omitted_bytes": file_omitted_bytes,
                        })
                    entry["files"].append(file_entry)
                    file_count += 1
                    total_bytes += written_size
                    omitted_output_count += file_omitted_count
                    omitted_bytes += file_omitted_bytes

                if _entry_has_main_file(entry):
                    manifest_entries.append(entry)
                else:
                    skipped_keys.append(record.key)

            manifest = {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "content_mode": content_mode,
                "compact_policy": {
                    "max_tool_output_bytes": COMPACT_TOOL_OUTPUT_LIMIT_BYTES,
                    "include_claude_support_files": False,
                } if content_mode == CONTENT_MODE_COMPACT else None,
                "omitted_output_count": omitted_output_count,
                "omitted_bytes": omitted_bytes,
                "sessions": manifest_entries,
            }
            bundle.writestr("manifest.json", _encode_manifest(manifest))
        replace_with_retry(tmp_output, output_path)
    except Exception:
        tmp_output.unlink(missing_ok=True)
        raise

    return SessionExportResult(
        path=output_path,
        session_count=len(manifest_entries),
        file_count=file_count,
        total_bytes=total_bytes,
        skipped_keys=sorted(set(skipped_keys)),
        content_mode=content_mode,
        omitted_output_count=omitted_output_count,
        omitted_bytes=omitted_bytes,
    )


def export_remote_sessions(
    ssh_name: str,
    output_path: str | Path,
    selected_keys: set[str] | list[str] | tuple[str, ...],
    provider: str = "all",
    content_mode: str = CONTENT_MODE_FULL,
) -> SessionExportResult:
    """Export selected sessions from an SSH server to a local .asxsession package."""
    content_mode = _normalize_content_mode(content_mode)
    keys = {str(key) for key in selected_keys}
    if not keys:
        raise ValueError("请选择要导出的会话")

    output_path = Path(output_path)
    _validate_local_export_path(output_path, default_claude_home(), default_codex_home())
    profile, client = _connect_ssh(ssh_name)
    claude_home = _remote_provider_home(client, profile, "claude")
    codex_home = _remote_provider_home(client, profile, "codex")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = list_remote_sessions(ssh_name, provider)
    selected = [record for record in records if record.key in keys]
    skipped_keys = sorted(keys - {record.key for record in selected})
    if not selected:
        raise ValueError("没有找到可导出的会话")

    manifest_entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    omitted_output_count = 0
    omitted_bytes = 0
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

                for remote_path in _remote_record_files(
                    sftp,
                    record,
                    include_support_files=content_mode == CONTENT_MODE_FULL,
                ):
                    if total_bytes >= MAX_PACKAGE_TOTAL_BYTES:
                        break
                    try:
                        relative_path, info = _remote_export_file_info(
                            sftp,
                            remote_path,
                            home,
                            record.provider,
                        )
                    except Exception:
                        continue
                    size = int(getattr(info, "st_size", 0) or 0)
                    compact_main = content_mode == CONTENT_MODE_COMPACT and remote_path == record.remote_path
                    compact_output_limit = min(
                        MAX_PACKAGE_FILE_BYTES,
                        MAX_PACKAGE_TOTAL_BYTES - total_bytes,
                    )
                    if compact_main:
                        if compact_output_limit <= 0:
                            continue
                    elif not _package_size_allowed(size, total_bytes):
                        continue
                    if len(entry["files"]) >= MAX_SESSION_EXPORT_FILES:
                        raise ValueError(
                            f"单个会话导出文件数量超过安全上限（{MAX_SESSION_EXPORT_FILES} 个）"
                        )

                    archive_path = f"files/{index}/{relative_path}"
                    try:
                        with sftp.open(remote_path, "rb") as source:
                            _validate_open_remote_source(sftp, remote_path, source, info)
                            if compact_main:
                                written_size, file_omitted_count, file_omitted_bytes = _write_compact_stream_to_bundle(
                                    source,
                                    bundle,
                                    archive_path,
                                    record.provider,
                                    max_output_bytes=compact_output_limit,
                                )
                            else:
                                written_size = _write_binary_stream_to_bundle(
                                    source,
                                    bundle,
                                    archive_path,
                                    expected_size=size,
                                )
                                file_omitted_count = 0
                                file_omitted_bytes = 0
                    except Exception:
                        continue
                    file_entry = {
                        "relative_path": relative_path,
                        "archive_path": archive_path,
                        "size": written_size,
                        "main": remote_path == record.remote_path,
                    }
                    if file_omitted_count:
                        file_entry.update({
                            "source_size": size,
                            "compacted": True,
                            "omitted_output_count": file_omitted_count,
                            "omitted_bytes": file_omitted_bytes,
                        })
                    entry["files"].append(file_entry)
                    file_count += 1
                    total_bytes += written_size
                    omitted_output_count += file_omitted_count
                    omitted_bytes += file_omitted_bytes

                if _entry_has_main_file(entry):
                    manifest_entries.append(entry)
                else:
                    skipped_keys.append(record.key)

            manifest = {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "content_mode": content_mode,
                "compact_policy": {
                    "max_tool_output_bytes": COMPACT_TOOL_OUTPUT_LIMIT_BYTES,
                    "include_claude_support_files": False,
                } if content_mode == CONTENT_MODE_COMPACT else None,
                "omitted_output_count": omitted_output_count,
                "omitted_bytes": omitted_bytes,
                "sessions": manifest_entries,
            }
            bundle.writestr("manifest.json", _encode_manifest(manifest))
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
        skipped_keys=sorted(set(skipped_keys)),
        content_mode=content_mode,
        omitted_output_count=omitted_output_count,
        omitted_bytes=omitted_bytes,
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
            file_entries = session.get("files")
            if not isinstance(file_entries, list):
                skipped_invalid += 1
                continue
            for file_entry in file_entries:
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
                    _validate_provider_session_path(provider, relative_path)
                    destination = _safe_destination(home, relative_path)
                    destination_exists = _validate_local_import_destination(home, destination)
                except ValueError:
                    skipped_invalid += 1
                    continue

                if destination_exists and not overwrite:
                    skipped_existing += 1
                    continue
                if imported_bytes + info.file_size > MAX_PACKAGE_TOTAL_BYTES:
                    skipped_invalid += 1
                    continue

                imported_size = info.file_size
                if target_project_text and destination.suffix.lower() == ".jsonl":
                    rewritten_size = _copy_rewritten_package_file_atomic(
                        bundle,
                        info,
                        destination,
                        target_project_text,
                        root=home,
                        overwrite=overwrite,
                        max_output_bytes=min(
                            MAX_PACKAGE_FILE_BYTES,
                            MAX_PACKAGE_TOTAL_BYTES - imported_bytes,
                        ),
                    )
                    written = rewritten_size is not None
                    if written:
                        imported_size = rewritten_size
                else:
                    written = _copy_package_file_atomic(
                        bundle,
                        info,
                        destination,
                        root=home,
                        overwrite=overwrite,
                    )
                if not written:
                    skipped_existing += 1
                    continue
                imported_bytes += imported_size
                file_count += 1
                imported_main = imported_main or file_entry.get("main") is True

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
                file_entries = session.get("files")
                if not isinstance(file_entries, list):
                    skipped_invalid += 1
                    continue
                for file_entry in file_entries:
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
                        _validate_provider_session_path(provider, relative_path)
                        destination = _safe_remote_destination(home, relative_path)
                    except ValueError:
                        skipped_invalid += 1
                        continue

                    if imported_bytes + info.file_size > MAX_PACKAGE_TOTAL_BYTES:
                        skipped_invalid += 1
                        continue
                    try:
                        destination_exists = _prepare_remote_import_destination(sftp, home, destination)
                    except ValueError:
                        skipped_invalid += 1
                        continue
                    if destination_exists and not overwrite:
                        skipped_existing += 1
                        continue

                    imported_size = info.file_size
                    if target_project_text and destination.lower().endswith(".jsonl"):
                        rewritten_size = _copy_rewritten_package_file_to_remote(
                            sftp,
                            bundle,
                            info,
                            destination,
                            target_project_text,
                            root=home,
                            overwrite=overwrite,
                            file_mode=0o600,
                            max_output_bytes=min(
                                MAX_PACKAGE_FILE_BYTES,
                                MAX_PACKAGE_TOTAL_BYTES - imported_bytes,
                            ),
                        )
                        written = rewritten_size is not None
                        if written:
                            imported_size = rewritten_size
                    else:
                        written = _copy_package_file_to_remote(
                            sftp,
                            bundle,
                            info,
                            destination,
                            root=home,
                            overwrite=overwrite,
                            file_mode=0o600,
                        )
                    if not written:
                        skipped_existing += 1
                        continue
                    imported_bytes += imported_size
                    file_count += 1
                    imported_main = imported_main or file_entry.get("main") is True

                if imported_main:
                    key = f"{provider}:{session.get('relative_path', '')}"
                    imported_sessions.add(key)
                    if provider == "codex":
                        imported_codex.append(session)
        if imported_codex:
            _update_remote_codex_index(sftp, codex_home, imported_codex)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass

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
            file_entries = session.get("files")
            if not isinstance(file_entries, list):
                continue
            for file_entry in file_entries:
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
            content_mode=_normalize_content_mode(manifest.get("content_mode", CONTENT_MODE_FULL)),
            omitted_output_count=_nonnegative_manifest_int(manifest.get("omitted_output_count")),
            omitted_bytes=_nonnegative_manifest_int(manifest.get("omitted_bytes")),
        )


def format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size_bytes} B"


def _normalize_content_mode(value: str) -> str:
    mode = str(value or CONTENT_MODE_FULL).strip().lower()
    if mode not in {CONTENT_MODE_FULL, CONTENT_MODE_COMPACT}:
        raise ValueError(f"不支持的会话迁移内容模式: {value}")
    return mode


def _nonnegative_manifest_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _encode_manifest(manifest: dict[str, Any]) -> bytes:
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("会话迁移包 manifest 超过安全大小上限")
    return encoded


def _json_value_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8", errors="replace"))


def _compact_tool_output(value: Any, *, provider: str) -> tuple[Any, int]:
    original_size = _json_value_size(value)
    if original_size <= COMPACT_TOOL_OUTPUT_LIMIT_BYTES:
        return value, 0
    if isinstance(value, list):
        block_type = "text" if provider == "claude" else "input_text"
        replacement: Any = [{"type": block_type, "text": COMPACT_TOOL_OUTPUT_MARKER}]
    else:
        replacement = COMPACT_TOOL_OUTPUT_MARKER
    omitted_bytes = max(0, original_size - _json_value_size(replacement))
    return replacement, omitted_bytes


def _compact_session_item(item: dict[str, Any], provider: str) -> tuple[int, int]:
    omitted_output_count = 0
    omitted_bytes = 0

    if provider == "codex" and item.get("type") == "response_item":
        payload = item.get("payload")
        if isinstance(payload, dict) and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        } and "output" in payload:
            replacement, removed = _compact_tool_output(payload.get("output"), provider=provider)
            if removed:
                payload["output"] = replacement
                omitted_output_count = 1
                omitted_bytes = removed

    if provider == "claude" and item.get("type") == "user":
        message = item.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result" or "content" not in block:
                    continue
                replacement, removed = _compact_tool_output(block.get("content"), provider=provider)
                if removed:
                    block["content"] = replacement
                    omitted_output_count += 1
                    omitted_bytes += removed

    # Claude currently duplicates many tool results in this top-level field.
    # Compacting only message.content would therefore leave the large payload
    # (and any secrets it contains) in the package a second time.
    if provider == "claude" and "toolUseResult" in item:
        replacement, removed = _compact_tool_output(item.get("toolUseResult"), provider=provider)
        if removed:
            item["toolUseResult"] = replacement
            omitted_output_count += 1
            omitted_bytes += removed

    return omitted_output_count, omitted_bytes


def _compact_session_jsonl_line(raw_line: bytes, provider: str) -> tuple[bytes, int, int]:
    try:
        item = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_line, 0, 0
    if not isinstance(item, dict):
        return raw_line, 0, 0

    omitted_output_count, omitted_bytes = _compact_session_item(item, provider)
    if not omitted_output_count:
        return raw_line, 0, 0

    if raw_line.endswith(b"\r\n"):
        newline = b"\r\n"
    elif raw_line.endswith(b"\n"):
        newline = b"\n"
    else:
        newline = b""
    compacted = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + newline
    return compacted, omitted_output_count, omitted_bytes


def _copy_session_jsonl(
    source,
    target,
    provider: str,
    *,
    compact: bool,
    max_output_bytes: int | None = None,
) -> tuple[int, int, int]:
    written_size = 0
    omitted_output_count = 0
    omitted_bytes = 0
    for raw_line in source:
        if isinstance(raw_line, str):
            raw_line = raw_line.encode("utf-8")
        output_line = raw_line
        line_omitted_count = 0
        line_omitted_bytes = 0
        if compact:
            output_line, line_omitted_count, line_omitted_bytes = _compact_session_jsonl_line(
                raw_line,
                provider,
            )
        if max_output_bytes is not None and written_size + len(output_line) > max_output_bytes:
            raise ValueError("精简后的会话仍超过单文件或迁移包大小上限")
        target.write(output_line)
        written_size += len(output_line)
        omitted_output_count += line_omitted_count
        omitted_bytes += line_omitted_bytes
    return written_size, omitted_output_count, omitted_bytes


def _write_compact_stream_to_bundle(
    source,
    bundle: zipfile.ZipFile,
    archive_path: str,
    provider: str,
    *,
    max_output_bytes: int,
) -> tuple[int, int, int]:
    """Compact before opening the ZIP entry so an oversize result leaves no orphan data."""
    with tempfile.SpooledTemporaryFile(max_size=EXPORT_SPOOL_MAX_MEMORY_BYTES, mode="w+b") as compacted:
        result = _copy_session_jsonl(
            source,
            compacted,
            provider,
            compact=True,
            max_output_bytes=max_output_bytes,
        )
        compacted.seek(0)
        with bundle.open(archive_path, "w") as target:
            shutil.copyfileobj(compacted, target)
        return result


def _write_binary_stream_to_bundle(
    source,
    bundle: zipfile.ZipFile,
    archive_path: str,
    *,
    expected_size: int,
) -> int:
    """Spool a changing remote source before creating its ZIP entry."""
    with tempfile.SpooledTemporaryFile(max_size=EXPORT_SPOOL_MAX_MEMORY_BYTES, mode="w+b") as staged:
        written = _copy_binary_stream(source, staged, expected_size)
        staged.seek(0)
        with bundle.open(archive_path, "w") as target:
            shutil.copyfileobj(staged, target)
        return written


def _package_size_allowed(file_size: int, current_total: int) -> bool:
    if file_size < 0:
        return False
    return file_size <= MAX_PACKAGE_FILE_BYTES and current_total + file_size <= MAX_PACKAGE_TOTAL_BYTES


def _entry_has_main_file(entry: dict[str, Any]) -> bool:
    files = entry.get("files")
    return isinstance(files, list) and any(isinstance(item, dict) and item.get("main") for item in files)


def _list_claude_sessions(claude_home: Path) -> list[SessionRecord]:
    projects_dir = claude_home / "projects"
    if not _local_directory_is_safe(projects_dir):
        return []

    records: list[SessionRecord] = []
    for path in _iter_local_claude_session_files(projects_dir):
        try:
            _validate_local_source_file(path, projects_dir)
            records.append(_parse_claude_session(path, claude_home))
        except Exception:
            continue
    return records


def _list_codex_sessions(codex_home: Path) -> list[SessionRecord]:
    sessions_dir = codex_home / "sessions"
    if not _local_directory_is_safe(sessions_dir):
        return []

    index = _read_codex_index(codex_home)
    records: list[SessionRecord] = []
    for path in (item for item in _iter_local_files(sessions_dir) if item.suffix == ".jsonl"):
        try:
            _validate_local_source_file(path, sessions_dir)
            records.append(_parse_codex_session(path, codex_home, index))
        except Exception:
            continue
    return records


def _list_remote_claude_sessions(sftp, claude_home: str, ssh_name: str) -> list[SessionRecord]:
    projects_dir = posixpath.join(claude_home, "projects")
    records: list[SessionRecord] = []
    projects_attr = _remote_lstat(sftp, projects_dir)
    if projects_attr is None or not _remote_attr_is_dir(projects_attr) or _remote_attr_is_link(projects_attr):
        return records
    for project_attr in _remote_listdir_attr(sftp, projects_dir):
        if not _safe_remote_child_name(getattr(project_attr, "filename", "")):
            continue
        project_dir = posixpath.join(projects_dir, project_attr.filename)
        actual_project_attr = _remote_lstat(sftp, project_dir, fallback_attr=project_attr)
        if (
            actual_project_attr is None
            or _remote_attr_is_link(actual_project_attr)
            or not _remote_attr_is_dir(actual_project_attr)
        ):
            continue
        for file_attr in _remote_listdir_attr(sftp, project_dir):
            if (
                not _safe_remote_child_name(getattr(file_attr, "filename", ""))
                or not file_attr.filename.endswith(".jsonl")
            ):
                continue
            path = posixpath.join(project_dir, file_attr.filename)
            actual_file_attr = _remote_lstat(sftp, path, fallback_attr=file_attr)
            if (
                actual_file_attr is None
                or _remote_attr_is_link(actual_file_attr)
                or not _remote_attr_is_file(actual_file_attr)
            ):
                continue
            try:
                records.append(_parse_remote_claude_session(
                    path,
                    claude_home,
                    ssh_name,
                    actual_file_attr,
                    _remote_read_text(sftp, path),
                ))
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
    generated_title = ""

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
        if item.get("type") == "ai-title":
            generated_title = _session_generated_title(item) or generated_title

    title = generated_title or title
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
    if indexed_title and not _is_context_message(indexed_title):
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
    generated_title = ""

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
        if item.get("type") == "ai-title":
            generated_title = _session_generated_title(item) or generated_title

    title = generated_title or title
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
    if indexed_title and not _is_context_message(indexed_title):
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


def _session_generated_title(item: dict[str, Any]) -> str:
    title = _clean_text(str(item.get("aiTitle") or item.get("title") or item.get("thread_name") or ""), limit=64)
    if not title or _is_context_message(title):
        return ""
    return title


def _is_context_message(text: str) -> bool:
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    if not lowered:
        return True
    context_prefixes = (
        "<environment_context>",
        "<system_context>",
        "<developer_context>",
        "<user_editable_context>",
        "<hook_prompt",
        "<command-",
        "<local-command-",
        "<local-command-caveat>",
        "<task-notification>",
        "# agents.md instructions for ",
        "agents.md instructions for ",
        "# claude.md instructions for ",
        "claude.md instructions for ",
    )
    if lowered.startswith(context_prefixes):
        return True
    if lowered.startswith("caveat: the messages below were generated by the user while running local commands"):
        return True
    if lowered.startswith("please continue from where you left off. complete any remaining work."):
        return True
    if lowered.startswith("reconnecting...") and not any(char in lowered for char in "？?。.!"):
        return True
    return False


def _title_from_summary(summary: str, fallback: str) -> str:
    if not summary:
        return fallback
    title = summary.splitlines()[0].strip()
    return _clean_text(title, limit=64) or fallback


def _file_time_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _relative_to_home(path: Path, home: Path) -> str:
    return path.resolve().relative_to(home.resolve()).as_posix()


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _path_is_link_or_reparse(path: Path, info: os.stat_result | None = None) -> bool:
    try:
        info = info or path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if int(getattr(info, "st_file_attributes", 0) or 0) & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError:
            return True
    return False


def _local_directory_is_safe(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISDIR(info.st_mode) and not _path_is_link_or_reparse(path, info)
    except OSError:
        return False


def _validate_local_source_file(path: Path, source_root: Path) -> os.stat_result:
    """Return lstat data only for a regular, non-reparse file below source_root."""
    path = _absolute_lexical_path(path)
    source_root = _absolute_lexical_path(source_root)
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"会话源文件越界: {path}") from exc
    if not relative.parts or not _local_directory_is_safe(source_root):
        raise ValueError(f"会话源目录不安全: {source_root}")

    current = source_root
    for part in relative.parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"会话源目录不可访问: {current}") from exc
        if not stat.S_ISDIR(info.st_mode) or _path_is_link_or_reparse(current, info):
            raise ValueError(f"会话源目录包含链接或重解析点: {current}")

    try:
        file_info = path.lstat()
    except OSError as exc:
        raise ValueError(f"会话源文件不可访问: {path}") from exc
    if not stat.S_ISREG(file_info.st_mode) or _path_is_link_or_reparse(path, file_info):
        raise ValueError(f"会话源文件不是普通文件: {path}")

    resolved_root = source_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"会话源文件真实路径越界: {path}") from exc
    return file_info


def _local_provider_session_root(home: Path, provider: str) -> Path:
    source_root_name = {"claude": "projects", "codex": "sessions"}.get(provider)
    if source_root_name is None:
        raise ValueError(f"不支持的会话来源: {provider}")
    return Path(home) / source_root_name


def _local_export_file_info(path: Path, home: Path, provider: str) -> tuple[str, os.stat_result]:
    source_root = _local_provider_session_root(home, provider)
    info = _validate_local_source_file(path, source_root)
    resolved_home = Path(home).resolve(strict=False)
    resolved_path = Path(path).resolve(strict=True)
    try:
        relative = resolved_path.relative_to(resolved_home).as_posix()
    except ValueError as exc:
        raise ValueError(f"会话源文件不在来源目录内: {path}") from exc
    _validate_provider_session_path(provider, relative)
    return relative, info


@contextmanager
def _open_validated_local_source(path: Path, source_root: Path, expected_info: os.stat_result):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    handle = None
    try:
        actual_info = os.fstat(descriptor)
        current_info = _validate_local_source_file(path, source_root)
        if (
            not stat.S_ISREG(actual_info.st_mode)
            or (actual_info.st_dev, actual_info.st_ino) != (current_info.st_dev, current_info.st_ino)
            or (expected_info.st_dev, expected_info.st_ino) != (actual_info.st_dev, actual_info.st_ino)
        ):
            raise ValueError(f"会话源文件在导出期间发生变化: {path}")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        yield handle
    finally:
        if handle is not None:
            handle.close()
        if descriptor >= 0:
            os.close(descriptor)


def _copy_binary_stream(source, target, expected_size: int) -> int:
    written = 0
    while True:
        chunk = source.read(min(1024 * 1024, expected_size - written + 1))
        if not chunk:
            return written
        written += len(chunk)
        if written > expected_size:
            raise SessionSourceChangedError("会话源文件在导出期间增大")
        target.write(chunk)


def _provider_home(provider: str, claude_home: Path, codex_home: Path) -> Path:
    if provider == "claude":
        return claude_home
    if provider == "codex":
        return codex_home
    raise ValueError(f"不支持的会话来源: {provider}")


def _validate_local_export_path(output_path: Path, claude_home: Path, codex_home: Path) -> None:
    """Keep an exported ZIP outside the live session trees.

    Replacing a selected JSONL file (or creating the ZIP inside a Claude
    support directory that is being archived) can destroy the source session
    or make the archive include its own temporary output.
    """
    resolved_output = Path(output_path).expanduser().resolve(strict=False)
    for home in (claude_home, codex_home):
        resolved_home = Path(home).expanduser().resolve(strict=False)
        if resolved_output == resolved_home or resolved_home in resolved_output.parents:
            raise ValueError("会话迁移包不能保存在 Claude/Codex 会话目录内")


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
    source = BytesIO(bytes(data))
    target = BytesIO()
    _copy_rewritten_jsonl_stream(
        source,
        target,
        target_project_path,
        max_output_bytes=MAX_PACKAGE_FILE_BYTES,
    )
    return target.getvalue()


def _copy_rewritten_jsonl_stream(
    source,
    target,
    target_project_path: str,
    *,
    max_output_bytes: int,
) -> int:
    """Rewrite JSONL cwd values incrementally instead of loading the whole session."""
    written = 0
    while True:
        raw_line = source.readline(MAX_REWRITE_JSONL_LINE_BYTES + 1)
        if not raw_line:
            break
        if isinstance(raw_line, str):
            raw_bytes = raw_line.encode("utf-8")
        else:
            raw_bytes = bytes(raw_line)
        if len(raw_bytes) > MAX_REWRITE_JSONL_LINE_BYTES:
            raise ValueError(
                "项目重映射的 JSONL 单行超过安全上限"
                f"（{MAX_REWRITE_JSONL_LINE_BYTES} 字节）"
            )
        if isinstance(raw_line, str):
            line = raw_line.rstrip("\r\n")
        else:
            line = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        output_line = line
        if line.strip():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                pass
            else:
                _rewrite_cwd_values(item, target_project_path)
                output_line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        encoded = (output_line + "\n").encode("utf-8")
        if written + len(encoded) > max_output_bytes:
            raise ValueError("项目重映射后的会话文件超过迁移包大小上限")
        target.write(encoded)
        written += len(encoded)
    return written


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


def _record_files(record: SessionRecord, *, include_support_files: bool = True):
    yield record.source_path
    if include_support_files and record.provider == "claude":
        support_dir = record.source_path.with_suffix("")
        if support_dir.is_dir():
            yield from _iter_local_files(support_dir)


def _iter_local_claude_session_files(projects_root: Path):
    projects_root = _absolute_lexical_path(projects_root)
    if not _local_directory_is_safe(projects_root):
        return
    try:
        with os.scandir(projects_root) as projects:
            for project_entry in projects:
                if not _safe_local_child_name(project_entry.name):
                    continue
                project_dir = projects_root / project_entry.name
                if not _local_directory_is_safe(project_dir):
                    continue
                try:
                    with os.scandir(project_dir) as entries:
                        for entry in entries:
                            if not entry.name.endswith(".jsonl") or not _safe_local_child_name(entry.name):
                                continue
                            path = project_dir / entry.name
                            try:
                                _validate_local_source_file(path, projects_root)
                                yield path
                            except (OSError, ValueError):
                                continue
                except OSError:
                    continue
    except OSError:
        return


def _iter_local_files(root: Path):
    root = _absolute_lexical_path(root)
    if not _local_directory_is_safe(root):
        return
    try:
        walker = os.walk(root, onerror=lambda _error: None)
        for dirpath, dirnames, filenames in walker:
            current_dir = Path(dirpath)
            safe_dirnames: list[str] = []
            for dirname in dirnames:
                child = current_dir / dirname
                if not _safe_local_child_name(dirname) or not _local_directory_is_safe(child):
                    continue
                try:
                    child.resolve(strict=True).relative_to(root.resolve(strict=True))
                except (OSError, ValueError):
                    continue
                safe_dirnames.append(dirname)
            dirnames[:] = safe_dirnames
            for filename in filenames:
                if not _safe_local_child_name(filename):
                    continue
                path = current_dir / filename
                try:
                    _validate_local_source_file(path, root)
                    yield path
                except (OSError, ValueError):
                    continue
    except OSError:
        return


def _remote_record_files(sftp, record: SessionRecord, *, include_support_files: bool = True):
    if not record.remote_path:
        return
    yield record.remote_path
    if include_support_files and record.provider == "claude":
        support_dir = record.remote_path[:-6] if record.remote_path.endswith(".jsonl") else record.remote_path
        yield from (
            path
            for path, _attr in _remote_walk_files(
                sftp,
                support_dir,
                suffix="",
                limit=MAX_REMOTE_SESSION_FILES,
            )
        )


def _safe_destination(root: Path, relative_path: str) -> Path:
    pure = _validated_portable_relative_path(relative_path)
    lexical_root = _absolute_lexical_path(root)
    target = lexical_root / Path(*pure.parts)
    resolved_root = lexical_root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(f"目标路径越界: {relative_path}")
    return target


def _validate_provider_session_path(provider: str, relative_path: str) -> None:
    """Restrict package writes to provider-owned session subtrees."""
    pure = _validated_portable_relative_path(relative_path)
    expected_root = {"claude": "projects", "codex": "sessions"}.get(provider)
    if (
        expected_root is None
        or len(pure.parts) < 2
        or pure.parts[0] != expected_root
    ):
        raise ValueError(f"不支持的 {provider} 会话路径: {relative_path}")


def _validated_portable_relative_path(relative_path: str) -> PurePosixPath:
    text = str(relative_path or "")
    if (
        not text
        or text != text.strip()
        or "\\" in text
        or ":" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise ValueError(f"不安全的相对路径: {relative_path}")
    windows_path = PureWindowsPath(text)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise ValueError(f"不安全的 Windows 路径: {relative_path}")
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or not pure.parts
        or pure.as_posix() != text
        or any(not _safe_portable_path_component(part) for part in pure.parts)
    ):
        raise ValueError(f"不安全的相对路径: {relative_path}")
    return pure


def _safe_portable_path_component(part: str) -> bool:
    if part in {"", ".", ".."} or part.endswith((" ", ".")):
        return False
    base = part.split(".", 1)[0].rstrip(" .").upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    return base not in reserved


def _safe_local_child_name(name: str) -> bool:
    text = str(name or "")
    return (
        text not in {"", ".", ".."}
        and "/" not in text
        and "\\" not in text
        and "\x00" not in text
    )


def _safe_remote_destination(root: str, relative_path: str) -> str:
    pure = _validated_portable_relative_path(relative_path)
    target = posixpath.normpath(posixpath.join(root, *pure.parts))
    root_normalized = posixpath.normpath(root)
    if target != root_normalized and not target.startswith(root_normalized.rstrip("/") + "/"):
        raise ValueError(f"目标路径越界: {relative_path}")
    return target


def _remote_relative_to_home(path: str, home: str) -> str:
    raw_path = str(path or "")
    raw_home = str(home or "")
    if "\\" in raw_path or "\x00" in raw_path or "\\" in raw_home or "\x00" in raw_home:
        raise ValueError(f"远端路径格式不安全: {path}")
    normalized_path = posixpath.normpath(raw_path)
    normalized_home = posixpath.normpath(raw_home)
    if normalized_home == "/":
        return normalized_path.lstrip("/")
    prefix = normalized_home.rstrip("/") + "/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix):]
    raise ValueError(f"远端路径不在会话目录内: {path}")


def _safe_remote_child_name(name: str) -> bool:
    text = str(name or "")
    return (
        text not in {"", ".", ".."}
        and "/" not in text
        and "\\" not in text
        and "\x00" not in text
    )


def _remote_lstat(sftp, path: str, *, fallback_attr=None):
    lstat_method = getattr(sftp, "lstat", None)
    if callable(lstat_method):
        try:
            return lstat_method(path)
        except Exception as exc:
            if ssh_manager._is_not_found_error(exc):
                return None
            raise
    if fallback_attr is not None:
        return fallback_attr
    try:
        return sftp.stat(path)
    except Exception as exc:
        if ssh_manager._is_not_found_error(exc):
            return None
        raise


def _remote_attr_is_link(attr) -> bool:
    mode = getattr(attr, "st_mode", None)
    return bool(mode is not None and stat.S_ISLNK(mode))


def _remote_source_root(home: str, provider: str) -> str:
    source_root_name = {"claude": "projects", "codex": "sessions"}.get(provider)
    if source_root_name is None:
        raise ValueError(f"不支持的会话来源: {provider}")
    return posixpath.normpath(posixpath.join(home, source_root_name))


def _remote_path_is_within(path: str, root: str) -> bool:
    normalized_path = posixpath.normpath(path)
    normalized_root = posixpath.normpath(root)
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root.rstrip("/") + "/")


def _validate_remote_source_file(sftp, path: str, source_root: str):
    normalized_path = posixpath.normpath(str(path or ""))
    normalized_root = posixpath.normpath(str(source_root or ""))
    if (
        "\\" in str(path or "")
        or "\x00" in str(path or "")
        or not _remote_path_is_within(normalized_path, normalized_root)
        or normalized_path == normalized_root
    ):
        raise ValueError(f"远端会话源文件越界: {path}")

    # Paramiko SFTP clients always expose lstat.  A small stat-only fallback is
    # kept for lightweight adapters, while real SSH exports take the no-follow
    # path below.
    if not callable(getattr(sftp, "lstat", None)):
        file_attr = sftp.stat(normalized_path)
        mode = getattr(file_attr, "st_mode", None)
        if mode is not None and not stat.S_ISREG(mode):
            raise ValueError(f"远端会话源不是普通文件: {path}")
        return file_attr

    root_attr = _remote_lstat(sftp, normalized_root)
    if root_attr is None or _remote_attr_is_link(root_attr) or not _remote_attr_is_dir(root_attr):
        raise ValueError(f"远端会话源目录不安全: {source_root}")

    relative = posixpath.relpath(normalized_path, normalized_root)
    current = normalized_root
    parts = relative.split("/")
    for part in parts[:-1]:
        if not _safe_remote_child_name(part):
            raise ValueError(f"远端会话源路径不安全: {path}")
        current = posixpath.join(current, part)
        attr = _remote_lstat(sftp, current)
        if attr is None or _remote_attr_is_link(attr) or not _remote_attr_is_dir(attr):
            raise ValueError(f"远端会话源目录包含链接: {current}")

    file_attr = _remote_lstat(sftp, normalized_path)
    if file_attr is None or _remote_attr_is_link(file_attr) or not _remote_attr_is_file(file_attr):
        raise ValueError(f"远端会话源不是普通文件: {path}")
    return file_attr


def _remote_export_file_info(sftp, path: str, home: str, provider: str):
    source_root = _remote_source_root(home, provider)
    info = _validate_remote_source_file(sftp, path, source_root)
    relative = _remote_relative_to_home(path, home)
    _validate_provider_session_path(provider, relative)
    return relative, info


def _validate_open_remote_source(sftp, path: str, source, expected_attr) -> None:
    if not callable(getattr(sftp, "lstat", None)):
        return
    current_attr = _remote_lstat(sftp, path)
    if current_attr is None or _remote_attr_is_link(current_attr) or not _remote_attr_is_file(current_attr):
        raise ValueError(f"远端会话源在导出期间变得不安全: {path}")
    opened_stat = getattr(source, "stat", None)
    if not callable(opened_stat):
        return
    opened_attr = opened_stat()
    if _remote_attr_is_link(opened_attr) or not _remote_attr_is_file(opened_attr):
        raise ValueError(f"远端会话源不是普通文件: {path}")
    expected_inode = getattr(expected_attr, "st_ino", None)
    current_inode = getattr(current_attr, "st_ino", None)
    opened_inode = getattr(opened_attr, "st_ino", None)
    if (
        expected_inode is not None
        and current_inode is not None
        and opened_inode is not None
        and not (expected_inode == current_inode == opened_inode)
    ):
        raise ValueError(f"远端会话源在导出期间发生变化: {path}")


def _remote_path_prefixes(path: str) -> list[str]:
    normalized = posixpath.normpath(path)
    absolute = normalized.startswith("/")
    current = "/" if absolute else ""
    result: list[str] = []
    for part in normalized.split("/"):
        if not part:
            continue
        current = posixpath.join(current, part) if current else part
        result.append(current)
    return result


def _prepare_remote_import_destination(sftp, root: str, destination: str) -> bool:
    root = posixpath.normpath(str(root or ""))
    destination = posixpath.normpath(str(destination or ""))
    if (
        not root
        or "\\" in root
        or "\x00" in root
        or not _remote_path_is_within(destination, root)
        or destination == root
    ):
        raise ValueError(f"远端会话目标路径越界: {destination}")

    for current in _remote_path_prefixes(posixpath.dirname(destination)):
        attr = _remote_lstat(sftp, current)
        if attr is None:
            if not _remote_path_is_within(current, root):
                raise ValueError(f"远端会话根目录的父目录不存在: {current}")
            try:
                sftp.mkdir(current)
            except Exception as exc:
                raise ValueError(f"无法创建远端会话目录: {current}") from exc
            try:
                sftp.chmod(current, 0o700)
            except Exception:
                pass
            attr = _remote_lstat(sftp, current)
        if attr is None or _remote_attr_is_link(attr) or not _remote_attr_is_dir(attr):
            raise ValueError(f"远端会话目录包含链接或不是目录: {current}")

    destination_attr = _remote_lstat(sftp, destination)
    if destination_attr is None:
        return False
    if _remote_attr_is_link(destination_attr) or not _remote_attr_is_file(destination_attr):
        raise ValueError(f"远端会话目标不是普通文件: {destination}")
    return True


def _commit_remote_temp_file(sftp, temp_path: str, destination: str, *, overwrite: bool) -> bool:
    if overwrite:
        ssh_manager._replace_remote_file(sftp, temp_path, destination)
        return True
    try:
        # SFTP v3 rename is create-if-absent; unlike posix-rename it must not
        # replace an existing destination, closing the initial-check race.
        sftp.rename(temp_path, destination)
        return True
    except Exception:
        destination_attr = _remote_lstat(sftp, destination)
        if destination_attr is not None:
            try:
                sftp.remove(temp_path)
            except Exception:
                pass
            if _remote_attr_is_link(destination_attr) or not _remote_attr_is_file(destination_attr):
                raise ValueError(f"远端会话目标在提交时变得不安全: {destination}")
            return False
        raise


def _safe_archive_path(archive_path: str) -> None:
    pure = _validated_portable_relative_path(archive_path)
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


def _validate_local_import_destination(root: Path, destination: Path) -> bool:
    root = _absolute_lexical_path(root)
    destination = _absolute_lexical_path(destination)
    try:
        relative_parent = destination.parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"本地会话目标路径越界: {destination}") from exc

    current = root
    for part in ((), *relative_parent.parts):
        if part:
            current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"无法检查本地会话目录: {current}") from exc
        if not stat.S_ISDIR(info.st_mode) or _path_is_link_or_reparse(current, info):
            raise ValueError(f"本地会话目录包含链接或重解析点: {current}")

    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"无法检查本地会话目标: {destination}") from exc
    if (
        not stat.S_ISREG(destination_info.st_mode)
        or _path_is_link_or_reparse(destination, destination_info)
    ):
        raise ValueError(f"本地会话目标不是普通文件: {destination}")
    return True


def _prepare_local_import_parent(root: Path, destination: Path) -> bool:
    existed = _validate_local_import_destination(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return _validate_local_import_destination(root, destination) or existed


def _commit_local_temp_file(temp_path: Path, destination: Path, *, overwrite: bool) -> bool:
    if overwrite:
        replace_with_retry(temp_path, destination)
        return True
    try:
        os.link(temp_path, destination)
    except FileExistsError:
        temp_path.unlink(missing_ok=True)
        return False
    temp_path.unlink(missing_ok=True)
    return True


def _write_local_bytes_atomic(
    destination: Path,
    data: bytes,
    *,
    root: Path,
    overwrite: bool,
) -> bool:
    _prepare_local_import_parent(root, destination)
    tmp_path = temp_path_for(destination)
    try:
        with tmp_path.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        _validate_local_import_destination(root, destination)
        return _commit_local_temp_file(tmp_path, destination, overwrite=overwrite)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_package_file_atomic(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    *,
    root: Path,
    overwrite: bool,
) -> bool:
    _prepare_local_import_parent(root, destination)
    tmp_path = temp_path_for(destination)
    try:
        with bundle.open(info, "r") as source, tmp_path.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        _validate_local_import_destination(root, destination)
        return _commit_local_temp_file(tmp_path, destination, overwrite=overwrite)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_rewritten_package_file_atomic(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    target_project_path: str,
    *,
    root: Path,
    overwrite: bool,
    max_output_bytes: int,
) -> int | None:
    _prepare_local_import_parent(root, destination)
    tmp_path = temp_path_for(destination)
    try:
        with bundle.open(info, "r") as source, tmp_path.open("xb") as target:
            written = _copy_rewritten_jsonl_stream(
                source,
                target,
                target_project_path,
                max_output_bytes=max_output_bytes,
            )
            target.flush()
            os.fsync(target.fileno())
        _validate_local_import_destination(root, destination)
        if not _commit_local_temp_file(tmp_path, destination, overwrite=overwrite):
            return None
        return written
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_package_file_to_remote(
    sftp,
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: str,
    *,
    root: str,
    overwrite: bool,
    file_mode: int | None = None,
) -> bool:
    _prepare_remote_import_destination(sftp, root, destination)
    temp_path = f"{destination}.tmp.{uuid.uuid4().hex}"
    try:
        with bundle.open(info, "r") as source, sftp.open(temp_path, "wb") as target:
            shutil.copyfileobj(source, target)
        if file_mode is not None:
            try:
                sftp.chmod(temp_path, file_mode)
            except Exception:
                pass
        _prepare_remote_import_destination(sftp, root, destination)
        if not _commit_remote_temp_file(sftp, temp_path, destination, overwrite=overwrite):
            return False
        if file_mode is not None:
            try:
                sftp.chmod(destination, file_mode)
            except Exception:
                pass
        return True
    except Exception:
        try:
            sftp.remove(temp_path)
        except Exception:
            pass
        raise


def _copy_rewritten_package_file_to_remote(
    sftp,
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: str,
    target_project_path: str,
    *,
    root: str,
    overwrite: bool,
    max_output_bytes: int,
    file_mode: int | None = None,
) -> int | None:
    _prepare_remote_import_destination(sftp, root, destination)
    temp_path = f"{destination}.tmp.{uuid.uuid4().hex}"
    try:
        with bundle.open(info, "r") as source, sftp.open(temp_path, "wb") as target:
            written = _copy_rewritten_jsonl_stream(
                source,
                target,
                target_project_path,
                max_output_bytes=max_output_bytes,
            )
        if file_mode is not None:
            try:
                sftp.chmod(temp_path, file_mode)
            except Exception:
                pass
        _prepare_remote_import_destination(sftp, root, destination)
        if not _commit_remote_temp_file(sftp, temp_path, destination, overwrite=overwrite):
            return None
        if file_mode is not None:
            try:
                sftp.chmod(destination, file_mode)
            except Exception:
                pass
        return written
    except Exception:
        try:
            sftp.remove(temp_path)
        except Exception:
            pass
        raise


def _write_remote_bytes_atomic(
    sftp,
    destination: str,
    data: bytes,
    *,
    root: str,
    overwrite: bool,
    file_mode: int | None = None,
) -> bool:
    _prepare_remote_import_destination(sftp, root, destination)
    temp_path = f"{destination}.tmp.{uuid.uuid4().hex}"
    try:
        with sftp.open(temp_path, "wb") as handle:
            handle.write(data)
        if file_mode is not None:
            try:
                sftp.chmod(temp_path, file_mode)
            except Exception:
                pass
        _prepare_remote_import_destination(sftp, root, destination)
        if not _commit_remote_temp_file(sftp, temp_path, destination, overwrite=overwrite):
            return False
        if file_mode is not None:
            try:
                sftp.chmod(destination, file_mode)
            except Exception:
                pass
        return True
    except Exception:
        try:
            sftp.remove(temp_path)
        except Exception:
            pass
        raise


def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, Any]:
    _ensure_unique_package_entries(bundle, {"manifest.json"})
    try:
        info = bundle.getinfo("manifest.json")
    except KeyError as e:
        raise ValueError("会话迁移包缺少 manifest.json") from e
    if info.is_dir():
        raise ValueError("会话迁移包 manifest 不是文件")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ValueError("会话迁移包 manifest 过大")
    try:
        manifest = json.loads(bundle.read(info).decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError("会话迁移包 manifest 损坏") from e
    if manifest.get("format") != PACKAGE_FORMAT:
        raise ValueError("不是 API切换器会话迁移包")
    if manifest.get("version") != PACKAGE_VERSION:
        raise ValueError(f"不支持的会话迁移包版本: {manifest.get('version')}")
    if not isinstance(manifest.get("sessions"), list):
        raise ValueError("会话迁移包格式不完整")
    return manifest


def _ensure_unique_package_entries(bundle: zipfile.ZipFile, names: set[str]) -> None:
    counts: dict[str, int] = {}
    for info in bundle.infolist():
        if info.filename in names:
            counts[info.filename] = counts.get(info.filename, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError("会话迁移包包含重复关键条目: " + ", ".join(duplicates))


def _read_codex_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    index_path = codex_home / "session_index.jsonl"
    try:
        index_info = index_path.lstat()
    except OSError:
        return {}
    if not stat.S_ISREG(index_info.st_mode) or _path_is_link_or_reparse(index_path, index_info):
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("id"):
                    result[str(item["id"])] = item
    except OSError:
        return {}
    return result


def _read_remote_codex_index(sftp, codex_home: str) -> dict[str, dict[str, Any]]:
    index_path = posixpath.join(codex_home, "session_index.jsonl")
    index_attr = _remote_lstat(sftp, index_path)
    if index_attr is None or _remote_attr_is_link(index_attr) or not _remote_attr_is_file(index_attr):
        return {}
    return _parse_codex_index_text(_remote_read_text(sftp, index_path, missing_ok=True))


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
    payload = _codex_index_append_payload(sessions)
    if not payload:
        return
    with _CODEX_INDEX_APPEND_LOCK:
        existed = _prepare_local_import_parent(codex_home, index_path)
        if existed and index_path.stat().st_size:
            payload = b"\n" + payload
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(index_path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("无法追加 Codex 会话索引")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _update_remote_codex_index(sftp, codex_home: str, sessions: list[dict[str, Any]]) -> None:
    index_path = posixpath.join(codex_home, "session_index.jsonl")
    payload = _codex_index_append_payload(sessions)
    if not payload:
        return
    existed = _prepare_remote_import_destination(sftp, codex_home, index_path)
    if existed:
        payload = b"\n" + payload
    with sftp.open(index_path, "ab") as handle:
        handle.write(payload)
    try:
        sftp.chmod(index_path, 0o600)
    except Exception:
        pass


def _codex_index_append_payload(sessions: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        item = {
            "id": session_id,
            "thread_name": session.get("title") or session.get("summary") or session_id,
            "updated_at": session.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        }
        lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def _remote_listdir_attr(sftp, path: str):
    try:
        return list(sftp.listdir_attr(path))
    except Exception:
        return []


def _remote_walk_files(sftp, root: str, suffix: str = "", limit: int = MAX_REMOTE_SESSION_FILES):
    root = posixpath.normpath(str(root or ""))
    root_attr = _remote_lstat(sftp, root)
    if root_attr is None or _remote_attr_is_link(root_attr) or not _remote_attr_is_dir(root_attr):
        return
    pending = deque([(root, root_attr)])
    visited: set[tuple[Any, ...]] = set()
    yielded = 0
    while pending and yielded < limit:
        current, current_attr = pending.popleft()
        identity = _remote_directory_identity(current, current_attr)
        if identity in visited:
            continue
        visited.add(identity)
        for attr in _remote_listdir_attr(sftp, current):
            filename = str(getattr(attr, "filename", "") or "")
            if not _safe_remote_child_name(filename):
                continue
            path = posixpath.normpath(posixpath.join(current, filename))
            if not _remote_path_is_within(path, root):
                continue
            actual_attr = _remote_lstat(sftp, path, fallback_attr=attr)
            if actual_attr is None or _remote_attr_is_link(actual_attr):
                continue
            if _remote_attr_is_dir(actual_attr):
                pending.append((path, actual_attr))
                continue
            if not _remote_attr_is_file(actual_attr):
                continue
            if suffix and not path.endswith(suffix):
                continue
            yielded += 1
            yield path, actual_attr
            if yielded >= limit:
                break


def _remote_directory_identity(path: str, attr) -> tuple[Any, ...]:
    device = getattr(attr, "st_dev", None)
    inode = getattr(attr, "st_ino", None)
    if device is not None and inode is not None:
        return ("inode", int(device), int(inode))
    return ("path", posixpath.normpath(path))


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
