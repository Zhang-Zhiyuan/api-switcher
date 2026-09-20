"""Local-only routing choices. These dialogs only update the parent's draft."""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from core.proxy_routing import MAX_SERVICE_NODE_POOL_SIZE
from core.subscription_routing_policy import route_candidate_count
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, font, input_style


AUTO_MODE = "订阅内自动切换"
FIXED_MODE = "固定节点"
POOL_MODE = "自选候选自动切换"
MAX_POOL_NODES = MAX_SERVICE_NODE_POOL_SIZE


def matching_nodes(nodes, query):
    terms = str(query).casefold().split()
    return [item for item in nodes if all(term in str(item["label"]).casefold() for term in terms)]


class NodeListFrame(ctk.CTkFrame):
    """Scale the single native list along with the surrounding CTk controls."""

    def _set_scaling(self, *args, **kwargs):
        super()._set_scaling(*args, **kwargs)
        self.sync_font()

    def sync_font(self):
        if hasattr(self, "listbox"):
            self.listbox.configure(font=font(13).create_scaled_tuple(self._get_widget_scaling()))


class DraftChoiceDialog(ctk.CTkToplevel):
    """Restore the draft editor's grab when a nested selector closes."""

    def destroy(self):
        if getattr(self, "_choice_destroyed", False):
            return
        self._choice_destroyed = True
        parent = self.master
        # Native Tk on Windows can crash if a just-laid-out nested window and
        # its parent are destroyed before queued geometry work completes.
        # Flush layout only, never timers/input via a reentrant update().
        self.update_idletasks()
        self.grab_release()
        super().destroy()
        try:
            if parent.winfo_exists() and not getattr(parent, "_closed", False):
                parent.grab_set()
        except tk.TclError:
            pass


