from __future__ import annotations

import threading


_AUTO_CONTINUE_CONTROL_CLASS = None
_AUTO_CONTINUE_CONTROL_LOCK = threading.Lock()
_AUTO_CONTINUE_CONTROL_PRELOAD_STARTED = False


def resolve_auto_continue_control_class():
    global _AUTO_CONTINUE_CONTROL_CLASS
    cached = _AUTO_CONTINUE_CONTROL_CLASS
    if cached is not None:
        return cached
    from ui.widgets.auto_continue_control import AutoContinueControl

    with _AUTO_CONTINUE_CONTROL_LOCK:
        if _AUTO_CONTINUE_CONTROL_CLASS is None:
            _AUTO_CONTINUE_CONTROL_CLASS = AutoContinueControl
        return _AUTO_CONTINUE_CONTROL_CLASS


def preload_auto_continue_control_class() -> None:
    global _AUTO_CONTINUE_CONTROL_PRELOAD_STARTED
    with _AUTO_CONTINUE_CONTROL_LOCK:
        if _AUTO_CONTINUE_CONTROL_CLASS is not None or _AUTO_CONTINUE_CONTROL_PRELOAD_STARTED:
            return
        _AUTO_CONTINUE_CONTROL_PRELOAD_STARTED = True

    def run():
        global _AUTO_CONTINUE_CONTROL_PRELOAD_STARTED
        try:
            resolve_auto_continue_control_class()
        except Exception:
            with _AUTO_CONTINUE_CONTROL_LOCK:
                _AUTO_CONTINUE_CONTROL_PRELOAD_STARTED = False

    threading.Thread(target=run, name="auto-continue-control-preload", daemon=True).start()
