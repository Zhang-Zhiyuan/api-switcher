import logging
import threading
from datetime import datetime
from tkinter import filedialog

import customtkinter as ctk

from core import session_migration
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast

logger = logging.getLogger(__name__)


class SessionMigrationTab(ctk.CTkScrollableFrame):
    """Tab for exporting and importing Claude Code / Codex local sessions."""

    FILTER_OPTIONS = {
        "全部": "all",
        "Claude Code": "claude",
        "Codex CLI": "codex",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._stats_label = None
        self._filter_combo = None
        self._provider_filter = "all"
        self._records: list[session_migration.SessionRecord] = []
        self._selected_keys: set[str] = set()
        self._refresh_generation = 0
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="会话迁移",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkLabel(
            title_area,
            text="读取 Claude Code / Codex CLI 本地历史会话，导出指定会话并在另一台机器导入。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(title_area, subtitle, padding=12, min_width=260, max_width=620)

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right", padx=(12, 0))
        ctk.CTkButton(
            actions,
            text="导入到项目",
            width=112,
            command=self._import_package_to_project,
            **button_style("primary"),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="导入迁移包",
            width=112,
            command=self._import_package,
            **button_style("accent"),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="导出选中",
            width=104,
            command=self._export_selected,
            **button_style("success"),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="刷新",
            width=82,
            command=self.refresh,
            **button_style("secondary"),
        ).pack(side="left")

        filter_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        filter_bar.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(filter_bar, text="来源", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(12, 0), pady=9)
        self._filter_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.FILTER_OPTIONS.keys()),
            width=140,
            command=self._on_filter_change,
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.pack(side="left", padx=(8, 0), pady=9)

        ctk.CTkButton(
            filter_bar,
            text="全选当前",
            width=92,
            command=self._select_visible,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(12, 0), pady=9)
        ctk.CTkButton(
            filter_bar,
            text="清空选择",
            width=92,
            command=self._clear_selection,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(8, 0), pady=9)

        self._stats_label = ctk.CTkLabel(filter_bar, text="", text_color=COLORS["muted"], font=font(12))
        self._stats_label.pack(side="right", padx=(12, 12))

        warning = ctk.CTkLabel(
            self,
            text="会话迁移包会包含完整对话内容和工具记录，可能包含敏感信息；跨机器导入不会迁移账号登录态或 API Key。",
            text_color=COLORS["warning"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        warning.pack(fill="x", padx=14, pady=(0, 8))
        bind_wraplength(self, warning, padding=42, min_width=260, max_width=900)

        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self.refresh()

    def refresh(self):
        if not self._cards_frame:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        provider_filter = self._provider_filter
        for widget in self._cards_frame.winfo_children():
            widget.destroy()

        if self._stats_label:
            self._stats_label.configure(text="正在读取本机会话...")
        ctk.CTkLabel(
            self._cards_frame,
            text="正在读取本机会话...",
            text_color=COLORS["muted"],
            font=font(13),
        ).pack(fill="x", pady=(22, 6))

        def worker():
            try:
                payload = {
                    "records": session_migration.list_sessions(provider_filter),
                    "error": None,
                }
            except Exception as exc:
                payload = {"records": [], "error": str(exc)}

            def finish():
                try:
                    if not self.winfo_exists() or generation != self._refresh_generation:
                        return
                    if payload["error"]:
                        show_toast(self.winfo_toplevel(), f"读取会话失败: {payload['error']}", is_error=True)
                    self._records = payload["records"]
                    self._render_records()
                except Exception:
                    logger.exception("Failed to finish session migration refresh")

            try:
                self.after(0, finish)
            except Exception:
                logger.exception("Failed to schedule session migration refresh")

        threading.Thread(target=worker, daemon=True).start()

    def _render_records(self):
        if not self._cards_frame:
            return
        for widget in self._cards_frame.winfo_children():
            widget.destroy()
        visible_keys = {record.key for record in self._records}
        self._selected_keys.intersection_update(visible_keys)
        total_size = sum(record.size_bytes for record in self._records)
        selected_count = len(self._selected_keys)
        if self._stats_label:
            self._stats_label.configure(
                text=f"会话 {len(self._records)}  |  已选 {selected_count}  |  主文件 {session_migration.format_size(total_size)}"
            )

        if not self._records:
            EmptyState(
                self._cards_frame,
                "没有找到本机会话",
                "Claude Code 会话通常在 ~/.claude/projects，Codex CLI 会话通常在 ~/.codex/sessions。",
                "刷新",
                self.refresh,
            ).pack(fill="x", pady=(12, 4))
            return

        for record in self._records:
            self._add_record_card(record)

    def _add_record_card(self, record: session_migration.SessionRecord):
        card = ctk.CTkFrame(self._cards_frame, **card_frame_kwargs())
        card.pack(fill="x", pady=5)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        selected_var = ctk.BooleanVar(value=record.key in self._selected_keys)
        ctk.CTkCheckBox(
            top,
            text="",
            width=20,
            checkbox_width=18,
            checkbox_height=18,
            variable=selected_var,
            command=lambda key=record.key, var=selected_var: self._toggle_selected(key, var.get()),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(top, text=record.title, text_color=COLORS["text"], font=font(15, "bold")).pack(side="left")
        ctk.CTkLabel(
            top,
            text="Claude" if record.provider == "claude" else "Codex",
            fg_color=COLORS["primary"] if record.provider == "claude" else COLORS["accent"],
            corner_radius=4,
            text_color=COLORS["text"],
            font=font(11, "bold"),
            padx=7,
            pady=1,
        ).pack(side="left", padx=(8, 0))

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(fill="x", padx=14, pady=(0, 10))
        info_lines = [
            f"更新时间: {self._display_time(record.updated_at)}  |  消息数: {record.message_count}  |  大小: {session_migration.format_size(record.size_bytes)}",
            f"会话 ID: {record.session_id}",
            f"项目: {record.project_path or record.project_key or '(未知)'}",
            f"文件: {record.relative_path}",
        ]
        if record.model:
            info_lines.insert(1, f"模型/来源: {record.model}")
        if record.summary and record.summary != record.title:
            info_lines.append(f"摘要: {record.summary}")

        for line in info_lines:
            label = ctk.CTkLabel(
                info_frame,
                text=line,
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
                justify="left",
            )
            label.pack(fill="x")
            bind_wraplength(info_frame, label, padding=4)

    def _on_filter_change(self, label: str):
        self._provider_filter = self.FILTER_OPTIONS.get(label, "all")
        self.refresh()

    def _toggle_selected(self, key: str, selected: bool):
        if selected:
            self._selected_keys.add(key)
        else:
            self._selected_keys.discard(key)
        self._render_records()

    def _select_visible(self):
        self._selected_keys.update(record.key for record in self._records)
        self._render_records()

    def _clear_selection(self):
        self._selected_keys.clear()
        self._render_records()

    def _export_selected(self):
        if not self._selected_keys:
            show_toast(self.winfo_toplevel(), "请先选择要导出的会话", is_error=True)
            return
        output_path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="导出会话迁移包",
            defaultextension=session_migration.PACKAGE_EXTENSION,
            filetypes=[
                ("API切换器会话迁移包", f"*{session_migration.PACKAGE_EXTENSION}"),
                ("所有文件", "*.*"),
            ],
        )
        if not output_path:
            return
        try:
            result = session_migration.export_sessions(output_path, self._selected_keys)
            message = (
                f"会话迁移包已导出: {result.session_count} 个会话, "
                f"{result.file_count} 个文件, {session_migration.format_size(result.total_bytes)}"
            )
            if result.skipped_keys:
                message += f"，{len(result.skipped_keys)} 个会话未找到"
            show_toast(self.winfo_toplevel(), message)
        except Exception as exc:
            show_toast(self.winfo_toplevel(), f"导出失败: {exc}", is_error=True)

    def _import_package(self):
        input_path = self._choose_package()
        if not input_path:
            return

        def do_import():
            try:
                result = session_migration.import_sessions(input_path, overwrite=False)
                self._show_import_result(result)
            except Exception as exc:
                show_toast(self.winfo_toplevel(), f"导入失败: {exc}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="确认导入会话",
            message=(
                self._package_summary_text(input_path)
                + "\n\n导入会把迁移包中的 Claude/Codex 会话写入本机对应历史目录；已有文件默认跳过，不会覆盖。"
            ),
            on_confirm=do_import,
        )

    def _import_package_to_project(self):
        input_path = self._choose_package()
        if not input_path:
            return
        target_project = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            title="选择新机器上的项目目录",
        )
        if not target_project:
            return

        def do_import():
            try:
                result = session_migration.import_sessions(
                    input_path,
                    overwrite=False,
                    target_project_path=target_project,
                )
                self._show_import_result(result)
            except Exception as exc:
                show_toast(self.winfo_toplevel(), f"导入失败: {exc}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="确认导入并重映射项目",
            message=(
                self._package_summary_text(input_path)
                + f"\n\n会话中的 cwd 会改写为:\n{target_project}\n\n"
                "Claude 会话也会写入该项目对应的 projects 目录；已有文件默认跳过，不会覆盖。"
            ),
            on_confirm=do_import,
        )

    def _choose_package(self):
        return filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="导入会话迁移包",
            filetypes=[
                ("API切换器会话迁移包", f"*{session_migration.PACKAGE_EXTENSION}"),
                ("所有文件", "*.*"),
            ],
        )

    def _package_summary_text(self, input_path: str) -> str:
        try:
            summary = session_migration.inspect_package(input_path)
        except Exception:
            return "会话迁移包摘要读取失败，但仍可尝试导入。"

        provider_text = ", ".join(f"{name}: {count}" for name, count in sorted(summary.providers.items()))
        lines = [
            f"迁移包包含 {summary.session_count} 个会话、{summary.file_count} 个文件、{session_migration.format_size(summary.total_bytes)}。",
        ]
        if provider_text:
            lines.append(f"来源: {provider_text}")
        if summary.project_paths:
            shown = summary.project_paths[:3]
            lines.append("原项目: " + " | ".join(shown))
            if len(summary.project_paths) > len(shown):
                lines.append(f"另有 {len(summary.project_paths) - len(shown)} 个项目路径")
        return "\n".join(lines)

    def _show_import_result(self, result: session_migration.SessionImportResult):
        message = f"会话迁移包已导入: {result.session_count} 个会话, {result.file_count} 个文件"
        if result.skipped_existing:
            message += f"，跳过已有文件 {result.skipped_existing} 个"
        if result.skipped_invalid:
            message += f"，跳过无效条目 {result.skipped_invalid} 个"
        show_toast(self.winfo_toplevel(), message)
        self.refresh()

    def _display_time(self, value: str) -> str:
        if not value:
            return "未知"
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return value[:19]
        return parsed.strftime("%Y-%m-%d %H:%M")
