import threading

import customtkinter as ctk
from ui.widgets.profile_card import ProfileCard
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.dialogs.profile_editor import ProfileEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from models.profile import ClaudeProfile
from core import profile_manager
from ui.theme import COLORS, bind_wraplength, button_style, font


class ClaudeTab(ctk.CTkScrollableFrame):
    """Tab for managing Claude Code API configs and official accounts."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._account_cards_frame = None
        self._runtime_label = None
        self._account_runtime_label = None
        self._refresh_generation = 0
        self._profile_render_after_id = None
        self._profile_render_after_ids = set()
        self._refresh_finish_after_id = None
        self._destroyed = False
        self._auto_continue_control = None
        self._auto_continue_host = None
        self._auto_continue_after_id = None
        self._build_ui()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="Claude Code",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle_label = ctk.CTkLabel(
            title_area,
            text="API 配置只管理第三方端点和密钥；官方账号只管理本机 Claude Code 登录快照",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle_label.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(title_area, subtitle_label, padding=24, min_width=240, max_width=720)

        self._runtime_label = ctk.CTkLabel(
            title_area,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._runtime_label.pack(anchor="w", fill="x", pady=(4, 0))
        bind_wraplength(title_area, self._runtime_label, padding=24, min_width=240, max_width=760)

        api_header = ctk.CTkFrame(self, fg_color="transparent")
        api_header.pack(fill="x", padx=14, pady=(4, 8))
        api_title = ctk.CTkFrame(api_header, fg_color="transparent")
        api_title.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            api_title,
            text="第三方 API 配置",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w")
        api_subtitle = ctk.CTkLabel(
            api_title,
            text="写入 Claude settings/config，用于切换 Anthropic-compatible API、模型和权限",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        api_subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(api_title, api_subtitle, padding=24, min_width=240, max_width=720)
        ctk.CTkButton(
            api_header,
            text="+ 新建 API 配置",
            width=126,
            command=self._create_profile,
            **button_style("primary"),
        ).pack(side="right")
        ctk.CTkButton(
            api_header,
            text="导入当前 API",
            width=126,
            command=self._import_current,
            **button_style("secondary"),
        ).pack(side="right", padx=(0, 8))

        # Cards container
        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="x", padx=14, pady=(0, 10))

        account_header = ctk.CTkFrame(self, fg_color="transparent")
        account_header.pack(fill="x", padx=14, pady=(8, 8))
        account_title = ctk.CTkFrame(account_header, fg_color="transparent")
        account_title.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            account_title,
            text="官方账号",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w")
        account_subtitle = ctk.CTkLabel(
            account_title,
            text="保存本机 Claude Code 登录凭据快照；切换后新开的终端会话生效",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        account_subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(account_title, account_subtitle, padding=24, min_width=240, max_width=720)
        self._account_runtime_label = ctk.CTkLabel(
            account_title,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._account_runtime_label.pack(anchor="w", fill="x", pady=(4, 0))
        bind_wraplength(account_title, self._account_runtime_label, padding=24, min_width=240, max_width=720)
        ctk.CTkButton(
            account_header,
            text="导入当前账号",
            width=126,
            command=self._import_current_account,
            **button_style("secondary"),
        ).pack(side="right")

        self._account_cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._account_cards_frame.pack(fill="x", padx=14, pady=(0, 10))

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

        self._auto_continue_host = ctk.CTkFrame(self, fg_color="transparent")
        self._auto_continue_host.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkLabel(
            self._auto_continue_host,
            text="自动续跑控制正在准备...",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(fill="x", pady=(10, 12))
        self._auto_continue_after_id = self.after(220, self._build_auto_continue_control)

        self.after(20, self.refresh)

    def destroy(self):
        self._destroyed = True
        self._refresh_generation += 1
        if self._auto_continue_after_id:
            try:
                self.after_cancel(self._auto_continue_after_id)
            except Exception:
                pass
            self._auto_continue_after_id = None
        self._cancel_profile_render()
        self._cancel_refresh_finish()
        super().destroy()

    def _build_auto_continue_control(self):
        self._auto_continue_after_id = None
        if self._destroyed or self._auto_continue_control or not self._auto_continue_host:
            return
        try:
            for child in self._auto_continue_host.winfo_children():
                child.destroy()
        except Exception:
            pass
        from ui.widgets.auto_continue_control import AutoContinueControl

        self._auto_continue_control = AutoContinueControl(self._auto_continue_host, provider="claude")
        self._auto_continue_control.pack(fill="x")

    def _cancel_profile_render(self):
        after_ids = set(self._profile_render_after_ids)
        if self._profile_render_after_id:
            after_ids.add(self._profile_render_after_id)
        for after_id in after_ids:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._profile_render_after_id = None
        self._profile_render_after_ids.clear()

    def _cancel_refresh_finish(self):
        if not self._refresh_finish_after_id:
            return
        try:
            self.after_cancel(self._refresh_finish_after_id)
        except Exception:
            pass
        self._refresh_finish_after_id = None

    def _refresh_shell_state(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_load_quick_switch_profiles"):
            top._load_quick_switch_profiles()
        tray = getattr(top, "tray_manager", None)
        if tray and tray.is_running():
            tray.update_menu()

    def _show_switch_preview(self, kind: str, name: str, on_confirm):
        top = self.winfo_toplevel()
        if hasattr(top, "_show_switch_preview"):
            top._show_switch_preview(kind, name, on_confirm, self._refresh_shell_state)
            return

        from ui.dialogs.switch_preview_dialog import show_switch_preview

        show_switch_preview(top, kind, name, on_confirm=on_confirm, on_cancel=self._refresh_shell_state)

    def refresh(self):
        if not self._cards_frame or not self._account_cards_frame:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._cancel_profile_render()
        self._show_refresh_loading()

        def worker():
            try:
                profiles = profile_manager.list_switchable_claude_profiles()
                account_profiles = profile_manager.list_claude_account_profiles()
                runtime = profile_manager.get_claude_runtime_summary()
                account_runtime = profile_manager.get_claude_account_runtime_summary()
                active = runtime.get("profile_name")
                active_account = account_runtime.get("profile_name")
                payload = {
                    "ok": True,
                    "profiles": [
                        {
                            "profile": profile,
                            "is_active": profile.name == active,
                            "auth_identity": profile_manager.describe_claude_profile_identity(profile),
                        }
                        for profile in profiles
                    ],
                    "accounts": [
                        {
                            "profile": account,
                            "is_active": account.name == active_account,
                            "snapshot": profile_manager.validate_claude_account_snapshot(account),
                        }
                        for account in account_profiles
                    ],
                    "runtime": runtime,
                    "account_runtime": account_runtime,
                    "error": "",
                }
            except Exception as exc:
                payload = {"ok": False, "error": str(exc)}

            def finish():
                self._refresh_finish_after_id = None
                if generation != self._refresh_generation or not self._is_alive():
                    return
                if not payload["ok"]:
                    self._show_refresh_error(payload["error"])
                    return
                self._render_refresh_payload(payload, generation)

            try:
                if not self._destroyed:
                    self._refresh_finish_after_id = self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name="claude-tab-refresh", daemon=True).start()

    def _is_alive(self) -> bool:
        if self._destroyed:
            return False
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _clear_frame(self, frame):
        for widget in frame.winfo_children():
            widget.destroy()

    def _show_refresh_loading(self):
        self._clear_frame(self._cards_frame)
        self._clear_frame(self._account_cards_frame)
        ctk.CTkLabel(
            self._cards_frame,
            text="正在后台读取 Claude API 配置...",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
        ).pack(fill="x", pady=(12, 4))
        ctk.CTkLabel(
            self._account_cards_frame,
            text="正在后台读取 Claude 官方账号快照...",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
        ).pack(fill="x", pady=(4, 4))

    def _show_refresh_error(self, message: str):
        self._clear_frame(self._cards_frame)
        self._clear_frame(self._account_cards_frame)
        text = f"读取 Claude 配置失败: {message}"
        ctk.CTkLabel(
            self._cards_frame,
            text=text,
            text_color=COLORS["danger"],
            font=font(12),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(12, 4))
        if self._runtime_label:
            self._runtime_label.configure(text=text, text_color=COLORS["danger"])

    def _render_refresh_payload(self, payload: dict, generation: int):
        for w in self._cards_frame.winfo_children():
            w.destroy()
        for w in self._account_cards_frame.winfo_children():
            w.destroy()

        profiles = payload["profiles"]
        account_profiles = payload["accounts"]
        runtime = payload["runtime"]
        account_runtime = payload["account_runtime"]
        active = runtime.get("profile_name")
        stored_active = runtime.get("stored_active")
        active_account = account_runtime.get("profile_name")
        stored_account = account_runtime.get("stored_active")

        if self._runtime_label:
            if active:
                text = (
                    f"API 当前写入: {active} | "
                    f"{runtime.get('provider')} / {runtime.get('model')} | "
                    f"{runtime.get('auth_identity')}"
                )
                color = COLORS["success"]
            elif runtime.get("has_settings") or runtime.get("has_config"):
                text = (
                    "API 当前写入: 未匹配到已保存 API 配置 | "
                    f"{runtime.get('provider')} / {runtime.get('model')} | "
                    f"{runtime.get('auth_identity')}"
                )
                color = COLORS["warning"]
            else:
                text = "API 当前写入: 未发现 settings/config"
                color = COLORS["muted"]

            if stored_active and stored_active != active:
                text = f"{text} | API 记录: {stored_active}"
            self._runtime_label.configure(text=text, text_color=color)

        if self._account_runtime_label:
            if active_account:
                account_text = f"官方账号当前生效: {active_account} | {account_runtime.get('identity')}"
                account_color = COLORS["success"]
            elif account_runtime.get("has_credentials"):
                if account_runtime.get("api_override_active"):
                    account_text = "官方账号当前未生效: 已有登录凭据，但当前被 API 配置覆盖"
                    account_color = COLORS["warning"]
                else:
                    account_text = f"官方账号当前未匹配已保存快照 | {account_runtime.get('identity')}"
                    account_color = COLORS["warning"]
            elif stored_account:
                account_text = f"官方账号最近记录: {stored_account} | 当前磁盘未发现可用登录凭据"
                account_color = COLORS["warning"]
            else:
                account_text = "官方账号当前未生效: 未发现可用登录凭据"
                account_color = COLORS["muted"]
            self._account_runtime_label.configure(text=account_text, text_color=account_color)

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无 Claude API 配置",
                "新建第三方 API 配置，或从当前 Claude Code API 设置中导入。",
                "新建 API 配置",
                self._create_profile,
            ).pack(fill="x", pady=(12, 4))
        else:
            self._render_profile_cards_batch(profiles, generation)

        if not account_profiles:
            EmptyState(
                self._account_cards_frame,
                "暂无 Claude 官方账号",
                "先在 Claude Code 登录账号，再导入当前账号快照。",
                "导入当前账号",
                self._import_current_account,
            ).pack(fill="x", pady=(4, 4))
            return

        self._render_account_cards_batch(account_profiles, generation)

    def _render_profile_cards_batch(self, profiles: list[dict], generation: int, start: int = 0):
        if generation != self._refresh_generation or not self._is_alive():
            return
        batch_size = 4
        end = min(len(profiles), start + batch_size)
        for item in profiles[start:end]:
            profile = item["profile"]
            is_active = bool(item["is_active"])
            info = [
                f"认证: {item.get('auth_identity') or 'no-auth'}  |  端点: {profile.base_url or '(默认)'}",
                f"Provider: {profile.provider}  |  模型: {profile.model}  |  推理力度: {profile.effort_level}  |  权限: {profile.permissions_mode}",
            ]
            card = ProfileCard(
                self._cards_frame, profile.name, info, is_active=is_active,
                active_label="当前 API",
                switch_label="切换 API",
                on_switch=self._switch_profile,
                on_test=self._test_profile,
                on_edit=self._edit_profile,
                on_clone=self._clone_profile,
                on_delete=self._delete_profile,
                border_color=COLORS["success"] if is_active else COLORS["border_soft"],
            )
            card.pack(fill="x", pady=5)
        if end >= len(profiles):
            self._profile_render_after_id = None
            return
        after_id = self.after(
            1,
            lambda: self._render_profile_cards_batch(profiles, generation, end),
        )
        self._profile_render_after_id = after_id
        self._profile_render_after_ids.add(after_id)

    def _render_account_cards_batch(self, accounts: list[dict], generation: int, start: int = 0):
        if generation != self._refresh_generation or not self._is_alive():
            return
        batch_size = 4
        end = min(len(accounts), start + batch_size)
        for item in accounts[start:end]:
            account = item["profile"]
            is_active = bool(item["is_active"])
            snapshot_ok, snapshot_status = item["snapshot"]
            info = [
                f"身份: {account.identity}",
                f"状态: {snapshot_status}  |  凭据: 本机加密保存  |  保存时间: {account.created_at or '-'}",
            ]
            card = ProfileCard(
                self._account_cards_frame, account.name, info, is_active=is_active,
                active_label="当前账号",
                switch_label="切换账号",
                on_switch=self._switch_account if snapshot_ok else None,
                on_delete=self._delete_account,
                border_color=COLORS["accent"] if is_active else (COLORS["danger"] if not snapshot_ok else COLORS["border_soft"]),
            )
            card.pack(fill="x", pady=5)
        if end >= len(accounts):
            self._profile_render_after_id = None
            return
        after_id = self.after(
            1,
            lambda: self._render_account_cards_batch(accounts, generation, end),
        )
        self._profile_render_after_id = after_id
        self._profile_render_after_ids.add(after_id)

    def _switch_profile(self, name):
        def perform_switch():
            try:
                from core import switcher

                switcher.switch_claude_profile(name)
                show_toast(self.winfo_toplevel(), f"已切换 Claude API 配置: {name}")
                self.refresh()
                self._refresh_shell_state()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"切换失败: {e}", is_error=True)

        self._show_switch_preview("claude_api", name, perform_switch)

    def _switch_account(self, name):
        def perform_switch():
            try:
                from core import switcher

                switcher.switch_claude_account(name)
                show_toast(self.winfo_toplevel(), f"已切换 Claude 官方账号: {name}；新开的终端会话生效")
                self.refresh()
                self._refresh_shell_state()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"账号切换失败: {e}", is_error=True)

        self._show_switch_preview("claude_account", name, perform_switch)

    def _test_profile(self, name):
        profiles = profile_manager.list_switchable_claude_profiles()
        profile = next((p for p in profiles if p.name == name), None)
        if not profile:
            show_toast(self.winfo_toplevel(), f"未找到 API 配置: {name}", is_error=True)
            return

        from core import security

        api_key = (
            security.get_secret(profile.auth_token_ref)
            or security.get_secret(getattr(profile, "primary_api_key_ref", None))
            or ""
        )
        if not api_key:
            show_toast(self.winfo_toplevel(), "该 API 配置没有可用 Auth Token", is_error=True)
            return

        show_toast(self.winfo_toplevel(), f"正在测试: {name}")

        def run_test():
            from core.api_tester import APITester
            from core.api_tester import TestResult
            from ui.dialogs.api_test_result_dialog import APITestResultDialog

            try:
                result = APITester.test_claude_api(api_key, profile.base_url, profile.model)
            except Exception as exc:
                result = TestResult(
                    False,
                    f"测试失败: {type(exc).__name__}",
                    error_details=str(exc)[:400],
                )

            def show_result():
                if self.winfo_exists():
                    APITestResultDialog(self.winfo_toplevel(), result, name)

            try:
                self.after(0, show_result)
            except Exception:
                pass

        import threading
        threading.Thread(target=run_test, daemon=True).start()

    def _edit_profile(self, name):
        profiles = profile_manager.list_switchable_claude_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(data, old_profile):
            from core import security

            token_ref = old_profile.auth_token_ref if old_profile else f"claude:{data['name']}:auth_token"
            primary_api_key_ref = old_profile.primary_api_key_ref if old_profile else None
            if data.get("auth_token"):
                security.set_secret(token_ref, data["auth_token"])
                primary_api_key_ref = primary_api_key_ref or f"claude:{data['name']}:primary_api_key"
                security.set_secret(primary_api_key_ref, data["auth_token"])

            new_profile = ClaudeProfile(
                name=data["name"],
                auth_token_ref=token_ref,
                primary_api_key_ref=primary_api_key_ref,
                base_url=data["base_url"],
                model=data["model"],
                effort_level=data["effort_level"],
                permissions_mode=data["permissions_mode"],
                skip_dangerous_prompt=data["skip_dangerous_prompt"],
                permissions_allow=old_profile.permissions_allow if old_profile else [],
                additional_directories=old_profile.additional_directories if old_profile else [],
                provider=data.get("provider", "custom"),
                custom_provider_name=data.get("custom_provider_name") or None,
            )
            profile_manager.save_claude_profile(
                new_profile,
                previous_name=old_profile.name if old_profile else None,
            )
            show_toast(self.winfo_toplevel(), f"已保存 Claude API 配置: {data['name']}")
            self.refresh()
            self._refresh_shell_state()

        ProfileEditorDialog(self.winfo_toplevel(), title="编辑 Claude API 配置",
                            profile=profile, profile_type="claude", on_save=on_save)

    def _create_profile(self):
        def on_save(data, _):
            from core import security

            token_ref = f"claude:{data['name']}:auth_token"
            primary_api_key_ref = None
            if data.get("auth_token"):
                security.set_secret(token_ref, data["auth_token"])
                primary_api_key_ref = f"claude:{data['name']}:primary_api_key"
                security.set_secret(primary_api_key_ref, data["auth_token"])

            profile = ClaudeProfile(
                name=data["name"],
                auth_token_ref=token_ref,
                primary_api_key_ref=primary_api_key_ref,
                base_url=data["base_url"],
                model=data["model"],
                effort_level=data["effort_level"],
                permissions_mode=data["permissions_mode"],
                skip_dangerous_prompt=data["skip_dangerous_prompt"],
                provider=data.get("provider", "custom"),
                custom_provider_name=data.get("custom_provider_name") or None,
            )
            profile_manager.save_claude_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建 Claude API 配置: {data['name']}")
            self.refresh()
            self._refresh_shell_state()

        ProfileEditorDialog(self.winfo_toplevel(), title="新建 Claude API 配置",
                            profile_type="claude", on_save=on_save)

    def _clone_profile(self, name):
        try:
            cloned = profile_manager.clone_claude_profile(name)
            show_toast(self.winfo_toplevel(), f"已复制 Claude API 配置为: {cloned.name}")
            self.refresh()
            self._refresh_shell_state()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"复制失败: {e}", is_error=True)

    def _delete_profile(self, name):
        def do_delete():
            profile_manager.delete_claude_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除 Claude API 配置: {name}")
            self.refresh()
            self._refresh_shell_state()

        ConfirmDialog(self.winfo_toplevel(), title="删除 API 配置",
                      message=f"确定要删除 API 配置 \"{name}\" 吗？\n关联的 API 密钥也会被清除，不会影响官方账号快照。",
                      on_confirm=do_delete)

    def _delete_account(self, name):
        def do_delete():
            profile_manager.delete_claude_account_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除 Claude 官方账号: {name}")
            self.refresh()
            self._refresh_shell_state()

        ConfirmDialog(self.winfo_toplevel(), title="删除官方账号",
                      message=f"确定要删除 \"{name}\" 吗？\n只会清除本应用保存的本机账号快照。",
                      on_confirm=do_delete)

    def _import_current(self):
        profile = profile_manager.import_current_claude()
        if profile:
            profile_manager.save_claude_profile(profile)
            profile_manager.set_active_claude(profile.name)
            profile_manager.set_active_claude_account(None)
            show_toast(self.winfo_toplevel(), f"已导入当前 Claude API 配置: {profile.name}")
            self.refresh()
            self._refresh_shell_state()
        else:
            show_toast(self.winfo_toplevel(), "未找到当前 Claude 第三方 API 配置", is_error=True)

    def _import_current_account(self):
        profile = profile_manager.import_current_claude_account()
        if profile:
            profile_manager.save_claude_account_profile(profile)
            if profile_manager.get_current_claude_account_name() == profile.name:
                profile_manager.set_active_claude_account(profile.name)
                message = f"已导入当前 Claude 官方账号: {profile.name}"
            else:
                message = f"已导入 Claude 官方账号: {profile.name}；点击切换后启用"
            show_toast(self.winfo_toplevel(), message)
            self.refresh()
            self._refresh_shell_state()
        else:
            show_toast(
                self.winfo_toplevel(),
                "未找到 Claude 官方登录凭据，请先在 Claude Code 登录账号",
                is_error=True,
            )
