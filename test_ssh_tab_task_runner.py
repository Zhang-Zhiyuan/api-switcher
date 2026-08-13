from ui.tabs import ssh_tab


class ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class FakeButton:
    def __init__(self):
        self.states = []

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.states.append(kwargs["state"])


class FakeSSHTab:
    def __init__(self):
        self._ssh_busy = False
        self._remote_inspect_button = FakeButton()
        self._remote_pull_button = FakeButton()
        self._remote_pull_options = {"item": ("kind",)}
        self.statuses = []
        self.toasts = []
        self.refreshed = False

    def _set_sync_status(self, message, severity="info"):
        self.statuses.append((message, severity))

    def _run_on_ui_thread(self, callback):
        callback()

    def winfo_exists(self):
        return True

    def winfo_toplevel(self):
        return self

    def refresh(self):
        self.refreshed = True


class FakeRemoteAutoTab:
    def __init__(self):
        self._remote_auto_busy = False
        self.busy_events = []
        self.statuses = []
        self.toasts = []

    def _set_remote_auto_busy(self, busy, message=None):
        self._remote_auto_busy = busy
        self.busy_events.append((busy, message))

    def _set_remote_auto_status(self, message, severity="info"):
        self.statuses.append((message, severity))

    def _run_on_ui_thread(self, callback):
        callback()

    def winfo_exists(self):
        return True

    def winfo_toplevel(self):
        return self


def test_ssh_task_restores_controls_when_done_callback_fails(monkeypatch):
    tab = FakeSSHTab()
    monkeypatch.setattr(ssh_tab.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(ssh_tab, "show_toast", lambda _root, message, **kwargs: tab.toasts.append((message, kwargs)))

    def fail_done(_payload):
        raise RuntimeError("render broken")

    ssh_tab.SSHTab._run_ssh_task(
        tab,
        "working",
        lambda: "ok",
        on_done=fail_done,
        refresh=True,
    )

    assert tab._ssh_busy is False
    assert tab._remote_inspect_button.states[-1] == "normal"
    assert tab._remote_pull_button.states[-1] == "normal"
    assert tab.refreshed is True
    assert tab.statuses[-1] == ("操作结果处理失败: render broken", "error")
    assert tab.toasts[-1][1]["is_error"] is True


def test_remote_auto_task_reports_done_callback_failure(monkeypatch):
    tab = FakeRemoteAutoTab()
    monkeypatch.setattr(ssh_tab.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(ssh_tab, "show_toast", lambda _root, message, **kwargs: tab.toasts.append((message, kwargs)))

    def fail_done(_payload):
        raise RuntimeError("render broken")

    ssh_tab.SSHTab._run_remote_auto_task(
        tab,
        "checking",
        lambda: {"results": [], "statuses": [], "failures": []},
        fail_done,
    )

    assert tab._remote_auto_busy is False
    assert tab.busy_events == [(True, "checking"), (False, None)]
    assert tab.statuses[-1] == ("远端自动续跑结果处理失败: render broken", "error")
    assert tab.toasts[-1][1]["is_error"] is True
