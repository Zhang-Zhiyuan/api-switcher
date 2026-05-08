import customtkinter as ctk
from core import parser, toml_parser, auth_parser, vscode_parser, switcher
from ui.widgets.toast import show_toast
from ui.theme import COLORS, button_style, card_frame_kwargs, font


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

        # Load current state
        settings = parser.read_claude_settings()
        if settings.get("permissions", {}).get("defaultMode") == "bypassPermissions":
            bypass_switch.select()
        else:
            bypass_switch.deselect()

        ctk.CTkLabel(
            perm_frame,
            text="开启后会同步更新 Claude Code 与 VS Code 的权限模式",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", padx=14, pady=(0, 12))

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

        self._refresh_overview()

    def refresh(self):
        """Refresh current permission state and overview text."""
        settings = parser.read_claude_settings()
        enabled = settings.get("permissions", {}).get("defaultMode") == "bypassPermissions"
        self._bypass_var.set(enabled)
        self._refresh_overview()

    def _toggle_bypass(self):
        enabled = self._bypass_var.get()
        try:
            switcher.toggle_bypass_permissions(enabled)
            state = "已开启" if enabled else "已关闭"
            show_toast(self.winfo_toplevel(), f"Bypass Permissions {state}")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"操作失败: {e}", is_error=True)

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
