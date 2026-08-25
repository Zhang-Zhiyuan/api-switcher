"""
自定义日志处理器，用于在 GUI 中显示日志
"""
import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from queue import Empty, Full, Queue
from typing import Optional


APP_LOG_MAX_BYTES = 8 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 2


def create_bounded_file_handler(
    path,
    *,
    max_bytes: int = APP_LOG_MAX_BYTES,
    backup_count: int = APP_LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    """Create a UTF-8 file handler whose disk usage remains bounded.

    The base filename is already split by day. Size-based rollover additionally
    protects a single noisy day from producing an arbitrarily large file that
    slows startup diagnostics and fills the user's profile directory.
    """

    return RotatingFileHandler(
        path,
        maxBytes=max(1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
        delay=True,
    )


class ExpectedLibraryNoiseFilter(logging.Filter):
    """Keep routine library disconnects from looking like application bugs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "paramiko.transport"
            and record.levelno == logging.ERROR
            and record.getMessage().startswith("Socket exception:")
        ):
            # Paramiko reports ordinary remote socket closure as ERROR even
            # though SSHManager already retries/surfaces the operation result.
            # Preserve the diagnostic as WARNING without inflating the UI's
            # application-error counter or hiding the event completely.
            record.levelno = logging.WARNING
            record.levelname = logging.getLevelName(logging.WARNING)
        return True


class GUILogHandler(logging.Handler):
    """将日志消息发送到队列，供 GUI 线程消费"""

    def __init__(self, manager: "LogManager"):
        super().__init__()
        self.manager = manager

        # 设置日志格式
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord):
        """发送日志记录到队列"""
        try:
            # 格式化日志消息
            msg = self.format(record)

            # 添加到队列（包含级别信息用于着色）
            self.manager.publish({
                'message': msg,
                'level': record.levelname,
                'levelno': record.levelno
            })
        except Exception:
            self.handleError(record)


class LogManager:
    """日志管理器，管理日志队列和处理器"""

    MAX_HISTORY = 5000
    MAX_QUEUE = 2000

    def __init__(self):
        self.log_queue: Queue = Queue(maxsize=self.MAX_QUEUE)
        self.gui_handler: Optional[GUILogHandler] = None
        self._is_initialized = False
        self._history = deque(maxlen=self.MAX_HISTORY)
        self._lock = threading.RLock()

    def initialize(self):
        """初始化日志系统"""
        with self._lock:
            if self._is_initialized:
                return

            # Publish the handler only after registration succeeds. This also
            # prevents concurrent app-start paths from installing duplicates.
            handler = GUILogHandler(self)
            handler.setLevel(logging.DEBUG)
            handler.addFilter(ExpectedLibraryNoiseFilter())
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            self.gui_handler = handler
            self._is_initialized = True

    def get_log_queue(self) -> Queue:
        """获取日志队列"""
        return self.log_queue

    def publish(self, entry: dict) -> None:
        """Store a log entry and notify UI consumers without unbounded growth."""
        item = dict(entry)
        with self._lock:
            self._history.append(item)
            try:
                self.log_queue.put_nowait(item)
            except Full:
                try:
                    self.log_queue.get_nowait()
                except Empty:
                    pass
                try:
                    self.log_queue.put_nowait(item)
                except Full:
                    pass

    def get_recent_entries(self, limit: int | None = None) -> list[dict]:
        """Return a snapshot of recent in-memory log entries."""
        with self._lock:
            entries = list(self._history)
        if limit is None or limit <= 0 or limit >= len(entries):
            return entries
        return entries[-limit:]

    def consume_recent_entries(self, limit: int | None = None) -> list[dict]:
        """Atomically snapshot history and discard the same queued backlog.

        A newly opened viewer renders the history snapshot. Draining the queue
        under the same lock prevents that identical backlog from being appended
        a second time while allowing later publications to remain queued.
        """
        with self._lock:
            entries = list(self._history)
            while True:
                try:
                    self.log_queue.get_nowait()
                except Empty:
                    break
        if limit is None or limit <= 0 or limit >= len(entries):
            return entries
        return entries[-limit:]

    def clear_history(self) -> None:
        """Clear in-memory logs and any queued-but-not-rendered entries."""
        with self._lock:
            self._history.clear()
            while True:
                try:
                    self.log_queue.get_nowait()
                except Empty:
                    break

    def shutdown(self):
        """关闭日志系统"""
        with self._lock:
            handler = self.gui_handler
            if handler is None:
                self._is_initialized = False
                return
            root_logger = logging.getLogger()
            root_logger.removeHandler(handler)
            handler.close()
            self.gui_handler = None
            self._is_initialized = False


# 全局日志管理器实例
log_manager = LogManager()
