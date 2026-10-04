"""Track ownership of pending node text without touching proxy or disk state."""


def _text(widget):
    if widget is None:
        return ""
    try:
        return widget.get("1.0", "end").strip()
    except Exception:
        # An unreadable editor must not be treated as disposable empty input.
        return None


def remember_generated_node(owner, text, profile_id):
    owner.__dict__["_generated_node_draft"] = (str(profile_id or ""), str(text).strip())


def forget_generated_node(owner):
    owner.__dict__.pop("_generated_node_draft", None)


def has_manual_node_draft(owner, widget):
    text = _text(widget)
    if text is None:
        return True
    generated = owner.__dict__.get("_generated_node_draft")
    return bool(text) and (generated is None or text != generated[1])


def discard_generated_node(owner, widget, set_summary, pending_label):
    generated = owner.__dict__.get("_generated_node_draft")
    text = _text(widget)
    if generated is not None:
        if text in ("", generated[1]):
            if widget is not None:
                widget.delete("1.0", "end")
            set_summary(f"{pending_label}: 未选择；请选择当前订阅的节点", "warning")
        forget_generated_node(owner)
    if has_manual_node_draft(owner, widget):
        set_summary(f"{pending_label}: 手工配置已保留（优先于订阅选择）", "warning")


def switch_node_draft_source(owner, widget, profile_id, set_summary, pending_label):
    """Discard only an unchanged generated draft from another subscription."""
    generated = owner.__dict__.get("_generated_node_draft")
    if generated is not None and generated[0] != str(profile_id or ""):
        discard_generated_node(owner, widget, set_summary, pending_label)
    elif has_manual_node_draft(owner, widget):
        set_summary(f"{pending_label}: 手工配置已保留（优先于订阅选择）", "warning")
