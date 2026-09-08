from pathlib import Path

import pytest

from core import remote_proxy


URL = "https://subscription.example.test/old"
PAYLOAD = b"proxies:\n - {name: old, type: http, server: example.test, port: 8080}\n"
NEW_PAYLOAD = PAYLOAD.replace(b"name: old", b"name: new")


@pytest.fixture
def subscription(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(remote_proxy, "_subscription_proxy_environment_diagnostic",
                        lambda _url: remote_proxy.ProxyEnvironmentDiagnostic())
    monkeypatch.setattr(remote_proxy, "_reconcile_subscription_proxy_environment",
                        lambda _url, diagnostic, **_kwargs: diagnostic)
    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription",
                        lambda **_kwargs: (PAYLOAD, "application/yaml", "utf-8"))
    profile = remote_proxy.save_proxy_subscription_profile("原名称", URL)
    remote_proxy.fetch_proxy_subscription(URL, profile_id=profile["id"])
    return remote_proxy.load_proxy_subscription_state()["profiles"][profile["id"]]


@pytest.mark.parametrize("explicit_id", [True, False])
@pytest.mark.parametrize("change", ["edit", "delete", "recreate", "edit_back", "replace_with_file"])
def test_stale_subscription_download_never_overwrites_changed_profile(monkeypatch, subscription, tmp_path, change, explicit_id):
    profile_id = subscription["id"]
    expected = {}

    def download(**_kwargs):
        if change in {"delete", "recreate"}:
            remote_proxy.delete_proxy_subscription_profile(profile_id)
            if change == "recreate":
                remote_proxy.save_proxy_subscription_profile("重建", URL, profile_id=profile_id)
        elif change == "replace_with_file":
            source = tmp_path / "replacement.yaml"
            source.write_bytes(NEW_PAYLOAD)
            remote_proxy.import_proxy_subscription_file(source, profile_id=profile_id)
        else:
            remote_proxy.save_proxy_subscription_profile("修改", URL + "-new", profile_id=profile_id)
            if change == "edit_back":
                remote_proxy.save_proxy_subscription_profile("改回", URL, profile_id=profile_id)
        expected.update(remote_proxy.load_proxy_subscription_state())
        return NEW_PAYLOAD, "application/yaml", "utf-8"

    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription", download)
    with pytest.raises(RuntimeError, match="本次下载结果已丢弃"):
        remote_proxy.fetch_proxy_subscription(URL, profile_id=profile_id if explicit_id else "")
    assert remote_proxy.load_proxy_subscription_state() == expected
    for profile in expected.get("profiles", {}).values():
        if profile.get("saved_path"):
            expected_payload = NEW_PAYLOAD if change == "replace_with_file" else PAYLOAD
            assert Path(profile["saved_path"]).read_bytes() == expected_payload


def test_queued_old_url_rejected_before_network(monkeypatch, subscription):
    remote_proxy.save_proxy_subscription_profile("修改", URL + "-new", profile_id=subscription["id"])
    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription",
                        lambda **_kwargs: pytest.fail("stale queued URL must not be downloaded"))
    with pytest.raises(RuntimeError, match="订阅链接已变更"):
        remote_proxy.fetch_proxy_subscription(URL, profile_id=subscription["id"])


def test_queued_download_never_recreates_deleted_profile(monkeypatch, subscription):
    remote_proxy.delete_proxy_subscription_profile(subscription["id"])
    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription",
                        lambda **_kwargs: pytest.fail("deleted profile must not be downloaded"))
    with pytest.raises(RuntimeError, match="已删除或不存在"):
        remote_proxy.fetch_proxy_subscription(URL, profile_id=subscription["id"])
    assert not remote_proxy.load_proxy_subscription_state()["profiles"]


def test_preview_download_does_not_read_or_write_subscription_state(monkeypatch, subscription):
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state",
                        lambda: pytest.fail("non-persistent preview must not load profile state"))
    result = remote_proxy.fetch_proxy_subscription(URL, persist=False, profile_id="not-saved")
    assert result.saved_path == ""
    assert len(result.nodes) == 1


def test_download_preserves_concurrent_rename_node_selection_and_quality(monkeypatch, subscription):
    profile_id = subscription["id"]

    def download(**_kwargs):
        remote_proxy.rename_proxy_subscription_profile(profile_id, "新名称")
        remote_proxy.save_proxy_subscription_profile_state(
            profile_id, selected_node_key="manual-node", node_latencies={"manual-node": 15},
        )
        return NEW_PAYLOAD, "application/yaml", "utf-8"

    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription", download)
    result = remote_proxy.fetch_proxy_subscription(URL, profile_id=profile_id)
    actual = remote_proxy.load_proxy_subscription_state()["profiles"][profile_id]
    assert actual["name"] == "新名称"
    assert actual["selected_node_key"] == "manual-node"
    assert actual["node_latencies"] == {"manual-node": 15}
    assert Path(result.saved_path).read_bytes() == NEW_PAYLOAD


def test_download_does_not_undo_active_subscription_change(monkeypatch, subscription):
    other = remote_proxy.save_proxy_subscription_profile("另一个", URL + "-other", activate=False)

    def download(**_kwargs):
        remote_proxy.set_active_proxy_subscription_profile(other["id"])
        return NEW_PAYLOAD, "application/yaml", "utf-8"

    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription", download)
    result = remote_proxy.fetch_proxy_subscription(URL, profile_id=subscription["id"], activate=True)
    assert remote_proxy.load_proxy_subscription_state()["active_profile_id"] == other["id"]
    assert Path(result.saved_path).read_bytes() == NEW_PAYLOAD


def test_slower_download_cannot_replace_newer_cache(monkeypatch, subscription):
    def download(**_kwargs):
        monkeypatch.setattr(remote_proxy, "_download_proxy_subscription",
                            lambda **_kwargs: (NEW_PAYLOAD, "application/yaml", "utf-8"))
        remote_proxy.fetch_proxy_subscription(URL, profile_id=subscription["id"])
        return PAYLOAD, "application/yaml", "utf-8"

    monkeypatch.setattr(remote_proxy, "_download_proxy_subscription", download)
    with pytest.raises(RuntimeError, match="本次下载结果已丢弃"):
        remote_proxy.fetch_proxy_subscription(URL, profile_id=subscription["id"])
    current = remote_proxy.load_proxy_subscription_state()["profiles"][subscription["id"]]
    assert Path(current["saved_path"]).read_bytes() == NEW_PAYLOAD
