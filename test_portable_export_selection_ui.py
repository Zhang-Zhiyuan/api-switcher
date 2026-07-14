from types import SimpleNamespace

from ui.dialogs.portable_export_selection_dialog import (
    PORTABLE_PROFILE_GROUPS,
    PortableExportSelectionDialog,
    normalize_portable_profile_options,
)
from ui.tabs import backup_tab as backup_tab_module
from ui.tabs.backup_tab import BackupTab


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Configurable:
    def __init__(self):
        self.values = {}

    def configure(self, **kwargs):
        self.values.update(kwargs)


def _dialog_without_tk(options):
    dialog = object.__new__(PortableExportSelectionDialog)
    dialog._options = normalize_portable_profile_options(options)
    dialog._variables = {
        key: {name: _Var(True) for name in dialog._options[key]}
        for key, _label in PORTABLE_PROFILE_GROUPS
    }
    dialog._total_count = sum(len(names) for names in dialog._options.values())
    dialog._status = _Configurable()
    dialog._error = _Configurable()
    dialog._confirm_button = _Configurable()
    return dialog


def test_portable_options_keep_supported_unique_profiles_in_display_order():
    options = normalize_portable_profile_options(
        {
            "claude_profiles": ["Claude A", "Claude A", "Claude B"],
            "codex_profiles": ["Codex A", "", None],
            "browser_profiles": ["Chrome"],
            "unsupported": ["ignored"],
        }
    )

    assert options == {
        "claude_profiles": ["Claude A", "Claude B"],
        "codex_profiles": ["Codex A"],
        "ssh_profiles": [],
        "browser_profiles": ["Chrome"],
    }


def test_portable_selection_defaults_to_all_and_rejects_empty_selection():
    dialog = _dialog_without_tk(
        {
            "claude_profiles": ["Claude A"],
            "ssh_profiles": ["Server A"],
            "browser_profiles": ["Chrome"],
        }
    )
    confirmed = []
    destroyed = []
    dialog._on_confirm = confirmed.append
    dialog.destroy = lambda: destroyed.append(True)

    assert dialog._selected_profiles() == {
        "claude_profiles": ["Claude A"],
        "codex_profiles": [],
        "ssh_profiles": ["Server A"],
        "browser_profiles": ["Chrome"],
    }

    dialog._set_all(False)
    dialog._confirm()

    assert confirmed == []
    assert destroyed == []
    assert dialog._error.values["text"] == "请至少选择一个 Profile"
    assert dialog._confirm_button.values["state"] == "disabled"

    dialog._variables["browser_profiles"]["Chrome"].set(True)
    dialog._update_status()
    dialog._confirm()

    assert destroyed == [True]
    assert confirmed == [
        {
            "claude_profiles": [],
            "codex_profiles": [],
            "ssh_profiles": [],
            "browser_profiles": ["Chrome"],
        }
    ]


def test_backup_tab_selects_before_password_and_exports_in_worker(monkeypatch):
    events = []
    selection_callback = []
    password_callback = []
    export_calls = []
    top = object()
    options = {
        "claude_profiles": ["Claude A", "Claude B"],
        "codex_profiles": ["Codex A"],
        "ssh_profiles": ["Server A"],
        "browser_profiles": ["Chrome"],
    }
    selection = {
        "claude_profiles": ["Claude B"],
        "codex_profiles": [],
        "ssh_profiles": ["Server A"],
        "browser_profiles": [],
    }

    def list_options():
        events.append("list-options")
        return options

    def export_profiles(path, password, *, selection):
        events.append("export")
        export_calls.append((path, password, selection))
        return SimpleNamespace(
            profile_count=2,
            secret_count=1,
            missing_secret_refs=[],
            browser_file_count=0,
            skipped_browser_files=[],
        )

    def selection_dialog(master, *, options, on_confirm):
        events.append("selection-dialog")
        assert master is top
        assert options is options_from_core
        selection_callback.append(on_confirm)

    def choose_path(**kwargs):
        events.append("save-dialog")
        assert kwargs["parent"] is top
        return "selected.asxprofile"

    def password_dialog(master, *, on_confirm, **kwargs):
        events.append("password-dialog")
        assert master is top
        assert kwargs["confirm_password"] is True
        password_callback.append(on_confirm)

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            events.append("thread-created")
            assert name == "portable-profile-export"
            assert daemon is True
            self.target = target

        def start(self):
            events.append("thread-start")
            self.target()

    options_from_core = options
    monkeypatch.setattr(
        backup_tab_module,
        "portable_migration",
        SimpleNamespace(
            list_portable_profile_options=list_options,
            export_portable_profiles=export_profiles,
        ),
    )
    monkeypatch.setattr(backup_tab_module, "PortableExportSelectionDialog", selection_dialog)
    monkeypatch.setattr(backup_tab_module.filedialog, "asksaveasfilename", choose_path)
    monkeypatch.setattr(backup_tab_module, "PasswordDialog", password_dialog)
    monkeypatch.setattr(backup_tab_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(backup_tab_module, "run_on_ui_thread", lambda _widget, callback: callback())
    monkeypatch.setattr(backup_tab_module, "show_toast", lambda *_args, **_kwargs: None)

    tab = object.__new__(BackupTab)
    tab.winfo_toplevel = lambda: top

    BackupTab._export_portable(tab)

    assert events == ["list-options", "selection-dialog"]
    assert export_calls == []

    selection_callback[0](selection)

    assert events == ["list-options", "selection-dialog", "save-dialog", "password-dialog"]
    assert export_calls == []

    password_callback[0]("strong-password")

    assert events == [
        "list-options",
        "selection-dialog",
        "save-dialog",
        "password-dialog",
        "thread-created",
        "thread-start",
        "export",
    ]
    assert export_calls == [("selected.asxprofile", "strong-password", selection)]
