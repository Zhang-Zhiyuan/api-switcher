import customtkinter as ctk
from tkinter import filedialog
from ui.widgets.masked_entry import MaskedEntry
from models.profile import SSHProfile
from ui.theme import COLORS, button_style, center_window, combo_style, font, input_style


class SSHEditorDialog(ctk.CTkToplevel):
    """Dialog for creating or editing an SSH server profile."""

    def __init__(self, master, title="编辑 SSH 服务器", profile: SSHProfile = None, on_save=None):
        super().__init__(master)
        self.title(title)
        self.geometry("620x620")
        self.minsize(540, 500)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_save = on_save
        self._profile = profile

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

        # Private key path
        key_row = ctk.CTkFrame(scroll, fg_color="transparent")
        key_row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            key_row,
            text="私钥路径",
            width=128,
            anchor="w",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left")
        self._key_entry = ctk.CTkEntry(key_row, width=296, **input_style())
        if profile and profile.private_key_path:
            self._key_entry.insert(0, profile.private_key_path)
        self._key_entry.pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            key_row,
            text="浏览",
            width=58,
            command=self._browse_key,
            **button_style("secondary", compact=True),
        ).pack(side="left")

        # Key passphrase
        self._add_field(scroll, "私钥密码", "key_passphrase", "", "masked")

        # Password
        self._add_field(scroll, "登录密码", "password", "", "masked")

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
        )
        self._test_result.pack(pady=(0, 5))

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

        center_window(self, master)

    def _add_field(self, parent, label, key, value="", field_type="entry"):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(
            row,
            text=label,
            width=128,
            anchor="w",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left")

        if field_type == "entry":
            widget = ctk.CTkEntry(row, width=360, **input_style())
            widget.insert(0, str(value))
            widget.pack(side="left", fill="x", expand=True)
        elif field_type == "masked":
            widget = MaskedEntry(row, width=360)
            if value:
                widget.set(str(value))
            widget.pack(side="left", fill="x", expand=True)
        elif field_type == "combo":
            widget = ctk.CTkComboBox(
                row,
                values=value,
                width=360,
                **combo_style(),
            )
            widget.pack(side="left", fill="x", expand=True)

        self._fields[key] = (widget, field_type)
        return widget

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
        self._test_result.configure(text="正在测试连接...", text_color=COLORS["muted"])
        self.update()

        try:
            data = self._collect_data()
            profile = self._build_profile(data)

            from core.ssh_manager import ssh_manager
            success, message = ssh_manager.test_connection(profile)

            if success:
                self._test_result.configure(text=message, text_color=COLORS["success"])
            else:
                self._test_result.configure(text=message, text_color=COLORS["danger"])
        except Exception as e:
            self._test_result.configure(text=f"测试失败: {e}", text_color=COLORS["danger"])

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
        }

    def _build_profile(self, data: dict) -> SSHProfile:
        from core import security

        password_ref = None
        key_passphrase_ref = None

        if data["auth_type"] == "password" and data["password"]:
            password_ref = f"ssh:{data['name']}:password"
            security.set_secret(password_ref, data["password"])

        if data["key_passphrase"]:
            key_passphrase_ref = f"ssh:{data['name']}:key_passphrase"
            security.set_secret(key_passphrase_ref, data["key_passphrase"])

        return SSHProfile(
            name=data["name"],
            host=data["host"],
            port=int(data["port"]),
            username=data["username"],
            auth_type=data["auth_type"],
            password_ref=password_ref,
            private_key_path=data["private_key_path"] or None,
            private_key_passphrase_ref=key_passphrase_ref,
        )

    def _save(self):
        try:
            data = self._collect_data()
            profile = self._build_profile(data)

            if self._on_save:
                self._on_save(profile, self._profile)
            self.destroy()
        except Exception as e:
            self._test_result.configure(text=f"保存失败: {e}", text_color=COLORS["danger"])
