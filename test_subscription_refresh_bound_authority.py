"""SSH timer authority survives the gap between planning and applying a route."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core import proxy_routing, remote_proxy
from core.proxy_update_result import update_result
from test_subscription_refresh_integration import scenario as _scenario_fixture


scenario = _scenario_fixture
_REAL_REFRESH = remote_proxy.refresh_running_ai_proxy_from_subscription


@pytest.fixture
def bound_refresh(scenario, monkeypatch):
    calls = SimpleNamespace(entry=[], bound=[], default=[])

    def refresh(*args, **kwargs):
        calls.entry.append(kwargs)
        return _REAL_REFRESH(*args, **kwargs)

    monkeypatch.setattr(remote_proxy, "refresh_running_ai_proxy_from_subscription", refresh)
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_a, **_k: SimpleNamespace(running=True))
    monkeypatch.setattr(remote_proxy, "format_proxy_node", lambda _node: "synthetic-node-text")
    monkeypatch.setattr(remote_proxy, "_find_matching_subscription_node", lambda nodes, *_args: nodes[0])
    monkeypatch.setattr(proxy_routing, "node_pool_warnings", lambda *_a, **_k: [])
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", lambda *a, **k:
                        calls.bound.append((a, k)) or update_result("bound applied", "applied"))
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy_verified", lambda *a, **k:
                        calls.default.append((a, k)) or update_result("default applied", "applied"))
    return calls


def _change_after_worker_unlock(monkeypatch, mutation):
    exits = 0

    @contextmanager
    def host_lock(_name):
        nonlocal exits
        yield
        exits += 1
        if exits == 1:
            mutation()

    monkeypatch.setattr(proxy_routing, "host_lock", host_lock)


@pytest.mark.parametrize("new_bindings", [{}, {"claude": "replacement"}])
def test_removed_or_reassigned_binding_cannot_become_default_update(
    scenario, bound_refresh, monkeypatch, new_bindings,
):
    _change_after_worker_unlock(monkeypatch, lambda: scenario.routes["server"].update(
        service_profile_bindings=new_bindings,
    ))
    result = scenario.run("ssh")
    assert not bound_refresh.default and not bound_refresh.bound
    assert result["steps"][-1]["outcome"] == "skipped"
    assert result["steps"][-1]["warning"]
    assert bound_refresh.entry[0]["require_service_binding"] is True
    assert "expected_current_key" not in bound_refresh.entry[0]


@pytest.mark.parametrize("field", ["url", "source_path", "source_revision"])
def test_bound_source_change_after_worker_unlock_rejects_old_result(
    scenario, bound_refresh, monkeypatch, field,
):
    _change_after_worker_unlock(monkeypatch, lambda: scenario.state["profiles"]["a"].update(
        {field: "synthetic-new-source"},
    ))
    result = scenario.run("ssh")
    assert not bound_refresh.default and not bound_refresh.bound
    assert result["steps"][-1]["outcome"] == "skipped"
    assert bound_refresh.entry[0]["expected_source"] == ("https://a.example/sub", "", "")


def test_unchanged_bound_source_does_not_need_to_be_active(scenario, bound_refresh):
    scenario.state["active_profile_id"] = "b"
    scenario.routes["server"]["service_profile_bindings"] = {"claude": "a"}
    result = scenario.run("ssh")
    assert len(bound_refresh.bound) == 1 and not bound_refresh.default
    assert result["steps"][-1]["outcome"] == "applied"
    assert bound_refresh.entry[0]["profile_id"] == "a"
    assert bound_refresh.entry[0]["require_service_binding"] is True
    assert bound_refresh.bound[0][1]["persist_selection"] is False


def test_default_timer_path_keeps_its_existing_origin_guards(scenario, bound_refresh):
    scenario.routes["server"]["service_profile_bindings"] = {}
    result = scenario.run("ssh")
    assert len(bound_refresh.default) == 1 and not bound_refresh.bound
    assert result["steps"][-1]["outcome"] == "applied"
    assert not bound_refresh.entry[0].get("require_service_binding", False)
    assert bound_refresh.entry[0]["expected_current_key"] == "original"
    assert bound_refresh.entry[0]["expected_source"] == ("https://a.example/sub", "", "")
    assert bound_refresh.default[0][1]["persist_selection"] is False


@pytest.mark.parametrize("profile_id", ["", "missing"])
def test_required_missing_binding_returns_typed_skip(scenario, bound_refresh, profile_id):
    result = _REAL_REFRESH("server", scenario.nodes["a"], profile_id=profile_id,
                           require_service_binding=True)
    assert result.outcome == "skipped" and result.warning and not result.retryable
    assert not bound_refresh.default and not bound_refresh.bound


def test_legacy_unbound_caller_can_still_request_default_update(scenario, bound_refresh):
    scenario.routes["server"]["service_profile_bindings"] = {}
    result = _REAL_REFRESH("server", scenario.nodes["a"], profile_id="a")
    assert result.outcome == "applied"
    assert len(bound_refresh.default) == 1 and not bound_refresh.bound
