"""Usage statistics data models and storage."""
import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from config.paths import STORAGE_DIR
from core.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

STATS_FILE = STORAGE_DIR / "usage_stats.json"
DAILY_STATS_FILE = STORAGE_DIR / "daily_stats.json"


@dataclass
class DailyStats:
    """Daily statistics snapshot."""
    date: str  # YYYY-MM-DD format
    switch_count: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error_count: int = 0
    success_count: int = 0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'DailyStats':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ProfileUsageStats:
    """Statistics for a single profile."""
    profile_name: str
    profile_type: str  # "claude" or "codex"

    # Usage counts
    switch_count: int = 0  # Number of times switched to this profile
    total_duration_seconds: float = 0.0  # Total time used (seconds)

    # Token usage (estimated)
    total_tokens: int = 0  # Total tokens used
    input_tokens: int = 0  # Input tokens
    output_tokens: int = 0  # Output tokens

    # Error tracking
    error_count: int = 0  # Total errors
    success_count: int = 0  # Successful operations

    # Timestamps
    first_used: Optional[str] = None  # ISO format
    last_used: Optional[str] = None  # ISO format
    last_switch_time: Optional[str] = None  # ISO format

    # Daily history
    daily_history: Dict[str, DailyStats] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        data = asdict(self)
        # Convert daily_history to serializable format
        data['daily_history'] = {
            date: stats.to_dict() if isinstance(stats, DailyStats) else stats
            for date, stats in self.daily_history.items()
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'ProfileUsageStats':
        """Create from dictionary."""
        data = dict(data)
        # Convert daily_history back to DailyStats objects
        daily_history = data.get('daily_history', {})
        if daily_history:
            data['daily_history'] = {
                date: DailyStats.from_dict(stats) if isinstance(stats, dict) else stats
                for date, stats in daily_history.items()
            }
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def record_switch(self):
        """Record a profile switch."""
        now = datetime.now()
        now_iso = now.isoformat()
        today = now.strftime("%Y-%m-%d")

        self.switch_count += 1
        self.last_switch_time = now_iso

        if not self.first_used:
            self.first_used = now_iso
        self.last_used = now_iso

        # Update daily stats
        self._get_or_create_daily_stats(today).switch_count += 1

    def record_usage(self, duration_seconds: float):
        """Record usage duration."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        self.total_duration_seconds += duration_seconds
        self.last_used = now.isoformat()

        # Update daily stats
        self._get_or_create_daily_stats(today).duration_seconds += duration_seconds

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += input_tokens + output_tokens
        self.last_used = now.isoformat()

        # Update daily stats
        daily = self._get_or_create_daily_stats(today)
        daily.input_tokens += input_tokens
        daily.output_tokens += output_tokens
        daily.total_tokens += input_tokens + output_tokens

    def record_error(self):
        """Record an error."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        self.error_count += 1
        self.last_used = now.isoformat()

        # Update daily stats
        self._get_or_create_daily_stats(today).error_count += 1

    def record_success(self):
        """Record a successful operation."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        self.success_count += 1
        self.last_used = now.isoformat()

        # Update daily stats
        self._get_or_create_daily_stats(today).success_count += 1

    def _get_or_create_daily_stats(self, date: str) -> DailyStats:
        """Get or create daily stats for a date."""
        if date not in self.daily_history:
            self.daily_history[date] = DailyStats(date=date)
        return self.daily_history[date]

    def get_stats_for_date_range(self, start_date: datetime, end_date: datetime) -> dict:
        """Get aggregated stats for a date range."""
        total_switches = 0
        total_tokens = 0
        total_errors = 0
        total_successes = 0
        total_duration = 0.0

        current = start_date
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            if date_str in self.daily_history:
                daily = self.daily_history[date_str]
                total_switches += daily.switch_count
                total_tokens += daily.total_tokens
                total_errors += daily.error_count
                total_successes += daily.success_count
                total_duration += daily.duration_seconds
            current += timedelta(days=1)

        return {
            "switch_count": total_switches,
            "total_tokens": total_tokens,
            "error_count": total_errors,
            "success_count": total_successes,
            "duration_seconds": total_duration
        }

    def get_error_rate(self) -> float:
        """Get error rate (0.0 to 1.0)."""
        total = self.error_count + self.success_count
        if total == 0:
            return 0.0
        return self.error_count / total

    def get_success_rate(self) -> float:
        """Get success rate (0.0 to 1.0)."""
        return 1.0 - self.get_error_rate()

    def format_duration(self) -> str:
        """Format duration as human-readable string."""
        seconds = int(self.total_duration_seconds)

        if seconds < 60:
            return f"{seconds}秒"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes}分钟"
        elif seconds < 86400:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}小时{minutes}分钟"
        else:
            days = seconds // 86400
            hours = (seconds % 86400) // 3600
            return f"{days}天{hours}小时"

    def format_tokens(self) -> str:
        """Format tokens with K/M units."""
        return format_token_count(self.total_tokens)


def format_token_count(count: int) -> str:
    """Format token count with K/M/B units."""
    if count < 1000:
        return str(count)
    elif count < 1_000_000:
        return f"{count / 1000:.1f}K"
    elif count < 1_000_000_000:
        return f"{count / 1_000_000:.1f}M"
    else:
        return f"{count / 1_000_000_000:.1f}B"


class UsageStatsManager:
    """Manager for usage statistics."""

    def __init__(self):
        self.stats: Dict[str, ProfileUsageStats] = {}
        self.load()

    def _get_key(self, profile_name: str, profile_type: str) -> str:
        """Get unique key for profile."""
        return f"{profile_type}:{profile_name}"

    def get_stats(self, profile_name: str, profile_type: str) -> ProfileUsageStats:
        """Get or create stats for a profile."""
        key = self._get_key(profile_name, profile_type)

        if key not in self.stats:
            self.stats[key] = ProfileUsageStats(
                profile_name=profile_name,
                profile_type=profile_type
            )

        return self.stats[key]

    def record_switch(self, profile_name: str, profile_type: str):
        """Record a profile switch."""
        stats = self.get_stats(profile_name, profile_type)
        stats.record_switch()
        self.save()
        logger.info(f"Recorded switch to {profile_type}:{profile_name}")

    def record_tokens(self, profile_name: str, profile_type: str,
                     input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage."""
        stats = self.get_stats(profile_name, profile_type)
        stats.record_tokens(input_tokens, output_tokens)
        self.save()

    def record_error(self, profile_name: str, profile_type: str):
        """Record an error."""
        stats = self.get_stats(profile_name, profile_type)
        stats.record_error()
        self.save()

    def record_success(self, profile_name: str, profile_type: str):
        """Record a success."""
        stats = self.get_stats(profile_name, profile_type)
        stats.record_success()
        self.save()

    def get_all_stats(self, profile_type: Optional[str] = None) -> List[ProfileUsageStats]:
        """Get all stats, optionally filtered by type."""
        stats_list = list(self.stats.values())

        if profile_type:
            stats_list = [s for s in stats_list if s.profile_type == profile_type]

        return stats_list

    def get_top_profiles(self, limit: int = 5,
                        profile_type: Optional[str] = None) -> List[ProfileUsageStats]:
        """Get top profiles by switch count."""
        stats_list = self.get_all_stats(profile_type)
        stats_list.sort(key=lambda s: s.switch_count, reverse=True)
        return stats_list[:limit]

    def get_recent_profiles(self, limit: int = 5,
                           profile_type: Optional[str] = None) -> List[ProfileUsageStats]:
        """Get recently used profiles."""
        stats_list = self.get_all_stats(profile_type)

        # Filter out profiles never used
        stats_list = [s for s in stats_list if s.last_used]

        # Sort by last used time
        stats_list.sort(
            key=lambda s: s.last_used if s.last_used else "",
            reverse=True
        )

        return stats_list[:limit]

    def get_summary(self, profile_type: Optional[str] = None,
                   start_date: Optional[datetime] = None,
                   end_date: Optional[datetime] = None) -> dict:
        """Get summary statistics, optionally filtered by date range."""
        stats_list = self.get_all_stats(profile_type)

        if not stats_list:
            return {
                "total_profiles": 0,
                "total_switches": 0,
                "total_tokens": 0,
                "total_errors": 0,
                "total_successes": 0,
                "average_error_rate": 0.0
            }

        # If date range specified, aggregate from daily stats
        if start_date and end_date:
            total_switches = 0
            total_tokens = 0
            total_errors = 0
            total_successes = 0

            for stats in stats_list:
                range_stats = stats.get_stats_for_date_range(start_date, end_date)
                total_switches += range_stats["switch_count"]
                total_tokens += range_stats["total_tokens"]
                total_errors += range_stats["error_count"]
                total_successes += range_stats["success_count"]
        else:
            # Use total stats
            total_switches = sum(s.switch_count for s in stats_list)
            total_tokens = sum(s.total_tokens for s in stats_list)
            total_errors = sum(s.error_count for s in stats_list)
            total_successes = sum(s.success_count for s in stats_list)

        # Calculate average error rate
        total_ops = total_errors + total_successes
        avg_error_rate = total_errors / total_ops if total_ops > 0 else 0.0

        return {
            "total_profiles": len(stats_list),
            "total_switches": total_switches,
            "total_tokens": total_tokens,
            "total_errors": total_errors,
            "total_successes": total_successes,
            "average_error_rate": avg_error_rate
        }

    def get_date_range_summary(self, days: int = 7,
                               profile_type: Optional[str] = None) -> dict:
        """Get summary for the last N days."""
        end_date = datetime.now().replace(hour=23, minute=59, second=59)
        start_date = (end_date - timedelta(days=days-1)).replace(hour=0, minute=0, second=0)
        return self.get_summary(profile_type, start_date, end_date)

    def get_daily_trend(self, days: int = 7,
                       profile_type: Optional[str] = None) -> List[dict]:
        """Get daily trend data for the last N days."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)

        stats_list = self.get_all_stats(profile_type)
        trend_data = []

        current = start_date
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            day_data = {
                "date": date_str,
                "switch_count": 0,
                "total_tokens": 0,
                "error_count": 0,
                "success_count": 0
            }

            for stats in stats_list:
                if date_str in stats.daily_history:
                    daily = stats.daily_history[date_str]
                    day_data["switch_count"] += daily.switch_count
                    day_data["total_tokens"] += daily.total_tokens
                    day_data["error_count"] += daily.error_count
                    day_data["success_count"] += daily.success_count

            trend_data.append(day_data)
            current += timedelta(days=1)

        return trend_data

    def clear_stats(self, profile_name: Optional[str] = None,
                   profile_type: Optional[str] = None):
        """Clear statistics."""
        if profile_name and profile_type:
            # Clear specific profile
            key = self._get_key(profile_name, profile_type)
            if key in self.stats:
                del self.stats[key]
        elif profile_type:
            # Clear all profiles of a type
            keys_to_delete = [
                k for k, v in self.stats.items()
                if v.profile_type == profile_type
            ]
            for key in keys_to_delete:
                del self.stats[key]
        else:
            # Clear all
            self.stats.clear()

        self.save()
        logger.info("Cleared usage statistics")

    def load(self):
        """Load statistics from file."""
        if not STATS_FILE.exists():
            return

        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.stats = {
                key: ProfileUsageStats.from_dict(value)
                for key, value in data.items()
            }

            logger.info(f"Loaded {len(self.stats)} usage statistics")

        except Exception as e:
            logger.error(f"Failed to load usage stats: {e}", exc_info=True)
            self.stats = {}

    def save(self):
        """Save statistics to file."""
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

            data = {
                key: value.to_dict()
                for key, value in self.stats.items()
            }

            content = json.dumps(data, indent=2, ensure_ascii=False)
            atomic_write_text(STATS_FILE, content)

        except Exception as e:
            logger.error(f"Failed to save usage stats: {e}", exc_info=True)


# Global instance
usage_stats = UsageStatsManager()
