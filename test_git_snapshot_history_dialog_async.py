import sys
import threading
import time
from types import ModuleType

from ui.dialogs.git_snapshot_history_dialog import GitSnapshotHistoryDialog


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _Label:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(kwargs)


def test_git_snapshot_refresh_runs_off_ui_thread(tmp_path, monkeypatch):
    git_started = threading.Event()
    release_git = threading.Event()
    diff_started = threading.Event()
    text_updates = []
    actions = []

    git_module = ModuleType("core.git_manager")

    class FakeGitManager:
        def __init__(self, path):
            self.path = path

        def is_git_repo(self):
            git_started.set()
            assert release_git.wait(timeout=2)
            return True

        def get_recent_commits(self, count=50, auto_only=True):
            assert count == 50
            assert auto_only is True
            return [
                {
                    "full_hash": "abcdef123456",
                    "short_hash": "abcdef1",
                    "date": "2026-06-16T00:00:00",
                    "message": "snapshot",
                    "changed_files": 1,
                    "auto_snapshot": True,
                }
            ]

        def has_changes(self):
            return False

        def get_commit_diff(self, commit_hash, stat_only=True):
            assert commit_hash == "abcdef123456"
            assert stat_only is True
            diff_started.set()
            return True, " file.py | 1 +"

    git_module.GitManager = FakeGitManager
    monkeypatch.setitem(sys.modules, "core.git_manager", git_module)

    dialog = object.__new__(GitSnapshotHistoryDialog)
    dialog._commits = []
    dialog._selected_hash = ""
    dialog._row_widgets = {}
    dialog._refresh_generation = 0
    dialog._diff_generation = 0
    dialog._project_var = _Var(str(tmp_path))
    dialog._count_var = _Var("50")
    dialog._auto_only_var = _Var(True)
    dialog._status_label = _Label()
    dialog._render_commit_list = lambda: None
    dialog._set_actions_enabled = lambda enabled: actions.append(enabled)
    dialog._set_text = lambda text: text_updates.append(text)
    dialog.winfo_exists = lambda: True
    dialog._run_on_ui_thread = lambda callback: callback()
    dialog.after = lambda *_args: (_ for _ in ()).throw(AssertionError("worker must not call Tk.after"))

    started_at = time.perf_counter()
    GitSnapshotHistoryDialog._refresh(dialog)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.5
    assert git_started.wait(1)
    release_git.set()
    assert diff_started.wait(1)
    deadline = time.time() + 1
    while not any("file.py" in text for text in text_updates) and time.time() < deadline:
        time.sleep(0.01)

    assert dialog._selected_hash == "abcdef123456"
    assert actions[0] is False
    assert True in actions
    assert any("正在后台读取 Git 快照" in text for text in text_updates)
    assert any("file.py" in text for text in text_updates)


def test_git_snapshot_thread_start_failure_reports_error(tmp_path, monkeypatch):
    class BrokenThread:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("ui.dialogs.git_snapshot_history_dialog.threading.Thread", BrokenThread)
    dialog = object.__new__(GitSnapshotHistoryDialog)
    dialog._refresh_generation = 0
    dialog._project_var = _Var(str(tmp_path))
    dialog._count_var = _Var("50")
    dialog._auto_only_var = _Var(True)
    dialog._selected_hash = ""
    dialog._status_label = _Label()
    dialog._set_actions_enabled = lambda _enabled: None
    dialog._set_text = lambda _text: None
    errors = []
    dialog._apply_refresh_error = lambda message, status="": errors.append((message, status))

    GitSnapshotHistoryDialog._refresh(dialog)

    assert errors == [("读取 Git 快照失败: thread unavailable", "启动失败")]
