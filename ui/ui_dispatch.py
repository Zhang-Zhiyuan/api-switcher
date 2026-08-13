from __future__ import annotations

import threading


def _safe_attribute(obj, name: str, default=None):
    """Read a Python attribute without triggering Tk's dynamic __getattr__."""
    try:
        return object.__getattribute__(obj, name)
    except Exception:
        return default


def _find_python_dispatch(widget):
    """Find an App dispatcher through Python master links without calling Tk."""
    current = widget
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        dispatch = _safe_attribute(current, "_ui_dispatch")
        if not callable(dispatch):
            dispatch = _safe_attribute(current, "_run_on_ui_thread")
        if callable(dispatch):
            return dispatch
        current = _safe_attribute(current, "master")
    return None


def run_on_ui_thread(widget, callback, logger=None, context: str = "UI callback") -> bool:
    if bool(_safe_attribute(widget, "_destroyed", False)):
        return False
    dispatch = _find_python_dispatch(widget)
    on_main_thread = threading.current_thread() is threading.main_thread()
    if not callable(dispatch) and on_main_thread:
        try:
            top = widget.winfo_toplevel()
            dispatch = _safe_attribute(top, "_run_on_ui_thread")
        except Exception:
            dispatch = None
    if callable(dispatch):
        try:
            result = dispatch(callback)
            return result is not False
        except Exception as exc:
            if logger is not None:
                try:
                    logger.debug("Failed to schedule %s: %s", context, exc)
                except Exception:
                    pass
            return False
    if on_main_thread:
        try:
            widget.after(0, callback)
            return True
        except Exception as exc:
            if logger is not None:
                try:
                    logger.debug("Failed to schedule %s: %s", context, exc)
                except Exception:
                    pass
    elif logger is not None:
        try:
            logger.debug("Failed to schedule %s: no thread-safe UI dispatcher", context)
        except Exception:
            pass
    return False
