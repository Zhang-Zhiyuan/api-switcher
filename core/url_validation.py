"""Safe, shared validation for user-configurable API base URLs."""
from __future__ import annotations

import ipaddress
import unicodedata
from urllib.parse import SplitResult, urlsplit, urlunsplit


class APIBaseURLValidationError(ValueError):
    """Raised when an API base URL is unsafe or cannot be requested."""


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _is_allowed_http_host(hostname: str) -> bool:
    """Allow plaintext HTTP only for exact loopback host names/addresses."""
    host = hostname.lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}


def canonicalize_api_base_url(
    value: object,
    *,
    default: object = "",
    allow_implicit_https: bool = True,
) -> str:
    """Validate and normalize an API base URL.

    HTTPS is required for remote endpoints.  Plain HTTP is accepted only for
    the exact local loopback hosts used by local gateways.  A missing scheme
    is interpreted as HTTPS when ``allow_implicit_https`` is true.  Paths and
    query strings are retained, while fragments and embedded credentials are
    rejected so API secrets cannot be sent to an attacker-controlled host via
    URL userinfo.
    """
    raw_text = str(value or "")
    if _has_control_character(raw_text):
        raise APIBaseURLValidationError("API 端点不能包含控制字符")
    text = raw_text.strip()
    if not text:
        raw_default = str(default or "")
        if _has_control_character(raw_default):
            raise APIBaseURLValidationError("API 端点不能包含控制字符")
        text = raw_default.strip()
    if not text:
        raise APIBaseURLValidationError("API 端点为空")
    if any(character.isspace() for character in text):
        raise APIBaseURLValidationError("API 端点不能包含空白字符")
    if "\\" in text:
        raise APIBaseURLValidationError("API 端点不能包含反斜杠")

    implicit_https = "://" not in text
    if implicit_https:
        if not allow_implicit_https:
            raise APIBaseURLValidationError("API 端点必须以 https:// 开头")
        text = f"https://{text}"

    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise APIBaseURLValidationError("API 端点格式无效") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise APIBaseURLValidationError("API 端点只支持 http:// 或 https://")
    if parsed.fragment:
        raise APIBaseURLValidationError("API 端点不能包含 URL fragment")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise APIBaseURLValidationError("API 端点不能包含用户名或密码")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise APIBaseURLValidationError("API 端点的主机或端口无效") from exc
    if not hostname:
        raise APIBaseURLValidationError("API 端点缺少主机名")
    if implicit_https and "." not in hostname and hostname.lower() != "localhost":
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise APIBaseURLValidationError("省略协议时必须填写完整主机名或 IP 地址") from exc
    if scheme == "http" and not _is_allowed_http_host(hostname):
        raise APIBaseURLValidationError("远程 API 端点必须使用 HTTPS；HTTP 仅允许本机回环地址")

    # Rebuild the authority without userinfo and normalize the host/scheme.
    # ``urlsplit().hostname`` removes IPv6 brackets, so add them back.
    normalized_host = hostname.lower()
    try:
        if ipaddress.ip_address(normalized_host).version == 6:
            normalized_host = f"[{normalized_host}]"
    except ValueError:
        pass
    authority = normalized_host if port is None else f"{normalized_host}:{port}"
    path = parsed.path.rstrip("/")
    normalized = SplitResult(scheme, authority, path, parsed.query, "")
    return urlunsplit(normalized)


def validate_api_base_url(
    value: object,
    *,
    default: object = "",
    allow_implicit_https: bool = True,
) -> str:
    """Return the canonical URL, raising ``ValueError`` when it is invalid."""
    return canonicalize_api_base_url(
        value,
        default=default,
        allow_implicit_https=allow_implicit_https,
    )


def is_valid_api_base_url(
    value: object,
    *,
    default: object = "",
    allow_implicit_https: bool = True,
) -> bool:
    """Boolean counterpart to :func:`validate_api_base_url`."""
    try:
        validate_api_base_url(
            value,
            default=default,
            allow_implicit_https=allow_implicit_https,
        )
    except (TypeError, ValueError):
        return False
    return True
