import customtkinter as ctk
from ui.widgets.masked_entry import MaskedEntry
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style
from core.providers import CODEX_REASONING_EFFORTS, ProviderRegistry


class ProfileEditorDialog(ctk.CTkToplevel):
    """Dialog for creating or editing a Claude or Codex profile."""

    def __init__(self, master, title="Edit Profile", profile=None, profile_type="claude",
                 on_save=None):
        super().__init__(master)
        self.title(title)
        self.geometry("640x720")
        self.minsize(560, 540)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_save = on_save
        self._profile = profile
        self._profile_type = profile_type
        self._provider_note_label = None

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
        self._error_label = ctk.CTkLabel(
            self,
            text="",
            text_color=COLORS["danger"],
            font=font(12),
        )
        self._error_label.pack(fill="x", padx=18, pady=(0, 6))

        if profile_type == "claude":
            self._build_claude_fields(scroll)
        else:
            self._build_codex_fields(scroll)

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 16))

        # Test connection button (left side)
        ctk.CTkButton(
            btn_frame,
            text="测试连接",
            width=100,
            command=self._test_connection,
            **button_style("accent"),
        ).pack(side="left")

        # Save and Cancel buttons (right side)
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
        elif field_type == "switch":
            widget = ctk.CTkSwitch(
                row,
                text="开启",
                text_color=COLORS["text"],
                progress_color=COLORS["success"],
                button_color=COLORS["text"],
            )
            if value:
                widget.select()
            widget.pack(side="left")

        self._fields[key] = (widget, field_type)
        return widget

    def _add_provider_note(self, parent):
        row = ctk.CTkFrame(parent, fg_color=COLORS["surface"], corner_radius=8)
        row.pack(fill="x", pady=(2, 8))
        label = ctk.CTkLabel(
            row,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            justify="left",
            anchor="w",
            padx=12,
            pady=9,
        )
        label.pack(fill="x")
        bind_wraplength(row, label, padding=28, min_width=260, max_width=520)
        self._provider_note_label = label

    def _attach_refresh_button(self, key: str) -> None:
        widget, _ = self._fields[key]
        ctk.CTkButton(
            widget.master,
            text="刷新",
            width=64,
            command=self._refresh_models,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(8, 0))

    def _build_claude_fields(self, parent):
        p = self._profile
        third_party_providers = [
            provider
            for provider in ProviderRegistry.get_claude_providers()
            if provider.name != "anthropic"
        ]
        default_provider = third_party_providers[0] if third_party_providers else ProviderRegistry.get_provider("custom")
        provider_options = [provider.display_name for provider in third_party_providers]

        # Provider 选择
        self._add_field(parent, "提供商", "provider",
                        provider_options, "combo")
        if p and hasattr(p, 'provider'):
            provider_config = ProviderRegistry.get_provider(p.provider)
            if provider_config and provider_config.name != "anthropic":
                self._fields["provider"][0].set(provider_config.display_name)
            elif default_provider:
                self._fields["provider"][0].set(default_provider.display_name)
        else:
            if default_provider:
                self._fields["provider"][0].set(default_provider.display_name)

        # 绑定 Provider 变化事件
        self._fields["provider"][0].configure(command=self._on_claude_provider_change)
        self._add_provider_note(parent)

        self._add_field(parent, "名称", "name", p.name if p else "")
        self._add_field(parent, "API 端点", "base_url",
                        p.base_url if p else (default_provider.base_url_for_claude() if default_provider else ""))
        self._add_field(parent, "Auth Token", "auth_token", "", "masked")

        # 模型选择 - 使用可编辑的 combobox
        model_widget = self._add_field(parent, "模型", "model",
                        default_provider.supported_models if default_provider else [], "combo")
        model_widget.configure(state="normal")  # 允许手动输入
        if p:
            model_widget.set(p.model)
        else:
            model_widget.set(default_provider.default_model if default_provider else "")
        self._attach_refresh_button("model")

        # 推理力度
        self._add_field(parent, "推理力度", "effort_level",
                        default_provider.reasoning_efforts if default_provider else [], "combo")
        if p:
            self._fields["effort_level"][0].set(p.effort_level)
        else:
            self._fields["effort_level"][0].set("high")

        self._add_field(parent, "权限模式", "permissions_mode",
                        ["bypassPermissions", "default"], "combo")
        if p:
            self._fields["permissions_mode"][0].set(p.permissions_mode)
        else:
            self._fields["permissions_mode"][0].set("bypassPermissions")

        self._add_field(parent, "跳过危险提示", "skip_dangerous_prompt",
                        p.skip_dangerous_prompt if p else True, "switch")

        # 自定义提供商名称（仅当 Provider = Custom 时显示）
        self._add_field(parent, "自定义名称", "custom_provider_name",
                        p.custom_provider_name if (p and hasattr(p, 'custom_provider_name')) else "")

        # 初始化时更新字段
        self._on_claude_provider_change(self._fields["provider"][0].get())
        if p:
            if getattr(p, "provider", "anthropic") != "anthropic":
                self._fields["base_url"][0].delete(0, "end")
                self._fields["base_url"][0].insert(0, p.base_url)
                self._fields["model"][0].set(p.model)
                self._fields["effort_level"][0].set(p.effort_level)

    def _build_codex_fields(self, parent):
        p = self._profile
        codex_providers = ProviderRegistry.get_codex_providers()
        default_provider = codex_providers[0] if codex_providers else ProviderRegistry.get_provider("custom")

        # Provider 选择（扩展）
        provider_options = [provider.display_name for provider in codex_providers]
        self._add_field(parent, "Provider", "codex_provider", provider_options, "combo")

        # 设置默认值
        if p and p.model_provider != "openai":
            provider_config = ProviderRegistry.get_provider(p.model_provider)
            if provider_config:
                self._fields["codex_provider"][0].set(provider_config.display_name)
            elif p.custom_name:
                self._fields["codex_provider"][0].set(p.custom_name)
            else:
                self._fields["codex_provider"][0].set("Custom")
        else:
            if default_provider:
                self._fields["codex_provider"][0].set(default_provider.display_name)

        # 绑定 Provider 变化事件
        self._fields["codex_provider"][0].configure(command=self._on_codex_provider_change)
        self._add_provider_note(parent)

        self._add_field(parent, "名称", "name", p.name if p else "")
        self._add_field(parent, "API Key", "api_key", "", "masked")

        # 模型选择 - 使用可编辑的 combobox
        model_widget = self._add_field(parent, "模型", "model", default_provider.supported_models if default_provider else [], "combo")
        model_widget.configure(state="normal")  # 允许手动输入
        if p:
            model_widget.set(p.model)
        else:
            model_widget.set(default_provider.default_model if default_provider else "")
        self._attach_refresh_button("model")

        # 推理力度 - 改为下拉框
        self._add_field(parent, "推理力度", "model_reasoning_effort",
                        CODEX_REASONING_EFFORTS, "combo")
        if p:
            self._fields["model_reasoning_effort"][0].set(p.model_reasoning_effort)
        else:
            self._fields["model_reasoning_effort"][0].set("high")

        self._add_field(parent, "自定义端点", "custom_base_url", p.custom_base_url if p else "")
        self._add_field(parent, "自定义名称", "custom_name", p.custom_name if p else "")
        self._add_field(parent, "Wire API", "custom_wire_api", p.custom_wire_api if p else "responses")

        self._add_field(parent, "审批策略", "approval_policy", ["never", "auto", "manual"], "combo")
        if p:
            self._fields["approval_policy"][0].set(p.approval_policy)
        else:
            self._fields["approval_policy"][0].set("never")

        self._add_field(parent, "沙盒模式", "sandbox_mode",
                        ["danger-full-access", "read-only", "off"], "combo")
        if p:
            self._fields["sandbox_mode"][0].set(p.sandbox_mode)
        else:
            self._fields["sandbox_mode"][0].set("danger-full-access")

        # 初始化时更新字段
        self._on_codex_provider_change(self._fields["codex_provider"][0].get())
        if p:
            if getattr(p, "model_provider", "openai") != "openai":
                self._fields["model"][0].set(p.model)
                self._fields["model_reasoning_effort"][0].set(p.model_reasoning_effort)
                self._fields["custom_base_url"][0].delete(0, "end")
                self._fields["custom_base_url"][0].insert(0, p.custom_base_url or "")
                self._fields["custom_name"][0].delete(0, "end")
                self._fields["custom_name"][0].insert(0, p.custom_name or "")
                self._fields["custom_wire_api"][0].delete(0, "end")
                self._fields["custom_wire_api"][0].insert(0, p.custom_wire_api or "responses")

    def _get_value(self, key):
        widget, ftype = self._fields[key]
        if ftype == "switch":
            return widget.get() == 1
        elif ftype == "masked":
            return widget.get()
        else:
            return widget.get()

    def _show_error(self, message: str) -> None:
        self._error_label.configure(text=message, text_color=COLORS["danger"])

    def _show_status(self, message: str, color: str = "muted") -> None:
        self._error_label.configure(text=message, text_color=COLORS[color])

    def _provider_note(self, provider, is_codex: bool) -> str:
        if provider is None:
            return "仅支持第三方 API Profile；官方账号登录态不会保存或切换。"

        parts = []
        if provider.notes:
            parts.append(provider.notes)

        if provider.reasoning_efforts:
            parts.append(f"推理力度可选: {', '.join(provider.reasoning_efforts)}。")
        else:
            parts.append("该 provider 不暴露推理力度；保存时会自动忽略推理力度字段。")

        if is_codex:
            parts.append(f"Codex wire_api: {provider.wire_api}。")
        if provider.name == "kimi":
            parts.append("Kimi 国际平台默认使用 .ai；中国平台密钥可把端点改为 https://api.moonshot.cn/v1。")
        if provider.name == "glm":
            parts.append("GLM 使用 Coding Plan 兼容端点；Claude Code 会写入 GLM 推荐的模型环境变量。")

        return " ".join(parts)

    def _update_provider_note(self, provider, is_codex: bool) -> None:
        if self._provider_note_label is not None:
            self._provider_note_label.configure(text=self._provider_note(provider, is_codex))

    def _collect_data(self) -> dict:
        data = {}
        for key in self._fields:
            value = self._get_value(key)
            data[key] = value.strip() if isinstance(value, str) else value
        return data

    def _get_secret_value(self, field_key: str, ref: str | None) -> str:
        value = self._get_value(field_key).strip() if field_key in self._fields else ""
        if value:
            return value
        if ref:
            from core import security
            return security.get_secret(ref) or ""
        return ""

    def _current_codex_provider(self):
        display_name = self._fields["codex_provider"][0].get()
        return ProviderRegistry.get_provider_by_display_name(display_name)

    def _current_claude_provider(self):
        return ProviderRegistry.get_provider_by_display_name(self._fields["provider"][0].get())

    def _on_claude_provider_change(self, provider_display_name):
        """当 Claude Provider 改变时更新相关字段"""
        provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
        if not provider:
            return
        self._update_provider_note(provider, is_codex=False)

        # 更新 API 端点
        if "base_url" in self._fields:
            base_url_widget = self._fields["base_url"][0]
            base_url_widget.delete(0, "end")
            base_url_widget.insert(0, provider.base_url_for_claude())

        # 更新模型列表
        if "model" in self._fields:
            model_widget = self._fields["model"][0]
            if provider.supported_models:
                model_widget.configure(values=provider.supported_models)
                model_widget.set(provider.default_model)
            else:
                model_widget.configure(values=[""])
                model_widget.set("")

        # 更新推理力度可见性
        if "effort_level" in self._fields:
            effort_widget, _ = self._fields["effort_level"]
            effort_row = effort_widget.master
            if provider.reasoning_efforts:
                effort_row.pack(fill="x", pady=5)
                effort_widget.configure(values=provider.reasoning_efforts)
                if effort_widget.get() not in provider.reasoning_efforts:
                    effort_widget.set(provider.reasoning_efforts[0])
            else:
                effort_row.pack_forget()

        # 更新自定义名称可见性
        if "custom_provider_name" in self._fields:
            custom_name_widget, _ = self._fields["custom_provider_name"]
            custom_name_row = custom_name_widget.master
            if provider.name == "custom":
                custom_name_row.pack(fill="x", pady=5)
            else:
                custom_name_row.pack_forget()

    def _on_codex_provider_change(self, provider_display_name):
        """当 Codex Provider 改变时更新相关字段"""
        provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
        if not provider:
            self._update_provider_note(None, is_codex=True)
            return

        self._update_provider_note(provider, is_codex=True)
        if "custom_base_url" in self._fields:
            self._fields["custom_base_url"][0].delete(0, "end")
            self._fields["custom_base_url"][0].insert(0, provider.base_url_for_codex())
        if "custom_name" in self._fields:
            self._fields["custom_name"][0].delete(0, "end")
            self._fields["custom_name"][0].insert(0, provider.display_name)
        if "custom_wire_api" in self._fields:
            self._fields["custom_wire_api"][0].delete(0, "end")
            self._fields["custom_wire_api"][0].insert(0, provider.wire_api)
        if "model" in self._fields:
            if provider.supported_models:
                self._fields["model"][0].configure(values=provider.supported_models)
                self._fields["model"][0].set(provider.default_model)
            else:
                self._fields["model"][0].configure(values=[""])
                self._fields["model"][0].set("")
        # 更新推理力度可见性
        if "model_reasoning_effort" in self._fields:
            effort_widget, _ = self._fields["model_reasoning_effort"]
            effort_row = effort_widget.master
            if provider.reasoning_efforts:
                effort_row.pack(fill="x", pady=5)
                effort_widget.configure(values=provider.reasoning_efforts)
                if effort_widget.get() not in provider.reasoning_efforts:
                    effort_widget.set(provider.reasoning_efforts[0])
            else:
                effort_row.pack_forget()

    def _test_connection(self):
        """Test API connection with current settings."""
        from core.api_tester import APITester
        import threading

        data = self._collect_data()

        # Validate required fields
        if self._profile_type == "claude":
            provider = self._current_claude_provider()
            api_key = self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None))
            base_url = data.get("base_url") or (provider.base_url_for_claude() if provider else "https://api.anthropic.com")
            model = data.get("model") or (provider.default_model if provider else "claude-sonnet-4")

            if not api_key:
                self._show_error("请先输入 Auth Token，或保存过带密钥的 Profile")
                return
            if not base_url:
                self._show_error("请先填写 API 端点")
                return

        else:  # codex
            api_key = self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None))
            if not api_key:
                self._show_error("请先输入 API Key，或保存过带密钥的 Profile")
                return

            provider = self._current_codex_provider()
            if provider:
                base_url = data.get("custom_base_url") or provider.base_url_for_codex()
                model = data.get("model") or provider.default_model
            else:
                base_url = data.get("custom_base_url")
                model = data.get("model")
            if not base_url:
                self._show_error("请先填写 API 端点")
                return

        # Show testing message
        self._show_status("正在测试连接...", "warning")

        # Run test in background thread
        def run_test():
            if self._profile_type == "claude":
                result = APITester.test_claude_api(api_key, base_url, model)
            else:
                result = APITester.test_openai_api(api_key, base_url, model)

            # Show result dialog in main thread
            self._safe_after(lambda: self._show_test_result(result, data.get("name", "")))

        thread = threading.Thread(target=run_test, daemon=True)
        thread.start()

    def _show_test_result(self, result, profile_name: str):
        """Show test result dialog."""
        from ui.dialogs.api_test_result_dialog import APITestResultDialog

        if not self.winfo_exists():
            return
        self._show_status("")
        APITestResultDialog(self, result, profile_name)

    def _safe_after(self, callback) -> None:
        """Schedule UI work from a background thread if the dialog still exists."""
        try:
            self.after(0, callback)
        except Exception:
            pass

    def _refresh_models(self):
        """Refresh model list from provider API, falling back to bundled presets."""
        from core.api_tester import APITester
        import threading

        data = self._collect_data()

        if self._profile_type == "claude":
            provider = self._current_claude_provider()
            api_key = self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None))
            base_url = data.get("base_url") or (provider.base_url_for_claude() if provider else "https://api.anthropic.com")
            fallback_models = provider.supported_models if provider else []
            fetcher = lambda: APITester.fetch_claude_models(api_key, base_url)
        else:
            provider = self._current_codex_provider()
            api_key = self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None))
            base_url = data.get("custom_base_url") or (provider.base_url_for_codex() if provider else "")
            fallback_models = provider.supported_models if provider else []
            fetcher = lambda: APITester.fetch_openai_models(api_key, base_url)

        if not base_url and not fallback_models:
            self._show_error("请先填写 API 端点")
            return

        if not api_key:
            if fallback_models:
                self._apply_model_list(fallback_models, "未输入密钥，已使用内置模型列表", is_error=False)
            else:
                self._show_error("刷新远程模型列表需要先输入 API Key")
            return

        self._show_status("正在刷新模型列表...", "warning")

        def run_refresh():
            result = fetcher()
            self._safe_after(lambda: self._handle_model_refresh_result(result, fallback_models))

        threading.Thread(target=run_refresh, daemon=True).start()

    def _apply_model_list(self, models: list[str], message: str, is_error: bool = False) -> None:
        if not self.winfo_exists():
            return
        if not models:
            self._show_error("没有可用模型")
            return

        model_widget, _ = self._fields["model"]
        current = model_widget.get()
        model_widget.configure(values=models)
        model_widget.set(current if current in models else models[0])
        self._show_error(message) if is_error else self._show_status(message, "success")

    def _handle_model_refresh_result(self, result, fallback_models: list[str]):
        if not self.winfo_exists():
            return
        if result.success and result.models:
            self._apply_model_list(result.models, result.message, is_error=False)
            return

        if fallback_models:
            details = f": {result.error_details}" if result.error_details else ""
            self._apply_model_list(fallback_models, f"刷新失败，已使用内置模型列表。{result.message}{details}", is_error=True)
        else:
            details = f": {result.error_details}" if result.error_details else ""
            self._show_error(f"刷新模型失败。{result.message}{details}")

    def _save(self):
        data = self._collect_data()

        if not data.get("name"):
            self._show_error("请输入 Profile 名称")
            return
        if not data.get("model"):
            self._show_error("请输入模型名称")
            return
        if self._profile_type == "claude" and not self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None)):
            self._show_error("请先输入 Auth Token")
            return
        if self._profile_type == "codex" and not self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None)):
            self._show_error("请先输入 API Key")
            return

        # 处理 Claude Profile 的 provider 字段
        if self._profile_type == "claude" and "provider" in data:
            provider_display_name = data["provider"]
            provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
            if provider:
                data["provider"] = provider.name

        # 处理 Codex Profile 的 provider 字段
        if self._profile_type == "codex" and "codex_provider" in data:
            provider_display_name = data["codex_provider"]
            provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
            if provider:
                data["model_provider"] = provider.name
                data["custom_base_url"] = data.get("custom_base_url") or provider.base_url_for_codex()
                data["custom_name"] = data.get("custom_name") or provider.display_name
                data["custom_wire_api"] = data.get("custom_wire_api") or provider.wire_api
                data["custom_requires_openai_auth"] = provider.requires_openai_auth
            else:
                data["model_provider"] = "custom"
                data["custom_requires_openai_auth"] = False
            del data["codex_provider"]

            if not data.get("custom_base_url"):
                self._show_error("第三方或自定义 Provider 需要 API 端点")
                return

        if self._on_save:
            self._on_save(data, self._profile)
        self.destroy()
