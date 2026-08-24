"""Regression tests for user-facing feedback semantics and secret safety."""

from types import SimpleNamespace

from ui.dialogs.api_test_result_dialog import APITestResultDialog
from ui.feedback import (
    feedback_duration_ms,
    feedback_title,
    infer_feedback_severity,
    resolve_feedback_severity,
    safe_feedback_text,
)
from ui.widgets.toast import Toast


def test_feedback_distinguishes_contained_failure_from_hard_failure():
    assert infer_feedback_severity("订阅已刷新；热更新失败，已保留原节点") == "warning"
    assert infer_feedback_severity("刷新失败，已使用内置模型列表") == "warning"
    assert infer_feedback_severity("热更新失败，恢复原节点失败，请重新检查") == "error"
    assert infer_feedback_severity("配置写入失败") == "error"
    assert infer_feedback_severity("自动恢复本机设置未完成") == "error"
    assert infer_feedback_severity("代理端口暂未监听，未改动环境变量") == "warning"
    assert infer_feedback_severity("检测到失效本机代理，本次请求已临时直连") == "warning"


def test_feedback_resolves_legacy_error_flag_without_turning_validation_red():
    assert resolve_feedback_severity("请先选择节点", is_error=True) == "warning"
    assert resolve_feedback_severity("未知英文异常", is_error=True) == "error"
    assert resolve_feedback_severity("配置写入失败", severity="warning", is_error=True) == "warning"


def test_feedback_redacts_common_credentials_and_query_tokens():
    message = (
        "API_KEY=sk-testplaceholder123456；"
        "Authorization: Bearer abc.def.ghi；"
        "https://example.test/v1?token=query-secret-value"
    )

    safe = safe_feedback_text(message)

    assert "sk-testplaceholder123456" not in safe
    assert "abc.def.ghi" not in safe
    assert "query-secret-value" not in safe
    assert safe.count("[REDACTED]") == 3


def test_long_and_important_feedback_stays_visible_longer():
    short_success = feedback_duration_ms("已保存", "success")
    long_warning = feedback_duration_ms("需要注意：" + "详细说明" * 40, "warning")
    error = feedback_duration_ms("操作失败", "error")

    assert short_success >= 3200
    assert long_warning > short_success
    assert error >= 7200
    assert long_warning <= 11_000
    assert feedback_title("busy") == "正在处理"


def test_toast_does_not_shadow_tkinter_callback_registration():
    # tkinter.Misc._register is called while CTkToplevel itself is being
    # constructed, so defining that name in Toast breaks every real popup.
    assert "_register" not in Toast.__dict__


def test_api_result_recommendations_explain_stale_proxy_action():
    dialog = object.__new__(APITestResultDialog)
    result = SimpleNamespace(
        message="网络错误",
        proxy_warning=(
            "检测到本程序配置的失效本机代理 127.0.0.1:17897，"
            "已自动清理变量（HTTPS_PROXY），本次请求已直连"
        ),
    )

    recommendations = APITestResultDialog._get_recommendations(dialog, result)

    assert any("重开终端和 VS Code" in item for item in recommendations)
    assert any("Win11 代理页重新启动" in item for item in recommendations)
    assert not any("尝试使用代理或 VPN" in item for item in recommendations)
