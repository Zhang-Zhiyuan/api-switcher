import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from config.paths import CODEX_AUTH

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def read_codex_auth() -> dict:
    if not CODEX_AUTH.exists():
        return {}
    try:
        return json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read {CODEX_AUTH}: {e}")
        return {}


def write_codex_auth(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CODEX_AUTH, content)


def apply_codex_oauth(auth: dict, profile) -> dict:
    """Apply OAuth tokens from a CodexProfile to auth.json."""
    from core import security

    auth = dict(auth)
    auth["auth_mode"] = "chatgpt"

    tokens_data = security.get_secret_json(profile.oauth_tokens_ref)
    if tokens_data:
        auth["tokens"] = tokens_data
    elif profile.oauth_tokens_ref:
        logger.warning("OAuth token reference exists but no valid token JSON was found")
        auth["tokens"] = {}
    else:
        auth["tokens"] = {}

    if tokens_data and profile.last_refresh:
        auth["last_refresh"] = profile.last_refresh
    else:
        auth["last_refresh"] = None

    # Clear API key if switching to OAuth
    auth["OPENAI_API_KEY"] = None

    return auth


def apply_codex_apikey(auth: dict, profile) -> dict:
    """Apply API key from a CodexProfile to auth.json."""
    from core import security

    auth = dict(auth)
    auth["auth_mode"] = "api_key"

    api_key = security.get_secret(profile.api_key_ref)
    if api_key:
        auth["OPENAI_API_KEY"] = api_key
    elif profile.api_key_ref:
        logger.warning("API key reference exists but no secret value was found")
        auth["OPENAI_API_KEY"] = None
    else:
        auth["OPENAI_API_KEY"] = None

    # Clear OAuth tokens if switching to API key
    auth["tokens"] = {}
    auth["last_refresh"] = None

    return auth


def extract_oauth_meta(auth: dict) -> dict:
    """Extract OAuth metadata from current auth.json."""
    tokens = auth.get("tokens", {})
    return {
        "auth_mode": auth.get("auth_mode", "chatgpt"),
        "last_refresh": auth.get("last_refresh"),
        "account_id": tokens.get("account_id"),
    }


def get_token_expiry(token: str) -> datetime | None:
    """Parse JWT and return expiry datetime. No network call needed."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None
