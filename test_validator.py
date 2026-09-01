from __future__ import annotations

import threading
from types import SimpleNamespace

from core import profile_manager, security
from core.api_tester import APITester
from core.ssh_manager import ssh_manager
from core.validator import ConfigValidator
from models.profile import CodexProfile


def test_ssh_health_checks_every_profile_with_bounded_parallelism(monkeypatch):
    profiles = [SimpleNamespace(name=f"server-{index}") for index in range(7)]
    gate = threading.Event()
    lock = threading.Lock()
    entered = 0
    checked: list[str] = []

    monkeypatch.setattr(profile_manager, "list_ssh_profiles", lambda: profiles)

    def fake_connect(profile, timeout, max_retries):
        nonlocal entered
        assert timeout == 4
        assert max_retries == 1
        with lock:
            entered += 1
            checked.append(profile.name)
            if entered >= 2:
                gate.set()
        assert gate.wait(timeout=1), "SSH health probes ran sequentially"
        return profile

    monkeypatch.setattr(ssh_manager, "connect", fake_connect)
    monkeypatch.setattr(
        ssh_manager,
        "execute_command",
        lambda client, command, timeout: ("Connection OK", ""),
    )

    validator = ConfigValidator()
    validator._validate_ssh()

    assert set(checked) == {profile.name for profile in profiles}
    result = next(item for item in validator.results if item.item == "连接测试")
    assert result.status == "ok"
    assert "7/7" in result.message
    assert "并行度 7" in result.message
    assert all("仅测试" not in item.message for item in validator.results)


def test_codex_openai_auth_health_does_not_require_provider_api_key(monkeypatch):
    profile = CodexProfile(
        name="login-relay",
        model="gpt-test",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_requires_openai_auth=True,
    )

    monkeypatch.setattr(profile_manager, "get_current_claude_name", lambda: None)
    monkeypatch.setattr(profile_manager, "get_current_codex_name", lambda: profile.name)
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [profile])
    monkeypatch.setattr(security, "get_secret", lambda _ref: None)
    monkeypatch.setattr(
        APITester,
        "test_openai_api",
        staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected API call"))),
    )

    validator = ConfigValidator()
    validator._validate_api_connections()

    result = next(item for item in validator.results if item.item == "Codex (login-relay)")
    assert result.status == "ok"
    assert "无需独立 Provider API Key" in result.message


def test_ssh_health_feedback_names_failed_profiles(monkeypatch):
    profiles = [SimpleNamespace(name="online"), SimpleNamespace(name="offline")]
    monkeypatch.setattr(profile_manager, "list_ssh_profiles", lambda: profiles)
    monkeypatch.setattr(ssh_manager, "connect", lambda profile, **_kwargs: profile)
    monkeypatch.setattr(
        ssh_manager,
        "execute_command",
        lambda client, *_args, **_kwargs: (
            ("Connection OK", "") if client.name == "online" else ("", "refused")
        ),
    )

    validator = ConfigValidator()
    validator._validate_ssh()

    result = next(item for item in validator.results if item.item == "连接测试")
    assert result.status == "warning"
    assert "1 个成功，1 个失败" in result.message
    assert "失败: offline" in result.message
