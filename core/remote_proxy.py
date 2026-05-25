from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass

from core import profile_manager, remote_config
from core.ssh_manager import ssh_manager


AI_PROXY_DOMAINS = (
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "auth0.openai.com",
    "anthropic.com",
    "claude.ai",
    "gemini.google.com",
    "generativelanguage.googleapis.com",
    "aistudio.google.com",
    "ai.google.dev",
    "makersuite.google.com",
)


@dataclass(frozen=True)
class RemoteAIProxyStatus:
    installed: bool
    running: bool
    config_path: str
    proxy_url: str
    detail: str = ""

    def summary(self) -> str:
        state = "运行中" if self.running else "未运行"
        installed = "已配置" if self.installed else "未配置"
        detail = f"；{self.detail}" if self.detail else ""
        return f"AI 代理{installed}，{state}: {self.proxy_url}{detail}"


def parse_proxy_node(text: str) -> dict:
    """Parse a Clash proxy node from an inline YAML/JSON-ish snippet."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请先粘贴 Clash 代理节点")

    candidate = raw
    if candidate.startswith("-"):
        candidate = candidate[1:].strip()
    inline_candidate = _extract_first_inline_map(candidate)
    if inline_candidate:
        candidate = inline_candidate

    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = _parse_inline_map(candidate[1:-1])
    else:
        parsed = _parse_block_map(candidate)

    if not isinstance(parsed, dict):
        raise ValueError("代理节点格式不正确")
    parsed = {str(k).strip(): v for k, v in parsed.items() if str(k).strip()}
    required = ["name", "type", "server", "port"]
    missing = [key for key in required if not str(parsed.get(key, "")).strip()]
    if missing:
        raise ValueError("代理节点缺少字段: " + "、".join(missing))
    return parsed


def build_mihomo_config(proxy_node: dict, mixed_port: int = 7890) -> str:
    node = dict(proxy_node)
    proxy_name = str(node.get("name") or "AI_PROXY").strip()
    node["name"] = proxy_name
    config = {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "proxies": [node],
        "proxy-groups": [
            {
                "name": "AI-PROXY",
                "type": "select",
                "proxies": [proxy_name],
            }
        ],
        "rules": [
            *(f"DOMAIN-SUFFIX,{domain},AI-PROXY" for domain in AI_PROXY_DOMAINS),
            "MATCH,DIRECT",
        ],
    }
    return _dump_yaml(config)


def install_ai_proxy(ssh_name: str, proxy_text: str, mixed_port: int = 7890) -> str:
    proxy_node = parse_proxy_node(proxy_text)
    ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_dir = posixpath.join(home, ".config", "mihomo")
    app_dir = posixpath.join(home, ".config", "api-switcher")
    local_bin_dir = posixpath.join(home, ".local", "bin")
    config_path = posixpath.join(config_dir, "config.yaml")
    env_path = posixpath.join(app_dir, "ai-proxy.env")
    start_path = posixpath.join(app_dir, "start-ai-proxy.sh")

    ssh_manager.write_remote_file(client, config_path, build_mihomo_config(proxy_node, mixed_port), file_mode=0o600)
    ssh_manager.write_remote_file(client, env_path, _build_env_file(mixed_port), file_mode=0o600)
    ssh_manager.write_remote_file(
        client,
        start_path,
        _build_start_script(config_dir, app_dir, local_bin_dir, mixed_port),
        file_mode=0o700,
    )

    command = _build_install_command(home, config_dir, app_dir, local_bin_dir, start_path, mixed_port)
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=360, log_command=False)
    if status != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"远端 AI 代理配置失败: {detail or status}")
    _write_shell_profile_block(client, home, env_path, start_path)
    result = (stdout or "").strip().splitlines()
    suffix = f"；{result[-1]}" if result else ""
    return f"AI 代理已部署到 {ssh_name}: http://127.0.0.1:{mixed_port}{suffix}"


def inspect_ai_proxy(ssh_name: str, mixed_port: int = 7890) -> RemoteAIProxyStatus:
    _ssh_profile, client = _connect_ssh(ssh_name)
    home = remote_config._remote_home(client)
    config_path = posixpath.join(home, ".config", "mihomo", "config.yaml")
    command = f"""
