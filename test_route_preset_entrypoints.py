"""Pure entrypoint contracts: no Tk construction, accounts, proxy changes or SSH."""
import copy
from types import MethodType, SimpleNamespace

import pytest

from core import local_proxy, proxy_routing
from ui.dialogs import service_routes_dialog
from ui.tabs import ssh_tab
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab
from ui.widgets.service_route_overview import ServiceRouteOverview


class Control:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


def forbidden(*_args, **_kwargs):
    pytest.fail("Opening a preset must not reload an existing draft or invoke a live operation")


@pytest.fixture(params=["local", "ssh"])
def entrypoint(request, monkeypatch):
    kind = request.param
    events, operations, dialogs, toasts = [], [], [], []
    master = object()
    tab = SimpleNamespace(_busy=False, _proxy_busy=False, _ssh_busy=False,
                          winfo_toplevel=lambda: master)
    tab._set_routing_status = tab._set_proxy_status = lambda *args: events.append(args)
    tab._load_proxy_preferences_ui = lambda: events.append("preferences")
    tab._refresh_subscription_profile_options = tab._refresh_proxy_subscription_profile_options = (
        lambda **kwargs: events.append(("subscriptions", kwargs)))
    selected = ["合成 SSH A", "合成 SSH B"]
    tab._selected_sync_server_names = lambda: selected[:]
    tab._require_selected_servers = MethodType(SSHTab._require_selected_servers, tab)
    monkeypatch.setattr(ssh_tab, "show_toast", lambda *args, **kwargs: toasts.append((args, kwargs)))
    attribute = "_service_routes_dialog" if kind == "local" else "_proxy_service_routes_dialog"
    scopes = ["Win11 本机（含共享 WSL）"] if kind == "local" else selected[:]
    setattr(tab, attribute, None)
    manage_method = LocalProxyTab._open_service_routes if kind == "local" else SSHTab._open_proxy_service_routes
    preset_method = LocalProxyTab._open_route_preset if kind == "local" else SSHTab._open_proxy_route_preset
    manage = MethodType(manage_method, tab)
    setattr(tab, "_open_service_routes" if kind == "local" else "_open_proxy_service_routes", manage)

    def record(name):
        def run(*args, **kwargs):
            operations.append((name, args, kwargs))
            return {"synthetic": name}
        return run

    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", record("local-load"))
    monkeypatch.setattr(local_proxy, "set_local_proxy_service_routes_and_apply", record("local-apply"))
    monkeypatch.setattr(proxy_routing, "load_ssh_route_editor_preferences", record("ssh-load"))
    monkeypatch.setattr(proxy_routing, "recover_ssh_routes", record("ssh-recover"))
    monkeypatch.setattr(proxy_routing, "apply_ssh_routes", record("ssh-apply"))

    class Dialog:
        def __init__(self, parent, **kwargs):
            self.master, self.kwargs = parent, kwargs
            self._scopes = list(kwargs["scopes"])
            self._scope = self._scopes[-1]
            self._drafts = {scope: {"unsaved": ["keep"]} for scope in self._scopes}
            self._status = Control()
            self.exists = True
            self.lifts = self.focuses = self.preset_requests = 0
            self._start_load = self._reload_catalog = forbidden
            dialogs.append(self)

        def winfo_exists(self):
            return self.exists

        def lift(self):
            self.lifts += 1

        def focus(self):
            self.focuses += 1

        def show_preset(self):
            self.preset_requests += 1

    monkeypatch.setattr(service_routes_dialog, "ServiceRoutesDialog", Dialog)
    return SimpleNamespace(kind=kind, tab=tab, master=master, attribute=attribute, scopes=scopes,
                           selected=selected, manage=manage, preset=MethodType(preset_method, tab),
                           events=events, operations=operations, dialogs=dialogs, toasts=toasts)


