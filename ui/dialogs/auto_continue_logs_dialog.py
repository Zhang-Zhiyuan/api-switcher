from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import customtkinter as ctk

from ui.theme import COLORS, button_style, card_frame_kwargs, center_window, combo_style, font, textbox_style
from ui.widgets.toast import show_toast

if TYPE_CHECKING:
    from core.auto_continue.diagnostics import AutoContinueLogEvent


def _load_auto_continue_events(provider: str, limit: int):
    from core.auto_continue.diagnostics import load_auto_continue_events

    return load_auto_continue_events(provider, limit)


def _format_auto_continue_diagnostics(provider: str, limit: int) -> str:
    from core.auto_continue.diagnostics import format_auto_continue_diagnostics

    return format_auto_continue_diagnostics(provider, limit)


class AutoContinueLogsDialog(ctk.CTkToplevel):
    """Recent auto-continue decision and recovery logs."""

    MAX_RENDERED_ROWS = 220

    def __init__(self, master, provider: str):
        super().__init__(master)
        self.provider = provider
        self.title(f"{provider} 自动续跑日志")
        self.geometry("1080x760")
        self.minsize(560, 480)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self._events: list[AutoContinueLogEvent] = []
        self._filtered_events: list[AutoContinueLogEvent] = []
        self._rendered_events: list[AutoContinueLogEvent] = []
        self._selected_index: int | None = None
        self._row_widgets: list[ctk.CTkFrame] = []
        self._diagnostics_text = ""
        self._refresh_generation = 0
        self._responsive_after_id = None
        self._responsive_state = None
        self._stat_cards = []

        self._build_ui()
        center_window(self, master)
        self.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._schedule_responsive_layout(delay_ms=0)
        self._refresh()

    def _build_ui(self):
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x", padx=18, pady=(18, 10))
        self._header.grid_columnconfigure(0, weight=1)

        self._title_area = ctk.CTkFrame(self._header, fg_color="transparent")
        self._title_area.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            self._title_area,
            text="自动续跑日志",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            self._title_area,
            text="按事件查看 Stop 决策、API 恢复、命中规则、次数、训练模板和 Git hash。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        self._header_actions = ctk.CTkFrame(self._header, fg_color="transparent")
        copy_diagnostics_button = ctk.CTkButton(
            self._header_actions,
            text="复制诊断",
            width=104,
            command=self._copy_diagnostics,
            **button_style("accent"),
        )
        copy_current_button = ctk.CTkButton(
            self._header_actions,
            text="复制当前",
            width=96,
            command=self._copy_selected_event,
            **button_style("secondary"),
        )
        refresh_button = ctk.CTkButton(
            self._header_actions,
            text="刷新",
            width=82,
            command=self._refresh,
            **button_style("secondary"),
        )
        self._header_action_buttons = [refresh_button, copy_current_button, copy_diagnostics_button]

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(controls, text="最近", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._limit_combo = ctk.CTkComboBox(
            controls,
            values=["50", "100", "200", "500"],
            width=90,
            command=lambda _value: self._refresh(),
            **combo_style(),
        )
        self._limit_combo.set("100")
        self._limit_combo.pack(side="left", padx=(8, 6))
        ctk.CTkLabel(controls, text="条", text_color=COLORS["muted"], font=font(12)).pack(side="left")

        ctk.CTkLabel(controls, text="筛选", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(18, 0))
        self._filter_combo = ctk.CTkComboBox(
            controls,
            values=["全部", "Stop", "API恢复", "block_stop", "allow_stop", "有 Git hash", "训练模板"],
            width=132,
            command=lambda _value: self._apply_filter(),
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.pack(side="left", padx=(8, 16))

        self._status_label = ctk.CTkLabel(
            controls,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        self._stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._stats_frame.pack(fill="x", padx=18, pady=(0, 10))
        self._total_value = self._summary_card(self._stats_frame, "事件", "0", COLORS["primary"])
        self._block_value = self._summary_card(self._stats_frame, "block_stop", "0", COLORS["success"])
        self._allow_value = self._summary_card(self._stats_frame, "allow_stop", "0", COLORS["warning"])
        self._recovery_value = self._summary_card(self._stats_frame, "API恢复", "0", COLORS["accent"])
        self._git_value = self._summary_card(self._stats_frame, "Git hash", "0", COLORS["secondary"])

        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self._body.pack_propagate(False)

        self._left_pane = ctk.CTkFrame(self._body, fg_color="transparent", width=390)
        self._left_pane.pack(side="left", fill="y", padx=(0, 12))
        self._left_pane.pack_propagate(False)
        ctk.CTkLabel(
            self._left_pane,
            text="最近事件",
            text_color=COLORS["text"],
            font=font(13, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        self._list_frame = ctk.CTkScrollableFrame(self._left_pane, fg_color="transparent")
        self._list_frame.pack(fill="both", expand=True)

        self._right_pane = ctk.CTkFrame(self._body, fg_color="transparent")
        self._right_pane.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(
            self._right_pane,
            text="事件详情",
            text_color=COLORS["text"],
            font=font(13, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        self._detail_text = ctk.CTkTextbox(self._right_pane, wrap="none", **textbox_style(monospace=True))
        self._detail_text.pack(fill="both", expand=True)

    def _summary_card(self, parent, title: str, value: str, color: str):
        card = ctk.CTkFrame(parent, **card_frame_kwargs())
        self._stat_cards.append(card)
        ctk.CTkLabel(card, text=title, text_color=COLORS["muted"], font=font(11)).pack(anchor="w", padx=10, pady=(8, 0))
        value_label = ctk.CTkLabel(card, text=value, text_color=color, font=font(18, "bold"))
        value_label.pack(anchor="w", padx=10, pady=(0, 8))
        return value_label

    def _logical_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_window_scaling())
        except (AttributeError, TypeError, ValueError):
            scaling = 1.0
        return max(1, round(width / scaling)) if scaling > 0 else max(1, width)

    def _schedule_responsive_layout(self, _event=None, delay_ms: int = 25) -> None:
        if self._responsive_after_id is not None:
            return

        def apply_layout():
            self._responsive_after_id = None
            try:
                if self.winfo_exists():
                    self._apply_responsive_layout()
            except Exception:
                pass

        try:
            self._responsive_after_id = self.after_idle(apply_layout) if delay_ms <= 0 else self.after(delay_ms, apply_layout)
        except Exception:
            self._responsive_after_id = None

    def _apply_responsive_layout(self) -> None:
        width = self._logical_width()
        stacked = width < 820
        stat_columns = 5 if width >= 820 else (3 if width >= 560 else 2)
        header_stacked = width < 760
        state = (stacked, stat_columns, header_stacked)
        if state == self._responsive_state:
            return
        self._responsive_state = state

        self._title_area.grid(row=0, column=0, sticky="ew")
        self._header_actions.grid(
            row=1 if header_stacked else 0,
            column=0 if header_stacked else 1,
            sticky="ew" if header_stacked else "e",
            padx=0 if header_stacked else (12, 0),
            pady=(8, 0) if header_stacked else 0,
        )
        for column in range(3):
            self._header_actions.grid_columnconfigure(column, weight=1, uniform="auto-log-actions")
        for index, button in enumerate(self._header_action_buttons):
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))

        for column in range(5):
            self._stats_frame.grid_columnconfigure(column, weight=0, minsize=0, uniform="")
        for column in range(stat_columns):
            self._stats_frame.grid_columnconfigure(column, weight=1, uniform="auto-log-stats")
        for index, card in enumerate(self._stat_cards):
            card.grid(
                row=index // stat_columns,
                column=index % stat_columns,
                sticky="ew",
                padx=(0 if index % stat_columns == 0 else 8, 0),
                pady=(0 if index < stat_columns else 8, 0),
            )

        self._left_pane.pack_forget()
        self._right_pane.pack_forget()
        if stacked:
            self._left_pane.configure(width=0, height=110)
            self._left_pane.pack(side="top", fill="x", pady=(0, 10))
            self._right_pane.pack(side="top", fill="both", expand=True)
        else:
            self._left_pane.configure(width=390, height=0)
            self._left_pane.pack(side="left", fill="y", padx=(0, 12))
            self._right_pane.pack(side="left", fill="both", expand=True)

    def _limit(self) -> int:
        try:
            return max(1, min(1000, int(self._limit_combo.get())))
        except Exception:
            return 100

    def _set_detail(self, text: str):
        self._detail_text.configure(state="normal")
        self._detail_text.delete("1.0", "end")
        self._detail_text.insert("1.0", text)
        self._detail_text.configure(state="disabled")

    def _update_stats(self):
        block_count = sum(1 for event in self._events if event.decision == "block_stop")
        allow_count = sum(1 for event in self._events if event.decision == "allow_stop")
        recovery_count = sum(1 for event in self._events if event.source == "API恢复")
        git_count = sum(1 for event in self._events if event.git_commit_hash)
        self._total_value.configure(text=str(len(self._events)))
        self._block_value.configure(text=str(block_count))
        self._allow_value.configure(text=str(allow_count))
        self._recovery_value.configure(text=str(recovery_count))
        self._git_value.configure(text=str(git_count))

    def _refresh(self):
        self._refresh_generation += 1
        generation = self._refresh_generation
        limit = self._limit()
        self._status_label.configure(text="正在后台读取自动续跑日志...", text_color=COLORS["muted"])
        self._set_detail("正在后台读取自动续跑日志，请稍候...")

        def worker():
            try:
                payload = {
                    "ok": True,
                    "events": _load_auto_continue_events(self.provider, limit),
                    "diagnostics": _format_auto_continue_diagnostics(self.provider, limit),
                    "error": "",
                }
            except Exception as exc:
                payload = {"ok": False, "events": [], "diagnostics": f"读取失败: {exc}", "error": str(exc)}

            def finish():
                try:
                    if generation != self._refresh_generation or not self.winfo_exists():
                        return
                    self._apply_refresh_payload(payload)
                except Exception as exc:
                    self._apply_refresh_error(str(exc))

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name=f"auto-continue-logs-{self.provider}", daemon=True).start()

    def _apply_refresh_payload(self, payload: dict):
        if not payload.get("ok"):
            self._apply_refresh_error(str(payload.get("error") or "读取失败"))
            return

        self._events = list(payload.get("events") or [])
        self._diagnostics_text = str(payload.get("diagnostics") or "")
        self._update_stats()
        self._apply_filter()
        self._status_label.configure(
            text=f"{self.provider} 日志已刷新，当前筛选 {len(self._filtered_events)} 条",
            text_color=COLORS["muted"],
        )

    def _apply_refresh_error(self, error: str):
        self._events = []
        self._filtered_events = []
        self._rendered_events = []
        self._diagnostics_text = f"读取失败: {error}"
        self._render_events()
        self._set_detail(f"读取失败: {error}")
        self._status_label.configure(text="读取失败", text_color=COLORS["danger"])

    def _apply_filter(self):
        selected = self._filter_combo.get()
        if selected == "Stop":
            self._filtered_events = [event for event in self._events if event.source == "Stop"]
        elif selected == "API恢复":
            self._filtered_events = [event for event in self._events if event.source == "API恢复"]
        elif selected == "block_stop":
            self._filtered_events = [event for event in self._events if event.decision == "block_stop"]
        elif selected == "allow_stop":
            self._filtered_events = [event for event in self._events if event.decision == "allow_stop"]
        elif selected == "有 Git hash":
            self._filtered_events = [event for event in self._events if event.git_commit_hash]
        elif selected == "训练模板":
            self._filtered_events = [event for event in self._events if event.training_template]
        else:
            self._filtered_events = list(self._events)

        self._selected_index = 0 if self._filtered_events else None
        self._render_events()
        if self._selected_index is not None:
            self._select_event(self._selected_index)
        else:
            self._set_detail("没有匹配的自动续跑日志。")
        rendered_count = min(len(self._filtered_events), self.MAX_RENDERED_ROWS)
        render_note = (
            f"，列表渲染最近 {rendered_count} 条"
            if len(self._filtered_events) > rendered_count
            else ""
        )
        self._status_label.configure(
            text=f"{self.provider} 当前筛选 {len(self._filtered_events)} / {len(self._events)} 条{render_note}",
            text_color=COLORS["muted"] if self._filtered_events or not self._events else COLORS["warning"],
        )

    def _event_color(self, event: AutoContinueLogEvent) -> str:
        if event.decision == "block_stop":
            return COLORS["success"]
        if event.decision == "allow_stop":
            return COLORS["warning"]
        if event.source == "API恢复":
            return COLORS["accent"]
        return COLORS["muted"]

    def _event_title(self, event: AutoContinueLogEvent) -> str:
        label = event.decision or event.source
        reason = event.reason or event.match or "-"
        return f"{label}  ·  {reason}"

    def _event_subtitle(self, event: AutoContinueLogEvent) -> str:
        parts = [event.timestamp or "-", event.hook_event or event.source]
        if event.count not in ("", None, -1):
            parts.append(f"续跑 {event.count}")
        if event.recovery_count not in ("", None):
            parts.append(f"恢复 {event.recovery_count}")
        if event.git_commit_hash:
            parts.append(f"Git {event.git_commit_hash}")
        return "  |  ".join(str(part) for part in parts if part)

    def _bind_row_click(self, widget, index: int):
        try:
            widget.bind("<Button-1>", lambda _event, i=index: self._select_event(i))
        except Exception:
            pass

    def _render_events(self):
        for child in self._list_frame.winfo_children():
            child.destroy()
        self._row_widgets = []
        self._rendered_events = self._filtered_events[:self.MAX_RENDERED_ROWS]

        if not self._filtered_events:
            ctk.CTkLabel(
                self._list_frame,
                text="没有匹配的日志",
                text_color=COLORS["muted"],
                font=font(12),
            ).pack(anchor="w", padx=6, pady=8)
            return

        for index, event in enumerate(self._rendered_events):
            row = ctk.CTkFrame(
                self._list_frame,
                corner_radius=8,
                fg_color=COLORS["surface"],
                border_width=1,
                border_color=COLORS["border_soft"],
            )
            row.pack(fill="x", pady=(0, 7), padx=(0, 4))
            self._bind_row_click(row, index)

            top = ctk.CTkFrame(row, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(8, 0))
            self._bind_row_click(top, index)
            ctk.CTkLabel(
                top,
                text=event.source,
                text_color=self._event_color(event),
                font=font(11, "bold"),
                width=54,
                anchor="w",
            ).pack(side="left")
            title = ctk.CTkLabel(
                top,
                text=self._event_title(event),
                text_color=COLORS["text"],
                font=font(12, "bold"),
                anchor="w",
            )
            title.pack(side="left", fill="x", expand=True)
            self._bind_row_click(title, index)

            subtitle = ctk.CTkLabel(
                row,
                text=self._event_subtitle(event),
                text_color=COLORS["muted"],
                font=font(11),
                anchor="w",
                justify="left",
            )
            subtitle.pack(fill="x", padx=10, pady=(3, 8))
            self._bind_row_click(subtitle, index)

            self._row_widgets.append(row)

    def _event_detail(self, event: AutoContinueLogEvent) -> str:
        lines = [
            f"时间: {event.timestamp or '-'}",
            f"Provider: {event.provider}",
            f"来源: {event.source}",
            f"Hook: {event.hook_event or '-'}",
            f"Decision: {event.decision or '-'}",
            f"Reason: {event.reason or '-'}",
            f"命中规则: {event.match or '-'}",
            f"续跑次数: {event.count if event.count not in ('', None, -1) else '-'}",
            f"API 恢复次数: {event.recovery_count if event.recovery_count not in ('', None) else '-'}",
            f"训练模板: {event.training_template or '-'}",
            f"Git commit hash: {event.git_commit_hash or '-'}",
            f"Session: {event.session_id or '-'}",
            "",
            "摘要:",
            event.excerpt or "-",
            "",
            "Raw JSON:",
            json.dumps(event.raw or {}, ensure_ascii=False, indent=2),
        ]
        return "\n".join(str(line) for line in lines)

    def _select_event(self, index: int):
        if index < 0 or index >= len(self._filtered_events):
            return
        self._selected_index = index
        for row_index, row in enumerate(self._row_widgets):
            selected = row_index == index
            row.configure(
                border_color=COLORS["primary"] if selected else COLORS["border_soft"],
                fg_color=COLORS["surface_alt"] if selected else COLORS["surface"],
            )
        self._set_detail(self._event_detail(self._filtered_events[index]))

    def _copy_diagnostics(self):
        text = self._diagnostics_text or _format_auto_continue_diagnostics(self.provider, self._limit())
        self.clipboard_clear()
        self.clipboard_append(text)
        show_toast(self, "自动续跑诊断信息已复制")

    def _copy_selected_event(self):
        if self._selected_index is None or self._selected_index >= len(self._filtered_events):
            show_toast(self, "没有可复制的事件", is_error=True)
            return
        self.clipboard_clear()
        self.clipboard_append(self._event_detail(self._filtered_events[self._selected_index]))
        show_toast(self, "当前事件详情已复制")
