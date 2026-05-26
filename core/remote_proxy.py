from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import posixpath
import re
import shlex
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib import parse as urlparse
from urllib import request as urlrequest

import yaml

from config.paths import STORAGE_DIR
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
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudcode-pa.googleapis.com",
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


@dataclass(frozen=True)
class ProxySubscriptionNode:
    index: int
    node: dict
    source: str = ""

    def display_name(self) -> str:
        return f"{self.index}. {describe_proxy_node(self.node)}"


@dataclass(frozen=True)
class ProxySubscriptionResult:
    nodes: tuple[ProxySubscriptionNode, ...]
    saved_path: str
    url: str = ""
    last_fetched_at: str = ""


def parse_proxy_node(text: str) -> dict:
    """Parse a Clash proxy node from an inline YAML/JSON-ish snippet."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请先粘贴 Clash 代理节点")

    candidate = _extract_first_proxy_entry(raw) or raw
    if candidate.startswith("-"):
        candidate = candidate[1:].strip()
    inline_candidate = _extract_first_inline_map(candidate)
    if inline_candidate:
        candidate = inline_candidate

    yaml_node = _parse_yaml_proxy_node(candidate)
    if yaml_node:
        return yaml_node

    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = _parse_inline_map(candidate[1:-1])
    else:
        parsed = _parse_block_map(candidate)

    if not isinstance(parsed, dict):
        raise ValueError("代理节点格式不正确")
    parsed = _normalize_proxy_node(parsed)
    return parsed


def parse_proxy_subscription_content(text: str) -> tuple[ProxySubscriptionNode, ...]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("订阅内容为空")

    nodes: list[dict] = []
    for variant in _subscription_text_variants(raw):
        nodes.extend(_parse_yaml_proxy_nodes(variant))
        nodes.extend(_parse_custom_proxy_nodes(variant))
        nodes.extend(_parse_proxy_uri_lines(variant))

    if not nodes:
        try:
            nodes.append(parse_proxy_node(raw))
        except Exception as exc:
            raise ValueError("订阅内容里没有识别到可用的 Clash/mihomo 节点") from exc

    unique_nodes = _dedupe_proxy_nodes(nodes)
    if not unique_nodes:
        raise ValueError("订阅内容里没有识别到可用的 Clash/mihomo 节点")
    return tuple(
        ProxySubscriptionNode(index=index, node=node, source="subscription")
        for index, node in enumerate(unique_nodes, 1)
    )


def fetch_proxy_subscription(
    url: str,
    timeout: int = 45,
    max_bytes: int = 5 * 1024 * 1024,
    persist: bool = True,
    retries: int = 3,
    retry_base_delay: float = 1.0,
) -> ProxySubscriptionResult:
    parsed_url = urlparse.urlparse((url or "").strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("订阅链接必须是 http 或 https 地址")
    normalized_url = urlparse.urlunparse(parsed_url)

    request = urlrequest.Request(
        normalized_url,
        headers={
            "User-Agent": "API-Switcher/1.0",
            "Accept": "text/plain, application/yaml, application/json, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )

    payload, content_type, charset = _download_proxy_subscription(
        request=request,
        timeout=timeout,
        max_bytes=max_bytes,
        retries=retries,
        retry_base_delay=retry_base_delay,
    )
    text = _decode_subscription_bytes(payload, charset)
    nodes = parse_proxy_subscription_content(text)
    saved_path = _save_proxy_subscription(normalized_url, payload, content_type)
    fetched_at = _now_iso()
    if persist:
        save_proxy_subscription_state(
            url=normalized_url,
            saved_path=str(saved_path),
            last_fetched_at=fetched_at,
            node_count=len(nodes),
            content_type=content_type,
            charset=charset,
        )
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(saved_path),
        url=normalized_url,
        last_fetched_at=fetched_at,
    )


def load_cached_proxy_subscription() -> ProxySubscriptionResult | None:
    state = load_proxy_subscription_state()
    saved_path = str(state.get("saved_path") or "").strip()
    if not saved_path:
        return None
    path = Path(saved_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        text = _decode_subscription_bytes(path.read_bytes(), str(state.get("charset") or "utf-8-sig"))
        nodes = parse_proxy_subscription_content(text)
    except Exception:
        return None
    return ProxySubscriptionResult(
        nodes=nodes,
        saved_path=str(path),
        url=str(state.get("url") or ""),
        last_fetched_at=str(state.get("last_fetched_at") or ""),
    )


def load_proxy_subscription_state() -> dict:
    path = _proxy_subscription_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_proxy_subscription_state(**updates) -> dict:
    directory = _proxy_subscription_dir()
    directory.mkdir(parents=True, exist_ok=True)
    state = load_proxy_subscription_state()
    for key, value in updates.items():
        if value is None:
            continue
        state[key] = value
    state["updated_at"] = _now_iso()
    path = _proxy_subscription_state_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return state


def set_proxy_subscription_auto_refresh(enabled: bool) -> dict:
    return save_proxy_subscription_state(auto_refresh=bool(enabled))


def set_proxy_subscription_selected_node(node: dict | None) -> dict:
    if not node:
        return save_proxy_subscription_state(selected_node_key="", selected_node_display="")
    normalized = _normalize_proxy_node(node)
    return save_proxy_subscription_state(
        selected_node_key=proxy_node_key(normalized),
        selected_node_display=describe_proxy_node(normalized),
    )


def proxy_node_key(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_proxy_node(node: dict) -> str:
    normalized = _normalize_proxy_node(node)
    return (
        f"{normalized['name']} "
        f"({normalized['type']}://{normalized['server']}:{normalized['port']})"
    )


def format_proxy_node(node: dict) -> str:
    return _dump_yaml(_normalize_proxy_node(node))


def _normalize_proxy_node(node: dict) -> dict:
    parsed = {str(k).strip(): v for k, v in node.items() if str(k).strip()}
    _apply_proxy_node_aliases(parsed)
    if not _has_value(parsed.get("name")) and _has_value(parsed.get("server")):
        type_label = str(parsed.get("type") or "proxy").strip() or "proxy"
        port_label = str(parsed.get("port") or "").strip()
        parsed["name"] = f"{type_label}-{parsed['server']}{':' + port_label if port_label else ''}"
    required = ["name", "type", "server", "port"]
    missing = [key for key in required if not _has_value(parsed.get(key))]
    if missing:
        raise ValueError("代理节点缺少字段: " + "、".join(missing))
    for key in ("name", "type", "server"):
        parsed[key] = str(parsed[key]).strip()
    if parsed["type"].lower() == "hy2":
        parsed["type"] = "hysteria2"
    elif parsed["type"].lower() == "socks":
        parsed["type"] = "socks5"
    parsed["port"] = _normalize_port(parsed["port"], "代理节点端口")
    return parsed


def _apply_proxy_node_aliases(parsed: dict) -> None:
    aliases = {
        "name": ("tag", "remark", "remarks", "ps"),
        "server": ("address", "host"),
        "port": ("server_port", "server-port", "serverPort"),
    }
    for canonical, names in aliases.items():
        if _has_value(parsed.get(canonical)):
            continue
        for alias in names:
            if _has_value(parsed.get(alias)):
                parsed[canonical] = parsed[alias]
                break


def build_mihomo_config(proxy_node: dict, mixed_port: int = 7890) -> str:
    node = dict(proxy_node)
    node["port"] = _normalize_port(node.get("port"), "代理节点端口")
    proxy_name = str(node.get("name") or "AI_PROXY").strip()
    node["name"] = proxy_name
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    config = {
        "mixed-port": mixed_port,
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
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
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
    pid_path = posixpath.join(home, ".config", "api-switcher", "ai-proxy.pid")
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    command = f"""
CONFIG={shlex.quote(config_path)}
PID_FILE={shlex.quote(pid_path)}
PORT={mixed_port}
installed=no
running=no
pid_running=no
port_listening=unknown
[ -s "$CONFIG" ] && installed=yes
if [ -s "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    pid_running=yes
    running=yes
  fi
fi
if command -v ss >/dev/null 2>&1; then
  port_listening=no
  ss -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes && running=yes || true
elif command -v netstat >/dev/null 2>&1; then
  port_listening=no
  netstat -ltn 2>/dev/null | grep -q ":$PORT " && port_listening=yes && running=yes || true
fi
printf 'installed=%s\\nrunning=%s\\npid_running=%s\\nport_listening=%s\\nconfig=%s\\n' "$installed" "$running" "$pid_running" "$port_listening" "$CONFIG"
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=20)
    if status != 0:
        raise RuntimeError((stderr or stdout or "远端 AI 代理状态检查失败").strip())
    values = _parse_key_values(stdout)
    detail = ""
    if values.get("pid_running") == "yes" and values.get("port_listening") == "no":
        detail = "进程存在，但端口未监听"
    elif values.get("pid_running") == "no" and values.get("port_listening") == "yes":
        detail = "端口已监听，但 pid 文件未更新"
    return RemoteAIProxyStatus(
        installed=values.get("installed") == "yes",
        running=values.get("running") == "yes",
        config_path=values.get("config") or config_path,
        proxy_url=f"http://127.0.0.1:{mixed_port}",
        detail=detail,
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
        if ":" not in line:
            continue
        key, value = _split_key_value(line)
        result[key] = _coerce_scalar(value)
    return result


def _parse_yaml_proxy_nodes(text: str) -> list[dict]:
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        proxy_section = _extract_yaml_proxy_section(text)
        if not proxy_section:
            return []
        try:
            parsed = yaml.safe_load(proxy_section)
        except Exception:
            return []

    candidates = []
    if isinstance(parsed, dict):
        proxies = parsed.get("proxies")
        if isinstance(proxies, list):
            candidates = proxies
        elif isinstance(proxies, dict):
            candidates = _proxy_mapping_values(proxies)
        elif _looks_like_proxy_node(parsed):
            candidates = [parsed]
    elif isinstance(parsed, list):
        candidates = parsed

    nodes = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            nodes.append(_normalize_proxy_node(candidate))
        except ValueError:
            continue
    return nodes


def _extract_yaml_proxy_section(text: str) -> str:
    match = re.search(r"(?m)^[ \t]*proxies\s*:", text or "")
    if not match:
        return ""
    lines = text[match.start():].splitlines()
    if not lines:
        return ""
    base_indent = len(lines[0]) - len(lines[0].lstrip(" "))
    collected = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            collected.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent and re.match(r"[A-Za-z0-9_-]+\s*:", stripped):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _proxy_mapping_values(proxies: dict) -> list[dict]:
    candidates = []
    for name, value in proxies.items():
        if not isinstance(value, dict):
            continue
        candidate = dict(value)
        if not _has_value(candidate.get("name")):
            candidate["name"] = name
        candidates.append(candidate)
    return candidates


def _parse_yaml_proxy_node(text: str) -> dict | None:
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(parsed, dict) or not _looks_like_proxy_node(parsed):
        return None
    try:
        return _normalize_proxy_node(parsed)
    except ValueError:
        return None


def _parse_custom_proxy_nodes(text: str) -> list[dict]:
    entries = _extract_proxy_entries(text)
    if not entries:
        entries = _extract_standalone_proxy_entries(text)

    nodes = []
    for entry in entries:
        try:
            nodes.append(parse_proxy_node(entry))
        except ValueError:
            continue
    return nodes


def _parse_proxy_uri_lines(text: str) -> list[dict]:
    nodes = []
    for candidate in _iter_proxy_uri_candidates(text):
        try:
            node = _parse_proxy_uri(candidate)
        except ValueError:
            continue
        if node:
            nodes.append(node)
    return nodes


def _iter_proxy_uri_candidates(text: str) -> list[str]:
    pattern = re.compile(
        r"(?i)\b(?:vmess|vless|trojan|ssr?|hy2|hysteria2|tuic)://[^\s<>'\"]+"
    )
    candidates = []
    for match in pattern.finditer(text or ""):
        candidate = _clean_proxy_uri_candidate(match.group(0))
        if candidate:
            candidates.append(candidate)
    for line in (text or "").splitlines():
        candidate = _clean_proxy_uri_candidate(line.strip())
        if candidate and re.match(r"(?i)^(?:socks5?|https?)://", candidate):
            candidates.append(candidate)
    return candidates


def _clean_proxy_uri_candidate(candidate: str) -> str:
    value = (candidate or "").strip().strip(",;")
    while value and value[-1] in ")]}":
        opener = {"}": "{", "]": "[", ")": "("}[value[-1]]
        if value.count(opener) >= value.count(value[-1]):
            break
        value = value[:-1]
    return value.strip()


def _parse_proxy_uri(text: str) -> dict:
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""
    if scheme == "vmess":
        return _parse_vmess_uri(text)
    if scheme == "vless":
        return _parse_vless_uri(text)
    if scheme == "trojan":
        return _parse_trojan_uri(text)
    if scheme == "ss":
        return _parse_ss_uri(text)
    if scheme == "ssr":
        return _parse_ssr_uri(text)
    if scheme in {"hy2", "hysteria2"}:
        return _parse_hysteria2_uri(text)
    if scheme == "tuic":
        return _parse_tuic_uri(text)
    if scheme in {"http", "https", "socks", "socks5"}:
        return _parse_basic_proxy_uri(text, scheme)
    raise ValueError("不支持的代理 URI")


def _parse_vmess_uri(text: str) -> dict:
    payload = _decode_base64_payload(text.split("://", 1)[1])
    if not payload:
        raise ValueError("vmess URI 不是有效的 base64 JSON")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("vmess URI 不是有效的 JSON") from exc

    name = str(data.get("ps") or data.get("add") or "vmess").strip()
    node = {
        "name": name,
        "type": "vmess",
        "server": data.get("add"),
        "port": data.get("port"),
        "uuid": data.get("id"),
        "alterId": _int_or_default(data.get("aid"), 0),
        "cipher": data.get("scy") or "auto",
        "network": data.get("net") or "tcp",
    }
    if str(data.get("tls") or "").lower() in {"tls", "true"}:
        node["tls"] = True
    if data.get("sni"):
        node["servername"] = data.get("sni")
    if node["network"] == "ws":
        ws_opts = {}
        if data.get("path"):
            ws_opts["path"] = data.get("path")
        if data.get("host"):
            ws_opts["headers"] = {"Host": data.get("host")}
        if ws_opts:
            node["ws-opts"] = ws_opts
    return _normalize_proxy_node(node)


def _parse_vless_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "vless"),
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": urlparse.unquote(parsed.username or ""),
        "network": query.get("type") or query.get("network") or "tcp",
        "udp": True,
    }
    encryption = query.get("encryption")
    if encryption:
        node["encryption"] = encryption
    _apply_common_uri_options(node, query)
    return _normalize_proxy_node(node)


