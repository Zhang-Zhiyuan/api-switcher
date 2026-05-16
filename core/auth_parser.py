import json
import logging
from pathlib import Path

from config.paths import CODEX_AUTH
from core.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def read_codex_auth() -> dict:
    if not CODEX_AUTH.exists():
        return {}
    try:
        return json.loads(CODEX_AUTH.read_text(encoding="utf-8-sig"))
    except Exception as e:
        logger.error(f"Failed to read {CODEX_AUTH}: {e}")
        return {}


def write_codex_auth(data: dict) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(CODEX_AUTH, content)


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
