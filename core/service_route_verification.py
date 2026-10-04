"""Bounded, anonymous observations of loaded service routes, never route repair."""
from __future__ import annotations

import json
import http.client
import queue
import shlex
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES, LOCAL_PROXY_BUILTIN_SITES
from core.proxy_update_result import update_result


TIMEOUT = 3
DEADLINE = 9
WORKERS = 4
_AI_LABELS = {"openai": "OpenAI API", "claude": "Claude/Anthropic", "google_ai": "Gemini/Google AI"}
_BUILTINS = {item["id"]: item for item in (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES)}


def bound_route_targets(preferences):
    """Only built-in bound targets have safe anonymous probe URLs.

    A routing-only reload rebuilds all source pools; do not restrict observation
    to the first source that happened to trigger a batched refresh.
    """
    bindings = preferences.get("service_profile_bindings", {})
    modes = preferences.get("service_route_modes", {})
    sites = preferences.get("builtin_sites", {})
    result = []
    for service in sorted(set(bindings) | set(modes)):
        item = _BUILTINS.get(service)
        reason = ""
        if modes.get(service) in {"default", "direct"}:
            reason = "默认或直连目标，本次不主动探测"
        elif item is None:
            reason = "自定义目标没有内置匿名探针"
        elif service not in _AI_LABELS and sites.get(service) is not True:
            reason = "目标已关闭"
        result.append({"service": service, "label": item["label"] if item else "自定义目标",
                       "probe_label": _AI_LABELS.get(service, service),
                       "url": item["health_check_url"] if item else "",
                       "expected": item["health_check_expected_status"] if item else "",
                       "reason": reason})
    return tuple(result)


def record(target, status, detail="", *, warning=None):
    return {"service": target["service"], "label": target["label"], "status": status, "detail": detail,
            "warning": status != "passed" if warning is None else bool(warning)}


def unverified(targets, reason, *, warning=True):
    return tuple(record(target, "unverified", target["reason"] or reason,
                        warning=warning and not bool(target["reason"])) for target in targets)


def verify_targets(targets, probe, *, deadline=DEADLINE, timeout=TIMEOUT, cancelled=None):
    """At most four daemon workers; one wall-clock budget, including slow reads.

    Probe implementations own/close their short-lived sockets. No thread can
    mutate routes; a timed-out observation cannot later overwrite the result.
    """
    stop = threading.Event()
    until = time.monotonic() + max(0, min(DEADLINE, float(deadline)))
    jobs, done = queue.Queue(), queue.Queue()
    results = {}
    for index, target in enumerate(targets):
        if target["reason"]:
            results[index] = record(target, "unverified", target["reason"], warning=False)
        else:
            jobs.put((index, target))

    def worker():
        while not stop.is_set() and time.monotonic() < until:
            if cancelled is not None and cancelled():
                return
            try:
                index, target = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                status, detail = probe(target, min(timeout, max(0.1, until - time.monotonic())))
                if status not in {"passed", "failed", "unverified"}:
                    status, detail = "unverified", "探针返回无效状态"
            except Exception:
                status, detail = "failed", "匿名探针执行失败"
            done.put((index, record(target, status, detail)))

    for _ in range(min(WORKERS, jobs.qsize())):
        threading.Thread(target=worker, name="route-short-probe", daemon=True).start()
    while len(results) < len(targets) and time.monotonic() < until:
        if cancelled is not None and cancelled():
            break
        try:
            index, result = done.get(timeout=min(0.1, max(0.001, until - time.monotonic())))
            results[index] = result
        except queue.Empty:
            continue
    stop.set()
    return tuple(results.get(index) or record(target, "unverified", "探测取消或超过本轮等待时间")
                 for index, target in enumerate(targets))


