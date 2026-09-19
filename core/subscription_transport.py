"""Bounded redirects and credential-safe diagnostics for subscription downloads."""

from __future__ import annotations

import math
import re
import string
import time
from urllib import parse as urlparse
from urllib import request as urlrequest

from core.redaction import redact_sensitive_text


class SubscriptionRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Keep every HTTP hop inside one deadline and the chosen proxy transport.

    urllib's default handler allows FTP redirects and drains redirect bodies
    without a byte limit. Neither is appropriate for credential-bearing URLs.
    """

    def __init__(self, deadline: float | None = None, *, strict: bool = True):
        self.deadline = float(deadline) if deadline is not None else None
        self.strict = bool(strict)

    def _remaining_timeout(self, request) -> float:
        remaining = None
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise TimeoutError("订阅下载超过总等待时间")
        original = getattr(request, "timeout", None)
        if isinstance(original, (int, float)) and not isinstance(original, bool):
            original = float(original)
            if math.isfinite(original) and original > 0:
                return min(original, remaining) if remaining is not None else original
        return remaining if remaining is not None else 15.0

    def http_request(self, request):
        request.timeout = self._remaining_timeout(request)
        return request

    https_request = http_request

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parsed = urlparse.urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("订阅重定向仅支持有效的 HTTP/HTTPS 地址")
        return (
            parsed.scheme.casefold(), parsed.hostname.casefold(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme.casefold() == "https" else 80),
        )

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("Location") or headers.get("URI")
        if location is None:
            return None
        try:
            timeout = self._remaining_timeout(req)
            try:
                # HTTP headers are decoded as Latin-1 by http.client. Match
                # urllib's escaping without decoding/re-encoding signed queries.
                escaped = urlparse.quote(str(location), encoding="iso-8859-1", safe=string.punctuation)
                target = urlparse.urljoin(req.full_url, escaped)
                original_origin = self._origin(req.full_url)
                target_origin = self._origin(target)
            except (ValueError, UnicodeError):
                raise ValueError("订阅服务器返回了无效或不支持的重定向地址") from None
            if self.strict and original_origin[0] == "https" and target_origin[0] != "https":
                raise ValueError("订阅重定向试图从 HTTPS 降级为 HTTP，已拒绝发送订阅凭据")
            new = self.redirect_request(req, fp, code, msg, headers, target)
            if new is None:
                return None
            visited = dict(getattr(req, "redirect_dict", {}))
            if visited.get(target, 0) >= self.max_repeats or len(visited) >= self.max_redirections:
                raise ValueError("订阅重定向次数过多或发生循环，已停止下载")
            visited[target] = visited.get(target, 0) + 1
            new.redirect_dict = visited
            new.remove_header("Proxy-authorization")
            if target_origin != original_origin:
                for name in ("Authorization", "Cookie"):
                    new.remove_header(name)
            # The standard HTTP handler sends Connection: close. Close the
            # discarded response now; reading it could consume unlimited time
            # and memory before the next hop's timeout even starts.
        finally:
            fp.close()
        return self.parent.open(new, timeout=min(timeout, self._remaining_timeout(req)))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_DIAGNOSTIC_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_SECRET_QUERY_KEY = re.compile(
    r"(?i)(?:api[_-]?key|key|token|(?:access|refresh|id|auth)[_-]?token|"
    r"auth|authorization|signature|sig|secret|password|passphrase)\Z"
)


def subscription_error_message(error: object, *, url: str = "", max_length: int = 600) -> str:
    """Hide subscription URLs, including credentials in unnamed path/query fields.

    Callers should raise the returned diagnostic ``from None`` when publishing
    it, so the original exception's unsafe message is not printed as a cause.
    """

    secrets = set()
    raw_url = str(url or "")
    if raw_url:
        secrets.add(raw_url)
        try:
            parsed = urlparse.urlsplit(raw_url)
            secrets.update(value for value in (parsed.username, parsed.password, parsed.query) if value)
            if parsed.path and parsed.path != "/":
                secrets.add(parsed.path)
            secrets.update(
                value for key, value in urlparse.parse_qsl(parsed.query, keep_blank_values=False)
                if len(value) >= 8 or _SECRET_QUERY_KEY.fullmatch(key)
            )
            selector = urlparse.urlunsplit(("", "", parsed.path, parsed.query, ""))
            if selector and selector != "/":
                secrets.add(selector)
            secrets.update(part for part in parsed.path.split("/") if len(part) >= 8)
        except ValueError:
            pass
        for value in tuple(secrets):
            secrets.add(urlparse.unquote(value))
            secrets.add(value.encode("unicode_escape").decode("ascii"))
    text = redact_sensitive_text(error, secrets=secrets)
    # Redirect exceptions can mention a different URL whose credentials were
    # never in the original request. Do not assume familiar parameter names.
    text = _DIAGNOSTIC_URL.sub("[订阅地址已隐藏]", text)
    text = " ".join(text.split())
    return redact_sensitive_text(text, max_length=max_length)
