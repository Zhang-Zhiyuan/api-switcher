from types import SimpleNamespace

import pytest

from core import api_tester, security
from core.providers import ProviderRegistry
from models.profile import ClaudeProfile, CodexProfile
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
def test_api_profile_test_redacts_secret_from_unexpected_worker_error(
    monkeypatch,
    module,
    tab_class,
    list_method,
    profile,
):
    secret = "worker-echoed-secret"
    tab = object.__new__(tab_class)
    tab._profile_tests_inflight = set()
    tab._profile_test_buttons = {profile.name: _FakeButton()}
    tab._destroyed = False
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: "root"
    if module is claude_tab:
        profile.auth_scheme = "auth_token"

    monkeypatch.setattr(module.profile_manager, list_method, lambda: [profile])
    monkeypatch.setattr(security, "get_secret", lambda _ref: secret)
    monkeypatch.setattr(module, "show_toast", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api_tester.APITester,
        "test_claude_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(f"echo {secret}")),
    )
    monkeypatch.setattr(
        api_tester.APITester,
        "benchmark_openai_wire_apis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(f"echo {secret}")),
    )
    monkeypatch.setattr(ProviderRegistry, "get_provider", lambda _name: None)
    captured = []
    monkeypatch.setattr(
        api_test_result_dialog,
        "APITestResultDialog",
        lambda _parent, result, _name: captured.append(result),
    )
    monkeypatch.setattr(module, "run_on_ui_thread", lambda _widget, callback: callback() or True)
    _DeferredThread.instances = []
    monkeypatch.setattr(module.threading, "Thread", _DeferredThread)

    tab._test_profile(profile.name)
    _DeferredThread.instances[0].target()

    assert len(captured) == 1
    assert secret not in (captured[0].error_details or "")
    assert "[REDACTED]" in (captured[0].error_details or "")


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


def _profile_edit_test_tab(tab_class):
    tab = object.__new__(tab_class)
    tab.winfo_toplevel = lambda: "root"
    tab.refresh = lambda: None
    tab._refresh_shell_state = lambda: None
    return tab


def test_claude_create_and_edit_defer_secrets_to_profile_transaction(monkeypatch):
    old_profile = ClaudeProfile(
        name="ClaudeOld",
        auth_token_ref="claude:ClaudeOld:auth_token",
        primary_api_key_ref="claude:ClaudeOld:primary_api_key",
        base_url="https://old.invalid",
        provider="custom",
    )
    transactions = []
    monkeypatch.setattr(
        claude_tab.profile_manager,
        "list_switchable_claude_profiles",
        lambda: [old_profile],
    )
    monkeypatch.setattr(
        claude_tab.profile_manager,
        "save_claude_profile_with_secrets",
        lambda profile, updates, previous_name=None: transactions.append(
            (profile, updates, previous_name)
        ),
    )
    monkeypatch.setattr(claude_tab, "show_toast", lambda *_args, **_kwargs: None)

    def fake_editor(*_args, **kwargs):
        editing = kwargs.get("profile") is not None
        kwargs["on_save"]({
            "name": "ClaudeRenamed" if editing else "ClaudeNew",
            "auth_token": "edited-secret" if editing else "new-secret",
            "base_url": "https://new.invalid",
            "model": "claude-test",
            "effort_level": "high",
            "permissions_mode": "default",
            "skip_dangerous_prompt": False,
            "provider": "custom",
            "custom_provider_name": None,
        }, kwargs.get("profile"))

    monkeypatch.setattr(claude_tab, "ProfileEditorDialog", fake_editor)
    tab = _profile_edit_test_tab(claude_tab.ClaudeTab)

    claude_tab.ClaudeTab._edit_profile(tab, "ClaudeOld")
    claude_tab.ClaudeTab._create_profile(tab)

    assert transactions[0][1] == {
        "claude:ClaudeOld:auth_token": "edited-secret",
    }
    assert transactions[0][0].primary_api_key_ref is None
    assert transactions[0][2] == "ClaudeOld"
    assert transactions[1][1] == {
        "claude:ClaudeNew:auth_token": "new-secret",
    }
    assert transactions[1][0].primary_api_key_ref is None
    assert transactions[1][2] is None


