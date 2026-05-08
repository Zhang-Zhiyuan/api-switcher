import json
import logging
from pathlib import Path

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


def apply_codex_apikey(auth: dict, profile) -> dict:
    """Apply API key from a CodexProfile to auth.json."""
    from core import security

    # Third-party API profiles must override any official ChatGPT login state
    # currently stored in auth.json. Do not resurrect older auth snapshots here.
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

    # Clear official-login tokens when switching to a third-party API profile.
    auth["tokens"] = {}
    auth["last_refresh"] = None

    return auth