def attach_verification(value, records):
    records = tuple(records)
    counts = {status: sum(row["status"] == status for row in records)
              for status in ("passed", "failed", "unverified")}
    detail = (f"匿名目标探测：通过 {counts['passed']} / 失败 {counts['failed']} / 未验证 {counts['unverified']}"
              "（仅代表本次匿名探测，不代表账号登录或 API 调用可用）")
    if records:
        detail += "；" + "；".join(f"{row['label']}：{dict(passed='通过', failed='失败', unverified='未验证')[row['status']]}"
                                  + (f"（{row['detail']}）" if row["detail"] else "") for row in records)
    loaded = "；配置已加载" if getattr(value, "outcome", "unknown") == "applied" else ""
    return update_result(f"{value}{loaded}；{detail}", getattr(value, "outcome", "unknown"),
                         retryable=getattr(value, "retryable", False),
                         warning=getattr(value, "warning", False) or any(row.get("warning", row["status"] != "passed") for row in records),
                         route_verification=records)


def classify_response(target, status, body):
    """No HTML/error page may masquerade as an anonymous AI challenge."""
    service = target["service"]
    if service in _AI_LABELS or service == "discord":
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = {}
        if status == 200:
            valid = isinstance(parsed, dict) and "error" not in parsed
            if service == "openai":
                valid = valid and parsed.get("object") == "list" and isinstance(parsed.get("data"), list)
            elif service == "claude":
                valid = valid and isinstance(parsed.get("data"), list) and isinstance(parsed.get("has_more"), bool)
            elif service == "google_ai":
                valid = valid and isinstance(parsed.get("models"), list)
            else:
                gateway = urlsplit(str(parsed.get("url") or "")) if valid else None
                valid = bool(gateway and gateway.scheme == "wss" and gateway.hostname == "gateway.discord.gg")
            return ("passed" if valid else "failed"), "HTTP 200 " + ("匿名响应结构符合预期" if valid else "匿名响应结构不符合预期")
        if service == "discord":
            return "failed", f"HTTP {status} 未收到 gateway 响应"
        error = parsed.get("error") if isinstance(parsed, dict) else None
        error = error if isinstance(error, dict) else {}
        message = " ".join(str(error.get(key) or "") for key in ("message", "type", "status", "code")).casefold()
        denied = any(word in message for word in ("region", "country", "location", "not supported", "disabled", "quota", "policy", "blocked"))
        auth = any(word in message for word in ("api key", "authentication", "unauthorized", "invalid_api_key",
                                               "x-api-key", "anthropic-version", "credential", "unregistered caller"))
        allowed = {401} if service == "openai" else {400, 401} if service == "claude" else {400, 401, 403}
        ok = bool(error) and auth and not denied and status in allowed
    else:
        expected = {int(value) for value in target["expected"].split("/") if value.isdigit()}
        ok = status in expected and status < 300
        if status == 204:
            ok = ok and not body.strip()
        # These built-in robots/text checks must not accept captive HTML pages.
        elif service != "telegram" and ("<html" in body.casefold() or "<!doctype html" in body.casefold()):
            ok = False
        elif service == "telegram":
            ok = ok and "telegram" in body.casefold() and "<html" in body.casefold()
            return ("passed" if ok else "failed"), f"HTTP {status} 仅页面 HTTP 短测"
    return ("passed" if ok else "failed"), f"HTTP {status} " + ("匿名响应符合预期" if ok else "匿名响应不符合预期")


