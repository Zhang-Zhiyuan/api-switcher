"""Persist explicit follow-default intent without applying inferred live routes."""

import copy
import json
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_routing, remote_proxy
from core.subscription_routing_policy import suggest_tagged_routes


@pytest.fixture
def isolated_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", tmp_path / "local-preferences.json")
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    local_proxy.clear_local_proxy_preferences_cache()
    yield tmp_path
    local_proxy.clear_local_proxy_preferences_cache()


def test_legacy_route_snapshot_has_empty_modes_and_copies_new_metadata():
    assert proxy_routing.normalize_routes({})["service_route_modes"] == {}
    assert proxy_routing.route_snapshot({})["service_route_modes"] == {}
    source = {"service_route_modes": {"youtube": "default"}}
    snapshot = proxy_routing.route_snapshot(source)
    snapshot["service_route_modes"]["claude"] = "default"
    assert source == {"service_route_modes": {"youtube": "default"}}


@pytest.mark.parametrize("raw", [None, [], "default", True, 0])
def test_route_modes_reject_non_object_authority(raw):
    with pytest.raises(ValueError, match="线路模式"):
        proxy_routing.normalize_routes({"service_route_modes": raw})


@pytest.mark.parametrize("mode", [None, True, 0, "auto", "Default", " default ", {}, []])
def test_route_modes_reject_unknown_values_without_mutating_input(mode):
    source = {"service_route_modes": {"youtube": mode}}
    previous = copy.deepcopy(source)
    with pytest.raises(ValueError, match="线路模式"):
        proxy_routing.normalize_routes(source)
    assert source == previous


@pytest.mark.parametrize("field", ["service_profile_bindings", "service_node_bindings"])
def test_route_modes_reject_conflicting_explicit_bindings(field):
    with pytest.raises(ValueError, match="冲突"):
        proxy_routing.normalize_routes({
            "service_route_modes": {"youtube": "default"}, field: {"youtube": "selected"},
        })


def test_route_modes_keep_valid_custom_targets_and_remove_deleted_ones():
    preferences = {
        "service_route_modes": {"custom:kept": "default", "custom:removed": "default"},
        "custom_targets": [{"id": "kept", "value": "example.test"}],
    }
    assert proxy_routing.normalize_routes(preferences)["service_route_modes"] == {"custom:kept": "default"}


def test_route_modes_reject_custom_target_discarded_by_validation():
    with pytest.raises(ValueError, match="无效的自定义目标"):
        proxy_routing.normalize_routes({
            "service_route_modes": {"custom:invalid": "default"},
            "custom_targets": [{"id": "invalid", "value": "not a domain"}],
        })


def test_suggestions_respect_persisted_default_without_current_dialog_edits():
    catalog = [{"id": "dc", "network_type": "datacenter", "nodes": [{"key": "one"}]}]
    source = proxy_routing.normalize_routes({"service_route_modes": {"youtube": "default"}})
    suggested, notices = suggest_tagged_routes(source, catalog)
    assert "youtube" not in suggested["service_profile_bindings"]
    assert suggested["service_profile_bindings"]["google"] == "dc"
    assert suggested["service_route_modes"] == {"youtube": "default"}
    assert "YouTube" in notices[-1]
    assert source["service_profile_bindings"] == {}


@pytest.mark.parametrize("raw", [None, [], {"youtube": "auto"}, {"youtube": False}])
def test_suggestions_do_not_repair_malformed_manual_mode_authority(raw):
    source = {"service_route_modes": raw}
    suggested, notices = suggest_tagged_routes(source, [])
    assert suggested == source
    assert "未自动分配" in notices[0]


def test_local_mode_survives_save_reload_and_unrelated_preference_write(isolated_routes, monkeypatch):
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: pytest.fail("metadata cannot apply live routes"))
    local_proxy.save_local_proxy_preferences(service_route_modes={"youtube": "default"})
    local_proxy.clear_local_proxy_preferences_cache()
    assert local_proxy.load_local_proxy_preferences()["service_route_modes"] == {"youtube": "default"}
    assert local_proxy._load_local_proxy_routing_preferences_strict()["service_route_modes"] == {"youtube": "default"}
    local_proxy.save_local_proxy_preferences(keep_running_on_exit=False)
    assert json.loads(local_proxy.LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8"))["service_route_modes"] == {"youtube": "default"}


@pytest.mark.parametrize("raw", [None, [], {"youtube": "auto"}, {"youtube": False}])
def test_local_invalid_mode_update_preserves_saved_authority(isolated_routes, raw):
    previous = local_proxy.save_local_proxy_preferences(service_route_modes={"youtube": "default"})
    previous_bytes = local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes()
    with pytest.raises(ValueError, match="线路模式"):
        local_proxy.save_local_proxy_preferences(service_route_modes=raw)
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_bytes() == previous_bytes
    assert local_proxy.load_local_proxy_preferences() == previous


def test_local_corrupt_mode_cannot_silently_be_saved_as_automatic(isolated_routes):
    original = '{"service_route_modes": {"youtube": "invalid"}}'
    local_proxy.LOCAL_PROXY_PREFS_PATH.write_text(original, encoding="utf-8")
    for operation in (
        local_proxy._load_local_proxy_routing_preferences_strict,
        lambda: local_proxy.save_local_proxy_preferences(keep_running_on_exit=False),
    ):
        with pytest.raises(ValueError, match="线路模式"):
            operation()
    assert local_proxy.LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8") == original


def test_ssh_mode_survives_snapshot_save_reload_without_deployment(isolated_routes, monkeypatch):
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: pytest.fail("metadata save cannot inspect a live host"))
    source = {"service_route_modes": {"youtube": "default"}}
    proxy_routing._save_ssh_routes("synthetic-host", source)
    assert proxy_routing.load_ssh_routes("synthetic-host")["service_route_modes"] == {"youtube": "default"}


