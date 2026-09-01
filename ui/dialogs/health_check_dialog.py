"""
健康检查对话框
"""
import customtkinter as ctk
import threading
import logging
from typing import Optional
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, button_style, center_window, font, textbox_style
from ui.ui_dispatch import run_on_ui_thread

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
        self._configure_result_tags()

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
        try:
            thread = threading.Thread(target=self._run_check, name="health-check", daemon=True)
            thread.start()
        except Exception as exc:
            self.is_checking = False
            self.check_button.configure(state="normal", text="重新检查")
            self.export_button.configure(state="normal")
            self.status_label.configure(text="健康检查启动失败")
            logger.error("Failed to start health check worker: %s", exc, exc_info=True)
            self._display_error(str(exc))

    def _run_check(self):
        """执行健康检查（后台线程）"""
        try:
            from core.validator import config_validator

            # 执行验证
            results = config_validator.validate_all()
            self.results = results

            # 在主线程更新 UI
            self._safe_after(lambda: self._display_results(results))

        except Exception as e:
            error_message = str(e)
            logger.error(f"Health check failed: {e}", exc_info=True)
            self._safe_after(lambda: self._display_error(error_message))

        finally:
            self.is_checking = False
            self._safe_after(self._finish_check)

    def _safe_after(self, callback) -> None:
        """Schedule UI work from a background thread if the dialog still exists."""
        run_on_ui_thread(self, callback, logger, "health check refresh")

    def _finish_check(self) -> None:
        if not self.winfo_exists():
            return
        self.check_button.configure(state="normal", text="重新检查")
        self.export_button.configure(state="normal")

    @staticmethod
    def _summarize_results(results) -> dict[str, int | bool]:
        """Summarize this dialog's immutable result snapshot.

        Do not read the module-level validator here: a delayed UI callback or
        another check must not replace the counts shown for this run. Unknown
        statuses are counted as errors so malformed plugin results fail safe.
        """

        statuses = [str(getattr(result, "status", "")).strip().lower() for result in results]
        ok_count = statuses.count("ok")
        warning_count = statuses.count("warning")
        error_count = len(statuses) - ok_count - warning_count
        return {
            "total": len(statuses),
            "ok": ok_count,
            "warning": warning_count,
            "error": error_count,
            "has_issues": warning_count > 0 or error_count > 0,
        }

    @classmethod
    def _build_report_text(cls, results, checked_at) -> str:
        """Build a credential-redacted report from one result snapshot."""

        summary = cls._summarize_results(results)
        lines = [
            "=" * 80,
            "系统健康检查报告",
            "=" * 80,
            "",
            f"检查时间: {checked_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"总计: {summary['total']} 项",
            f"正常: {summary['ok']} 项",
            f"警告: {summary['warning']} 项",
            f"错误: {summary['error']} 项",
            "",
        ]
        categories = {}
        for result in results:
            category = safe_feedback_text(getattr(result, "category", "未分类"))
            categories.setdefault(category, []).append(result)

        status_labels = {"ok": "✓ 正常", "warning": "⚠ 警告", "error": "✗ 错误"}
        for category, items in categories.items():
            lines.extend(("=" * 80, category, "=" * 80, ""))
            for item in items:
                status = str(getattr(item, "status", "")).strip().lower()
                status_text = status_labels.get(status, "✗ 状态无效")
                item_name = safe_feedback_text(getattr(item, "item", "未知检查项"))
                message = safe_feedback_text(getattr(item, "message", ""))
                lines.append(f"{status_text} - {item_name}: {message}")
                suggestion = safe_feedback_text(getattr(item, "suggestion", ""))
                if suggestion:
                    lines.append(f"  建议: {suggestion}")
                lines.append("")
        return "\n".join(lines) + "\n"

    def _display_results(self, results):
        """显示检查结果"""
        if not self.winfo_exists():
            return
        # 清空文本框
        self.result_text.delete("1.0", "end")

        # 获取摘要
        summary = self._summarize_results(results)

        # 更新统计信息
        self.total_label.configure(text=f"总计: {summary['total']}")
        self.ok_label.configure(text=f"✓ 正常: {summary['ok']}")
        self.warning_label.configure(text=f"⚠ 警告: {summary['warning']}")
        self.error_label.configure(
            text=safe_feedback_text(f"✗ 错误: {summary['error']}")
        )

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
            self._insert_text(f"{safe_feedback_text(category)}\n", "category")
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
                self._insert_text(f"{safe_feedback_text(item.item)}: ", "bold")
                self._insert_text(f"{safe_feedback_text(item.message)}\n", color)

                # 修复建议
                if item.suggestion:
                    self._insert_text(
                        f"   建议: {safe_feedback_text(item.suggestion)}\n",
                        "suggestion",
                    )

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

    def _configure_result_tags(self) -> None:
        """Configure scalable CTkTextbox tags without its forbidden font option."""

        colors = {
            "bold": COLORS["text"],
            "category": COLORS["primary"],
            "green": COLORS["success"],
            "orange": COLORS["warning"],
            "red": COLORS["danger"],
            "suggestion": COLORS["muted"],
        }
        for tag, foreground in colors.items():
            self.result_text.tag_config(tag, foreground=foreground)

    def _display_error(self, error_message: str):
        """显示错误信息"""
        if not self.winfo_exists():
            return
        self.status_label.configure(text="检查失败")
        self.result_text.delete("1.0", "end")
        self._insert_text("健康检查失败\n\n", "bold")
        self._insert_text(f"错误信息: {safe_feedback_text(error_message)}\n", "red")

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

            report = self._build_report_text(self.results, datetime.now())
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(report)

            logger.info(f"Health check report exported to: {filepath}")
            self.status_label.configure(text=f"报告已导出: {filepath}")

        except Exception as e:
            logger.error(f"Failed to export report: {e}", exc_info=True)
            self.status_label.configure(text=safe_feedback_text(f"导出失败: {e}"))