def test_codex_create_and_edit_defer_secrets_to_profile_transaction(monkeypatch):
    old_profile = CodexProfile(
        name="CodexOld",
        api_key_ref="codex:CodexOld:api_key",
        model_provider="custom",
        disable_response_storage=False,
    )
    transactions = []
    monkeypatch.setattr(
        codex_tab.profile_manager,
        "list_switchable_codex_profiles",
        lambda: [old_profile],
    )
    monkeypatch.setattr(
        codex_tab.profile_manager,
        "save_codex_profile_with_secrets",
        lambda profile, updates, previous_name=None: transactions.append(
            (profile, updates, previous_name)
        ),
    )
    monkeypatch.setattr(codex_tab, "show_toast", lambda *_args, **_kwargs: None)

    def fake_editor(*_args, **kwargs):
        editing = kwargs.get("profile") is not None
        kwargs["on_save"]({
            "name": "CodexRenamed" if editing else "CodexNew",
            "api_key": "edited-secret" if editing else "new-secret",
            "model": "codex-test",
            "model_provider": "custom",
            "model_reasoning_effort": "high",
            "custom_base_url": "https://new.invalid/v1",
            "custom_name": "Custom",
            "custom_wire_api": "responses",
            "custom_env_key": "OPENAI_API_KEY",
            "custom_requires_openai_auth": False,
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
        }, kwargs.get("profile"))

    monkeypatch.setattr(codex_tab, "ProfileEditorDialog", fake_editor)
    tab = _profile_edit_test_tab(codex_tab.CodexTab)

    codex_tab.CodexTab._edit_profile(tab, "CodexOld")
    codex_tab.CodexTab._create_profile(tab)

    assert transactions[0][1] == {
        "codex:CodexRenamed:api_key": "edited-secret",
    }
    assert transactions[0][2] == "CodexOld"
    assert transactions[0][0].disable_response_storage is False
    assert transactions[1][1] == {
        "codex:CodexNew:api_key": "new-secret",
    }
    assert transactions[1][2] is None


@pytest.mark.parametrize(
    "module,tab_class,import_method,legacy_methods",
    [
        (
            claude_tab,
            claude_tab.ClaudeTab,
            "import_current_claude",
            ("save_claude_profile", "set_active_claude", "set_active_claude_account"),
        ),
        (
            codex_tab,
            codex_tab.CodexTab,
            "import_current_codex",
            ("save_codex_profile", "set_active_codex", "set_active_codex_account"),
        ),
    ],
)
def test_api_import_ui_relies_on_single_core_transaction(
    monkeypatch,
    module,
    tab_class,
    import_method,
    legacy_methods,
):
    profile = SimpleNamespace(name="Imported")
    monkeypatch.setattr(module.profile_manager, import_method, lambda: profile)
    for method in legacy_methods:
        monkeypatch.setattr(
            module.profile_manager,
            method,
            lambda *_args, _method=method, **_kwargs: pytest.fail(f"duplicate call: {_method}"),
        )
    toasts = []
    monkeypatch.setattr(module, "show_toast", lambda *_args, **_kwargs: toasts.append((_args, _kwargs)))
    tab = _profile_edit_test_tab(tab_class)
    refreshed = []
    tab.refresh = lambda: refreshed.append("profiles")
    tab._refresh_shell_state = lambda: refreshed.append("shell")

    tab_class._import_current(tab)

    assert len(toasts) == 1
    assert refreshed == ["profiles", "shell"]


def test_claude_account_import_ui_does_not_resave_transactional_import(monkeypatch):
    profile = SimpleNamespace(name="Official")
    monkeypatch.setattr(claude_tab.profile_manager, "import_current_claude_account", lambda: profile)
    monkeypatch.setattr(
        claude_tab.profile_manager,
        "save_claude_account_profile",
        lambda *_args, **_kwargs: pytest.fail("duplicate Claude account save"),
    )
    monkeypatch.setattr(claude_tab.profile_manager, "get_current_claude_account_name", lambda: profile.name)
    active = []
    monkeypatch.setattr(claude_tab.profile_manager, "set_active_claude_account", active.append)
    monkeypatch.setattr(claude_tab, "show_toast", lambda *_args, **_kwargs: None)
    tab = _profile_edit_test_tab(claude_tab.ClaudeTab)

    claude_tab.ClaudeTab._import_current_account(tab)

    assert active == [profile.name]
