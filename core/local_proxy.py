from __future__ import annotations

import gzip
import io
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from config.paths import STORAGE_DIR
from core import persistent_env, remote_proxy, vscode_parser


DEFAULT_LOCAL_MIXED_PORT = 17897
LOCAL_PORT_CANDIDATES = tuple(range(DEFAULT_LOCAL_MIXED_PORT, DEFAULT_LOCAL_MIXED_PORT + 50))
LOCAL_PROXY_DIR = STORAGE_DIR / "local_ai_proxy"
LOCAL_PROXY_CONFIG_DIR = LOCAL_PROXY_DIR / "mihomo"
LOCAL_PROXY_BIN_DIR = LOCAL_PROXY_DIR / "bin"
LOCAL_PROXY_STATE_PATH = LOCAL_PROXY_DIR / "state.json"
LOCAL_PROXY_LOG_PATH = LOCAL_PROXY_DIR / "mihomo.log"
LOCAL_PROXY_PID_PATH = LOCAL_PROXY_DIR / "mihomo.pid"


@dataclass(frozen=True)
class LocalAIProxyStatus:
    installed: bool
    running: bool
    config_path: str
    proxy_url: str
    detail: str = ""

    def summary(self) -> str:
        state = "运行中" if self.running else "未运行"
        installed = "已配置" if self.installed else "未配置"
        detail = f"；{self.detail}" if self.detail else ""
        return f"本机 AI 代理{installed}，{state}: {self.proxy_url}{detail}"


def install_local_ai_proxy(proxy_text: str, mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> str:
    if os.name != "nt":
        raise RuntimeError("本机 AI 代理目前只支持 Windows")
    mixed_port = _select_local_mixed_port(mixed_port)
    proxy_node = remote_proxy.parse_proxy_node(proxy_text)
    _ensure_local_dirs()
    config_path = LOCAL_PROXY_CONFIG_DIR / "config.yaml"
    binary_path = _ensure_mihomo_binary()
    proxy_url = _proxy_url(mixed_port)

    state = _load_state()
    if not isinstance(state.get("previous_env"), dict):
        state["previous_env"] = _capture_previous_env()
    if not isinstance(state.get("previous_vscode"), dict):
        state["previous_vscode"] = _capture_vscode_proxy_state(vscode_parser.read_vscode_settings())

    config_path.write_text(remote_proxy.build_mihomo_config(proxy_node, mixed_port), encoding="utf-8")
    try:
        _start_local_mihomo(binary_path, mixed_port)
        _apply_local_env(mixed_port)
        _apply_local_vscode_proxy(mixed_port)
    except Exception:
        _restore_local_env(state, mixed_port)
        _restore_local_vscode_proxy(state, mixed_port)
        _cleanup_managed_process(binary_path, state)
        raise

    state.update(
        {
            "mixed_port": mixed_port,
            "proxy_url": proxy_url,
            "config_path": str(config_path),
            "binary_path": str(binary_path),
            "pid": _read_pid(),
            "node_display": remote_proxy.describe_proxy_node(proxy_node),
            "updated_at": remote_proxy._now_iso(),
        }
    )
    _save_state(state)
    return (
        f"本机 AI 代理已启动: {proxy_url}；"
        "已写入 Windows 用户环境变量和 VS Code 本机代理设置，新终端生效"
    )


def inspect_local_ai_proxy(mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> LocalAIProxyStatus:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or mixed_port,
        "本机代理端口",
    )
    config_path = Path(state.get("config_path") or (LOCAL_PROXY_CONFIG_DIR / "config.yaml"))
    pid = _read_pid()
    pid_running = _is_pid_running(pid) if pid else False
    port_listening = _is_port_listening(mixed_port)
    installed = config_path.exists()
    detail = ""
    if pid_running and not port_listening:
        detail = "进程存在，但端口未监听"
    elif installed and port_listening and not pid_running:
        detail = "端口已监听，但 pid 文件未更新或不是本工具进程"
    elif not installed and port_listening:
        detail = "默认端口被其他程序占用，本工具启动时会自动选择空闲端口"
    return LocalAIProxyStatus(
        installed=installed,
        running=pid_running or (installed and port_listening),
        config_path=str(config_path),
        proxy_url=_proxy_url(mixed_port),
        detail=detail,
    )


def stop_local_ai_proxy(restore_settings: bool = True) -> str:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    pid = _read_pid()
    stopped = False
    skipped_unmanaged = False
    if pid and _is_pid_running(pid):
        if _is_managed_mihomo_pid(pid, state=state):
            _terminate_pid(pid)
            stopped = True
        else:
            skipped_unmanaged = True
    LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)
    if restore_settings:
        _restore_local_env(state, mixed_port)
        _restore_local_vscode_proxy(state, mixed_port)
    _save_state({})
    if stopped:
        return "本机 AI 代理已停止"
    if skipped_unmanaged:
        return "本机 AI 代理未停止：pid 文件指向的进程不是本工具启动的代理，已跳过"
    return "本机 AI 代理未发现运行中的受管进程"


