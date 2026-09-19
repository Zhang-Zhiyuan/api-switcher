"""Read-only managed routing inspection. No DNS, target requests or proxy changes.

Controller contract: https://wiki.metacubex.one/en/api/
Only /configs, /rules and /proxies are read; never /delay or /connections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from core.lazy_imports import LazyModule
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES, LOCAL_PROXY_BUILTIN_SITES
from core.proxy_health import HEALTH_TTL_SECONDS, parse_proxy_health, proxy_health_summary
from core.redaction import redact_sensitive_text

local_proxy = LazyModule("core.local_proxy")
remote_proxy = LazyModule("core.remote_proxy")
proxy_routing = LazyModule("core.proxy_routing")

MAX_BYTES = 4 * 1024 * 1024
SNAPSHOT_TTL = 60
HEALTH_TTL = HEALTH_TTL_SECONDS

# Exactly the same bounded, proxy-independent reader runs locally and over SSH.
# This is trusted static source, never assembled from a user URL or credentials.
_CONTROLLER_READER = r'''
import json
import time
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("controller redirect refused")

deadline = time.monotonic() + 8
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
def read(path):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("controller deadline")
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     headers={"Accept": "application/json"}, method="GET")
    with opener.open(request, timeout=min(2, remaining)) as response:
        if response.status != 200:
            raise RuntimeError("controller status")
        declared = response.headers.get("Content-Length")
        declared = int(declared) if declared is not None else None
        if declared is not None and not 0 <= declared <= 4194304:
            raise ValueError("controller response too large or invalid length")
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("controller deadline")
            chunk = response.read1(min(65536, 4194305 - size))
            size += len(chunk)
            if size > 4194304:
                raise ValueError("controller response too large")
            if not chunk:
                break
            chunks.append(chunk)
        if declared is not None and size != declared:
            raise ValueError("incomplete controller response")
    if time.monotonic() >= deadline:
        raise TimeoutError("controller deadline")
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise ValueError("invalid controller response")
    return value

config = read("/configs")
expected = globals().get("expected_mixed_port")
if expected is not None and (type(config.get("mixed-port")) is not int or config["mixed-port"] != expected):
    raise ValueError("controller mixed-port mismatch")
rules = read("/rules")
proxies = read("/proxies")
def rule_signature(value):
    return [(item.get("type"), item.get("payload"), item.get("proxy"),
             (item.get("extra") or {}).get("disabled", False)) for item in value.get("rules", [])]
if rule_signature(read("/rules")) != rule_signature(rules) or read("/configs") != config:
    raise RuntimeError("controller configuration changed during inspection")
# Do not transmit proxy server addresses/passwords or connection browsing data.
allowed = ("name", "type", "now", "all", "alive", "history", "extra", "testUrl")
result = {"mode": config.get("mode"),
          "rules": [{"type": item.get("type"), "payload": item.get("payload"), "proxy": item.get("proxy"),
                     "extra": {"disabled": (item.get("extra") or {}).get("disabled", False)}}
                    for item in rules.get("rules", [])],
          "proxies": {name: {key: item[key] for key in allowed if key in item}
                      for name, item in proxies.get("proxies", {}).items()
                      if isinstance(item, dict)}}
'''


def clean(value) -> str:
    return " ".join(redact_sensitive_text(str(value or "")).split())


def target_host(value: str) -> str:
    """Discard URL path/query/fragment; accept a single HTTP(S) URL, domain or IP."""
    text = str(value or "").strip()
    if not text or len(text) > 8192 or any(ord(c) < 32 for c in text) or "\\" in text:
        raise ValueError("请输入一个有效的 HTTP(S) 网址、域名或 IP")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        if "%" in text:
            raise ValueError("不支持带接口作用域的 IPv6 地址；请填写不含 % 的地址")
        return str(address)
    try:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        host = (parsed.hostname or "").rstrip(".").lower()
        if "%" in host:
            raise ValueError
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            host = host.encode("idna").decode("ascii")
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                     for part in host.split(".")):
            raise ValueError
        return host
    except (ValueError, UnicodeError):
        raise ValueError("网址格式无效；不接受含账号密码的链接") from None


def rule_type(value) -> str:
    # The controller has returned both DomainSuffix / IPCIDR and uppercase forms.
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


@dataclass(frozen=True)
class RuleMatch:
    route: str = ""
    rule: str = ""
    certain: bool = False
    reason: str = "没有匹配结果"


def match_rules(host: str, rules: list[dict], mode: str = "rule") -> RuleMatch:
    """Conservative first-match interpretation, without resolving a domain."""
    if mode.lower() == "direct":
        return RuleMatch("DIRECT", "DIRECT 模式", True, "运行模式覆盖了分流规则")
    if mode.lower() == "global":
        return RuleMatch("GLOBAL", "GLOBAL 模式", True, "运行模式覆盖了分流规则")
    if mode.lower() != "rule":
        return RuleMatch(reason="运行模式未知，请刷新检查")
    address = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    uncertain = []
    for entry in rules:
        extra = entry.get("extra") or {}
        if isinstance(extra, dict) and extra.get("disabled") is True:
            continue
        kind, payload, route = rule_type(entry.get("type")), str(entry.get("payload") or ""), str(entry.get("proxy") or "")
        label = clean(f'{entry.get("type", "?")},{payload},{route}')
        hit = False
        if kind in {"DOMAIN", "DOMAINSUFFIX", "DOMAINKEYWORD"}:
            if address is None:
                if not payload:
                    uncertain.append(label)
                    continue
                # Controller payloads are already the kernel's canonical values.
                # Trimming dots or IDNA-encoding a keyword changes its meaning.
                domain = payload.lower()
                if kind == "DOMAIN":
                    hit = host == domain
                elif kind == "DOMAINSUFFIX":
                    hit = host == domain or host.endswith("." + domain)
                else:
                    hit = domain in host
        elif kind in {"IPCIDR", "IPCIDR6"}:
            if address is not None:
                try:
                    hit = address in ipaddress.ip_network(payload, strict=False)
                except ValueError:
                    uncertain.append(label)
            else:
                # /rules does not reliably expose no-resolve: do not assume it.
                uncertain.append(label)
        elif kind in {"MATCH", "FINAL"}:
            hit = True
        else:
            uncertain.append(label)
        if hit:
            if route.upper() == "PASS":
                continue
            if uncertain:
                return RuleMatch(route, label, False, "前置规则需 DNS、IP 属地或连接信息：" + uncertain[0])
            return RuleMatch(route, label, True, "按规则顺序推演；未发起目标请求")
    return RuleMatch(reason="规则依赖额外信息，无法确定" if uncertain else "没有命中已保存的专属规则")


def saved_rules(preferences: dict) -> list[dict]:
    """Reuse deployment's rule ownership (including explicit custom overrides)."""
    plan = local_proxy._service_route_blueprint(preferences)
    domains = plan["proxy_domain_routes"]
    networks = plan["proxy_ip_cidr_routes"]
    return [
        {"type": "DOMAIN-SUFFIX", "payload": host, "proxy": domains[host]}
        for host in sorted(domains, key=lambda domain: (-domain.count("."), -len(domain)))
    ] + [
        {"type": "IP-CIDR6" if ":" in cidr else "IP-CIDR", "payload": cidr, "proxy": networks[cidr]}
        for cidr in sorted(networks, key=lambda value: -ipaddress.ip_network(value, strict=False).prefixlen)
    ]


