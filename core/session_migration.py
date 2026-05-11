"""Local Claude Code and Codex session discovery and migration."""
from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PACKAGE_FORMAT = "api-switcher-session-migration"
PACKAGE_VERSION = 1
PACKAGE_EXTENSION = ".asxsession"
MAX_SCAN_LINES = 2000


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
) -> list[SessionRecord]:
    """Return local Claude Code and/or Codex sessions sorted newest first."""
    provider = (provider or "all").lower()
    claude_home = claude_home or default_claude_home()
    codex_home = codex_home or default_codex_home()

    records: list[SessionRecord] = []
    if provider in {"all", "claude"}:
        records.extend(_list_claude_sessions(claude_home))
    if provider in {"all", "codex"}:
        records.extend(_list_codex_sessions(codex_home))

    return sorted(records, key=lambda item: item.updated_at or item.created_at, reverse=True)


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

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
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
                    relative_path = _remap_relative_path(provider, relative_path, target_project_text)
                    destination = _safe_destination(home, relative_path)
                except ValueError:
                    skipped_invalid += 1
                    continue

                if destination.exists() and not overwrite:
                    skipped_existing += 1
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                if target_project_text and destination.suffix.lower() == ".jsonl":
                    data = bundle.read(archive_path)
                    destination.write_bytes(_rewrite_jsonl_cwd(data, target_project_text))
                else:
                    with bundle.open(archive_path, "r") as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
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
                    file_count += 1
                    try:
                        total_bytes += int(file_entry.get("size") or 0)
                    except (TypeError, ValueError):
                        pass
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


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= MAX_SCAN_LINES:
                break
            line = line.strip()
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


def _safe_destination(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"不安全的相对路径: {relative_path}")
    target = (root / Path(*pure.parts)).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError(f"目标路径越界: {relative_path}")
    return target


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


def _update_codex_index(codex_home: Path, sessions: list[dict[str, Any]]) -> None:
    index_path = codex_home / "session_index.jsonl"
    existing = _read_codex_index(codex_home)
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
        return
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in sorted(existing.values(), key=lambda value: str(value.get("updated_at") or ""))
    ]
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
