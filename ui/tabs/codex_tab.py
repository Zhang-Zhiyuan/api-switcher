import customtkinter as ctk
from ui.widgets.profile_card import ProfileCard
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.widgets.auto_continue_control import AutoContinueControl
from ui.dialogs.profile_editor import ProfileEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from models.profile import CodexProfile
from core import profile_manager, switcher, security
from ui.theme import COLORS, button_style, font


class CodexTab(ctk.CTkScrollableFrame):
    """Tab for managing Codex CLI profiles."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._runtime_label = None
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="Codex CLI Profile",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="管理认证方式、模型供应商、审批策略与沙盒模式",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        self._runtime_label = ctk.CTkLabel(
            title_area,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._runtime_label.pack(anchor="w", pady=(4, 0))

        ctk.CTkButton(
            header,
            text="+ 新建 Profile",
            width=126,
            command=self._create_profile,
            **button_style("primary"),
        ).pack(side="right")
        ctk.CTkButton(
            header,
            text="导入当前配置",
            width=126,
            command=self._import_current,
            **button_style("secondary"),
        ).pack(side="right", padx=(0, 8))

        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="x", padx=14, pady=(0, 10))

        # Separator
        separator = ctk.CTkFrame(self, height=1, fg_color=COLORS["border_soft"])
        separator.pack(fill="x", padx=14, pady=10)

        # Auto Continue section
        auto_continue_header = ctk.CTkFrame(self, fg_color="transparent")
        auto_continue_header.pack(fill="x", padx=14, pady=(10, 8))

        ctk.CTkLabel(
            auto_continue_header,
            text="自动续跑",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            auto_continue_header,
            text="检测未完成任务并自动继续执行，避免手动输入 'continue'",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        # Auto Continue control
        self._auto_continue_control = AutoContinueControl(self, provider="codex")
        self._auto_continue_control.pack(fill="x", padx=14, pady=(0, 10))

        self.refresh()

    def _refresh_shell_state(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_load_quick_switch_profiles"):
            top._load_quick_switch_profiles()
        tray = getattr(top, "tray_manager", None)
        if tray and tray.is_running():
            tray.update_menu()

    def refresh(self):
        if not self._cards_frame:
            return
        for w in self._cards_frame.winfo_children():
            w.destroy()

        profiles = profile_manager.list_switchable_codex_profiles()
        runtime = profile_manager.get_codex_runtime_summary()
        active = runtime.get("profile_name")
        stored_active = runtime.get("stored_active")

        if self._runtime_label:
            if active:
                text = (
                    f"Codex on disk: {active} | "
                    f"{runtime.get('provider')} / {runtime.get('model')} | "
                    f"{runtime.get('auth_mode')}:{runtime.get('auth_identity')}"
                )
                color = COLORS["success"]
            elif runtime.get("has_config") or runtime.get("has_auth"):
                text = (
                    "Codex on disk: unmatched saved profile | "
                    f"{runtime.get('provider')} / {runtime.get('model')} | "
                    f"{runtime.get('auth_mode')}:{runtime.get('auth_identity')}"
                )
                color = COLORS["warning"]
            else:
                text = "Codex on disk: no config/auth found"
                color = COLORS["muted"]

            if stored_active and stored_active != active:
                text = f"{text} | last app switch: {stored_active}"
            self._runtime_label.configure(text=text, text_color=color)

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无 Codex Profile",
                "新建一个配置，或从当前 Codex CLI 设置中导入。",
                "新建 Profile",
                self._create_profile,
            ).pack(fill="x", pady=(12, 4))
            return

        for p in profiles:
            is_active = p.name == active
            auth_identity = profile_manager.describe_codex_profile_identity(p)
            auth_desc = f"API Key ({auth_identity})"
            info = [
                f"认证: {auth_desc}  |  模型: {p.model}  |  Provider: {p.model_provider}",
                f"端点: {p.custom_base_url or '(默认)'}  |  审批: {p.approval_policy}  |  沙盒: {p.sandbox_mode}",
            ]

            card = ProfileCard(
                self._cards_frame, p.name, info, is_active=is_active,
                on_switch=self._switch_profile,
                on_edit=self._edit_profile,
                on_delete=self._delete_profile,
                border_color=COLORS["primary"] if is_active else COLORS["border_soft"],
            )
            card.pack(fill="x", pady=5)

    def _switch_profile(self, name):
        try:
            switcher.switch_codex_profile(name)
            show_toast(self.winfo_toplevel(), f"已切换到: {name}")
            self.refresh()
            self._refresh_shell_state()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"切换失败: {e}", is_error=True)

    def _edit_profile(self, name):
        profiles = profile_manager.list_switchable_codex_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(data, old_profile):
            api_key_ref = old_profile.api_key_ref if old_profile else None

            if data.get("api_key"):
                api_key_ref = f"codex:{data['name']}:api_key"
                security.set_secret(api_key_ref, data["api_key"])

            new_profile = CodexProfile(
                name=data["name"],
                auth_mode="api_key",
                api_key_ref=api_key_ref,
                model=data["model"],
                model_provider=data["model_provider"],
                model_reasoning_effort=data.get("model_reasoning_effort", "high"),
                custom_base_url=data.get("custom_base_url") or None,
                custom_name=data.get("custom_name") or None,
                custom_wire_api=data.get("custom_wire_api") or None,
                custom_requires_openai_auth=data.get("custom_requires_openai_auth", False),
                approval_policy=data.get("approval_policy", "never"),
                sandbox_mode=data.get("sandbox_mode", "danger-full-access"),
            )
            profile_manager.save_codex_profile(new_profile)
            show_toast(self.winfo_toplevel(), f"已保存: {data['name']}")
            self.refresh()
            self._refresh_shell_state()

        ProfileEditorDialog(self.winfo_toplevel(), title="编辑 Codex Profile",
                            profile=profile, profile_type="codex", on_save=on_save)

    def _create_profile(self):
        def on_save(data, _):
            api_key_ref = None

            if data.get("api_key"):
                api_key_ref = f"codex:{data['name']}:api_key"
                security.set_secret(api_key_ref, data["api_key"])

            profile = CodexProfile(
                name=data["name"],
                auth_mode="api_key",
                api_key_ref=api_key_ref,
                model=data["model"],
                model_provider=data["model_provider"],
                model_reasoning_effort=data.get("model_reasoning_effort", "high"),
                custom_base_url=data.get("custom_base_url") or None,
                custom_name=data.get("custom_name") or None,
                custom_wire_api=data.get("custom_wire_api") or None,
                custom_requires_openai_auth=data.get("custom_requires_openai_auth", False),
                approval_policy=data.get("approval_policy", "never"),
                sandbox_mode=data.get("sandbox_mode", "danger-full-access"),
            )
            profile_manager.save_codex_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建: {data['name']}")
            self.refresh()
            self._refresh_shell_state()

        ProfileEditorDialog(self.winfo_toplevel(), title="新建 Codex Profile",
                            profile_type="codex", on_save=on_save)

    def _delete_profile(self, name):
        def do_delete():
            profile_manager.delete_codex_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除: {name}")
            self.refresh()
            self._refresh_shell_state()

        ConfirmDialog(self.winfo_toplevel(), title="删除 Profile",
                      message=f"确定要删除 \"{name}\" 吗？\n关联的密钥也会被清除。",
                      on_confirm=do_delete)

    def _import_current(self):
        profile = profile_manager.import_current_codex()
        if profile:
            profile_manager.save_codex_profile(profile)
            show_toast(self.winfo_toplevel(), "已导入当前 Codex 配置")
            self.refresh()
            self._refresh_shell_state()
        else:
            show_toast(self.winfo_toplevel(), "未找到当前 Codex 配置", is_error=True)