def health_summary(group: dict, node: dict, now: datetime) -> str:
    return proxy_health_summary(parse_proxy_health(group, node, now, ttl_seconds=HEALTH_TTL))


@dataclass
class RouteSnapshot:
    scope: str
    preferences: dict
    catalog: list[dict]
    runtime: dict = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    error: str = ""
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    saved: list[dict] = field(default_factory=list)
    groups: dict[str, str] = field(default_factory=dict)

    def route_label(self, route: str) -> str:
        return self.groups.get(route) or {"AI-PROXY": "默认代理线路", "DIRECT": "直连", "GLOBAL": "全局策略组"}.get(route) or clean(route)

    def stale(self, now=None) -> bool:
        age = ((now or datetime.now(timezone.utc)) - self.captured_at).total_seconds()
        return age < 0 or age > SNAPSHOT_TTL

    def selected(self, route: str, now=None) -> str:
        proxies = self.runtime.get("proxies") or {}
        group = proxies.get(route) or {}
        current, seen = route, set()
        for _ in range(16):
            if current in {"DIRECT", "REJECT", "REJECT-DROP"}:
                return "直连（未测试连通性）" if current == "DIRECT" else "拒绝连接"
            if current in seen:
                return "策略组循环引用，无法确定节点"
            seen.add(current)
            item = proxies.get(current)
            if not isinstance(item, dict):
                return "节点不可读：" + clean(current)
            if "all" not in item:
                label = self.labels.get(current) or clean(current) + "（内核标识，未映射订阅名称）"
                return f'{label}\n{health_summary(group, item, now or datetime.now(timezone.utc))}'
            current = item.get("now")
            if not current or current not in item.get("all", []):
                return "当前节点未确定（初始化中或按连接选路）"
        return "策略组层级过深，无法确定节点"

    def explain(self, value: str, now=None) -> str:
        host = target_host(value)
        intended = match_rules(host, self.saved)
        lines = [f"目标：{host}", "只分析主机名/IP；路径和查询参数不参与分流，也不会发送或保存。",
                 f"已保存规则：{intended.rule or intended.reason}"]
        if intended.route:
            lines.append("已保存线路：" + self.route_label(intended.route))
        if not self.runtime:
            lines.append("运行状态：" + (self.error or "未检查"))
        else:
            match = match_rules(host, self.runtime["rules"], self.runtime["mode"])
            lines.extend([f"运行规则：{match.rule or '无法确定'}", match.reason])
            if match.certain:
                lines.append("运行线路：" + self.route_label(match.route))
                lines.append("内核当前选择：" + self.selected(match.route, now))
                if intended.certain:
                    lines.append("核对：" + ("此目标的策略组与已保存规则一致" if intended.route == match.route
                                             else "与已保存策略组不同；可能尚未应用或已被其他配置覆盖"))
            elif match.route:
                lines.append("条件候选：" + clean(match.route) + "；不代表最终出口")
        if self.stale(now):
            lines.append("快照已过期，请刷新；以上不是实时状态。")
        lines.append("仅适用于已进入此 mihomo 的流量；不验证浏览器/WSL 是否继承代理，不代表真实出口 IP。")
        return clean_lines(lines)


