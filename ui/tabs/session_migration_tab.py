from __future__ import annotations

import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from core.lazy_imports import LazyModule
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.tabs.tab_visibility import is_active_tab
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font, recent_user_scroll
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast

logger = logging.getLogger(__name__)


profile_manager = LazyModule("core.profile_manager")
session_migration = LazyModule("core.session_migration")


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _session_record_summary(records, selected_keys: set[str]) -> dict:
    visible_keys = {record.key for record in records}
    selected_count = sum(1 for key in selected_keys if key in visible_keys)
    total_size = sum(_nonnegative_int(getattr(record, "size_bytes", 0)) for record in records)
    return {
        "visible_keys": visible_keys,
        "selected_count": selected_count,
        "total_size": total_size,
    }


class SessionMigrationTab(ctk.CTkScrollableFrame):
    """Tab for exporting and importing Claude Code / Codex local sessions."""

    RENDER_BATCH_SIZE = 1
    RENDER_BATCH_DELAY_MS = 90
    INITIAL_REFRESH_DELAY_MS = 900
    SCROLL_IDLE_RENDER_MS = 850
    SCROLL_RETRY_RENDER_MS = 260
    MAX_VISIBLE_RECORDS = 3
    VISIBLE_RECORDS_STEP = 12

    FILTER_OPTIONS = {
        "全部": "all",
        "Claude Code": "claude",
        "Codex CLI": "codex",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._destroyed = False
        self._ui_dispatch = self._resolve_ui_dispatch()
        self._cards_frame = None
        self._stats_label = None
        self._filter_combo = None
        self._source_location_combo = None
        self._target_location_combo = None
        self._location_options: dict[str, str] = {"本机": ""}
        self._provider_filter = "all"
        self._records: list[session_migration.SessionRecord] = []
        self._selected_keys: set[str] = set()
        self._refresh_generation = 0
        self._record_render_generation = 0
        self._record_render_after_id = None
        self._deferred_render_after_id = None
        self._initial_refresh_after_id = None
        self._inactive_clear_after_id = None
        self._deferred_refresh_pending = False
        self._deferred_render_pending = False
        self._visible_limit = self.MAX_VISIBLE_RECORDS
        self._build_ui()

    def destroy(self):
        self._destroyed = True
        self._cancel_record_render()
        self._cancel_deferred_render()
        self._cancel_initial_refresh()
        self._cancel_inactive_clear()
        super().destroy()

    def _resolve_ui_dispatch(self):
        try:
            dispatch = getattr(self.winfo_toplevel(), "_run_on_ui_thread", None)
        except Exception:
            return None
        return dispatch if callable(dispatch) else None

    def _run_on_ui_thread(self, callback):
        if getattr(self, "_destroyed", False):
            return
        dispatch = getattr(self, "_ui_dispatch", None)
        if callable(dispatch):
            dispatch(callback)
            return
        try:
            self.after(0, callback)
        except Exception:
            logger.exception("Failed to schedule session migration UI callback")

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
            text="读取本机或 SSH 服务器上的 Claude Code / Codex CLI 历史会话，导出迁移包并导入到本机或其他 SSH 服务器。",
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
            text="导入到目标项目",
            width=128,
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
            text="迁移到目标",
            width=112,
            command=self._transfer_selected_to_target,
            **button_style("primary"),
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
        location_values = list(self._location_options.keys())
        ctk.CTkLabel(filter_bar, text="读取位置", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(12, 0), pady=9)
        self._source_location_combo = ctk.CTkComboBox(
            filter_bar,
            values=location_values,
            width=150,
            command=self._on_source_location_change,
            **combo_style(),
        )
        self._source_location_combo.set(location_values[0])
        self._source_location_combo.pack(side="left", padx=(8, 0), pady=9)

        ctk.CTkLabel(filter_bar, text="导入目标", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(12, 0), pady=9)
        self._target_location_combo = ctk.CTkComboBox(
            filter_bar,
            values=location_values,
            width=150,
            **combo_style(),
        )
        self._target_location_combo.set(location_values[0])
        self._target_location_combo.pack(side="left", padx=(8, 0), pady=9)

        ctk.CTkLabel(filter_bar, text="会话类型", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(12, 0), pady=9)
        self._filter_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.FILTER_OPTIONS.keys()),
            width=120,
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
            text="会话迁移包会包含完整对话内容和工具记录，可能包含敏感信息；导入本机或 SSH 服务器只迁移历史会话，不迁移账号登录态或 API Key。",
            text_color=COLORS["warning"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        warning.pack(fill="x", padx=14, pady=(0, 8))
        bind_wraplength(self, warning, padding=42, min_width=260, max_width=900)

        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        ctk.CTkLabel(
            self._cards_frame,
            text="会话列表稍后读取...",
            text_color=COLORS["muted"],
            font=font(13),
        ).pack(fill="x", pady=(22, 6))

        self._schedule_initial_refresh()

    def _schedule_initial_refresh(self):
        if self._initial_refresh_after_id or getattr(self, "_destroyed", False):
            return
        try:
            self._initial_refresh_after_id = self.after(self.INITIAL_REFRESH_DELAY_MS, self.refresh)
        except Exception:
            self._initial_refresh_after_id = None
            self.refresh()

    def refresh(self):
        self._initial_refresh_after_id = None
        if getattr(self, "_destroyed", False):
            return
        if not is_active_tab(self):
            self._deferred_refresh_pending = True
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_RENDER_MS):
            self._deferred_refresh_pending = True
            self._schedule_initial_refresh()
            return
        self._deferred_refresh_pending = False
        if not self._cards_frame:
            return
        self._cancel_inactive_clear()
        self._refresh_location_options()
        self._visible_limit = self.MAX_VISIBLE_RECORDS
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._cancel_record_render()
        self._cancel_deferred_render()
        provider_filter = self._provider_filter
        source_ssh_name = self._current_source_ssh_name()
        source_label = self._endpoint_label(source_ssh_name)
        for widget in self._cards_frame.winfo_children():
            widget.destroy()

        if self._stats_label:
            self._stats_label.configure(text=f"正在读取{source_label}会话...")
        ctk.CTkLabel(
            self._cards_frame,
            text=f"正在读取{source_label}会话...",
            text_color=COLORS["muted"],
            font=font(13),
        ).pack(fill="x", pady=(22, 6))

        def worker():
            try:
                payload = {
                    "records": session_migration.list_sessions(provider_filter, ssh_name=source_ssh_name),
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

            self._run_on_ui_thread(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _render_records(self):
        if not self._cards_frame:
            return
        if not is_active_tab(self):
            self._deferred_render_pending = True
            return
        self._cancel_record_render()
        self._record_render_generation += 1
        generation = self._record_render_generation
        for widget in self._cards_frame.winfo_children():
            widget.destroy()
        records = list(self._records)
        summary = _session_record_summary(records, self._selected_keys)
        self._selected_keys.intersection_update(summary["visible_keys"])
        self._update_stats_label(records)

        if not records:
            source_label = self._endpoint_label(self._current_source_ssh_name()).strip()
            EmptyState(
                self._cards_frame,
                f"没有找到{source_label}会话",
                f"{source_label}上的 Claude Code 会话通常在 ~/.claude/projects，Codex CLI 会话通常在 ~/.codex/sessions。",
                "刷新",
                self.refresh,
            ).pack(fill="x", pady=(12, 4))
            return

        visible_limit = max(self.MAX_VISIBLE_RECORDS, int(self._visible_limit or self.MAX_VISIBLE_RECORDS))
        visible_records = records[:visible_limit]
        self._schedule_record_batch(visible_records, generation, 0, total_count=len(records), delay_ms=45)

    def _schedule_record_batch(
        self,
        records: list[session_migration.SessionRecord],
        generation: int,
        start: int,
        total_count: int | None = None,
        delay_ms: int | None = None,
    ):
        if self._record_render_after_id:
            return

        def run():
            self._record_render_after_id = None
            self._render_record_batch(records, generation, start, total_count=total_count)

        try:
            self._record_render_after_id = self.after(
                self.RENDER_BATCH_DELAY_MS if delay_ms is None else max(1, int(delay_ms)),
                run,
            )
        except Exception:
            self._record_render_after_id = None
            self._render_record_batch(records, generation, start, total_count=total_count)

    def _update_stats_label(self, records=None):
        if not self._stats_label:
            return
        records = list(self._records if records is None else records)
        summary = _session_record_summary(records, self._selected_keys)
        self._stats_label.configure(
            text=(
                f"会话 {len(records)}  |  已选 {summary['selected_count']}  |  "
                f"主文件 {session_migration.format_size(summary['total_size'])}"
            )
        )

    def _cancel_record_render(self):
        if not self._record_render_after_id:
            return
        try:
            self.after_cancel(self._record_render_after_id)
        except Exception:
            pass
        self._record_render_after_id = None

    def _cancel_deferred_render(self):
        if not self._deferred_render_after_id:
            return
        try:
            self.after_cancel(self._deferred_render_after_id)
        except Exception:
            pass
        self._deferred_render_after_id = None

    def _cancel_initial_refresh(self):
        if not self._initial_refresh_after_id:
            return
        try:
            self.after_cancel(self._initial_refresh_after_id)
        except Exception:
            pass
        self._initial_refresh_after_id = None

    def _cancel_inactive_clear(self):
        if not self._inactive_clear_after_id:
            return
        try:
            self.after_cancel(self._inactive_clear_after_id)
        except Exception:
            pass
        self._inactive_clear_after_id = None

    def _schedule_inactive_clear(self, delay_ms: int = 160):
        if self._inactive_clear_after_id or getattr(self, "_destroyed", False):
            return

        def run():
            self._inactive_clear_after_id = None
            if getattr(self, "_destroyed", False) or is_active_tab(self):
                return
            frame = self._cards_frame
            if frame is None:
                return
            children = list(frame.winfo_children())
            if not children:
                return
            for child in children:
                try:
                    child.destroy()
                except Exception:
                    pass
            if self._records:
                self._deferred_render_pending = True

        try:
            self._inactive_clear_after_id = self.after(max(1, int(delay_ms)), run)
        except Exception:
            self._inactive_clear_after_id = None

    def _schedule_deferred_render(self, delay_ms: int = 1):
        if self._deferred_render_after_id or getattr(self, "_destroyed", False):
            return
        self._cancel_inactive_clear()

        def run():
            self._deferred_render_after_id = None
            if getattr(self, "_destroyed", False):
                return
            if not is_active_tab(self):
                self._deferred_render_pending = True
                return
            self._deferred_render_pending = False
            self._render_records()

        try:
            self._deferred_render_after_id = self.after(max(1, int(delay_ms)), run)
        except Exception:
            self._deferred_render_after_id = None
            if not getattr(self, "_destroyed", False):
                self._deferred_render_pending = False
                self._render_records()

    def _suspend_background_work(self):
        if self._initial_refresh_after_id:
            self._deferred_refresh_pending = True
            self._cancel_initial_refresh()
        if self._record_render_after_id:
            self._deferred_render_pending = True
            self._cancel_record_render()
        self._schedule_inactive_clear()

    def _resume_background_work(self):
        self._cancel_inactive_clear()
        if self._deferred_refresh_pending:
            self._deferred_refresh_pending = False
            self._schedule_initial_refresh()
        if not self._deferred_render_pending:
            return
        self._schedule_deferred_render()

    def _render_record_batch(
        self,
        records: list[session_migration.SessionRecord],
        generation: int,
        start: int,
        total_count: int | None = None,
    ):
        if generation != self._record_render_generation or not self._cards_frame:
            return
        if not is_active_tab(self):
            self._deferred_render_pending = True
            self._record_render_after_id = None
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_RENDER_MS):
            self._schedule_record_batch(records, generation, start, total_count=total_count)
            return
        end = min(start + self.RENDER_BATCH_SIZE, len(records))
        for record in records[start:end]:
            self._add_record_card(record)
        if end >= len(records):
            self._record_render_after_id = None
            total = len(records) if total_count is None else int(total_count)
            remaining = max(0, total - len(records))
            if remaining:
                self._add_show_more_footer(remaining)
            return
        self._schedule_record_batch(records, generation, end, total_count=total_count)

    def _add_show_more_footer(self, remaining: int):
        footer = ctk.CTkFrame(self._cards_frame, **card_frame_kwargs())
        footer.pack(fill="x", pady=(4, 10))
        ctk.CTkLabel(
            footer,
            text=f"还有 {remaining} 个会话未显示",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
        ).pack(side="left", padx=14, pady=12)
        ctk.CTkButton(
            footer,
            text="显示更多",
            width=92,
            command=self._show_more_records,
            **button_style("secondary", compact=True),
        ).pack(side="right", padx=14, pady=10)

    def _show_more_records(self):
        self._visible_limit = max(
            self.MAX_VISIBLE_RECORDS,
            int(self._visible_limit or self.MAX_VISIBLE_RECORDS) + self.VISIBLE_RECORDS_STEP,
        )
        self._render_records()

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
        ctk.CTkLabel(
            top,
            text="Claude" if record.provider == "claude" else "Codex",
            fg_color=COLORS["primary"] if record.provider == "claude" else COLORS["accent"],
            corner_radius=4,
            text_color=COLORS["text"],
            font=font(11, "bold"),
            padx=7,
            pady=1,
        ).pack(side="right", padx=(8, 0))
        title_label = ctk.CTkLabel(
            top,
            text=record.title,
            text_color=COLORS["text"],
            font=font(15, "bold"),
            anchor="w",
            justify="left",
            wraplength=780,
        )
        title_label.pack(side="left", fill="x", expand=True)

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(fill="x", padx=14, pady=(0, 10))
        info_lines = [
            f"位置: {'SSH ' + record.ssh_name if record.origin == 'ssh' else '本机'}",
            f"更新时间: {self._display_time(record.updated_at)}  |  消息数: {record.message_count}  |  大小: {session_migration.format_size(record.size_bytes)}",
            f"会话 ID: {record.session_id}",
            f"项目: {record.project_path or record.project_key or '(未知)'}",
            f"文件: {record.relative_path}",
        ]
        if record.model:
            info_lines.insert(1, f"模型/来源: {record.model}")
        if record.summary and record.summary != record.title:
            info_lines.append(f"摘要: {record.summary}")

        info_label = ctk.CTkLabel(
            info_frame,
            text="\n".join(info_lines),
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
            wraplength=900,
        )
        info_label.pack(fill="x")

    def _on_filter_change(self, label: str):
        self._provider_filter = self.FILTER_OPTIONS.get(label, "all")
        self._visible_limit = self.MAX_VISIBLE_RECORDS
        self.refresh()

    def _on_source_location_change(self, _label: str):
        self._selected_keys.clear()
        self._visible_limit = self.MAX_VISIBLE_RECORDS
        self.refresh()

    def _toggle_selected(self, key: str, selected: bool):
        if selected:
            self._selected_keys.add(key)
        else:
            self._selected_keys.discard(key)
        self._update_stats_label()

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
            source_ssh_name = self._current_source_ssh_name()
            result = self._export_current_selection_to_package(output_path, source_ssh_name)
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
        target_ssh_name = self._current_target_ssh_name()
        target_label = self._endpoint_label(target_ssh_name)

        def do_import():
            try:
                result = self._import_package_to_endpoint(input_path, target_ssh_name)
                self._show_import_result(result)
            except Exception as exc:
                show_toast(self.winfo_toplevel(), f"导入失败: {exc}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="确认导入会话",
            message=(
                self._package_summary_text(input_path)
                + f"\n\n导入会把迁移包中的 Claude/Codex 会话写入{target_label}对应历史目录；已有文件默认跳过，不会覆盖。"
            ),
            on_confirm=do_import,
        )

    def _import_package_to_project(self):
        input_path = self._choose_package()
        if not input_path:
            return
        target_ssh_name = self._current_target_ssh_name()
        target_label = self._endpoint_label(target_ssh_name)
        if target_ssh_name:
            dialog = ctk.CTkInputDialog(
                title="输入远端项目目录",
                text=f"请输入 {target_label} 上的新项目目录，例如 /home/user/project",
            )
            target_project = (dialog.get_input() or "").strip()
        else:
            target_project = filedialog.askdirectory(
                parent=self.winfo_toplevel(),
                title="选择新机器上的项目目录",
            )
        if not target_project:
            return

        def do_import():
            try:
                result = self._import_package_to_endpoint(input_path, target_ssh_name, target_project)
                self._show_import_result(result)
            except Exception as exc:
                show_toast(self.winfo_toplevel(), f"导入失败: {exc}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="确认导入并重映射项目",
            message=(
                self._package_summary_text(input_path)
                + f"\n\n会话会导入到{target_label}；会话中的 cwd 会改写为:\n{target_project}\n\n"
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

    def _transfer_selected_to_target(self):
        if not self._selected_keys:
            show_toast(self.winfo_toplevel(), "请先选择要迁移的会话", is_error=True)
            return
        source_ssh_name = self._current_source_ssh_name()
        target_ssh_name = self._current_target_ssh_name()
        source_label = self._endpoint_label(source_ssh_name)
        target_label = self._endpoint_label(target_ssh_name)
        if source_ssh_name == target_ssh_name:
            show_toast(self.winfo_toplevel(), "读取位置和导入目标相同，请选择不同目标", is_error=True)
            return

        def do_transfer():
            self._run_transfer_task(source_ssh_name, target_ssh_name)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="迁移选中会话",
            message=(
                f"将把已选 {len(self._selected_keys)} 个会话从{source_label}迁移到{target_label}。\n"
                "会先生成临时会话包再导入目标；已有文件默认跳过，不会覆盖。"
            ),
            on_confirm=do_transfer,
        )

    def _run_transfer_task(self, source_ssh_name: str, target_ssh_name: str):
        selected_keys = set(self._selected_keys)
        provider_filter = self._provider_filter
        if self._stats_label:
            self._stats_label.configure(text="正在迁移选中会话...")

        def worker():
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=session_migration.PACKAGE_EXTENSION) as handle:
                    temp_path = Path(handle.name)
                exported = self._export_current_selection_to_package(
                    temp_path,
                    source_ssh_name,
                    selected_keys=selected_keys,
                    provider_filter=provider_filter,
                )
                imported = self._import_package_to_endpoint(temp_path, target_ssh_name)
                payload = {"ok": True, "exported": exported, "imported": imported, "error": None}
            except Exception as exc:
                payload = {"ok": False, "exported": None, "imported": None, "error": str(exc)}
            finally:
                if temp_path:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

            def finish():
                if not self.winfo_exists():
                    return
                if not payload["ok"]:
                    show_toast(self.winfo_toplevel(), f"迁移失败: {payload['error']}", is_error=True)
                    self.refresh()
                    return
                imported = payload["imported"]
                message = f"会话已迁移到目标: {imported.session_count} 个会话, {imported.file_count} 个文件"
                if imported.skipped_existing:
                    message += f"，跳过已有文件 {imported.skipped_existing} 个"
                if imported.skipped_invalid:
                    message += f"，跳过无效条目 {imported.skipped_invalid} 个"
                show_toast(self.winfo_toplevel(), message)
                self.refresh()

            self._run_on_ui_thread(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _export_current_selection_to_package(
        self,
        output_path,
        source_ssh_name: str,
        selected_keys: set[str] | None = None,
        provider_filter: str | None = None,
    ):
        keys = selected_keys or self._selected_keys
        provider = provider_filter or self._provider_filter
        if source_ssh_name:
            return session_migration.export_remote_sessions(
                source_ssh_name,
                output_path,
                keys,
                provider=provider,
            )
        return session_migration.export_sessions(output_path, keys)

    @staticmethod
    def _import_package_to_endpoint(input_path, target_ssh_name: str, target_project_path: str | None = None):
        if target_ssh_name:
            return session_migration.import_sessions_to_ssh(
                target_ssh_name,
                input_path,
                overwrite=False,
                target_project_path=target_project_path,
            )
        return session_migration.import_sessions(
            input_path,
            overwrite=False,
            target_project_path=target_project_path,
        )

    def _refresh_location_options(self):
        current_source = self._source_location_combo.get() if self._source_location_combo else "本机"
        current_target = self._target_location_combo.get() if self._target_location_combo else "本机"
        options = {"本机": ""}
        try:
            for profile in profile_manager.list_ssh_profiles():
                options[f"SSH: {profile.name}"] = profile.name
        except Exception:
            pass
        self._location_options = options
        values = list(options.keys())
        for combo, current in ((self._source_location_combo, current_source), (self._target_location_combo, current_target)):
            if not combo:
                continue
            try:
                combo.configure(values=values)
                combo.set(current if current in options else "本机")
            except Exception:
                pass

    def _current_source_ssh_name(self) -> str:
        label = self._source_location_combo.get() if self._source_location_combo else "本机"
        return self._location_options.get(label, "")

    def _current_target_ssh_name(self) -> str:
        label = self._target_location_combo.get() if self._target_location_combo else "本机"
        return self._location_options.get(label, "")

    @staticmethod
    def _endpoint_label(ssh_name: str | None) -> str:
        return f" SSH: {ssh_name} " if ssh_name else "本机"

    def _display_time(self, value: str) -> str:
        if not value:
            return "未知"
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return value[:19]
        return parsed.strftime("%Y-%m-%d %H:%M")
