"""SSH deployment feedback follows outcomes, not a successful Python return."""
from types import MethodType, SimpleNamespace

import pytest

from core.proxy_update_result import ProxyUpdateResult, update_result
from ui.tabs import ssh_tab


def _payload(*results, failures=(), names=None):
    return {"ok": True, "result": {
        "results": list(results), "failures": list(failures),
        "server_names": names or [f"server-{index}" for index in range(len(results) + len(failures))],
    }}


@pytest.mark.parametrize("message", [
    "server: 候选未通过隔离稳定性验证，未修改正式代理",
    "server: 自动尝试 4 个节点仍未通过隔离 3/3×4 服务 + compact 验证，未修改正式代理",
    "server: 候选未通过隔离稳定性验证；自动换节点测速失败: synthetic；未修改正式代理",
    "server: AI 代理未运行，已跳过热更新",
    "server: 未知的旧版完成文本",
])
def test_uncommitted_string_deployment_is_not_reported_as_success(message):
    feedback, severity = ssh_tab._proxy_deployment_feedback(_payload(message))
    assert severity == "warning"
    assert feedback.startswith("AI 代理部署未完成")
    assert message in feedback


@pytest.mark.parametrize("outcome, warning, expected", [
    ("applied", False, "success"),
    ("unchanged", False, "success"),
    ("applied", True, "warning"),
    ("retained", True, "warning"),
    ("skipped", True, "warning"),
    ("failed", True, "error"),
    ("unknown", False, "warning"),
])
def test_typed_outcome_survives_batch_formatting_and_overrides_probe_words(outcome, warning, expected):
    result = update_result("隔离验证通过", outcome, warning=warning, retryable=True)
    formatted = ssh_tab._format_server_batch_item("server", result)
    assert isinstance(formatted, ProxyUpdateResult)
    assert formatted.outcome == outcome
    assert formatted.warning is warning
    assert formatted.retryable is True
    assert str(formatted) == "server: 隔离验证通过"
    feedback, severity = ssh_tab._proxy_deployment_feedback(_payload(formatted))
    assert severity == expected
    if not result.applied:
        assert feedback.startswith("AI 代理部署未完成")


@pytest.mark.parametrize("message, expected", [
    ("AI 代理已部署到 server；隔离验证通过: 4/4", "success"),
    ("server: 原节点验证失败，已自动切换到 backup；验证通过: 4/4", "warning"),
])
def test_confirmed_legacy_deployment_remains_recognized(message, expected):
    feedback, severity = ssh_tab._proxy_deployment_feedback(_payload(message))
    assert severity == expected
    assert "未完成" not in feedback
    assert message in feedback


def test_partial_batch_preserves_retained_and_exception_details():
    success = update_result("a: 隔离验证通过", "applied")
    retained = update_result("b: 已保留当前运行节点；隔离验证通过", "retained", warning=True)
    feedback, severity = ssh_tab._proxy_deployment_feedback(
        _payload(success, retained, failures=["c: 连接失败"]),
    )
    assert severity == "warning"
    assert "部分完成（确认 1/3 台）" in feedback
    assert all(detail in feedback for detail in (str(success), str(retained), "c: 连接失败"))


@pytest.mark.parametrize("payload, expected", [
    ({"ok": False, "error": "线程未启动"}, "error"),
    (_payload(failures=["a: 连接失败"]), "error"),
    (_payload(names=["a"]), "warning"),
])
def test_failed_or_empty_batch_never_reports_completion(payload, expected):
    feedback, severity = ssh_tab._proxy_deployment_feedback(payload)
    assert severity == expected
    assert not feedback.startswith("AI 代理部署完成")


@pytest.mark.parametrize("result, expected", [
    ("a: 候选未通过隔离稳定性验证，未修改正式代理", "warning"),
    (update_result("a: 已恢复更新前完整配置；隔离验证通过", "retained", warning=True), "warning"),
    (update_result("a: 候选隔离验证通过，但正式热更新失败", "failed", warning=True), "error"),
])
def test_deploy_callback_updates_both_statuses_and_toast_without_real_ssh(monkeypatch, result, expected):
    statuses, toasts = [], []
    tab = SimpleNamespace(
        _proxy_subscription_nodes=[], _proxy_quality_results={},
        _require_selected_servers=lambda _setter: ["a"],
        _proxy_node_input=lambda: "synthetic node",
        _set_proxy_selected_summary=lambda *_args: None,
        _set_proxy_status=lambda message, severity: statuses.append(("proxy", message, severity)),
        _set_sync_status=lambda message, severity: statuses.append(("sync", message, severity)),
        _format_server_target=lambda _names: "a",
        _proxy_strict_privacy_setting=lambda: None,
        winfo_toplevel=lambda: None,
    )
    tab._run_server_batch = MethodType(ssh_tab.SSHTab._run_server_batch, tab)
    tab._run_proxy_ssh_task = lambda _message, worker, on_done: on_done({"ok": True, "result": worker()})
    monkeypatch.setattr(ssh_tab, "remote_proxy", SimpleNamespace(
        parse_proxy_node=lambda _text: {}, describe_proxy_node=lambda _node: "synthetic",
        install_ai_proxy_verified=lambda *_args, **_kwargs: result,
    ))
    monkeypatch.setattr(ssh_tab, "ConfirmDialog", lambda *_args, **kwargs: kwargs["on_confirm"]())
    monkeypatch.setattr(ssh_tab, "show_toast", lambda _master, message, **kwargs: toasts.append((message, kwargs)))
    ssh_tab.SSHTab._deploy_ai_proxy(tab)
    assert [item[0] for item in statuses] == ["sync", "proxy"]
    assert all(item[2] == expected and str(result) in item[1] for item in statuses)
    assert toasts == [(statuses[0][1], {"severity": expected})]
