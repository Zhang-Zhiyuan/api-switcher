"""
日志查看器 Tab
"""
import customtkinter as ctk
import logging
from queue import Empty
from tkinter import filedialog
from datetime import datetime

from core.log_handler import log_manager
from ui.theme import COLORS, button_style, combo_style, font, textbox_style
from ui.widgets.toast import show_toast


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _coerce_levelno(value, level: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(getattr(logging, level, logging.INFO))


def _prepare_log_entries(log_entries: list[dict], filter_level: str) -> tuple[list[tuple[str, str]], dict[str, int]]:
    filter_levelno = int(getattr(logging, filter_level, logging.DEBUG))
    visible_entries: list[tuple[str, str]] = []
    count_delta = {level: 0 for level in LOG_LEVELS}

    for log_entry in log_entries:
        if not isinstance(log_entry, dict):
            continue
        level = str(log_entry.get("level") or "INFO").upper()
        if level not in count_delta:
            level = "INFO"
        levelno = _coerce_levelno(log_entry.get("levelno"), level)
        message = str(log_entry.get("message") or "")

        count_delta[level] += 1
        if levelno >= filter_levelno:
            visible_entries.append((level, message))

    return visible_entries, count_delta


class LogViewerTab(ctk.CTkScrollableFrame):
    """日志查看器 Tab"""

    LOG_BATCH_LIMIT = 250
    ACTIVE_POLL_MS = 80
    IDLE_POLL_MS = 450

    # 日志级别颜色映射
    LEVEL_COLORS = {
        'DEBUG': COLORS['muted'],
        'INFO': COLORS['text'],
        'WARNING': '#FFA500',  # Orange
        'ERROR': '#FF6B6B',    # Red
        'CRITICAL': '#FF0000', # Bright Red
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._auto_scroll = True
        self._filter_level = 'DEBUG'  # 显示所有级别
        self._poll_after_id = None
        self._build_ui()
        self._start_log_polling()

    def destroy(self):
        self._cancel_log_polling()
        super().destroy()

    def _build_ui(self):
        """构建 UI"""
        # 顶部工具栏
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=14, pady=(14, 8))

        # 标题区域
        title_area = ctk.CTkFrame(toolbar, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="日志查看器",
            text_color=COLORS["text"],
            font=font(18, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="实时查看应用程序日志，支持过滤和导出",
            text_color=COLORS["muted"],
            font=font(12)
        ).pack(anchor="w", pady=(2, 0))

        # 按钮区域
        ctk.CTkButton(
            toolbar,
            text="导出日志",
            width=96,
            command=self._export_logs,
            **button_style("accent")
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            toolbar,
            text="清空日志",
            width=96,
            command=self._clear_logs,
            **button_style("danger")
        ).pack(side="right", padx=(8, 0))

        # 过滤工具栏
        filter_bar = ctk.CTkFrame(self, fg_color="transparent")
        filter_bar.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(
            filter_bar,
            text="日志级别:",
            text_color=COLORS["muted"],
            font=font(12)
        ).pack(side="left", padx=(0, 8))

        self._level_combo = ctk.CTkComboBox(
            filter_bar,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            width=120,
            command=self._on_level_change,
            **combo_style(),
        )
        self._level_combo.set("DEBUG")
        self._level_combo.pack(side="left", padx=(0, 12))

        # 自动滚动开关
        self._auto_scroll_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            filter_bar,
            text="自动滚动",
            variable=self._auto_scroll_var,
            command=self._toggle_auto_scroll,
            text_color=COLORS["text"],
            font=font(12)
        ).pack(side="left", padx=(0, 12))

        # 统计信息
        self._stats_label = ctk.CTkLabel(
            filter_bar,
            text="",
            text_color=COLORS["muted"],
            font=font(12)
        )
        self._stats_label.pack(side="right")

        # 日志显示区域
        log_frame = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=8)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self._log_text = ctk.CTkTextbox(
            log_frame,
            wrap="word",
            activate_scrollbars=True,
            **textbox_style(monospace=True),
        )
        self._log_text.pack(fill="both", expand=True, padx=2, pady=2)

        # 配置标签用于着色
        for level, color in self.LEVEL_COLORS.items():
            self._log_text.tag_config(level, foreground=color)

        # 统计计数器
        self._log_counts = {
            level: 0
            for level in LOG_LEVELS
        }

    def _start_log_polling(self):
        """开始轮询日志队列"""
        self._poll_logs()

    def _schedule_log_polling(self, delay_ms: int | None = None):
        self._cancel_log_polling()
        try:
            self._poll_after_id = self.after(delay_ms or self.IDLE_POLL_MS, self._poll_logs)
        except Exception:
            self._poll_after_id = None

    def _cancel_log_polling(self):
        if not self._poll_after_id:
            return
        try:
            self.after_cancel(self._poll_after_id)
        except Exception:
            pass
        self._poll_after_id = None

    def _poll_logs(self):
        """从队列中获取日志并显示"""
        self._poll_after_id = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        log_entries = []
        try:
            log_entries = self._drain_log_queue()
            if log_entries:
                self._append_log_entries(log_entries)
                self._update_stats()

        except Exception as e:
            logging.error(f"Error polling logs: {e}")

        self._schedule_log_polling(self.ACTIVE_POLL_MS if log_entries else self.IDLE_POLL_MS)

    def _drain_log_queue(self) -> list[dict]:
        log_entries = []
        while len(log_entries) < self.LOG_BATCH_LIMIT:
            try:
                log_entries.append(log_manager.get_log_queue().get_nowait())
            except Empty:
                break
        return log_entries

    def _add_log_entry(self, log_entry: dict):
        """添加日志条目"""
        self._append_log_entries([log_entry])

    def _append_log_entries(self, log_entries: list[dict]):
        visible_entries, count_delta = _prepare_log_entries(log_entries, self._filter_level)
        for level, count in count_delta.items():
            self._log_counts[level] += count

        if not visible_entries:
            return

        self._log_text.configure(state="normal")
        for level, message in visible_entries:
            start_index = self._log_text.index("end-1c")
            self._log_text.insert("end", message + "\n")
            end_index = self._log_text.index("end-1c")
            self._log_text.tag_add(level, start_index, end_index)
        self._log_text.configure(state="disabled")

        if self._auto_scroll:
            self._log_text.see("end")

    def _on_level_change(self, value: str):
        """日志级别过滤改变"""
        self._filter_level = value
        show_toast(self.winfo_toplevel(), f"日志级别已设置为: {value}")

    def _toggle_auto_scroll(self):
        """切换自动滚动"""
        self._auto_scroll = self._auto_scroll_var.get()

    def _update_stats(self):
        """更新统计信息"""
        total = sum(self._log_counts.values())
        errors = self._log_counts['ERROR'] + self._log_counts['CRITICAL']
        warnings = self._log_counts['WARNING']

        stats_text = f"总计: {total}  |  警告: {warnings}  |  错误: {errors}"
        self._stats_label.configure(text=stats_text)

    def _clear_logs(self):
        """清空日志"""
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

        # 重置计数
        for key in self._log_counts:
            self._log_counts[key] = 0
        self._update_stats()

        show_toast(self.winfo_toplevel(), "日志已清空")

    def _export_logs(self):
        """导出日志到文件"""
        try:
            # 选择保存位置
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_filename = f"logs_{timestamp}.txt"

            filepath = filedialog.asksaveasfilename(
                title="导出日志",
                defaultextension=".txt",
                initialfile=default_filename,
                filetypes=[
                    ("文本文件", "*.txt"),
                    ("所有文件", "*.*")
                ]
            )

            if not filepath:
                return

            # 获取日志内容
            content = self._log_text.get("1.0", "end-1c")

            # 写入文件
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)

            show_toast(self.winfo_toplevel(), f"日志已导出到: {filepath}")

        except Exception as e:
            show_toast(self.winfo_toplevel(), f"导出失败: {e}", is_error=True)

    def refresh(self):
        """刷新（占位方法，保持与其他 Tab 一致）"""
        pass
