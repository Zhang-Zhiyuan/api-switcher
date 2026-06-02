import customtkinter as ctk
from ui.widgets.masked_entry import MaskedEntry
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style
from core.providers import ProviderRegistry


class ProfileEditorDialog(ctk.CTkToplevel):
    """Dialog for creating or editing a Claude or Codex API configuration."""

    def __init__(self, master, title="编辑 API 配置", profile=None, profile_type="claude",
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
        self._test_busy = False
        self._refresh_busy = False
        self._refresh_buttons = []
        self._last_model_for_effort_options = None

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
        self._test_btn = ctk.CTkButton(
            btn_frame,
            text="测试连接",
            width=100,
            command=self._test_connection,
            **button_style("accent"),
        )
        self._test_btn.pack(side="left")

        # Save and Cancel buttons (right side)
        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=84,
            command=self.destroy,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        self._save_btn = ctk.CTkButton(
            btn_frame,
            text="保存",
            width=84,
            command=self._save,
            **button_style("primary"),
        )
        self._save_btn.pack(side="right")

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
        button = ctk.CTkButton(
            widget.master,
            text="刷新最佳",
            width=78,
            command=self._refresh_models,
            **button_style("secondary", compact=True),
        )
        button.pack(side="left", padx=(8, 0))
        self._refresh_buttons.append(button)

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
        self._add_field(parent, "API 提供商", "provider",
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

        self._add_field(parent, "API 配置名称", "name", p.name if p else "")
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
        self._bind_model_effort_refresh(model_widget)
        self._attach_refresh_button("model")

        # 推理力度
        self._add_field(parent, "推理力度", "effort_level",
                        ProviderRegistry.get_reasoning_efforts_for_model(
                            default_provider.name if default_provider else "",
                            model_widget.get(),
                        ),
                        "combo")
        if p:
            self._fields["effort_level"][0].set(p.effort_level)
        else:
            self._fields["effort_level"][0].set(
                ProviderRegistry.get_default_reasoning_effort_for_model(
                    default_provider.name if default_provider else "",
                    model_widget.get(),
                ) or "high"
            )

        self._add_field(parent, "权限模式", "permissions_mode",
                        ["dontAsk", "acceptEdits", "bypassPermissions", "default", "plan", "auto"], "combo")
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
                self._refresh_reasoning_effort_options("effort_level", self._current_claude_provider())

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

        self._add_field(parent, "API 配置名称", "name", p.name if p else "")
        self._add_field(parent, "API Key", "api_key", "", "masked")

        # 模型选择 - 使用可编辑的 combobox
        model_widget = self._add_field(parent, "模型", "model", default_provider.supported_models if default_provider else [], "combo")
        model_widget.configure(state="normal")  # 允许手动输入
        if p:
            model_widget.set(p.model)
        else:
            model_widget.set(default_provider.default_model if default_provider else "")
        self._bind_model_effort_refresh(model_widget)
        self._attach_refresh_button("model")

        # 推理力度 - 改为下拉框
        self._add_field(parent, "推理力度", "model_reasoning_effort",
                        ProviderRegistry.get_reasoning_efforts_for_model(
                            default_provider.name if default_provider else "",
                            model_widget.get(),
                        ),
                        "combo")
        if p:
            self._fields["model_reasoning_effort"][0].set(p.model_reasoning_effort)
        else:
            self._fields["model_reasoning_effort"][0].set(
                ProviderRegistry.get_default_reasoning_effort_for_model(
                    default_provider.name if default_provider else "",
                    model_widget.get(),
                ) or "high"
            )

        self._add_field(parent, "自定义端点", "custom_base_url", p.custom_base_url if p else "")
        self._add_field(parent, "自定义名称", "custom_name", p.custom_name if p else "")
        wire_api_widget = self._add_field(parent, "Wire API", "custom_wire_api", ["auto", "responses"], "combo")
        wire_api_widget.set(self._display_wire_api(p.custom_wire_api if p else None))
        self._add_field(parent, "环境变量名", "custom_env_key", p.custom_env_key if p else "OPENAI_API_KEY")

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
                self._set_wire_api_value(p.custom_wire_api)
                self._fields["custom_env_key"][0].delete(0, "end")
                self._fields["custom_env_key"][0].insert(0, p.custom_env_key or ProviderRegistry.get_codex_env_key_for_profile(p))
                self._refresh_reasoning_effort_options("model_reasoning_effort", self._current_codex_provider())

    def _get_value(self, key):
        widget, ftype = self._fields[key]
        if ftype == "switch":
            return widget.get() == 1
        elif ftype == "masked":
            return widget.get()
        else:
            return widget.get()

    def _display_wire_api(self, wire_api: str | None) -> str:
        wire_api = str(wire_api or "").strip().lower()
        return wire_api if wire_api == "responses" else "auto"

    def _set_wire_api_value(self, wire_api: str | None) -> None:
        if "custom_wire_api" not in self._fields:
            return
        widget, _ = self._fields["custom_wire_api"]
        value = self._display_wire_api(wire_api)
        try:
            widget.set(value)
        except Exception:
            widget.delete(0, "end")
            widget.insert(0, value)

    def _show_error(self, message: str) -> None:
        self._error_label.configure(text=message, text_color=COLORS["danger"])

    def _show_status(self, message: str, color: str = "muted") -> None:
        self._error_label.configure(text=message, text_color=COLORS[color])

    def _provider_note(self, provider, is_codex: bool) -> str:
        if provider is None:
            return "仅支持第三方 API 配置；官方账号登录态在“官方账号”区单独导入和切换。"

        parts = []
        if provider.notes:
            parts.append(provider.notes)

        if provider.reasoning_efforts:
            parts.append(f"推理力度会随模型调整，基础可选: {', '.join(provider.reasoning_efforts)}；Opus 类模型会额外显示 max。")
        else:
            parts.append("该 provider 不暴露推理力度；保存时会自动忽略推理力度字段。")

        if is_codex:
            parts.append(f"Codex wire_api: {provider.wire_api}；默认环境变量: {provider.codex_env_key}。")
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
        provider = ProviderRegistry.get_provider_by_display_name(display_name)
        if provider:
            return provider
        return ProviderRegistry.get_provider("custom")

    def _current_claude_provider(self):
        return ProviderRegistry.get_provider_by_display_name(self._fields["provider"][0].get())

    def _current_custom_provider_name(self) -> str | None:
        for key in ("custom_name", "custom_provider_name"):
            if key not in self._fields:
                continue
            value = str(self._fields[key][0].get() or "").strip()
            if value:
                return value
        return getattr(self._profile, "custom_name", None) or getattr(self._profile, "custom_provider_name", None)

    def _bind_model_effort_refresh(self, model_widget) -> None:
        model_widget.configure(command=lambda _value: self._on_model_change())
        try:
            model_widget.bind("<FocusOut>", lambda _event: self._on_model_change())
            model_widget.bind("<Return>", lambda _event: self._on_model_change())
        except Exception:
            pass

    def _on_model_change(self) -> None:
        model = self._fields["model"][0].get().strip() if "model" in self._fields else ""
        force_model_default = model != self._last_model_for_effort_options
        if self._profile_type == "claude":
            self._refresh_reasoning_effort_options(
                "effort_level",
                self._current_claude_provider(),
                force_model_default=force_model_default,
            )
        else:
            self._refresh_reasoning_effort_options(
                "model_reasoning_effort",
                self._current_codex_provider(),
                force_model_default=force_model_default,
            )

    def _refresh_reasoning_effort_options(self, field_key: str, provider, force_model_default: bool = False) -> None:
        if field_key not in self._fields or "model" not in self._fields:
            return
        effort_widget, _ = self._fields[field_key]
        effort_row = effort_widget.master
        model = self._fields["model"][0].get().strip()
        custom_name = self._current_custom_provider_name()
        efforts = ProviderRegistry.get_reasoning_efforts_for_model(
            provider.name if provider else "",
            model,
            custom_name,
        )
        if not efforts:
            effort_row.pack_forget()
            self._last_model_for_effort_options = model
            return

        effort_row.pack(fill="x", pady=5)
        effort_widget.configure(values=efforts)

        current = str(effort_widget.get() or "").strip()
        preferred = ProviderRegistry.get_default_reasoning_effort_for_model(
            provider.name if provider else "",
            model,
            custom_name,
        )
        should_use_preferred = current not in efforts
        if force_model_default and preferred in efforts and current in {"", "high", "xhigh"} and current != preferred:
            should_use_preferred = True

        if should_use_preferred:
            effort_widget.set(preferred if preferred in efforts else efforts[0])
        self._last_model_for_effort_options = model

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

        self._refresh_reasoning_effort_options("effort_level", provider, force_model_default=True)

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
            self._set_wire_api_value(provider.wire_api)
        if "custom_env_key" in self._fields:
            self._fields["custom_env_key"][0].delete(0, "end")
            self._fields["custom_env_key"][0].insert(0, provider.codex_env_key)
        if "model" in self._fields:
            if provider.supported_models:
                self._fields["model"][0].configure(values=provider.supported_models)
                self._fields["model"][0].set(provider.default_model)
            else:
                self._fields["model"][0].configure(values=[""])
                self._fields["model"][0].set("")
        self._refresh_reasoning_effort_options("model_reasoning_effort", provider, force_model_default=True)

    def _test_connection(self):
        """Test API connection with current settings."""
        from core.api_tester import APITester
        import threading

        if self._test_busy:
            return

        data = self._collect_data()

        # Validate required fields
        if self._profile_type == "claude":
            provider = self._current_claude_provider()
            api_key = self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None))
            base_url = data.get("base_url") or (provider.base_url_for_claude() if provider else "https://api.anthropic.com")
            model = data.get("model") or ""

            if not api_key:
                self._show_error("请先输入 Auth Token，或保存过带密钥的 API 配置")
                return
            if not base_url:
                self._show_error("请先填写 API 端点")
                return

        else:  # codex
            api_key = self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None))
            if not api_key:
                self._show_error("请先输入 API Key，或保存过带密钥的 API 配置")
                return

            provider = self._current_codex_provider()
            if provider:
                base_url = data.get("custom_base_url") or provider.base_url_for_codex()
                model = data.get("model") or ""
            else:
                base_url = data.get("custom_base_url")
                model = data.get("model") or ""
            if not base_url:
                self._show_error("请先填写 API 端点")
                return

        self._set_test_busy(True)
        self._show_status("正在测试连接...", "warning")

        # Run test in background thread
        def run_test():
            try:
                if self._profile_type == "claude":
                    result = APITester.test_claude_api(api_key, base_url, model)
                else:
                    result = APITester.benchmark_openai_wire_apis(
                        api_key,
                        base_url,
                        model,
                        repeat_count=3,
                        wire_apis=("responses",),
                    )
            except Exception as exc:
                from core.api_tester import TestResult

                result = TestResult(False, f"测试失败: {type(exc).__name__}", error_details=str(exc)[:400])

            # Show result dialog in main thread
            self._safe_after(lambda: self._apply_test_result(result, data.get("name", "")))

        thread = threading.Thread(target=run_test, name="api-profile-test", daemon=True)
        thread.start()

    def _set_test_busy(self, busy: bool) -> None:
        self._test_busy = busy
        try:
            self._test_btn.configure(
                state="disabled" if busy else "normal",
                text="测试中..." if busy else "测试连接",
            )
        except Exception:
            pass

    def _apply_test_result(self, result, profile_name: str):
        if not self.winfo_exists():
            return
        self._set_test_busy(False)
        if getattr(result, "selected_model", None) and "model" in self._fields:
            self._fields["model"][0].set(result.selected_model)
            self._on_model_change()
        if getattr(result, "recommended_wire_api", None) and "custom_wire_api" in self._fields:
            self._set_wire_api_value(result.recommended_wire_api)
        self._show_test_result(result, profile_name)

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

        if self._refresh_busy:
            return

        data = self._collect_data()

        if self._profile_type == "claude":
            provider = self._current_claude_provider()
            api_key = self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None))
            base_url = data.get("base_url") or (provider.base_url_for_claude() if provider else "https://api.anthropic.com")
            fallback_models = provider.supported_models if provider else []

            def fetcher():
                return APITester.fetch_claude_models(api_key, base_url)
        else:
            provider = self._current_codex_provider()
            api_key = self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None))
            base_url = data.get("custom_base_url") or (provider.base_url_for_codex() if provider else "")
            fallback_models = provider.supported_models if provider else []

            def fetcher():
                return APITester.fetch_openai_models(api_key, base_url)

        if not base_url and not fallback_models:
            self._show_error("请先填写 API 端点")
            return

        if not api_key:
            if fallback_models:
                self._apply_model_list(fallback_models, "未输入密钥，已使用内置模型列表并选择推荐模型", is_error=False)
            else:
                self._show_error("刷新远程模型列表需要先输入 API Key")
            return

        self._set_refresh_busy(True)
        self._show_status("正在刷新模型列表...", "warning")

        def run_refresh():
            try:
                result = fetcher()
            except Exception as exc:
                from core.api_tester import ModelListResult

                result = ModelListResult(
                    success=False,
                    message=f"刷新失败: {type(exc).__name__}",
                    error_details=str(exc)[:400],
                )
            self._safe_after(lambda: self._handle_model_refresh_result(result, fallback_models, provider))

        threading.Thread(target=run_refresh, name="api-model-refresh", daemon=True).start()

    def _set_refresh_busy(self, busy: bool) -> None:
        self._refresh_busy = busy
        state = "disabled" if busy else "normal"
        text = "刷新中..." if busy else "刷新最佳"
        for button in self._refresh_buttons:
            try:
                button.configure(state=state, text=text)
            except Exception:
                pass

    def _remote_models_for_selection(self, remote_models: list[str], fallback_models: list[str], provider) -> list[str]:
        if provider and getattr(provider, "name", "") == "anthropic":
            return list(dict.fromkeys(remote_models + fallback_models))
        return remote_models

    def _apply_model_list(self, models: list[str], message: str, is_error: bool = False,
                          preferred_model: str | None = None,
                          model_metadata: dict | None = None) -> None:
        from core.api_tester import APITester

        if not self.winfo_exists():
            return
        if not models:
            self._show_error("没有可用模型")
            return

        model_widget, _ = self._fields["model"]
        sorted_models = APITester.sort_models_by_preference(models, model_metadata)
        recommended = (
            preferred_model
            if preferred_model in sorted_models
            else APITester.recommend_best_model(sorted_models, model_metadata)
        )
        model_widget.configure(values=sorted_models)
        model_widget.set(recommended or sorted_models[0])
        self._on_model_change()
        suffix = f"；已选择推荐模型 {recommended}" if recommended else ""
        self._show_error(message + suffix) if is_error else self._show_status(message + suffix, "success")

    def _handle_model_refresh_result(self, result, fallback_models: list[str], provider):
        if not self.winfo_exists():
            return
        self._set_refresh_busy(False)
        if result.success and result.models:
            models = self._remote_models_for_selection(result.models, fallback_models, provider)
            preferred = result.recommended_model
            if provider and getattr(provider, "name", "") == "anthropic":
                preferred = "opus[1m]" if "opus[1m]" in models else preferred
            self._apply_model_list(
                models,
                result.message,
                is_error=False,
                preferred_model=preferred,
                model_metadata=getattr(result, "model_metadata", None),
            )
            return

        if fallback_models:
            details = f": {result.error_details}" if result.error_details else ""
            self._apply_model_list(fallback_models, f"刷新失败，已使用内置模型列表。{result.message}{details}", is_error=True)
        else:
            details = f": {result.error_details}" if result.error_details else ""
            self._show_error(f"刷新模型失败。{result.message}{details}")

    def _resolve_latest_model_for_save(self, api_key: str, base_url: str, provider, is_codex: bool) -> str:
        from core.api_tester import APITester

        try:
            result = (
                APITester.fetch_openai_models(api_key, base_url, timeout=12)
                if is_codex
                else APITester.fetch_claude_models(api_key, base_url, timeout=12)
            )
            if result.success:
                return result.latest_model or result.recommended_model or ""
        except Exception:
            pass
        return (provider.default_model if provider else "") or ("gpt-5.5" if is_codex else "claude-sonnet-4")

    def _recommend_wire_api_for_save(self, api_key: str, base_url: str, model: str, provider) -> str:
        from core.api_tester import APITester

        try:
            result = APITester.benchmark_openai_wire_apis(
                api_key,
                base_url,
                model,
                timeout=8,
                repeat_count=3,
                wire_apis=("responses",),
            )
            if result.recommended_wire_api:
                return result.recommended_wire_api
        except Exception:
            pass
        return (provider.wire_api if provider else "") or "responses"

    def _save(self):
        data = self._collect_data()

        if not data.get("name"):
            self._show_error("请输入 API 配置名称")
            return
        if self._profile_type == "claude" and not self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None)):
            self._show_error("请先输入 Auth Token")
            return
        if self._profile_type == "codex" and not self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None)):
            self._show_error("请先输入 API Key")
            return

        # 处理 Claude API 配置的 provider 字段
        if self._profile_type == "claude" and "provider" in data:
            provider_display_name = data["provider"]
            provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
            if provider:
                data["provider"] = provider.name
            api_key = self._get_secret_value("auth_token", getattr(self._profile, "auth_token_ref", None))
            base_url = data.get("base_url") or (provider.base_url_for_claude() if provider else "")
            if not data.get("model"):
                self._show_status("模型为空，正在从接口模型列表选择最新模型...", "warning")
                self.update_idletasks()
                data["model"] = self._resolve_latest_model_for_save(api_key, base_url, provider, is_codex=False)
            if not data.get("model"):
                self._show_error("无法自动选择模型，请手动填写模型名称")
                return

        # 处理 Codex API 配置的 provider 字段
        if self._profile_type == "codex" and "codex_provider" in data:
            provider_display_name = data["codex_provider"]
            provider = ProviderRegistry.get_provider_by_display_name(provider_display_name)
            if provider:
                data["model_provider"] = provider.name
                data["custom_base_url"] = data.get("custom_base_url") or provider.base_url_for_codex()
                data["custom_name"] = data.get("custom_name") or provider.display_name
                data["custom_env_key"] = data.get("custom_env_key") or provider.codex_env_key
                data["custom_requires_openai_auth"] = provider.requires_openai_auth
            else:
                data["model_provider"] = "custom"
                data["custom_requires_openai_auth"] = False
            del data["codex_provider"]

            if not data.get("custom_base_url"):
                self._show_error("第三方或自定义 Provider 需要 API 端点")
                return
            api_key = self._get_secret_value("api_key", getattr(self._profile, "api_key_ref", None))
            wire_api = str(data.get("custom_wire_api") or "").strip().lower()
            data["custom_wire_api"] = "" if wire_api == "auto" else wire_api
            if data["custom_wire_api"] and data["custom_wire_api"] != "responses":
                self._show_error("Wire API 只能选择 auto 或 responses")
                return
            if not data.get("model"):
                self._show_status("模型为空，正在从接口模型列表选择最新模型...", "warning")
                self.update_idletasks()
                data["model"] = self._resolve_latest_model_for_save(
                    api_key,
                    data["custom_base_url"],
                    provider,
                    is_codex=True,
                )
            if not data.get("model"):
                self._show_error("无法自动选择模型，请手动填写模型名称")
                return
            if not data.get("custom_wire_api"):
                self._show_status("Wire API 为空，正在三轮测试 responses 可用性...", "warning")
                self.update_idletasks()
                data["custom_wire_api"] = self._recommend_wire_api_for_save(
                    api_key,
                    data["custom_base_url"],
                    data["model"],
                    provider,
                )

        if self._on_save:
            self._on_save(data, self._profile)
        self.destroy()
