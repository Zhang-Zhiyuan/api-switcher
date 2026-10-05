"""Native wheel, lifecycle and redraw regressions; synthetic widgets only."""
import gc
import time
import weakref

import customtkinter as ctk
import pytest

from ui import theme


@pytest.fixture
def view(tk_root, monkeypatch):
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *exc: errors.append(exc))
    window = ctk.CTkToplevel(tk_root)
    window.geometry("600x480")
    outer = ctk.CTkScrollableFrame(window)
    outer.pack(fill="both", expand=True)
    inner = ctk.CTkScrollableFrame(outer, height=120)
    inner.pack(fill="x", pady=10)
    content = ctk.CTkFrame(inner, height=1200)
    content.pack(fill="x")
    ctk.CTkFrame(outer, height=1200).pack(fill="x")
    deadline = time.monotonic() + 3
    while not window.winfo_viewable() or inner._parent_canvas.winfo_height() <= 1:
        assert time.monotonic() < deadline, "synthetic window did not map"
        tk_root.update()
        time.sleep(0.01)
    tk_root.update()
    yield tk_root, window, outer, inner, content
    window.destroy()
    tk_root.update()
    assert not errors


def test_closed_and_hidden_frames_do_not_accumulate_global_bindings_or_references(view):
    root, window, outer, inner, _ = view
    hooks = ("<MouseWheel>", "<KeyPress-Shift_L>", "<KeyPress-Shift_R>",
             "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>")
    before = {sequence: root.bind_all(sequence) for sequence in hooks}
    assert before["<MouseWheel>"].count("_dispatch_mouse_wheel") == 1
    references = []
    for _ in range(40):
        host = ctk.CTkFrame(window)
        frame = ctk.CTkScrollableFrame(host)
        references.append(weakref.ref(frame))
        host.destroy()
    del frame, host
    gc.collect()
    assert all(reference() is None for reference in references)
    assert {sequence: root.bind_all(sequence) for sequence in hooks} == before
    inner._parent_canvas.event_generate("<MouseWheel>", delta=-120)
    root.update()
    assert inner._parent_canvas.yview()[0] > 0
    assert outer._parent_canvas.yview()[0] == 0


@pytest.mark.parametrize("delta, boundary", [(-120, 1), (120, 0)])
def test_nested_wheel_moves_one_surface_and_bubbles_only_at_boundary(view, delta, boundary):
    root, _, outer, inner, _ = view
    parent, canvas = outer._parent_canvas, inner._parent_canvas
    canvas.yview_moveto(0.4)
    parent.yview_moveto(0.4)
    root.update()
    before_inner, before_outer = canvas.yview(), parent.yview()
    canvas.event_generate("<MouseWheel>", delta=delta)
    root.update()
    assert canvas.yview() != before_inner
    assert parent.yview() == before_outer
    canvas.yview_moveto(boundary)
    root.update()
    canvas.event_generate("<MouseWheel>", delta=delta)
    root.update()
    assert parent.yview() != before_outer
    assert theme.recent_user_scroll(inner, idle_ms=1000)


def test_custom_global_handlers_survive_and_scrolling_is_not_multiplied(view):
    root, window, _, inner, _ = view
    calls = []
    binding = inner.bind_all("<MouseWheel>", lambda event: calls.append(event.delta), add="+")
    try:
        for _ in range(3):
            host = ctk.CTkFrame(window)
            ctk.CTkScrollableFrame(host)
            host.destroy()
        canvas = inner._parent_canvas
        canvas.yview_moveto(0)
        root.update()
        before = canvas.canvasy(0)
        for _ in range(10):
            canvas.event_generate("<MouseWheel>", delta=-120)
        root.update()
        assert calls == [-120] * 10
        assert canvas.canvasy(0) - before == 240
    finally:
        # Remove just this test's listener, not unrelated application handlers.
        root._unbind(("bind", "all", "<MouseWheel>"), binding)


