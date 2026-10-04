"""Logical DPI/pack contracts using widget doubles only; never creates Tk."""
from types import SimpleNamespace
import tkinter

import pytest

from ui.dialogs import confirm_dialog, profile_editor, ssh_editor
from ui.theme import sync_scrollable_frame_width


class Widget:
    def __init__(self, master=None, *, kind="widget", **options):
        self.master = master
        self.kind = kind
        self.options = options
        self.packed = []
        self.pack_options = {}
        self.grid_options = {}
        self.bindings = {}
        self.value = ""
        self.scaling = 1.0
        self.width = 300

    def pack(self, **options):
        self.pack_options = options
        siblings = self.master.packed
        if self in siblings:
            siblings.remove(self)
        if options.get("before") in siblings:
            siblings.insert(siblings.index(options["before"]), self)
        else:
            siblings.append(self)

    def pack_forget(self):
        if self in self.master.packed:
            self.master.packed.remove(self)

    def pack_configure(self, **options):
        self.pack_options.update(options)

    def grid(self, **options):
        self.grid_options = options

    def grid_columnconfigure(self, *_args, **_kwargs):
        pass

    def bind(self, sequence, callback, **_kwargs):
        self.bindings[sequence] = callback

    def configure(self, **options):
        self.options.update(options)

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def insert(self, _index, value):
        self.value = value

    def winfo_width(self):
        return self.width

    def _get_widget_scaling(self):
        return self.scaling


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0, 2.5])
def test_paired_footer_breakpoint_uses_logical_not_physical_width(scale):
    frame, primary, secondary = Widget(), Widget(), Widget()
    frame.width = round(200 * scale)
    frame.scaling = scale
    confirm_dialog._bind_two_button_footer(frame, primary, secondary)
    assert primary.grid_options["row"] == 0
    assert secondary.grid_options["row"] == 1
    assert secondary.grid_options["column"] == 0

    frame.width = round(240 * scale)
    frame.bindings["<Configure>"](SimpleNamespace(width=frame.width))
    assert secondary.grid_options["row"] == 0
    assert secondary.grid_options["column"] == 1


@pytest.mark.parametrize("editor", [profile_editor.ProfileEditorDialog, ssh_editor.SSHEditorDialog])
@pytest.mark.parametrize("window_scale,widget_scale", [(1.0, 1.5), (2.0, 1.5), (1.5, 1.5)])
def test_editor_breakpoint_uses_content_widget_scaling(editor, window_scale, widget_scale):
    dialog = object.__new__(editor)
    dialog.winfo_width = lambda: 900
    dialog._get_window_scaling = lambda: window_scale
    dialog._test_btn = SimpleNamespace(_get_widget_scaling=lambda: widget_scale)
    assert dialog._logical_width() == round(900 / widget_scale)


def stale_scroll_geometry(content_width=1455, viewport_width=1055):
    calls = []
    scroll = SimpleNamespace(_orientation="vertical", _create_window_id=42,
                             winfo_width=lambda: content_width)
    scroll._parent_canvas = SimpleNamespace(
        winfo_width=lambda: viewport_width,
        # The configured item size alone cannot detect this native Tk failure.
        itemcget=lambda *_args: str(viewport_width),
        itemconfigure=lambda item_id, **options: calls.append((item_id, options)),
    )
    return scroll, calls


def test_scroll_width_repair_checks_actual_frame_even_when_item_size_is_correct():
    scroll, calls = stale_scroll_geometry()
    assert sync_scrollable_frame_width(scroll)
    assert calls == [(42, {"width": 1055})]
    scroll.winfo_width = lambda: 1055
    assert not sync_scrollable_frame_width(scroll)
    assert len(calls) == 1


@pytest.mark.parametrize("content_width,viewport_width", [(1055, 1055), (1, 1055), (1455, 1), (0, 0)])
def test_scroll_width_repair_ignores_aligned_or_unrealized_geometry(content_width, viewport_width):
    scroll, calls = stale_scroll_geometry(content_width, viewport_width)
    assert not sync_scrollable_frame_width(scroll)
    assert not calls


def test_scroll_width_repair_does_not_constrain_horizontal_content():
    scroll, calls = stale_scroll_geometry()
    scroll._orientation = "horizontal"
    assert not sync_scrollable_frame_width(scroll)
    assert not calls


