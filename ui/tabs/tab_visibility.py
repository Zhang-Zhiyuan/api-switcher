def _owner_tab_label(widget) -> str:
    current = widget
    while current is not None:
        label = str(getattr(current, "_api_switcher_tab_label", "") or "")
        if label:
            return label
        current = getattr(current, "master", None)
    return ""


def is_active_tab(widget) -> bool:
    label = _owner_tab_label(widget)
    if not label:
        return True
    try:
        tabview = getattr(widget.winfo_toplevel(), "_tabview", None)
        return tabview is None or tabview.get() == label
    except Exception:
        return True
