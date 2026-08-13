"""
错误恢复统计和分析工具
"""
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from dataclasses import dataclass


SUCCESSFUL_RECOVERY_ACTIONS = {
    "recovered",
    "recovery_succeeded",
    "recovery_success",
    "success",
    "successfully_recovered",
}
RECOVERY_DISPATCH_ACTIONS = {"attempting_recovery"}
NON_RECOVERY_STRATEGIES = {"abort", "none", "notify_user"}
NON_RECOVERABLE_ERROR_TYPES = {
    "auth",
    "authentication_error",
    "invalid",
    "invalid_request",
    "permission",
    "permission_denied",
    "quota",
    "quota_exceeded",
    "unknown",
}


def _explicit_recovery_outcome(entry: Dict[str, Any]) -> Optional[bool]:
    """Return an explicitly logged outcome, if a newer log format provides one."""
    for key in ("recovery_success", "recovery_succeeded"):
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes"}:
                return True
            if normalized in {"0", "false", "no"}:
                return False
    return None


def _entry_reports_successful_recovery(entry: Dict[str, Any], action: Any = "") -> bool:
    """Count recovery outcomes using both current and forward-compatible logs.

    Current production hooks write ``attempting_recovery`` after accepting an
    error and immediately before emitting the recovery command.  They do not
    write a later success event, so that action is the durable indication that
    recovery was dispatched.  Explicit outcome fields take precedence when a
    newer producer supplies them.
    """
    explicit_outcome = _explicit_recovery_outcome(entry)
    if explicit_outcome is not None:
        return explicit_outcome

    normalized_action = str(action or "").strip().lower()
    if normalized_action in SUCCESSFUL_RECOVERY_ACTIONS:
        return True
    if normalized_action not in RECOVERY_DISPATCH_ACTIONS:
        return False

    strategy = str(entry.get("recovery_strategy") or "").strip().lower()
    if strategy in NON_RECOVERY_STRATEGIES:
        return False
    error_type = str(entry.get("error_type") or "").strip().lower()
    return error_type not in NON_RECOVERABLE_ERROR_TYPES


@dataclass
class ErrorStats:
    """错误统计信息"""
    total_errors: int = 0
    total_recoveries: int = 0
    recovery_success_rate: float = 0.0
    errors_by_type: Dict[str, int] = None
    errors_by_session: Dict[str, int] = None
    most_common_error: Optional[str] = None
    avg_recovery_count: float = 0.0
    recent_errors: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.errors_by_type is None:
            self.errors_by_type = {}
        if self.errors_by_session is None:
            self.errors_by_session = {}
        if self.recent_errors is None:
            self.recent_errors = []