def _ensure_local_dirs() -> None:
    LOCAL_PROXY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PROXY_BIN_DIR.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict:
    try:
        data = json.loads(LOCAL_PROXY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    _ensure_local_dirs()
    temp_path = LOCAL_PROXY_STATE_PATH.with_name(f"{LOCAL_PROXY_STATE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(LOCAL_PROXY_STATE_PATH)
    finally:
        temp_path.unlink(missing_ok=True)


def _proxy_url(mixed_port: int) -> str:
    return f"http://127.0.0.1:{mixed_port}"


def _select_local_mixed_port(preferred_port: int = DEFAULT_LOCAL_MIXED_PORT) -> int:
    state = _load_state()
    pid = _read_pid()
    preferred = remote_proxy._normalize_port(
        state.get("mixed_port") or preferred_port,
        "本机代理端口",
    )
    if pid and _is_pid_running(pid):
        return preferred
    if not _is_port_listening(preferred):
        return preferred
    for port in LOCAL_PORT_CANDIDATES:
        if not _is_port_listening(port):
            return port
    raise RuntimeError(
        f"本机 AI 代理候选端口 {LOCAL_PORT_CANDIDATES[0]}-{LOCAL_PORT_CANDIDATES[-1]} 均被占用"
    )


def _local_proxy_env_values(mixed_port: int) -> dict[str, str]:
    return remote_proxy._proxy_env_values(mixed_port)


def _apply_local_env(mixed_port: int) -> None:
    persistent_env.set_local_user_env(_local_proxy_env_values(mixed_port))


def _capture_previous_env() -> dict:
    previous = {}
    for key in remote_proxy.PROXY_ENV_KEYS:
        value = persistent_env._local_user_env_value(key)
        previous[key] = {"exists": value is not None, "value": value or ""}
    return previous


def _restore_local_env(state: dict, mixed_port: int) -> None:
    previous = state.get("previous_env")
    if not isinstance(previous, dict):
        return
    expected = _local_proxy_env_values(mixed_port)
    updates = {}
    deletes = []
    for key in remote_proxy.PROXY_ENV_KEYS:
        current = persistent_env._local_user_env_value(key)
        if current != expected.get(key):
            continue
        item = previous.get(key)
        if isinstance(item, dict) and item.get("exists") and item.get("value"):
            updates[key] = str(item.get("value") or "")
        else:
            deletes.append(key)
    if updates:
        persistent_env.set_local_user_env(updates)
    if deletes:
        persistent_env.delete_local_user_env(deletes)


def _capture_vscode_proxy_state(settings: dict) -> dict:
    terminal_env = settings.get("terminal.integrated.env.windows")
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    return {
        "http.proxy": {"exists": "http.proxy" in settings, "value": settings.get("http.proxy")},
        "http.proxySupport": {
            "exists": "http.proxySupport" in settings,
            "value": settings.get("http.proxySupport"),
        },
        "terminal.integrated.env.windows": {
            key: {"exists": key in terminal_env, "value": terminal_env.get(key)}
            for key in remote_proxy.PROXY_ENV_KEYS
        },
    }


def _apply_local_vscode_proxy_settings(settings: dict, mixed_port: int) -> tuple[dict, bool]:
    env = _local_proxy_env_values(mixed_port)
    updated = dict(settings or {})
    changed = False
    proxy_url = env["API_SWITCHER_AI_PROXY_URL"]

    if updated.get("http.proxy") != proxy_url:
        updated["http.proxy"] = proxy_url
        changed = True
    if updated.get("http.proxySupport") != "override":
        updated["http.proxySupport"] = "override"
        changed = True

    terminal_env = updated.get("terminal.integrated.env.windows")
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    else:
        terminal_env = dict(terminal_env)
    for key in remote_proxy.PROXY_ENV_KEYS:
        if terminal_env.get(key) != env[key]:
            terminal_env[key] = env[key]
            changed = True
    if updated.get("terminal.integrated.env.windows") != terminal_env:
        updated["terminal.integrated.env.windows"] = terminal_env
        changed = True
    return updated, changed


def _apply_local_vscode_proxy(mixed_port: int) -> None:
    settings = vscode_parser.read_vscode_settings()
    updated, changed = _apply_local_vscode_proxy_settings(settings, mixed_port)
    if changed:
        vscode_parser.write_vscode_settings(updated)


def _restore_vscode_key(settings: dict, key: str, previous: dict, expected_value) -> bool:
    item = previous.get(key)
    if not isinstance(item, dict) or settings.get(key) != expected_value:
        return False
    if item.get("exists"):
        settings[key] = item.get("value")
    else:
        settings.pop(key, None)
    return True


def _restore_vscode_proxy_settings(settings: dict, previous: dict, mixed_port: int) -> tuple[dict, bool]:
    env = _local_proxy_env_values(mixed_port)
    updated = dict(settings or {})
    changed = False
    changed = _restore_vscode_key(updated, "http.proxy", previous, env["API_SWITCHER_AI_PROXY_URL"]) or changed
    changed = _restore_vscode_key(updated, "http.proxySupport", previous, "override") or changed

    terminal_key = "terminal.integrated.env.windows"
    terminal_env = updated.get(terminal_key)
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    else:
        terminal_env = dict(terminal_env)
    previous_terminal = previous.get(terminal_key)
    if isinstance(previous_terminal, dict):
        for key in remote_proxy.PROXY_ENV_KEYS:
            if terminal_env.get(key) != env[key]:
                continue
            item = previous_terminal.get(key)
            if isinstance(item, dict) and item.get("exists"):
                terminal_env[key] = item.get("value")
            else:
                terminal_env.pop(key, None)
            changed = True
    if changed:
        if terminal_env:
            updated[terminal_key] = terminal_env
        else:
            updated.pop(terminal_key, None)
    return updated, changed


def _restore_local_vscode_proxy(state: dict, mixed_port: int) -> None:
    previous = state.get("previous_vscode")
    if not isinstance(previous, dict):
        return
    settings = vscode_parser.read_vscode_settings()
    updated, changed = _restore_vscode_proxy_settings(settings, previous, mixed_port)
    if changed:
        vscode_parser.write_vscode_settings(updated)


def _ensure_mihomo_binary() -> Path:
    binary_path = LOCAL_PROXY_BIN_DIR / "mihomo.exe"
    if binary_path.exists():
        return binary_path

    existing = shutil.which("mihomo") or shutil.which("mihomo.exe") or shutil.which("clash") or shutil.which("clash.exe")
    if existing:
        return Path(existing)

    _download_mihomo_binary(binary_path)
    return binary_path


def _windows_asset_pattern() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "windows-arm64"
    if machine in {"amd64", "x86_64", "x64"}:
        return "windows-amd64"
    raise RuntimeError(f"不支持的 Windows 架构: {platform.machine()}")


def _pick_mihomo_asset(assets: list[dict], pattern: str) -> dict:
    def usable(asset: dict) -> bool:
        name = str(asset.get("name") or "").lower()
        if pattern not in name:
            return False
        if not name.endswith((".zip", ".gz", ".exe")):
            return False
        return not any(token in name for token in ("sha256", "checksums"))

    candidates = [asset for asset in assets if usable(asset) and "compatible" not in str(asset.get("name", "")).lower()]
    if not candidates:
        candidates = [asset for asset in assets if usable(asset)]
    if not candidates:
        raise RuntimeError(f"没有找到匹配 {pattern} 的 mihomo Windows 发行包")
    return candidates[0]


def _download_mihomo_binary(target: Path) -> None:
    pattern = _windows_asset_pattern()
    release_request = urllib.request.Request(
        "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "API-Switcher/1.0",
        },
    )
    with urllib.request.urlopen(release_request, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))
    asset = _pick_mihomo_asset(data.get("assets") or [], pattern)
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise RuntimeError("mihomo 发行包缺少下载地址")
    asset_request = urllib.request.Request(url, headers={"User-Agent": "API-Switcher/1.0"})
    with urllib.request.urlopen(asset_request, timeout=180) as response:
        payload = response.read()
    _write_mihomo_payload(target, url, payload)


