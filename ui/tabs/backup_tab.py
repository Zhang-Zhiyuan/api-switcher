import customtkinter as ctk
from tkinter import filedialog

from core import backup_manager, portable_migration
from ui.widgets.toast import show_toast
from ui.widgets.empty_state import EmptyState
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.dialogs.password_dialog import PasswordDialog
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


class BackupTab(ctk.CTkScrollableFrame):
    """Tab for managing backups."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._list_frame = None
        self._build_ui()

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
            text="创建本机备份，或导出可跨电脑迁移的加密 Profile 包",
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
            text="立即备份",
            width=108,
            command=self._create_backup,
            **button_style("primary"),
        ).pack(side="right")

        self._list_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        self.refresh()

    def refresh(self):
        if not self._list_frame:
            return
        for w in self._list_frame.winfo_children():
            w.destroy()

        backups = backup_manager.list_backups()

        if not backups:
            EmptyState(
                self._list_frame,
                "暂无备份记录",
                "创建一个备份后，可以在这里快速回滚。",
                "立即备份",
                self._create_backup,
            ).pack(fill="x", pady=(12, 4))
            return

        for entry in backups:
            card = ctk.CTkFrame(self._list_frame, **card_frame_kwargs())
            card.pack(fill="x", pady=5)

            # Timestamp and description
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

            # Files info
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

            # Actions
            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.pack(anchor="e", padx=14, pady=(0, 12))

            ctk.CTkButton(
                btn_frame,
                text="回滚到此",
                width=86,
                command=lambda e=entry: self._restore(e),
                **button_style("warning", compact=True),
            ).pack(side="left", padx=(0, 5))

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
                self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"回滚失败: {e}", is_error=True)

        ConfirmDialog(self.winfo_toplevel(), title="确认回滚",
                      message=f"确定要回滚到 {entry.timestamp} 吗？\n当前配置会被先自动备份。",
                      on_confirm=do_restore)

    def _prune(self):
        def do_prune():
            removed = backup_manager.prune_backups(keep_count=20)
            show_toast(self.winfo_toplevel(), f"已清理 {removed} 个旧备份")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="清理备份",
                      message="将保留最近 20 个备份，其余删除。继续？",
                      on_confirm=do_prune)

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
                if result.browser_profile_count:
                    message += f"，浏览器数据 {result.browser_profile_count} 个/{result.browser_file_count} 个文件"
                if result.missing_secret_refs:
                    message += f"，{len(result.missing_secret_refs)} 个密钥缺失"
                if result.skipped_browser_files:
                    message += f"，跳过 {len(result.skipped_browser_files)} 个浏览器文件"
                show_toast(self.winfo_toplevel(), message)
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"导出失败: {e}", is_error=True)

        PasswordDialog(
            self.winfo_toplevel(),
            title="设置迁移密码",
            message="迁移包会包含 API Key、OAuth Token、SSH 密码，以及托管浏览器 Profile 的 cookies/本地存储等数据。请设置一个强密码，导入到另一台电脑时需要再次输入。",
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
                if result.browser_profile_count:
                    message += f"，浏览器数据 {result.browser_profile_count} 个/{result.browser_file_count} 个文件"
                if result.skipped_browser_files:
                    message += f"，跳过 {len(result.skipped_browser_files)} 个浏览器文件"
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
            message="导入会合并迁移包中的 Profile；同名 Profile 会被替换，密钥会写入本机凭据存储，托管浏览器 Profile 数据会还原到本机数据目录。",
            confirm_password=False,
            on_confirm=do_import,
        )