def _parse_trojan_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "trojan"),
        "type": "trojan",
        "server": parsed.hostname,
        "port": parsed.port,
        "password": urlparse.unquote(parsed.username or ""),
        "network": query.get("type") or query.get("network") or "tcp",
        "udp": True,
    }
    _apply_common_uri_options(node, query)
    return _normalize_proxy_node(node)


def _parse_ss_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    name = _uri_name(parsed, "ss")
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        if ":" in userinfo:
            cipher, password = userinfo.split(":", 1)
            cipher = urlparse.unquote(cipher)
            password = urlparse.unquote(password)
        else:
            decoded = _decode_base64_userinfo(userinfo)
            cipher, password = decoded.split(":", 1)
        server = parsed.hostname
        port = parsed.port
    else:
        decoded = _decode_base64_userinfo(parsed.netloc)
        userinfo, endpoint = decoded.rsplit("@", 1)
        cipher, password = userinfo.split(":", 1)
        server, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)

    node = {
        "name": name,
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": cipher,
        "password": password,
        "udp": True,
    }
    if query.get("plugin"):
        node["plugin"] = query["plugin"]
    return _normalize_proxy_node(node)


def _parse_ssr_uri(text: str) -> dict:
    payload = _decode_base64_payload(text.split("://", 1)[1]) or _decode_base64_component(text.split("://", 1)[1])
    if not payload:
        raise ValueError("ssr URI 不是有效的 base64 内容")
    main, _, query_text = payload.partition("/?")
    parts = main.split(":")
    if len(parts) < 6:
        raise ValueError("ssr URI 主体字段不足")
    server, port, protocol, method, obfs, password_encoded = parts[:6]
    query = _query_map(query_text)
    node = {
        "name": _decode_base64_component(query.get("remarks") or "") or server,
        "type": "ssr",
        "server": server,
        "port": port,
        "cipher": method,
        "password": _decode_base64_component(password_encoded),
        "protocol": protocol,
        "obfs": obfs,
    }
    if query.get("obfsparam"):
        node["obfs-param"] = _decode_base64_component(query["obfsparam"])
    if query.get("protoparam"):
        node["protocol-param"] = _decode_base64_component(query["protoparam"])
    return _normalize_proxy_node(node)