@pytest.mark.parametrize("preset", [False, True])
def test_new_route_window_passes_explicit_mode_without_doing_io(entrypoint, preset):
    form = entrypoint
    (form.preset if preset else form.manage)()
    assert len(form.dialogs) == 1
    dialog = form.dialogs[0]
    assert dialog.master is form.master
    assert dialog.kwargs["scopes"] == form.scopes
    assert dialog.kwargs["initial_preset"] is preset
    assert dialog.preset_requests == 0  # Async load completion, not the tab, opens the child.
    assert form.operations == []
    assert form.events == []


def test_callbacks_keep_original_device_scope_and_expected_snapshot(entrypoint):
    form = entrypoint
    form.preset()
    callbacks = form.dialogs[0].kwargs
    scope = form.scopes[-1]
    before, draft = {"synthetic": "before"}, {"synthetic": "draft"}
    callbacks["load_preferences"](scope)
    callbacks["apply_preferences"](scope, draft, before)
    if form.kind == "local":
        assert "recover_preferences" not in callbacks
        assert form.operations == [("local-load", (), {}), ("local-apply", (draft,), {"expected": before})]
    else:
        callbacks["recover_preferences"](scope)
        assert form.operations == [("ssh-load", (scope,), {}),
                                   ("ssh-apply", (scope, draft), {"expected": before}),
                                   ("ssh-recover", (scope,), {})]
    callbacks["on_tags_saved"]()
    assert form.events[-1] == ("subscriptions", {"preserve_editor": True})


@pytest.mark.parametrize("preset", [False, True])
def test_existing_window_reuses_original_unsaved_drafts_without_reloading(entrypoint, preset):
    form = entrypoint
    form.manage()
    dialog = form.dialogs[0]
    before = copy.deepcopy(dialog._drafts)
    drafts, scope = dialog._drafts, dialog._scope
    for _ in range(3):
        (form.preset if preset else form.manage)()
    assert form.dialogs == [dialog]
    assert getattr(form.tab, form.attribute) is dialog
    assert dialog._drafts is drafts and dialog._drafts == before and dialog._scope == scope
    assert dialog.lifts == dialog.focuses == 3
    assert dialog.preset_requests == (3 if preset else 0)
    assert not form.operations


@pytest.mark.parametrize("preset", [False, True])
def test_closed_window_can_be_replaced_without_reusing_stale_context(entrypoint, preset):
    form = entrypoint
    form.manage()
    previous = form.dialogs[0]
    previous.exists = False
    (form.preset if preset else form.manage)()
    assert len(form.dialogs) == 2
    assert getattr(form.tab, form.attribute) is form.dialogs[-1]
    assert form.dialogs[-1].kwargs["initial_preset"] is preset
    assert previous.lifts == previous.focuses == previous.preset_requests == 0
    assert not form.operations


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("preset", [False, True])
def test_busy_guards_do_not_create_or_touch_an_existing_window(entrypoint, existing, preset):
    form = entrypoint
    if existing:
        form.manage()
    for busy_attribute in (("_busy",) if form.kind == "local" else ("_proxy_busy", "_ssh_busy")):
        setattr(form.tab, busy_attribute, True)
        (form.preset if preset else form.manage)()
        setattr(form.tab, busy_attribute, False)
        assert len(form.dialogs) == int(existing)
        assert form.events[-1][1] == "warning" and "正在进行" in form.events[-1][0]
        if existing:
            assert form.dialogs[0].lifts == form.dialogs[0].focuses == form.dialogs[0].preset_requests == 0
    assert not form.operations


@pytest.mark.parametrize("entrypoint", ["local"], indirect=True)
def test_local_service_specific_manage_retains_filter_without_forcing_preset(entrypoint):
    form = entrypoint
    form.manage("youtube")
    assert form.dialogs[0].kwargs["initial_service"] == "youtube"
    assert form.dialogs[0].kwargs["initial_preset"] is False


@pytest.mark.parametrize("preset", [False, True])
@pytest.mark.parametrize("entrypoint", ["ssh"], indirect=True)
def test_ssh_no_selection_prompts_without_creating_routes(entrypoint, preset):
    form = entrypoint
    form.selected.clear()
    (form.preset if preset else form.manage)()
    assert not form.dialogs and not form.operations
    assert form.events[-1][1] == "warning" and "勾选" in form.events[-1][0]
    assert len(form.toasts) == 1


