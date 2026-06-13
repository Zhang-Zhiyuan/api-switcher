"""Usage statistics dashboard tab."""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
import customtkinter as ctk
from ui.theme import COLORS, button_style, combo_style, font
from core.usage_stats import usage_stats, format_token_count
from ui.widgets.toast import show_toast

logger = logging.getLogger(__name__)


def _summary_int(summary: dict, key: str) -> int:
    try:
        return max(0, int(summary.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _summary_success_rate_text(summary: dict) -> str:
    successes = _summary_int(summary, "total_successes")
    total_ops = _summary_int(summary, "total_errors") + successes
    if total_ops <= 0:
        return "N/A"
    return f"{(successes / total_ops) * 100:.1f}%"


class UsageStatsTab(ctk.CTkScrollableFrame):
    """Usage statistics dashboard tab."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._auto_refresh_after_id = None
        self._refresh_generation = 0
        self._build_ui()
        self.after(20, self.refresh)

        # Auto-refresh every 30 seconds
        self._schedule_auto_refresh()

    def _auto_refresh(self):
        """Auto-refresh stats."""
        self._auto_refresh_after_id = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self.refresh()
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self):
        self._cancel_auto_refresh()
        try:
            self._auto_refresh_after_id = self.after(30000, self._auto_refresh)
        except Exception:
            self._auto_refresh_after_id = None

    def _cancel_auto_refresh(self):
        if not self._auto_refresh_after_id:
            return
        try:
            self.after_cancel(self._auto_refresh_after_id)
        except Exception:
            pass
        self._auto_refresh_after_id = None

    def destroy(self):
        self._cancel_auto_refresh()
        super().destroy()

    def _build_ui(self):
        """Build the UI."""
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            title_area,
            text="使用统计仪表板",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_area,
            text="查看配置使用情况、Token 消耗和性能统计",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        # Controls
        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.pack(side="right")

        # Date range filter
        ctk.CTkLabel(controls, text="时间:", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(0, 5))

        self.date_filter = ctk.CTkComboBox(
            controls,
            values=["全部时间", "今日", "本周", "本月", "最近7天", "最近30天"],
            width=110,
            command=lambda _: self.refresh(),
            **combo_style(),
        )
        self.date_filter.pack(side="left", padx=5)
        self.date_filter.set("全部时间")

        # Type filter
        ctk.CTkLabel(controls, text="类型:", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(10, 5))

        self.type_filter = ctk.CTkComboBox(
            controls,
            values=["全部", "Claude", "Codex"],
            width=100,
            command=lambda _: self.refresh(),
            **combo_style(),
        )
        self.type_filter.pack(side="left", padx=5)
        self.type_filter.set("全部")

        ctk.CTkButton(
            controls,
            text="刷新",
            width=80,
            command=self.refresh,
            **button_style("secondary"),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            controls,
            text="清零",
            width=70,
            command=self._clear_stats,
            **button_style("danger"),
        ).pack(side="left", padx=5)

        # Summary cards
        summary_frame = ctk.CTkFrame(self, fg_color="transparent")
        summary_frame.pack(fill="x", padx=14, pady=(0, 10))

        self.summary_cards = {}
        cards_data = [
            ("total_profiles", "配置总数", "0"),
            ("total_switches", "切换次数", "0"),
            ("total_tokens", "Token 总量", "0"),
            ("success_rate", "成功率", "0%"),
        ]

        for i, (key, title, default) in enumerate(cards_data):
            card = self._create_summary_card(summary_frame, title, default)
            card.grid(row=0, column=i, padx=5, pady=5, sticky="ew")
            summary_frame.grid_columnconfigure(i, weight=1)
            self.summary_cards[key] = card

        # Top profiles section
        top_section = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"]
        )
        top_section.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        ctk.CTkLabel(
            top_section,
            text="最常用配置 (Top 10)",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w", padx=15, pady=(15, 10))

        self.top_profiles_frame = ctk.CTkFrame(top_section, fg_color="transparent")
        self.top_profiles_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        # Recent profiles section
        recent_section = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"]
        )
        recent_section.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        ctk.CTkLabel(
            recent_section,
            text="最近使用配置",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w", padx=15, pady=(15, 10))

        self.recent_profiles_frame = ctk.CTkFrame(recent_section, fg_color="transparent")
        self.recent_profiles_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        # Daily trend section
        trend_section = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"]
        )
        trend_section.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        trend_header = ctk.CTkFrame(trend_section, fg_color="transparent")
        trend_header.pack(fill="x", padx=15, pady=(15, 10))

        ctk.CTkLabel(
            trend_header,
            text="使用趋势",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            trend_header,
            text="最近7天",
            text_color=COLORS["muted"],
            font=font(11),
        ).pack(side="left", padx=(10, 0))

        self.trend_frame = ctk.CTkFrame(trend_section, fg_color="transparent")
        self.trend_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

    def _create_summary_card(self, parent, title: str, value: str):
        """Create a summary card."""
        card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"]
        )

        ctk.CTkLabel(
            card,
            text=title,
            text_color=COLORS["muted"],
            font=font(11),
        ).pack(pady=(15, 5))

        value_label = ctk.CTkLabel(
            card,
            text=value,
            text_color=COLORS["text"],
            font=font(24, "bold"),
        )
        value_label.pack(pady=(0, 15))

        # Store reference to value label
        card.value_label = value_label

        return card

    def _create_profile_row(self, parent, stats, show_switch_button=False):
        """Create a profile statistics row."""
        row = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface_alt"],
            corner_radius=6,
            height=60
        )

        # Profile name and type
        info_frame = ctk.CTkFrame(row, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, padx=15, pady=10)

        name_label = ctk.CTkLabel(
            info_frame,
            text=stats.profile_name,
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w"
        )
        name_label.pack(anchor="w")

        type_text = "Claude Code" if stats.profile_type == "claude" else "Codex CLI"
        type_label = ctk.CTkLabel(
            info_frame,
            text=type_text,
            text_color=COLORS["muted"],
            font=font(10),
            anchor="w"
        )
        type_label.pack(anchor="w")

        # Stats
        stats_frame = ctk.CTkFrame(row, fg_color="transparent")
        stats_frame.pack(side="left", padx=10)

        self._add_stat_item(stats_frame, "切换", str(stats.switch_count), 0)
        self._add_stat_item(stats_frame, "Token", stats.format_tokens(), 1)

        # Success rate
        total_ops = stats.error_count + stats.success_count
        if total_ops > 0:
            success_rate = (stats.success_count / total_ops) * 100
            rate_text = f"{success_rate:.1f}%"
            if success_rate >= 90:
                rate_color = COLORS["success"]
            elif success_rate >= 70:
                rate_color = COLORS["warning"]
            else:
                rate_color = COLORS["danger"]
        else:
            rate_text = "N/A"
            rate_color = COLORS["muted"]

        self._add_stat_item(stats_frame, "成功率", rate_text, 2, rate_color)

        # Last used
        if stats.last_used:
            try:
                last_used = datetime.fromisoformat(stats.last_used)
                time_str = last_used.strftime("%m-%d %H:%M")
            except Exception:
                time_str = "N/A"
        else:
            time_str = "从未"

        self._add_stat_item(stats_frame, "最后使用", time_str, 3)

        # Duration
        self._add_stat_item(stats_frame, "时长", stats.format_duration(), 4)

        # Switch button (optional)
        if show_switch_button:
            btn = ctk.CTkButton(
                row,
                text="切换",
                width=60,
                command=lambda: self._switch_to_profile(stats),
                **button_style("primary"),
            )
            btn.pack(side="right", padx=10)

        return row

    def _add_stat_item(self, parent, label: str, value: str, column: int, value_color: str = None):
        """Add a stat item to the stats frame."""
        item = ctk.CTkFrame(parent, fg_color="transparent")
        item.grid(row=0, column=column, padx=8)

        ctk.CTkLabel(
            item,
            text=label,
            text_color=COLORS["muted"],
            font=font(9),
        ).pack()

        ctk.CTkLabel(
            item,
            text=value,
            text_color=value_color or COLORS["text"],
            font=font(11, "bold"),
        ).pack()

    def _switch_to_profile(self, stats):
        """Switch to a profile."""
        try:
            from core import switcher

            if stats.profile_type == "claude":
                switcher.switch_claude_profile(stats.profile_name)
                show_toast(self.winfo_toplevel(), f"已切换到: {stats.profile_name}")
            else:
                switcher.switch_codex_profile(stats.profile_name)
                show_toast(self.winfo_toplevel(), f"已切换到: {stats.profile_name}")

            # Refresh parent tabs
            self.winfo_toplevel().refresh_all()

        except Exception as e:
            logger.error(f"Failed to switch profile: {e}", exc_info=True)
            show_toast(self.winfo_toplevel(), f"切换失败: {e}", is_error=True)

    def _get_date_range(self):
        """Get date range based on filter."""
        date_text = self.date_filter.get()
        now = datetime.now()

        if date_text == "今日":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif date_text == "本周":
            # Monday as start of week
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif date_text == "本月":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif date_text == "最近7天":
            start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif date_text == "最近30天":
            start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:  # 全部时间
            return None, None

        return start, end

    def refresh(self):
        """Refresh statistics display."""
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            # Get filters
            filter_text = self.type_filter.get()
            profile_type = None
            if filter_text == "Claude":
                profile_type = "claude"
            elif filter_text == "Codex":
                profile_type = "codex"

            start_date, end_date = self._get_date_range()

        except Exception as e:
            logger.error(f"Failed to refresh stats: {e}", exc_info=True)
            return

        def worker():
            try:
                payload = {
                    "ok": True,
                    "dashboard": usage_stats.get_dashboard_data(
                        profile_type=profile_type,
                        start_date=start_date,
                        end_date=end_date,
                        top_limit=10,
                        recent_limit=10,
                        trend_days=7,
                    ),
                    "error": "",
                }
            except Exception as exc:
                payload = {"ok": False, "dashboard": None, "error": str(exc)}

            def finish():
                try:
                    if generation != self._refresh_generation or not self.winfo_exists():
                        return
                    if not payload["ok"]:
                        logger.error("Failed to refresh stats: %s", payload["error"])
                        return
                    self._apply_dashboard(profile_type, payload["dashboard"])
                    logger.info("Refreshed usage statistics")
                except Exception as exc:
                    logger.error("Failed to apply usage statistics: %s", exc, exc_info=True)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name="usage-stats-refresh", daemon=True).start()

    def _apply_dashboard(self, profile_type: Optional[str], dashboard: dict):
        summary = dashboard["summary"]

        self.summary_cards["total_profiles"].value_label.configure(
            text=str(summary["total_profiles"])
        )
        self.summary_cards["total_switches"].value_label.configure(
            text=str(summary["total_switches"])
        )
        self.summary_cards["total_tokens"].value_label.configure(
            text=format_token_count(summary["total_tokens"])
        )

        self.summary_cards["success_rate"].value_label.configure(text=_summary_success_rate_text(summary))

        self._populate_profiles(self.top_profiles_frame, dashboard["top_profiles"], show_switch=True)
        self._populate_profiles(self.recent_profiles_frame, dashboard["recent_profiles"], show_switch=True)
        self._populate_trend(profile_type, dashboard["trend"])

    def _populate_trend(self, profile_type: Optional[str] = None, trend_data: Optional[list[dict]] = None):
        """Populate daily trend chart."""
        # Clear existing
        for widget in self.trend_frame.winfo_children():
            widget.destroy()

        try:
            trend_data = trend_data if trend_data is not None else usage_stats.get_daily_trend(7, profile_type)

            if not trend_data or all(d["switch_count"] == 0 for d in trend_data):
                empty_label = ctk.CTkLabel(
                    self.trend_frame,
                    text="暂无趋势数据",
                    text_color=COLORS["muted"],
                    font=font(12),
                )
                empty_label.pack(pady=20)
                return

            # Find max value for scaling
            max_switches = max(d["switch_count"] for d in trend_data)
            max_tokens = max(d["total_tokens"] for d in trend_data)

            # Create chart
            chart_frame = ctk.CTkFrame(self.trend_frame, fg_color="transparent")
            chart_frame.pack(fill="both", expand=True, pady=10)

            for i, day_data in enumerate(trend_data):
                day_frame = ctk.CTkFrame(chart_frame, fg_color="transparent")
                day_frame.grid(row=0, column=i, padx=5, sticky="s")

                # Date label
                try:
                    date_obj = datetime.strptime(day_data["date"], "%Y-%m-%d")
                    date_label = date_obj.strftime("%m/%d")
                    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][date_obj.weekday()]
                except Exception:
                    date_label = day_data["date"][-5:]
                    weekday = ""

                # Bar height (scaled)
                switch_count = day_data["switch_count"]
                if max_switches > 0:
                    bar_height = int((switch_count / max_switches) * 100) + 20
                else:
                    bar_height = 20

                # Bar
                bar = ctk.CTkFrame(
                    day_frame,
                    width=40,
                    height=bar_height,
                    fg_color=COLORS["primary"] if switch_count > 0 else COLORS["border_soft"],
                    corner_radius=4
                )
                bar.pack(pady=(0, 5))

                # Value label
                ctk.CTkLabel(
                    day_frame,
                    text=str(switch_count),
                    text_color=COLORS["text"],
                    font=font(10, "bold"),
                ).pack()

                # Date label
                ctk.CTkLabel(
                    day_frame,
                    text=date_label,
                    text_color=COLORS["muted"],
                    font=font(9),
                ).pack()

                # Weekday label
                if weekday:
                    ctk.CTkLabel(
                        day_frame,
                        text=weekday,
                        text_color=COLORS["muted"],
                        font=font(8),
                    ).pack()

            # Legend
            legend_frame = ctk.CTkFrame(self.trend_frame, fg_color="transparent")
            legend_frame.pack(pady=(10, 0))

            ctk.CTkLabel(
                legend_frame,
                text="切换次数",
                text_color=COLORS["muted"],
                font=font(10),
            ).pack(side="left", padx=10)

            # Show token trend if available
            if max_tokens > 0:
                ctk.CTkLabel(
                    legend_frame,
                    text=f"Token 总量: {format_token_count(sum(d['total_tokens'] for d in trend_data))}",
                    text_color=COLORS["muted"],
                    font=font(10),
                ).pack(side="left", padx=10)

        except Exception as e:
            logger.error(f"Failed to populate trend: {e}", exc_info=True)
            error_label = ctk.CTkLabel(
                self.trend_frame,
                text=f"加载趋势失败: {e}",
                text_color=COLORS["danger"],
                font=font(11),
            )
            error_label.pack(pady=20)

    def _populate_profiles(self, parent, stats_list, show_switch=False):
        """Populate profiles list."""
        # Clear existing
        for widget in parent.winfo_children():
            widget.destroy()

        if not stats_list:
            empty_label = ctk.CTkLabel(
                parent,
                text="暂无数据",
                text_color=COLORS["muted"],
                font=font(12),
            )
            empty_label.pack(pady=20)
            return

        # Add rows
        for stats in stats_list:
            row = self._create_profile_row(parent, stats, show_switch_button=show_switch)
            row.pack(fill="x", pady=3)

    def _clear_stats(self):
        """Clear statistics with options."""
        from ui.dialogs.confirm_dialog import ConfirmDialog

        # Get current filters
        filter_text = self.type_filter.get()
        date_text = self.date_filter.get()

        # Build confirmation message
        if filter_text == "全部" and date_text == "全部时间":
            msg = "确定要清空所有使用统计数据吗？\n此操作不可撤销。"
        elif filter_text != "全部" and date_text == "全部时间":
            msg = f"确定要清空所有 {filter_text} 配置的统计数据吗？\n此操作不可撤销。"
        elif filter_text == "全部" and date_text != "全部时间":
            msg = f"注意：当前选择了时间范围「{date_text}」，\n但清零操作会清空该类型的全部历史数据。\n\n确定要继续吗？"
        else:
            msg = f"注意：当前选择了时间范围「{date_text}」，\n但清零操作会清空 {filter_text} 配置的全部历史数据。\n\n确定要继续吗？"

        def on_confirm():
            try:
                profile_type = None
                if filter_text == "Claude":
                    profile_type = "claude"
                elif filter_text == "Codex":
                    profile_type = "codex"

                usage_stats.clear_stats(profile_type=profile_type)
                self.refresh()

                show_toast(self.winfo_toplevel(), "统计数据已清零")

            except Exception as e:
                logger.error(f"Failed to clear stats: {e}", exc_info=True)
                show_toast(self.winfo_toplevel(), f"清零失败: {e}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            "确认清零统计",
            msg,
            on_confirm
        )
