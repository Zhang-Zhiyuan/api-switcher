from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from core.git_manager import GitManager
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.theme import COLORS, button_style, center_window, combo_style, font, input_style, textbox_style
from ui.widgets.toast import show_toast


class GitSnapshotHistoryDialog(ctk.CTkToplevel):
    """Inspect automatic Git snapshots and roll back to a selected commit."""

    def __init__(self, master, project_path: str | Path | None = None):
        super().__init__(master)
        self.title("Git 快照历史")
        self.geometry("1040x760")
        self.minsize(820, 580)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self._commits: list[dict] = []
        self._selected_hash = ""
        self._project_var = ctk.StringVar(value=str(Path(project_path or Path.cwd()).resolve()))
        self._count_var = ctk.StringVar(value="50")
        self._auto_only_var = ctk.BooleanVar(value=True)

        self._build_ui()
        center_window(self, master)
        self._refresh()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 10))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="Git 快照历史",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="查看自动快照、触发原因、改动文件数、commit hash，并可复制 diff 或回滚。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="刷新",
            width=82,
            command=self._refresh,
            **button_style("secondary"),
        ).pack(side="right")

        path_row = ctk.CTkFrame(self, fg_color="transparent")
        path_row.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(path_row, text="项目目录", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._path_entry = ctk.CTkEntry(path_row, textvariable=self._project_var, **input_style())
        self._path_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ctk.CTkButton(
            path_row,
            text="选择",
            width=72,
            command=self._choose_project,
            **button_style("secondary"),
        ).pack(side="left")

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=(0, 10))

        ctk.CTkCheckBox(
            controls,
            text="仅自动快照",
            variable=self._auto_only_var,
            command=self._refresh,
            text_color=COLORS["text"],
            fg_color=COLORS["success"],
            hover_color=COLORS["success_hover"],
            border_color=COLORS["border"],
            checkmark_color=COLORS["text"],
            font=font(12),
        ).pack(side="left", padx=(0, 16))

        ctk.CTkLabel(controls, text="最近", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._count_combo = ctk.CTkComboBox(
            controls,
            values=["20", "50", "100", "200"],
            variable=self._count_var,
            width=88,
            command=lambda _value: self._refresh(),
            **combo_style(),
        )
        self._count_combo.pack(side="left", padx=(8, 6))
        ctk.CTkLabel(controls, text="条", text_color=COLORS["muted"], font=font(12)).pack(side="left")

        self._status_label = ctk.CTkLabel(
            controls,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._status_label.pack(side="left", fill="x", expand=True, padx=(18, 0))

        select_row = ctk.CTkFrame(self, fg_color="transparent")
        select_row.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(select_row, text="快照", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._commit_combo = ctk.CTkComboBox(
            select_row,
            values=["(无快照)"],
            width=760,
            command=lambda _value: self._show_selected_commit(stat_only=True),
            **combo_style(),
        )
        self._commit_combo.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self._text = ctk.CTkTextbox(self, wrap="none", **textbox_style(monospace=True))
        self._text.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", padx=18, pady=(0, 18))

        ctk.CTkButton(
            button_row,
            text="显示 Diff",
            width=96,
            command=lambda: self._show_selected_commit(stat_only=False),
            **button_style("secondary"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            button_row,
            text="复制 Diff",
            width=96,
            command=self._copy_diff,
            **button_style("secondary"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            button_row,
            text="复制 Hash",
            width=96,
            command=self._copy_hash,
            **button_style("accent"),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            button_row,
            text="软回滚",
            width=96,
            command=lambda: self._confirm_rollback(hard=False),
            **button_style("warning"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            button_row,
            text="硬回滚",
            width=96,
            command=lambda: self._confirm_rollback(hard=True),
            **button_style("danger"),
        ).pack(side="right", padx=(8, 0))

    def _project_path(self) -> Path:
        return Path(self._project_var.get().strip()).expanduser().resolve()

    def _manager(self) -> GitManager:
        return GitManager(self._project_path())

    def _count(self) -> int:
        try:
            return int(self._count_var.get())
        except Exception:
            return 50

    def _choose_project(self):
        selected = filedialog.askdirectory(
            title="选择 Git 项目目录",
            initialdir=str(self._project_path()) if self._project_var.get().strip() else str(Path.cwd()),
        )
        if selected:
            self._project_var.set(selected)
            self._refresh()

    def _commit_label(self, commit: dict) -> str:
        date = str(commit.get("date") or "").replace("T", " ")
        if "+" in date:
            date = date.split("+", 1)[0]
        message = str(commit.get("message") or "")
        changed = commit.get("changed_files", 0)
        return f"{commit.get('short_hash')} | {date} | {changed} files | {message}"

    def _selected_commit(self) -> dict | None:
        if not self._commits:
            return None
        current = self._commit_combo.get()
        for commit in self._commits:
            if self._commit_label(commit) == current:
                return commit
        return self._commits[0]

    def _set_text(self, text: str):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")

    def _refresh(self):
        try:
            manager = self._manager()
            self._commits = manager.get_recent_commits(
                count=self._count(),
                auto_only=bool(self._auto_only_var.get()),
            )
            labels = [self._commit_label(commit) for commit in self._commits]
            if labels:
                self._commit_combo.configure(values=labels, state="normal")
                self._commit_combo.set(labels[0])
                self._status_label.configure(
                    text=f"已读取 {len(labels)} 条快照",
                    text_color=COLORS["muted"],
                )
                self._show_selected_commit(stat_only=True)
            else:
                self._commit_combo.configure(values=["(无快照)"], state="disabled")
                self._commit_combo.set("(无快照)")
                self._status_label.configure(text="没有找到匹配的快照", text_color=COLORS["warning"])
                self._set_text("没有找到 Git 快照。可以取消“仅自动快照”，或确认该目录是否为 Git 仓库。")
        except Exception as e:
            self._commits = []
            self._status_label.configure(text="读取失败", text_color=COLORS["danger"])
            self._set_text(f"读取 Git 快照失败: {e}")

    def _show_selected_commit(self, stat_only: bool = True):
        commit = self._selected_commit()
        if not commit:
            return
        commit_hash = str(commit.get("full_hash") or commit.get("hash") or "")
        manager = self._manager()
        ok, diff = manager.get_commit_diff(commit_hash, stat_only=stat_only)
        if not ok:
            self._set_text(diff)
            return
        header = [
            f"Commit: {commit_hash}",
            f"短 hash: {commit.get('short_hash') or commit.get('hash')}",
            f"时间: {commit.get('date')}",
            f"触发原因: {commit.get('message')}",
            f"改动文件数: {commit.get('changed_files', 0)}",
            f"自动快照: {'是' if commit.get('auto_snapshot') else '否'}",
            "",
        ]
        self._set_text("\n".join(header) + diff)

    def _copy_hash(self):
        commit = self._selected_commit()
        if not commit:
            show_toast(self, "没有可复制的 hash", is_error=True)
            return
        commit_hash = str(commit.get("full_hash") or commit.get("hash") or "")
        self.clipboard_clear()
        self.clipboard_append(commit_hash)
        show_toast(self, "commit hash 已复制")

    def _copy_diff(self):
        commit = self._selected_commit()
        if not commit:
            show_toast(self, "没有可复制的 diff", is_error=True)
            return
        commit_hash = str(commit.get("full_hash") or commit.get("hash") or "")
        ok, diff = self._manager().get_commit_diff(commit_hash, stat_only=False)
        if not ok:
            show_toast(self, diff, is_error=True)
            return
        self.clipboard_clear()
        self.clipboard_append(diff)
        show_toast(self, "diff 已复制")

    def _confirm_rollback(self, hard: bool):
        commit = self._selected_commit()
        if not commit:
            show_toast(self, "没有可回滚的快照", is_error=True)
            return
        commit_hash = str(commit.get("full_hash") or commit.get("hash") or "")
        mode = "硬回滚" if hard else "软回滚"
        detail = (
            "硬回滚会把工作区重置到该快照；回滚前会自动创建安全标签。"
            if hard
            else "软回滚只移动 HEAD，改动会保留在暂存区；回滚前会自动创建安全标签。"
        )

        def do_rollback():
            ok, message = self._manager().rollback_to_commit(commit_hash, hard=hard)
            show_toast(self, message, is_error=not ok)
            self._refresh()

        ConfirmDialog(
            self,
            title=f"确认{mode}",
            message=f"确定要回滚到 {commit.get('short_hash') or commit_hash[:12]} 吗？\n{detail}",
            on_confirm=do_rollback,
        )
