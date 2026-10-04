"""SSH target selection must not reread profiles or rebuild server widgets."""
from types import SimpleNamespace

import pytest

from models.profile import SSHProfile
from ui.tabs import ssh_tab
from ui.tabs.ssh_tab import SSHTab


class Variable:
    def __init__(self, *, master=None, value=False):
        self.value = value
        self.writes = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        self.writes.append(value)


class Widget:
    def __init__(self, *_args, **kwargs):
        self.options = kwargs
        self.children = []
        self.destroyed = False

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def pack(self, **_kwargs):
        pass

    def grid(self, **_kwargs):
        pass

    def grid_columnconfigure(self, *_args, **_kwargs):
        pass

    def winfo_children(self):
        return self.children[:]

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def tab(monkeypatch):
    result = object.__new__(SSHTab)
    result._server_selection_vars = {}
    result._selected_server_names = set()
    result._cards_frame = Widget()
    result._batch_target_label = Widget()
    result._batch_select_all_button = Widget(state="normal")
    result._batch_clear_button = Widget(state="normal")
    result._set_server_profile_cache([SSHProfile("A", "a.invalid"), SSHProfile("B", "b.invalid")])
    result.context_updates = []
    result.remote_resets = []
    result.provider_updates = []
    result._update_target_context_ui = result.context_updates.append
    result._reset_remote_pull_options = result.remote_resets.append
    result._on_remote_auto_provider_change = lambda: result.provider_updates.append(True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("selection must not refresh, read profiles, or start I/O")

    result.refresh = forbidden
    monkeypatch.setattr(ssh_tab, "profile_manager", SimpleNamespace(list_ssh_profiles=forbidden))
    return result


def test_all_and_clear_update_existing_variables_without_refresh(tab):
    variables = {name: Variable() for name in ("A", "B")}
    tab._server_selection_vars.update(variables)
    tab._select_all_batch_servers()
    assert tab._selected_server_names == {"A", "B"}
    assert all(variable.get() for variable in variables.values())
    assert tab.context_updates[-1] == ["A", "B"]
    tab._clear_batch_servers()
    assert tab._selected_server_names == set()
    assert all(not variable.get() for variable in variables.values())
    assert tab.context_updates[-1] == []
    assert tab._server_selection_vars == variables
    assert len(tab.remote_resets) == len(tab.provider_updates) == 2


def test_unchanged_selection_does_not_write_tk_variables(tab):
    tab._server_selection_vars = {"A": Variable(value=True), "B": Variable(value=True)}
    tab._selected_server_names = {"A", "B"}
    tab._select_all_batch_servers()
    assert all(not variable.writes for variable in tab._server_selection_vars.values())


@pytest.mark.parametrize("select", [True, False])
def test_late_rendered_cards_use_latest_bulk_selection(tab, monkeypatch, select):
    for name in ("CTkFrame", "CTkLabel", "CTkButton", "CTkCheckBox"):
        monkeypatch.setattr(ssh_tab.ctk, name, Widget)
    monkeypatch.setattr(ssh_tab.ctk, "BooleanVar", Variable)
    monkeypatch.setattr(ssh_tab, "bind_wraplength", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ssh_tab, "font", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ssh_tab, "button_style", lambda *_args, **_kwargs: {})
    first, second = tab._server_profiles
    tab._selected_server_names = {"A", "B"} if not select else set()
    tab._render_server_card({"profile": first})
    first_variable = tab._server_selection_vars["A"]
    (tab._select_all_batch_servers if select else tab._clear_batch_servers)()
    tab._render_server_card({"profile": second})
    assert tab._server_selection_vars["A"] is first_variable
    assert all(variable.get() is select for variable in tab._server_selection_vars.values())
    assert tab._selected_sync_server_names() == (["A", "B"] if select else [])


def test_rebuilding_cards_discards_old_variables_but_retains_selection(tab):
    old_variable = Variable(value=True)
    tab._server_selection_vars["A"] = old_variable
    tab._selected_server_names = {"A"}
    card = Widget()
    tab._cards_frame.children = [card]
    tab._clear_server_cards()
    assert card.destroyed
    assert tab._server_selection_vars == {}
    assert tab._selected_server_names == {"A"}
    tab._clear_batch_servers()
    assert not old_variable.writes


def test_deleted_server_is_removed_from_batch_and_checkbox_state(tab):
    removed = Variable(value=True)
    retained = Variable(value=True)
    tab._selected_server_names = {"A", "B"}
    tab._server_selection_vars = {"A": removed, "B": retained}
    tab._set_server_profile_cache([SSHProfile("B", "b.invalid")])
    tab._update_batch_target_label()
    assert tab._selected_server_names == {"B"}
    assert tab._selected_sync_server_names() == ["B"]
    assert removed.get() is False and retained.get() is True
    tab._clear_server_cards()
    assert "A" not in tab._server_selection_vars


def test_empty_server_list_disables_bulk_actions_and_clears_invalid_selection(tab):
    tab._set_server_profile_cache([])
    tab._selected_server_names = {"removed"}
    tab._server_selection_vars = {"removed": Variable(value=True)}
    tab._update_batch_target_label()
    assert tab._selected_server_names == set()
    assert not tab._server_selection_vars["removed"].get()
    assert tab._batch_select_all_button.options["state"] == "disabled"
    assert tab._batch_clear_button.options["state"] == "disabled"
    assert tab.context_updates[-1] == []


def test_single_checkbox_selection_keeps_other_variables_and_batch_order(tab):
    tab._server_selection_vars = {"A": Variable(), "B": Variable()}
    tab._toggle_batch_server("B", True)
    assert tab._server_selection_vars["B"].get()
    assert not tab._server_selection_vars["A"].get()
    tab._toggle_batch_server("A", True)
    assert tab._selected_sync_server_names() == ["A", "B"]
    tab._toggle_batch_server("deleted", True)
    assert tab._selected_server_names == {"A", "B"}
