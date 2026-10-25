"""Bounded, credential-free diagnostics for temporary remote proxy cores."""

import json

from core.redaction import redact_sensitive_text


CORE_DIAGNOSTIC_MARKER = "candidate_diagnostic\t"
CORE_FAILURE_REASONS = {
    "dns": "节点或上游域名解析失败",
    "timeout": "节点或上游连接超时",
    "refused": "节点或上游拒绝连接",
    "certificate": "TLS 证书校验失败",
    "authentication": "节点认证失败",
    "handshake": "代理协议握手失败",
}

# Runs before deleting the isolated core directory. Emit only fixed categories
# and known probe hosts; raw core logs may contain subscription credentials.
CORE_DIAGNOSTIC_READER = r'''
import json
import sys

hosts = ("api.openai.com", "chatgpt.com", "api.anthropic.com",
         "generativelanguage.googleapis.com")
patterns = (
    ("dns", ("dns resolve failed", "no such host", "couldn't find ip", "no ip address")),
    ("certificate", ("certificate verify failed", "x509:", "unknown authority")),
    ("authentication", ("authentication failed", "unauthorized", "invalid password")),
    ("refused", ("connection refused",)),
    ("timeout", ("i/o timeout", "context deadline exceeded", "timed out")),
    ("handshake", ("handshake failed", "tls handshake",)),
)
try:
    with open(sys.argv[1], "rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        lines = stream.read(65536).decode("utf-8", "replace").splitlines()
    reasons = {}
    for line in lines:
        text = line.lower()
        if "error" not in text and "failed" not in text:
            continue
        category = next((code for code, tokens in patterns if any(token in text for token in tokens)), None)
        if category:
            # Prefer a concrete DNS/certificate cause over a later timeout.
            for host in hosts:
                if host + ":443" in text and reasons.get(host) not in ("dns", "certificate"):
                    reasons[host] = category
    if reasons:
        print("candidate_diagnostic\t" + json.dumps(reasons, sort_keys=True))
except OSError:
    pass
'''


def candidate_failure_details(results, output: str, targets) -> str:
    """Deduplicate rounds and prefer core causes over a secondary TLS EOF."""
    from urllib.parse import urlsplit

    diagnostics = {}
    for line in str(output or "").splitlines():
        if line.startswith(CORE_DIAGNOSTIC_MARKER) and len(line) < 4096:
            try:
                value = json.loads(line[len(CORE_DIAGNOSTIC_MARKER):])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                diagnostics.update({key: code for key, code in value.items()
                                    if isinstance(code, str) and code in CORE_FAILURE_REASONS})
    hosts = {label: urlsplit(url).hostname for label, url in targets}
    details = []
    for label, host in hosts.items():
        result = next((item for item in results if item.label == label and not item.ok), None)
        if result is None:
            continue
        code = diagnostics.get(host)
        reason = CORE_FAILURE_REASONS.get(code)
        if not reason:
            reason = " ".join(redact_sensitive_text(result.detail).split())[:180] or "连接失败"
        details.append(f"{label}：{reason}")
    return "；".join(details)
