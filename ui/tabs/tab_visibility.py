def is_active_tab(widget) -> bool:
    label = getattr(widget, "_api_switcher_tab_label", "")
    if not label:
        return True
    try:
        tabview = getattr(widget.winfo_toplevel(), "_tabview", None)
        return tabview is None or tabview.get() == label
    except Exception:
        return True
