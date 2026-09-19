"""Subscription labels stay local, explicit and independent of the active profile."""

from pathlib import Path

import pytest

from core import remote_proxy


URL = "https://subscription.example.test/nodes"
PAYLOAD = b"proxies:\n - {name: demo, type: http, server: example.test, port: 8080}\n"


@pytest.fixture(autouse=True)
def isolated_subscriptions(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(remote_proxy, "_subscription_proxy_environment_diagnostic",
                        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic())
    monkeypatch.setattr(remote_proxy, "_reconcile_subscription_proxy_environment",
                        lambda _url, diagnostic, **_kwargs: diagnostic)
    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription",
                        lambda **_kwargs: (PAYLOAD, "application/yaml", "utf-8"))
    remote_proxy.clear_proxy_subscription_state_cache()
    yield
    remote_proxy.clear_proxy_subscription_state_cache()


@pytest.mark.parametrize("network_type,label", [
    ("unknown", "未标记"), ("residential", "家宽"), ("datacenter", "非家宽"),
])
def test_subscription_network_type_round_trip_and_display_label(network_type, label):
    profile = remote_proxy.save_proxy_subscription_profile("任意名称", URL)
    assert profile["network_type"] == "unknown"
    result = remote_proxy.set_proxy_subscription_network_type(profile["id"], network_type)
    assert result["network_type"] == network_type
    result["network_type"] = "do-not-mutate-saved-state"
    remote_proxy.clear_proxy_subscription_state_cache()
    state = remote_proxy.load_proxy_subscription_state()
    assert state["network_type"] == network_type
    assert state["profiles"][profile["id"]]["network_type"] == network_type
    assert remote_proxy.proxy_subscription_network_type_label(network_type) == label
    assert remote_proxy.list_proxy_subscription_profiles()[0]["network_type"] == network_type


@pytest.mark.parametrize("invalid", ["", "Residential", "家宽", " residential ", None, 1, {}, []])
def test_subscription_network_type_rejects_invalid_writes_without_touching_storage(invalid):
    profile = remote_proxy.save_proxy_subscription_profile("主力", URL)
    state_path = remote_proxy._proxy_subscription_state_path()
    before = state_path.read_bytes()
    writers = (
        lambda: remote_proxy.set_proxy_subscription_network_type(profile["id"], invalid),
        lambda: remote_proxy.save_proxy_subscription_profile_state(profile["id"], network_type=invalid),
        lambda: remote_proxy.save_proxy_subscription_state(network_type=invalid),
    )
    for writer in writers:
        with pytest.raises(ValueError, match="订阅类型无效"):
            writer()
        assert state_path.read_bytes() == before


@pytest.mark.parametrize("missing_id", ["", "missing", " ", None])
def test_subscription_network_type_never_falls_back_to_active_profile(missing_id):
    remote_proxy.save_proxy_subscription_profile("主力", URL)
    before = remote_proxy.load_proxy_subscription_state()
    with pytest.raises(ValueError, match="订阅配置不存在"):
        remote_proxy.set_proxy_subscription_network_type(missing_id, "residential")
    assert remote_proxy.load_proxy_subscription_state() == before


def test_label_updates_only_requested_profile_even_when_another_is_active():
    first = remote_proxy.save_proxy_subscription_profile("甲", URL)
    second = remote_proxy.save_proxy_subscription_profile("乙", URL + "/second")
    before = remote_proxy.load_proxy_subscription_state()
    remote_proxy.set_proxy_subscription_network_type(first["id"], "residential")
    after = remote_proxy.load_proxy_subscription_state()
    assert after["active_profile_id"] == second["id"]
    assert after["network_type"] == "unknown"
    assert after["profiles"][second["id"]] == before["profiles"][second["id"]]
    assert after["profiles"][first["id"]]["network_type"] == "residential"
    assert after["profiles"][first["id"]]["source_revision"] == before["profiles"][first["id"]]["source_revision"]
    remote_proxy.set_proxy_subscription_network_type(second["id"], "datacenter")
    assert remote_proxy.set_active_proxy_subscription_profile(first["id"])["network_type"] == "residential"
    assert remote_proxy.load_proxy_subscription_state()["network_type"] == "residential"
    assert remote_proxy.set_active_proxy_subscription_profile(second["id"])["network_type"] == "datacenter"
    assert remote_proxy.load_proxy_subscription_state()["network_type"] == "datacenter"


def test_label_survives_rename_resave_download_and_concurrent_edit(monkeypatch):
    profile = remote_proxy.save_proxy_subscription_profile("甲", URL)
    profile_id = profile["id"]
    remote_proxy.set_proxy_subscription_network_type(profile_id, "residential")
    assert remote_proxy.rename_proxy_subscription_profile(profile_id, "家宽 A")["network_type"] == "residential"
    assert remote_proxy.save_proxy_subscription_profile("重新保存", URL, profile_id=profile_id)["network_type"] == "residential"
    remote_proxy.fetch_proxy_subscription(URL, profile_id=profile_id)
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "residential"

    def download(**_kwargs):
        remote_proxy.set_proxy_subscription_network_type(profile_id, "datacenter")
        return PAYLOAD, "application/yaml", "utf-8"

    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription", download)
    result = remote_proxy.fetch_proxy_subscription(URL, profile_id=profile_id)
    assert Path(result.saved_path).read_bytes() == PAYLOAD
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "datacenter"


