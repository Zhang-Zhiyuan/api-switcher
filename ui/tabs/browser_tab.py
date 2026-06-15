import importlib
import threading

import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast


PROFILE_RENDER_BATCH_SIZE = 6


class _LazyModule:
    def __init__(self, module_name: str):
        self._module_name = module_name
        self._module = None
        self._lock = threading.RLock()

    def _load(self):
        module = self._module
        if module is not None:
            return module
        with self._lock:
            if self._module is None:
                self._module = importlib.import_module(self._module_name)
            return self._module

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


class _LazyAttribute:
    def __init__(self, module_name: str, attr_name: str):
        self._module = _LazyModule(module_name)
        self._attr_name = attr_name

    def _load(self):
        return getattr(self._module, self._attr_name)

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


profile_manager = _LazyModule("core.profile_manager")
browser_data_manager = _LazyAttribute("core.browser_data_manager", "browser_data_manager")
browser_launcher = _LazyAttribute("core.browser_launcher", "browser_launcher")
browser_profile_manager = _LazyAttribute("core.browser_profile_manager", "browser_profile_manager")
BrowserProfileEditorDialog = _LazyAttribute("ui.dialogs.browser_profile_editor", "BrowserProfileEditorDialog")
BulkOperationResultDialog = _LazyAttribute("ui.dialogs.bulk_operation_result_dialog", "BulkOperationResultDialog")
ConfirmDialog = _LazyAttribute("ui.dialogs.confirm_dialog", "ConfirmDialog")
DangerConfirmDialog = _LazyAttribute("ui.dialogs.danger_confirm_dialog", "DangerConfirmDialog")


def _diagnosis_bool(diagnosis: dict | None, key: str, default: bool = False) -> bool:
    if not isinstance(diagnosis, dict):
        return default
    return bool(diagnosis.get(key, default))


def _diagnosis_text(diagnosis: dict | None, key: str) -> str:
    if not isinstance(diagnosis, dict):
        return ""
    value = diagnosis.get(key)
    return str(value).strip() if value is not None else ""


def _browser_diagnosis_matches_filter(diagnosis: dict | None, filter_mode: str) -> bool:
    if filter_mode == "all":
        return True
    valid = _diagnosis_bool(diagnosis, "valid")
    executable_found = _diagnosis_bool(diagnosis, "executable_found")
    profile_path_exists = _diagnosis_bool(diagnosis, "profile_path_exists")
    browser_running = _diagnosis_bool(diagnosis, "browser_running")
    if filter_mode == "issues":
        return (not valid) or (not executable_found) or (not profile_path_exists) or browser_running
    if filter_mode == "launchable":
        return valid and executable_found and profile_path_exists
    if filter_mode == "resettable":
        return _diagnosis_bool(diagnosis, "can_full_reset")
    return True


def _browser_profiles_summary(profiles, diagnoses: dict, selected_names: set[str]) -> dict:
    profile_names = {p.name for p in profiles}
    diagnosis_items = [diagnoses.get(name, {}) for name in profile_names]
    issues_count = sum(1 for d in diagnosis_items if _browser_diagnosis_matches_filter(d, "issues"))
    launchable_count = sum(1 for d in diagnosis_items if _browser_diagnosis_matches_filter(d, "launchable"))
    resettable_count = sum(1 for d in diagnosis_items if _browser_diagnosis_matches_filter(d, "resettable"))
    return {
        "visible_names": profile_names,
        "total_count": len(profile_names),
        "issues_count": issues_count,
        "launchable_count": launchable_count,
        "resettable_count": resettable_count,
        "selected_count": len(selected_names & profile_names),
    }


def _visible_profile_names(profiles, diagnoses: dict, filter_mode: str) -> list[str]:
    return [
        p.name
        for p in profiles
        if _browser_diagnosis_matches_filter(diagnoses.get(p.name, {}), filter_mode)
    ]


def _diagnosis_failure(error: Exception) -> dict[str, bool | str]:
    message = str(error).strip() or error.__class__.__name__
    return {
        "valid": False,
        "executable_found": False,
        "profile_path_exists": False,
        "browser_running": False,
        "can_full_reset": False,
        "validation_error": f"诊断失败: {message}",
        "full_reset_reason": "诊断失败，暂不允许整目录清理",
    }


