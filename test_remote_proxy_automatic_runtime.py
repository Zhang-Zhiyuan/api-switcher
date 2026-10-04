"""Automatic SSH hot updates: synthetic transport, exact snapshot rollback."""
from copy import deepcopy
import shlex
from types import SimpleNamespace

import pytest
import yaml

from core import remote_proxy
from core.proxy_update_result import update_result
from models.profile import SSHProfile


@pytest.fixture
def deployment(monkeypatch):
    old_node = {"name": "old synthetic", "type": "http", "server": "old.invalid", "port": 8080}
    new_node = {"name": "new synthetic", "type": "http", "server": "new.invalid", "port": 8080}
    backup = {"name": "old fallback", "type": "http", "server": "fallback.invalid", "port": 8080}
    original = remote_proxy.build_mihomo_config(old_node, 7890, fallback_proxy_nodes=[backup],
                                               health_checked_group=True, strict_privacy=True)
    original += "# synthetic prior full topology and recovery metadata\n"
    candidate = remote_proxy.build_mihomo_config(new_node, 7890, health_checked_group=True,
                                                strict_privacy=True)
    state = {"config": original, "routes": {}, "fail_runtime": False, "hook": None,
             "reload_code": 0, "rollback_code": 0, "runtime_code": 0, "runtime_error": False,
             "runtime_transform": None, "writes": [], "events": [], "selections": []}
    state["profile"] = SSHProfile("synthetic-server", "synthetic-host.invalid")
    state["active_profile_id"] = "synthetic-subscription"
    state["subscriptions"] = {"synthetic-subscription": {
        "url": "https://subscription.invalid/synthetic", "source_path": "", "source_revision": "one",
    }}
    monkeypatch.setattr(remote_proxy.profile_manager, "list_ssh_profiles", lambda: [state["profile"]])
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state",
                        lambda: {"active_profile_id": state["active_profile_id"], "profiles": deepcopy(state["subscriptions"])})
    client = object()
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda *_a, **_k: SimpleNamespace(running=True))
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda *_a, **_k: (None, client))
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda _client: "/synthetic/home")
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda *_a, **_k: state["config"])
    monkeypatch.setattr(remote_proxy.proxy_routing, "load_ssh_routes", lambda _name: deepcopy(state["routes"]))
    monkeypatch.setattr(remote_proxy.proxy_routing, "ssh_probe_kwargs", lambda _name: {})
    monkeypatch.setattr(remote_proxy.proxy_routing, "ssh_config_options", lambda *_a, **_k: {})
    monkeypatch.setattr(remote_proxy, "_remote_proxy_fallback_nodes", lambda *_a, **_k: ())
    monkeypatch.setattr(remote_proxy, "build_mihomo_config", lambda *_a, **_k: candidate)
    monkeypatch.setattr(remote_proxy, "_repair_remote_proxy_integrations", lambda *_a: "")

    def write(_client, _path, content, **kwargs):
        assert kwargs["file_mode"] == 0o600
        state["writes"].append(content)
        state["config"] = content

    def execute(_client, command, **kwargs):
        assert kwargs["log_command"] is False
        if "ROUNDS=1" in command:
            assert "STRICT=1" in command and "TIMEOUT=3" in command
            assert "include_compact = False" in command
            assert kwargs["timeout"] == 12
            state["events"].append("runtime")
            if state["hook"]:
                state["hook"]()
            if state["runtime_error"]:
                raise TimeoutError("synthetic post-apply timeout")
            raw = next(line.removeprefix("TARGETS_JSON=") for line in command.splitlines()
                       if line.startswith("TARGETS_JSON="))
            targets = yaml.safe_load(shlex.split(raw)[0])
            output = "\n".join(f"probe\t{label}\t{0 if state['fail_runtime'] else 1}\tsynthetic\t3"
                               for label, _url in targets)
            if state["runtime_transform"]:
                output = state["runtime_transform"](output)
            return state["runtime_code"], output, "synthetic stderr"
        assert "configs?force=true" in command
        restored = state["config"] == original
        state["events"].append("restore" if restored else "reload")
        return state["rollback_code"] if restored else state["reload_code"], "", ""

    def isolated(*_args, **_kwargs):
        state["events"].append("isolated")
        return f"synthetic: AI 稳定性 {remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES}/{remote_proxy.REMOTE_AI_STABILITY_EXPECTED_PROBES} 可达"

    def save(node, **kwargs):
        state["events"].append("selection")
        state["selections"].append((node, kwargs))

    monkeypatch.setattr(remote_proxy.ssh_manager, "write_remote_file", write)
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", execute)
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolated)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", save)
    return SimpleNamespace(state=state, original=original, candidate=candidate, old_node=old_node, new_node=new_node)