def _parse_hysteria2_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    node = {
        "name": _uri_name(parsed, "hysteria2"),
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": parsed.port,
        "password": urlparse.unquote(parsed.username or query.get("password") or ""),
    }
    if query.get("sni"):
        node["sni"] = query["sni"]
    if _truthy(query.get("insecure") or query.get("allowInsecure")):
        node["skip-cert-verify"] = True
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])
    for key in ("obfs", "obfs-password", "up", "down"):
        if query.get(key):
            node[key] = query[key]
    return _normalize_proxy_node(node)


def _parse_tuic_uri(text: str) -> dict:
    parsed = urlparse.urlparse(text)
    query = _query_map(parsed.query)
    username = urlparse.unquote(parsed.username or "")
    password = urlparse.unquote(parsed.password or "")
    if ":" in username and not password:
        username, password = username.split(":", 1)
    node = {
        "name": _uri_name(parsed, "tuic"),
        "type": "tuic",
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": username,
        "password": password or query.get("password") or "",
    }
    if query.get("sni"):
        node["sni"] = query["sni"]
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])
    if query.get("congestion_control"):
        node["congestion-controller"] = query["congestion_control"]
    if query.get("udp_relay_mode"):
        node["udp-relay-mode"] = query["udp_relay_mode"]
    if _truthy(query.get("allowInsecure") or query.get("insecure")):
        node["skip-cert-verify"] = True
    return _normalize_proxy_node(node)


