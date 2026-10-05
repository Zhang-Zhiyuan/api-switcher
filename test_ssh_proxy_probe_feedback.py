"""Probe callback feedback without SSH, sockets, workers, or Tk."""
from types import MethodType, SimpleNamespace

import pytest

from core import remote_proxy
from ui.tabs import ssh_tab as module


def payload(results=(), failures=(), names=("synthetic",)):
    return {"ok": True, "result": {
        "results": list(results), "failures": list(failures), "server_names": list(names),
    }}


GOOD = "synthetic: AI 连通性 3/3 可达；OpenAI 正常；Claude 正常；Gemini 正常"


@pytest.mark.parametrize("result, expected", [
    (payload([GOOD]), "success"),
    (payload(["synthetic: AI 连通性 0/3 可达；三个目标均不可达"]), "warning"),
    (payload(["synthetic: AI 连通性 2/3 可达；Claude 不可达"]), "warning"),
    (payload(["synthetic: 代理未运行，跳过 AI 连通性探测"]), "warning"),
    (payload(["synthetic: 未得到连通性测试结果"]), "warning"),
    (payload(["synthetic: AI 连通性 0/0 可达"]), "warning"),
    (payload(["synthetic: AI 连通性 1/1 可达"]), "warning"),
    (payload(["synthetic: AI 连通性 4/3 可达"]), "warning"),
    (payload([GOOD + "；探测结果不完整: 实收 4/3"]), "warning"),
    (payload(), "warning"),
    (payload([GOOD], names=["synthetic", "missing"]), "warning"),
    (payload([GOOD, "second: AI 连通性 0/3 可达"], names=["synthetic", "second"]), "warning"),
    (payload([GOOD], ["second: synthetic SSH failure"], names=["synthetic", "second"]), "warning"),
    (payload(failures=["synthetic: synthetic SSH failure"]), "error"),
    ({"ok": False, "error": "synthetic worker failure"}, "error"),
])
def test_ssh_probe_reports_measured_health_not_just_completed_worker(monkeypatch, result, expected):
    statuses, toasts = [], []

    def set_status(kind, message, severity):
        statuses.append((kind, message, severity))
        if kind == "sync":
            label.text = message

    label = SimpleNamespace(text="")
    label.cget = lambda _key: label.text
    tab = SimpleNamespace(
        _require_selected_servers=lambda _setter: ["synthetic"],
        _format_server_target=lambda _names: "synthetic",
        _set_proxy_status=lambda message, severity: set_status("proxy", message, severity),
        _set_sync_status=lambda message, severity: set_status("sync", message, severity),
        _sync_status_label=label,
        _run_proxy_ssh_task=lambda _message, _worker, on_done: on_done(result),
        winfo_toplevel=lambda: None,
    )
    tab._show_server_batch_result = MethodType(module.SSHTab._show_server_batch_result, tab)
    monkeypatch.setattr(module, "remote_proxy", remote_proxy)
    monkeypatch.setattr(module, "show_toast", lambda _top, message, **kwargs: toasts.append((message, kwargs)))
    module.SSHTab._probe_ai_proxy(tab)
    assert [item[0] for item in statuses] == ["sync", "proxy"]
    assert all(item[2] == expected for item in statuses)
    assert len(toasts) == 1
    assert toasts[0][1]["severity"] == expected
    details = result.get("result") or {}
    for detail in [*details.get("results", []), *details.get("failures", [])]:
        assert all(detail in item[1] for item in statuses)
    if not result["ok"]:
        assert all(result["error"] in item[1] for item in statuses)
