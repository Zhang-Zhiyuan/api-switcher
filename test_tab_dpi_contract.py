"""DPI contracts using CTk's real scaling wrapper with native Tk calls replaced."""
from types import SimpleNamespace
import tkinter

import pytest
from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass

from ui.app import App
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture
def scaled_grid(monkeypatch):
    # Do not construct a Tk object/root. The Python-only CTk grid wrapper is
    # exercised while all inherited native geometry calls are replaced.
    def grid(self, **kwargs):
        self.native_grid.update(kwargs)

    monkeypatch.setattr(tkinter.Grid, "grid", grid)
    monkeypatch.setattr(tkinter.Grid, "grid_info", lambda self: dict(self.native_grid))
    monkeypatch.setattr(tkinter.Grid, "grid_forget", lambda self: setattr(self, "native_grid", {}))
    widget = object.__new__(CTkBaseClass)
    widget._CTkScalingBaseClass__scaling_type = "widget"
    widget._CTkScalingBaseClass__widget_scaling = 1.0
    widget.native_grid = {}
    frame = SimpleNamespace(grid_slaves=lambda: [widget], grid_columnconfigure=lambda *_a, **_k: None)
    return widget, frame


@pytest.mark.parametrize("capture_scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("restore_scale", [1.0, 1.5, 2.0])
def test_ssh_grid_capture_restores_logical_padding_once(scaled_grid, capture_scale, restore_scale):
    widget, frame = scaled_grid
    widget._CTkScalingBaseClass__widget_scaling = capture_scale
    widget.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 12), pady=4)
    snapshot = SSHTab._capture_grid_layout(frame)
    # A compact layout overwrites CTk's replay cache before the original wide
    # layout is restored, so the capture must own an unscaled copy.
    widget.grid(row=0, column=0, padx=0, pady=(6, 0))
    widget._CTkScalingBaseClass__widget_scaling = restore_scale
    SSHTab._restore_grid_layout(frame, snapshot, 4)
    assert widget.native_grid["padx"] == (8 * restore_scale, 12 * restore_scale)
    assert widget.native_grid["pady"] == 4 * restore_scale
    assert widget._last_geometry_manager_call["kwargs"]["padx"] == (8, 12)
    assert widget.native_grid["row"] == 2 and widget.native_grid["columnspan"] == 2


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("tab_type", [SSHTab, LocalProxyTab])
def test_tab_breakpoints_use_widget_logical_width(tab_type, scale):
    tab = SimpleNamespace(winfo_width=lambda: round(800 * scale), _get_widget_scaling=lambda: scale)
    assert tab_type._logical_layout_width(tab) == 800


def test_main_breakpoints_use_widget_scale_not_window_scale():
    window = SimpleNamespace(winfo_width=lambda: 1200, _get_window_scaling=lambda: 1.0,
                             _shell=SimpleNamespace(_get_widget_scaling=lambda: 1.5))
    assert App._logical_main_width(window) == 800


@pytest.mark.parametrize("missing_cache", [True, False])
def test_ctk_padding_falls_back_to_unscaling_if_cache_missing_or_partial(scaled_grid, missing_cache):
    widget, frame = scaled_grid
    widget._CTkScalingBaseClass__widget_scaling = 1.5
    widget.grid(row=0, column=0, padx=(8, 12), pady=4, ipadx=7)
    if missing_cache:
        widget._last_geometry_manager_call = None
    else:
        widget.grid(row=2)
    snapshot = SSHTab._capture_grid_layout(frame)
    widget._CTkScalingBaseClass__widget_scaling = 2
    SSHTab._restore_grid_layout(frame, snapshot, 1)
    assert widget.native_grid["padx"] == (16, 24)
    assert widget.native_grid["pady"] == 8
    assert widget.native_grid["ipadx"] == 7  # CTk does not scale Tk's internal padding.