def _parse_basic_proxy_uri(text: str, scheme: str) -> dict:
    parsed = urlparse.urlparse(text)
    node = {
        "name": _uri_name(parsed, scheme),
        "type": "socks5" if scheme in {"socks", "socks5"} else "http",
        "server": parsed.hostname,
        "port": parsed.port,
    }
    if parsed.username:
        node["username"] = urlparse.unquote(parsed.username)
    if parsed.password:
        node["password"] = urlparse.unquote(parsed.password)
    if scheme == "https":
        node["tls"] = True
    return _normalize_proxy_node(node)


def _apply_common_uri_options(node: dict, query: dict[str, str]) -> None:
    security = (query.get("security") or "").lower()
    if security in {"tls", "reality"} or query.get("tls", "").lower() in {"1", "true", "tls"}:
        node["tls"] = True
    servername = query.get("sni") or query.get("servername") or query.get("peer")
    if servername:
        node["servername"] = servername
    if query.get("flow"):
        node["flow"] = query["flow"]
    if query.get("fp"):
        node["client-fingerprint"] = query["fp"]
    if _truthy(query.get("allowInsecure") or query.get("insecure")):
        node["skip-cert-verify"] = True
    if query.get("alpn"):
        node["alpn"] = _split_csv(query["alpn"])

    network = str(node.get("network") or "").lower()
    if network == "ws":
        ws_opts = {}
        if query.get("path"):
            ws_opts["path"] = query["path"]
        if query.get("host"):
            ws_opts["headers"] = {"Host": query["host"]}
        if ws_opts:
            node["ws-opts"] = ws_opts
    elif network == "grpc":
        grpc_opts = {}
        service_name = query.get("serviceName") or query.get("service_name") or query.get("grpc-service-name")
        if service_name:
            grpc_opts["grpc-service-name"] = service_name
        if query.get("mode"):
            grpc_opts["grpc-mode"] = query["mode"]
        if grpc_opts:
            node["grpc-opts"] = grpc_opts
    elif network == "httpupgrade":
        httpupgrade_opts = {}
        if query.get("path"):
            httpupgrade_opts["path"] = query["path"]
        if query.get("host"):
            httpupgrade_opts["host"] = query["host"]
        if httpupgrade_opts:
            node["httpupgrade-opts"] = httpupgrade_opts
    elif network in {"http", "h2"}:
        http_opts = {}
        if query.get("path"):
            http_opts["path"] = [query["path"]]
        if query.get("host"):
            http_opts["headers"] = {"Host": _split_csv(query["host"])}
        if http_opts:
            node["h2-opts" if network == "h2" else "http-opts"] = http_opts
    if security == "reality":
        reality_opts = {}
        if query.get("pbk"):
            reality_opts["public-key"] = query["pbk"]
        if query.get("sid"):
            reality_opts["short-id"] = query["sid"]
        if query.get("spx"):
            reality_opts["spider-x"] = query["spx"]
        if reality_opts:
            node["reality-opts"] = reality_opts