def probe_target(target, port, timeout=TIMEOUT):
    """Explicit loopback CONNECT, no environment proxy bypass or redirects."""
    builtin = _BUILTINS.get(target["service"])
    if not builtin or target["url"] != builtin["health_check_url"]:
        return "unverified", "非内置匿名探针"
    parsed = urlsplit(target["url"])
    connection = http.client.HTTPSConnection("127.0.0.1", int(port), timeout=timeout,
                                           context=ssl.create_default_context())
    active_socket = []
    expired = threading.Event()
    deadline = time.monotonic() + timeout

    def close():
        expired.set()
        for sock in [*active_socket, connection.sock]:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        connection.close()

    timer = threading.Timer(timeout, close)
    timer.daemon = True
    timer.start()
    try:
        connection.set_tunnel(parsed.hostname, 443)
        connection.connect()
        active_socket.append(connection.sock)
        if expired.is_set() or time.monotonic() >= deadline:
            return "failed", "匿名探测超时"
        connection.request("GET", parsed.path or "/", headers={"Accept": "application/json,text/plain,*/*",
                                                               "User-Agent": "API-Switcher/1.0"})
        if expired.is_set() or time.monotonic() >= deadline:
            return "failed", "匿名探测超时"
        response = connection.getresponse()
        body = response.read(8192 if target["service"] == "telegram" else 65537)
        if expired.is_set() or time.monotonic() >= deadline:
            return "failed", "匿名探测超时"
        if len(body) > 65536:
            return "failed", "匿名响应超过大小限制"
        return classify_response(target, response.status, body.decode("utf-8", errors="replace"))
    except Exception:
        return "failed", "连接失败或匿名探测超时"
    finally:
        timer.cancel()
        close()


def build_remote_command(targets, port, *, config_path="", expected_config=""):
    """Standalone stdlib script: also available in frozen builds, no user URLs."""
    active = []
    for target in targets:
        builtin = _BUILTINS.get(target["service"])
        if target["reason"] or not builtin:
            continue
        if target["url"] != builtin["health_check_url"]:
            raise ValueError("非内置匿名探针")
        active.append({key: target[key] for key in ("service", "url", "expected")})
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("代理端口无效")
    import hashlib
    fingerprint = hashlib.sha256(expected_config.encode("utf-8")).hexdigest() if expected_config else ""
    return ("if command -v python3 >/dev/null 2>&1; then\npython3 - "
            + shlex.quote(json.dumps(active, ensure_ascii=True)) + " " + str(port)
            + " " + shlex.quote(config_path) + " " + shlex.quote(fingerprint)
            + " <<'API_SWITCHER_ROUTE_PROBE'\n" + _REMOTE_SCRIPT
            + "\nAPI_SWITCHER_ROUTE_PROBE\nelse\nprintf '%s\\n' '[]'\nfi")


def parse_remote_records(targets, code, output):
    try:
        rows = json.loads(output) if code == 0 else []
        if not isinstance(rows, list):
            rows = []
    except (TypeError, ValueError):
        rows = []
    groups = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("service"), str):
            groups.setdefault(row["service"], []).append(row)
    result = []
    for target in targets:
        matches = groups.get(target["service"], [])
        if target["reason"]:
            result.append(record(target, "unverified", target["reason"], warning=False))
        elif len(matches) == 1 and matches[0].get("status") in {"passed", "failed"}:
            # Do not trust arbitrary remote text; only return a numeric status.
            status = matches[0]["status"]
            http_status = matches[0].get("http_status")
            detail = f"HTTP {http_status} 匿名响应" if isinstance(http_status, int) and 100 <= http_status <= 599 else "连接失败或探测超时"
            result.append(record(target, status, detail))
        else:
            result.append(record(target, "unverified", "远端探针缺失、取消或超过等待时间"))
    return tuple(result)