def clean_lines(lines) -> str:
    return "\n".join(redact_sensitive_text(str(line)) for line in lines)


def service_examples(preferences: dict):
    for item in (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES):
        if item in LOCAL_PROXY_AI_SERVICES or (preferences.get("builtin_sites") or {}).get(item["id"]):
            yield item["id"], item["label"], item["targets"][0]
    for item in preferences.get("custom_targets") or ():
        if item.get("enabled", True):
            value = item["value"]
            if item.get("kind") == "ip-cidr":
                value = str(ipaddress.ip_network(value, strict=False).network_address)
            yield "custom:" + item["id"], item.get("target") or item["value"], value


def snapshot_report(snapshot: RouteSnapshot, now=None) -> str:
    lines = ["运行规则快照 · " + clean(snapshot.scope),
             "读取时间：" + snapshot.captured_at.astimezone().strftime("%m-%d %H:%M:%S"),
             "快照已过期，请刷新。" if snapshot.stale(now) else "快照有效期 60 秒；节点之后仍可能变化。",
             "每个目标展示一个代表域名/IP，不能替代该服务全部子域名或整个 IP 段的检测。", ""]
    if snapshot.error:
        lines.extend([snapshot.error, "仍可在上方推演已保存的专属规则。", ""])
    for _service, label, host in service_examples(snapshot.preferences):
        intended = match_rules(target_host(host), snapshot.saved)
        lines.append(f"{clean(label)}  ·  示例 {clean(host)}")
        lines.append("  已保存线路：" + (snapshot.route_label(intended.route) if intended.route else intended.reason))
        if snapshot.runtime:
            actual = match_rules(target_host(host), snapshot.runtime["rules"], snapshot.runtime["mode"])
            if actual.certain:
                comparison = "策略组一致" if intended.certain and actual.route == intended.route else "策略组不同 / 未单独绑定"
                lines.append(f"  {comparison} · 运行线路：{snapshot.route_label(actual.route)}")
                lines.append("  当前选择：" + snapshot.selected(actual.route, now).replace("\n", "\n  "))
            else:
                lines.append("  无法确定：" + actual.reason)
        else:
            lines.append("  运行配置未确认；不是节点故障结论。")
        lines.append("")
    return clean_lines(lines)


