"""
错误恢复统计和分析工具
"""
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from dataclasses import dataclass


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

    def __init__(self, log_path: Path):
        """
        初始化分析器

        Args:
            log_path: 错误恢复日志文件路径 (error_recovery_log.jsonl)
        """
        self.log_path = log_path

    def analyze(self, days: int = 7) -> ErrorStats:
        """
        分析错误恢复日志

        Args:
            days: 分析最近多少天的数据

        Returns:
            ErrorStats: 统计信息
        """
        if not self.log_path.exists():
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

            if action == "attempting_recovery":
                successful_recoveries += 1
                recovery_counts.append(recovery_count)

        stats.errors_by_type = dict(error_types)
        stats.errors_by_session = dict(session_errors)
        stats.most_common_error = error_types.most_common(1)[0][0] if error_types else None
        stats.total_recoveries = successful_recoveries
        stats.recovery_success_rate = (successful_recoveries / stats.total_errors * 100) if stats.total_errors > 0 else 0
        stats.avg_recovery_count = sum(recovery_counts) / len(recovery_counts) if recovery_counts else 0

        # 最近的错误
        stats.recent_errors = entries[-10:]  # 最近 10 条

        return stats

    def _read_log_entries(self, days: int) -> List[Dict[str, Any]]:
        """读取日志条目"""
        cutoff_date = datetime.now() - timedelta(days=days)
        entries = []

        try:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)

                        # 检查时间戳
                        timestamp_str = entry.get("timestamp")
                        if timestamp_str:
                            try:
                                # 处理多种时间戳格式
                                timestamp_str = timestamp_str.replace('Z', '+00:00')
                                timestamp = datetime.fromisoformat(timestamp_str)
                                if timestamp < cutoff_date:
                                    continue
                            except (ValueError, AttributeError):
                                # 时间戳格式错误，仍然包含该条目
                                pass

                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

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
        if not self.log_path.exists():
            return []

        session_entries = []

        try:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)
                        if entry.get("session_id") == session_id:
                            session_entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

        return session_entries

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


def get_analyzer(provider: str) -> ErrorRecoveryAnalyzer:
    """
    获取指定 Provider 的分析器

    Args:
        provider: "claude" 或 "codex"

    Returns:
        ErrorRecoveryAnalyzer
    """
    if provider.lower() == "claude":
        log_path = Path.home() / ".claude" / "error_recovery_log.jsonl"
    elif provider.lower() == "codex":
        log_path = Path.home() / ".codex" / "error_recovery_log.jsonl"
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return ErrorRecoveryAnalyzer(log_path)