def test_wheel_on_inner_scrollbar_does_not_also_move_outer_page(view):
    root, _, outer, inner, _ = view
    parent, canvas = outer._parent_canvas, inner._parent_canvas
    for fraction in (0.4, 1.0):
        canvas.yview_moveto(fraction)
        root.update()
        before_parent, before_inner = parent.yview(), canvas.yview()
        inner._scrollbar._canvas.event_generate("<MouseWheel>", delta=-120)
        root.update()
        assert parent.yview() == before_parent
        if fraction < 1:
            assert canvas.yview()[0] > before_inner[0]


def test_shift_wheel_moves_horizontal_scroller_without_a_prior_shift_keypress(view):
    root, _, outer, inner, _ = view
    horizontal = ctk.CTkScrollableFrame(outer, orientation="horizontal", height=80)
    horizontal.pack(fill="x", before=inner._parent_frame)
    ctk.CTkFrame(horizontal, width=2400, height=40).pack()
    root.update()
    canvas = horizontal._parent_canvas
    before_parent = outer._parent_canvas.yview()
    canvas.event_generate("<MouseWheel>", delta=-120, state=1)
    root.update()
    assert canvas.xview()[0] > 0
    assert outer._parent_canvas.yview() == before_parent
    # A following vertical gesture is independent of Shift release/focus events.
    before_horizontal = canvas.xview()
    canvas.event_generate("<MouseWheel>", delta=-120, state=0)
    root.update()
    assert canvas.xview() == before_horizontal
    assert outer._parent_canvas.yview()[0] > before_parent[0]


def test_scrollregion_changes_for_resize_not_for_scroll_or_drag(view, monkeypatch):
    root, _, outer, inner, content = view
    canvas = inner._parent_canvas
    updates = []
    original = canvas.configure

    def configure(*args, **kwargs):
        if "scrollregion" in kwargs:
            updates.append(kwargs["scrollregion"])
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas, "configure", configure)
    for fraction in (0.1, 0.3, 0.5, 0.2):
        canvas.yview_moveto(fraction)
        root.update()
    assert not updates
    content.configure(height=1800)
    root.update()
    assert updates
    assert float(canvas.cget("scrollregion").split()[-1]) >= 1800
    updates.clear()
    parent_before = outer._parent_canvas.yview()
    bar = inner._scrollbar._canvas
    bar.event_generate("<Button-1>", x=bar.winfo_width() // 2, y=int(bar.winfo_height() * 0.7))
    bar.event_generate("<B1-Motion>", x=bar.winfo_width() // 2, y=int(bar.winfo_height() * 0.8))
    root.update()
    assert canvas.yview()[0] > 0.4 and not updates
    assert outer._parent_canvas.yview() == parent_before
    content.configure(height=40)
    root.update()
    assert canvas.yview() == (0, 1), {
        "updates": updates, "bounds": canvas.bbox("all"), "region": canvas.cget("scrollregion"),
        "frame_size": (inner.winfo_width(), inner.winfo_height()),
        "content_height": (content.winfo_height(), content.winfo_reqheight()),
    }


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.5])
def test_wheel_and_wrap_layout_after_dpi_change(view, scale):
    root, _, _, inner, _ = view
    previous = ctk.ScalingTracker.widget_scaling
    label = ctk.CTkLabel(inner, text="合成测试布局 " * 40, justify="left")
    label.pack(fill="x")
    theme.bind_wraplength(inner, label, padding=16, max_width=2000)
    try:
        ctk.set_widget_scaling(scale)
        root.update()
        canvas = inner._parent_canvas
        canvas.yview_moveto(0)
        root.update()
        canvas.event_generate("<MouseWheel>", delta=-120)
        root.update()
        assert canvas.yview()[0] > 0
        assert label.cget("wraplength") == round(inner.winfo_width() / inner._get_widget_scaling()) - 16
        # Shift is taken from the actual event, not a key flag left on an old tab.
        before = canvas.yview()
        canvas.event_generate("<MouseWheel>", delta=-120, state=1)
        root.update()
        assert canvas.yview() == before
    finally:
        ctk.set_widget_scaling(previous)
        root.update()