class RouteNodeDialog(DraftChoiceDialog):
    def __init__(self, master, *, service_label, profile_name, nodes, selected_key, on_select,
                 auto_route_usable=True, auto_route_candidate_count=None,
                 selected_keys=None, on_select_pool=None):
        super().__init__(master)
        self.title("选择节点策略")
        self.geometry("700x710" if on_select_pool or selected_keys else "680x600")
        self.minsize(480, 540 if on_select_pool or selected_keys else 410)
        self.configure(fg_color=COLORS["app_bg"])
        self._nodes = [dict(item) for item in nodes]
        self._keys = {item["key"] for item in self._nodes}
        self._candidate_count = route_candidate_count({
            "nodes": self._nodes, "auto_route_candidate_count": auto_route_candidate_count,
        })
        self._auto_route_usable = auto_route_usable is True and self._candidate_count > 0
        self._selected_key = selected_key
        self._on_select = on_select
        source_keys = [selected_keys] if isinstance(selected_keys, str) else (selected_keys or ())
        self._selected_keys = list(dict.fromkeys(key for key in source_keys if isinstance(key, str) and key))
        self._on_select_pool = on_select_pool
        self._pool_enabled = callable(on_select_pool) or bool(self._selected_keys)
        self._pool_feedback = ""
        self._refreshing_list = False
        self._filter_after_id = None
        self._closed = False
        self._visible = []
        self._mode = POOL_MODE if self._selected_keys else (FIXED_MODE if selected_key else AUTO_MODE)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=(6, 16))
        self._pool_frame = ctk.CTkFrame(footer, fg_color=COLORS["surface"])
        self._pool_caption = ctk.CTkLabel(self._pool_frame, text="已选候选 · 从上到下为故障切换优先顺序",
                                         font=font(11), anchor="w")
        self._pool_caption.pack(fill="x", padx=8, pady=(5, 2))
        pool_body = NodeListFrame(self._pool_frame, fg_color="transparent", height=90)
        pool_body.pack(fill="x", padx=8, pady=(0, 6))
        pool_actions = ctk.CTkFrame(pool_body, fg_color="transparent", height=80)
        pool_actions.pack(side="right", fill="y", padx=(8, 0))
        for label, command in (("上移", lambda: self._move_pool(-1)),
                               ("下移", lambda: self._move_pool(1)),
                               ("移除", self._remove_pool)):
            ctk.CTkButton(pool_actions, text=label, width=66, command=command,
                          **{**button_style("secondary", compact=True), "height": 24}).pack(fill="x", pady=1)
        self._pool_list = tk.Listbox(
            pool_body, exportselection=False, activestyle="dotbox", height=4,
            bg=COLORS["field_bg"], fg=COLORS["text"], selectbackground=COLORS["primary"],
            selectforeground=COLORS["text"], borderwidth=0, highlightthickness=0,
        )
        pool_body.listbox = self._pool_list
        pool_body.sync_font()
        pool_scroll = ctk.CTkScrollbar(pool_body, command=self._pool_list.yview, height=80)
        pool_scroll.pack(side="right", fill="y")
        self._pool_list.configure(yscrollcommand=pool_scroll.set)
        self._pool_list.pack(fill="both", expand=True)
        self._pool_list.bind("<Delete>", lambda _event: self._remove_pool())
        self._selection = ctk.CTkLabel(footer, text="", font=font(12), anchor="w", justify="left", height=22)
        self._selection.pack(fill="x", pady=(0, 8))
        bind_wraplength(footer, self._selection, padding=4)
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.pack(fill="x")
        actions.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(actions, text="取消", command=self.destroy, **button_style("secondary")).grid(
            row=0, column=0, sticky="ew", padx=(0, 8))
        self._choose = ctk.CTkButton(actions, text="使用此选择", command=self._commit, **button_style("accent"))
        self._choose.grid(row=0, column=1, sticky="ew")

        heading = ctk.CTkLabel(self, text=safe_feedback_text(f"{service_label} · {profile_name}"),
                              font=font(17, "bold"), text_color=COLORS["text"], anchor="w", justify="left")
        heading.pack(fill="x", padx=18, pady=(16, 8))
        bind_wraplength(self, heading, padding=40)
        modes = [AUTO_MODE, POOL_MODE, FIXED_MODE] if self._pool_enabled else [AUTO_MODE, FIXED_MODE]
        self._modes = ctk.CTkSegmentedButton(self, values=modes, command=self._set_mode, font=font(12),
                                           selected_color=COLORS["primary"], unselected_color=COLORS["secondary"])
        self._modes.pack(fill="x", padx=18)
        self._modes.set(self._mode)
        note_text = ("全订阅：使用首选和订阅备用。自选：只在勾选节点间按顺序故障切换。\n"
                     "固定：不自动换出口。列表来自本地缓存；选择仅写入草稿，保存并应用后生效。"
                     if self._pool_enabled else
                     "自动：只在此订阅内故障切换。固定：保持所选节点，不自动换出口。\n"
                     "列表来自本地缓存，未执行实时测速。这里的选择只写入草稿。")
        note = ctk.CTkLabel(self, text=note_text,
                           text_color=COLORS["muted"], font=font(11), anchor="w", justify="left")
        note.pack(fill="x", padx=18, pady=8)
        bind_wraplength(self, note, padding=40)
        self._search = ctk.CTkEntry(self, placeholder_text="搜索候选节点：名称 / 地区关键词，可用空格组合", **input_style())
        self._search.pack(fill="x", padx=18)
        self._search.bind("<KeyRelease>", self._schedule_filter)
        self._search.bind("<<Paste>>", self._schedule_filter, add="+")
        self._search.bind("<<Cut>>", self._schedule_filter, add="+")
        self._search.bind("<Down>", self._focus_list)
        self._count = ctk.CTkLabel(self, text="", font=font(11), text_color=COLORS["muted"], anchor="w", height=20)
        self._count.pack(fill="x", padx=18, pady=(4, 0))
        bind_wraplength(self, self._count, padding=40)

        # One native list rather than one Tk frame per node: large subscriptions
        # stay searchable without constructing thousands of widgets.
        body = NodeListFrame(self, fg_color=COLORS["field_bg"])
        body.pack(fill="both", expand=True, padx=18, pady=(4, 0))
        self._list = tk.Listbox(body, exportselection=False, activestyle="dotbox", height=6,
                               bg=COLORS["field_bg"], fg=COLORS["text"], font=font(12),
                               selectbackground=COLORS["primary"], selectforeground=COLORS["text"],
                               borderwidth=0, highlightthickness=0)
        body.listbox = self._list
        body.sync_font()
        scrollbar = ctk.CTkScrollbar(body, command=self._list.yview)
        scrollbar.pack(side="right", fill="y")
        horizontal = ctk.CTkScrollbar(body, orientation="horizontal", command=self._list.xview, height=12)
        horizontal.pack(side="bottom", fill="x")
        self._list.pack(fill="both", expand=True, padx=6, pady=6)
        self._list.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self._list.bind("<<ListboxSelect>>", self._select_visible)
        self._list.bind("<Double-Button-1>", self._commit_from_list)
        self._list.bind("<Return>", self._commit_from_list)
        self._filter()
        center_window(self, master)
        self.grab_set()
        self._search.focus_set()

    def _set_mode(self, mode):
        if mode not in {AUTO_MODE, FIXED_MODE, POOL_MODE} or (mode == POOL_MODE and not self._pool_enabled):
            return
        self._mode = mode
        self._modes.set(mode)
        self._filter()

    def _schedule_filter(self, _event=None):
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(100, self._filter)

    def _filter(self):
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
            self._filter_after_id = None
        self._visible = matching_nodes(self._nodes, self._search.get())
        self._refreshing_list = True
        self._list.configure(selectmode="multiple" if self._mode == POOL_MODE else "browse")
        self._list.delete(0, "end")
        labels = [("☑ " if item["key"] in self._selected_keys else "☐ ")
                  + safe_feedback_text(str(item["label"])) if self._mode == POOL_MODE
                  else safe_feedback_text(str(item["label"])) for item in self._visible]
        # Chunk Tcl arguments, keeping the underlying list widget and selection.
        for start in range(0, len(labels), 200):
            self._list.insert("end", *labels[start:start + 200])
        for index, item in enumerate(self._visible):
            if self._mode == POOL_MODE and item["key"] in self._selected_keys:
                self._list.selection_set(index)
            elif self._mode != POOL_MODE and item["key"] == self._selected_key:
                self._list.selection_set(index)
                self._list.activate(index)
                self._list.see(index)
                break
        self._refreshing_list = False
        self._count.configure(text=f"显示 {len(self._visible)} / {len(self._nodes)} 个节点"
                              + (" · 无匹配结果，请更换关键词" if not self._visible else
                                 " · 点击勾选/取消，搜索不会丢失已选项" if self._mode == POOL_MODE else
                                 " · 点击节点即可选择固定出口"))
        if self._mode == POOL_MODE:
            self._pool_frame.pack(fill="x", pady=(0, 6), before=self._selection)
            self._render_pool()
        else:
            self._pool_frame.pack_forget()
        self._update_selection()

    def _render_pool(self, selected_index=None):
        if selected_index is None:
            selected = self._pool_list.curselection()
            selected_index = selected[0] if selected else None
        self._pool_list.delete(0, "end")
        labels = {item["key"]: safe_feedback_text(str(item["label"])) for item in self._nodes}
        for index, key in enumerate(self._selected_keys):
            label = labels.get(key, "已失效的候选 · " + safe_feedback_text(key[:16]))
            self._pool_list.insert("end", f"{index + 1}. {label}")
            if key not in self._keys:
                self._pool_list.itemconfigure(index, fg=COLORS["warning"])
        if selected_index is not None and self._selected_keys:
            selected_index = max(0, min(selected_index, len(self._selected_keys) - 1))
            self._pool_list.selection_set(selected_index)
            self._pool_list.see(selected_index)
        self._pool_caption.configure(text=f"已选 {len(self._selected_keys)}/{MAX_POOL_NODES} · 从上到下为故障切换优先顺序")

    def _move_pool(self, step):
        selected = self._pool_list.curselection()
        if self._mode != POOL_MODE or not selected:
            return
        index = selected[0]
        target = index + step
        if 0 <= target < len(self._selected_keys):
            self._selected_keys[index], self._selected_keys[target] = self._selected_keys[target], self._selected_keys[index]
            self._render_pool(target)

    def _remove_pool(self):
        selected = self._pool_list.curselection()
        if self._mode != POOL_MODE or not selected:
            return
        self._selected_keys.pop(selected[0])
        self._pool_feedback = ""
        self._filter()
        self._render_pool(selected[0])

    def _update_selection(self):
        item = next((item for item in self._nodes if item["key"] == self._selected_key), None)
        if self._mode == AUTO_MODE:
            text = "使用订阅首选；备用按服务策略筛选，仅在此订阅内故障切换。"
            valid = bool(self._nodes) and self._auto_route_usable
            if not self._nodes or not self._candidate_count:
                text = "该订阅暂无可用缓存，请返回编辑器重读缓存或先拉取订阅。"
            elif not self._auto_route_usable:
                text = "当前订阅首选无法独立运行，请选择固定节点，或返回订阅区更换首选后重读缓存。"
            elif self._candidate_count == 1:
                text = "仅 1 个独立候选节点，暂无备用可切换；将使用订阅首选。"
        elif self._mode == POOL_MODE:
            missing = sum(key not in self._keys for key in self._selected_keys)
            valid = bool(self._selected_keys) and not missing and len(self._selected_keys) <= MAX_POOL_NODES and callable(self._on_select_pool)
            text = f"仅在这 {len(self._selected_keys)} 个候选中按顺序故障切换，不使用订阅内其他节点。"
            if not self._selected_keys:
                text = f"请勾选候选节点（最多 {MAX_POOL_NODES} 个）；可在已选列表中调整优先顺序。"
            elif missing:
                text = f"有 {missing} 个已选节点不在当前缓存中。已保留原选择，请移除失效项或重新拉取订阅后再保存。"
            elif len(self._selected_keys) > MAX_POOL_NODES:
                text = f"已选数量超过 {MAX_POOL_NODES} 个，请移除多余候选；不会静默截断原列表。"
            elif len(self._selected_keys) == 1:
                text = "仅选 1 个候选，暂无备用可切换；可继续勾选同订阅节点。"
            if self._pool_feedback:
                text = self._pool_feedback + " " + text
        else:
            text = "已选固定节点：" + safe_feedback_text(str(item["label"])) if item else "请选择一个节点；原固定节点缺失时不会自动替换。"
            valid = item is not None
        self._selection.configure(text=text, text_color=COLORS["text"] if valid else COLORS["warning"])
        self._choose.configure(state="normal" if valid else "disabled")

    def _select_visible(self, _event=None):
        if self._refreshing_list:
            return
        selected = self._list.curselection()
        if self._mode == POOL_MODE:
            scroll_position = self._list.yview()[0]
            visible_keys = {item["key"] for item in self._visible}
            requested = [self._visible[index]["key"] for index in selected if index < len(self._visible)]
            self._selected_keys = [key for key in self._selected_keys if key not in visible_keys or key in requested]
            self._pool_feedback = ""
            for key in requested:
                if key in self._selected_keys:
                    continue
                if len(self._selected_keys) >= MAX_POOL_NODES:
                    self._pool_feedback = f"最多选择 {MAX_POOL_NODES} 个候选，未添加超出的节点。"
                    break
                self._selected_keys.append(key)
            self._filter()
            self._list.yview_moveto(scroll_position)
            return
        if selected and selected[0] < len(self._visible):
            self._selected_key = self._visible[selected[0]]["key"]
            self._set_mode(FIXED_MODE)

    def _focus_list(self, _event=None):
        if self._visible:
            self._list.focus_set()
            if not self._list.curselection():
                self._list.activate(0)
                if self._mode != POOL_MODE:
                    self._list.selection_set(0)
                    self._select_visible()
        return "break"

    def _commit_from_list(self, _event=None):
        if self._mode == POOL_MODE:
            # Multi-selection must not close after a double click or Enter;
            # only the explicit footer action commits the complete candidate set.
            return "break"
        if self._list.curselection():
            self._select_visible()
            self._commit()
        return "break"

    def _commit(self):
        if self._closed or not self._nodes:
            return
        if self._mode == POOL_MODE:
            if (not self._selected_keys or len(self._selected_keys) > MAX_POOL_NODES
                    or any(key not in self._keys for key in self._selected_keys)
                    or not callable(self._on_select_pool)):
                return
            self._on_select_pool(list(self._selected_keys))
            self.destroy()
            return
        if self._mode == AUTO_MODE and not self._auto_route_usable:
            return
        key = "" if self._mode == AUTO_MODE else self._selected_key
        if self._mode == FIXED_MODE and key not in self._keys:
            return
        self._on_select(key)
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
            self._filter_after_id = None
        super().destroy()


