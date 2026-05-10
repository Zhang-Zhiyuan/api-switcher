import customtkinter as ctk
from tkinter import filedialog

from core.browser_profile_manager import browser_profile_manager
from models.profile import BrowserProfile
from ui.theme import COLORS, button_style, center_window, combo_style, font, input_style


class BrowserProfileEditorDialog(ctk.CTkToplevel):
    """Dialog for creating or editing a browser profile."""

    def __init__(self, master, title="编辑浏览器 Profile", profile: BrowserProfile | None = None, on_save=None):
        super().__init__(master)
        self.title(title)
        self.geometry("700x760")
        self.minsize(580, 600)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_save = on_save
        self._profile = profile
        self._fields = {}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(header, text=title, text_color=COLORS["text"], font=font(18, "bold")).pack(anchor="w")

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        self._add_field(scroll, "名称", "name", profile.name if profile else "")
        self._add_combo(scroll, "浏览器", "browser_type", ["chrome", "edge"], profile.browser_type if profile else "chrome")
        self._add_combo(scroll, "模式", "profile_mode", ["managed", "external"], profile.profile_mode if profile else "managed")
        self._add_combo(scroll, "默认目标", "start_target", ["chatgpt", "claude", "custom"], profile.start_target if profile else "chatgpt")
        self._add_field(scroll, "自定义 URL", "custom_url", profile.custom_url if profile else "")
        self._add_field(scroll, "备注", "notes", profile.notes if profile else "")

        launch_box = ctk.CTkFrame(scroll, fg_color=COLORS["surface"], corner_radius=8, border_width=1, border_color=COLORS["border_soft"])
        launch_box.pack(fill="x", pady=(12, 6))
        ctk.CTkLabel(launch_box, text="隔离启动", text_color=COLORS["text"], font=font(13, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        self._add_field(launch_box, "窗口宽度", "launch_width", getattr(profile, "launch_width", 1280) if profile else 1280)
        self._add_field(launch_box, "窗口高度", "launch_height", getattr(profile, "launch_height", 900) if profile else 900)
        self._add_field(launch_box, "语言代码", "launch_language", getattr(profile, "launch_language", "zh-CN") if profile else "zh-CN")
        ctk.CTkLabel(
            launch_box,
            text="Profile 会隔离 Cookies、本地存储、IndexedDB 和浏览器缓存；网页仍可能基于 IP、系统、显卡、字体、WebGL、Canvas、时区等生成设备指纹，跨机器无法保证完全一致。",
            text_color=COLORS["muted"],
            font=font(11),
            justify="left",
            wraplength=500,
        ).pack(fill="x", padx=12, pady=(4, 10))

        # Executable row
        exe_row = ctk.CTkFrame(scroll, fg_color="transparent")
        exe_row.pack(fill="x", pady=5)
        ctk.CTkLabel(exe_row, text="浏览器可执行文件", width=128, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._exe_entry = ctk.CTkEntry(exe_row, width=300, **input_style())
        if profile and profile.browser_executable:
            self._exe_entry.insert(0, profile.browser_executable)
        self._exe_entry.pack(side="left", padx=(0, 5), fill="x", expand=True)
        ctk.CTkButton(exe_row, text="浏览", width=58, command=self._browse_exe, **button_style("secondary", compact=True)).pack(side="left")

        # Path row
        path_row = ctk.CTkFrame(scroll, fg_color="transparent")
        path_row.pack(fill="x", pady=5)
        ctk.CTkLabel(path_row, text="Profile 路径", width=128, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._path_entry = ctk.CTkEntry(path_row, width=300, **input_style())
        if profile and profile.user_data_dir:
            self._path_entry.insert(0, profile.user_data_dir)
        self._path_entry.pack(side="left", padx=(0, 5), fill="x", expand=True)
        ctk.CTkButton(path_row, text="浏览", width=58, command=self._browse_dir, **button_style("secondary", compact=True)).pack(side="left", padx=(0, 5))
        ctk.CTkButton(path_row, text="生成", width=58, command=self._generate_managed_path, **button_style("accent", compact=True)).pack(side="left")

        # Switches
        switches = ctk.CTkFrame(scroll, fg_color="transparent")
        switches.pack(fill="x", pady=(10, 4))
        self._allow_full_reset = ctk.CTkSwitch(
            switches,
            text="允许整目录清理（仅建议托管 profile）",
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._allow_full_reset.pack(anchor="w")
        if profile and profile.allow_full_reset:
            self._allow_full_reset.select()

        self._created_by_app = ctk.CTkSwitch(
            switches,
            text="该目录由应用创建/管理",
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._created_by_app.pack(anchor="w", pady=(8, 0))
        if profile and profile.created_by_app:
            self._created_by_app.select()
        elif not profile:
            self._created_by_app.select()

        # Validate button + message
        validate_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        validate_frame.pack(fill="x", pady=(12, 6))
        ctk.CTkButton(validate_frame, text="检查配置", width=110, command=self._validate_profile, **button_style("accent")).pack()
        self._validate_result = ctk.CTkLabel(scroll, text="", text_color=COLORS["muted"], font=font(12))
        self._validate_result.pack(pady=(0, 5))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(btn_frame, text="取消", width=84, command=self.destroy, **button_style("secondary")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_frame, text="保存", width=84, command=self._save, **button_style("primary")).pack(side="right")

        center_window(self, master)

    def _add_field(self, parent, label, key, value=""):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(row, text=label, width=128, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        widget = ctk.CTkEntry(row, width=380, **input_style())
        widget.insert(0, str(value or ""))
        widget.pack(side="left", fill="x", expand=True)
        self._fields[key] = widget
        return widget

    def _add_combo(self, parent, label, key, values, current):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(row, text=label, width=128, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        widget = ctk.CTkComboBox(
            row,
            values=values,
            width=380,
            **combo_style(),
        )
        widget.set(current)
        widget.pack(side="left", fill="x", expand=True)
        self._fields[key] = widget
        return widget

    def _browse_dir(self):
        path = filedialog.askdirectory(title="选择浏览器 Profile 目录")
        if path:
            self._path_entry.delete(0, "end")
            self._path_entry.insert(0, path)

    def _browse_exe(self):
        path = filedialog.askopenfilename(
            title="选择浏览器可执行文件",
            filetypes=[("Executable", "*.exe"), ("All Files", "*.*")],
        )
        if path:
            self._exe_entry.delete(0, "end")
            self._exe_entry.insert(0, path)

    def _generate_managed_path(self):
        name = self._fields["name"].get().strip() or "profile"
        browser_type = self._fields["browser_type"].get()
        path = browser_profile_manager.build_managed_profile_path(name, browser_type)
        self._path_entry.delete(0, "end")
        self._path_entry.insert(0, str(path))
        self._fields["profile_mode"].set("managed")
        self._created_by_app.select()

    def _field_int(self, key: str, label: str) -> int:
        value = self._fields[key].get().strip()
        if not value:
            raise ValueError(f"{label}不能为空")
        try:
            return int(value)
        except ValueError as e:
            raise ValueError(f"{label}必须是数字") from e

    def _collect_profile(self) -> BrowserProfile:
        return BrowserProfile(
            name=self._fields["name"].get().strip(),
            browser_type=self._fields["browser_type"].get(),
            profile_mode=self._fields["profile_mode"].get(),
            user_data_dir=self._path_entry.get().strip(),
            start_target=self._fields["start_target"].get(),
            custom_url=self._fields["custom_url"].get().strip() or None,
            notes=self._fields["notes"].get().strip() or None,
            allow_full_reset=self._allow_full_reset.get() == 1,
            created_by_app=self._created_by_app.get() == 1,
            browser_executable=self._exe_entry.get().strip() or None,
            launch_width=self._field_int("launch_width", "窗口宽度"),
            launch_height=self._field_int("launch_height", "窗口高度"),
            launch_language=self._fields["launch_language"].get().strip() or None,
        )

    def _validate_profile(self):
        try:
            profile = self._collect_profile()
            valid, error = browser_profile_manager.validate_profile(profile)
            if valid:
                self._validate_result.configure(text="配置有效", text_color=COLORS["success"])
            else:
                self._validate_result.configure(text=error, text_color=COLORS["danger"])
        except Exception as e:
            self._validate_result.configure(text=f"检查失败: {e}", text_color=COLORS["danger"])

    def _save(self):
        try:
            profile = self._collect_profile()
            valid, error = browser_profile_manager.validate_profile(profile)
            if not valid:
                self._validate_result.configure(text=error, text_color=COLORS["danger"])
                return
            if self._on_save:
                self._on_save(profile, self._profile)
            self.destroy()
        except Exception as e:
            self._validate_result.configure(text=f"保存失败: {e}", text_color=COLORS["danger"])