def apply(deployment, **kwargs):
    return remote_proxy.reload_ai_proxy_verified(
        "synthetic-server", remote_proxy.format_proxy_node(deployment.new_node), automatic_update=True, **kwargs,
    )


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("profile_id", ["", "synthetic-subscription"])
def test_automatic_update_only_persists_after_actual_runtime_passes(deployment, persist, profile_id):
    result = apply(deployment, persist_selection=persist, profile_id=profile_id)
    assert result.outcome == "applied" and not result.warning and not result.retryable
    assert deployment.state["events"] == ["isolated", "reload", "runtime"] + (["selection"] if persist else [])
    assert deployment.state["writes"] == [deployment.candidate]
    assert deployment.state["selections"] == (
        [(deployment.new_node, {"profile_id": profile_id} if profile_id else {})] if persist else []
    )
    assert "正式入口短测 4/4 通过" in result


@pytest.mark.parametrize("failure", ["failed", "exception", "exit", "truncated", "duplicate"])
def test_actual_entry_failure_restores_entire_old_topology(deployment, failure):
    state = deployment.state
    if failure == "failed":
        state["fail_runtime"] = True
    elif failure == "exception":
        state["runtime_error"] = True
    elif failure == "exit":
        state["runtime_code"] = 3
    elif failure == "truncated":
        state["runtime_transform"] = lambda output: output.splitlines()[0]
    else:
        state["runtime_transform"] = lambda output: "\n".join([output.splitlines()[0]] * 4)
    result = apply(deployment, persist_selection=True, profile_id="synthetic-subscription")
    assert result.outcome == "retained" and result.warning and result.retryable
    assert state["config"] == deployment.original
    assert state["writes"] == [deployment.candidate, deployment.original]
    assert state["events"] == ["isolated", "reload", "runtime", "restore"]
    assert not state["selections"]
    assert "完整配置及线路拓扑" in result


@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("change", ["config", "routes"])
def test_manual_edits_during_runtime_probe_prevent_rollback_and_selection(deployment, success, change):
    state = deployment.state
    state["fail_runtime"] = not success
    replacement = deployment.candidate + "# newer manual topology\n"
    state["hook"] = (lambda: state.update(config=replacement)) if change == "config" else (
        lambda: state.update(routes={"manual": "changed"})
    )
    result = apply(deployment)
    assert result.outcome == "skipped" and result.warning and not result.retryable
    assert state["writes"] == [deployment.candidate]
    assert not state["selections"] and "restore" not in state["events"]
    if change == "config":
        assert state["config"] == replacement


@pytest.mark.parametrize("change", ["config", "routes"])
def test_manual_edits_during_isolated_probe_prevent_apply(deployment, monkeypatch, change):
    state = deployment.state

    def isolated(*_args, **_kwargs):
        if change == "config":
            state["config"] += "# manual topology with same primary\n"
        else:
            state["routes"] = {"changed": True}
        return "AI 稳定性 13/13 可达"

    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolated)
    result = apply(deployment)
    assert result.outcome == "skipped" and result.warning
    assert not state["writes"] and not state["selections"]