class GeometryWidget:
    def __init__(self, *, width=900, scale=1.5, children=()):
        self.width, self.scale = width, scale
        self.children = tuple(children)
        self.options = {}
        self.grid_calls = []
        self.columns = {}
        self.callback = None

    def winfo_width(self):
        return self.width

    def _get_widget_scaling(self):
        return self.scale

    def grid(self, **kwargs):
        self.options.update(kwargs)
        self.grid_calls.append(dict(kwargs))

    def grid_info(self):
        return dict(self.options)

    def grid_forget(self):
        self.options.clear()

    def pack_forget(self):
        pass

    def pack_slaves(self):
        return self.children

    def grid_columnconfigure(self, column, **kwargs):
        self.columns[column] = kwargs

    def bind(self, _sequence, callback, **_kwargs):
        self.callback = callback

    def resize(self, width, scale):
        self.width, self.scale = width, scale
        self.callback(SimpleNamespace(width=width, widget=self))


def test_native_tk_padding_is_not_unscaled():
    widget = GeometryWidget(scale=2)
    widget.grid(row=1, column=0, padx=(12, 18), pady=6)
    frame = SimpleNamespace(grid_slaves=lambda: [widget], grid_columnconfigure=lambda *_a, **_k: None)
    SSHTab._restore_grid_layout(frame, SSHTab._capture_grid_layout(frame), 1)
    assert widget.options["padx"] == (12, 18) and widget.options["pady"] == 6


def test_header_actions_reflow_at_widget_scale_without_recreating_widgets():
    header, title, actions = GeometryWidget(), GeometryWidget(), GeometryWidget()
    SSHTab._bind_compact_header(header, title, actions)
    assert actions.options["row"] == 0 and actions.options["column"] == 1
    header.resize(720, 2.25)
    assert actions.options["row"] == 1 and actions.options["column"] == 0
    assert actions.options["sticky"] == "w"
    count = len(actions.grid_calls)
    header.resize(710, 2.25)
    assert len(actions.grid_calls) == count
    header.resize(1200, 1.5)
    assert actions.options["row"] == 0 and actions.options["column"] == 1


def test_header_repeated_dpi_roundtrips_restore_all_geometry_options():
    header, title, actions = GeometryWidget(), GeometryWidget(), GeometryWidget()
    SSHTab._bind_compact_header(header, title, actions)
    original = (dict(title.options), dict(actions.options), dict(header.columns))
    for _ in range(3):
        for logical_width, scale in ((300, 2.25), (559, 1.5), (560, 2.5), (600, 1.5)):
            header.resize(logical_width * scale, scale)
        assert (title.options, actions.options, header.columns) == original


@pytest.mark.parametrize("columns", [1, 2, 3])
def test_action_group_unpacks_every_sibling_before_first_grid(columns):
    managers = ["pack", "pack", "pack"]

    class Button(GeometryWidget):
        def __init__(self, index):
            super().__init__()
            self.index = index

        def pack_forget(self):
            if managers[self.index] == "pack":
                managers[self.index] = ""

        def grid(self, **kwargs):
            assert "pack" not in managers, "Tk cannot grid while a sibling is still packed"
            managers[self.index] = "grid"
            super().grid(**kwargs)

    buttons = tuple(Button(index) for index in range(3))
    frame = GeometryWidget(children=buttons)
    SSHTab._grid_action_buttons(frame, buttons, columns)
    assert managers == ["grid"] * 3
    SSHTab._grid_action_buttons(frame, buttons, 1)
    assert managers == ["grid"] * 3


def test_server_card_info_and_buttons_wrap_and_restore_at_high_dpi():
    row = GeometryWidget(width=1440, scale=1.5)
    target, status, text = GeometryWidget(), GeometryWidget(), GeometryWidget()
    buttons = tuple(GeometryWidget() for _ in range(3))
    actions = GeometryWidget(children=buttons)
    SSHTab._bind_server_card_layout(row, target, status, text, actions)
    assert text.options["column"] == 2 and actions.options["column"] == 3
    row.resize(620, 2.25)
    assert text.options["row"] == 1 and text.options["columnspan"] == 4
    assert actions.options["row"] == 2 and actions.options["sticky"] == "ew"
    assert [button.options["column"] for button in buttons] == [0, 1, 2]
    row.resize(330, 2.25)
    assert [button.options["row"] for button in buttons] == [0, 1, 2]
    row.resize(1440, 1.5)
    assert actions.options["row"] == 0 and actions.options["column"] == 3
    assert target.options["rowspan"] == 2
    assert text.options["columnspan"] == 1


