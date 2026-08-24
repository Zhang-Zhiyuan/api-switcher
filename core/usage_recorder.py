"""Usage statistics recorder - automatically tracks profile usage."""
import logging
import threading
import time
from typing import Optional
from core.usage_stats import usage_stats

logger = logging.getLogger(__name__)


class UsageRecorder:
    """Records usage statistics automatically."""

    def __init__(self):
        self._lock = threading.RLock()
        self.current_profile: Optional[str] = None
        self.current_type: Optional[str] = None
        self.session_start_time: Optional[float] = None

    def start_session(self, profile_name: str, profile_type: str):
        """Start tracking a profile session."""
        with self._lock:
            # End previous session if exists. The RLock keeps a concurrent
            # tray/UI switch from interleaving the old and new sessions.
            if self.current_profile and self.current_type:
                self.end_session()

            self.current_profile = profile_name
            self.current_type = profile_type
            # Wall-clock corrections can jump backwards and previously wrote
            # negative usage durations. Monotonic time measures only elapsed
            # process time and is stable across clock synchronization.
            self.session_start_time = time.monotonic()

            # Record the switch
            usage_stats.record_switch(profile_name, profile_type)

        logger.info(f"Started session for {profile_type}:{profile_name}")

    def end_session(self):
        """End current tracking session."""
        with self._lock:
            if not self.current_profile or not self.current_type:
                return

            profile_name = self.current_profile
            profile_type = self.current_type
            if self.session_start_time is not None:
                duration = max(0.0, time.monotonic() - self.session_start_time)
                usage_stats.record_usage(
                    profile_name,
                    profile_type,
                    duration,
                )

                logger.info(
                    f"Ended session for {profile_type}:{profile_name}, "
                    f"duration: {duration:.1f}s"
                )

            self.current_profile = None
            self.current_type = None
            self.session_start_time = None

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage for current profile."""
        with self._lock:
            if not self.current_profile or not self.current_type:
                logger.warning("No active session to record tokens")
                return

            profile_name = self.current_profile
            profile_type = self.current_type
            usage_stats.record_tokens(
                profile_name,
                profile_type,
                input_tokens,
                output_tokens
            )

        logger.debug(
            f"Recorded tokens for {profile_type}:{profile_name}: "
            f"input={input_tokens}, output={output_tokens}"
        )

    def record_error(self):
        """Record an error for current profile."""
        with self._lock:
            if not self.current_profile or not self.current_type:
                logger.warning("No active session to record error")
                return

            profile_name = self.current_profile
            profile_type = self.current_type
            usage_stats.record_error(profile_name, profile_type)
        logger.debug(f"Recorded error for {profile_type}:{profile_name}")

    def record_success(self):
        """Record a success for current profile."""
        with self._lock:
            if not self.current_profile or not self.current_type:
                logger.warning("No active session to record success")
                return

            profile_name = self.current_profile
            profile_type = self.current_type
            usage_stats.record_success(profile_name, profile_type)
        logger.debug(f"Recorded success for {profile_type}:{profile_name}")

    def get_current_session_duration(self) -> float:
        """Get current session duration in seconds."""
        with self._lock:
            if self.session_start_time is None:
                return 0.0

            return max(0.0, time.monotonic() - self.session_start_time)


# Global instance
usage_recorder = UsageRecorder()