def test_rollback_failure_never_claims_restored_or_commits_selection(deployment):
    deployment.state.update(fail_runtime=True, rollback_code=1)
    result = apply(deployment)
    assert result.outcome == "failed" and result.warning and result.retryable
    assert "恢复未完成" in result
    assert not deployment.state["selections"]


def test_apply_skipped_does_not_run_actual_entry_probe(deployment, monkeypatch):
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", lambda *_a, **_k: update_result("stopped", "skipped"))
    result = apply(deployment)
    assert result.outcome == "skipped"
    assert deployment.state["events"] == ["isolated"]
    assert not deployment.state["writes"] and not deployment.state["selections"]


def test_candidate_rejection_is_typed_retained_without_runtime_changes(deployment, monkeypatch):
    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", lambda *_a, **_k: "AI 稳定性 0/13 可达")
    result = apply(deployment)
    assert result.outcome == "retained" and result.warning and result.retryable
    assert not deployment.state["writes"] and not deployment.state["selections"]


def config_with_rules(rules):
    return f"# {remote_proxy.AI_PROXY_CONFIG_MARKER}\n" + yaml.safe_dump({"mode": "rule", "rules": rules})


def test_actual_probe_skips_independent_and_direct_targets_even_with_custom_overrides():
    config = config_with_rules([
        "DOMAIN-SUFFIX,api.openai.com,DIRECT",  # Specific custom override wins.
        "DOMAIN-SUFFIX,openai.com,AI-PROXY",
        "DOMAIN-SUFFIX,chatgpt.com,AI-PROXY",
        "DOMAIN-SUFFIX,anthropic.com,CLAUDE-INDEPENDENT",
        "DOMAIN-SUFFIX,googleapis.com,AI-PROXY",
        "MATCH,DIRECT",
    ])
    assert [label for label, _url in remote_proxy._remote_automatic_probe_targets(config)] == [
        "ChatGPT 出口", "Gemini/Google AI",
    ]


def test_no_default_ai_targets_does_not_claim_connectivity_or_start_probe(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unrelated routes must not be probed")

    monkeypatch.setattr(remote_proxy, "_connect_ssh", forbidden)
    ok, message = remote_proxy._probe_remote_automatic_update_runtime(
        "synthetic", 7890, config_with_rules(["MATCH,INDEPENDENT"]),
    )
    assert ok is None and "仅确认配置热加载" in message


@pytest.mark.parametrize("rules", [["GEOIP,CN,DIRECT", "MATCH,AI-PROXY"], ["DOMAIN-SUFFIX,openai.com,DIRECT"]])
def test_uncertain_or_missing_rule_match_is_not_treated_as_default_route(rules):
    assert remote_proxy._remote_automatic_probe_targets(config_with_rules(rules)) == ()


def test_restore_helper_rechecks_ownership_immediately_before_write(deployment):
    deployment.state["config"] = deployment.candidate + "# external update"
    with pytest.raises(remote_proxy._RemoteProxyUpdateOwnershipChanged):
        remote_proxy._restore_remote_automatic_config(
            "synthetic-server", 7890, deployment.original, deployment.candidate, {},
        )
    assert not deployment.state["writes"]


def test_failed_selection_persist_does_not_undo_verified_runtime(deployment, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic store unavailable")

    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_selected_node", unavailable)
    result = apply(deployment)
    assert result.outcome == "applied" and result.warning and not result.retryable
    assert deployment.state["config"] == deployment.candidate
    assert deployment.state["writes"] == [deployment.candidate]


def test_short_probe_command_retains_strict_classification_without_compact():
    command = remote_proxy._build_probe_command(
        7890, 3, rounds=1, strict=True, targets=remote_proxy.REMOTE_AI_STABILITY_TARGETS[:1],
        include_compact=False,
    )
    assert "include_compact = False" in command and "if strict and include_compact:" in command
    assert "OpenAI API 身份已校验" in command
    assert "ROUNDS=1" in command and "TIMEOUT=3" in command


@pytest.mark.parametrize("phase", ["isolation", "runtime"])
def test_same_named_ssh_target_change_never_continues_or_restores_another_host(deployment, monkeypatch, phase):
    state = deployment.state

    def change_target():
        state["profile"] = SSHProfile("synthetic-server", "new-host.invalid")

    if phase == "isolation":
        def isolate(*_args, **_kwargs):
            change_target()
            return "AI 稳定性 13/13 可达"
        monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolate)
    else:
        state["hook"] = change_target
        state["fail_runtime"] = True
    result = apply(deployment)
    assert result.outcome == "skipped"
    assert not state["selections"]
    assert state["writes"] == ([] if phase == "isolation" else [deployment.candidate])
    assert "restore" not in state["events"]


@pytest.mark.parametrize("phase", ["before", "isolation", "runtime"])
def test_saved_subscription_origin_change_prevents_automatic_commit(deployment, monkeypatch, phase):
    state = deployment.state

    def changed():
        state["active_profile_id"] = "new-manual-subscription"

    if phase == "before":
        changed()
    elif phase == "isolation":
        def isolate(*_args, **_kwargs):
            changed()
            return "AI 稳定性 13/13 可达"
        monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolate)
    else:
        state["hook"] = changed
        state["fail_runtime"] = True
    result = apply(deployment, profile_id="synthetic-subscription",
                   _expected_current_key=remote_proxy.proxy_node_key(deployment.old_node))
    assert result.outcome == "skipped"
    assert not state["selections"]
    assert state["writes"] == ([] if phase != "runtime" else [deployment.candidate])


