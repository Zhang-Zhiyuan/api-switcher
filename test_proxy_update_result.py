import copy
import pickle

import pytest

from core.proxy_update_result import ProxyUpdateResult, update_result, with_update_message


@pytest.mark.parametrize("outcome,applied", [("applied", True), ("unchanged", True),
                                           ("retained", False), ("failed", False),
                                           ("skipped", False), ("unknown", False)])
def test_string_compatibility_and_explicit_outcome(outcome, applied):
    result = update_result("显示文案", outcome, warning=True, retryable=True)
    assert result == "显示文案" and str(result) == "显示文案"
    assert result.applied is applied
    assert result.warning and result.retryable


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))])
def test_copy_preserves_status(clone):
    result = clone(update_result("test", "retained", warning=True, retryable=True))
    assert isinstance(result, ProxyUpdateResult)
    assert result.outcome == "retained" and result.warning and result.retryable


def test_message_context_does_not_lose_metadata():
    result = with_update_message(update_result("old", "retained", warning=True, retryable=True), "new")
    assert result == "new" and result.outcome == "retained" and result.warning and result.retryable


def test_untyped_message_never_infers_success():
    result = with_update_message("已成功", "new")
    assert result.outcome == "unknown" and result.warning and not result.applied


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        update_result("test", "typo")


def test_successful_local_reload_keeps_pool_warning_severity(monkeypatch):
    from core import local_proxy, proxy_routing

    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: {})
    monkeypatch.setattr(proxy_routing, "node_pool_warnings", lambda *args, **kwargs: ["指定池缺少一个节点"])
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda state: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda port: True)
    monkeypatch.setattr(local_proxy, "_read_local_managed_proxy_node", lambda: {
        "name": "synthetic", "type": "http", "server": "synthetic.example", "port": 8080,
    })
    monkeypatch.setattr(local_proxy, "reload_local_ai_proxy", lambda *args, **kwargs: update_result("loaded", "applied"))
    result = local_proxy.apply_local_proxy_routing_to_running()
    assert result.applied and result.warning
    assert "指定池缺少一个节点" in result
