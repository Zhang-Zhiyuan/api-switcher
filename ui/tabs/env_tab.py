import threading
import customtkinter as ctk

from core import persistent_env, profile_manager, ssh_manager
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font
from ui.widgets.persistent_env_control import PersistentEnvControl
from ui.widgets.toast import show_toast


class EnvTab(ctk.CTkScrollableFrame):
    """Dedicated tab for persistent local and SSH user environment variables."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._ssh_busy = False
        self._server_combo = None
        self._server_status_label = None
        self._local_env_control = None
        self._remote_env_control = None
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            header,
            text="环境变量",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkLabel(
            header,
            text="一处管理 HF_TOKEN、API Key、Google Drive/Gemini、代理等持久环境变量",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(header, subtitle, padding=24, min_width=260, max_width=760)

        self._local_env_control = PersistentEnvControl(
            self,
            title="本机 Windows 用户（默认 HF_TOKEN）",
            status_text="变量名默认 HF_TOKEN，也可下拉选择 OpenAI、Google Drive、代理等变量；新打开的 PowerShell/CMD/终端会自动读取。",
            write_label="写入本机用户",
            delete_label="删除本机变量",
            on_write=self._write_local_env,
            on_delete=self._delete_local_env,
        )
        self._local_env_control.pack(fill="x", padx=14, pady=(0, 12))

        target_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        target_frame.pack(fill="x", padx=14, pady=(0, 10))
        target_grid = ctk.CTkFrame(target_frame, fg_color="transparent")
        target_grid.pack(fill="x", padx=14, pady=14)
        target_grid.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            target_grid,
            text="SSH 目标服务器",
            text_color=COLORS["muted"],
            width=108,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._server_combo = ctk.CTkComboBox(target_grid, width=260, **combo_style())
        self._server_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        ctk.CTkButton(
            target_grid,
            text="刷新服务器",
            width=108,
            command=self._refresh_server_combo,
            **button_style("secondary"),
        ).grid(row=0, column=2, sticky="e")

        self._server_status_label = ctk.CTkLabel(
            target_grid,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._server_status_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        bind_wraplength(target_grid, self._server_status_label, padding=20)

        self._remote_env_control = PersistentEnvControl(
            self,
            title="SSH 登录用户（默认 HF_TOKEN）",
            status_text="选择上方 SSH 服务器后，写入对应登录用户 HOME；变量名默认 HF_TOKEN，不修改系统级 /etc/environment。",
            write_label="写入 SSH 用户",
            delete_label="删除 SSH 变量",
            on_write=self._write_remote_env,
            on_delete=self._delete_remote_env,
        )
        self._remote_env_control.pack(fill="x", padx=14, pady=(0, 12))

        self.refresh()

    def refresh(self):
        if self._local_env_control:
            self._local_env_control.refresh_sources()
        if self._remote_env_control:
            self._remote_env_control.refresh_sources()
        self._refresh_server_combo()

    def _write_local_env(self, control):
        try:
            result = persistent_env.set_local_user_env(control.env_update())
            message = f"{result.summary()}。{result.details}"
            control.set_status(message, "success")
            show_toast(self.winfo_toplevel(), result.summary())
        except Exception as e:
            message = f"写入失败: {e}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)

    def _delete_local_env(self, control):
        try:
            result = persistent_env.delete_local_user_env(control.env_names())
            message = f"{result.summary()}。{result.details}"
            control.set_status(message, "warning")
            show_toast(self.winfo_toplevel(), result.summary())
        except Exception as e:
            message = f"删除失败: {e}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)

    def _refresh_server_combo(self):
        if not self._server_combo:
            return
        profiles = profile_manager.list_ssh_profiles()
        names = [profile.name for profile in profiles]
        current = self._server_combo.get()
        self._server_combo.configure(values=names if names else ["(暂无 SSH 服务器)"])
        if names:
            self._server_combo.set(current if current in names else names[0])
            self._set_server_status(f"已找到 {len(names)} 台 SSH 服务器；写入前会自动连接所选服务器。")
        else:
            self._server_combo.set("(暂无 SSH 服务器)")
            self._set_server_status("暂无 SSH 服务器；请先在“SSH 服务器”页添加。", "warning")

    def _set_server_status(self, message: str, severity: str = "info"):
        if not self._server_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._server_status_label.configure(text=message, text_color=color)

    def _selected_server_name(self) -> str | None:
        if not self._server_combo:
            return None
        server_name = self._server_combo.get().strip()
        if not server_name or server_name.startswith("("):
            message = "请先选择 SSH 服务器"
            self._set_server_status(message, "error")
            if self._remote_env_control:
                self._remote_env_control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return None
        return server_name

    def _run_ssh_task(self, busy_message: str, worker, on_done):
        if self._ssh_busy:
            show_toast(self.winfo_toplevel(), "SSH 环境变量操作正在进行中，请稍等", is_error=True)
            return

        self._ssh_busy = True
        self._set_server_status(busy_message)

        def run():
            try:
                payload = {"ok": True, "result": worker(), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._ssh_busy = False
                on_done(payload)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _write_remote_env(self, control):
        server_name = self._selected_server_name()
        if not server_name:
            return

        profile = next((p for p in profile_manager.list_ssh_profiles() if p.name == server_name), None)
        if not profile:
            message = f"未找到服务器: {server_name}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        try:
            variables = control.env_update()
        except Exception as e:
            message = f"写入失败: {e}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        def worker():
            client = ssh_manager.ssh_manager.connect(profile)
            return persistent_env.set_remote_user_env(client, variables)

        def done(payload):
            if not payload["ok"]:
                message = f"写入失败: {payload['error']}"
                self._set_server_status(message, "error")
                control.set_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            result = payload["result"]
            source_files = "、".join(result.shell_files) if result.shell_files else "shell 启动文件"
            message = f"{result.summary()}。文件: {result.env_file}；已接入: {source_files}"
            self._set_server_status(result.summary(), "success")
            control.set_status(message, "success")
            show_toast(self.winfo_toplevel(), result.summary())

        names = ", ".join(variables.keys())
        busy_message = f"正在向 {server_name} 写入环境变量: {names}..."
        control.set_status(busy_message)
        self._run_ssh_task(busy_message, worker, done)

    def _delete_remote_env(self, control):
        server_name = self._selected_server_name()
        if not server_name:
            return

        profile = next((p for p in profile_manager.list_ssh_profiles() if p.name == server_name), None)
        if not profile:
            message = f"未找到服务器: {server_name}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        try:
            variable_names = control.env_names()
        except Exception as e:
            message = f"删除失败: {e}"
            control.set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        def worker():
            client = ssh_manager.ssh_manager.connect(profile)
            return persistent_env.delete_remote_user_env(client, variable_names)

        def done(payload):
            if not payload["ok"]:
                message = f"删除失败: {payload['error']}"
                self._set_server_status(message, "error")
                control.set_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            result = payload["result"]
            message = f"{result.summary()}。文件: {result.env_file}；{result.details}"
            self._set_server_status(result.summary(), "success")
            control.set_status(message, "warning")
            show_toast(self.winfo_toplevel(), result.summary())

        names = ", ".join(variable_names)
        busy_message = f"正在从 {server_name} 删除环境变量: {names}..."
        control.set_status(busy_message)
        self._run_ssh_task(busy_message, worker, done)
