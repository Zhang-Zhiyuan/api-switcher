import threading

import customtkinter as ctk
from tkinter import filedialog

from core.lazy_imports import LazyAttribute, LazyModule
from ui.widgets.toast import show_toast
from ui.widgets.empty_state import EmptyState
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


backup_manager = LazyModule("core.backup_manager")
local_config_bundle = LazyModule("core.local_config_bundle")
portable_migration = LazyModule("core.portable_migration")
ConfirmDialog = LazyAttribute("ui.dialogs.confirm_dialog", "ConfirmDialog")
PasswordDialog = LazyAttribute("ui.dialogs.password_dialog", "PasswordDialog")


class BackupTab(ctk.CTkScrollableFrame):
    """Tab for managing backups."""

    RENDER_BATCH_SIZE = 12

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._list_frame = None
        self._refresh_generation = 0
        self._render_after_id = None
        self._build_ui()

    def destroy(self):
        self._cancel_render()
        super().destroy()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="备份管理",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="创建本机备份，导出完整配置 ZIP，或导出可跨电脑迁移的加密 Profile 包",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="导入迁移包",
            width=108,
            command=self._import_portable,
            **button_style("accent"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            header,
            text="导出迁移包",
            width=108,
            command=self._export_portable,
            **button_style("success"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            header,
            text="清理旧备份",
            width=108,
            command=self._prune,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            header,
            text="回滚最近",
            width=108,
            command=self._restore_latest,
            **button_style("warning"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            header,
            text="立即备份",
            width=108,
            command=self._create_backup,
            **button_style("primary"),
        ).pack(side="right")

        self._build_local_config_zip_panel()

        self._list_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        self.after(20, self.refresh)

    def refresh(self):
        if not self._list_frame:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._cancel_render()
        for w in self._list_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self._list_frame,
            text="正在读取备份记录...",
            text_color=COLORS["muted"],
            font=font(13),
        ).pack(fill="x", pady=(22, 6))

        def worker():
            try:
                payload = {"ok": True, "backups": backup_manager.list_backups(), "error": ""}
            except Exception as exc:
                payload = {"ok": False, "backups": [], "error": str(exc)}

            def finish():
                try:
                    if generation != self._refresh_generation or not self.winfo_exists():
                        return
                    self._render_backups(payload, generation)
                except Exception:
                    return

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name="backup-tab-refresh", daemon=True).start()

    def _cancel_render(self):
        if not self._render_after_id:
            return
        try:
            self.after_cancel(self._render_after_id)
        except Exception:
            pass
        self._render_after_id = None

    def _render_backups(self, payload: dict, generation: int):
        if not self._list_frame:
            return
        for w in self._list_frame.winfo_children():
            w.destroy()

        if not payload.get("ok"):
            EmptyState(
                self._list_frame,
                "读取备份记录失败",
                payload.get("error") or "请稍后重试。",
                "重新读取",
                self.refresh,
            ).pack(fill="x", pady=(12, 4))
            return

        backups = list(payload.get("backups") or [])
        if not backups:
            EmptyState(
                self._list_frame,
                "暂无备份记录",
                "创建一个备份后，可以在这里快速回滚。",
                "立即备份",
                self._create_backup,
            ).pack(fill="x", pady=(12, 4))
            return

        self._render_backup_batch(backups, generation, 0)

    def _render_backup_batch(self, backups, generation: int, start: int):
        if generation != self._refresh_generation or not self._list_frame:
            return
        end = min(start + self.RENDER_BATCH_SIZE, len(backups))
        for entry in backups[start:end]:
            self._render_backup_card(entry)
        if end >= len(backups):
            self._render_after_id = None
            return
        self._render_after_id = self.after(
            1,
            lambda: self._render_backup_batch(backups, generation, end),
        )

    def _render_backup_card(self, entry):
        card = ctk.CTkFrame(self._list_frame, **card_frame_kwargs())
        card.pack(fill="x", pady=5)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            top,
            text=entry.timestamp,
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            top,
            text=entry.description,
            text_color=COLORS["primary"],
            font=font(12, "bold"),
        ).pack(side="left", padx=(10, 0))

        files_text = ", ".join(entry.files) if entry.files else "(无文件)"
        files_label = ctk.CTkLabel(
            card,
            text=f"包含: {files_text}",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        files_label.pack(fill="x", padx=14, pady=(0, 8))
        bind_wraplength(card, files_label, padding=36)

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(anchor="e", padx=14, pady=(0, 12))

        ctk.CTkButton(
            btn_frame,
            text="回滚到此",
            width=86,
            command=lambda e=entry: self._restore(e),
            **button_style("warning", compact=True),
        ).pack(side="left", padx=(0, 5))

    def _build_local_config_zip_panel(self):
        panel = ctk.CTkFrame(self, **card_frame_kwargs())
        panel.pack(fill="x", padx=14, pady=(0, 10))

        text_area = ctk.CTkFrame(panel, fg_color="transparent")
        text_area.pack(side="left", fill="x", expand=True, padx=14, pady=12)

        ctk.CTkLabel(
            text_area,
            text="完整配置 ZIP",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w")
        desc = ctk.CTkLabel(
            text_area,
            text="一键导出/导入本机保存的 API、官方账号快照、SSH 服务器、浏览器 Profile 元数据和引用密钥；密钥用迁移密码加密。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        desc.pack(fill="x", pady=(3, 0))
        bind_wraplength(text_area, desc, padding=8, min_width=320, max_width=760)

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(side="right", padx=14, pady=12)
        ctk.CTkButton(
            actions,
            text="导入 ZIP",
            width=94,
            command=self._import_local_config_zip,
            **button_style("accent", compact=True),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="导出 ZIP",
            width=94,
            command=self._export_local_config_zip,
            **button_style("success", compact=True),
        ).pack(side="right")

    def _create_backup(self):
        try:
            entry = backup_manager.create_backup("手动备份")
            show_toast(self.winfo_toplevel(), f"备份已创建: {entry.timestamp}")
            self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"备份失败: {e}", is_error=True)

    def _restore(self, entry):
        def do_restore():
            try:
                restored = backup_manager.restore_backup(entry)
                show_toast(self.winfo_toplevel(), f"已回滚 {len(restored)} 个文件")
                top = self.winfo_toplevel()
                if hasattr(top, "refresh_all"):
                    top.refresh_all()
                else:
                    self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"回滚失败: {e}", is_error=True)

        ConfirmDialog(self.winfo_toplevel(), title="确认回滚",
                      message=f"确定要回滚到 {entry.timestamp} 吗？\n当前配置会被先自动备份。",
                      on_confirm=do_restore)

    def _restore_latest(self):
        entry = backup_manager.get_latest_backup()
        if not entry:
            show_toast(self.winfo_toplevel(), "暂无可回滚的备份", is_error=True)
            return
        self._restore(entry)

    def _prune(self):
        def do_prune():
            removed = backup_manager.prune_backups(keep_count=20)
            show_toast(self.winfo_toplevel(), f"已清理 {removed} 个旧备份")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="清理备份",
                      message="将保留最近 20 个备份，其余删除。继续？",
                      on_confirm=do_prune)

    def _export_local_config_zip(self):
        output_path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="导出完整配置 ZIP",
            defaultextension=".zip",
            filetypes=[
                ("API切换器完整配置 ZIP", "*.zip"),
                ("所有文件", "*.*"),
            ],
        )
        if not output_path:
            return

        def do_export(password: str):
            try:
                result = local_config_bundle.export_local_config_zip(output_path, password)
                message = f"完整配置 ZIP 已导出: {result.profile_count} 个 Profile, {result.secret_count} 个密钥"
                if result.missing_secret_refs:
                    message += f"，{len(result.missing_secret_refs)} 个密钥缺失"
                show_toast(self.winfo_toplevel(), message)
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"导出 ZIP 失败: {e}", is_error=True)

        PasswordDialog(
            self.winfo_toplevel(),
            title="设置完整配置 ZIP 密码",
            message="ZIP 会包含本机保存的 API、官方账号快照、SSH 服务器、浏览器 Profile 元数据，以及这些条目引用的 API Key、账号 token、SSH 密码/私钥口令。请设置强密码。",
            confirm_password=True,
            on_confirm=do_export,
        )

    def _import_local_config_zip(self):
        input_path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="导入完整配置 ZIP",
            filetypes=[
                ("API切换器完整配置 ZIP", "*.zip"),
                ("所有文件", "*.*"),
            ],
        )
        if not input_path:
            return

        try:
            summary = local_config_bundle.inspect_local_config_zip(input_path)
            summary_text = f"{summary.profile_count} 个 Profile，{summary.secret_count} 个密钥"
            if summary.missing_secret_count:
                summary_text += f"，源包缺失 {summary.missing_secret_count} 个密钥"
            if summary.created_at:
                summary_text += f"\n创建时间: {summary.created_at}"
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"读取 ZIP 失败: {e}", is_error=True)
            return

        def ask_password():
            PasswordDialog(
                self.winfo_toplevel(),
                title="输入完整配置 ZIP 密码",
                message="导入会合并 ZIP 中的 API、官方账号快照、SSH 和浏览器 Profile；同名 Profile 会被替换。导入前会自动创建一份配置备份。",
                confirm_password=False,
                on_confirm=do_import,
            )

        def do_import(password: str):
            try:
                result = local_config_bundle.import_local_config_zip(input_path, password)
                message = f"完整配置 ZIP 已导入: {result.profile_count} 个 Profile, {result.secret_count} 个密钥"
                if result.skipped_secret_refs:
                    message += f"，{len(result.skipped_secret_refs)} 个密钥跳过"
                show_toast(self.winfo_toplevel(), message)
                top = self.winfo_toplevel()
                if hasattr(top, "refresh_all"):
                    top.refresh_all()
                else:
                    self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"导入 ZIP 失败: {e}", is_error=True)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="导入完整配置 ZIP",
            message=(
                f"将导入: {summary_text}\n\n"
                "导入会合并 ZIP 中的本地 API、官方账号、SSH 服务器等配置；同名 Profile 会被替换。继续？"
            ),
            on_confirm=ask_password,
        )

    def _export_portable(self):
        output_path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="导出 Profile 迁移包",
            defaultextension=".asxprofile",
            filetypes=[
                ("API切换器迁移包", "*.asxprofile"),
                ("JSON 文件", "*.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not output_path:
            return

        def do_export(password: str):
            try:
                result = portable_migration.export_portable_profiles(output_path, password)
                message = f"迁移包已导出: {result.profile_count} 个 Profile, {result.secret_count} 个密钥"
                if result.missing_secret_refs:
                    message += f"，{len(result.missing_secret_refs)} 个密钥缺失"
                if result.browser_file_count:
                    message += f"，浏览器文件 {result.browser_file_count} 个"
                if result.skipped_browser_files:
                    message += f"，{len(result.skipped_browser_files)} 个浏览器文件跳过"
                show_toast(self.winfo_toplevel(), message)
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"导出失败: {e}", is_error=True)

        PasswordDialog(
            self.winfo_toplevel(),
            title="设置迁移密码",
            message="迁移包会包含 API/SSH Profile 密钥，以及浏览器 Profile 的 Cookies、Local Storage、IndexedDB 等登录数据。Chromium Cookies 仍可能受原电脑系统账号加密限制。请设置强密码。",
            confirm_password=True,
            on_confirm=do_export,
        )

    def _import_portable(self):
        input_path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="导入 Profile 迁移包",
            filetypes=[
                ("API切换器迁移包", "*.asxprofile"),
                ("JSON 文件", "*.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not input_path:
            return

        def do_import(password: str):
            try:
                result = portable_migration.import_portable_profiles(input_path, password)
                message = f"迁移包已导入: {result.profile_count} 个 Profile, {result.secret_count} 个密钥"
                if result.browser_file_count:
                    message += f"，浏览器文件 {result.browser_file_count} 个"
                if result.skipped_browser_files:
                    message += f"，{len(result.skipped_browser_files)} 个浏览器文件跳过"
                show_toast(self.winfo_toplevel(), message)
                top = self.winfo_toplevel()
                if hasattr(top, "refresh_all"):
                    top.refresh_all()
                else:
                    self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"导入失败: {e}", is_error=True)

        PasswordDialog(
            self.winfo_toplevel(),
            title="输入迁移密码",
            message="导入会合并迁移包中的 API/SSH/浏览器 Profile；同名 Profile 会被替换，浏览器数据会恢复到本机托管目录。",
            confirm_password=False,
            on_confirm=do_import,
        )