CONFIG={shlex.quote(config_path)}
PORT={int(mixed_port)}
installed=no
running=no
[ -s "$CONFIG" ] && installed=yes
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | grep -q ":$PORT " && running=yes || true
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | grep -q ":$PORT " && running=yes || true
fi
printf 'installed=%s\\nrunning=%s\\nconfig=%s\\n' "$installed" "$running" "$CONFIG"
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=20)
    if status != 0:
        raise RuntimeError((stderr or stdout or "远端 AI 代理状态检查失败").strip())
    values = _parse_key_values(stdout)
    return RemoteAIProxyStatus(
        installed=values.get("installed") == "yes",
        running=values.get("running") == "yes",
        config_path=values.get("config") or config_path,
        proxy_url=f"http://127.0.0.1:{mixed_port}",
    )


def _connect_ssh(ssh_name: str):
    profiles = profile_manager.list_ssh_profiles()
    profile = next((item for item in profiles if item.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile, ssh_manager.connect(profile)


def _parse_inline_map(text: str) -> dict:
    result = {}
    for part in _split_top_level(text, ","):
        if not part.strip():
            continue
        key, value = _split_key_value(part)
        result[key] = _coerce_scalar(value)
    return result


def _parse_block_map(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        key, value = _split_key_value(line)
        result[key] = _coerce_scalar(value)
    return result


def _extract_first_inline_map(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    quote = ""
    escape = False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1].strip()
    return ""


def _split_top_level(text: str, delimiter: str) -> list[str]:
    parts = []
    current = []
    quote = ""
    escape = False
    depth = 0
    for char in text:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        if char == delimiter and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _split_key_value(text: str) -> tuple[str, str]:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == ":":
            key = text[:index].strip().strip("\"'")
            value = text[index + 1:].strip()
            if not key:
                raise ValueError("代理节点包含空字段名")
            return key, value
    raise ValueError(f"代理节点字段缺少冒号: {text}")


def _coerce_scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_map(value[1:-1])
    if value.startswith("[") and value.endswith("]"):
        return [_coerce_scalar(part) for part in _split_top_level(value[1:-1], ",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    return value


def _dump_yaml(value, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                lines.append(_dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{_yaml_scalar(value)}"


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _build_env_file(mixed_port: int) -> str:
    proxy_url = f"http://127.0.0.1:{int(mixed_port)}"
    no_proxy = "127.0.0.1,localhost,::1,*.local"
    return "\n".join([
        "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.",
        f"export API_SWITCHER_AI_PROXY_URL={shlex.quote(proxy_url)}",
        f"export HTTP_PROXY={shlex.quote(proxy_url)}",
        f"export HTTPS_PROXY={shlex.quote(proxy_url)}",
        f"export ALL_PROXY={shlex.quote(proxy_url)}",
        f"export http_proxy={shlex.quote(proxy_url)}",
        f"export https_proxy={shlex.quote(proxy_url)}",
        f"export all_proxy={shlex.quote(proxy_url)}",
        f"export NO_PROXY={shlex.quote(no_proxy)}",
        f"export no_proxy={shlex.quote(no_proxy)}",
        "",
    ])


def _build_start_script(config_dir: str, app_dir: str, local_bin_dir: str, mixed_port: int) -> str:
    return f"""#!/bin/sh
set -eu
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
PID_FILE="$APP_DIR/ai-proxy.pid"
LOG_FILE="$APP_DIR/ai-proxy.log"
PORT={int(mixed_port)}
RESTART="${{1:-}}"
BIN="$LOCAL_BIN_DIR/mihomo"
if [ ! -x "$BIN" ]; then
  BIN="$(command -v mihomo 2>/dev/null || command -v clash 2>/dev/null || true)"
fi
if [ -z "$BIN" ]; then
  echo "mihomo/clash not found" >&2
  exit 1
fi
if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    if [ "$RESTART" = "restart" ]; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
    else
      exit 0
    fi
  fi
fi
mkdir -p "$APP_DIR"
nohup "$BIN" -d "$CONFIG_DIR" >>"$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"
sleep 2
new_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$new_pid" ] || ! kill -0 "$new_pid" 2>/dev/null; then
  echo "mihomo failed to stay running; see $LOG_FILE" >&2
  exit 2
fi
if command -v ss >/dev/null 2>&1; then
  for _ in 1 2 3 4 5; do
    ss -ltn 2>/dev/null | grep -q ":$PORT " && exit 0
    sleep 1
  done
  echo "mihomo is running but port $PORT is not listening yet; see $LOG_FILE" >&2
  exit 3
fi
"""


def _build_install_command(
    home: str,
    config_dir: str,
    app_dir: str,
    local_bin_dir: str,
    start_path: str,
    mixed_port: int,
) -> str:
    return f"""set -eu
HOME_DIR={shlex.quote(home)}
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
START_SCRIPT={shlex.quote(start_path)}
PORT={int(mixed_port)}
BIN="$LOCAL_BIN_DIR/mihomo"
mkdir -p "$CONFIG_DIR" "$APP_DIR" "$LOCAL_BIN_DIR"
if [ ! -x "$BIN" ] && ! command -v mihomo >/dev/null 2>&1 && ! command -v clash >/dev/null 2>&1; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "远端未安装 python3，且未找到 mihomo/clash，无法自动下载 mihomo" >&2
    exit 2
  fi
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "$arch" in
    x86_64|amd64) pattern="linux-amd64" ;;
    aarch64|arm64) pattern="linux-arm64" ;;
    armv7l|armv7*) pattern="linux-armv7" ;;
    *) echo "不支持的远端架构: $arch" >&2; exit 3 ;;
  esac
  python3 - "$pattern" "$BIN" <<'PY'
import gzip
import json
import os
import sys
import urllib.request

pattern, target = sys.argv[1], sys.argv[2]
with urllib.request.urlopen("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest", timeout=45) as response:
    data = json.loads(response.read().decode("utf-8"))
assets = data.get("assets") or []

def usable(asset):
    name = str(asset.get("name") or "").lower()
    if pattern not in name or not name.endswith(".gz"):
        return False
    return not any(token in name for token in ("deb", "rpm", "sha256", "checksums"))

candidates = [asset for asset in assets if usable(asset) and "compatible" not in str(asset.get("name", "")).lower()]
if not candidates:
    candidates = [asset for asset in assets if usable(asset)]
if not candidates:
    raise SystemExit(f"no mihomo asset matched {{pattern}}")
url = candidates[0]["browser_download_url"]
with urllib.request.urlopen(url, timeout=120) as response:
    payload = response.read()
if url.lower().endswith(".gz"):
    payload = gzip.decompress(payload)
with open(target, "wb") as handle:
    handle.write(payload)
os.chmod(target, 0o755)
print("downloaded=" + url)
PY
fi
"$START_SCRIPT" restart
printf 'config=%s\\nproxy=http://127.0.0.1:%s\\n' "$CONFIG_DIR/config.yaml" "$PORT"
"""


def _write_shell_profile_block(client, home: str, env_path: str, start_path: str) -> None:
    block = "\n".join([
        "# >>> API切换器 AI proxy >>>",
        f"if [ -f {shlex.quote(env_path)} ]; then . {shlex.quote(env_path)}; fi",
        f"if [ -x {shlex.quote(start_path)} ]; then {shlex.quote(start_path)} >/dev/null 2>&1 & fi",
        "# <<< API切换器 AI proxy <<<",
    ])
    script = f"""
set -eu
BLOCK_START="# >>> API切换器 AI proxy >>>"
BLOCK_END="# <<< API切换器 AI proxy <<<"
BLOCK={shlex.quote(block)}
for file in {shlex.quote(posixpath.join(home, ".profile"))} {shlex.quote(posixpath.join(home, ".bashrc"))}; do
  touch "$file"
  tmp="$file.tmp.$$"
  awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
    $0 == start {{skip=1; next}}
    $0 == end {{skip=0; next}}
    skip != 1 {{print}}
  ' "$file" > "$tmp"
  printf "\\n%s\\n" "$BLOCK" >> "$tmp"
  mv "$tmp" "$file"
done
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, script, timeout=30, log_command=False)
    if status != 0:
        raise RuntimeError((stderr or stdout or "写入 shell 代理环境失败").strip())


def _parse_key_values(text: str) -> dict[str, str]:
    values = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values
