import customtkinter as ctk
import json
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

    def _save_oauth_input(self, profile_name: str, value: str) -> str:
        """Store OAuth tokens entered as JSON, with a raw-token fallback."""
        oauth_ref = f"codex:{profile_name}:oauth_tokens"
        value = value.strip()
        if not value:
            return oauth_ref

        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                security.set_secret_json(oauth_ref, parsed)
            else:
                security.set_secret_json(oauth_ref, {"token": str(parsed)})
        except json.JSONDecodeError:
            security.set_secret_json(oauth_ref, {"token": value})
        return oauth_ref

    def refresh(self):
        if not self._cards_frame:
            return
        for w in self._cards_frame.winfo_children():
            w.destroy()

        profiles = profile_manager.list_codex_profiles()
        active = profile_manager.get_active_codex_name()

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
            auth_desc = "OAuth" if p.auth_mode == "chatgpt" else "API Key"
            info = [
                f"认证: {auth_desc}  |  模型: {p.model}  |  Provider: {p.model_provider}",
                f"端点: {p.custom_base_url or '(默认)'}  |  审批: {p.approval_policy}  |  沙盒: {p.sandbox_mode}",
            ]

            # Token expiry for OAuth profiles
            if p.auth_mode == "chatgpt" and p.oauth_tokens_ref:
                tokens = security.get_secret_json(p.oauth_tokens_ref)
                if tokens and tokens.get("id_token"):
                    from core.auth_parser import get_token_expiry
                    from datetime import datetime, timezone
                    exp = get_token_expiry(tokens["id_token"])
                    if exp:
                        now = datetime.now(timezone.utc)
                        if exp < now:
                            info.append(f"Token 状态: 已过期 ({exp.strftime('%Y-%m-%d %H:%M')})")
                        else:
                            delta = exp - now
                            hours = int(delta.total_seconds() / 3600)
                            info.append(f"Token 过期: {exp.strftime('%Y-%m-%d %H:%M')} (剩余 {hours}h)")

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
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"切换失败: {e}", is_error=True)

    def _edit_profile(self, name):
        profiles = profile_manager.list_codex_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(data, old_profile):
            oauth_ref = old_profile.oauth_tokens_ref if old_profile else None
            api_key_ref = old_profile.api_key_ref if old_profile else None

            if data.get("api_key"):
                api_key_ref = f"codex:{data['name']}:api_key"
                security.set_secret(api_key_ref, data["api_key"])

            if data.get("oauth_token"):
                oauth_ref = self._save_oauth_input(data["name"], data["oauth_token"])

            new_profile = CodexProfile(
                name=data["name"],
                auth_mode=data["auth_mode"],
                api_key_ref=api_key_ref,
                oauth_tokens_ref=oauth_ref,
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

        ProfileEditorDialog(self.winfo_toplevel(), title="编辑 Codex Profile",
                            profile=profile, profile_type="codex", on_save=on_save)

    def _create_profile(self):
        def on_save(data, _):
            api_key_ref = None
            oauth_ref = None

            if data.get("api_key"):
                api_key_ref = f"codex:{data['name']}:api_key"
                security.set_secret(api_key_ref, data["api_key"])

            if data.get("oauth_token"):
                oauth_ref = self._save_oauth_input(data["name"], data["oauth_token"])

            profile = CodexProfile(
                name=data["name"],
                auth_mode=data["auth_mode"],
                api_key_ref=api_key_ref,
                oauth_tokens_ref=oauth_ref,
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

        ProfileEditorDialog(self.winfo_toplevel(), title="新建 Codex Profile",
                            profile_type="codex", on_save=on_save)

    def _delete_profile(self, name):
        def do_delete():
            profile_manager.delete_codex_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除: {name}")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="删除 Profile",
                      message=f"确定要删除 \"{name}\" 吗？\n关联的密钥也会被清除。",
                      on_confirm=do_delete)

    def _import_current(self):
        profile = profile_manager.import_current_codex()
        if profile:
            profile_manager.save_codex_profile(profile)
            show_toast(self.winfo_toplevel(), "已导入当前 Codex 配置")
            self.refresh()
        else:
            show_toast(self.winfo_toplevel(), "未找到当前 Codex 配置", is_error=True)