class RouteScopeCopyDialog(DraftChoiceDialog):
    def __init__(self, master, *, source, scopes, dirty_scopes, on_copy):
        super().__init__(master)
        self.title("选择分流草稿的接收方")
        self.geometry("560x470")
        self.minsize(400, 330)
        self.configure(fg_color=COLORS["app_bg"])
        self._on_copy = on_copy
        self._vars = {}
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=14)
        ctk.CTkButton(footer, text="取消", width=80, command=self.destroy, **button_style("secondary")).pack(side="left")
        self._copy = ctk.CTkButton(footer, text="复制到选中草稿", state="disabled", command=self._commit,
                                  **button_style("accent"))
        self._copy.pack(side="right")
        hint = ctk.CTkLabel(self, text=safe_feedback_text(f"来源：{source}\n仅覆盖勾选位置的分流草稿；不会立即应用，也不更改各自的默认代理。"),
                            font=font(12), text_color=COLORS["text"], anchor="w", justify="left")
        hint.pack(fill="x", padx=18, pady=14)
        bind_wraplength(self, hint, padding=40)
        body = ctk.CTkScrollableFrame(self, fg_color=COLORS["surface"])
        body.pack(fill="both", expand=True, padx=18)
        self._labels = {}
        for scope in scopes:
            var = ctk.BooleanVar(value=False)
            self._vars[scope] = var
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=8, padx=6)
            row.grid_columnconfigure(1, weight=1)
            check = ctk.CTkCheckBox(row, text="", variable=var, command=self._changed, width=22,
                                    checkbox_width=16, checkbox_height=16)
            check.grid(row=0, column=0, sticky="nw", padx=(0, 6))
            label = ctk.CTkLabel(row, text=safe_feedback_text(scope), font=font(12), anchor="w", justify="left", height=24, width=1)
            label.grid(row=0, column=1, sticky="ew")
            label.bind("<Button-1>", lambda _event, button=check: button.toggle())
            bind_wraplength(label, label, padding=4)
            self._labels[scope] = label
            if scope in dirty_scopes:
                warning = ctk.CTkLabel(row, text="有未保存修改，复制后将被覆盖", font=font(11), anchor="w", justify="left",
                                      text_color=COLORS["warning"], height=20, width=1)
                warning.grid(row=1, column=1, sticky="ew")
                bind_wraplength(warning, warning, padding=4)
        center_window(self, master)
        self.grab_set()

    def _changed(self):
        self._copy.configure(state="normal" if any(var.get() for var in self._vars.values()) else "disabled")

    def _commit(self):
        scopes = [scope for scope, var in self._vars.items() if var.get()]
        if scopes:
            self._on_copy(scopes)
            self.destroy()