def test_local_import_preserves_existing_subscription_label(tmp_path):
    source = tmp_path / "offline.yaml"
    source.write_bytes(PAYLOAD)
    remote_proxy.import_proxy_subscription_file(source)
    profile_id = remote_proxy.active_proxy_subscription_profile()["id"]
    remote_proxy.set_proxy_subscription_network_type(profile_id, "datacenter")
    remote_proxy.import_proxy_subscription_file(source)
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "datacenter"
    remote_proxy.rename_proxy_subscription_profile(profile_id, "本地家宽")
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "datacenter"


@pytest.mark.parametrize("legacy_value", [None, "", "broken", "residential", [], {}])
def test_old_and_malformed_profile_labels_are_read_conservatively(legacy_value):
    profile = {"id": "a", "url": URL, "name": "住宅家宽"}
    if legacy_value is not None:
        profile["network_type"] = legacy_value
    state = remote_proxy._normalize_proxy_subscription_state({"profiles": {"a": profile}, "active_profile_id": "a"})
    expected = "residential" if legacy_value == "residential" else "unknown"
    assert state["profiles"]["a"]["network_type"] == expected
    assert state["network_type"] == expected
    assert remote_proxy.proxy_subscription_network_type_label(legacy_value) == (
        "家宽" if expected == "residential" else "未标记"
    )


def test_legacy_single_subscription_migrates_explicit_label():
    state = remote_proxy._normalize_proxy_subscription_state({"url": URL, "network_type": "datacenter"})
    profile_id = state["active_profile_id"]
    assert state["profiles"][profile_id]["network_type"] == "datacenter"
    assert state["network_type"] == "datacenter"


def test_generic_state_updates_validate_and_keep_network_type():
    profile = remote_proxy.save_proxy_subscription_profile("甲", URL)
    remote_proxy.save_proxy_subscription_profile_state(profile["id"], network_type="residential")
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "residential"
    remote_proxy.save_proxy_subscription_state(network_type="datacenter")
    assert remote_proxy.active_proxy_subscription_profile()["network_type"] == "datacenter"


def test_bulk_labels_commit_together_and_preserve_active_profile(monkeypatch):
    first = remote_proxy.save_proxy_subscription_profile("甲", URL)
    second = remote_proxy.save_proxy_subscription_profile("乙", URL + "/second")
    updates = {first["id"]: "residential", second["id"]: "datacenter"}
    commits = []
    persist = remote_proxy._persist_proxy_subscription_state

    def record(state):
        commits.append(state)
        return persist(state)

    monkeypatch.setattr(remote_proxy, "_persist_proxy_subscription_state", record)
    result = remote_proxy.set_proxy_subscription_network_types(updates, expected={key: "unknown" for key in updates})
    assert len(commits) == 1
    assert {key: profile["network_type"] for key, profile in result.items()} == updates
    state = remote_proxy.load_proxy_subscription_state()
    assert state["active_profile_id"] == second["id"]
    assert state["network_type"] == "datacenter"
    remote_proxy.set_proxy_subscription_network_types(updates, expected=updates)
    assert len(commits) == 1  # No-op saves do not churn the state file or its timestamps.


@pytest.mark.parametrize("conflict", ["missing", "modified", "invalid_type", "expected_scope"])
def test_bulk_labels_validate_every_change_before_any_write(conflict):
    first = remote_proxy.save_proxy_subscription_profile("甲", URL)
    second = remote_proxy.save_proxy_subscription_profile("乙", URL + "/second")
    updates = {first["id"]: "residential", second["id"]: "datacenter"}
    expected = {key: "unknown" for key in updates}
    if conflict == "missing":
        updates["missing"] = "residential"
        expected["missing"] = "unknown"
    elif conflict == "modified":
        remote_proxy.set_proxy_subscription_network_type(second["id"], "residential")
    elif conflict == "invalid_type":
        updates[second["id"]] = "住宅"
    else:
        expected.pop(second["id"])
    state_path = remote_proxy._proxy_subscription_state_path()
    before = state_path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        remote_proxy.set_proxy_subscription_network_types(updates, expected=expected)
    assert state_path.read_bytes() == before


def test_bulk_labels_preserve_concurrent_rename_and_unedited_labels():
    first = remote_proxy.save_proxy_subscription_profile("甲", URL)
    second = remote_proxy.save_proxy_subscription_profile("乙", URL + "/second")
    remote_proxy.rename_proxy_subscription_profile(first["id"], "新名称")
    remote_proxy.set_proxy_subscription_network_type(second["id"], "datacenter")
    remote_proxy.set_proxy_subscription_network_types({first["id"]: "residential"}, expected={first["id"]: "unknown"})
    state = remote_proxy.load_proxy_subscription_state()
    assert state["profiles"][first["id"]]["name"] == "新名称"
    assert state["profiles"][second["id"]]["network_type"] == "datacenter"
