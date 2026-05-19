"""
错误统计对话框
"""
import customtkinter as ctk
import threading
from pathlib import Path
from datetime import datetime

from ui.theme import COLORS, button_style, card_frame_kwargs, center_window, combo_style, font, textbox_style


class ErrorStatsDialog(ctk.CTkToplevel):
    """错误统计对话框"""

    def __init__(self, parent, provider: str):
        super().__init__(parent)

        self.provider = provider
        self.title(f"{provider} 错误统计")
        self.geometry("900x700")
        self.resizable(True, True)
        self.minsize(760, 560)
        self.configure(fg_color=COLORS["app_bg"])

        # 模态对话框
        self.transient(parent)
        self.grab_set()

        self.stats = None
        self._create_widgets()
        center_window(self, parent)
        self._load_stats()

    def _create_widgets(self):
        """创建界面组件"""
        # 顶部信息栏
        info_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        info_frame.pack(fill="x", padx=20, pady=(20, 10))

        self.status_label = ctk.CTkLabel(
            info_frame,
            text=f"{self.provider} 错误恢复统计",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        )
        self.status_label.pack(pady=10)

        # 时间范围选择
        range_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        range_frame.pack(pady=5)

        ctk.CTkLabel(range_frame, text="时间范围", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=5)

        self.days_var = ctk.StringVar(value="7")
        days_options = ["1", "7", "30", "90"]
        days_menu = ctk.CTkComboBox(
            range_frame,
            variable=self.days_var,
            values=days_options,
            command=lambda _: self._load_stats(),
            width=100,
            **combo_style(),
        )
        days_menu.pack(side="left", padx=5)
        ctk.CTkLabel(range_frame, text="天", text_color=COLORS["muted"], font=font(12)).pack(side="left")

        # 统计卡片区域
        cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        cards_frame.pack(fill="x", padx=20, pady=10)

        # 总错误数
        self.total_card = self._create_stat_card(cards_frame, "总错误数", "0", "#e74c3c")
        self.total_card.pack(side="left", padx=5, expand=True, fill="both")

        # 成功恢复数
        self.recovery_card = self._create_stat_card(cards_frame, "成功恢复", "0", "#2ecc71")
        self.recovery_card.pack(side="left", padx=5, expand=True, fill="both")

        # 恢复成功率
        self.rate_card = self._create_stat_card(cards_frame, "恢复成功率", "0%", "#3498db")
        self.rate_card.pack(side="left", padx=5, expand=True, fill="both")

        # 平均恢复次数
        self.avg_card = self._create_stat_card(cards_frame, "平均恢复次数", "0", "#f39c12")
        self.avg_card.pack(side="left", padx=5, expand=True, fill="both")

        # 详细统计区域
        detail_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        detail_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # 使用 Textbox 显示详细信息
        self.detail_text = ctk.CTkTextbox(
            detail_frame,
            wrap="word",
            **textbox_style(monospace=True),
        )
        self.detail_text.pack(fill="both", expand=True, padx=5, pady=5)

        # 按钮栏
        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=20, pady=(10, 20))

        ctk.CTkButton(
            button_frame,
            text="刷新",
            command=self._load_stats,
            width=100,
            **button_style("secondary"),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            button_frame,
            text="导出报告",
            command=self._export_report,
            width=100,
            **button_style("accent"),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            button_frame,
            text="清空日志",
            command=self._clear_logs,
            width=100,
            **button_style("danger"),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            button_frame,
            text="关闭",
            command=self.destroy,
            width=100,
            **button_style("primary"),
        ).pack(side="right", padx=5)

    def _create_stat_card(self, parent, title: str, value: str, color: str):
        """创建统计卡片"""
        card = ctk.CTkFrame(parent, fg_color=color, corner_radius=8)

        ctk.CTkLabel(
            card,
            text=title,
            font=font(11),
            text_color="white"
        ).pack(pady=(10, 5))

        value_label = ctk.CTkLabel(
            card,
            text=value,
            font=font(20, "bold"),
            text_color="white"
        )
        value_label.pack(pady=(0, 10))

        # 保存 value_label 引用以便更新
        card.value_label = value_label

        return card

    def _load_stats(self):
        """加载统计数据"""
        self.status_label.configure(text="正在加载统计数据...")
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "加载中...")
        self.detail_text.configure(state="disabled")

        # 在后台线程加载
        thread = threading.Thread(target=self._load_stats_thread, daemon=True)
        thread.start()

    def _load_stats_thread(self):
        """后台线程加载统计数据"""
        try:
            from core.auto_continue.error_analyzer import get_analyzer

            days = int(self.days_var.get())
            analyzer = get_analyzer(self.provider)
            stats = analyzer.analyze(days)
            self.stats = stats

            # 在主线程更新 UI
            self._safe_after(lambda: self._display_stats(stats))

        except Exception as e:
            error_message = str(e)
            self._safe_after(lambda: self._display_error(error_message))

    def _safe_after(self, callback) -> None:
        """Schedule UI work from a background thread if the dialog still exists."""
        try:
            if self.winfo_exists():
                self.after(0, callback)
        except Exception:
            pass

    def _display_stats(self, stats):
        """显示统计数据"""
        if not self.winfo_exists():
            return
        # 更新卡片
        self.total_card.value_label.configure(text=str(stats.total_errors))
        self.recovery_card.value_label.configure(text=str(stats.total_recoveries))
        self.rate_card.value_label.configure(text=f"{stats.recovery_success_rate:.1f}%")
        self.avg_card.value_label.configure(text=f"{stats.avg_recovery_count:.1f}")

        # 更新状态
        self.status_label.configure(text=f"{self.provider} 错误恢复统计 (最近 {self.days_var.get()} 天)")

        # 显示详细信息
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")

        # 按错误类型统计
        self._insert_text("按错误类型统计\n", "header")
        self._insert_text("=" * 80 + "\n\n", "separator")

        if stats.errors_by_type:
            for error_type, count in sorted(stats.errors_by_type.items(), key=lambda x: x[1], reverse=True):
                percentage = (count / stats.total_errors * 100) if stats.total_errors > 0 else 0
                self._insert_text(f"{error_type:30s} ", "normal")
                self._insert_text(f"{count:5d} ", "count")
                self._insert_text(f"({percentage:5.1f}%)\n", "percentage")
        else:
            self._insert_text("暂无数据\n", "muted")

        self._insert_text("\n")

        # 最常见错误
        if stats.most_common_error:
            self._insert_text("最常见错误\n", "header")
            self._insert_text("=" * 80 + "\n\n", "separator")
            self._insert_text(f"{stats.most_common_error}\n\n", "highlight")

        # 最近的错误
        self._insert_text("最近的错误 (最多 10 条)\n", "header")
        self._insert_text("=" * 80 + "\n\n", "separator")

        if stats.recent_errors:
            for entry in stats.recent_errors:
                timestamp = entry.get("timestamp", "N/A")
                error_type = entry.get("error_type", "unknown")
                error_code = entry.get("error_code", "N/A")
                error_message = entry.get("error_message", "N/A")
                action = entry.get("action", "N/A")
                recovery_count = entry.get("recovery_count", 0)

                self._insert_text("时间: ", "label")
                self._insert_text(f"{timestamp}\n", "normal")

                self._insert_text("类型: ", "label")
                self._insert_text(f"{error_type}\n", "normal")

                self._insert_text("代码: ", "label")
                self._insert_text(f"{error_code}\n", "normal")

                self._insert_text("消息: ", "label")
                self._insert_text(f"{error_message}\n", "normal")

                self._insert_text("操作: ", "label")
                self._insert_text(f"{action}\n", "normal")

                self._insert_text("恢复次数: ", "label")
                self._insert_text(f"{recovery_count}\n", "count")

                self._insert_text("-" * 80 + "\n\n", "separator")
        else:
            self._insert_text("暂无数据\n", "muted")

        self.detail_text.configure(state="disabled")
        self.detail_text.see("1.0")

    def _insert_text(self, text: str, tag: str = "normal"):
        """插入带标签的文本"""
        start_index = self.detail_text.index("end-1c")
        self.detail_text.insert("end", text)
        end_index = self.detail_text.index("end-1c")

        if tag:
            self.detail_text.tag_add(tag, start_index, end_index)

        # 配置标签样式
        if tag == "header":
            self.detail_text.tag_config(tag, font=("Microsoft YaHei UI", 13, "bold"), foreground=COLORS["primary"])
        elif tag == "separator":
            self.detail_text.tag_config(tag, foreground=COLORS["border"])
        elif tag == "label":
            self.detail_text.tag_config(tag, font=("Consolas", 11, "bold"), foreground=COLORS["text"])
        elif tag == "count":
            self.detail_text.tag_config(tag, foreground=COLORS["danger"], font=("Consolas", 11, "bold"))
        elif tag == "percentage":
            self.detail_text.tag_config(tag, foreground=COLORS["muted"])
        elif tag == "highlight":
            self.detail_text.tag_config(tag, foreground=COLORS["danger"], font=("Consolas", 12, "bold"))
        elif tag == "muted":
            self.detail_text.tag_config(tag, foreground=COLORS["muted"], font=("Consolas", 11, "italic"))

    def _display_error(self, error_message: str):
        """显示错误信息"""
        if not self.winfo_exists():
            return
        self.status_label.configure(text="加载失败")
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self._insert_text("加载统计数据失败\n\n", "header")
        self._insert_text(f"错误信息: {error_message}\n", "highlight")
        self.detail_text.configure(state="disabled")

    def _export_report(self):
        """导出报告"""
        if not self.stats:
            from ui.widgets.toast import show_toast
            show_toast(self, "请先加载统计数据", is_error=True)
            return

        try:
            from tkinter import filedialog
            from core.auto_continue.error_analyzer import get_analyzer

            # 选择保存位置
            default_filename = f"{self.provider}_error_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = filedialog.asksaveasfilename(
                parent=self,
                title="导出错误统计报告",
                defaultextension=".txt",
                initialfile=default_filename,
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )

            if not filepath:
                return

            # 导出报告
            days = int(self.days_var.get())
            analyzer = get_analyzer(self.provider)
            analyzer.export_report(Path(filepath), days)

            from ui.widgets.toast import show_toast
            show_toast(self, f"报告已导出: {filepath}")

        except Exception as e:
            from ui.widgets.toast import show_toast
            show_toast(self, f"导出失败: {e}", is_error=True)

    def _clear_logs(self):
        """清空日志"""
        from ui.dialogs.confirm_dialog import ConfirmDialog

        def do_clear():
            try:
                if self.provider.lower() == "claude":
                    config_dir = Path.home() / ".claude"
                else:
                    config_dir = Path.home() / ".codex"

                for base_dir in [config_dir / "tmp", config_dir]:
                    for filename in ["error_recovery_log.jsonl", "error_recovery_state.json"]:
                        path = base_dir / filename
                        if path.exists():
                            path.unlink()

                from ui.widgets.toast import show_toast
                show_toast(self, "日志已清空")
                self._load_stats()

            except Exception as e:
                from ui.widgets.toast import show_toast
                show_toast(self, f"清空失败: {e}", is_error=True)

        ConfirmDialog(
            self,
            title="确认清空",
            message="确定要清空所有错误恢复日志吗？\n此操作不可撤销。",
            on_confirm=do_clear
        )
