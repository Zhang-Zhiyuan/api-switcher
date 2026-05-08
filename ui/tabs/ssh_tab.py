import customtkinter as ctk
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.dialogs.ssh_editor import SSHEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from core import profile_manager, ssh_manager, sync_manager
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font


class SSHTab(ctk.CTkScrollableFrame):
    """Tab for managing SSH servers and syncing configs."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._sync_frame = None
        self._build_ui()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="SSH 服务器管理",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="连接远程服务器，并同步当前 Claude 与 Codex 配置",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="+ 新建服务器",
            width=126,
            command=self._create_server,
            **button_style("primary"),
        ).pack(side="right")

        # Server cards
        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="x", padx=14, pady=(0, 8))

        # Sync panel
        sync_header = ctk.CTkFrame(self, fg_color="transparent")
        sync_header.pack(fill="x", padx=14, pady=(8, 5))
        ctk.CTkLabel(
            sync_header,
            text="配置同步",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(side="left")

        self._sync_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        self._sync_frame.pack(fill="x", padx=14, pady=(0, 12))

        # Sync controls
        sync_controls = ctk.CTkFrame(self._sync_frame, fg_color="transparent")
        sync_controls.pack(fill="x", padx=14, pady=14)
        sync_controls.grid_columnconfigure(1, weight=1)

        # Server selector
        ctk.CTkLabel(
            sync_controls,
            text="目标服务器",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._server_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            **combo_style(),
        )
        self._server_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))

        # Sync buttons
        ctk.CTkButton(
            sync_controls,
            text="同步当前配置",
            width=126,
            command=self._sync_current,
            **button_style("primary"),
        ).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ctk.CTkButton(
            sync_controls,
            text="从服务器拉取",
            width=126,
            command=self._pull_from_server,
            **button_style("accent"),
        ).grid(row=0, column=3, sticky="e")

        self.refresh()

    def refresh(self):
        if not self._cards_frame:
            return

        # Clear cards
        for w in self._cards_frame.winfo_children():
            w.destroy()

        profiles = profile_manager.list_ssh_profiles()
        active = profile_manager.get_active_ssh_name()

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无 SSH 服务器",
                "添加一台服务器后，可以把本机配置同步到远程环境。",
                "新建服务器",
                self._create_server,
            ).pack(fill="x", pady=(12, 4))
        else:
            for p in profiles:
                is_active = p.name == active
                is_connected = ssh_manager.ssh_manager.is_connected(p.name)
                status = "已连接" if is_connected else "未连接"
                status_color = COLORS["success"] if is_connected else COLORS["muted_soft"]

                info = [
                    f"地址: {p.host}:{p.port}  |  用户: {p.username}  |  认证: {p.auth_type}",
                    f"状态: {status}",
                ]

                card_frame = ctk.CTkFrame(
                    self._cards_frame,
                    **card_frame_kwargs(COLORS["success"] if is_connected else COLORS["border_soft"]),
                )
                card_frame.pack(fill="x", pady=5)

                # Header
                top = ctk.CTkFrame(card_frame, fg_color="transparent")
                top.pack(fill="x", padx=14, pady=(12, 4))

                indicator = ctk.CTkLabel(top, text="●", text_color=status_color, font=font(15))
                indicator.pack(side="left")

                name_label = ctk.CTkLabel(top, text=p.name, text_color=COLORS["text"], font=font(15, "bold"))
                name_label.pack(side="left", padx=(7, 0))

                if is_active:
                    ctk.CTkLabel(
                        top,
                        text="当前",
                        fg_color=COLORS["primary"],
                        corner_radius=4,
                        text_color=COLORS["text"],
                        font=font(11, "bold"),
                        padx=7,
                        pady=1,
                    ).pack(side="left", padx=(8, 0))

                # Info
                info_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
                info_frame.pack(fill="x", padx=14, pady=(0, 8))
                for line in info:
                    lbl = ctk.CTkLabel(
                        info_frame,
                        text=line,
                        text_color=COLORS["muted"],
                        font=font(12),
                        anchor="w",
                        justify="left",
                    )
                    lbl.pack(fill="x")
                    bind_wraplength(info_frame, lbl, padding=4)

                # Buttons
                btn_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
                btn_frame.pack(fill="x", padx=14, pady=(0, 12))

                if is_connected:
                    ctk.CTkButton(
                        btn_frame,
                        text="断开",
                        width=62,
                        command=lambda n=p.name: self._disconnect(n),
                        **button_style("danger", compact=True),
                    ).pack(side="left", padx=(0, 6))
                else:
                    ctk.CTkButton(
                        btn_frame,
                        text="连接",
                        width=62,
                        command=lambda n=p.name: self._connect(n),
                        **button_style("primary", compact=True),
                    ).pack(side="left", padx=(0, 6))

                ctk.CTkButton(
                    btn_frame,
                    text="编辑",
                    width=58,
                    command=lambda n=p.name: self._edit_server(n),
                    **button_style("secondary", compact=True),
                ).pack(side="left", padx=(0, 6))

                ctk.CTkButton(
                    btn_frame,
                    text="删除",
                    width=58,
                    command=lambda n=p.name: self._delete_server(n),
                    **button_style("danger", compact=True),
                ).pack(side="left")

        # Update server combo
        server_names = [p.name for p in profiles]
        self._server_combo.configure(values=server_names if server_names else ["(无)"])
        if server_names:
            self._server_combo.set(server_names[0])
        else:
            self._server_combo.set("(无)")

    def _create_server(self):
        def on_save(profile, _):
            profile_manager.save_ssh_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建: {profile.name}")
            self.refresh()

        SSHEditorDialog(self.winfo_toplevel(), title="新建 SSH 服务器", on_save=on_save)

    def _edit_server(self, name):
        profiles = profile_manager.list_ssh_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(new_profile, _):
            profile_manager.save_ssh_profile(new_profile)
            show_toast(self.winfo_toplevel(), f"已保存: {new_profile.name}")
            self.refresh()

        SSHEditorDialog(self.winfo_toplevel(), title="编辑 SSH 服务器",
                        profile=profile, on_save=on_save)

    def _delete_server(self, name):
        def do_delete():
            ssh_manager.ssh_manager.disconnect(name)
            profile_manager.delete_ssh_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除: {name}")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="删除服务器",
                      message=f"确定要删除 \"{name}\" 吗？\n关联的密钥也会被清除。",
                      on_confirm=do_delete)

    def _connect(self, name):
        try:
            profiles = profile_manager.list_ssh_profiles()
            profile = next((p for p in profiles if p.name == name), None)
            if profile:
                ssh_manager.ssh_manager.connect(profile)
                show_toast(self.winfo_toplevel(), f"已连接到 {profile.host}")
                self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"连接失败: {e}", is_error=True)

    def _disconnect(self, name):
        ssh_manager.ssh_manager.disconnect(name)
        show_toast(self.winfo_toplevel(), f"已断开连接: {name}")
        self.refresh()

    def _sync_current(self):
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        try:
            message = sync_manager.sync_all_to_server(server_name)
            show_toast(self.winfo_toplevel(), message)
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"同步失败: {e}", is_error=True)

    def _pull_from_server(self):
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        try:
            msg1 = sync_manager.pull_claude_from_server(server_name)
            msg2 = sync_manager.pull_codex_from_server(server_name)
            show_toast(self.winfo_toplevel(), f"{msg1} | {msg2}")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"拉取失败: {e}", is_error=True)