def test_server_card_roundtrips_clear_old_spans_padding_and_button_rows():
    row = GeometryWidget(width=1440, scale=1.5)
    target, status, text = GeometryWidget(), GeometryWidget(), GeometryWidget()
    buttons = tuple(GeometryWidget() for _ in range(3))
    actions = GeometryWidget(children=buttons)
    SSHTab._bind_server_card_layout(row, target, status, text, actions)
    widgets = (target, status, text, actions, *buttons)
    original = [dict(widget.options) for widget in widgets]
    original_columns = (dict(row.columns), dict(actions.columns))
    for _ in range(3):
        for logical_width, scale in ((599, 2.25), (239, 1.5), (159, 2.5), (240, 2), (600, 1.5)):
            row.resize(logical_width * scale, scale)
        assert [widget.options for widget in widgets] == original
        assert (row.columns, actions.columns) == original_columns


def test_ultranarrow_sync_form_unstacks_label_input_pairs_and_action_rows():
    names = (
        "_target_summary_label", "_target_hint_label", "_push_content_label", "_sync_kind_combo",
        "_profile_combo", "_push_button_frame", "_remote_pull_label", "_remote_pull_type_combo",
        "_remote_pull_combo", "_remote_pull_button_frame", "_remote_pull_hint", "_codex_wire_api_label",
        "_codex_wire_api_combo", "_codex_wire_api_hint", "_clear_api_label", "_clear_api_combo",
        "_clear_api_hint", "_clear_api_button", "_git_login_label", "_git_login_status_label",
        "_git_button_frame", "_sync_status_label",
    )
    tab = SimpleNamespace(**{name: GeometryWidget() for name in names})
    tab.width = 320
    tab._logical_layout_width = lambda: tab.width
    tab._responsive_state = None
    tab._sync_controls = GeometryWidget()
    tab._apply_deployment_responsive_layout = lambda **_kwargs: None
    tab._grid_action_buttons = SSHTab._grid_action_buttons
    buttons = (GeometryWidget(), GeometryWidget())
    tab._sync_action_groups = ((tab._push_button_frame, buttons, 2),)
    SSHTab._apply_responsive_layout(tab)
    assert [getattr(tab, name).options["row"] for name in names] == list(range(len(names)))
    assert all(getattr(tab, name).options["column"] == 0 for name in names)
    assert [button.options["row"] for button in buttons] == [0, 1]
    tab.width = 700
    SSHTab._apply_responsive_layout(tab)
    assert tab._sync_kind_combo.options["column"] == 1
    assert [button.options["column"] for button in buttons] == [0, 1]
    tab.width = 1200
    SSHTab._apply_responsive_layout(tab)
    assert tab._push_button_frame.options["column"] == 3
    wide = [dict(getattr(tab, name).options) for name in names]
    wide_columns = dict(tab._sync_controls.columns)
    for _ in range(3):
        tab.width = 320
        SSHTab._apply_responsive_layout(tab)
        tab.width = 700
        SSHTab._apply_responsive_layout(tab)
        assert tab._sync_kind_combo.options.get("columnspan", 1) == 1
        assert tab._sync_kind_combo.options["padx"] == (8, 0)
        assert tab._push_button_frame.options["row"] == 4
        tab.width = 1200
        SSHTab._apply_responsive_layout(tab)
        assert [getattr(tab, name).options for name in names] == wide
        assert tab._sync_controls.columns == wide_columns
        assert [button.options["row"] for button in buttons] == [0, 0]