@pytest.mark.parametrize("preset", [False, True])
@pytest.mark.parametrize("selection", [["合成 SSH B"], ["合成 SSH C"], ["合成 SSH A", "合成 SSH C"]])
@pytest.mark.parametrize("entrypoint", ["ssh"], indirect=True)
def test_ssh_changed_scope_set_never_retargets_or_discards_old_drafts(entrypoint, preset, selection):
    form = entrypoint
    form.manage()
    dialog = form.dialogs[0]
    before = copy.deepcopy((dialog._scopes, dialog._scope, dialog._drafts))
    form.selected[:] = selection
    (form.preset if preset else form.manage)()
    assert form.dialogs == [dialog] and dialog.preset_requests == 0
    assert (dialog._scopes, dialog._scope, dialog._drafts) == before
    assert dialog.lifts == dialog.focuses == 1
    assert form.events[-1][1] == "warning" and "原来选择的服务器" in form.events[-1][0]
    assert dialog._status.options["text"] == form.events[-1][0]
    assert not form.operations


@pytest.mark.parametrize("entrypoint", ["ssh"], indirect=True)
def test_ssh_same_scope_set_in_different_order_preserves_current_scope(entrypoint):
    form = entrypoint
    form.manage()
    dialog = form.dialogs[0]
    before = copy.deepcopy((dialog._scopes, dialog._scope, dialog._drafts))
    form.selected.reverse()
    form.preset()
    assert form.dialogs == [dialog] and dialog.preset_requests == 1
    assert (dialog._scopes, dialog._scope, dialog._drafts) == before
    assert not form.events and not form.operations


def test_local_busy_state_disables_the_new_overview_preset_and_its_callback():
    calls = []
    overview = SimpleNamespace(_enabled=True, _preset=Control(), _manage=Control(), _inspect=Control(),
                               _rows={}, _preset_command=lambda: calls.append("preset"))
    overview.set_enabled = MethodType(ServiceRouteOverview.set_enabled, overview)
    names = ("fetch_button", "latency_button", "quality_button", "use_node_button", "hot_update_node_button",
             "quality_settings_button", "ping0_button", "load_file_button", "start_button", "inspect_button",
             "test_button", "stop_button", "apply_routing_button", "subscription_profile_save_button",
             "subscription_profile_delete_button", "quality_cancel_button", "auto_refresh_check",
             "periodic_update_check", "subscription_picker", "subscription_profile_combo", "subscription_name_entry",
             "subscription_entry")
    tab = SimpleNamespace(**{"_" + name: None for name in names})
    tab._route_overview = overview
    tab._update_subscription_profile_form_controls = lambda: None
    for busy, expected in ((True, "disabled"), (False, "normal"), (True, "disabled")):
        LocalProxyTab._set_busy(tab, busy)
        assert overview._preset.options["state"] == expected
        ServiceRouteOverview._open_preset(overview)
    assert calls == ["preset"]


def test_ssh_proxy_busy_state_disables_the_new_preset_button_without_needing_nodes():
    names = ("fetch_button", "latency_button", "quality_button", "use_node_button", "hot_update_button",
             "quality_settings_button", "ping0_button", "load_file_button", "deploy_button", "inspect_button",
             "remote_test_button", "remote_cleanup_button", "subscription_profile_save_button",
             "subscription_profile_delete_button", "quality_cancel_button", "auto_refresh_check",
             "periodic_update_check", "subscription_picker", "subscription_profile_combo", "subscription_name_entry",
             "subscription_entry")
    tab = SimpleNamespace(**{"_proxy_" + name: None for name in names})
    tab._proxy_route_preset_button = Control()
    tab._proxy_subscription_options = {}
    tab._update_proxy_subscription_profile_form_controls = lambda: None
    for busy, expected in ((True, "disabled"), (False, "normal"), (True, "disabled")):
        SSHTab._set_proxy_busy(tab, busy)
        assert tab._proxy_route_preset_button.options["state"] == expected
