import threading
import customtkinter as ctk
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.dialogs.ssh_editor import SSHEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from core import profile_manager, ssh_manager, sync_manager, remote_auto_continue
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font


class SSHTab(ctk.CTkScrollableFrame):
    """Tab for managing SSH servers and syncing configs."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._sync_frame = None
        self._sync_kind_combo = None
        self._profile_combo = None
        self._sync_status_label = None
        self._ssh_busy = False
        self._remote_auto_provider_combo = None
        self._remote_auto_status_label = None
        self._remote_auto_buttons = []
        self._remote_auto_busy = False
        self._sync_kind_options = {
            "Claude API": "claude_api",
            "Claude 账号": "claude_account",
            "Codex API": "codex_api",
            "Codex 账号": "codex_account",
        }
        self._remote_auto_options = {
            "Claude": "claude",
            "Codex": "codex",
            "Claude + Codex": "all",
        }
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
            text="连接远程服务器，并把本机 API 或账号配置推送到远程环境",
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
        sync_controls.grid_columnconfigure(2, weight=1)

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

        ctk.CTkButton(
            sync_controls,
            text="推送当前生效",
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

        ctk.CTkLabel(
            sync_controls,
            text="推送内容",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._sync_kind_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._sync_kind_options.keys()),
            width=132,
            command=lambda _value: self._refresh_sync_profile_combo(),
            **combo_style(),
        )
        self._sync_kind_combo.grid(row=1, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._sync_kind_combo.set("Claude API")

        self._profile_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            **combo_style(),
        )
        self._profile_combo.grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(10, 0))

        ctk.CTkButton(
            sync_controls,
            text="推送所选",
            width=126,
            command=self._sync_selected,
            **button_style("primary"),
        ).grid(row=1, column=3, sticky="e", pady=(10, 0))

        self._sync_status_label = ctk.CTkLabel(
            sync_controls,
            text="就绪",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._sync_status_label.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, self._sync_status_label, padding=20)

        auto_header = ctk.CTkFrame(self, fg_color="transparent")
        auto_header.pack(fill="x", padx=14, pady=(4, 5))
        ctk.CTkLabel(
            auto_header,
            text="远端自动续跑",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            auto_header,
            text="把本机 Claude/Codex 自动续跑设置安装到已连接的 SSH 服务器",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(10, 0))

        auto_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        auto_frame.pack(fill="x", padx=14, pady=(0, 12))
        auto_controls = ctk.CTkFrame(auto_frame, fg_color="transparent")
        auto_controls.pack(fill="x", padx=14, pady=14)
        auto_controls.grid_columnconfigure(1, weight=1)
        auto_controls.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(
            auto_controls,
            text="安装对象",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._remote_auto_provider_combo = ctk.CTkComboBox(
            auto_controls,
            values=list(self._remote_auto_options.keys()),
            width=160,
            **combo_style(),
        )
        self._remote_auto_provider_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self._remote_auto_provider_combo.set("Claude + Codex")

        check_button = ctk.CTkButton(
            auto_controls,
            text="检查",
            width=78,
            command=self._check_remote_auto_continue,
            **button_style("secondary"),
        )
        check_button.grid(row=0, column=2, sticky="e", padx=(0, 8))
        install_button = ctk.CTkButton(
            auto_controls,
            text="安装/修复",
            width=102,
            command=self._install_remote_auto_continue,
            **button_style("primary"),
        )
        install_button.grid(row=0, column=3, sticky="e", padx=(0, 8))
        pause_button = ctk.CTkButton(
            auto_controls,
            text="暂停",
            width=78,
            command=self._pause_remote_auto_continue,
            **button_style("warning"),
        )
        pause_button.grid(row=0, column=4, sticky="e", padx=(0, 8))
        uninstall_button = ctk.CTkButton(
            auto_controls,
            text="卸载",
            width=78,
            command=self._uninstall_remote_auto_continue,
            **button_style("danger"),
        )
        uninstall_button.grid(row=0, column=5, sticky="e")
        self._remote_auto_buttons = [check_button, install_button, pause_button, uninstall_button]

        self._remote_auto_status_label = ctk.CTkLabel(
            auto_controls,
            text="未检查。安装时会同步本机自动续跑设置，并要求远端具备 sh 和 Python 3.6+。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._remote_auto_status_label.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        bind_wraplength(auto_controls, self._remote_auto_status_label, padding=20)

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
                remote_dirs = []
                if getattr(p, "remote_claude_dir", None):
                    remote_dirs.append(f"Claude: {p.remote_claude_dir}")
                if getattr(p, "remote_codex_dir", None):
                    remote_dirs.append(f"Codex: {p.remote_codex_dir}")
                if remote_dirs:
                    info.append("远端目录: " + "  |  ".join(remote_dirs))

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
        current_server = self._server_combo.get()
        self._server_combo.configure(values=server_names if server_names else ["(无)"])
        if server_names:
            self._server_combo.set(current_server if current_server in server_names else server_names[0])
        else:
            self._server_combo.set("(无)")
        self._refresh_sync_profile_combo()

    def _create_server(self):
        def on_save(profile, _):
            ssh_manager.ssh_manager.disconnect(profile.name)
            profile_manager.save_ssh_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建: {profile.name}")
            self.refresh()

        SSHEditorDialog(self.winfo_toplevel(), title="新建 SSH 服务器", on_save=on_save)

    def _edit_server(self, name):
        profiles = profile_manager.list_ssh_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(new_profile, old_profile):
            previous_name = old_profile.name if old_profile else None
            if previous_name:
                ssh_manager.ssh_manager.disconnect(previous_name)
            ssh_manager.ssh_manager.disconnect(new_profile.name)
            profile_manager.save_ssh_profile(new_profile, previous_name=previous_name)
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

    def _set_sync_status(self, message: str, severity: str = "info"):
        if not self._sync_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._sync_status_label.configure(text=message, text_color=color)

    def _run_ssh_task(self, busy_message: str, worker, on_done=None, refresh: bool = False):
        if self._ssh_busy:
            show_toast(self.winfo_toplevel(), "SSH 操作正在进行中，请稍等", is_error=True)
            return

        self._ssh_busy = True
        self._set_sync_status(busy_message)

        def run():
            try:
                payload = {"ok": True, "result": worker(), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._ssh_busy = False
                if on_done:
                    on_done(payload)
                elif payload["ok"]:
                    message = str(payload["result"] or "操作完成")
                    self._set_sync_status(message, "success")
                    show_toast(self.winfo_toplevel(), message)
                else:
                    message = f"操作失败: {payload['error']}"
                    self._set_sync_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                if refresh:
                    self.refresh()

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _connect(self, name):
        profiles = profile_manager.list_ssh_profiles()
        profile = next((p for p in profiles if p.name == name), None)
        if not profile:
            show_toast(self.winfo_toplevel(), f"未找到服务器: {name}", is_error=True)
            return

        self._run_ssh_task(
            f"正在连接 {profile.host}:{profile.port}...",
            lambda: (ssh_manager.ssh_manager.connect(profile), f"已连接到 {profile.host}")[1],
            refresh=True,
        )

    def _disconnect(self, name):
        ssh_manager.ssh_manager.disconnect(name)
        show_toast(self.winfo_toplevel(), f"已断开连接: {name}")
        self.refresh()

    def _sync_current(self):
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        self._run_ssh_task(
            f"正在推送当前生效配置到 {server_name}...",
            lambda: sync_manager.sync_all_to_server(server_name),
        )

    def _selected_sync_kind(self) -> str:
        if not self._sync_kind_combo:
            return "claude_api"
        return self._sync_kind_options.get(self._sync_kind_combo.get(), "claude_api")

    def _profile_names_for_kind(self, kind: str) -> list[str]:
        if kind == "claude_api":
            return [p.name for p in profile_manager.list_switchable_claude_profiles()]
        if kind == "claude_account":
            return [p.name for p in profile_manager.list_claude_account_profiles()]
        if kind == "codex_api":
            return [p.name for p in profile_manager.list_switchable_codex_profiles()]
        if kind == "codex_account":
            return [p.name for p in profile_manager.list_codex_account_profiles()]
        return []

    def _refresh_sync_profile_combo(self):
        if not self._profile_combo:
            return
        current_profile = self._profile_combo.get()
        profile_names = self._profile_names_for_kind(self._selected_sync_kind())
        self._profile_combo.configure(values=profile_names if profile_names else ["(无)"])
        if profile_names:
            self._profile_combo.set(current_profile if current_profile in profile_names else profile_names[0])
        else:
            self._profile_combo.set("(无)")

    def _sync_selected(self):
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        profile_name = self._profile_combo.get()
        if profile_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择要推送的 API 或账号", is_error=True)
            return

        kind = self._selected_sync_kind()

        def do_sync():
            self._run_ssh_task(
                f"正在推送 {profile_name} 到 {server_name}...",
                lambda: sync_manager.sync_selected_to_server(server_name, kind, profile_name),
            )

        if kind in {"claude_account", "codex_account"}:
            ConfirmDialog(
                self.winfo_toplevel(),
                title="确认推送账号",
                message=f"将把 \"{profile_name}\" 的官方登录凭据写入服务器 \"{server_name}\"。\n确定继续吗？",
                on_confirm=do_sync,
            )
            return

        do_sync()

    def _selected_remote_auto_targets(self) -> list[str]:
        if not self._remote_auto_provider_combo:
            return ["claude", "codex"]
        selected = self._remote_auto_options.get(self._remote_auto_provider_combo.get(), "all")
        if selected == "all":
            return ["claude", "codex"]
        return [selected]

    def _selected_server_name(self) -> str | None:
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return None
        return server_name

    def _set_remote_auto_status(self, message: str, is_error: bool = False, severity: str | None = None):
        if self._remote_auto_status_label:
            level = severity or ("error" if is_error else "info")
            color = {
                "error": COLORS["danger"],
                "warning": COLORS["warning"],
            }.get(level, COLORS["muted"])
            self._remote_auto_status_label.configure(
                text=message,
                text_color=color,
            )

    def _set_remote_auto_busy(self, busy: bool, message: str | None = None):
        self._remote_auto_busy = busy
        state = "disabled" if busy else "normal"
        for button in self._remote_auto_buttons:
            try:
                button.configure(state=state)
            except Exception:
                pass
        if self._remote_auto_provider_combo:
            try:
                self._remote_auto_provider_combo.configure(state=state)
            except Exception:
                pass
        if message:
            self._set_remote_auto_status(message)

    def _run_remote_auto_task(self, busy_message: str, worker, on_done):
        if self._remote_auto_busy:
            show_toast(self.winfo_toplevel(), "远端自动续跑操作正在进行中，请稍等", is_error=True)
            return

        self._set_remote_auto_busy(True, busy_message)

        def run():
            try:
                payload = worker()
            except Exception as e:
                payload = {
                    "results": [],
                    "statuses": [],
                    "failures": [str(e)],
                }

            def finish():
                if not self.winfo_exists():
                    return
                self._set_remote_auto_busy(False)
                on_done(payload)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _summarize_remote_auto_status(self, statuses, failures: list[str] | None = None) -> str:
        parts = [status.summary() for status in statuses]
        if failures:
            parts.append("失败: " + "；".join(failures))
        return " | ".join(parts) if parts else "没有可显示的远端自动续跑状态"

    def _collect_remote_auto_statuses(self, server_name: str, targets: list[str]) -> tuple[list, list[str]]:
        statuses = []
        failures = []
        for provider in targets:
            try:
                statuses.append(remote_auto_continue.get_remote_auto_continue_status(server_name, provider))
            except Exception as e:
                failures.append(f"{provider}: {e}")
        return statuses, failures

    def _show_remote_auto_result(self, payload, default_message: str, expect_ready: bool = False):
        statuses = payload.get("statuses", [])
        failures = payload.get("failures", [])
        results = payload.get("results", [])
        message = self._summarize_remote_auto_status(statuses, failures)
        has_not_ready = expect_ready and any(not status.ready for status in statuses)
        severity = "error" if failures else "warning" if has_not_ready else "info"
        self._set_remote_auto_status(message, severity=severity)
        toast_message = " | ".join(results)
        if failures:
            toast_message = (toast_message + " | " if toast_message else "") + "失败: " + "；".join(failures)
        show_toast(self.winfo_toplevel(), toast_message or default_message, is_error=bool(failures))

    def _check_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            statuses, failures = self._collect_remote_auto_statuses(server_name, targets)
            return {"statuses": statuses, "failures": failures, "results": []}

        self._run_remote_auto_task(
            f"正在检查 {server_name} 的远端自动续跑状态...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端自动续跑检查完成", expect_ready=True),
        )

    def _install_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            results = []
            failures = []
            for provider in targets:
                try:
                    results.append(remote_auto_continue.install_remote_auto_continue(server_name, provider))
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在安装/修复 {server_name} 的远端自动续跑...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端自动续跑安装完成", expect_ready=True),
        )

    def _pause_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            results = []
            failures = []
            for provider in targets:
                try:
                    results.append(remote_auto_continue.pause_remote_auto_continue(server_name, provider))
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在暂停 {server_name} 的远端自动续跑...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端自动续跑已暂停"),
        )

    def _uninstall_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()
        target_label = "、".join("Claude" if p == "claude" else "Codex" for p in targets)

        def do_uninstall():
            def worker():
                results = []
                failures = []
                for provider in targets:
                    try:
                        results.append(remote_auto_continue.uninstall_remote_auto_continue(server_name, provider))
                    except Exception as e:
                        failures.append(f"{provider}: {e}")
                statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
                failures.extend(status_failures)
                return {"statuses": statuses, "failures": failures, "results": results}

            self._run_remote_auto_task(
                f"正在卸载 {server_name} 的远端自动续跑...",
                worker,
                lambda payload: self._show_remote_auto_result(payload, "远端自动续跑已卸载"),
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="卸载远端自动续跑",
            message=f"确定要从服务器 \"{server_name}\" 卸载 {target_label} 自动续跑吗？\n这会移除远端 hook、脚本、设置和指导块。",
            on_confirm=do_uninstall,
        )

    def _pull_from_server(self):
        server_name = self._server_combo.get()
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        def worker():
            results = []
            failures = []
            for label, puller in [
                ("Claude", sync_manager.pull_claude_from_server),
                ("Codex", sync_manager.pull_codex_from_server),
            ]:
                try:
                    results.append(puller(server_name))
                except Exception as e:
                    failures.append(f"{label}: {e}")
            return results, failures

        def done(payload):
            if not payload["ok"]:
                message = f"拉取失败: {payload['error']}"
                self._set_sync_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            results, failures = payload["result"]
            if results and failures:
                message = " | ".join(results) + " | 部分失败: " + "；".join(failures)
                self._set_sync_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
            elif results:
                message = " | ".join(results)
                self._set_sync_status(message, "success")
                show_toast(self.winfo_toplevel(), message)
            else:
                message = "拉取失败: " + "；".join(failures)
                self._set_sync_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
            self.refresh()

        self._run_ssh_task(
            f"正在从 {server_name} 拉取配置...",
            worker,
            on_done=done,
        )
