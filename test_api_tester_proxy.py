"""Regression tests for API tester proxy diagnostics."""

import urllib.request
import os
import threading

from core import api_tester
from core.api_tester import APITester


def test_invalid_loopback_proxy_is_detected_without_mutating_environment(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.delenv("API_SWITCHER_AI_PROXY_URL", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897", "all": "http://127.0.0.1:17897"},
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr("core.api_tester.socket.create_connection", refused)

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert "127.0.0.1:17897" in warning
    assert "临时直连" in warning


def test_non_loopback_proxy_is_not_automatically_bypassed(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://proxy.example:8080"},
    )

    assert APITester._invalid_local_proxy_warning("https://gateway.example/v1/models") == ""


def test_refused_wininet_loopback_proxy_without_env_is_bypassed(monkeypatch):
    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7897"},
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: None,
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )

    warning = APITester._invalid_local_proxy_warning(
        "https://gateway.example/v1/models"
    )

    assert "失效的 Windows 系统代理 127.0.0.1:7897" in warning
    assert "临时直连" in warning
    assert "未修改该系统代理设置" in warning


def test_confirmed_refused_wininet_proxy_only_disables_enable_switch(monkeypatch):
    from contextlib import nullcontext
    import winreg

    from core import local_proxy

    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7897"},
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: None,
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    monkeypatch.setattr(
        local_proxy,
        "_local_proxy_operation_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    values = {
        "ProxyEnable": (True, 1, winreg.REG_DWORD),
        "ProxyServer": (True, "127.0.0.1:7897", winreg.REG_SZ),
    }
    monkeypatch.setattr(
        local_proxy,
        "_read_windows_system_proxy_value",
        lambda name: values[name],
    )
    writes = []
    notifications = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(winreg, "CreateKeyEx", lambda *_args, **_kwargs: Key())
    monkeypatch.setattr(
        winreg,
        "SetValueEx",
        lambda _key, name, _reserved, value_type, value: writes.append(
            (name, value_type, value)
        ),
    )
    monkeypatch.setattr(
        local_proxy,
        "_notify_windows_proxy_change",
        lambda: notifications.append(True),
    )

    disabled = APITester.disable_invalid_windows_system_proxy()

    assert disabled == ("127.0.0.1", 7897)
    assert writes == [("ProxyEnable", winreg.REG_DWORD, 0)]
    assert notifications == [True]


def test_urlopen_bypasses_refused_loopback_proxy(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://localhost:17897"},
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:17897")

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setattr("core.api_tester.socket.create_connection", refused)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []

    class DirectOpener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: DirectOpener())
    request = urllib.request.Request("https://gateway.example/v1/models")

    response, warning = APITester._urlopen(request, timeout=7)

    assert isinstance(response, Response)
    assert calls == [(request.full_url, 7)]
    assert "localhost:17897" in warning


def test_request_keeps_direct_bypass_after_precheck_auto_cleanup(monkeypatch):
    checks = []
    direct_calls = []

    def warning(_cls, _url):
        checks.append(True)
        return "已自动清理失效本机代理，本次请求已直连" if len(checks) == 1 else ""

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_size=-1):
            return b"{}"

        @staticmethod
        def getcode():
            return 200

    class DirectOpener:
        def open(self, request, timeout):
            direct_calls.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(APITester, "_invalid_local_proxy_warning", classmethod(warning))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: DirectOpener())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must preserve direct bypass")
        ),
    )

    ok, payload, result = APITester._request_json(
        "https://gateway.example/v1/models",
        {},
        timeout=3,
    )

    assert ok is True
    assert payload == {}
    assert result.proxy_warning
    assert len(checks) == 1
    assert direct_calls == [("https://gateway.example/v1/models", 3)]


def test_loopback_proxy_probe_cache_is_capacity_bounded(monkeypatch):
    class ConnectedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(api_tester.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: ConnectedSocket(),
    )

    for port in range(20000, 20000 + APITester.MAX_PROXY_CHECK_CACHE_ENTRIES + 20):
        endpoint, available = APITester._check_loopback_proxy(
            f"http://127.0.0.1:{port}",
            force=True,
        )
        assert endpoint == ("127.0.0.1", port)
        assert available is True

    assert len(APITester._proxy_check_cache) == APITester.MAX_PROXY_CHECK_CACHE_ENTRIES
    APITester._proxy_check_cache.clear()


def test_invalid_local_proxy_env_names_only_returns_refused_loopback_values(monkeypatch):
    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:17897")

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(api_tester.socket, "create_connection", refused)

    names = APITester.invalid_local_proxy_env_names()

    assert set(names) == {"HTTPS_PROXY", "ALL_PROXY"}
    assert os.environ["HTTP_PROXY"] == "http://proxy.example:8080"