@pytest.mark.parametrize("raw", [None, [], {"youtube": "auto"}, {"youtube": False}])
def test_ssh_invalid_mode_save_does_not_replace_valid_routes(isolated_routes, raw):
    proxy_routing._save_ssh_routes("synthetic-host", {"service_route_modes": {"youtube": "default"}})
    path = proxy_routing._host_path("synthetic-host")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="线路模式"):
        proxy_routing._save_ssh_routes("synthetic-host", {"service_route_modes": raw})
    assert path.read_bytes() == original


def test_single_service_manual_default_and_explicit_subscription_keep_intent(isolated_routes, monkeypatch):
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: "synthetic applied")
    monkeypatch.setattr(local_proxy, "_proxy_subscription_profile_for_route", lambda _profile_id: {"name": "synthetic"})
    monkeypatch.setattr(local_proxy, "_selected_subscription_route_pool", lambda *_args, **_kwargs: ({}, ()))
    local_proxy.set_local_proxy_service_profile_binding_and_apply("youtube", "")
    assert local_proxy.load_local_proxy_preferences()["service_route_modes"] == {"youtube": "default"}
    local_proxy.set_local_proxy_service_profile_binding_and_apply("youtube", "dc")
    preferences = local_proxy.load_local_proxy_preferences()
    assert preferences["service_profile_bindings"] == {"youtube": "dc"}
    assert preferences["service_route_modes"] == {}
    local_proxy.set_local_proxy_service_profile_binding_and_apply("youtube", "")
    assert local_proxy.load_local_proxy_preferences()["service_route_modes"] == {"youtube": "default"}


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_apply_rollback_restores_manual_default_metadata(isolated_routes, monkeypatch, scope):
    previous = proxy_routing.normalize_routes({"service_route_modes": {"youtube": "default"}})
    updated = proxy_routing.normalize_routes({"service_route_modes": {"claude": "default"}})

    def fail(*_args, **_kwargs):
        raise OSError("synthetic reload failure")

    if scope == "local":
        local_proxy.save_local_proxy_preferences(**previous)
        monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", fail)

        def apply():
            return local_proxy.set_local_proxy_service_routes_and_apply(updated, expected=previous)

        def read():
            return proxy_routing.route_snapshot(local_proxy._load_local_proxy_routing_preferences_strict())
    else:
        proxy_routing._save_ssh_routes("synthetic-host", previous)
        monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: SimpleNamespace(running=True))
        monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *_args: {
            "name": "synthetic", "type": "http", "server": "127.0.0.1", "port": 9,
        })
        monkeypatch.setattr(remote_proxy, "reload_ai_proxy", fail)

        def apply():
            return proxy_routing.apply_ssh_routes("synthetic-host", updated, expected=previous)

        def read():
            return proxy_routing.load_ssh_routes("synthetic-host")
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        apply()
    assert read() == previous


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_stale_editor_cannot_overwrite_new_manual_default_mode(isolated_routes, monkeypatch, scope):
    expected = proxy_routing.normalize_routes({})
    current = proxy_routing.normalize_routes({"service_route_modes": {"youtube": "default"}})
    if scope == "local":
        local_proxy.save_local_proxy_preferences(**current)
        monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: pytest.fail("stale save cannot apply"))
        with pytest.raises(RuntimeError, match="其他操作修改"):
            local_proxy.set_local_proxy_service_routes_and_apply(expected, expected=expected)
    else:
        proxy_routing._save_ssh_routes("synthetic-host", current)
        monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_args: pytest.fail("stale save cannot connect"))
        with pytest.raises(RuntimeError, match="其他操作修改"):
            proxy_routing.apply_ssh_routes("synthetic-host", expected, expected=expected)


def test_single_service_apply_failure_restores_default_intent(isolated_routes, monkeypatch):
    local_proxy.save_local_proxy_preferences(service_route_modes={"youtube": "default"})
    monkeypatch.setattr(local_proxy, "_proxy_subscription_profile_for_route", lambda _profile_id: {"name": "synthetic"})
    monkeypatch.setattr(local_proxy, "_selected_subscription_route_pool", lambda *_args, **_kwargs: ({}, ()))

    def fail():
        raise OSError("synthetic reload failure")

    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", fail)
    with pytest.raises(RuntimeError, match="已恢复原绑定"):
        local_proxy.set_local_proxy_service_profile_binding_and_apply("youtube", "dc")
    restored = local_proxy._load_local_proxy_routing_preferences_strict()
    assert restored["service_route_modes"] == {"youtube": "default"}
    assert restored["service_profile_bindings"] == {}
