import threading

import customtkinter as ctk
from tkinter import filedialog
from ui.widgets.masked_entry import MaskedEntry
from models.profile import SSHProfile
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style
from ui.ui_dispatch import run_on_ui_thread


class SSHEditorDialog(ctk.CTkToplevel):
    """Dialog for creating or editing an SSH server profile."""

    def __init__(self, master, title="编辑 SSH 服务器", profile: SSHProfile = None, on_save=None):
        super().__init__(master)
        self.title(title)
        self.geometry("640x700")
        self.minsize(540, 500)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._destroyed = False
        self._ui_dispatch = getattr(master, "_run_on_ui_thread", None)
        self._on_save = on_save
        self._profile = profile
        self._field_rows = {}
        self._key_row = None
        self._test_busy = False
        self._field_layouts = {}
        self._responsive_after_id = None
        self._responsive_stacked = None

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            header,
            text=title,
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        self._fields = {}

        # Name
        self._add_field(scroll, "服务器名称", "name", profile.name if profile else "")

        # Host
        self._add_field(scroll, "主机地址", "host", profile.host if profile else "")

        # Port
        self._add_field(scroll, "端口", "port", str(profile.port) if profile else "22")

        # Username
        self._add_field(scroll, "用户名", "username", profile.username if profile else "root")

        # Auth type
        self._add_field(scroll, "认证方式", "auth_type", ["key", "password"], "combo")
        if profile:
            self._fields["auth_type"][0].set(profile.auth_type)
        else:
            self._fields["auth_type"][0].set("key")
        self._fields["auth_type"][0].configure(command=self._on_auth_type_change)

        # Private key path
        key_row = ctk.CTkFrame(scroll, fg_color="transparent")
        self._key_row = key_row
        key_row.pack(fill="x", pady=5)
        key_label = ctk.CTkLabel(
            key_row,
            text="私钥路径",
            width=128,
            anchor="w",
            text_color=COLORS["muted"],
            font=font(12),
        )
        key_label.pack(side="left")
        self._key_entry = ctk.CTkEntry(key_row, width=180, **input_style())
        if profile and profile.private_key_path:
            self._key_entry.insert(0, profile.private_key_path)
        self._key_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        key_browse = ctk.CTkButton(
            key_row,
            text="浏览",
            width=58,
            command=self._browse_key,
            **button_style("secondary", compact=True),
        )
        key_browse.pack(side="left")
        self._field_layouts["private_key_path"] = {
            "label": key_label,
            "field": self._key_entry,
            "buttons": [key_browse],
        }

        # Key passphrase
        self._add_field(scroll, "私钥密码", "key_passphrase", "", "masked")

        # Password
        self._add_field(scroll, "登录密码", "password", "", "masked")

        self._auth_hint = ctk.CTkLabel(
            scroll,
            text="",
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
            justify="left",
        )
        self._auth_hint.pack(fill="x", pady=(0, 6), padx=(128, 0))
        bind_wraplength(scroll, self._auth_hint, padding=160, min_width=220, max_width=620)

        # Remote config directories
        self._add_field(
            scroll,
            "Claude目录",
            "remote_claude_dir",
            profile.remote_claude_dir if profile and profile.remote_claude_dir else "~/.claude",
        )
        self._add_field(
            scroll,
            "Codex目录",
            "remote_codex_dir",
            profile.remote_codex_dir if profile and profile.remote_codex_dir else "~/.codex",
        )

        # Test button
        test_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        test_frame.pack(fill="x", pady=(12, 6))
        self._test_btn = ctk.CTkButton(
            test_frame,
            text="测试连接",
            width=110,
            command=self._test_connection,
            **button_style("accent"),
        )
        self._test_btn.pack()

        self._test_result = ctk.CTkLabel(
            scroll,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._test_result.pack(fill="x", pady=(0, 5))
        bind_wraplength(scroll, self._test_result, padding=48, min_width=260, max_width=620)

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=84,
            command=self.destroy,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame,
            text="保存",
            width=84,
            command=self._save,
            **button_style("primary"),
        ).pack(side="right")

        self.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._schedule_responsive_layout(delay_ms=0)
        center_window(self, master)
        self._on_auth_type_change(self._get_value("auth_type"))

    def destroy(self):
        self._destroyed = True
        super().destroy()

    def _add_field(self, parent, label, key, value="", field_type="entry"):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        self._field_rows[key] = row
        label_widget = ctk.CTkLabel(
            row,
            text=label,
            width=128,
            anchor="w",
            text_color=COLORS["muted"],
            font=font(12),
        )
        label_widget.pack(side="left")

        if field_type == "entry":
            widget = ctk.CTkEntry(row, width=180, **input_style())
            widget.insert(0, str(value))
            widget.pack(side="left", fill="x", expand=True)
        elif field_type == "masked":
            widget = MaskedEntry(row, width=180)
            if value:
                widget.set(str(value))
            widget.pack(side="left", fill="x", expand=True)
        elif field_type == "combo":
            widget = ctk.CTkComboBox(
                row,
                values=value,
                width=180,
                **combo_style(),
            )
            widget.pack(side="left", fill="x", expand=True)

        self._fields[key] = (widget, field_type)
        self._field_layouts[key] = {
            "label": label_widget,
            "field": widget,
            "buttons": [],
        }
        return widget

    def _logical_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_window_scaling())
        except (AttributeError, TypeError, ValueError):
            scaling = 1.0
        return max(1, round(width / scaling)) if scaling > 0 else max(1, width)

    def _schedule_responsive_layout(self, _event=None, delay_ms: int = 20) -> None:
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
        stacked = self._logical_width() < 620
        if stacked == self._responsive_stacked:
            return
        self._responsive_stacked = stacked
        for layout in self._field_layouts.values():
            label = layout["label"]
            field = layout["field"]
            buttons = list(layout["buttons"])
            for widget in (label, field, *buttons):
                widget.pack_forget()
            if stacked:
                label.configure(width=0)
                label.pack(side="top", fill="x", anchor="w")
                field.pack(side="top", fill="x", expand=True, pady=(3, 0))
                for index, button in enumerate(buttons):
                    button.pack(side="left", fill="x", expand=True, pady=(5, 0), padx=(0 if index == 0 else 5, 0))
            else:
                label.configure(width=128)
                label.pack(side="left")
                field.pack(side="left", fill="x", expand=True, padx=(0, 5) if buttons else 0)
                for index, button in enumerate(buttons):
                    button.pack(side="left", padx=(0, 5) if index < len(buttons) - 1 else 0)
        self._auth_hint.pack_configure(padx=(0, 0) if stacked else (128, 0))

    def _pack_after(self, row, after_row) -> None:
        if row is None:
            return
        try:
            row.pack(fill="x", pady=5, after=after_row)
        except Exception:
            row.pack(fill="x", pady=5)

    def _on_auth_type_change(self, _value=None):
        auth_type = self._get_value("auth_type") if "auth_type" in self._fields else "key"
        auth_row = self._field_rows.get("auth_type")
        key_passphrase_row = self._field_rows.get("key_passphrase")
        password_row = self._field_rows.get("password")

        if auth_type == "password":
            if self._key_row:
                self._key_row.pack_forget()
            if key_passphrase_row:
                key_passphrase_row.pack_forget()
            self._pack_after(password_row, auth_row)
            self._auth_hint.configure(
                text="密码认证适合 root/password 服务器；密码会通过系统凭据管理器本机保存。"
            )
            return

        self._pack_after(self._key_row, auth_row)
        self._pack_after(key_passphrase_row, self._key_row)
        if password_row:
            password_row.pack_forget()
        self._auth_hint.configure(text="密钥认证需要填写私钥路径；私钥密码可留空，已有值会在保存时保留。")

    def _get_value(self, key):
        widget, ftype = self._fields[key]
        if ftype == "masked":
            return widget.get()
        else:
            return widget.get()

    def _browse_key(self):
        path = filedialog.askopenfilename(
            title="选择私钥文件",
            filetypes=[("SSH Keys", "id_rsa id_ed25519 *.pem"), ("All Files", "*.*")]
        )
        if path:
            self._key_entry.delete(0, "end")
            self._key_entry.insert(0, path)

    def _test_connection(self):
        if self._test_busy:
            return
        try:
            data = self._collect_data()
            profile = self._build_profile(data)
        except Exception as e:
            self._test_result.configure(text=f"测试失败: {e}", text_color=COLORS["danger"])
            return

        self._set_test_busy(True, "正在测试连接...")

        def run_test():
            from core.ssh_manager import ssh_manager

            try:
                ssh_manager.disconnect(profile.name)
                success, message = ssh_manager.test_connection(profile)
            except Exception as e:
                success = False
                message = f"测试失败: {e}"
            self._safe_after(lambda: self._finish_test(success, message))

        try:
            threading.Thread(target=run_test, name="ssh-editor-test", daemon=True).start()
        except Exception as exc:
            self._set_test_busy(False)
            self._test_result.configure(
                text=f"无法启动连接测试: {exc}",
                text_color=COLORS["danger"],
            )

    def _set_test_busy(self, busy: bool, message: str | None = None) -> None:
        self._test_busy = busy
        self._test_btn.configure(
            state="disabled" if busy else "normal",
            text="测试中..." if busy else "测试连接",
        )
        if message:
            self._test_result.configure(text=message, text_color=COLORS["muted"])

    def _finish_test(self, success: bool, message: str) -> None:
        if not self.winfo_exists():
            return
        self._set_test_busy(False)
        self._test_result.configure(
            text=message,
            text_color=COLORS["success"] if success else COLORS["danger"],
        )

    def _safe_after(self, callback) -> bool:
        return run_on_ui_thread(self, callback)

    def _collect_data(self) -> dict:
        return {
            "name": self._get_value("name"),
            "host": self._get_value("host"),
            "port": self._get_value("port"),
            "username": self._get_value("username"),
            "auth_type": self._get_value("auth_type"),
            "private_key_path": self._key_entry.get(),
            "key_passphrase": self._get_value("key_passphrase"),
            "password": self._get_value("password"),
            "remote_claude_dir": self._get_value("remote_claude_dir"),
            "remote_codex_dir": self._get_value("remote_codex_dir"),
        }

    def _build_profile(self, data: dict) -> SSHProfile:
        from core.ssh_profile_builder import build_ssh_profile_from_data

        return build_ssh_profile_from_data(data, self._profile)

    def _save(self):
        try:
            data = self._collect_data()
            profile = self._build_profile(data)

            if self._on_save:
                self._on_save(profile, self._profile)
            self.destroy()
        except Exception as e:
            self._test_result.configure(text=f"保存失败: {e}", text_color=COLORS["danger"])
