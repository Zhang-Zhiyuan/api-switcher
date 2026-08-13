from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import core.browser_launcher as browser_launcher_module
from core.browser_launcher import BrowserLauncher
from models.profile import BrowserProfile


@pytest.fixture
def launch_browser(tmp_path, monkeypatch):
    from core import local_proxy

    executable = tmp_path / "chrome.exe"
    executable.touch()
    user_data_dir = tmp_path / "profile"
    user_data_dir.mkdir()

    launcher = BrowserLauncher()
    monkeypatch.setattr(
        launcher,
        "find_browser_executable",
        lambda _browser_type, _explicit_path=None: executable,
    )
    monkeypatch.setattr(browser_launcher_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        lambda: False,
    )

    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(browser_launcher_module.subprocess, "Popen", popen)

    def run(*, profile_mode="managed"):
        profile = BrowserProfile(
            name="test",
            browser_type="chrome",
            profile_mode=profile_mode,
            user_data_dir=str(user_data_dir),
        )
        launcher.launch(profile)
        return popen.call_args.args[0]

    return run


def _proxy_flags(command):
    return [
        argument
        for argument in command
        if argument.startswith("--proxy-server=")
        or argument.startswith("--host-resolver-rules=")
        or argument.startswith("--force-webrtc-ip-handling-policy=")
        or argument == "--disable-quic"
    ]


def test_managed_browser_uses_running_strict_local_proxy(launch_browser, monkeypatch):
    from core import local_proxy
    from core.browser_data_manager import browser_data_manager

    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        lambda: True,
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=True,
            strict_privacy_active=True,
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(browser_data_manager, "is_browser_running", lambda _profile: False)

    command = launch_browser()

    assert "--proxy-server=http://127.0.0.1:17897" in command
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in command
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in command
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" not in command
    assert "--disable-quic" in command
    assert command[-1] == "https://chatgpt.com/"


def test_managed_browser_keeps_existing_behavior_when_strict_config_is_inactive(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    inspect = Mock(
        return_value=SimpleNamespace(
            running=True,
            strict_privacy_active=False,
            proxy_url="http://127.0.0.1:17897",
        )
    )
    monkeypatch.setattr(local_proxy, "inspect_local_ai_proxy", inspect)

    command = launch_browser()

    assert _proxy_flags(command) == []
    inspect.assert_called_once_with()


def test_managed_browser_keeps_existing_behavior_when_proxy_is_not_running(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=False,
            strict_privacy_active=True,
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    assert _proxy_flags(launch_browser()) == []


def test_managed_browser_keeps_existing_behavior_when_proxy_inspection_fails(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        Mock(side_effect=RuntimeError("state unavailable")),
    )

    assert _proxy_flags(launch_browser()) == []


def test_managed_browser_rejects_non_loopback_proxy_status(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=True,
            strict_privacy_active=True,
            proxy_url="http://192.0.2.1:17897",
        ),
    )

    with pytest.raises(RuntimeError, match="代理地址校验失败"):
        launch_browser()


def test_external_browser_does_not_receive_managed_proxy_flags(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    authority = Mock()
    inspect = Mock()
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        authority,
    )
    monkeypatch.setattr(local_proxy, "inspect_local_ai_proxy", inspect)

    command = launch_browser(profile_mode="external")

    assert _proxy_flags(command) == []
    authority.assert_not_called()
    inspect.assert_not_called()


def test_managed_browser_fails_closed_if_strict_is_enabled_during_inspection(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    authority = Mock(side_effect=[False, True])
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        authority,
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=False,
            strict_privacy_active=False,
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    with pytest.raises(RuntimeError, match="受管代理未运行"):
        launch_browser()

    assert authority.call_count == 2
    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_managed_browser_rejects_desired_strict_mode_after_failed_apply(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=True,
            strict_privacy_desired=True,
            strict_privacy_active=False,
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    with pytest.raises(RuntimeError, match="未通过已应用严格配置校验"):
        launch_browser()

    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_managed_browser_rejects_when_authoritative_strict_proxy_is_stopped(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        lambda: True,
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=False,
            strict_privacy_active=False,
            proxy_url="http://127.0.0.1:17897",
        ),
    )

    with pytest.raises(RuntimeError, match="受管代理未运行"):
        launch_browser()

    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_managed_browser_rejects_when_strict_status_inspection_fails(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        lambda: True,
    )
    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        Mock(side_effect=RuntimeError("state unavailable")),
    )

    with pytest.raises(RuntimeError, match="无法验证本机受管代理状态"):
        launch_browser()

    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_managed_browser_rejects_when_strict_preference_authority_is_unreadable(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    authority = Mock(side_effect=RuntimeError("corrupt preferences"))
    inspect = Mock()
    monkeypatch.setattr(
        local_proxy,
        "local_proxy_strict_privacy_desired_authoritative",
        authority,
    )
    monkeypatch.setattr(local_proxy, "inspect_local_ai_proxy", inspect)

    with pytest.raises(RuntimeError, match="无法确认.*严格隐私偏好"):
        launch_browser()

    inspect.assert_not_called()
    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_managed_browser_treats_legacy_status_without_active_flag_as_non_strict(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(running=True, proxy_url="http://127.0.0.1:17897"),
    )

    assert _proxy_flags(launch_browser()) == []


def test_strict_managed_browser_rejects_profile_owned_by_existing_process(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy
    from core.browser_data_manager import browser_data_manager

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=True,
            strict_privacy_active=True,
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    monkeypatch.setattr(browser_data_manager, "is_browser_running", lambda _profile: True)

    with pytest.raises(RuntimeError, match="Profile 仍被 Chrome/Edge 进程占用"):
        launch_browser()

    browser_launcher_module.subprocess.Popen.assert_not_called()


def test_strict_managed_browser_launches_when_profile_lock_is_free(
    launch_browser,
    monkeypatch,
):
    from core import local_proxy
    from core.browser_data_manager import browser_data_manager

    monkeypatch.setattr(
        local_proxy,
        "inspect_local_ai_proxy",
        lambda: SimpleNamespace(
            running=True,
            strict_privacy_active=True,
            proxy_url="http://127.0.0.1:17897",
        ),
    )
    lock_check = Mock(return_value=False)
    monkeypatch.setattr(browser_data_manager, "is_browser_running", lock_check)

    command = launch_browser()

    lock_check.assert_called_once()
    assert "--proxy-server=http://127.0.0.1:17897" in command
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in command
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in command
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" not in command
    assert "--disable-quic" in command