_REMOTE_SCRIPT = r'''
import hashlib, http.client, json, os, pathlib, queue, socket, ssl, sys, threading, time
from urllib.parse import urlsplit
targets, port = json.loads(sys.argv[1]), int(sys.argv[2])
jobs, results = queue.Queue(), queue.Queue()
deadline = time.monotonic() + 9
config_path = sys.argv[3] if len(sys.argv) > 3 else ''
expected = sys.argv[4] if len(sys.argv) > 4 else ''
def identity():
    if not config_path: return 'synthetic-no-config-guard'
    try:
        path = pathlib.Path(config_path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected: return None
        pid_path = path.parent.parent / 'api-switcher' / 'ai-proxy.pid'
        pid = int(pid_path.read_text().strip())
        if pid <= 1: return None
        os.kill(pid, 0)
        args = pathlib.Path('/proc/%s/cmdline' % pid).read_bytes().decode().split('\x00')
        owned = any(args[index] == '-f' and args[index+1] == str(path)
                    or args[index] == '-d' and args[index+1] == str(path.parent)
                    for index in range(len(args)-1))
        return pid if owned else None
    except Exception: return None
original_identity = identity()
if original_identity is None:
    print('[]')
    raise SystemExit(0)
for item in targets:
    jobs.put(item)

def probe(item):
    remaining = min(3, max(0.1, deadline - time.monotonic()))
    conn = http.client.HTTPSConnection('127.0.0.1', port, timeout=remaining, context=ssl.create_default_context())
    sockets = []
    expired = threading.Event()
    probe_deadline = time.monotonic() + remaining
    code = 0
    def close():
        expired.set()
        for sock in sockets + [conn.sock]:
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        conn.close()
    timer = threading.Timer(remaining, close)
    timer.daemon = True
    timer.start()
    try:
        target = urlsplit(item['url'])
        conn.set_tunnel(target.hostname, 443)
        conn.connect()
        sockets.append(conn.sock)
        if expired.is_set() or time.monotonic() >= probe_deadline: return False, code
        conn.request('GET', target.path or '/', headers={'Accept':'application/json,text/plain,*/*','User-Agent':'API-Switcher/1.0'})
        if expired.is_set() or time.monotonic() >= probe_deadline: return False, code
        response = conn.getresponse()
        code = response.status
        data = response.read(8192 if item['service'] == 'telegram' else 65537)
        if expired.is_set() or time.monotonic() >= probe_deadline: return False, code
        body = data.decode('utf-8', errors='replace')
        if len(data) > 65536:
            return False, code
        service = item['service']
        if service in ('openai', 'claude', 'google_ai', 'discord'):
            try: value = json.loads(body)
            except (ValueError, TypeError): value = {}
            if code == 200:
                valid = isinstance(value, dict) and 'error' not in value
                if service == 'openai': valid = valid and value.get('object') == 'list' and isinstance(value.get('data'), list)
                elif service == 'claude': valid = valid and isinstance(value.get('data'), list) and isinstance(value.get('has_more'), bool)
                elif service == 'google_ai': valid = valid and isinstance(value.get('models'), list)
                else:
                    gateway = urlsplit(str(value.get('url') or '')) if valid else None
                    valid = bool(gateway and gateway.scheme == 'wss' and gateway.hostname == 'gateway.discord.gg')
                return valid, code
            if service == 'discord': return False, code
            error = value.get('error') if isinstance(value, dict) else None
            error = error if isinstance(error, dict) else {}
            message = ' '.join(str(error.get(key) or '') for key in ('message','type','status','code')).casefold()
            denied = any(word in message for word in ('region','country','location','not supported','disabled','quota','policy','blocked'))
            auth = any(word in message for word in ('api key','authentication','unauthorized','invalid_api_key','x-api-key','anthropic-version','credential','unregistered caller'))
            allowed = {401} if service == 'openai' else {400,401} if service == 'claude' else {400,401,403}
            return bool(error) and auth and not denied and code in allowed, code
        ok = code in {int(value) for value in item['expected'].split('/') if value.isdigit()} and code < 300
        if code == 204: ok = ok and not body.strip()
        elif service != 'telegram' and ('<html' in body.casefold() or '<!doctype html' in body.casefold()): ok = False
        elif service == 'telegram': ok = ok and 'telegram' in body.casefold() and '<html' in body.casefold()
        return ok, code
    except Exception:
        return False, code
    finally:
        timer.cancel()
        close()

def worker():
    while time.monotonic() < deadline:
        try: item = jobs.get_nowait()
        except queue.Empty: return
        ok, code = probe(item)
        results.put({'service':item['service'], 'status':'passed' if ok else 'failed', 'http_status':code})

for _ in range(min(4, len(targets))):
    threading.Thread(target=worker, daemon=True).start()
collected = []
while len(collected) < len(targets) and time.monotonic() < deadline:
    try: collected.append(results.get(timeout=min(0.1, max(0.001, deadline-time.monotonic()))))
    except queue.Empty: pass
print(json.dumps(collected if identity() == original_identity else []))
'''