def test_scroll_width_repair_handles_missing_or_destroyed_widgets():
    assert not sync_scrollable_frame_width(None)
    assert not sync_scrollable_frame_width(SimpleNamespace())
    scroll, calls = stale_scroll_geometry()

    def destroyed():
        raise tkinter.TclError("synthetic destroyed widget")

    scroll.winfo_width = destroyed
    assert not sync_scrollable_frame_width(scroll)
    assert not calls


@pytest.mark.parametrize("editor", [profile_editor.ProfileEditorDialog, ssh_editor.SSHEditorDialog])
def test_editor_repairs_scroll_width_even_with_unchanged_responsive_bucket(editor):
    dialog = object.__new__(editor)
    dialog._logical_width = lambda: 560
    dialog._responsive_stacked = True
    dialog._form_scroll, calls = stale_scroll_geometry()
    dialog._apply_responsive_layout()
    assert calls == [(42, {"width": 1055})]


@pytest.fixture
def fake_constructors(monkeypatch):
    def initialize(dialog, *_args, **_kwargs):
        dialog.packed = []

    monkeypatch.setattr(profile_editor.ctk.CTkToplevel, "__init__", initialize)
    for method in ("title", "geometry", "minsize", "resizable", "configure", "grab_set", "bind"):
        monkeypatch.setattr(profile_editor.ctk.CTkToplevel, method, lambda *_args, **_kwargs: None)
    for name in ("CTkFrame", "CTkScrollableFrame", "CTkLabel", "CTkButton", "CTkEntry", "CTkComboBox"):
        monkeypatch.setattr(profile_editor.ctk, name,
                            lambda master, _kind=name, **kwargs: Widget(master, kind=_kind, **kwargs))
    for module in (profile_editor, ssh_editor):
        monkeypatch.setattr(module, "font", lambda *_args, **_kwargs: ("synthetic", 12))
        monkeypatch.setattr(module, "button_style", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(module, "input_style", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(module, "combo_style", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(module, "MaskedEntry", lambda master, **kwargs: Widget(master, **kwargs))
        monkeypatch.setattr(module, "bind_wraplength", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(module, "center_window", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(profile_editor.ProfileEditorDialog, "_build_claude_fields", lambda *_args: None)
    monkeypatch.setattr(profile_editor.ProfileEditorDialog, "_build_codex_fields", lambda *_args: None)
    monkeypatch.setattr(profile_editor.ProfileEditorDialog, "_schedule_responsive_layout", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ssh_editor.SSHEditorDialog, "_schedule_responsive_layout", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("kind", ["claude", "codex", "ssh"])
def test_editor_reserves_footer_before_expanding_scroll_body(fake_constructors, kind):
    master = SimpleNamespace()
    dialog = (ssh_editor.SSHEditorDialog(master) if kind == "ssh"
              else profile_editor.ProfileEditorDialog(master, profile_type=kind))
    scroll = next(item for item in dialog.packed if item.kind == "CTkScrollableFrame")
    footer = next(item for item in dialog.packed
                  if any(child.options.get("text") == "保存" for child in item.packed))
    assert dialog.packed.index(footer) < dialog.packed.index(scroll)
    assert footer.pack_options.get("side") == "bottom"
    assert scroll.pack_options.get("expand") is True
    if kind != "ssh":
        assert dialog._error_label.master is scroll


@pytest.mark.parametrize("logical_width,expected_indent", [(560, (0, 0)), (640, (128, 0))])
def test_ssh_directory_hint_tracks_responsive_field_indent(logical_width, expected_indent):
    dialog = object.__new__(ssh_editor.SSHEditorDialog)
    dialog._logical_width = lambda: logical_width
    dialog._responsive_stacked = None
    dialog._field_layouts = {}
    dialog._auth_hint = Widget()
    dialog._remote_dir_hint = Widget()
    dialog._apply_responsive_layout()
    assert dialog._auth_hint.pack_options["padx"] == expected_indent
    assert dialog._remote_dir_hint.pack_options["padx"] == expected_indent


def test_ssh_directory_hint_wrap_uses_its_allocated_width(fake_constructors, monkeypatch):
    bindings = []
    monkeypatch.setattr(ssh_editor, "bind_wraplength",
                        lambda container, label, **options: bindings.append((container, label, options)))
    dialog = ssh_editor.SSHEditorDialog(SimpleNamespace())
    binding = next(item for item in bindings if item[1] is dialog._remote_dir_hint)
    assert binding[0] is dialog._remote_dir_hint
    assert binding[2]["padding"] < 10


@pytest.fixture
def feedback_dialog(monkeypatch):
    dialog = object.__new__(profile_editor.ProfileEditorDialog)
    pending, moves, regions = {}, [], []
    now, next_id, bounds = [0.0], [0], [(0, 0, 580, 500)]

    def after(delay, callback):
        next_id[0] += 1
        pending[next_id[0]] = (now[0] + delay / 1000, callback)
        return next_id[0]

    def advance(seconds):
        target = now[0] + seconds
        while pending:
            timer_id, (due, callback) = min(pending.items(), key=lambda item: item[1][0])
            if due > target:
                break
            pending.pop(timer_id)
            now[0] = due
            callback()
        now[0] = target

    monkeypatch.setattr(profile_editor.time, "monotonic", lambda: now[0])
    dialog._destroyed = False
    dialog._error_label = Widget()
    dialog._form_scroll = SimpleNamespace(_parent_canvas=SimpleNamespace(
        yview_moveto=moves.append,
        bbox=lambda _tag: bounds[0],
        configure=lambda **options: regions.append(options["scrollregion"]),
    ))
    dialog.after = after
    dialog.after_cancel = lambda timer_id: pending.pop(timer_id, None)
    dialog.after_idle = lambda *_args: pytest.fail("one idle cannot guarantee settled text geometry")
    return SimpleNamespace(dialog=dialog, advance=advance, pending=pending,
                           moves=moves, regions=regions, bounds=bounds)


def test_long_api_feedback_waits_for_delayed_wrap_then_reveals_latest_region(feedback_dialog):
    state = feedback_dialog
    state.dialog._show_error("synthetic validation failure\n" * 40)
    assert not state.moves and len(state.pending) == 1
    assert "synthetic validation failure" in state.dialog._error_label.options["text"]

    state.advance(0.08)
    state.bounds[0] = (0, 0, 580, 670)
    state.dialog._schedule_feedback_reveal()
    state.advance(0.09)
    assert not state.moves
    state.bounds[0] = (0, 0, 580, 830)
    state.dialog._schedule_feedback_reveal()
    state.advance(0.121)
    assert state.regions == [(0, 0, 580, 830)]
    assert state.moves == [1.0]
    assert not state.pending

    # Later user-driven resize/scrolling must not keep pinning the form bottom.
    state.dialog._schedule_feedback_reveal()
    state.advance(1)
    assert state.moves == [1.0] and not state.pending


def test_feedback_resize_debounce_has_a_bounded_lifetime(feedback_dialog):
    state = feedback_dialog
    state.dialog._show_status("synthetic status")
    for _ in range(5):
        state.advance(0.09)
        state.dialog._schedule_feedback_reveal()
    assert not state.moves
    state.advance(0.051)
    assert state.moves == [1.0] and not state.pending


def test_new_feedback_supersedes_even_already_queued_old_callback(feedback_dialog):
    state = feedback_dialog
    state.dialog._show_error("old error")
    stale = next(iter(state.pending.values()))[1]
    state.dialog._show_status("new status")
    assert len(state.pending) == 1
    stale()
    assert not state.moves
    state.advance(0.121)
    assert state.moves == [1.0]


def test_feedback_destroy_cancels_pending_scroll_without_accessing_tk(feedback_dialog, monkeypatch):
    state = feedback_dialog
    monkeypatch.setattr(profile_editor.ctk.CTkToplevel, "destroy", lambda _self: None)
    state.dialog._show_error("synthetic failure")
    stale = next(iter(state.pending.values()))[1]
    state.dialog.destroy()
    assert not state.pending
    stale()
    assert not state.moves and not state.regions


def test_clearing_api_feedback_does_not_jump_away_from_edited_field(feedback_dialog):
    state = feedback_dialog
    state.dialog._show_status("")
    assert not state.pending
    state.dialog._show_error("old error")
    stale = next(iter(state.pending.values()))[1]
    state.dialog._show_status("")
    stale()
    state.advance(1)
    assert not state.pending and not state.moves
