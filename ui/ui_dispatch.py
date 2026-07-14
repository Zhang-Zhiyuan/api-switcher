from __future__ import annotations


def run_on_ui_thread(widget, callback, logger=None, context: str = "UI callback") -> bool:
    if getattr(widget, "_destroyed", False):
        return False
    dispatch = getattr(widget, "_ui_dispatch", None)
    if not callable(dispatch):
        dispatch = getattr(widget, "_run_on_ui_thread", None)
    if not callable(dispatch):
        try:
            dispatch = getattr(widget.winfo_toplevel(), "_run_on_ui_thread", None)
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
    try:
        widget.after(0, callback)
        return True
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug("Failed to schedule %s: %s", context, exc)
            except Exception:
                pass
        return False
