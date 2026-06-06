import json
import logging
from pathlib import Path

from config.paths import CODEX_AUTH
from core.atomic_io import atomic_write_text
from core.file_cache import CACHE_MISS, FileValueCache

logger = logging.getLogger(__name__)
_JSON_FILE_CACHE = FileValueCache()


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def clear_codex_auth_cache(path: Path | None = None) -> None:
    _JSON_FILE_CACHE.clear(path)


def read_codex_auth() -> dict:
    cached = _JSON_FILE_CACHE.get(CODEX_AUTH)
    if cached is not CACHE_MISS:
        return cached if isinstance(cached, dict) else {}

    if not CODEX_AUTH.exists():
        _JSON_FILE_CACHE.set(CODEX_AUTH, {})
        return {}
    try:
        data = json.loads(CODEX_AUTH.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            logger.error(f"Failed to read {CODEX_AUTH}: top-level JSON is not an object")
            _JSON_FILE_CACHE.set(CODEX_AUTH, {})
            return {}
        _JSON_FILE_CACHE.set(CODEX_AUTH, data)
        return data
    except Exception as e:
        logger.error(f"Failed to read {CODEX_AUTH}: {e}")
        _JSON_FILE_CACHE.clear(CODEX_AUTH)
        return {}


def write_codex_auth(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CODEX_AUTH, content)
    _JSON_FILE_CACHE.set(CODEX_AUTH, data)


def apply_codex_apikey(auth: dict, profile) -> dict:
    """Apply API key from a CodexProfile to auth.json."""
    from core import security

    # Third-party API profiles must override any official ChatGPT login state.
    # Newer Codex CLI builds parse optional token/refresh fields strictly, so
    # keep API-key auth minimal instead of leaving stale ChatGPT fields behind.
    auth = {"auth_mode": "apikey"}

    api_key = security.get_secret(profile.api_key_ref)
    if api_key:
        auth["OPENAI_API_KEY"] = api_key
    elif profile.api_key_ref:
        logger.warning("API key reference exists but no secret value was found")
        auth["OPENAI_API_KEY"] = ""
    else:
        auth["OPENAI_API_KEY"] = ""

    return auth


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
