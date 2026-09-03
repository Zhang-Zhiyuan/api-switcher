from __future__ import annotations

from core import remote_proxy
from ui.tabs.local_proxy_tab import (
    NEW_SUBSCRIPTION_PROFILE_LABEL,
    LocalProxyTab,
)
from ui.tabs.ssh_tab import (
    NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL,
    SSHTab,
)


class _WidgetStub:
    def __init__(self, value: str = ""):
        self.value = value
        self.options = {}
        self.focused = False

    def get(self) -> str:
        return self.value

    def set(self, value) -> None:
        self.value = str(value)

    def delete(self, _start, _end=None) -> None:
        self.value = ""

    def insert(self, _index, value) -> None:
        self.value = str(value)

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)

    def focus_set(self) -> None:
        self.focused = True


def _isolated_subscription_storage(monkeypatch, path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", path)
    remote_proxy.clear_proxy_subscription_state_cache()


def _local_tab_stub() -> LocalProxyTab:
    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._periodic_update_running = False
    tab._subscription_hot_update_lock_owned = False
    tab._subscription_profile_loading = False
    tab._subscription_profile_edit_id = ""
    tab._subscription_form_snapshot = None
    tab._subscription_profile_options = {}
    tab._subscription_profiles_snapshot = []
    tab._subscription_profile_combo = _WidgetStub(NEW_SUBSCRIPTION_PROFILE_LABEL)
    tab._subscription_name_entry = _WidgetStub()
    tab._subscription_entry = _WidgetStub()
    tab._subscription_profile_save_button = _WidgetStub()
    tab._subscription_profile_reset_button = _WidgetStub()
    tab._subscription_profile_delete_button = _WidgetStub()
    tab._saved_subscription_load_generation = 0
    tab._latency_results = {}
    tab._quality_results = {}
    tab._prefer_quality_sort = False
    tab._subscription_options = {}
    tab._set_status = lambda *_args, **_kwargs: None
    tab._set_cache_status = lambda *_args, **_kwargs: None
    tab._set_subscription_nodes = lambda *_args, **_kwargs: None
    tab._refresh_service_route_profile_options = lambda *_args, **_kwargs: None
    tab._load_subscription_cache_for_state = lambda *_args, **_kwargs: None
    tab.winfo_toplevel = lambda: object()
    return tab


def _ssh_tab_stub() -> SSHTab:
    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._ssh_busy = False
    tab._proxy_periodic_update_running = False
    tab._proxy_subscription_hot_update_lock_owned = False
    tab._proxy_subscription_profile_loading = False
    tab._proxy_subscription_profile_edit_id = ""
    tab._proxy_subscription_form_snapshot = None
    tab._proxy_subscription_profile_options = {}
    tab._proxy_subscription_profile_combo = _WidgetStub(
        NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL
    )
    tab._proxy_subscription_name_entry = _WidgetStub()
    tab._proxy_subscription_entry = _WidgetStub()
    tab._proxy_subscription_profile_save_button = _WidgetStub()
    tab._proxy_subscription_profile_reset_button = _WidgetStub()
    tab._proxy_subscription_profile_delete_button = _WidgetStub()
    tab._proxy_saved_subscription_load_generation = 0
    tab._proxy_latency_results = {}
    tab._proxy_latency_server_count = 0
    tab._proxy_quality_results = {}
    tab._proxy_prefer_quality_sort = False
    tab._proxy_subscription_options = {}
    tab._set_proxy_status = lambda *_args, **_kwargs: None
    tab._set_proxy_cache_status = lambda *_args, **_kwargs: None
    tab._set_proxy_subscription_nodes = lambda *_args, **_kwargs: None
    tab._load_proxy_subscription_cache_for_state = lambda *_args, **_kwargs: None
    tab.winfo_toplevel = lambda: object()
    return tab


def _seed_two_profiles():
    first = remote_proxy.save_proxy_subscription_profile(
        "家宽 A",
        "https://a.example/subscription",
    )
    second = remote_proxy.save_proxy_subscription_profile(
        "机房 B",
        "https://b.example/subscription",
    )
    remote_proxy.set_active_proxy_subscription_profile(first["id"])
    return first, second


def test_subscription_selectors_always_offer_explicit_new_mode(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, _second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()

    local = _local_tab_stub()
    local._refresh_subscription_profile_options(state)
    local._apply_subscription_profile_inputs(state)
    ssh = _ssh_tab_stub()
    ssh._refresh_proxy_subscription_profile_options(state)
    ssh._apply_proxy_subscription_profile_inputs(state)

    assert local._subscription_profile_combo.options["values"][0] == NEW_SUBSCRIPTION_PROFILE_LABEL
    assert NEW_SUBSCRIPTION_PROFILE_LABEL not in local._subscription_profile_options
    assert local._subscription_profile_edit_id == first["id"]
    assert (
        ssh._proxy_subscription_profile_combo.options["values"][0]
        == NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL
    )
    assert NEW_PROXY_SUBSCRIPTION_PROFILE_LABEL not in ssh._proxy_subscription_profile_options
    assert ssh._proxy_subscription_profile_edit_id == first["id"]


def test_local_existing_profile_name_and_url_are_updated_in_place(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _local_tab_stub()
    tab._refresh_subscription_profile_options(state)
    tab._apply_subscription_profile_inputs(state)
    tab._subscription_name_entry.value = "家宽 A 新名称"
    tab._subscription_entry.value = "https://new-a.example/subscription"

    updated = tab._save_subscription_profile(show_message=False)
    profiles = remote_proxy.list_proxy_subscription_profiles()

    assert updated["id"] == first["id"]
    assert updated["name"] == "家宽 A 新名称"
    assert updated["url"] == "https://new-a.example/subscription"
    assert len(profiles) == 2
    assert any(item["id"] == second["id"] for item in profiles)


def test_ssh_existing_profile_name_and_url_are_updated_in_place(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _ssh_tab_stub()
    tab._refresh_proxy_subscription_profile_options(state)
    tab._apply_proxy_subscription_profile_inputs(state)
    tab._proxy_subscription_name_entry.value = "家宽 A 新名称"
    tab._proxy_subscription_entry.value = "https://new-a.example/subscription"

    updated = tab._save_proxy_subscription_profile(show_message=False)
    profiles = remote_proxy.list_proxy_subscription_profiles()

    assert updated["id"] == first["id"]
    assert updated["name"] == "家宽 A 新名称"
    assert updated["url"] == "https://new-a.example/subscription"
    assert len(profiles) == 2
    assert any(item["id"] == second["id"] for item in profiles)


def test_local_new_mode_clears_form_without_switching_persisted_profile(
    monkeypatch,
    tmp_path,
):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, _second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _local_tab_stub()
    tab._refresh_subscription_profile_options(state)
    tab._apply_subscription_profile_inputs(state)

    tab._on_subscription_profile_selected(NEW_SUBSCRIPTION_PROFILE_LABEL)

    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == first["id"]
    assert tab._subscription_profile_edit_id == ""
    assert tab._subscription_name_entry.get() == ""
    assert tab._subscription_entry.get() == ""
    assert tab._subscription_profile_save_button.options["text"] == "新增订阅"
    assert tab._subscription_profile_delete_button.options["state"] == "disabled"


def test_local_new_mode_creates_profile_without_overwriting_previous_one(
    monkeypatch,
    tmp_path,
):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, _second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _local_tab_stub()
    tab._refresh_subscription_profile_options(state)
    tab._apply_subscription_profile_inputs(state)
    tab._on_subscription_profile_selected(NEW_SUBSCRIPTION_PROFILE_LABEL)
    tab._subscription_name_entry.value = "备用 C"
    tab._subscription_entry.value = "https://c.example/subscription"

    created = tab._save_subscription_profile(show_message=False)
    profiles = remote_proxy.list_proxy_subscription_profiles()

    assert created["id"] != first["id"]
    assert len(profiles) == 3
    assert next(item for item in profiles if item["id"] == first["id"])["name"] == "家宽 A"


def test_dirty_local_profile_requires_confirmation_before_switch(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _local_tab_stub()
    tab._refresh_subscription_profile_options(state)
    tab._apply_subscription_profile_inputs(state)
    source_label = tab._subscription_profile_combo.get()
    target_label = next(
        label
        for label, profile_id in tab._subscription_profile_options.items()
        if profile_id == second["id"]
    )
    tab._subscription_name_entry.value = "尚未保存"
    tab._subscription_profile_combo.set(target_label)
    dialogs = []
    monkeypatch.setattr(
        "ui.tabs.local_proxy_tab.ConfirmDialog",
        lambda *_args, **kwargs: dialogs.append(kwargs),
    )

    tab._on_subscription_profile_selected(target_label)

    assert tab._subscription_profile_combo.get() == source_label
    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == first["id"]
    assert len(dialogs) == 1

    dialogs[0]["on_confirm"]()

    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == second["id"]
    assert tab._subscription_name_entry.get() == "机房 B"


def test_dirty_ssh_profile_requires_confirmation_before_switch(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    first, second = _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _ssh_tab_stub()
    tab._refresh_proxy_subscription_profile_options(state)
    tab._apply_proxy_subscription_profile_inputs(state)
    source_label = tab._proxy_subscription_profile_combo.get()
    target_label = next(
        label
        for label, profile_id in tab._proxy_subscription_profile_options.items()
        if profile_id == second["id"]
    )
    tab._proxy_subscription_name_entry.value = "尚未保存"
    tab._proxy_subscription_profile_combo.set(target_label)
    dialogs = []
    monkeypatch.setattr(
        "ui.tabs.ssh_tab.ConfirmDialog",
        lambda *_args, **kwargs: dialogs.append(kwargs),
    )

    tab._on_proxy_subscription_profile_selected(target_label)

    assert tab._proxy_subscription_profile_combo.get() == source_label
    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == first["id"]
    assert len(dialogs) == 1

    dialogs[0]["on_confirm"]()

    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == second["id"]
    assert tab._proxy_subscription_name_entry.get() == "机房 B"


def test_local_undo_restores_saved_name_and_url(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()
    tab = _local_tab_stub()
    tab._refresh_subscription_profile_options(state)
    tab._apply_subscription_profile_inputs(state)
    tab._subscription_name_entry.value = "临时名称"
    tab._subscription_entry.value = "https://temporary.example/subscription"

    result = tab._reset_subscription_profile_form()

    assert result == "break"
    assert tab._subscription_name_entry.get() == "家宽 A"
    assert tab._subscription_entry.get() == "https://a.example/subscription"
    assert tab._subscription_profile_form_dirty() is False


def test_automatic_refresh_never_saves_or_overwrites_new_draft():
    local = _local_tab_stub()
    local._subscription_name_entry.value = "草稿"
    local._subscription_entry.value = "https://draft.example/subscription"
    statuses = []
    local._set_status = lambda message, severity="info": statuses.append((message, severity))
    local._save_subscription_profile = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("automatic refresh must not save a draft")
    )

    local._fetch_subscription(auto=True, show_message=False)

    assert statuses[-1][1] == "warning"
    assert "未保存修改" in statuses[-1][0]


def test_ssh_form_controls_make_edit_state_explicit():
    tab = _ssh_tab_stub()
    tab._proxy_subscription_entry.value = "https://new.example/subscription"
    tab._update_proxy_subscription_profile_form_controls()

    assert tab._proxy_subscription_profile_save_button.options == {
        "text": "新增订阅",
        "state": "normal",
    }
    assert tab._proxy_subscription_profile_reset_button.options["text"] == "清空"
    assert tab._proxy_subscription_profile_delete_button.options["state"] == "disabled"

    tab._proxy_subscription_profile_edit_id = "profile-a"
    tab._proxy_subscription_form_snapshot = (
        "profile-a",
        "旧名称",
        "https://new.example/subscription",
    )
    tab._proxy_subscription_name_entry.value = "新名称"
    tab._update_proxy_subscription_profile_form_controls()

    assert tab._proxy_subscription_profile_save_button.options["text"] == "保存修改"
    assert tab._proxy_subscription_profile_reset_button.options["text"] == "撤销"
    assert tab._proxy_subscription_profile_delete_button.options["state"] == "normal"


def test_existing_profile_actions_start_in_safe_clean_state(monkeypatch, tmp_path):
    _isolated_subscription_storage(monkeypatch, tmp_path)
    _seed_two_profiles()
    state = remote_proxy.load_proxy_subscription_state()

    local = _local_tab_stub()
    local._refresh_subscription_profile_options(state)
    local._apply_subscription_profile_inputs(state)
    ssh = _ssh_tab_stub()
    ssh._refresh_proxy_subscription_profile_options(state)
    ssh._apply_proxy_subscription_profile_inputs(state)

    assert local._subscription_profile_save_button.options["state"] == "disabled"
    assert local._subscription_profile_reset_button.options["state"] == "disabled"
    assert local._subscription_profile_delete_button.options["state"] == "normal"
    assert ssh._proxy_subscription_profile_save_button.options["state"] == "disabled"
    assert ssh._proxy_subscription_profile_reset_button.options["state"] == "disabled"
    assert ssh._proxy_subscription_profile_delete_button.options["state"] == "normal"
