"""Shared local-only subscription classification editor for both proxy tabs."""
from __future__ import annotations

import queue
import threading
import tkinter as tk

import customtkinter as ctk

from core import remote_proxy
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font


class SubscriptionTagsDialog(ctk.CTkToplevel):
    def __init__(self, master, catalog, on_saved):
        super().__init__(master)
        self.title("订阅类型标记")
        self.geometry("680x540")
        self.minsize(480, 360)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self._previous_grab = self.grab_current()
        self._on_saved = on_saved
        self._closed = False
        self._busy = False
        self._poll_id = None
        self._queue = queue.Queue()
        self._cancelled = threading.Event()
        self._original = {}
        self._combos = {}
        self._label_types = {label: value for value, label in remote_proxy.PROXY_SUBSCRIPTION_NETWORK_TYPE_LABELS.items()}
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 10))
        ctk.CTkLabel(header, text="为订阅标记线路类型", font=font(18, "bold"),
                     text_color=COLORS["text"], anchor="w").pack(fill="x")
        notice = ctk.CTkLabel(
            header, text="标记为手动分类，不代表实测。保存仅改标记，不切换线路。\n"
                         "家宽适合需要住宅出口的服务；YouTube、Google 等可选择非家宽订阅，避免占用家宽流量。",
            font=font(12), text_color=COLORS["muted"], anchor="w", justify="left",
        )
        notice.pack(fill="x", pady=(8, 0))
        bind_wraplength(header, notice, padding=8)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=(8, 16))
        self._save_button = ctk.CTkButton(footer, text="保存标记", command=self._save,
                                         **button_style("primary", compact=True))
        self._save_button.pack(side="right")
        ctk.CTkButton(footer, text="关闭", width=84, command=self._close,
                      **button_style("secondary", compact=True)).pack(side="right", padx=(0, 8))
        self._status = ctk.CTkLabel(self, text="", font=font(12), text_color=COLORS["muted"],
                                   anchor="w", justify="left")
        self._status.pack(side="bottom", fill="x", padx=18, pady=(4, 0))
        bind_wraplength(self, self._status, padding=44)

        self._rows = ctk.CTkScrollableFrame(self, fg_color=COLORS["surface"], corner_radius=8)
        self._rows.pack(fill="both", expand=True, padx=18)
        self._rows.grid_columnconfigure(0, weight=1)
        for entry in catalog:
            profile_id = str(entry.get("id") or "").strip()
            if not profile_id or profile_id in self._original:
                continue
            network_type = remote_proxy.normalize_proxy_subscription_network_type(entry.get("network_type"))
            self._original[profile_id] = network_type
            row = len(self._original) - 1
            identity = ctk.CTkFrame(self._rows, fg_color="transparent")
            identity.grid(row=row, column=0, sticky="ew", padx=(10, 14), pady=10)
            name = ctk.CTkLabel(identity, text=safe_feedback_text(str(entry.get("name") or "未命名订阅")),
                                font=font(13, "bold"), text_color=COLORS["text"], anchor="w", justify="left")
            name.pack(fill="x")
            bind_wraplength(identity, name, padding=8)
            ctk.CTkLabel(identity, text=f"订阅 ID：{profile_id[:12]}", font=font(11),
                         text_color=COLORS["muted"], anchor="w").pack(fill="x", pady=(3, 0))
            combo = ctk.CTkComboBox(self._rows, values=list(self._label_types), state="readonly", width=122,
                                    **combo_style())
            combo.set(remote_proxy.proxy_subscription_network_type_label(network_type))
            combo.grid(row=row, column=1, padx=(0, 10), pady=10)
            self._combos[profile_id] = combo
        if not self._original:
            ctk.CTkLabel(self._rows, text="暂无订阅，请先添加订阅链接或导入节点。", font=font(13),
                         text_color=COLORS["muted"]).grid(row=0, column=0, padx=12, pady=28)
            self._save_button.configure(state="disabled")
        center_window(self, master)
        self.grab_set()

    def _set_busy(self, busy):
        self._busy = busy
        self._save_button.configure(state="disabled" if busy or not self._original else "normal")
        for combo in self._combos.values():
            combo.configure(state="disabled" if busy else "readonly")

    def _save(self):
        if self._closed or self._busy:
            return
        updates = {}
        for profile_id, combo in self._combos.items():
            value = self._label_types.get(combo.get())
            if value is None:
                self._status.configure(text="请选择有效的订阅类型。", text_color=COLORS["warning"])
                return
            if value != self._original[profile_id]:
                updates[profile_id] = value
        if not updates:
            self._status.configure(text="标记没有变化，无需保存。", text_color=COLORS["muted"])
            return
        expected = {key: self._original[key] for key in updates}
        self._set_busy(True)
        self._status.configure(text="正在保存标记；不会切换正在运行的线路…", text_color=COLORS["muted"])
        output, cancelled = self._queue, self._cancelled

        def worker():
            try:
                remote_proxy.set_proxy_subscription_network_types(updates, expected=expected)
                message = (updates, "")
            except Exception as exc:
                message = ({}, safe_feedback_text(str(exc).strip() or type(exc).__name__))
            if not cancelled.is_set():
                output.put(message)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self._set_busy(False)
            self._status.configure(text="无法开始保存：" + safe_feedback_text(str(exc).strip() or type(exc).__name__),
                                   text_color=COLORS["warning"])
            return
        self._poll_id = self.after(40, self._poll)

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            updates, error = self._queue.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(40, self._poll)
            return
        self._set_busy(False)
        if error:
            self._status.configure(text="保存失败：" + error, text_color=COLORS["warning"])
            return
        self._original.update(updates)
        try:
            if self._on_saved is not None:
                self._on_saved()
        except Exception as exc:
            if not self._closed:
                self._status.configure(text="标记已保存，但列表刷新失败：" + safe_feedback_text(str(exc).strip() or type(exc).__name__),
                                       text_color=COLORS["warning"])
            return
        self.destroy()

    def _close(self):
        if self._closed:
            return
        if self._busy:
            self._status.configure(text="正在保存标记，请等待完成后关闭；不会切换运行线路。", text_color=COLORS["warning"])
            return
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        self._cancelled.set()
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        # Flush queued layout before destroying nested CTk windows on Windows.
        self.update_idletasks()
        held_grab = self.grab_current() is self
        if held_grab:
            self.grab_release()
        super().destroy()
        previous = self._previous_grab
        try:
            if (held_grab and previous is not None and previous.winfo_exists()
                    and not getattr(previous, "_closed", False) and previous.grab_current() is None):
                previous.grab_set()
        except tk.TclError:
            pass