def test_saved_subscription_entry_rejects_stale_default_before_any_apply(deployment):
    deployment.state["routes"] = {"service_profile_bindings": {}}
    result = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "synthetic-server", [], profile_id="synthetic-subscription", expected_current_key="stale-key",
    )
    assert result.outcome == "skipped" and result.warning
    assert not deployment.state["events"] and not deployment.state["writes"]


def test_saved_subscription_entry_forwards_origin_to_final_commit_guard(deployment, monkeypatch):
    deployment.state["routes"] = {"service_profile_bindings": {}}
    expected = remote_proxy.proxy_node_key(deployment.old_node)
    source = ("https://subscription.invalid/synthetic", "", "one")
    captured = []

    def verified(*_args, **kwargs):
        captured.append(kwargs)
        return update_result("synthetic applied", "applied")

    monkeypatch.setattr(remote_proxy, "reload_ai_proxy_verified", verified)
    result = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "synthetic-server", [remote_proxy.ProxySubscriptionNode(0, deployment.old_node)],
        profile_id="synthetic-subscription", persist_selection=False, expected_current_key=expected, expected_source=source,
    )
    assert result.outcome == "applied"
    assert captured[0]["_expected_current_key"] == expected
    assert captured[0]["_expected_source"] == source
    assert captured[0]["persist_selection"] is False


def test_unchanged_deployment_uses_exact_existing_text_for_runtime_ownership(deployment, monkeypatch):
    original = deployment.original
    monkeypatch.setattr(remote_proxy, "build_mihomo_config", lambda *_args, **_kwargs: original.rstrip())
    result = apply(deployment, persist_selection=False)
    assert result.outcome == "unchanged"
    assert "正式入口短测 4/4 通过" in result
    assert not deployment.state["writes"]
    assert deployment.state["config"] == original


def test_unchanged_deployment_failed_probe_never_writes_or_reloads(deployment, monkeypatch):
    original = deployment.original
    monkeypatch.setattr(remote_proxy, "build_mihomo_config", lambda *_args, **_kwargs: original.rstrip())
    deployment.state["fail_runtime"] = True
    result = apply(deployment)
    assert result.outcome == "retained" and result.retryable and result.warning
    assert "配置未改动" in result and "未执行重载" in result
    assert deployment.state["events"] == ["isolated", "runtime"]
    assert not deployment.state["writes"] and not deployment.state["selections"]
    assert deployment.state["config"] == original


