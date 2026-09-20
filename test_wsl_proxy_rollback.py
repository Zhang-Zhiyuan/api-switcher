"""A failed WSL deployment attempts every owned rollback without hiding cause."""
import subprocess

import pytest

from core import wsl_proxy
from test_wsl_proxy import _completed, _target


@pytest.mark.parametrize("hooks_fail", [False, True])
def test_rule_cleanup_error_does_not_skip_new_hooks_or_hide_deployment_error(monkeypatch, hooks_fail):
    target = _target()
    actions = []
    monkeypatch.setattr(wsl_proxy, "_run_wsl_command", lambda *args, **kwargs: _completed(
        stdout="configured=1\nprofiles=.profile\ncreated=.profile\n"))
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *args, **kwargs: False)
    rule = wsl_proxy._scoped_firewall_rule_name(target, 17897)
    monkeypatch.setattr(wsl_proxy, "_ensure_scoped_firewall_rule", lambda *args: rule)

    def remove_rule(value, **kwargs):
        actions.append(("rule", value))
        raise subprocess.TimeoutExpired("synthetic-netsh", 12)

    def remove_hooks(distro, **kwargs):
        actions.append(("hooks", distro, kwargs["created_profiles"]))
        if hooks_fail:
            raise OSError("synthetic WSL unavailable")

    monkeypatch.setattr(wsl_proxy, "_delete_firewall_rule", remove_rule)
    monkeypatch.setattr(wsl_proxy, "_remove_profile_hooks", remove_hooks)
    with pytest.raises(RuntimeError) as caught:
        wsl_proxy.install_proxy_integration(target, 17897, "synthetic-mihomo.exe")
    assert actions == [("rule", rule), ("hooks", target.distro, (".profile",))]
    assert "WSL NAT" in str(caught.value)
    assert "回滚未完成" in str(caught.value)
    assert "防火墙" in str(caught.value)
    if hooks_fail:
        assert "环境入口" in str(caught.value)


def test_profile_install_error_is_not_replaced_by_cleanup_failure(monkeypatch):
    monkeypatch.setattr(wsl_proxy, "_run_wsl_command", lambda *args, **kwargs: _completed(
        returncode=1, stdout="created=.profile\n", stderr="synthetic profile write failed"))
    monkeypatch.setattr(wsl_proxy, "_remove_profile_hooks", lambda *args, **kwargs: (
        _ for _ in ()).throw(OSError("synthetic WSL cleanup unavailable")))
    with pytest.raises(RuntimeError) as caught:
        wsl_proxy.install_proxy_integration(_target(), 17897, "synthetic-mihomo.exe")
    assert "写入 WSL 代理环境入口失败" in str(caught.value)
    assert "synthetic profile write failed" in str(caught.value)
    assert "回滚未完成" in str(caught.value)


def test_compatible_hooks_stay_untouched_when_rule_rollback_fails(monkeypatch):
    target = _target()
    old_rule = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="a")
    new_rule = wsl_proxy._scoped_firewall_rule_name(target, 17897, slot="b")
    deleted = []
    monkeypatch.setattr(wsl_proxy, "_run_wsl_command", lambda *args, **kwargs: _completed(stdout="configured=1\n"))
    monkeypatch.setattr(wsl_proxy, "probe_wsl_proxy_tcp", lambda *args, **kwargs: False)
    monkeypatch.setattr(wsl_proxy, "_ensure_scoped_firewall_rule", lambda *args: new_rule)

    def fail_delete(rule, **kwargs):
        deleted.append(rule)
        raise OSError("synthetic rollback failure")

    monkeypatch.setattr(wsl_proxy, "_delete_firewall_rule", fail_delete)
    monkeypatch.setattr(wsl_proxy, "_remove_profile_hooks", lambda *args, **kwargs: pytest.fail("retain previous hooks"))
    previous = {"owner": "api-switcher", "distro": target.distro, "mixed_port": 17897,
                "guest_cidr": target.guest_cidr, "firewall_rule": old_rule}
    with pytest.raises(RuntimeError, match="WSL NAT"):
        wsl_proxy.install_proxy_integration(target, 17897, "synthetic-mihomo.exe", previous_state=previous)
    assert deleted == [new_rule]


def test_rollback_reports_nonzero_cleanup_exit_codes_instead_of_assuming_success(monkeypatch):
    actions = []
    monkeypatch.setattr(wsl_proxy, "_netsh_executable", lambda: "synthetic-netsh.exe")
    monkeypatch.setattr(wsl_proxy, "_run_windows_command", lambda *args, **kwargs: (
        actions.append("firewall") or _completed(returncode=1, stderr="synthetic access denied")))
    monkeypatch.setattr(wsl_proxy, "_run_wsl_command", lambda *args, **kwargs: (
        actions.append("hooks") or _completed(returncode=1, stderr="synthetic profile locked")))
    message = wsl_proxy._rollback_failed_proxy_integration(
        _target().distro, created_profiles=(".profile",), remove_hooks=True,
        firewall_rule=wsl_proxy._scoped_firewall_rule_name(_target(), 17897),
    )
    assert actions == ["firewall", "hooks"]
    assert "回滚未完成" in message
    assert "synthetic access denied" in message and "synthetic profile locked" in message


def test_timeout_feedback_does_not_expand_entire_cleanup_script(monkeypatch):
    monkeypatch.setattr(wsl_proxy, "_remove_profile_hooks", lambda *args, **kwargs: (
        _ for _ in ()).throw(subprocess.TimeoutExpired("private-script-" * 1000, 10)))
    message = wsl_proxy._rollback_failed_proxy_integration(
        _target().distro, created_profiles=(), remove_hooks=True,
    )
    assert "超时" in message
    assert "private-script" not in message
    assert len(message) < 150