class ErrorRecoveryAnalyzer:
    """错误恢复分析器"""

    def __init__(self, log_path: Path, additional_log_paths: Optional[List[Path]] = None):
        """
        初始化分析器

        Args:
            log_path: 错误恢复日志文件路径 (error_recovery_log.jsonl)
        """
        self.log_path = Path(log_path)
        paths = [self.log_path, *(Path(path) for path in (additional_log_paths or []))]
        self.log_paths = tuple(dict.fromkeys(paths))

    def analyze(self, days: int = 7) -> ErrorStats:
        """
        分析错误恢复日志

        Args:
            days: 分析最近多少天的数据

        Returns:
            ErrorStats: 统计信息
        """
        if not any(path.exists() for path in self.log_paths):
            return ErrorStats()

        # 读取日志
        entries = self._read_log_entries(days)
        if not entries:
            return ErrorStats()

        # 统计
        stats = ErrorStats()
        stats.total_errors = len(entries)

        # 按错误类型统计
        error_types = Counter()
        session_errors = defaultdict(int)
        recovery_counts = []
        successful_recoveries = 0

        for entry in entries:
            error_type = entry.get("error_type", "unknown")
            session_id = entry.get("session_id", "unknown")
            action = entry.get("action", "")
            recovery_count = entry.get("recovery_count", 0)

            error_types[error_type] += 1
            session_errors[session_id] += 1

            if _entry_reports_successful_recovery(entry, action):
                successful_recoveries += 1
                try:
                    recovery_counts.append(float(recovery_count))
                except (TypeError, ValueError):
                    pass

        stats.errors_by_type = dict(error_types)
        stats.errors_by_session = dict(session_errors)
        stats.most_common_error = error_types.most_common(1)[0][0] if error_types else None
        stats.total_recoveries = successful_recoveries
        stats.recovery_success_rate = (successful_recoveries / stats.total_errors * 100) if stats.total_errors > 0 else 0
        stats.avg_recovery_count = sum(recovery_counts) / len(recovery_counts) if recovery_counts else 0

        # 最近的错误
        stats.recent_errors = entries[-10:]  # 最近 10 条

        return stats

    @staticmethod
    def _local_naive_timestamp(value: Any) -> Optional[datetime]:
        """Normalize ISO timestamps to local naive time for safe comparisons."""
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        timestamp = datetime.fromisoformat(text)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone().replace(tzinfo=None)
        return timestamp

    def _read_log_entries(self, days: int) -> List[Dict[str, Any]]:
        """读取日志条目"""
        cutoff_date = datetime.now() - timedelta(days=days)
        indexed_entries = []
        seen_entry_occurrences = set()
        sequence = 0

        for log_path in self.log_paths:
            source_signature_counts = defaultdict(int)
            try:
                handle = open(log_path, 'r', encoding='utf-8')
            except (OSError, IOError):
                continue

            try:
                with handle as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue

                        try:
                            signature = json.dumps(
                                entry,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        except (TypeError, ValueError):
                            signature = line
                        occurrence = source_signature_counts[signature]
                        source_signature_counts[signature] += 1
                        occurrence_key = (signature, occurrence)
                        if occurrence_key in seen_entry_occurrences:
                            continue
                        seen_entry_occurrences.add(occurrence_key)

                        timestamp = None
                        timestamp_str = entry.get("timestamp")
                        if timestamp_str:
                            try:
                                timestamp = self._local_naive_timestamp(timestamp_str)
                            except (ValueError, TypeError, OverflowError):
                                # Keep malformed historical entries visible.
                                timestamp = None
                            if timestamp is not None and timestamp < cutoff_date:
                                continue

                        indexed_entries.append((timestamp, sequence, entry))
                        sequence += 1
            except (OSError, IOError):
                continue

        # Legacy and current log files may overlap in time. Sort valid ISO
        # timestamps while preserving source/file order for malformed entries.
        indexed_entries.sort(key=lambda item: (item[0] or datetime.min, item[1]))
        return [entry for _timestamp, _sequence, entry in indexed_entries]

    def _all_log_entries(self) -> List[Dict[str, Any]]:
        """Read all valid entries from current and legacy log locations."""
        entries = []
        seen_entry_occurrences = set()
        for log_path in self.log_paths:
            source_signature_counts = defaultdict(int)
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        signature = json.dumps(
                            entry,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        occurrence = source_signature_counts[signature]
                        source_signature_counts[signature] += 1
                        occurrence_key = (signature, occurrence)
                        if occurrence_key in seen_entry_occurrences:
                            continue
                        seen_entry_occurrences.add(occurrence_key)
                        entries.append(entry)
            except (OSError, IOError):
                continue
        return entries

    def get_error_timeline(self, days: int = 7) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取错误时间线（按日期分组）

        Args:
            days: 最近多少天

        Returns:
            Dict[日期, 错误列表]
        """
        entries = self._read_log_entries(days)
        timeline = defaultdict(list)

        for entry in entries:
            timestamp_str = entry.get("timestamp")
            if timestamp_str:
                try:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    date_key = timestamp.strftime("%Y-%m-%d")
                    timeline[date_key].append(entry)
                except Exception:
                    pass

        return dict(timeline)

    def get_session_details(self, session_id: str) -> List[Dict[str, Any]]:
        """
        获取特定会话的所有错误记录

        Args:
            session_id: 会话 ID

        Returns:
            该会话的所有错误记录
        """
        if not any(path.exists() for path in self.log_paths):
            return []
        return [
            entry
            for entry in self._all_log_entries()
            if entry.get("session_id") == session_id
        ]

    def export_report(self, output_path: Path, days: int = 7) -> None:
        """
        导出分析报告

        Args:
            output_path: 输出文件路径
            days: 分析最近多少天的数据
        """
        stats = self.analyze(days)
        timeline = self.get_error_timeline(days)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("错误恢复分析报告\n")
            f.write("=" * 80 + "\n\n")

            f.write(f"分析时间范围: 最近 {days} 天\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            # 总体统计
            f.write("=" * 80 + "\n")
            f.write("总体统计\n")
            f.write("=" * 80 + "\n\n")
            f.write(f"总错误数: {stats.total_errors}\n")
            f.write(f"成功恢复数: {stats.total_recoveries}\n")
            f.write(f"恢复成功率: {stats.recovery_success_rate:.1f}%\n")
            f.write(f"平均恢复次数: {stats.avg_recovery_count:.1f}\n")
            f.write(f"最常见错误: {stats.most_common_error or 'N/A'}\n\n")

            # 按错误类型统计
            f.write("=" * 80 + "\n")
            f.write("按错误类型统计\n")
            f.write("=" * 80 + "\n\n")
            for error_type, count in sorted(stats.errors_by_type.items(), key=lambda x: x[1], reverse=True):
                percentage = (count / stats.total_errors * 100) if stats.total_errors > 0 else 0
                f.write(f"{error_type:30s} {count:5d} ({percentage:5.1f}%)\n")
            f.write("\n")

            # 按会话统计
            f.write("=" * 80 + "\n")
            f.write("按会话统计 (Top 10)\n")
            f.write("=" * 80 + "\n\n")
            top_sessions = sorted(stats.errors_by_session.items(), key=lambda x: x[1], reverse=True)[:10]
            for session_id, count in top_sessions:
                f.write(f"{session_id:40s} {count:5d} 次错误\n")
            f.write("\n")

            # 时间线
            f.write("=" * 80 + "\n")
            f.write("错误时间线\n")
            f.write("=" * 80 + "\n\n")
            for date in sorted(timeline.keys(), reverse=True):
                errors = timeline[date]
                f.write(f"{date}: {len(errors)} 次错误\n")

                # 按错误类型分组
                type_counts = Counter(e.get("error_type", "unknown") for e in errors)
                for error_type, count in type_counts.most_common():
                    f.write(f"  - {error_type}: {count}\n")
                f.write("\n")

            # 最近的错误
            f.write("=" * 80 + "\n")
            f.write("最近的错误 (最多 10 条)\n")
            f.write("=" * 80 + "\n\n")
            for entry in stats.recent_errors:
                timestamp = entry.get("timestamp", "N/A")
                error_type = entry.get("error_type", "unknown")
                error_code = entry.get("error_code", "N/A")
                error_message = entry.get("error_message", "N/A")
                action = entry.get("action", "N/A")
                recovery_count = entry.get("recovery_count", 0)

                f.write(f"时间: {timestamp}\n")
                f.write(f"类型: {error_type}\n")
                f.write(f"代码: {error_code}\n")
                f.write(f"消息: {error_message}\n")
                f.write(f"操作: {action}\n")
                f.write(f"恢复次数: {recovery_count}\n")
                f.write("-" * 80 + "\n\n")


def _get_provider_recovery_log_path(provider: str) -> Path:
    if provider.lower() == "claude":
        config_dir = Path.home() / ".claude"
    elif provider.lower() == "codex":
        config_dir = Path.home() / ".codex"
    else:
        raise ValueError(f"Unknown provider: {provider}")

    current_log_path = config_dir / "tmp" / "error_recovery_log.jsonl"
    legacy_log_path = config_dir / "error_recovery_log.jsonl"
    if current_log_path.exists() or not legacy_log_path.exists():
        return current_log_path
    return legacy_log_path


def _get_provider_recovery_log_paths(provider: str) -> tuple[Path, ...]:
    """Return the preferred path first and retain the legacy source as history."""
    primary = _get_provider_recovery_log_path(provider)
    config_dir = primary.parent.parent if primary.parent.name == "tmp" else primary.parent
    current = config_dir / "tmp" / "error_recovery_log.jsonl"
    legacy = config_dir / "error_recovery_log.jsonl"
    return tuple(dict.fromkeys((primary, current, legacy)))


def get_analyzer(provider: str) -> ErrorRecoveryAnalyzer:
    """
    获取指定 Provider 的分析器

    Args:
        provider: "claude" 或 "codex"

    Returns:
        ErrorRecoveryAnalyzer
    """
    paths = _get_provider_recovery_log_paths(provider)
    return ErrorRecoveryAnalyzer(paths[0], list(paths[1:]))