def _validate_runtime(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not isinstance(data.get("proxies"), dict):
        raise ValueError("内核响应格式不完整")
    if len(data["rules"]) > 20000 or len(data["proxies"]) > 10000:
        raise ValueError("内核响应超出诊断上限")
    if not all(isinstance(rule, dict) and all(isinstance(rule.get(key), str) for key in ("type", "payload", "proxy"))
               for rule in data["rules"]):
        raise ValueError("内核规则格式不完整")
    if data.get("mode") not in {"rule", "direct", "global"}:
        raise ValueError("无法识别内核运行模式")
    for item in data["proxies"].values():
        if not isinstance(item, dict):
            raise ValueError("内核节点格式不完整")
        if "all" in item and (not isinstance(item["all"], list) or not all(isinstance(name, str) for name in item["all"])):
            raise ValueError("内核策略组格式不完整")
        if item.get("now") is not None and not isinstance(item["now"], str):
            raise ValueError("内核当前节点格式不完整")
        if item.get("testUrl") is not None and not isinstance(item["testUrl"], str):
            raise ValueError("内核探针地址格式不完整")
    return data


def _read_controller(port: int, *, expected_mixed_port: int | None = None) -> dict:
    namespace = {"port": remote_proxy._normalize_port(port, "控制器端口"),
                 "expected_mixed_port": (remote_proxy._normalize_port(expected_mixed_port, "代理端口")
                                         if expected_mixed_port is not None else None)}
    exec(compile(_CONTROLLER_READER, "<read-only-controller>", "exec"), namespace)
    return _validate_runtime(namespace["result"])


def _read_remote_controller(client, port: int, *, expected_mixed_port: int | None = None) -> dict:
    port = remote_proxy._normalize_port(port, "控制器端口")
    expected = remote_proxy._normalize_port(expected_mixed_port, "代理端口") if expected_mixed_port is not None else None
    command = ("python3 - <<'API_SWITCHER_READ_ONLY_ROUTES'\nport = " + str(port)
               + "\nexpected_mixed_port = " + repr(expected) + "\n" + _CONTROLLER_READER)
    command += "\nprint(json.dumps(result, ensure_ascii=True))\nAPI_SWITCHER_READ_ONLY_ROUTES"
    code, stdout, _stderr = remote_proxy.ssh_manager.execute_command_with_status(
        client, command, timeout=12, log_command=False, max_output_bytes=MAX_BYTES,
    )
    if code != 0:
        raise RuntimeError("远端控制器读取失败；请确认 python3 和受管内核正在运行")
    return _validate_runtime(json.loads(stdout))


def _node_labels(config_text: str) -> dict[str, str]:
    """Match deployed connection fingerprints; cache names are never guessed by order."""
    import yaml

    if not config_text or remote_proxy.AI_PROXY_CONFIG_MARKER not in config_text:
        return {}
    config = yaml.safe_load(config_text)
    if not isinstance(config, dict):
        return {}
    deployed = {}
    for node in config.get("proxies") or ():
        if isinstance(node, dict):
            deployed.setdefault(remote_proxy._proxy_node_connection_key(node), []).append(str(node.get("name") or ""))
    labels = {}
    if not deployed:
        return labels
    for profile in remote_proxy.list_proxy_subscription_profiles():
        try:
            cached = remote_proxy.load_cached_proxy_subscription(profile)
            for entry in cached.nodes if cached else ():
                key = remote_proxy._proxy_node_connection_key(entry.node)
                if key in deployed and (label := clean(entry.node.get("name"))):
                    for alias in deployed.pop(key):
                        labels[alias] = label
                    if not deployed:
                        return labels
        except Exception:
            continue  # An unavailable cache cannot block read-only runtime diagnostics.
    return labels


def load_snapshot(ssh_name: str | None = None) -> RouteSnapshot:
    """Worker entry point. Never install, reconcile env, reload or select a node."""
    preferences = (proxy_routing.load_ssh_routes(ssh_name) if ssh_name is not None else
                   local_proxy._load_local_proxy_routing_preferences_strict())
    snapshot = RouteSnapshot(ssh_name if ssh_name is not None else "Win11 本机（含共享 WSL）",
                             preferences, [])
    snapshot.saved = saved_rules(preferences)
    try:
        snapshot.catalog = [{"id": item["id"], "name": clean(item.get("name")) or "未命名订阅"}
                            for item in remote_proxy.list_proxy_subscription_profiles()]
    except Exception:
        pass
    profiles = {item["id"]: item["name"] for item in snapshot.catalog}
    blueprint = local_proxy._service_route_blueprint(preferences)
    for item in blueprint["requested"].values():
        group = blueprint["service_routes"][item["service_ids"][0]]
        snapshot.groups[group] = profiles.get(item["profile_id"], "订阅已失效") + (
            " · 固定节点" if item["node_key"] else " · 订阅内自动切换")
    config_text = ""
    labels_verified = False
    try:
        if ssh_name is not None:
            status = remote_proxy.inspect_ai_proxy(ssh_name)
            if not status.running:
                snapshot.error = "远端受管代理未运行或无法确认归属；未读取其他代理。"
                return snapshot
            _profile, client = remote_proxy._connect_ssh(ssh_name)
            snapshot.runtime = _read_remote_controller(client, remote_proxy.mihomo_controller_port(7890), expected_mixed_port=7890)
            snapshot.captured_at = datetime.now(timezone.utc)
            try:
                config_text = remote_proxy.ssh_manager.read_remote_file(client, status.config_path, timeout=3, max_bytes=MAX_BYTES) or ""
            except Exception:
                pass
        else:
            state, pid = local_proxy._load_state(), local_proxy._read_pid()
            if not pid or not local_proxy._is_pid_running(pid) or not local_proxy._is_managed_mihomo_pid(pid, state=state):
                snapshot.error = "本机受管代理未运行或无法确认归属；未读取其他代理。"
                return snapshot
            mixed_port = state.get("mixed_port") or local_proxy.DEFAULT_LOCAL_MIXED_PORT
            port = remote_proxy.mihomo_controller_port(mixed_port)
            snapshot.runtime = _read_controller(port, expected_mixed_port=mixed_port)
            snapshot.captured_at = datetime.now(timezone.utc)
            after_state = local_proxy._load_state()
            if any(state.get(key) != after_state.get(key) for key in local_proxy.LOCAL_PROXY_APPLIED_CONFIG_STATE_KEYS):
                raise RuntimeError("已应用配置在检查期间变化，请刷新")
            if pid != local_proxy._read_pid() or not local_proxy._is_managed_mihomo_pid(pid, state=state):
                raise RuntimeError("代理进程在检查期间变化，请刷新")
            try:
                with local_proxy._managed_local_config_path(state).open("rb") as handle:
                    raw = handle.read(MAX_BYTES + 1)
                if len(raw) <= MAX_BYTES:
                    config_text = raw.decode("utf-8")
                labels_verified = local_proxy._applied_local_config_matches(
                    state, local_proxy._managed_local_config_path(state), mixed_port, pid=pid,
                ) and hashlib.sha256(raw).hexdigest() == state.get("applied_config_sha256")
            except (OSError, UnicodeError):
                pass
    except Exception:
        snapshot.runtime = {}
        snapshot.error = "运行配置读取失败或检查期间发生变化。原代理未改动，请稍后刷新或使用“检查代理”。"
    try:
        snapshot.labels = _node_labels(config_text)
        if not labels_verified:
            snapshot.labels = {alias: f"{alias}（部署文件对应：{name}；内存节点参数未核对）"
                               for alias, name in snapshot.labels.items()}
    except Exception:
        pass
    return snapshot
