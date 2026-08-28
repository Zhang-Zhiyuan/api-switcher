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
from ui.tabs.env_tab import EnvTab
from ui.tabs.ssh_tab import SSHTab
from ui.widgets.persistent_env_control import PersistentEnvControl
from ui.widgets.proxy_quality_panel import ProxyQualityPanel
from ui.widgets.toast import Toast


class _CaptureLabel:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


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
    assert "；Authorization:" in safe


def test_feedback_redacts_proxy_credentials_short_tokens_and_subscription_paths():
    github_token = "ghp_" + "a" * 36
    message = (
        "proxy=http://alice:proxy-password@127.0.0.1:7890；"
        "GH_TOKEN=x；"
        f"raw={github_token}；"
        "https://example.test/sub/12345678-secret；"
        "https://example.test/config?signature=request-signature；重试失败"
    )

    safe = safe_feedback_text(message)

    assert "alice" not in safe
    assert "proxy-password" not in safe
    assert "GH_TOKEN=[REDACTED]" in safe
    assert github_token not in safe
    assert "12345678-secret" not in safe
    assert "request-signature" not in safe
    assert "http://[REDACTED]@127.0.0.1:7890" in safe
    assert safe.endswith("；重试失败")


def test_persistent_status_labels_redact_before_rendering():
    message = "连接失败；API_KEY=x；proxy=http://user:password@127.0.0.1:7890"

    ssh_tab = object.__new__(SSHTab)
    ssh_labels = {
        "_sync_status_label": SSHTab._set_sync_status,
        "_proxy_status_label": SSHTab._set_proxy_status,
        "_proxy_cache_label": SSHTab._set_proxy_cache_status,
        "_proxy_selected_label": SSHTab._set_proxy_selected_summary,
        "_git_login_status_label": SSHTab._set_git_login_status,
        "_remote_auto_status_label": SSHTab._set_remote_auto_status,
    }
    rendered = []
    for attribute, setter in ssh_labels.items():
        label = _CaptureLabel()
        setattr(ssh_tab, attribute, label)
        setter(ssh_tab, message, severity="error")
        rendered.append(label.config["text"])

    env_tab = object.__new__(EnvTab)
    env_tab._server_status_label = _CaptureLabel()
    EnvTab._set_server_status(env_tab, message, "error")
    rendered.append(env_tab._server_status_label.config["text"])

    env_control = object.__new__(PersistentEnvControl)
    env_control.status_label = _CaptureLabel()
    PersistentEnvControl.set_status(env_control, message, "error")
    rendered.append(env_control.status_label.config["text"])

    quality_panel = object.__new__(ProxyQualityPanel)
    quality_panel._status_label = _CaptureLabel()
    ProxyQualityPanel._set_status(quality_panel, message, "error")
    rendered.append(quality_panel._status_label.config["text"])

    assert rendered
    assert all("API_KEY=[REDACTED]" in text for text in rendered)
    assert all("http://[REDACTED]@127.0.0.1:7890" in text for text in rendered)
    assert all("user" not in text and "password" not in text for text in rendered)


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
