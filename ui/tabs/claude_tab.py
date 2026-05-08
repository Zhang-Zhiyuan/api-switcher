import customtkinter as ctk
from ui.widgets.profile_card import ProfileCard
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.widgets.auto_continue_control import AutoContinueControl
from ui.dialogs.profile_editor import ProfileEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from models.profile import ClaudeProfile
from core import profile_manager, switcher, security
from ui.theme import COLORS, button_style, font


class ClaudeTab(ctk.CTkScrollableFrame):
    """Tab for managing Claude Code profiles."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._build_ui()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="Claude Code Profile",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="保存不同端点、模型和权限模式，快速切换当前配置",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="+ 新建 Profile",
            width=126,
            command=self._create_profile,
            **button_style("primary"),
        ).pack(side="right")

        # Import button
        ctk.CTkButton(
            header,
            text="导入当前配置",
            width=126,
            command=self._import_current,
            **button_style("secondary"),
        ).pack(side="right", padx=(0, 8))

        # Cards container
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
        self._auto_continue_control = AutoContinueControl(self, provider="claude")
        self._auto_continue_control.pack(fill="x", padx=14, pady=(0, 10))

        self.refresh()

    def refresh(self):
        if not self._cards_frame:
            return
        for w in self._cards_frame.winfo_children():
            w.destroy()

        profiles = profile_manager.list_claude_profiles()
        active = profile_manager.get_active_claude_name()

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无 Claude Profile",
                "新建一个配置，或从当前 Claude Code 设置中导入。",
                "新建 Profile",
                self._create_profile,
            ).pack(fill="x", pady=(12, 4))
            return

        for p in profiles:
            is_active = p.name == active
            info = [
                f"端点: {p.base_url}",
                f"Provider: {p.provider}  |  模型: {p.model}  |  推理力度: {p.effort_level}  |  权限: {p.permissions_mode}",
            ]
            card = ProfileCard(
                self._cards_frame, p.name, info, is_active=is_active,
                on_switch=self._switch_profile,
                on_edit=self._edit_profile,
                on_delete=self._delete_profile,
                border_color=COLORS["success"] if is_active else COLORS["border_soft"],
            )
            card.pack(fill="x", pady=5)

    def _switch_profile(self, name):
        try:
            switcher.switch_claude_profile(name)
            show_toast(self.winfo_toplevel(), f"已切换到: {name}")
            self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"切换失败: {e}", is_error=True)

    def _edit_profile(self, name):
        profiles = profile_manager.list_claude_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(data, old_profile):
            token_ref = old_profile.auth_token_ref if old_profile else f"claude:{data['name']}:auth_token"
            if data.get("auth_token"):
                security.set_secret(token_ref, data["auth_token"])

            new_profile = ClaudeProfile(
                name=data["name"],
                auth_token_ref=token_ref,
                base_url=data["base_url"],
                model=data["model"],
                effort_level=data["effort_level"],
                permissions_mode=data["permissions_mode"],
                skip_dangerous_prompt=data["skip_dangerous_prompt"],
                provider=data.get("provider", "anthropic"),
                custom_provider_name=data.get("custom_provider_name") or None,
            )
            profile_manager.save_claude_profile(new_profile)
            show_toast(self.winfo_toplevel(), f"已保存: {data['name']}")
            self.refresh()

        ProfileEditorDialog(self.winfo_toplevel(), title="编辑 Claude Profile",
                            profile=profile, profile_type="claude", on_save=on_save)

    def _create_profile(self):
        def on_save(data, _):
            token_ref = f"claude:{data['name']}:auth_token"
            if data.get("auth_token"):
                security.set_secret(token_ref, data["auth_token"])

            profile = ClaudeProfile(
                name=data["name"],
                auth_token_ref=token_ref,
                base_url=data["base_url"],
                model=data["model"],
                effort_level=data["effort_level"],
                permissions_mode=data["permissions_mode"],
                skip_dangerous_prompt=data["skip_dangerous_prompt"],
                provider=data.get("provider", "anthropic"),
                custom_provider_name=data.get("custom_provider_name") or None,
            )
            profile_manager.save_claude_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建: {data['name']}")
            self.refresh()

        ProfileEditorDialog(self.winfo_toplevel(), title="新建 Claude Profile",
                            profile_type="claude", on_save=on_save)

    def _delete_profile(self, name):
        def do_delete():
            profile_manager.delete_claude_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除: {name}")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="删除 Profile",
                      message=f"确定要删除 \"{name}\" 吗？\n关联的密钥也会被清除。",
                      on_confirm=do_delete)

    def _import_current(self):
        profile = profile_manager.import_current_claude()
        if profile:
            profile_manager.save_claude_profile(profile)
            show_toast(self.winfo_toplevel(), "已导入当前 Claude 配置")
            self.refresh()
        else:
            show_toast(self.winfo_toplevel(), "未找到当前 Claude 配置", is_error=True)
