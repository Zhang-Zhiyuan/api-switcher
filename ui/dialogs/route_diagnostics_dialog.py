"""Read-only routing inspector shared by Windows and explicitly selected SSH hosts."""
from __future__ import annotations

import queue
import threading

import customtkinter as ctk

from core import proxy_route_diagnostics as diagnostics
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style, textbox_style


class RouteDiagnosticsDialog(ctk.CTkToplevel):
    def __init__(self, master, *, scopes=None, loader=None):
        super().__init__(master)
        self.title("分流检查 · 运行状态与网址去向")
        self.geometry("960x740")
        self.minsize(620, 480)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self._scopes = scopes or {"Win11 本机（含共享 WSL）": None}
        self._scope = next(iter(self._scopes))
        self._loader = loader or diagnostics.load_snapshot
        self._snapshot = None
        self._closed = False
        self._busy = False
        self._generation = 0
        self._queue = queue.Queue()
        self._cancelled = threading.Event()
        self._poll_id = None
        self._start_id = None
        self._query_host = ""
        self._stale = False
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(header, text="线路配置了，也要看实际怎么走", font=font(19, "bold"),
                     text_color=COLORS["text"], anchor="w").pack(fill="x")
        notice = ctk.CTkLabel(header, text="只读检查：读取内核规则、当前节点和已有探针记录。不会测速、切换节点或修改代理。",
                              font=font(12), text_color=COLORS["muted"], anchor="w", justify="left")
        notice.pack(fill="x", pady=(6, 10))
        bind_wraplength(header, notice, padding=8)
        scope_row = ctk.CTkFrame(header, fg_color="transparent")
        scope_row.pack(fill="x")
        scope_row.grid_columnconfigure(0, weight=1)
        self._scope_combo = ctk.CTkComboBox(scope_row, values=list(self._scopes), state="readonly",
                                           command=self._switch_scope, **combo_style())
        self._scope_combo.set(self._scope)
        self._scope_combo.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._refresh = ctk.CTkButton(scope_row, text="刷新运行状态", width=120, command=self.refresh,
                                      **button_style("secondary", compact=True))
        self._refresh.grid(row=0, column=1)

        query_row = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=8)
        query_row.pack(fill="x", padx=18, pady=(4, 8))
        query_row.grid_columnconfigure(0, weight=1)
        self._entry = ctk.CTkEntry(query_row, placeholder_text="输入网址、域名或 IP，例如 api.example.com",
                                  **input_style())
        self._entry.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        self._entry.bind("<Return>", lambda _event: self._show_query())
        self._query = ctk.CTkButton(query_row, text="检查网址去向", width=120, command=self._show_query,
                                    **button_style("primary", compact=True))
        self._query.grid(row=0, column=1, padx=(0, 10), pady=10)
        self._status = ctk.CTkLabel(self, text="准备读取…", font=font(12), text_color=COLORS["muted"],
                                   anchor="w", justify="left")
        self._status.pack(fill="x", padx=18)
        bind_wraplength(self, self._status, padding=44)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=(8, 16))
        self._overview = ctk.CTkButton(footer, text="返回运行总览", command=self._show_overview,
                                       **button_style("secondary", compact=True))
        self._overview.pack(side="left")
        self._copy = ctk.CTkButton(footer, text="复制本页报告", command=self._copy_report,
                                   **button_style("secondary", compact=True))
        self._copy.pack(side="left", padx=(8, 0))
        ctk.CTkButton(footer, text="关闭", width=84, command=self.destroy,
                      **button_style("secondary", compact=True)).pack(side="right")
        self._report = ctk.CTkTextbox(self, wrap="word", **textbox_style())
        self._report.pack(fill="both", expand=True, padx=18, pady=(8, 0))
        for tag, color in (("heading", "accent"), ("warning", "warning"), ("secondary", "muted")):
            self._report._textbox.tag_configure(tag, foreground=COLORS[color])
        self._report.configure(state="disabled")
        center_window(self, master)
        self._start_id = self.after(0, self.refresh)

    def _set_report(self, text):
        self._report.configure(state="normal")
        self._report.delete("1.0", "end")
        sanitized = safe_feedback_text(text)
        self._report.insert("1.0", sanitized)
        for line, content in enumerate(sanitized.splitlines(), 1):
            tag = ""
            if any(marker in content for marker in ("已过期", "无法确定", "失败", "策略组不同", "与已保存策略组不同", "未核对")):
                tag = "warning"
            elif content.startswith(("目标：", "运行规则快照")) or "  ·  示例 " in content:
                tag = "heading"
            elif content.startswith(("读取时间", "快照", "每个目标", "仅适用", "只分析")):
                tag = "secondary"
            if tag:
                self._report._textbox.tag_add(tag, f"{line}.0", f"{line}.end")
        self._report.configure(state="disabled")

    def _copy_report(self):
        if self._closed or self._busy or not self._snapshot:
            return
        try:
            text = safe_feedback_text(self._report.get("1.0", "end").strip())
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status.configure(text="已复制本页脱敏报告；运行快照与历史探针均不代表实时请求结果。", text_color=COLORS["muted"])
        except Exception:
            self._status.configure(text="复制失败，请稍后重试。", text_color=COLORS["warning"])

    def _set_busy(self, value):
        self._busy = value
        state = "disabled" if value else "normal"
        self._refresh.configure(state=state, text="读取中…" if value else "刷新运行状态")
        self._query.configure(state="normal" if not value and self._snapshot else "disabled")
        self._copy.configure(state="normal" if not value and self._snapshot else "disabled")
        self._scope_combo.configure(state="disabled" if value else "readonly")

    def _switch_scope(self, scope):
        if self._closed or self._busy or scope not in self._scopes or scope == self._scope:
            return
        self._scope = scope
        self._scope_combo.set(scope)
        self.refresh()

    def refresh(self):
        self._start_id = None
        if self._closed or self._busy:
            return
        self._generation += 1
        generation, scope = self._generation, self._scopes[self._scope]
        self._snapshot = None
        self._set_busy(True)
        self._status.configure(text="正在读取所选位置；只查询受管内核，不向输入的网址发请求。", text_color=COLORS["muted"])
        self._set_report("正在获取运行快照。旧结果已失效，不会当成本次检查结果。")
        results, loader, cancelled = self._queue, self._loader, self._cancelled

        def worker():
            try:
                payload = (generation, loader(scope), "")
            except Exception as exc:
                payload = (generation, None, safe_feedback_text(str(exc)))
            if not cancelled.is_set():
                results.put(payload)

        try:
            threading.Thread(target=worker, name="read-only-route-diagnostics", daemon=True).start()
        except Exception as exc:
            results.put((generation, None, safe_feedback_text(str(exc))))
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        self._poll_id = self.after(80, self._poll)

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            generation, snapshot, error = self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            if generation == self._generation:
                self._snapshot = snapshot
                self._set_busy(False)
                if error:
                    self._status.configure(text="读取失败；未修改运行配置，可刷新重试。", text_color=COLORS["warning"])
                    self._set_report(error)
                else:
                    self._stale = snapshot.stale()
                    self._render()
        if self._snapshot and self._snapshot.stale() != self._stale:
            self._stale = self._snapshot.stale()
            self._render()
        self._poll_id = self.after(80 if self._busy else 1000, self._poll)

    def _render(self):
        if not self._snapshot:
            return
        snapshot = self._snapshot
        text = snapshot.explain(self._query_host) if self._query_host else diagnostics.snapshot_report(snapshot)
        self._set_report(text)
        self._status.configure(
            text="快照已过期，请刷新后再判断线路。" if snapshot.stale() else
                 (snapshot.error or "已读取内核运行规则；历史探针通过不等于账号可用，未测试真实出口 IP。"),
            text_color=COLORS["warning"] if snapshot.stale() or snapshot.error else COLORS["muted"],
        )

    def _show_query(self):
        if self._closed or self._busy or not self._snapshot:
            return
        try:
            host = diagnostics.target_host(self._entry.get())
        except ValueError as exc:
            self._status.configure(text=str(exc), text_color=COLORS["warning"])
            return
        self._query_host = host
        self._entry.delete(0, "end")
        self._entry.insert(0, host)  # Do not retain pasted URL query credentials.
        self._render()

    def _show_overview(self):
        self._query_host = ""
        if not self._busy:
            self._render()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        self._cancelled.set()
        self._snapshot = None
        self._generation += 1
        for after_id in (self._poll_id, self._start_id):
            if after_id is not None:
                self.after_cancel(after_id)
        self.update_idletasks()
        super().destroy()
