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


class LogViewerTab(ctk.CTkScrollableFrame):
    """日志查看器 Tab"""

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
        self._build_ui()
        self._start_log_polling()

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
            'DEBUG': 0,
            'INFO': 0,
            'WARNING': 0,
            'ERROR': 0,
            'CRITICAL': 0
        }

    def _start_log_polling(self):
        """开始轮询日志队列"""
        self._poll_logs()

    def _poll_logs(self):
        """从队列中获取日志并显示"""
        try:
            # 批量处理日志（最多100条）
            batch_count = 0
            while batch_count < 100:
                try:
                    log_entry = log_manager.get_log_queue().get_nowait()
                    self._add_log_entry(log_entry)
                    batch_count += 1
                except Empty:
                    break

            # 更新统计信息
            if batch_count > 0:
                self._update_stats()

        except Exception as e:
            logging.error(f"Error polling logs: {e}")

        # 继续轮询（每100ms）
        self.after(100, self._poll_logs)

    def _add_log_entry(self, log_entry: dict):
        """添加日志条目"""
        level = log_entry['level']
        levelno = log_entry['levelno']
        message = log_entry['message']

        # 更新计数
        if level in self._log_counts:
            self._log_counts[level] += 1

        # 检查过滤级别
        filter_levelno = getattr(logging, self._filter_level)
        if levelno < filter_levelno:
            return

        # 添加到文本框
        self._log_text.configure(state="normal")

        # 插入日志消息
        start_index = self._log_text.index("end-1c")
        self._log_text.insert("end", message + "\n")
        end_index = self._log_text.index("end-1c")

        # 应用颜色标签
        self._log_text.tag_add(level, start_index, end_index)

        self._log_text.configure(state="disabled")

        # 自动滚动到底部
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
