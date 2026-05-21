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
        self.geometry("1120x780")
        self.minsize(900, 620)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self._commits: list[dict] = []
        self._selected_hash = ""
        self._row_widgets: dict[str, ctk.CTkFrame] = {}
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
            text="点选快照查看 diff、复制 hash，并可安全回滚到某个自动快照。",
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
        self._path_entry.bind("<Return>", lambda _event: self._refresh())
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

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        left = ctk.CTkFrame(body, fg_color="transparent", width=390)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        ctk.CTkLabel(
            left,
            text="快照列表",
            text_color=COLORS["text"],
            font=font(13, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        self._list_frame = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self._list_frame.pack(fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(
            right,
            text="快照详情 / Diff",
            text_color=COLORS["text"],
            font=font(13, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        self._text = ctk.CTkTextbox(right, wrap="none", **textbox_style(monospace=True))
        self._text.pack(fill="both", expand=True)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", padx=18, pady=(0, 18))

        self._show_diff_btn = ctk.CTkButton(
            button_row,
            text="显示 Diff",
            width=96,
            command=lambda: self._show_selected_commit(stat_only=False),
            **button_style("secondary"),
        )
        self._show_diff_btn.pack(side="left", padx=(0, 8))
        self._copy_diff_btn = ctk.CTkButton(
            button_row,
            text="复制 Diff",
            width=96,
            command=self._copy_diff,
            **button_style("secondary"),
        )
        self._copy_diff_btn.pack(side="left", padx=(0, 8))
        self._copy_hash_btn = ctk.CTkButton(
            button_row,
            text="复制 Hash",
            width=96,
            command=self._copy_hash,
            **button_style("accent"),
        )
        self._copy_hash_btn.pack(side="left", padx=(0, 8))

        self._soft_rollback_btn = ctk.CTkButton(
            button_row,
            text="软回滚",
            width=96,
            command=lambda: self._confirm_rollback(hard=False),
            **button_style("warning"),
        )
        self._soft_rollback_btn.pack(side="right", padx=(8, 0))
        self._hard_rollback_btn = ctk.CTkButton(
            button_row,
            text="硬回滚",
            width=96,
            command=lambda: self._confirm_rollback(hard=True),
            **button_style("danger"),
        )
        self._hard_rollback_btn.pack(side="right", padx=(8, 0))

    def _project_path(self) -> Path:
        raw = self._project_var.get().strip()
        if not raw:
            raise ValueError("项目目录不能为空")
        return Path(raw).expanduser().resolve()

    def _manager(self) -> GitManager:
        return GitManager(self._project_path())

    def _count(self) -> int:
        try:
            return max(1, min(500, int(self._count_var.get())))
        except Exception:
            return 50

    def _choose_project(self):
        try:
            current = self._project_path()
            initial_dir = current if current.exists() and current.is_dir() else Path.cwd()
        except Exception:
            initial_dir = Path.cwd()
        selected = filedialog.askdirectory(
            title="选择 Git 项目目录",
            initialdir=str(initial_dir),
        )
        if selected:
            self._project_var.set(selected)
            self._refresh()

    def _set_text(self, text: str):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")

    def _set_actions_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for button in [
            self._show_diff_btn,
            self._copy_diff_btn,
            self._copy_hash_btn,
            self._soft_rollback_btn,
            self._hard_rollback_btn,
        ]:
            button.configure(state=state)

    def _commit_hash(self, commit: dict) -> str:
        return str(commit.get("full_hash") or commit.get("hash") or "")

    def _short_date(self, commit: dict) -> str:
        date = str(commit.get("date") or "").replace("T", " ")
        if "+" in date:
            date = date.split("+", 1)[0]
        return date

    def _selected_commit(self) -> dict | None:
        if not self._commits:
            return None
        for commit in self._commits:
            if self._commit_hash(commit) == self._selected_hash:
                return commit
        return self._commits[0]

    def _bind_row_click(self, widget, commit_hash: str):
        try:
            widget.bind("<Button-1>", lambda _event, h=commit_hash: self._select_commit(h))
        except Exception:
            pass

    def _render_commit_list(self):
        for child in self._list_frame.winfo_children():
            child.destroy()
        self._row_widgets = {}

        if not self._commits:
            ctk.CTkLabel(
                self._list_frame,
                text="没有快照",
                text_color=COLORS["muted"],
                font=font(12),
            ).pack(anchor="w", padx=6, pady=8)
            return

        for commit in self._commits:
            commit_hash = self._commit_hash(commit)
            selected = commit_hash == self._selected_hash
            row = ctk.CTkFrame(
                self._list_frame,
                corner_radius=8,
                fg_color=COLORS["surface_alt"] if selected else COLORS["surface"],
                border_width=1,
                border_color=COLORS["primary"] if selected else COLORS["border_soft"],
            )
            row.pack(fill="x", pady=(0, 7), padx=(0, 4))
            self._bind_row_click(row, commit_hash)

            top = ctk.CTkFrame(row, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(8, 0))
            self._bind_row_click(top, commit_hash)
            ctk.CTkLabel(
                top,
                text=commit.get("short_hash") or commit_hash[:8],
                text_color=COLORS["accent"] if commit.get("auto_snapshot") else COLORS["muted"],
                font=font(12, "bold"),
                width=74,
                anchor="w",
            ).pack(side="left")
            title = ctk.CTkLabel(
                top,
                text=str(commit.get("message") or "(no message)")[:80],
                text_color=COLORS["text"],
                font=font(12, "bold"),
                anchor="w",
            )
            title.pack(side="left", fill="x", expand=True)
            self._bind_row_click(title, commit_hash)

            subtitle = ctk.CTkLabel(
                row,
                text=f"{self._short_date(commit)}  |  {commit.get('changed_files', 0)} 文件",
                text_color=COLORS["muted"],
                font=font(11),
                anchor="w",
                justify="left",
            )
            subtitle.pack(fill="x", padx=10, pady=(3, 8))
            self._bind_row_click(subtitle, commit_hash)
            self._row_widgets[commit_hash] = row

    def _refresh(self):
        try:
            path = self._project_path()
            if not path.exists() or not path.is_dir():
                self._commits = []
                self._selected_hash = ""
                self._render_commit_list()
                self._set_actions_enabled(False)
                self._status_label.configure(text="项目目录不存在", text_color=COLORS["danger"])
                self._set_text(f"项目目录不存在或不是目录:\n{path}")
                return

            manager = GitManager(path)
            if not manager.is_git_repo():
                self._commits = []
                self._selected_hash = ""
                self._render_commit_list()
                self._set_actions_enabled(False)
                self._status_label.configure(text="不是 Git 仓库", text_color=COLORS["warning"])
                self._set_text("当前目录不是 Git 仓库。自动快照首次触发后会自动初始化仓库。")
                return

            previous_hash = self._selected_hash
            self._commits = manager.get_recent_commits(
                count=self._count(),
                auto_only=bool(self._auto_only_var.get()),
            )
            if self._commits:
                hashes = {self._commit_hash(commit) for commit in self._commits}
                self._selected_hash = previous_hash if previous_hash in hashes else self._commit_hash(self._commits[0])
                dirty = manager.has_changes()
                dirty_text = "工作区有未提交更改" if dirty else "工作区干净"
                self._status_label.configure(
                    text=f"已读取 {len(self._commits)} 条快照，{dirty_text}",
                    text_color=COLORS["warning"] if dirty else COLORS["muted"],
                )
                self._set_actions_enabled(True)
                self._render_commit_list()
                self._show_selected_commit(stat_only=True)
            else:
                self._selected_hash = ""
                self._render_commit_list()
                self._set_actions_enabled(False)
                self._status_label.configure(text="没有找到匹配的快照", text_color=COLORS["warning"])
                self._set_text("没有找到 Git 快照。可以取消“仅自动快照”，或确认该目录是否已经产生自动提交。")
        except Exception as e:
            self._commits = []
            self._selected_hash = ""
            self._render_commit_list()
            self._set_actions_enabled(False)
            self._status_label.configure(text="读取失败", text_color=COLORS["danger"])
            self._set_text(f"读取 Git 快照失败: {e}")

    def _select_commit(self, commit_hash: str):
        self._selected_hash = commit_hash
        self._render_commit_list()
        self._show_selected_commit(stat_only=True)

    def _display_text(self, text: str, max_chars: int = 450_000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n[Diff 太大，界面已截断；复制 Diff 会复制完整内容。]"

    def _show_selected_commit(self, stat_only: bool = True):
        commit = self._selected_commit()
        if not commit:
            return
        commit_hash = self._commit_hash(commit)
        ok, diff = self._manager().get_commit_diff(commit_hash, stat_only=stat_only)
        if not ok:
            self._set_text(diff)
            return
        header = [
            f"Commit: {commit_hash}",
            f"短 hash: {commit.get('short_hash') or commit_hash[:8]}",
            f"时间: {commit.get('date')}",
            f"触发原因: {commit.get('message')}",
            f"改动文件数: {commit.get('changed_files', 0)}",
            f"自动快照: {'是' if commit.get('auto_snapshot') else '否'}",
            "",
        ]
        self._set_text("\n".join(header) + self._display_text(diff))

    def _copy_hash(self):
        commit = self._selected_commit()
        if not commit:
            show_toast(self, "没有可复制的 hash", is_error=True)
            return
        self.clipboard_clear()
        self.clipboard_append(self._commit_hash(commit))
        show_toast(self, "commit hash 已复制")

    def _copy_diff(self):
        commit = self._selected_commit()
        if not commit:
            show_toast(self, "没有可复制的 diff", is_error=True)
            return
        ok, diff = self._manager().get_commit_diff(self._commit_hash(commit), stat_only=False)
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
        commit_hash = self._commit_hash(commit)
        mode = "硬回滚" if hard else "软回滚"
        try:
            dirty = self._manager().has_changes()
        except Exception:
            dirty = False
        dirty_hint = "\n当前工作区有未提交更改，回滚前会先创建安全快照/标签。" if dirty else ""
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
            message=f"确定要回滚到 {commit.get('short_hash') or commit_hash[:12]} 吗？\n{detail}{dirty_hint}",
            on_confirm=do_rollback,
        )
