"""
自定义日志处理器，用于在 GUI 中显示日志
"""
import logging
from queue import Queue
from typing import Optional


class GUILogHandler(logging.Handler):
    """将日志消息发送到队列，供 GUI 线程消费"""

    def __init__(self, log_queue: Queue):
        super().__init__()
        self.log_queue = log_queue

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
            self.log_queue.put({
                'message': msg,
                'level': record.levelname,
                'levelno': record.levelno
            })
        except Exception:
            self.handleError(record)


class LogManager:
    """日志管理器，管理日志队列和处理器"""

    def __init__(self):
        self.log_queue: Queue = Queue()
        self.gui_handler: Optional[GUILogHandler] = None
        self._is_initialized = False

    def initialize(self):
        """初始化日志系统"""
        if self._is_initialized:
            return

        # 创建 GUI 日志处理器
        self.gui_handler = GUILogHandler(self.log_queue)
        self.gui_handler.setLevel(logging.DEBUG)

        # 添加到根日志记录器
        root_logger = logging.getLogger()
        root_logger.addHandler(self.gui_handler)

        self._is_initialized = True

    def get_log_queue(self) -> Queue:
        """获取日志队列"""
        return self.log_queue

    def shutdown(self):
        """关闭日志系统"""
        if self.gui_handler:
            root_logger = logging.getLogger()
            root_logger.removeHandler(self.gui_handler)
            self.gui_handler.close()
            self._is_initialized = False


# 全局日志管理器实例
log_manager = LogManager()
