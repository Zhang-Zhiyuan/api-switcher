from __future__ import annotations

import sys
import threading
from types import ModuleType

from ui.dialogs.health_check_dialog import HealthCheckDialog


class _Widget:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(kwargs)

    def delete(self, *_args):
        return None


def test_health_check_worker_uses_dispatcher_without_direct_tk_calls(monkeypatch):
    validator_module = ModuleType("core.validator")

    class Validator:
        @staticmethod
        def validate_all():
            return ["ok"]

    validator_module.config_validator = Validator()
    monkeypatch.setitem(sys.modules, "core.validator", validator_module)

    callbacks = []
    dialog = object.__new__(HealthCheckDialog)
    dialog.results = []
    dialog.is_checking = True
    dialog._run_on_ui_thread = lambda callback: callbacks.append(callback)
    dialog.winfo_exists = lambda: (_ for _ in ()).throw(
        AssertionError("worker must not call Tk.winfo_exists")
    )
    dialog.after = lambda *_args: (_ for _ in ()).throw(
        AssertionError("worker must not call Tk.after")
    )
    displayed = []
    finished = []
    dialog._display_results = lambda results: displayed.append(results)
    dialog._display_error = lambda error: (_ for _ in ()).throw(AssertionError(error))
    dialog._finish_check = lambda: finished.append(True)

    thread = threading.Thread(target=HealthCheckDialog._run_check, args=(dialog,))
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert dialog.results == ["ok"]
    assert dialog.is_checking is False
    assert len(callbacks) == 2
    for callback in callbacks:
        callback()
    assert displayed == [["ok"]]
    assert finished == [True]


def test_health_check_start_failure_restores_controls(monkeypatch):
    class BrokenThread:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("ui.dialogs.health_check_dialog.threading.Thread", BrokenThread)
    dialog = object.__new__(HealthCheckDialog)
    dialog.is_checking = False
    dialog.check_button = _Widget()
    dialog.export_button = _Widget()
    dialog.result_text = _Widget()
    dialog.status_label = _Widget()
    errors = []
    dialog._display_error = lambda error: errors.append(error)

    HealthCheckDialog._start_check(dialog)

    assert dialog.is_checking is False
    assert dialog.check_button.calls[-1] == {"state": "normal", "text": "重新检查"}
    assert dialog.export_button.calls[-1] == {"state": "normal"}
    assert dialog.status_label.calls[-1] == {"text": "健康检查启动失败"}
    assert errors == ["thread unavailable"]