def test_clear_invalid_local_proxy_env_changes_only_process_values(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setattr(
        APITester,
        "invalid_local_proxy_env_names",
        classmethod(lambda _cls, **_kwargs: ("HTTPS_PROXY",)),
    )
    monkeypatch.setattr("core.persistent_env._local_user_env_value_strict", lambda _name: None)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")

    removed = APITester.clear_invalid_local_proxy_env()

    assert removed == ("HTTPS_PROXY",)
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["HTTP_PROXY"] == "http://proxy.example:8080"


def test_clear_invalid_local_proxy_env_deletes_windows_user_values(monkeypatch):
    APITester._proxy_check_cache.clear()
    deleted = []
    monkeypatch.setattr(
        APITester,
        "invalid_local_proxy_env_names",
        classmethod(lambda _cls, **_kwargs: ("HTTPS_PROXY", "ALL_PROXY")),
    )
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: "http://127.0.0.1:17897",
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    monkeypatch.setattr(
        "core.persistent_env.delete_local_user_env",
        lambda names: deleted.append(tuple(names)),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:17897")

    removed = APITester.clear_invalid_local_proxy_env()

    assert removed == ("HTTPS_PROXY", "ALL_PROXY")
    assert deleted == [("HTTPS_PROXY", "ALL_PROXY")]
    assert "HTTPS_PROXY" not in os.environ
    assert "ALL_PROXY" not in os.environ


def test_invalid_local_proxy_written_by_app_is_auto_cleaned(monkeypatch):
    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(api_tester.socket, "create_connection", refused)
    monkeypatch.setattr(
        "core.local_proxy._load_state",
        lambda: {
            "mixed_port": 17897,
            "proxy_url": "http://127.0.0.1:17897",
            "previous_env": {},
            "managed_proxy_env": {
                "owner": "api-switcher",
                "proxy_url": "http://127.0.0.1:17897",
                "variables": ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            },
        },
    )
    monkeypatch.setattr("core.persistent_env._local_user_env_value_strict", lambda _name: None)
    deleted = []
    monkeypatch.setattr(
        "core.persistent_env.delete_local_user_env",
        lambda names: deleted.append(tuple(names)),
    )

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert "已自动清理变量" in warning
    assert "HTTPS_PROXY" not in os.environ
    assert deleted == []


def test_owned_dead_proxy_restores_all_managed_settings_before_request(monkeypatch):
    from contextlib import nullcontext

    from core import local_proxy

    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    state = {
        "mixed_port": 17897,
        "proxy_url": "http://127.0.0.1:17897",
        "managed_proxy_env": {
            "owner": "api-switcher",
            "proxy_url": "http://127.0.0.1:17897",
            "variables": list(local_proxy.remote_proxy.PROXY_ENV_KEYS),
        },
    }
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)
    monkeypatch.setattr(
        local_proxy,
        "_local_proxy_operation_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        local_proxy,
        "reconcile_local_ai_proxy_startup_settings",
        lambda: (
            "检测到本工具上次代理已退出，已自动恢复它写入的 Windows 环境变量、"
            "VS Code 和系统代理设置；已打开的终端需重开"
        ),
    )
    monkeypatch.setattr(
        APITester,
        "clear_invalid_local_proxy_env",
        classmethod(
            lambda _cls, *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("full checkpoint restore must run first")
            )
        ),
    )

    warning = APITester._invalid_local_proxy_warning(
        "https://gateway.example/v1/models"
    )

    assert "已自动恢复本程序启动前保存的 Windows 环境变量" in warning
    assert "VS Code 和系统代理设置" in warning


def test_owned_dead_system_proxy_restores_checkpoint_without_proxy_env(monkeypatch):
    from contextlib import nullcontext

    from core import local_proxy

    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: None,
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    state = {
        "mixed_port": 17897,
        "proxy_url": "http://127.0.0.1:17897",
        "managed_proxy_env": {
            "owner": "api-switcher",
            "proxy_url": "http://127.0.0.1:17897",
            "variables": list(local_proxy.remote_proxy.PROXY_ENV_KEYS),
        },
    }
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _state: False)
    monkeypatch.setattr(
        local_proxy,
        "_local_proxy_operation_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    reconciled = []
    monkeypatch.setattr(
        local_proxy,
        "reconcile_local_ai_proxy_startup_settings",
        lambda: reconciled.append(True) or (
            "检测到本工具上次代理已退出，已自动恢复它写入的 Windows 环境变量、"
            "VS Code 和系统代理设置；已打开的终端需重开"
        ),
    )

    warning = APITester._invalid_local_proxy_warning(
        "https://gateway.example/v1/models"
    )

    assert reconciled == [True]
    assert "已自动恢复本程序启动前保存的 Windows 环境变量" in warning
    assert "VS Code 和系统代理设置" in warning


def test_unknown_invalid_local_proxy_is_not_auto_cleaned(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.delenv("API_SWITCHER_AI_PROXY_URL", raising=False)
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError("connection refused")),
    )
    monkeypatch.setattr("core.local_proxy._load_state", lambda: {})

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert "已自动清理变量" not in warning
    assert "临时直连" in warning
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:17897"