def _query_map(query: str) -> dict[str, str]:
    values = {}
    for key, items in urlparse.parse_qs(query, keep_blank_values=True).items():
        values[key] = items[-1] if items else ""
    return values


def _uri_name(parsed, fallback: str) -> str:
    return urlparse.unquote(parsed.fragment or parsed.hostname or fallback).strip() or fallback


def _decode_base64_userinfo(value: str) -> str:
    decoded = _decode_base64_payload(value)
    if not decoded:
        decoded = _decode_base64_component(value)
        if not decoded:
            raise ValueError("SS URI 用户信息不是有效 base64")
    if ":" not in decoded:
        raise ValueError("SS URI 用户信息缺少加密方式或密码")
    return decoded


def _decode_base64_component(value: str) -> str:
    raw = urlparse.unquote(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    padded = compact + ("=" * (-len(compact) % 4))
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace").strip()
    except (binascii.Error, ValueError):
        return ""


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_or_default(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _download_proxy_subscription(
    *,
    request: urlrequest.Request,
    timeout: int,
    max_bytes: int,
    retries: int,
    retry_base_delay: float,
) -> tuple[bytes, str, str]:
    try:
        attempts = max(1, int(retries))
    except (TypeError, ValueError):
        attempts = 3
    attempts = max(1, attempts)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urlrequest.urlopen(request, timeout=timeout) as response:
                status = _int_or_default(getattr(response, "status", getattr(response, "code", 200)), 200)
                if 400 <= status < 500:
                    raise ValueError(f"订阅链接返回 HTTP {status}，请检查订阅地址是否有效")
                if status >= 500:
                    raise RuntimeError(f"订阅服务器返回 HTTP {status}")

                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ValueError("订阅内容超过 5MB，已停止读取")

                headers = getattr(response, "headers", {}) or {}
                payload = _decode_http_payload(
                    payload,
                    content_encoding=_header_value(headers, "Content-Encoding"),
                    max_bytes=max_bytes,
                )
                return payload, _response_content_type(headers), _response_charset(headers) or "utf-8"
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ValueError(f"订阅链接返回 HTTP {exc.code}，请检查订阅地址是否有效") from exc
            last_error = exc
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc

        if attempt < attempts:
            delay = _retry_delay_seconds(retry_base_delay, attempt)
            if delay > 0:
                time.sleep(delay)

    suffix = f"（已重试 {attempts} 次）" if attempts > 1 else ""
    raise RuntimeError(f"订阅下载失败{suffix}: {last_error}") from last_error


def _decode_http_payload(payload: bytes, content_encoding: str, max_bytes: int) -> bytes:
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding:
        decoded = gzip.decompress(payload)
    elif "deflate" in encoding:
        try:
            decoded = zlib.decompress(payload)
        except zlib.error:
            decoded = zlib.decompress(payload, -zlib.MAX_WBITS)
    else:
        return payload

    if len(decoded) > max_bytes:
        raise ValueError("订阅内容解压后超过 5MB，已停止读取")
    return decoded


def _header_value(headers, name: str) -> str:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    for key in (name, name.lower(), name.title()):
        try:
            value = getter(key, "")
        except TypeError:
            value = getter(key)
        if value:
            return str(value)
    return ""


def _response_content_type(headers) -> str:
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        try:
            content_type = getter()
        except Exception:
            content_type = ""
        if content_type:
            return str(content_type)
    content_type = _header_value(headers, "Content-Type").split(";", 1)[0].strip()
    return content_type or "text/plain"


def _response_charset(headers) -> str:
    getter = getattr(headers, "get_content_charset", None)
    if callable(getter):
        try:
            charset = getter()
        except Exception:
            charset = ""
        if charset:
            return str(charset)
    content_type = _header_value(headers, "Content-Type")
    match = re.search(r"charset\s*=\s*([^;\s]+)", content_type, flags=re.I)
    return match.group(1).strip("\"'") if match else ""


def _decode_subscription_bytes(payload: bytes, charset: str) -> str:
    seen = set()
    candidates = [charset, "utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"]
    for candidate in candidates:
        encoding = str(candidate or "").strip().strip("\"'")
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _retry_delay_seconds(retry_base_delay: float, failed_attempt: int) -> float:
    try:
        base_delay = float(retry_base_delay)
    except (TypeError, ValueError):
        base_delay = 1.0
    return min(max(0.0, base_delay) * max(1, failed_attempt), 15.0)


def _looks_like_proxy_node(value: dict) -> bool:
    return all(key in value for key in ("name", "type", "server", "port"))


def _subscription_text_variants(text: str) -> list[str]:
    variants = [text]
    if "://" not in text and not re.search(r"(?m)^[ \t]*proxies\s*:", text):
        decoded = _decode_base64_text(text)
        if decoded and decoded not in variants:
            variants.append(decoded)
    return variants


def _decode_base64_text(text: str) -> str:
    decoded = _decode_base64_payload(text)
    if decoded and ("://" in decoded or re.search(r"(?m)^[ \t]*proxies\s*:", decoded)):
        return decoded
    return ""


def _decode_base64_payload(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 8 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return ""
    padded = compact + ("=" * (-len(compact) % 4))
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            payload = decoder(padded)
            decoded = payload.decode("utf-8", errors="strict").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded:
            return decoded
    return ""


def _dedupe_proxy_nodes(nodes: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for node in nodes:
        try:
            normalized = _normalize_proxy_node(node)
        except ValueError:
            continue
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def _proxy_subscription_dir() -> Path:
    return STORAGE_DIR / "proxy_subscriptions"


def _proxy_subscription_state_path() -> Path:
    return _proxy_subscription_dir() / "subscription_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _save_proxy_subscription(url: str, payload: bytes, content_type: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    raw_start = payload.lstrip()[:80].lower()
    extension = ".yaml" if "yaml" in content_type or raw_start.startswith((b"proxies:", b"proxy-groups:")) else ".txt"
    directory = _proxy_subscription_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"subscription-{digest}{extension}"
    path.write_bytes(payload)
    return Path(path)


def _extract_proxy_entries(text: str) -> list[str]:
    lines = text.splitlines()
    in_proxies = False
    base_indent = 0
    proxy_indent = 0
    entries: list[str] = []
    collected: list[str] = []

    def flush_current():
        if collected:
            entries.append("\n".join(collected).strip())
            collected.clear()

    for raw_line in lines:
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not in_proxies:
            inline_match = re.fullmatch(r"proxies\s*:\s*(.+)", stripped)
            if inline_match:
                inline_value = inline_match.group(1).strip()
                return _extract_inline_map_items(inline_value) or [inline_value]
            if re.fullmatch(r"proxies\s*:\s*", stripped):
                in_proxies = True
                base_indent = indent
            continue

        if indent <= base_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("-") and (not collected or indent <= proxy_indent):
            flush_current()
            proxy_indent = indent
            item = stripped[1:].strip()
            if item:
                collected.append(item)
            continue
        if collected:
            collected.append(stripped)
    flush_current()
    return entries


def _extract_standalone_proxy_entries(text: str) -> list[str]:
    entries = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or stripped.startswith("{"):
            entries.append(stripped)
    return entries


def _extract_inline_map_items(text: str) -> list[str]:
    items = []
    quote = ""
    escape = False
    start = -1
    depth = 0
    for index, char in enumerate(text):
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
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                items.append(text[start:index + 1].strip())
                start = -1
    return items


def _extract_first_proxy_entry(text: str) -> str:
    entries = _extract_proxy_entries(text)
    return entries[0] if entries else ""


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


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalize_port(value, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}必须是 1-65535 的整数")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是 1-65535 的整数") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{label}必须在 1-65535 之间")
    return port


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
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    proxy_url = f"http://127.0.0.1:{mixed_port}"
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
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    return f"""#!/bin/sh
set -eu
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
PID_FILE="$APP_DIR/ai-proxy.pid"
LOG_FILE="$APP_DIR/ai-proxy.log"
PORT={mixed_port}
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
elif command -v netstat >/dev/null 2>&1; then
  for _ in 1 2 3 4 5; do
    netstat -ltn 2>/dev/null | grep -q ":$PORT " && exit 0
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
    mixed_port = _normalize_port(mixed_port, "本地代理端口")
    return f"""set -eu
HOME_DIR={shlex.quote(home)}
CONFIG_DIR={shlex.quote(config_dir)}
APP_DIR={shlex.quote(app_dir)}
LOCAL_BIN_DIR={shlex.quote(local_bin_dir)}
START_SCRIPT={shlex.quote(start_path)}
PORT={mixed_port}
BIN="$LOCAL_BIN_DIR/mihomo"
mkdir -p "$CONFIG_DIR" "$APP_DIR" "$LOCAL_BIN_DIR"
if [ ! -x "$BIN" ] && ! command -v mihomo >/dev/null 2>&1; then
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "$arch" in
    x86_64|amd64) pattern="linux-amd64" ;;
    aarch64|arm64) pattern="linux-arm64" ;;
    armv7l|armv7*) pattern="linux-armv7" ;;
    *) echo "不支持的远端架构: $arch" >&2; exit 3 ;;
  esac
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$pattern" "$BIN" <<'PY'
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
    then
      if command -v clash >/dev/null 2>&1; then
        echo "mihomo 下载失败，回退使用远端已有 clash" >&2
      else
        exit 4
      fi
    fi
  elif command -v clash >/dev/null 2>&1; then
    echo "远端未安装 python3，回退使用已有 clash" >&2
  else
    echo "远端未安装 python3，且未找到 mihomo/clash，无法自动下载 mihomo" >&2
    exit 2
  fi
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
