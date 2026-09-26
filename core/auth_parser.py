import json
import logging
import base64
import math
from datetime import datetime, timezone
from pathlib import Path

from config.paths import CODEX_AUTH
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache

logger = logging.getLogger(__name__)
_JSON_FILE_CACHE = FileValueCache()


def normalize_codex_token_fields(auth: dict) -> dict:
    """Canonicalize legacy field aliases without modifying the source snapshot."""
    result = dict(auth)
    raw_tokens = auth.get("tokens")
    if not isinstance(raw_tokens, dict):
        return result
    tokens = dict(raw_tokens)
    for canonical, alias in (("id_token", "idToken"), ("access_token", "accessToken"),
                             ("refresh_token", "refreshToken"), ("account_id", "accountId")):
        first, second = tokens.get(canonical), tokens.get(alias)
        if first and second and first != second:
            raise ValueError(f"Codex 登录字段 {canonical}/{alias} 冲突，请重新导出当前登录")
        if not first and alias in tokens:
            tokens[canonical] = second
        tokens.pop(alias, None)
    result["tokens"] = tokens
    return result


def _auth_timestamp(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        return result if math.isfinite(result) and result > 0 else 0.0
    except (ValueError, OverflowError, OSError):
        return 0.0


def _unique_jwt_claims(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate token claim")
        result[key] = value
    return result


def _invalid_jwt_constant(_value: str):
    raise ValueError("Invalid token JSON number")


def codex_token_claims(token: object) -> dict:
    """Read metadata only; decoding does not verify a JWT or validate a login."""
    if not isinstance(token, str) or len(token) > 1024 * 1024:
        return {}
    try:
        encoded = token.split(".")[1]
        # Match the client's strict JSON decoding: never silently pick the last
        # duplicated claim, accept NaN/Infinity, or discard base64 junk bytes.
        claims = json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True),
            object_pairs_hook=_unique_jwt_claims, parse_constant=_invalid_jwt_constant,
        )
        return claims if isinstance(claims, dict) else {}
    except (ValueError, IndexError, UnicodeError, RecursionError):
        return {}


def codex_auth_is_newer(candidate: dict, other: dict) -> bool:
    """Compare only mutually available metadata for an already-matched account.

    A rotated bundle's last_refresh outranks JWT expiry (different token kinds
    can have different lifetimes). Missing metadata is unknown, not "older".
    Never synthesize a last_refresh or assemble tokens from different bundles.
    """
    left = candidate.get("tokens") or {}
    right = other.get("tokens") or {}
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_claims = codex_token_claims(left.get("access_token") or left.get("accessToken"))
    right_claims = codex_token_claims(right.get("access_token") or right.get("accessToken"))
    for first, second in ((candidate.get("last_refresh"), other.get("last_refresh")),
                          (left_claims.get("iat"), right_claims.get("iat")),
                          (left_claims.get("exp"), right_claims.get("exp"))):
        a, b = _auth_timestamp(first), _auth_timestamp(second)
        if a and b and a != b:
            return a > b
    return False


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def clear_codex_auth_cache(path: Path | None = None) -> None:
    _JSON_FILE_CACHE.clear(path)


def read_codex_auth() -> dict:
    try:
        CODEX_AUTH.stat()
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(CODEX_AUTH, {})
        return {}
    except OSError as e:
        logger.error("Failed to access %s: %s", CODEX_AUTH, e)
        _JSON_FILE_CACHE.clear(CODEX_AUTH)
        raise

    cached = _JSON_FILE_CACHE.get(CODEX_AUTH)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    try:
        data = json.loads(CODEX_AUTH.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"{CODEX_AUTH} 的顶层 JSON 必须是对象")
        _JSON_FILE_CACHE.set(CODEX_AUTH, data)
        return data
    except FileNotFoundError:
        _JSON_FILE_CACHE.set(CODEX_AUTH, {})
        return {}
    except Exception as e:
        logger.error("Failed to read %s: %s", CODEX_AUTH, e)
        _JSON_FILE_CACHE.clear(CODEX_AUTH)
        raise


def write_codex_auth(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CODEX_AUTH, content)
    _JSON_FILE_CACHE.set(CODEX_AUTH, data)


def apply_codex_apikey(auth: dict, profile) -> dict:
    """Remove stale Codex API-key auth while preserving official login tokens.

    Third-party Codex providers read their key from the env var named by
    config.toml's model_providers.<id>.env_key. Keeping that secret in auth.json
    collides with official ChatGPT login snapshots and makes provider-specific
    env_key switching harder to reason about.
    """
    if getattr(profile, "custom_requires_openai_auth", False):
        return dict(auth or {})
    return clear_codex_api_auth(auth)


def clear_codex_api_auth(auth: dict) -> dict:
    """Remove Codex API-key auth while preserving official ChatGPT tokens."""
    auth = dict(auth or {})
    auth.pop("OPENAI_API_KEY", None)

    mode = str(auth.get("auth_mode") or "").strip().lower()
    if mode in {"apikey", "api_key"}:
        tokens = auth.get("tokens")
        if isinstance(tokens, dict) and any(bool(value) for value in tokens.values()):
            auth["auth_mode"] = "chatgpt"
        else:
            auth.pop("auth_mode", None)

    return auth