def test_metadata_only_update_failed_probe_restores_metadata_without_reloading(deployment, monkeypatch):
    original = deployment.original
    metadata_update = original + remote_proxy.proxy_routing.ROUTE_SNAPSHOT_MARKER + "synthetic-new-metadata\n"
    monkeypatch.setattr(remote_proxy, "build_mihomo_config", lambda *_args, **_kwargs: metadata_update)
    deployment.state["fail_runtime"] = True
    result = apply(deployment)
    assert result.outcome == "retained" and result.retryable and result.warning
    assert deployment.state["events"] == ["isolated", "runtime"]
    assert deployment.state["writes"] == [metadata_update, original]
    assert not deployment.state["selections"]
    assert deployment.state["config"] == original


def test_bound_routes_keep_underlying_outcome_and_do_not_test_unrelated_targets(deployment, monkeypatch):
    from core import service_route_verification as verification

    deployment.state["routes"] = {"service_profile_bindings": {"claude": "bound"}}
    monkeypatch.setattr(remote_proxy.proxy_routing, "node_pool_warnings", lambda *_a, **_k: ["synthetic pool warning"])
    monkeypatch.setattr(remote_proxy, "reload_ai_proxy", lambda *_a, **_k: update_result("synthetic retained", "retained", retryable=True))
    monkeypatch.setattr(verification, "build_remote_command", lambda *_a, **_k: pytest.fail("retained config must not be probed"))
    result = remote_proxy.refresh_running_ai_proxy_from_subscription(
        "synthetic-server", [], profile_id="bound",
    )
    assert result.outcome == "retained" and result.retryable and result.warning
    assert len(result.route_verification) == 1
    row = result.route_verification[0]
    assert row["service"] == "claude" and row["status"] == "unverified"
    assert "本轮配置未变或未加载，未重复探测" in row["detail"]
    assert not deployment.state["events"]


@pytest.mark.parametrize("phase", ["before", "isolation", "runtime"])
@pytest.mark.parametrize("field", ["url", "source_path", "source_revision"])
def test_same_subscription_id_source_change_rejects_stale_probe_commit(deployment, monkeypatch, phase, field):
    state = deployment.state
    source = state["subscriptions"]["synthetic-subscription"]
    expected_source = tuple(source[key] for key in ("url", "source_path", "source_revision"))

    def changed():
        source[field] = "synthetic-new-source"

    if phase == "before":
        changed()
    elif phase == "isolation":
        def isolate(*_args, **_kwargs):
            changed()
            return "AI 稳定性 13/13 可达"
        monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolate)
    else:
        state["hook"] = changed
        state["fail_runtime"] = True
    result = apply(deployment, profile_id="synthetic-subscription",
                   _expected_current_key=remote_proxy.proxy_node_key(deployment.old_node),
                   _expected_source=expected_source)
    assert result.outcome == "skipped" and result.warning and not result.retryable
    assert not state["selections"]
    assert state["writes"] == ([] if phase != "runtime" else [deployment.candidate])
    assert "restore" not in state["events"]
    assert expected_source[0] not in result  # Never expose credential-bearing subscription URLs.


def test_deleted_subscription_rejects_stale_source_even_if_active_id_is_unchanged(deployment, monkeypatch):
    source = tuple(deployment.state["subscriptions"]["synthetic-subscription"][key]
                   for key in ("url", "source_path", "source_revision"))

    def isolate(*_args, **_kwargs):
        deployment.state["subscriptions"].clear()
        return "AI 稳定性 13/13 可达"

    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", isolate)
    result = apply(deployment, profile_id="synthetic-subscription", _expected_source=source)
    assert result.outcome == "skipped"
    assert not deployment.state["writes"] and not deployment.state["selections"]


def test_source_only_guard_does_not_require_bound_subscription_to_be_active(deployment):
    deployment.state["active_profile_id"] = "other"
    source = tuple(deployment.state["subscriptions"]["synthetic-subscription"][key]
                   for key in ("url", "source_path", "source_revision"))
    assert remote_proxy._remote_automatic_subscription_origin_matches(
        "synthetic-subscription", None, expected_source=source,
    )