class BrowserTab(ctk.CTkScrollableFrame):
    """Tab for managing Chrome / Edge browser profiles."""

    FILTER_OPTIONS = {
        "全部": "all",
        "仅异常": "issues",
        "可启动": "launchable",
        "可重置": "resettable",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._filter_mode = "all"
        self._selected_names: set[str] = set()
        self._refresh_generation = 0
        self._profile_render_generation = 0
        self._profile_render_after_id = None
        self._cached_profiles = ()
        self._cached_active = ""
        self._cached_diagnoses = {}
        self._has_profile_cache = False
        self._build_ui()

    def destroy(self):
        self._cancel_profile_render()
        super().destroy()

    def _toast(self, message: str, is_error: bool = False):
        """Helper to show toast messages."""
        show_toast(self.winfo_toplevel(), message, is_error=is_error)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title_area, text="浏览器 Profile", text_color=COLORS["text"], font=font(18, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="管理 Chrome / Edge 多账号 Profile，并按 Profile 清理 ChatGPT / Claude 站点数据",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        action_bar = ctk.CTkFrame(header, fg_color="transparent")
        action_bar.pack(side="right", padx=(12, 0))
        ctk.CTkButton(action_bar, text="+ 新建 Profile", width=126, command=self._create_profile, **button_style("primary")).pack(side="left")
        ctk.CTkButton(action_bar, text="刷新全部诊断", width=122, command=self.refresh, **button_style("secondary")).pack(side="left", padx=(8, 0))

        quick_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        quick_bar.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(quick_bar, text="快速创建", text_color=COLORS["muted"], font=font(12)).pack(side="left", padx=(12, 0), pady=9)
        ctk.CTkButton(quick_bar, text="Chrome-ChatGPT", width=132, command=lambda: self._quick_create("chrome", "chatgpt"), **button_style("primary", compact=True)).pack(side="left", padx=(8, 0), pady=9)
        ctk.CTkButton(quick_bar, text="Chrome-Claude", width=126, command=lambda: self._quick_create("chrome", "claude"), **button_style("accent", compact=True)).pack(side="left", padx=(8, 0), pady=9)
        ctk.CTkButton(quick_bar, text="Edge-ChatGPT", width=120, command=lambda: self._quick_create("edge", "chatgpt"), **button_style("primary", compact=True)).pack(side="left", padx=(8, 0), pady=9)
        ctk.CTkButton(quick_bar, text="Edge-Claude", width=114, command=lambda: self._quick_create("edge", "claude"), **button_style("accent", compact=True)).pack(side="left", padx=(8, 12), pady=9)

        filter_bar = ctk.CTkFrame(self, fg_color="transparent")
        filter_bar.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(filter_bar, text="筛选", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._filter_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.FILTER_OPTIONS.keys()),
            width=160,
            command=self._on_filter_change,
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.pack(side="left", padx=(8, 0))

        self._stats_label = ctk.CTkLabel(filter_bar, text="", text_color=COLORS["muted"], font=font(12))
        self._stats_label.pack(side="right")

        bulk_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        bulk_bar.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkButton(bulk_bar, text="全选当前", width=96, command=self._select_visible, **button_style("secondary", compact=True)).pack(side="left", padx=(12, 0), pady=9)
        ctk.CTkButton(bulk_bar, text="清空选择", width=96, command=self._clear_selection, **button_style("secondary", compact=True)).pack(side="left", padx=(8, 0), pady=9)
        ctk.CTkButton(bulk_bar, text="批量清理 GPT", width=108, command=lambda: self._bulk_clear_sites("chatgpt"), **button_style("warning", compact=True)).pack(side="left", padx=(12, 0), pady=9)
        ctk.CTkButton(bulk_bar, text="批量清理 Claude", width=122, command=lambda: self._bulk_clear_sites("claude"), **button_style("warning", compact=True)).pack(side="left", padx=(8, 0), pady=9)
        ctk.CTkButton(bulk_bar, text="批量清理两者", width=122, command=lambda: self._bulk_clear_sites("both"), **button_style("warning", compact=True)).pack(side="left", padx=(8, 12), pady=9)

        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="x", padx=14, pady=(0, 12))

        self.after(20, self.refresh)

    def refresh(self):
        if not self._cards_frame:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._cancel_profile_render()
        for w in self._cards_frame.winfo_children():
            w.destroy()
        if self._stats_label:
            self._stats_label.configure(text="正在诊断浏览器 Profile...")
        ctk.CTkLabel(
            self._cards_frame,
            text="正在诊断浏览器 Profile...",
            text_color=COLORS["muted"],
            font=font(13),
        ).pack(fill="x", pady=(22, 6))

        def worker():
            try:
                summary = profile_manager.get_browser_profiles_summary()
                profiles = tuple(summary.get("profiles") or ())
                active = summary.get("active") or ""
                diagnoses = {}
                for p in profiles:
                    try:
                        diagnoses[p.name] = browser_profile_manager.diagnose_profile(p)
                    except Exception as exc:
                        diagnoses[p.name] = _diagnosis_failure(exc)
                payload = {"ok": True, "profiles": profiles, "active": active, "diagnoses": diagnoses, "error": ""}
            except Exception as exc:
                payload = {"ok": False, "profiles": [], "active": "", "diagnoses": {}, "error": str(exc)}

            def finish():
                try:
                    if not self.winfo_exists() or generation != self._refresh_generation:
                        return
                    if not payload["ok"]:
                        self._has_profile_cache = False
                        self._cached_profiles = ()
                        self._cached_active = ""
                        self._cached_diagnoses = {}
                        for w in self._cards_frame.winfo_children():
                            w.destroy()
                        if self._stats_label:
                            self._stats_label.configure(text="浏览器 Profile 诊断失败")
                        EmptyState(
                            self._cards_frame,
                            "浏览器 Profile 诊断失败",
                            payload["error"] or "请稍后重试。",
                            "重新诊断",
                            self.refresh,
                        ).pack(fill="x", pady=(12, 4))
                        return
                    self._cached_profiles = tuple(payload["profiles"])
                    self._cached_active = payload["active"]
                    self._cached_diagnoses = dict(payload["diagnoses"])
                    self._has_profile_cache = True
                    self._render_profiles(payload["profiles"], payload["active"], payload["diagnoses"])
                except Exception:
                    return

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name="browser-profile-diagnostics", daemon=True).start()

    def _cancel_profile_render(self):
        self._profile_render_generation += 1
        if not self._profile_render_after_id:
            return
        try:
            self.after_cancel(self._profile_render_after_id)
        except Exception:
            pass
        self._profile_render_after_id = None

    def _render_profiles(self, profiles, active, diagnoses):
        if not self._cards_frame:
            return
        self._cancel_profile_render()
        profiles = tuple(profiles or ())
        diagnoses = dict(diagnoses or {})
        for w in self._cards_frame.winfo_children():
            w.destroy()

        summary = _browser_profiles_summary(profiles, diagnoses, self._selected_names)
        self._selected_names.intersection_update(summary["visible_names"])
        self._update_stats_label(profiles, diagnoses)

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无浏览器 Profile",
                "添加一个 Chrome / Edge Profile，用于隔离 ChatGPT / Claude 账号。",
                "新建 Profile",
                self._create_profile,
            ).pack(fill="x", pady=(12, 4))
            return

        visible_profiles = [
            p
            for p in profiles
            if _browser_diagnosis_matches_filter(diagnoses.get(p.name, {}), self._filter_mode)
        ]

        if not visible_profiles:
            EmptyState(
                self._cards_frame,
                "没有匹配的 Profile",
                "当前筛选条件下没有可显示的浏览器 Profile。",
                "重置筛选",
                self._reset_filter,
            ).pack(fill="x", pady=(12, 4))
            return

        self._profile_render_generation += 1
        render_generation = self._profile_render_generation
        self._render_profile_batch(visible_profiles, active, diagnoses, render_generation, 0)

    def _update_stats_label(self, profiles, diagnoses):
        if not self._stats_label:
            return
        summary = _browser_profiles_summary(tuple(profiles or ()), dict(diagnoses or {}), self._selected_names)
        self._stats_label.configure(
            text=(
                f"总数 {summary['total_count']}  |  异常 {summary['issues_count']}  |  "
                f"可启动 {summary['launchable_count']}  |  可重置 {summary['resettable_count']}  |  "
                f"已选中 {summary['selected_count']}"
            )
        )

    def _render_profile_batch(self, profiles, active, diagnoses, generation: int, start: int):
        if generation != self._profile_render_generation or not self._cards_frame:
            return
        end = min(start + PROFILE_RENDER_BATCH_SIZE, len(profiles))
        for p in profiles[start:end]:
            self._render_profile_card(p, active, diagnoses.get(p.name, {}))
        if end >= len(profiles):
            self._profile_render_after_id = None
            return

        try:
            self._profile_render_after_id = self.after(
                1,
                lambda: self._render_profile_batch(profiles, active, diagnoses, generation, end),
            )
        except Exception:
            self._profile_render_after_id = None

    def _render_profile_card(self, p, active, diagnosis):
        if not self._cards_frame:
            return
        is_active = p.name == active
        valid = _diagnosis_bool(diagnosis, "valid")
        executable_found = _diagnosis_bool(diagnosis, "executable_found")
        profile_path_exists = _diagnosis_bool(diagnosis, "profile_path_exists")
        browser_running = _diagnosis_bool(diagnosis, "browser_running")
        can_full_reset = _diagnosis_bool(diagnosis, "can_full_reset")
        validation_error = _diagnosis_text(diagnosis, "validation_error")
        full_reset_reason = _diagnosis_text(diagnosis, "full_reset_reason")

        card = ctk.CTkFrame(
            self._cards_frame,
            **card_frame_kwargs(COLORS["primary"] if is_active else COLORS["border_soft"]),
        )
        card.pack(fill="x", pady=5)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        selected_var = ctk.BooleanVar(value=p.name in self._selected_names)
        ctk.CTkCheckBox(
            top,
            text="",
            width=20,
            checkbox_width=18,
            checkbox_height=18,
            variable=selected_var,
            command=lambda name=p.name, var=selected_var: self._toggle_selected(name, var.get()),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(top, text=p.name, text_color=COLORS["text"], font=font(15, "bold")).pack(side="left")
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

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(fill="x", padx=14, pady=(0, 8))
        info_lines = [
            f"浏览器: {p.browser_type}  |  模式: {p.profile_mode}  |  默认目标: {p.start_target}",
            f"隔离启动: Default 分区  |  窗口 {p.launch_width}x{p.launch_height}  |  语言 {p.launch_language or '浏览器默认'}",
            f"路径: {p.user_data_dir}",
            f"可执行文件: {p.browser_executable or '(自动探测)'}",
            (
                f"诊断: 配置{'正常' if valid else '异常'}  |  "
                f"EXE {'就绪' if executable_found else '缺失'}  |  "
                f"路径 {'存在' if profile_path_exists else '缺失'}  |  "
                f"占用 {'是' if browser_running else '否'}"
            ),
            f"整目录清理: {'允许' if can_full_reset else '不允许'}",
            "设备一致性: 可保持站点数据隔离；不承诺跨机器硬件/系统指纹完全相同",
        ]
        if not valid and validation_error:
            info_lines.append(f"配置问题: {validation_error}")
        if not can_full_reset and full_reset_reason:
            info_lines.append(f"重置限制: {full_reset_reason}")
        if p.notes:
            info_lines.append(f"备注: {p.notes}")
        for line in info_lines:
            info_label = ctk.CTkLabel(
                info_frame,
                text=line,
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
                justify="left",
            )
            info_label.pack(fill="x")
            bind_wraplength(info_frame, info_label, padding=4)

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(fill="x", padx=14, pady=(0, 12))

        btn_row1 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_row1.pack(fill="x", pady=(0, 4))
        ctk.CTkButton(btn_row1, text="启动 ChatGPT", width=96, command=lambda prof=p: self._launch(prof, "chatgpt"), **button_style("primary", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row1, text="启动 Claude", width=96, command=lambda prof=p: self._launch(prof, "claude"), **button_style("accent", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row1, text="清理 GPT", width=76, command=lambda prof=p: self._clear_sites(prof, "chatgpt"), **button_style("warning", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row1, text="清理 Claude", width=86, command=lambda prof=p: self._clear_sites(prof, "claude"), **button_style("warning", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row1, text="清理两者", width=86, command=lambda prof=p: self._clear_sites(prof, "both"), **button_style("warning", compact=True)).pack(side="left", padx=(0, 6))
        if can_full_reset:
            ctk.CTkButton(btn_row1, text="整目录清理", width=96, command=lambda prof=p: self._full_reset(prof), **button_style("danger", compact=True)).pack(side="left", padx=(0, 6))

        btn_row2 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_row2.pack(fill="x")
        ctk.CTkButton(btn_row2, text="打开目录", width=78, command=lambda prof=p: self._open_dir(prof), **button_style("secondary", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row2, text="复制", width=58, command=lambda prof=p: self._clone_profile(prof), **button_style("secondary", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row2, text="编辑", width=58, command=lambda name=p.name: self._edit_profile(name), **button_style("secondary", compact=True)).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_row2, text="删除", width=58, command=lambda name=p.name: self._delete_profile(name), **button_style("danger", compact=True)).pack(side="left")

    def _on_filter_change(self, value: str):
        self._filter_mode = self.FILTER_OPTIONS.get(value, "all")
        if self._has_profile_cache:
            self._render_profiles(self._cached_profiles, self._cached_active, self._cached_diagnoses)
        else:
            self.refresh()

    def _matches_filter(self, diagnosis: dict) -> bool:
        return _browser_diagnosis_matches_filter(diagnosis, self._filter_mode)

    def _reset_filter(self):
        self._filter_mode = "all"
        self._filter_combo.set("全部")
        if self._has_profile_cache:
            self._render_profiles(self._cached_profiles, self._cached_active, self._cached_diagnoses)
        else:
            self.refresh()

    def _toggle_selected(self, name: str, selected: bool):
        if selected:
            self._selected_names.add(name)
        else:
            self._selected_names.discard(name)
        if self._has_profile_cache:
            self._update_stats_label(self._cached_profiles, self._cached_diagnoses)

    def _select_visible(self):
        if not self._has_profile_cache:
            self._toast("正在刷新诊断后再全选当前")
            self.refresh()
            return
        visible_names = _visible_profile_names(self._cached_profiles, self._cached_diagnoses, self._filter_mode)
        self._selected_names.update(visible_names)
        self._toast(f"已选中当前筛选下 {len(visible_names)} 个 Profile")
        self._render_profiles(self._cached_profiles, self._cached_active, self._cached_diagnoses)

    def _clear_selection(self):
        self._selected_names.clear()
        self._toast("已清空选择")
        if self._has_profile_cache:
            self._render_profiles(self._cached_profiles, self._cached_active, self._cached_diagnoses)
        else:
            self.refresh()

    def _bulk_clear_sites(self, scope: str):
        if not self._selected_names:
            self._toast("请先选择至少一个 Profile", is_error=True)
            return

        label = {"chatgpt": "ChatGPT", "claude": "Claude", "both": "ChatGPT 与 Claude"}[scope]

        def do_bulk_clear():
            profiles = {p.name: p for p in profile_manager.list_browser_profiles()}
            success = 0
            failures: list[str] = []
            for name in sorted(self._selected_names):
                profile = profiles.get(name)
                if not profile:
                    failures.append(f"{name}: Profile 不存在")
                    continue
                try:
                    browser_data_manager.clear_site_data(profile, scope)
                    success += 1
                except Exception as e:
                    failures.append(f"{name}: {e}")

            if failures:
                self._toast(f"已清理 {success} 个，失败 {len(failures)} 个")
                BulkOperationResultDialog(
                    self.winfo_toplevel(),
                    title="批量清理结果",
                    success_count=success,
                    failure_items=failures,
                    success_label=f"目标站点: {label}",
                )
            else:
                self._toast(f"已清理 {success} 个 Profile 的 {label} 站点数据")

            self.refresh()

        ConfirmDialog(
            self.winfo_toplevel(),
            title="批量清理站点数据",
            message=f"将清理所选 {len(self._selected_names)} 个 Profile 中 {label} 的站点数据和登录态。\n请先关闭相关浏览器后继续。",
            on_confirm=do_bulk_clear,
        )

    def _create_profile(self):
        def on_save(profile, _old):
            browser_profile_manager.save_profile(profile)
            profile_manager.set_active_browser(profile.name)
            self._toast(f"已创建: {profile.name}")
            self.refresh()

        BrowserProfileEditorDialog(self.winfo_toplevel(), title="新建浏览器 Profile", on_save=on_save)

    def _quick_create(self, browser_type: str, target: str):
        try:
            profile = browser_profile_manager.create_template_profile(browser_type, target)
            profile_manager.set_active_browser(profile.name)
            self._toast(f"已快速创建: {profile.name}")
            self.refresh()
        except Exception as e:
            self._toast(f"快速创建失败: {e}", is_error=True)

    def _edit_profile(self, name: str):
        profiles = profile_manager.list_browser_profiles()
        profile = next((p for p in profiles if p.name == name), None)
        if not profile:
            self._toast("未找到 Profile", is_error=True)
            return

        def on_save(new_profile, old_profile):
            browser_profile_manager.save_profile(
                new_profile,
                previous_name=old_profile.name if old_profile else None,
            )
            self._toast(f"已保存: {new_profile.name}")
            self.refresh()

        BrowserProfileEditorDialog(self.winfo_toplevel(), title="编辑浏览器 Profile", profile=profile, on_save=on_save)

    def _clone_profile(self, profile):
        try:
            cloned = browser_profile_manager.clone_profile(profile)
            profile_manager.set_active_browser(cloned.name)
            self._toast(f"已复制为: {cloned.name}")
            self.refresh()
        except Exception as e:
            self._toast(f"复制失败: {e}", is_error=True)

    def _delete_profile(self, name: str):
        def do_delete():
            browser_profile_manager.delete_profile(name)
            self._toast(f"已删除: {name}")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="删除 Profile", message=f"确定要删除 \"{name}\" 吗？\n不会自动删除浏览器目录。", on_confirm=do_delete)

    def _launch(self, profile, target: str):
        try:
            browser_launcher.launch(profile, target=target)
            profile_manager.set_active_browser(profile.name)
            self._toast(f"已启动 {profile.browser_type}: {target}")
            self.refresh()
        except Exception as e:
            self._toast(f"启动失败: {e}", is_error=True)

    def _clear_sites(self, profile, scope: str):
        def do_clear():
            try:
                browser_data_manager.clear_site_data(profile, scope)
                label = {"chatgpt": "ChatGPT", "claude": "Claude", "both": "ChatGPT 与 Claude"}[scope]
                self._toast(f"已清理 {label} 站点数据")
            except Exception as e:
                self._toast(f"清理失败: {e}", is_error=True)

        label = {"chatgpt": "ChatGPT", "claude": "Claude", "both": "ChatGPT 与 Claude"}[scope]
        ConfirmDialog(
            self.winfo_toplevel(),
            title="清理站点数据",
            message=f"将清理该 Profile 中 {label} 的站点数据和登录态。\n请先关闭浏览器后继续。",
            on_confirm=do_clear,
        )

    def _full_reset(self, profile):
        def do_reset():
            try:
                browser_data_manager.full_reset(profile)
                self._toast("已完成整目录清理")
            except Exception as e:
                self._toast(f"整目录清理失败: {e}", is_error=True)

        DangerConfirmDialog(
            self.winfo_toplevel(),
            title="危险操作",
            message="这将清空该托管 Profile 目录下的全部浏览器数据，且无法撤销。\n请先关闭对应浏览器后继续。",
            confirm_text=profile.name,
            on_confirm=do_reset,
        )

    def _open_dir(self, profile):
        try:
            import subprocess
            subprocess.Popen(["explorer", profile.user_data_dir])
        except Exception as e:
            self._toast(f"打开目录失败: {e}", is_error=True)
