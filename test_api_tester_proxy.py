"""Regression tests for API tester proxy diagnostics."""

import urllib.request
import os

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
    monkeypatch.setattr(APITester, "invalid_local_proxy_env_names", classmethod(lambda _cls: ("HTTPS_PROXY",)))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")

    removed = APITester.clear_invalid_local_proxy_env()

    assert removed == ("HTTPS_PROXY",)
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["HTTP_PROXY"] == "http://proxy.example:8080"


def test_clear_invalid_local_proxy_env_deletes_windows_user_values(monkeypatch):
    APITester._proxy_check_cache.clear()
    deleted = []
    monkeypatch.setattr(APITester, "invalid_local_proxy_env_names", classmethod(lambda _cls: ("HTTPS_PROXY", "ALL_PROXY")))
    monkeypatch.setattr(api_tester.os, "name", "nt")
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
    assert deleted == [("HTTPS_PROXY",)]


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
    assert deleted == [("HTTPS_PROXY",)]