def test_invalid_proxy_uses_app_markers_when_state_file_is_missing(monkeypatch, tmp_path):
    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setenv("API_SWITCHER_AI_PROXY_URL", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError("connection refused")),
    )
    monkeypatch.setattr("core.local_proxy._load_state", lambda: {})
    monkeypatch.setattr("core.persistent_env._local_user_env_value_strict", lambda _name: None)
    monkeypatch.setattr("core.local_proxy.LOCAL_PROXY_CONFIG_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("# Managed by API切换器 AI proxy\n", encoding="utf-8")
    deleted = []
    monkeypatch.setattr(
        "core.persistent_env.delete_local_user_env",
        lambda names: deleted.append(tuple(names)),
    )

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert "已自动清理变量" in warning
    assert deleted == []


def test_cleanup_does_not_delete_a_newer_persistent_user_proxy(monkeypatch):
    APITester._proxy_check_cache.clear()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        APITester,
        "invalid_local_proxy_env_names",
        classmethod(lambda _cls, **_kwargs: ("HTTPS_PROXY",)),
    )
    monkeypatch.setattr(
        "core.persistent_env._local_user_env_value_strict",
        lambda _name: "http://127.0.0.1:18888",
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    deleted = []
    monkeypatch.setattr(
        "core.persistent_env.delete_local_user_env",
        lambda names: deleted.append(tuple(names)),
    )

    removed = APITester.clear_invalid_local_proxy_env()

    assert removed == ("HTTPS_PROXY",)
    assert "HTTPS_PROXY" not in os.environ
    assert deleted == []


def test_owned_proxy_is_not_auto_cleaned_while_lifecycle_operation_holds_lock(monkeypatch):
    from core import local_proxy

    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    monkeypatch.setattr(
        api_tester.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    state = {
        "mixed_port": 17897,
        "proxy_url": "http://127.0.0.1:17897",
        "previous_env": {},
        "managed_proxy_env": {
            "owner": "api-switcher",
            "proxy_url": "http://127.0.0.1:17897",
            "variables": ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
        },
    }
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr("core.persistent_env._local_user_env_value_strict", lambda _name: None)
    deleted = []
    monkeypatch.setattr(
        "core.persistent_env.delete_local_user_env",
        lambda names: deleted.append(tuple(names)),
    )
    ready = threading.Event()
    release = threading.Event()

    def hold_lifecycle_lock():
        with local_proxy._local_proxy_operation_lock("test-reload", timeout=1):
            ready.set()
            release.wait(timeout=3)

    worker = threading.Thread(target=hold_lifecycle_lock, daemon=True)
    worker.start()
    assert ready.wait(timeout=2)
    try:
        warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")
    finally:
        release.set()
        worker.join(timeout=3)

    assert "未清理环境变量" in warning
    assert "HTTPS_PROXY" in os.environ
    assert deleted == []


def test_cached_refusal_is_rechecked_before_auto_cleanup(monkeypatch):
    APITester._proxy_check_cache.clear()
    for name in APITester.LOCAL_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setattr(api_tester.os, "name", "nt")
    monkeypatch.setattr(
        api_tester.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:17897"},
    )
    calls = {"count": 0}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def refused_then_recovered(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionRefusedError()
        return Connection()

    monkeypatch.setattr(api_tester.socket, "create_connection", refused_then_recovered)
    monkeypatch.setattr(
        "core.local_proxy._load_state",
        lambda: {
            "mixed_port": 17897,
            "proxy_url": "http://127.0.0.1:17897",
            "previous_env": {},
            "managed_proxy_env": {
                "owner": "api-switcher",
                "proxy_url": "http://127.0.0.1:17897",
                "variables": ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            },
        },
    )

    warning = APITester._invalid_local_proxy_warning("https://gateway.example/v1/models")

    assert warning == ""
    assert calls["count"] >= 2
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:17897"
