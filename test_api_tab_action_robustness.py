from types import SimpleNamespace

import pytest

from core import api_tester, security
from core.providers import ProviderRegistry
from ui.dialogs import api_test_result_dialog
from ui.tabs import claude_tab, codex_tab


class _FakeButton:
    def __init__(self):
        self.state = "normal"
        self.text = "测试"

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)
        self.text = kwargs.get("text", self.text)


class _DeferredThread:
    instances = []

    def __init__(self, *, target, **_kwargs):
        self.target = target
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


def _api_tab_cases():
    return (
        (
            claude_tab,
            claude_tab.ClaudeTab,
            "list_switchable_claude_profiles",
            SimpleNamespace(
                name="demo",
                auth_token_ref="claude:demo:token",
                primary_api_key_ref=None,
                base_url="https://mock.invalid",
                model="mock-claude",
            ),
        ),
        (
            codex_tab,
            codex_tab.CodexTab,
            "list_switchable_codex_profiles",
            SimpleNamespace(
                name="demo",
                api_key_ref="codex:demo:key",
                custom_base_url="https://mock.invalid",
                model="mock-codex",
                model_provider="mock",
            ),
        ),
    )


@pytest.mark.parametrize("module,tab_class,list_method,profile", _api_tab_cases())
def test_api_profile_test_blocks_duplicates_and_restores_button(
    monkeypatch,
    module,
    tab_class,
    list_method,
    profile,
):
    button = _FakeButton()
    toasts = []
    dialogs = []
    tab = object.__new__(tab_class)
    tab._profile_tests_inflight = set()
    tab._profile_test_buttons = {profile.name: button}
    tab._destroyed = False
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: "root"

    monkeypatch.setattr(module.profile_manager, list_method, lambda: [profile])
    monkeypatch.setattr(security, "get_secret", lambda _ref: "mock-secret")
    monkeypatch.setattr(module, "show_toast", lambda *args, **kwargs: toasts.append((args, kwargs)))
    _DeferredThread.instances = []
    monkeypatch.setattr(module.threading, "Thread", _DeferredThread)

    tab._test_profile(profile.name)
    tab._test_profile(profile.name)

    assert len(_DeferredThread.instances) == 1
    assert tab._profile_tests_inflight == {profile.name}
    assert button.state == "disabled"
    assert button.text == "测试中"
    assert any("请勿重复点击" in args[1] for args, _kwargs in toasts)

    # The test request and result dialog are fully mocked; no API call occurs.
    monkeypatch.setattr(api_tester.APITester, "test_claude_api", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_tester.APITester, "benchmark_openai_wire_apis", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ProviderRegistry, "get_provider", lambda _name: None)
    monkeypatch.setattr(
        api_test_result_dialog,
        "APITestResultDialog",
        lambda *args, **kwargs: dialogs.append((args, kwargs)),
    )

    def dispatch(_widget, callback):
        callback()
        return True

    monkeypatch.setattr(module, "run_on_ui_thread", dispatch)
    _DeferredThread.instances[0].target()

    assert tab._profile_tests_inflight == set()
    assert button.state == "normal"
    assert button.text == "测试"
    assert len(dialogs) == 1


@pytest.mark.parametrize("module,tab_class,list_method,profile", _api_tab_cases())
def test_api_profile_test_thread_start_failure_rolls_back_state(
    monkeypatch,
    module,
    tab_class,
    list_method,
    profile,
):
    class StartFailThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    button = _FakeButton()
    toasts = []
    tab = object.__new__(tab_class)
    tab._profile_tests_inflight = set()
    tab._profile_test_buttons = {profile.name: button}
    tab._destroyed = False
    tab.winfo_toplevel = lambda: "root"

    monkeypatch.setattr(module.profile_manager, list_method, lambda: [profile])
    monkeypatch.setattr(security, "get_secret", lambda _ref: "mock-secret")
    monkeypatch.setattr(module, "show_toast", lambda *args, **kwargs: toasts.append((args, kwargs)))
    monkeypatch.setattr(module.threading, "Thread", StartFailThread)

    tab._test_profile(profile.name)

    assert tab._profile_tests_inflight == set()
    assert button.state == "normal"
    assert button.text == "测试"
    assert "无法启动测试任务" in toasts[-1][0][1]
    assert toasts[-1][1]["is_error"] is True


@pytest.mark.parametrize("module,tab_class,_list_method,_profile", _api_tab_cases())
def test_api_tab_refresh_thread_start_failure_replaces_loading_state(
    monkeypatch,
    module,
    tab_class,
    _list_method,
    _profile,
):
    class StartFailThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    tab = object.__new__(tab_class)
    tab._cards_frame = object()
    tab._account_cards_frame = object()
    tab._refresh_generation = 0
    tab._cancel_profile_render = lambda: None
    loading = []
    errors = []
    tab._show_refresh_loading = lambda: loading.append(True)
    tab._show_refresh_error = errors.append
    monkeypatch.setattr(module.threading, "Thread", StartFailThread)

    tab_class.refresh(tab)

    assert loading == [True]
    assert errors == ["刷新任务启动失败: thread unavailable"]
