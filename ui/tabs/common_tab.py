import customtkinter as ctk
from tkinter import filedialog

from config import paths
from core import parser, toml_parser, auth_parser, startup_manager, vscode_parser, switcher
from ui.widgets.toast import show_toast
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


class CommonTab(ctk.CTkScrollableFrame):
    """Tab for common settings and quick overview."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            header,
            text="通用设置",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="统一调整权限模式，并查看当前本机配置摘要",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        # --- Permission Toggle ---
        perm_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        perm_frame.pack(fill="x", padx=14, pady=(0, 10))

        ctk.CTkLabel(
            perm_frame,
            text="权限设置",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))

        self._bypass_var = ctk.BooleanVar(value=False)
        bypass_switch = ctk.CTkSwitch(
            perm_frame,
            text="Bypass Permissions",
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
            variable=self._bypass_var,
            command=self._toggle_bypass,
        )
        bypass_switch.pack(anchor="w", padx=14, pady=5)

        ctk.CTkLabel(
            perm_frame,
            text="开启后会同步更新 Claude Code 与 VS Code 的权限模式",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # --- System Integration ---
        system_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        system_frame.pack(fill="x", padx=14, pady=(0, 10))

        system_head = ctk.CTkFrame(system_frame, fg_color="transparent")
        system_head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(
            system_head,
            text="系统集成",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")

        self._startup_repair_button = ctk.CTkButton(
            system_head,
            text="修复自启动",
            width=92,
            command=self._repair_startup,
            **button_style("secondary", compact=True),
        )
        self._startup_repair_button.pack(side="right")

        self._startup_var = ctk.BooleanVar(value=False)
        self._startup_switch = ctk.CTkSwitch(
            system_frame,
            text="开机自启动，并自动进入系统托盘",
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
            variable=self._startup_var,
            command=self._toggle_startup,
        )
        self._startup_switch.pack(anchor="w", padx=14, pady=(0, 6))

        self._startup_status_label = ctk.CTkLabel(
            system_frame,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._startup_status_label.pack(fill="x", padx=14, pady=(0, 12))
        bind_wraplength(system_frame, self._startup_status_label, padding=32)

        # --- Data Directory ---
        storage_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        storage_frame.pack(fill="x", padx=14, pady=(0, 12))

        storage_head = ctk.CTkFrame(storage_frame, fg_color="transparent")
        storage_head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(
            storage_head,
            text="数据存储",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")

        ctk.CTkButton(
            storage_head,
            text="刷新",
            width=72,
            command=self._refresh_storage_info,
            **button_style("secondary", compact=True),
        ).pack(side="right")

        self._storage_info_label = ctk.CTkLabel(
            storage_frame,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._storage_info_label.pack(fill="x", padx=14, pady=(0, 8))
        bind_wraplength(storage_frame, self._storage_info_label, padding=32)

        storage_buttons = ctk.CTkFrame(storage_frame, fg_color="transparent")
        storage_buttons.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkButton(
            storage_buttons,
            text="打开数据目录",
            width=108,
            command=self._open_data_dir,
            **button_style("primary", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            storage_buttons,
            text="复制路径",
            width=78,
            command=self._copy_data_dir,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            storage_buttons,
            text="选择目录",
            width=86,
            command=self._choose_data_dir,
            **button_style("accent", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            storage_buttons,
            text="便携模式",
            width=86,
            command=self._enable_portable_mode,
            **button_style("success", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            storage_buttons,
            text="恢复默认",
            width=86,
            command=self._restore_default_storage,
            **button_style("warning", compact=True),
        ).pack(side="left")

        # --- Current Config Overview ---
        overview_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        overview_frame.pack(fill="x", padx=14, pady=(0, 12))

        overview_head = ctk.CTkFrame(overview_frame, fg_color="transparent")
        overview_head.pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkLabel(
            overview_head,
            text="当前配置概览",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")

        ctk.CTkButton(
            overview_head,
            text="刷新",
            width=72,
            command=self._refresh_overview,
            **button_style("secondary", compact=True),
        ).pack(side="right")

        self._overview_text = ctk.CTkTextbox(
            overview_frame,
            height=300,
            fg_color=COLORS["app_bg"],
            border_color=COLORS["border"],
            border_width=1,
            text_color=COLORS["text"],
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
            font=font(12, family="Consolas"),
            corner_radius=8,
        )
        self._overview_text.pack(fill="x", padx=14, pady=(0, 14))

        self.after(20, self.refresh)

    def refresh(self):
        """Refresh current permission state and overview text."""
        settings = parser.read_claude_settings()
        enabled = settings.get("permissions", {}).get("defaultMode") == "bypassPermissions"
        self._bypass_var.set(enabled)
        self._refresh_startup_info()
        self._refresh_storage_info()
        self._refresh_overview()

    def _toggle_bypass(self):
        enabled = self._bypass_var.get()
        try:
            switcher.toggle_bypass_permissions(enabled)
            state = "已开启" if enabled else "已关闭"
            show_toast(self.winfo_toplevel(), f"Bypass Permissions {state}")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"操作失败: {e}", is_error=True)

    def _refresh_startup_info(self):
        status = startup_manager.get_startup_status()
        self._startup_var.set(status.enabled)

        if not status.supported:
            self._startup_switch.configure(state="disabled")
            self._startup_repair_button.configure(state="disabled")
            self._startup_status_label.configure(
                text="当前系统不支持此开机自启动方式。Windows 下会写入当前用户的 Run 注册表项。",
                text_color=COLORS["muted"],
            )
            return

        self._startup_switch.configure(state="normal")
        repair_enabled = bool(status.enabled and not status.matches_expected and not status.error)
        self._startup_repair_button.configure(state="normal" if repair_enabled else "disabled")
        if status.error:
            text = f"读取自启动状态失败: {status.error}"
            color = COLORS["danger"]
        elif status.enabled and status.matches_expected:
            text = "已启用。下次登录 Windows 后会自动启动，并以托盘模式运行。"
            color = COLORS["success"]
        elif status.enabled:
            text = "已启用，但启动命令不是当前程序路径。点击“修复自启动”可更新到当前版本。"
            color = COLORS["warning"]
        else:
            text = "未启用。开启后会注册到当前 Windows 用户，不需要管理员权限。"
            color = COLORS["muted"]
        self._startup_status_label.configure(text=text, text_color=color)

    def _toggle_startup(self):
        enabled = self._startup_var.get()
        try:
            status = startup_manager.set_startup_enabled(enabled)
            self._refresh_startup_info()
            top = self.winfo_toplevel()
            tray = getattr(top, "tray_manager", None)
            if tray and tray.is_running():
                tray.update_menu()
            if enabled and status.enabled:
                show_toast(top, "已开启开机自启动，启动后会进入系统托盘")
            elif not enabled:
                show_toast(top, "已关闭开机自启动")
        except Exception as e:
            self._startup_var.set(not enabled)
            self._refresh_startup_info()
            show_toast(self.winfo_toplevel(), f"自启动设置失败: {e}", is_error=True)

    def _repair_startup(self):
        try:
            startup_manager.enable_startup()
            self._refresh_startup_info()
            top = self.winfo_toplevel()
            tray = getattr(top, "tray_manager", None)
            if tray and tray.is_running():
                tray.update_menu()
            show_toast(top, "已更新开机自启动命令")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"修复失败: {e}", is_error=True)

    def _refresh_storage_info(self):
        info = paths.get_storage_info()
        source_labels = {
            paths.ENV_DATA_DIR: "环境变量",
            paths.DATA_DIR_POINTER_FILE: "自定义目录文件",
            "portable": "便携模式",
            "%APPDATA%": "Windows Roaming",
            "home-roaming": "Windows Roaming",
            "%LOCALAPPDATA%": "Windows Local",
            "home-local": "Windows Local",
            "temp-fallback": "临时目录 fallback",
            "cwd-fallback": "当前目录 fallback",
        }
        source = source_labels.get(info["source"], str(info["source"]))
        writable = "可写" if info["writable"] else f"不可写: {info['write_error']}"
        pointer_state = "已设置" if (info["data_dir_pointer_exists"] or info["user_data_dir_pointer_exists"]) else "未设置"
        portable_state = "已启用" if info["portable_marker_exists"] or info["portable"] else "未启用"
        warnings = info.get("warnings") or []
        warning_text = "\n警告: " + " | ".join(warnings[:3]) if warnings else ""
        self._storage_info_label.configure(
            text=(
                f"当前目录: {info['storage_dir']}\n"
                f"来源: {source}  |  状态: {writable}\n"
                f"程序目录: {info['app_dir']}\n"
                f"自定义目录文件: {pointer_state}  |  便携模式: {portable_state}"
                f"{warning_text}\n"
                "更改目录或便携模式会复制当前数据，并在下次启动后生效。"
            )
        )

    def _open_data_dir(self):
        try:
            paths.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(paths.STORAGE_DIR)
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"打开失败: {e}", is_error=True)

    def _copy_data_dir(self):
        try:
            top = self.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(str(paths.STORAGE_DIR))
            show_toast(top, "已复制数据目录路径")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"复制失败: {e}", is_error=True)

    def _choose_data_dir(self):
        selected = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            title="选择 API切换器数据目录",
        )
        if not selected:
            return
        try:
            copied = paths.write_data_dir_pointer(selected, copy_current=True)
            self._refresh_storage_info()
            show_toast(
                self.winfo_toplevel(),
                f"已设置自定义目录并复制 {len(copied)} 个项目，重启后生效",
            )
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"设置失败: {e}", is_error=True)

    def _enable_portable_mode(self):
        try:
            copied = paths.enable_portable_storage(copy_current=True)
            self._refresh_storage_info()
            show_toast(
                self.winfo_toplevel(),
                f"已启用便携模式并复制 {len(copied)} 个项目，重启后生效",
            )
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"启用失败: {e}", is_error=True)

    def _restore_default_storage(self):
        try:
            changed = paths.disable_portable_storage()
            self._refresh_storage_info()
            if changed:
                show_toast(self.winfo_toplevel(), "已恢复默认数据目录，重启后生效")
            else:
                show_toast(self.winfo_toplevel(), "当前已经使用默认数据目录")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"恢复失败: {e}", is_error=True)

    def _refresh_overview(self):
        self._overview_text.configure(state="normal")
        self._overview_text.delete("1.0", "end")

        lines = []

        # Claude
        claude = parser.read_claude_settings()
        env = claude.get("env", {})
        lines.append("=== Claude Code ===")
        lines.append(f"  Base URL:    {env.get('ANTHROPIC_BASE_URL', '(未设置)')}")
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        lines.append(f"  Auth Token:  {token[:12]}...{token[-4:]}" if len(token) > 16 else f"  Auth Token:  {token}")
        lines.append(f"  Model:       {claude.get('model', '(未设置)')}")
        lines.append(f"  Effort:      {claude.get('effortLevel', '(未设置)')}")
        lines.append(f"  Permissions: {claude.get('permissions', {}).get('defaultMode', 'default')}")
        lines.append("")

        # Codex
        codex_cfg = toml_parser.read_codex_config()
        lines.append("=== Codex CLI ===")
        lines.append(f"  Model:       {codex_cfg.get('model', '(未设置)')}")
        lines.append(f"  Provider:    {codex_cfg.get('model_provider', '(未设置)')}")
        lines.append(f"  Effort:      {codex_cfg.get('model_reasoning_effort', '(未设置)')}")
        lines.append(f"  Approval:    {codex_cfg.get('approval_policy', '(未设置)')}")
        lines.append(f"  Sandbox:     {codex_cfg.get('sandbox_mode', '(未设置)')}")
        provider_id = codex_cfg.get("model_provider", "custom")
        custom = codex_cfg.get("model_providers", {}).get(provider_id, {})
        if custom:
            lines.append(f"  Base URL:    {custom.get('base_url', '-')}")
        lines.append("")

        # Codex Auth
        codex_auth = auth_parser.read_codex_auth()
        lines.append("=== Codex Auth ===")
        lines.append(f"  Auth Mode:   {codex_auth.get('auth_mode', '(未设置)')}")
        api_key = codex_auth.get("OPENAI_API_KEY") or ""
        if api_key:
            lines.append(f"  API Key:     {api_key[:8]}...{api_key[-4:]}")
        tokens = codex_auth.get("tokens", {})
        if tokens.get("account_id"):
            lines.append(f"  Account:     {tokens['account_id']}")
        lines.append(f"  Last Refresh: {codex_auth.get('last_refresh', '(无)')}")
        lines.append("")

        # VS Code
        vscode = vscode_parser.read_vscode_settings()
        lines.append("=== VS Code (Claude 相关) ===")
        lines.append(f"  Skip Perms:  {vscode.get('claudeCode.allowDangerouslySkipPermissions', '(未设置)')}")
        lines.append(f"  Init Mode:   {vscode.get('claudeCode.initialPermissionMode', '(未设置)')}")
        lines.append(f"  Model:       {vscode.get('claudeCode.selectedModel', '(未设置)')}")

        self._overview_text.insert("1.0", "\n".join(lines))
        self._overview_text.configure(state="disabled")
