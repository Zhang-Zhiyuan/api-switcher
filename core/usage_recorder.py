"""Usage statistics recorder - automatically tracks profile usage."""
import logging
import time
from datetime import datetime
from typing import Optional
from core.usage_stats import usage_stats

logger = logging.getLogger(__name__)


class UsageRecorder:
    """Records usage statistics automatically."""

    def __init__(self):
        self.current_profile: Optional[str] = None
        self.current_type: Optional[str] = None
        self.session_start_time: Optional[float] = None

    def start_session(self, profile_name: str, profile_type: str):
        """Start tracking a profile session."""
        # End previous session if exists
        if self.current_profile and self.current_type:
            self.end_session()

        self.current_profile = profile_name
        self.current_type = profile_type
        self.session_start_time = time.time()

        # Record the switch
        usage_stats.record_switch(profile_name, profile_type)

        logger.info(f"Started session for {profile_type}:{profile_name}")

    def end_session(self):
        """End current tracking session."""
        if not self.current_profile or not self.current_type:
            return

        if self.session_start_time:
            duration = time.time() - self.session_start_time
            stats = usage_stats.get_stats(self.current_profile, self.current_type)
            stats.record_usage(duration)
            usage_stats.save()

            logger.info(
                f"Ended session for {self.current_type}:{self.current_profile}, "
                f"duration: {duration:.1f}s"
            )

        self.current_profile = None
        self.current_type = None
        self.session_start_time = None

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage for current profile."""
        if not self.current_profile or not self.current_type:
            logger.warning("No active session to record tokens")
            return

        usage_stats.record_tokens(
            self.current_profile,
            self.current_type,
            input_tokens,
            output_tokens
        )

        logger.debug(
            f"Recorded tokens for {self.current_type}:{self.current_profile}: "
            f"input={input_tokens}, output={output_tokens}"
        )

    def record_error(self):
        """Record an error for current profile."""
        if not self.current_profile or not self.current_type:
            logger.warning("No active session to record error")
            return

        usage_stats.record_error(self.current_profile, self.current_type)
        logger.debug(f"Recorded error for {self.current_type}:{self.current_profile}")

    def record_success(self):
        """Record a success for current profile."""
        if not self.current_profile or not self.current_type:
            logger.warning("No active session to record success")
            return

        usage_stats.record_success(self.current_profile, self.current_type)
        logger.debug(f"Recorded success for {self.current_type}:{self.current_profile}")

    def get_current_session_duration(self) -> float:
        """Get current session duration in seconds."""
        if not self.session_start_time:
            return 0.0

        return time.time() - self.session_start_time


# Global instance
usage_recorder = UsageRecorder()