def _write_mihomo_payload(target: Path, url: str, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        lower_url = url.lower()
        if lower_url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                exe_names = [
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".exe") and ("mihomo" in name.lower() or "clash" in name.lower())
                ]
                if not exe_names:
                    raise RuntimeError("mihomo zip 里没有找到可执行文件")
                temp_path.write_bytes(archive.read(exe_names[0]))
        elif lower_url.endswith(".gz"):
            temp_path.write_bytes(gzip.decompress(payload))
        else:
            temp_path.write_bytes(payload)
        temp_path.replace(target)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_pid() -> int | None:
    try:
        return int(LOCAL_PROXY_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _is_windows_pid_running(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_windows_pid_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_process_image_path(pid: int) -> str:
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _normalize_existing_path(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except Exception:
        return os.path.normcase(str(path))


def _is_managed_mihomo_pid(
    pid: int,
    state: dict | None = None,
    binary_path: str | Path | None = None,
) -> bool:
    if not pid or not _is_pid_running(pid):
        return False
    if os.name != "nt":
        return True

    image_path = _windows_process_image_path(pid)
    if not image_path:
        return False
    image_name = Path(image_path).name.lower()
    if image_name not in {"mihomo.exe", "clash.exe"}:
        return False

    expected_paths = []
    if binary_path:
        expected_paths.append(binary_path)
    stored_binary = (state or {}).get("binary_path") if isinstance(state, dict) else None
    if stored_binary:
        expected_paths.append(stored_binary)
    expected_paths.append(LOCAL_PROXY_BIN_DIR / "mihomo.exe")

    image_normalized = _normalize_existing_path(image_path)
    for expected in expected_paths:
        if image_normalized and image_normalized == _normalize_existing_path(expected):
            return True
    return False


def _cleanup_managed_process(binary_path: Path, state: dict | None = None) -> None:
    pid = _read_pid()
    if pid and _is_managed_mihomo_pid(pid, state=state, binary_path=binary_path):
        _terminate_pid(pid)
    LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not _is_pid_running(pid):
            return
        time.sleep(0.2)


def _is_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _start_local_mihomo(binary_path: Path, mixed_port: int) -> None:
    pid = _read_pid()
    if pid and _is_pid_running(pid):
        state = _load_state()
        if _is_managed_mihomo_pid(pid, state=state, binary_path=binary_path):
            _terminate_pid(pid)
        else:
            LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)
            if _is_port_listening(mixed_port):
                raise RuntimeError(f"本机端口 127.0.0.1:{mixed_port} 已被其他程序占用")
    elif _is_port_listening(mixed_port):
        raise RuntimeError(f"本机端口 127.0.0.1:{mixed_port} 已被占用，请先关闭占用该端口的程序")

    LOCAL_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    with LOCAL_PROXY_LOG_PATH.open("ab") as log_handle:
        process = subprocess.Popen(
            [str(binary_path), "-d", str(LOCAL_PROXY_CONFIG_DIR)],
            cwd=str(LOCAL_PROXY_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    LOCAL_PROXY_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError(f"mihomo 启动失败，详见日志: {LOCAL_PROXY_LOG_PATH}")
    for _ in range(10):
        if _is_port_listening(mixed_port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"mihomo 已启动但端口 {mixed_port} 未监听，详见日志: {LOCAL_PROXY_LOG_PATH}")
