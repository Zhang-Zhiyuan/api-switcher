from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import local_proxy, remote_proxy


def _node() -> dict:
    return {
        "name": "test-node",
        "type": "vless",
        "server": "proxy.example.com",
        "port": 443,
        "uuid": "00000000-0000-0000-0000-000000000001",
    }


def test_local_proxy_operation_lock_is_reentrant_and_released_after_error(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path / "local_ai_proxy")

    with local_proxy._local_proxy_operation_lock("outer", timeout=0.5):
        with local_proxy._local_proxy_operation_lock("inner", timeout=0.5):
            assert local_proxy._LOCAL_PROXY_OPERATION_CONTEXT.depth == 2

    with pytest.raises(ValueError, match="boom"):
        with local_proxy._local_proxy_operation_lock("failing", timeout=0.5):
            raise ValueError("boom")

    with local_proxy._local_proxy_operation_lock("after-error", timeout=0.5):
        assert local_proxy._LOCAL_PROXY_OPERATION_CONTEXT.depth == 1


def test_local_proxy_operation_lock_blocks_a_second_process(monkeypatch, tmp_path):
    data_dir = tmp_path / "shared-data"
    proxy_dir = data_dir / "local_ai_proxy"
    ready_path = tmp_path / "child-ready"
    release_path = tmp_path / "child-release"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", proxy_dir)
    repo_root = Path(local_proxy.__file__).resolve().parent.parent
    child_code = "\n".join(
        (
            "import sys, time",
            "from pathlib import Path",
            "from core import local_proxy",
            "ready, release = Path(sys.argv[1]), Path(sys.argv[2])",
            "with local_proxy._local_proxy_operation_lock('child', timeout=2):",
            "    ready.write_text('ready', encoding='utf-8')",
            "    deadline = time.monotonic() + 10",
            "    while not release.exists() and time.monotonic() < deadline:",
            "        time.sleep(0.02)",
        )
    )
    env = os.environ.copy()
    env["API_SWITCHER_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(ready_path), str(release_path)],
        cwd=str(repo_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready_path.exists():
            _stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"child did not acquire proxy operation lock: {stderr}")

        with pytest.raises(RuntimeError, match="另一个 API切换器实例"):
            with local_proxy._local_proxy_operation_lock("parent", timeout=0.15):
                pytest.fail("second process unexpectedly acquired the proxy operation lock")
    finally:
        release_path.write_text("release", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)

    assert process.returncode == 0


def test_install_publishes_initial_config_atomically(monkeypatch, tmp_path):
    proxy_dir = tmp_path / "local_ai_proxy"
    config_dir = proxy_dir / "mihomo"
    config_dir.mkdir(parents=True)
    writes = []
    real_atomic_write = local_proxy.atomic_write_text

    def tracked_atomic_write(path, content, encoding="utf-8"):
        writes.append(Path(path))
        real_atomic_write(path, content, encoding=encoding)

    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", proxy_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", config_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PREFS_PATH", proxy_dir / "preferences.json")
    monkeypatch.setattr(local_proxy, "atomic_write_text", tracked_atomic_write)
    local_proxy.clear_local_proxy_preferences_cache()
    local_proxy.save_local_proxy_preferences(strict_privacy=False)
    monkeypatch.setattr(local_proxy, "_select_local_mixed_port", lambda _port: 17897)
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: tmp_path / "mihomo.exe")
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_save_state", lambda _state: None)
    monkeypatch.setattr(local_proxy, "_capture_previous_env", lambda: {})
    monkeypatch.setattr(local_proxy, "_capture_vscode_proxy_state", lambda _settings: {})
    monkeypatch.setattr(local_proxy, "_capture_windows_system_proxy_state", lambda: {})
    monkeypatch.setattr(local_proxy.vscode_parser, "read_vscode_settings", lambda: {})
    monkeypatch.setattr(local_proxy, "_start_local_mihomo", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(local_proxy, "_apply_local_env", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_apply_local_vscode_proxy", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_apply_windows_system_proxy", lambda _port: None)
    monkeypatch.setattr(local_proxy, "_save_last_proxy_node", lambda _node: None)

    local_proxy.install_local_ai_proxy(remote_proxy.format_proxy_node(_node()))

    assert config_dir / "config.yaml" in writes


def test_start_publishes_managed_pid_atomically(monkeypatch, tmp_path):
    proxy_dir = tmp_path / "local_ai_proxy"
    log_path = proxy_dir / "mihomo.log"
    pid_path = proxy_dir / "mihomo.pid"
    writes = []
    listen_results = iter((False, True))

    class FakeProcess:
        pid = 9876

        @staticmethod
        def poll():
            return None

    real_atomic_write = local_proxy.atomic_write_text

    def write_with_real_helper(path, content, encoding="utf-8"):
        writes.append((Path(path), str(content)))
        real_atomic_write(path, content, encoding=encoding)

    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", proxy_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", proxy_dir / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_LOG_PATH", log_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", pid_path)
    monkeypatch.setattr(local_proxy, "atomic_write_text", write_with_real_helper)
    monkeypatch.setattr(local_proxy, "_validate_local_mihomo_config", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: next(listen_results))
    monkeypatch.setattr(local_proxy.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(local_proxy.time, "sleep", lambda _seconds: None)

    local_proxy._start_local_mihomo(tmp_path / "isolated-mihomo.exe", 17897)

    assert (pid_path, "9876") in writes
    assert pid_path.read_text(encoding="utf-8") == "9876"


def test_failed_start_cleans_new_process_and_pid_file(monkeypatch, tmp_path):
    proxy_dir = tmp_path / "local_ai_proxy"
    pid_path = proxy_dir / "mihomo.pid"
    listen_results = iter([False] * 11)
    terminated = []

    class FakeProcess:
        pid = 9876

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", proxy_dir)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", proxy_dir / "mihomo")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_LOG_PATH", proxy_dir / "mihomo.log")
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_PID_PATH", pid_path)
    monkeypatch.setattr(local_proxy, "_validate_local_mihomo_config", lambda *_args: None)
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: int(pid_path.read_text()) if pid_path.exists() else None)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: next(listen_results))
    monkeypatch.setattr(local_proxy.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(local_proxy.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        local_proxy,
        "_terminate_pid",
        lambda pid: terminated.append(pid) or True,
    )

    with pytest.raises(RuntimeError, match="端口 17897 未监听"):
        local_proxy._start_local_mihomo(tmp_path / "isolated-mihomo.exe", 17897)

    assert terminated == [9876]
    assert not pid_path.exists()


def test_invalid_config_is_rejected_before_existing_proxy_is_stopped(monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_DIR", tmp_path)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", tmp_path / "mihomo")
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 4321)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        local_proxy,
        "_validate_local_mihomo_config",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("配置预检失败")),
    )
    monkeypatch.setattr(
        local_proxy,
        "_terminate_pid",
        lambda pid: stopped.append(pid) or True,
    )

    with pytest.raises(RuntimeError, match="配置预检失败"):
        local_proxy._start_local_mihomo(tmp_path / "mihomo.exe", 17897)

    assert stopped == []
