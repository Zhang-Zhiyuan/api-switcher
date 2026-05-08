"""
健康检查对话框
"""
import customtkinter as ctk
import threading
import logging
from typing import Optional
from ui.theme import COLORS, button_style, center_window, font, textbox_style

logger = logging.getLogger(__name__)


class HealthCheckDialog(ctk.CTkToplevel):
    """健康检查对话框"""

    def __init__(self, parent):
        super().__init__(parent)

        self.title("系统健康检查")
        self.geometry("900x700")
        self.resizable(True, True)
        self.minsize(760, 560)
        self.configure(fg_color=COLORS["app_bg"])

        # 模态对话框
        self.transient(parent)
        self.grab_set()

        self.results = []
        self.is_checking = False

        self._create_widgets()
        center_window(self, parent)

    def _create_widgets(self):
        """创建界面组件"""
        # 顶部信息栏
        info_frame = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=8)
        info_frame.pack(fill="x", padx=20, pady=(20, 10))

        self.status_label = ctk.CTkLabel(
            info_frame,
            text="点击「开始检查」按钮进行系统健康检查",
            text_color=COLORS["text"],
            font=font(14)
        )
        self.status_label.pack(pady=10)

        # 统计信息
        stats_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        stats_frame.pack(pady=10)

        self.total_label = ctk.CTkLabel(stats_frame, text="总计: 0", text_color=COLORS["muted"], font=font(12))
        self.total_label.grid(row=0, column=0, padx=15)

        self.ok_label = ctk.CTkLabel(stats_frame, text="✓ 正常: 0", font=font(12), text_color=COLORS["success"])
        self.ok_label.grid(row=0, column=1, padx=15)

        self.warning_label = ctk.CTkLabel(stats_frame, text="⚠ 警告: 0", font=font(12), text_color=COLORS["warning"])
        self.warning_label.grid(row=0, column=2, padx=15)

        self.error_label = ctk.CTkLabel(stats_frame, text="✗ 错误: 0", font=font(12), text_color=COLORS["danger"])
        self.error_label.grid(row=0, column=3, padx=15)

        # 结果显示区域
        result_frame = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=8)
        result_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # 使用 Textbox 显示结果
        self.result_text = ctk.CTkTextbox(
            result_frame,
            wrap="word",
            **textbox_style(monospace=True),
        )
        self.result_text.pack(fill="both", expand=True, padx=5, pady=5)

        # 按钮栏
        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=20, pady=(10, 20))

        self.check_button = ctk.CTkButton(
            button_frame,
            text="开始检查",
            command=self._start_check,
            width=120,
            **button_style("primary")
        )
        self.check_button.pack(side="left", padx=5)

        self.export_button = ctk.CTkButton(
            button_frame,
            text="导出报告",
            command=self._export_report,
            width=120,
            state="disabled",
            **button_style("accent")
        )
        self.export_button.pack(side="left", padx=5)

        self.close_button = ctk.CTkButton(
            button_frame,
            text="关闭",
            command=self.destroy,
            width=120,
            **button_style("secondary")
        )
        self.close_button.pack(side="right", padx=5)

    def _start_check(self):
        """开始健康检查"""
        if self.is_checking:
            return

        self.is_checking = True
        self.check_button.configure(state="disabled", text="检查中...")
        self.export_button.configure(state="disabled")
        self.result_text.delete("1.0", "end")
        self.status_label.configure(text="正在进行健康检查，请稍候...")

        # 在后台线程执行检查
        thread = threading.Thread(target=self._run_check, daemon=True)
        thread.start()

    def _run_check(self):
        """执行健康检查（后台线程）"""
        try:
            from core.validator import config_validator

            # 执行验证
            results = config_validator.validate_all()
            self.results = results

            # 在主线程更新 UI
            self.after(0, self._display_results, results)

        except Exception as e:
            logger.error(f"Health check failed: {e}", exc_info=True)
            self.after(0, self._display_error, str(e))

        finally:
            self.is_checking = False
            self.after(0, lambda: self.check_button.configure(state="normal", text="重新检查"))
            self.after(0, lambda: self.export_button.configure(state="normal"))

    def _display_results(self, results):
        """显示检查结果"""
        from core.validator import config_validator

        # 清空文本框
        self.result_text.delete("1.0", "end")

        # 获取摘要
        summary = config_validator.get_summary()

        # 更新统计信息
        self.total_label.configure(text=f"总计: {summary['total']}")
        self.ok_label.configure(text=f"✓ 正常: {summary['ok']}")
        self.warning_label.configure(text=f"⚠ 警告: {summary['warning']}")
        self.error_label.configure(text=f"✗ 错误: {summary['error']}")

        # 更新状态
        if summary['has_issues']:
            self.status_label.configure(
                text=f"检查完成 - 发现 {summary['warning']} 个警告，{summary['error']} 个错误"
            )
        else:
            self.status_label.configure(text="检查完成 - 所有项目正常 ✓")

        # 按类别分组显示结果
        categories = {}
        for result in results:
            if result.category not in categories:
                categories[result.category] = []
            categories[result.category].append(result)

        # 显示结果
        for category, items in categories.items():
            self._insert_text(f"\n{'=' * 80}\n", "bold")
            self._insert_text(f"{category}\n", "category")
            self._insert_text(f"{'=' * 80}\n\n", "bold")

            for item in items:
                # 状态图标和颜色
                if item.status == "ok":
                    icon = "✓"
                    color = "green"
                elif item.status == "warning":
                    icon = "⚠"
                    color = "orange"
                else:
                    icon = "✗"
                    color = "red"

                # 检查项
                self._insert_text(f"{icon} ", color)
                self._insert_text(f"{item.item}: ", "bold")
                self._insert_text(f"{item.message}\n", color)

                # 修复建议
                if item.suggestion:
                    self._insert_text(f"   建议: {item.suggestion}\n", "suggestion")

                self._insert_text("\n")

        # 滚动到顶部
        self.result_text.see("1.0")

    def _insert_text(self, text: str, tag: Optional[str] = None):
        """插入文本并应用标签"""
        start_index = self.result_text.index("end-1c")
        self.result_text.insert("end", text)
        end_index = self.result_text.index("end-1c")

        if tag:
            self.result_text.tag_add(tag, start_index, end_index)

        # 配置标签样式
        if tag == "bold":
            self.result_text.tag_config(tag, font=("Consolas", 11, "bold"))
        elif tag == "category":
            self.result_text.tag_config(tag, font=("Microsoft YaHei UI", 13, "bold"), foreground=COLORS["primary"])
        elif tag == "green":
            self.result_text.tag_config(tag, foreground=COLORS["success"])
        elif tag == "orange":
            self.result_text.tag_config(tag, foreground=COLORS["warning"])
        elif tag == "red":
            self.result_text.tag_config(tag, foreground=COLORS["danger"])
        elif tag == "suggestion":
            self.result_text.tag_config(tag, foreground=COLORS["muted"], font=("Consolas", 10, "italic"))

    def _display_error(self, error_message: str):
        """显示错误信息"""
        self.status_label.configure(text="检查失败")
        self.result_text.delete("1.0", "end")
        self._insert_text("健康检查失败\n\n", "bold")
        self._insert_text(f"错误信息: {error_message}\n", "red")

    def _export_report(self):
        """导出检查报告"""
        if not self.results:
            return

        try:
            from tkinter import filedialog
            from datetime import datetime

            # 选择保存位置
            default_filename = f"health_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = filedialog.asksaveasfilename(
                parent=self,
                title="导出健康检查报告",
                defaultextension=".txt",
                initialfile=default_filename,
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )

            if not filepath:
                return

            # 生成报告内容
            from core.validator import config_validator
            summary = config_validator.get_summary()

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("=" * 80 + "\n")
                f.write("系统健康检查报告\n")
                f.write("=" * 80 + "\n\n")
                f.write(f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总计: {summary['total']} 项\n")
                f.write(f"正常: {summary['ok']} 项\n")
                f.write(f"警告: {summary['warning']} 项\n")
                f.write(f"错误: {summary['error']} 项\n\n")

                # 按类别分组
                categories = {}
                for result in self.results:
                    if result.category not in categories:
                        categories[result.category] = []
                    categories[result.category].append(result)

                # 写入详细结果
                for category, items in categories.items():
                    f.write("=" * 80 + "\n")
                    f.write(f"{category}\n")
                    f.write("=" * 80 + "\n\n")

                    for item in items:
                        status_text = {"ok": "✓ 正常", "warning": "⚠ 警告", "error": "✗ 错误"}[item.status]
                        f.write(f"{status_text} - {item.item}: {item.message}\n")
                        if item.suggestion:
                            f.write(f"  建议: {item.suggestion}\n")
                        f.write("\n")

            logger.info(f"Health check report exported to: {filepath}")
            self.status_label.configure(text=f"报告已导出: {filepath}")

        except Exception as e:
            logger.error(f"Failed to export report: {e}", exc_info=True)
            self.status_label.configure(text=f"导出失败: {e}")
