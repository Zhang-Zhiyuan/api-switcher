from ui.widgets import auto_continue_control as control_module
from ui.widgets.auto_continue_control import AutoContinueControl


class _Label:
    def __init__(self):
        self.configurations = []

    def configure(self, **kwargs):
        self.configurations.append(kwargs)


def test_auto_continue_refresh_thread_start_failure_replaces_loading_state(monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    control = object.__new__(AutoContinueControl)
    control.provider = "claude"
    control._refresh_generation = 0
    control._status_label = _Label()
    info = []
    errors = []
    control._set_info_text = info.append
    control._apply_refresh_error = errors.append
    monkeypatch.setattr(control_module.threading, "Thread", FailingThread)

    AutoContinueControl.refresh(control)

    assert control._refresh_generation == 1
    assert info == ["正在后台读取自动续跑状态..."]
    assert errors == ["刷新任务启动失败: thread unavailable"]
