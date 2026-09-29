"""Preview offline presets; the caller owns the draft and explicit live apply."""
from __future__ import annotations

import copy

import customtkinter as ctk

from core import proxy_routing
from core.route_presets import NETWORK_LABELS, ROUTE_PRESETS, plan_route_preset, preset_sources
from ui.dialogs.modal_dialog import RestoreGrabDialog
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font
from ui.widgets.service_route_overview import route_description


class RoutePresetDialog(RestoreGrabDialog):
    def __init__(self, master, *, scope, preferences, catalog, on_accept,
                 protected_services=(), strict_privacy=False):
        super().__init__(master)
        self.title("智能分流预设")
        self.geometry("800x740")
        self.minsize(540, 480)
        self.configure(fg_color=COLORS["app_bg"])
        self._preferences = copy.deepcopy(preferences)
        self._catalog = copy.deepcopy(catalog)
        self._protected = set(protected_services)
        self._strict_privacy = strict_privacy
        self._on_accept = on_accept
        self._plan = None
        self._options = {}
        self._source_combos = {}
        self._preview_rows = {}
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=12)
        self._status = ctk.CTkLabel(footer, text="", font=font(12), anchor="w", justify="left", height=22)
        self._status.pack(fill="x", pady=(0, 6))
        bind_wraplength(footer, self._status, padding=4)
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.pack(fill="x")
        actions.grid_columnconfigure((0, 1), weight=1, uniform="preset-actions")
        ctk.CTkButton(actions, text="取消", command=self.destroy, **button_style("secondary")).grid(
            row=0, column=0, sticky="ew", padx=(0, 8))
        self._accept_button = ctk.CTkButton(actions, text="使用预设草稿", command=self._accept,
                                           state="disabled", **button_style("accent"))
        self._accept_button.grid(row=0, column=1, sticky="ew")

        body = self._body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        ctk.CTkLabel(body, text="按用途，一次规划好线路", font=font(18, "bold"), anchor="w").pack(fill="x", padx=6)
        scope_label = ctk.CTkLabel(body, text="应用位置：" + safe_feedback_text(scope), font=font(12),
                                   text_color=COLORS["muted"], anchor="w", justify="left")
        scope_label.pack(fill="x", padx=6, pady=(2, 10))
        bind_wraplength(body, scope_label, padding=16)
        self._scheme_labels = {item["label"]: key for key, item in ROUTE_PRESETS.items()}
        self._scheme = ctk.CTkComboBox(body, values=list(self._scheme_labels), state="readonly",
                                      command=lambda _value: self._refresh(), **combo_style())
        self._scheme.set(ROUTE_PRESETS["balanced"]["label"])
        self._scheme.pack(fill="x", padx=6)
        self._description = ctk.CTkLabel(body, text="", font=font(12), anchor="w", justify="left", text_color=COLORS["muted"])
        self._description.pack(fill="x", padx=6, pady=(4, 10))
        bind_wraplength(body, self._description, padding=16)

        sources_box = ctk.CTkFrame(body, fg_color=COLORS["surface"])
        sources_box.pack(fill="x", padx=6, pady=(0, 8))
        sources_box.grid_columnconfigure(1, weight=1)
        for index, (network_type, label) in enumerate(NETWORK_LABELS.items()):
            options = {"自动推荐": ""}
            for item in preset_sources(self._catalog, network_type):
                text = " ".join(safe_feedback_text(str(item.get("name") or "未命名订阅")).split())
                base, suffix = text, 2
                while text in options:
                    text = f"{base} ({suffix})"
                    suffix += 1
                options[text] = item["id"]
            self._options[network_type] = options
            ctk.CTkLabel(sources_box, text=label + "订阅", font=font(12)).grid(row=index, column=0, padx=10, pady=8)
            combo = ctk.CTkComboBox(sources_box, values=list(options), state="readonly",
                                   command=lambda _value: self._refresh(), **combo_style())
            combo.set("自动推荐")
            combo.grid(row=index, column=1, sticky="ew", padx=10, pady=8)
            self._source_combos[network_type] = combo
        self._replace = ctk.BooleanVar(value=False)
        self._replace_checkbox = ctk.CTkCheckBox(
            body, text="重新规划已有分流（仍保留关闭的目标）", variable=self._replace,
            command=self._refresh, font=font(12), checkbox_width=18, checkbox_height=18)
        self._replace_checkbox.pack(fill="x", padx=6, pady=(4, 8))
        note = ctk.CTkLabel(body, text="默认保留手动选择；自定义和全局代理范围不变。\n"
                            "推荐仅依据标记与缓存，未验证出口质量和实时连通性。",
                            font=font(11), anchor="w", justify="left", text_color=COLORS["muted"])
        note.pack(fill="x", padx=6)
        bind_wraplength(body, note, padding=16)
        self._source_summary = ctk.CTkLabel(body, text="", font=font(12), anchor="w", justify="left")
        self._source_summary.pack(fill="x", padx=6, pady=(10, 4))
        bind_wraplength(body, self._source_summary, padding=16)
        ctk.CTkLabel(body, text="目标预览 · 未保存、未应用", font=font(14, "bold"), anchor="w").pack(fill="x", padx=6, pady=(8, 4))
        for row in proxy_routing.route_rows(self._preferences):
            if row["id"] == "custom" or row["id"].startswith("custom:"):
                continue
            tile = ctk.CTkFrame(body, fg_color=COLORS["surface"], corner_radius=6)
            tile.pack(fill="x", padx=6, pady=(0, 6))
            heading = ctk.CTkLabel(tile, text=row["label"], font=font(12, "bold"), anchor="w")
            heading.pack(fill="x", padx=10, pady=(6, 0))
            detail = ctk.CTkLabel(tile, text="", font=font(11), anchor="w", justify="left", height=18)
            detail.pack(fill="x", padx=10, pady=(0, 6))
            bind_wraplength(tile, detail, padding=24)
            self._preview_rows[row["id"]] = (heading, detail)
        self._refresh()
        center_window(self, master)
        self.grab_set()

    def _choices(self):
        return {"preset_id": self._scheme_labels[self._scheme.get()],
                "sources": {key: self._options[key][combo.get()] for key, combo in self._source_combos.items()},
                "replace_existing": bool(self._replace.get())}

    def _refresh(self):
        choices = self._choices()
        preset_id = choices["preset_id"]
        self._description.configure(text=ROUTE_PRESETS[preset_id]["description"])
        for key, combo in self._source_combos.items():
            used = preset_id == "balanced" or (key == "residential" if preset_id == "ai_only" else key == "datacenter")
            combo.configure(state="readonly" if used else "disabled")
        try:
            plan = plan_route_preset(self._preferences, self._catalog, **choices,
                                     protected_services=self._protected, strict_privacy=self._strict_privacy)
        except ValueError as exc:
            self._plan = None
            self._status.configure(text=safe_feedback_text(str(exc)), text_color=COLORS["warning"])
            self._accept_button.configure(state="disabled", text="无法生成此预设")
            self._source_summary.configure(text="原草稿和当前网络均未改变。", text_color=COLORS["warning"])
            for heading, detail in self._preview_rows.values():
                heading.configure(text=heading.cget("text").split(" · ")[0])
                detail.configure(text="未生成修改，请选择其他预设。", text_color=COLORS["muted"])
            return
        self._plan = plan
        labels = {item["id"]: " ".join(safe_feedback_text(str(item.get("name") or "未命名订阅")).split())
                  for item in self._catalog if isinstance(item, dict) and isinstance(item.get("id"), str)}
        summary = []
        for key, source in plan["sources"].items():
            summary.append(f"{NETWORK_LABELS[key]} → {labels.get(source['id'], '暂未分配')}\n{source['reason']}")
        self._source_summary.configure(text="\n".join(summary), text_color=COLORS["muted"])
        rows = {row["id"]: row for row in proxy_routing.route_rows(plan["draft"])}
        statuses = {"changed": "将修改", "kept": "保留", "unavailable": "待处理", "unchanged": "无需修改"}
        for decision in plan["decisions"]:
            service = decision["service"]
            description = route_description(rows[service], plan["draft"], self._catalog)
            heading, detail = self._preview_rows[service]
            heading.configure(text=decision["label"] + " · " + statuses[decision["status"]])
            destination = description["profile"]
            if decision["status"] in {"kept", "unavailable"}:
                destination += " → " + description["node"]
            detail.configure(text=f"{destination}\n{decision['reason']}",
                              text_color=COLORS["warning"] if decision["status"] == "unavailable" else COLORS["muted"])
        count = len(plan["changed_services"])
        missing = sum(item["status"] == "unavailable" for item in plan["decisions"])
        self._status.configure(text=f"{count} 项修改 · {missing} 项待处理\n仅生成草稿，保存并应用后生效。",
                               text_color=COLORS["warning"] if missing else COLORS["muted"])
        if not count and not missing:
            self._status.configure(text="已有线路已保留；需要重新分配请勾选“重新规划”。"
                                   if not choices["replace_existing"] else "当前线路已符合预设，无需修改；已关闭的目标不变。")
        self._accept_button.configure(state="normal" if count else "disabled",
                                      text=f"使用预设草稿（{count}）" if count else "无需修改")

    def _accept(self):
        if self._modal_destroyed or self._plan is None or not self._plan["changed_services"]:
            return
        try:
            self._on_accept(**self._choices(), expected_plan=copy.deepcopy(self._plan))
        except Exception as exc:
            self._status.configure(text=safe_feedback_text(str(exc)), text_color=COLORS["warning"])
            return
        self.destroy()
