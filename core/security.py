import json
import zlib
import base64
import importlib
import logging
import hashlib
import re
import threading
from pathlib import Path

from config.paths import KEYRING_SERVICE, SECRETS_DIR

logger = logging.getLogger(__name__)
_KEYRING = None
_KEYRING_LOCK = threading.RLock()


def _keyring():
    global _KEYRING
    if _KEYRING is not None:
        return _KEYRING
    with _KEYRING_LOCK:
        if _KEYRING is None:
            _KEYRING = importlib.import_module("keyring")
        return _KEYRING


def _get_backend_type() -> str:
    """Check which keyring backend is available."""
    try:
        backend = _keyring().get_keyring()
        return type(backend).__name__
    except Exception:
        return "unknown"


def set_secret(key: str | None, value: str | None) -> None:
    """Store a secret string. Tries keyring first, falls back to DPAPI file."""
    if not key:
        raise ValueError("Secret key is required")
    if value is None:
        value = ""
    try:
        _keyring().set_password(KEYRING_SERVICE, key, value)
        logger.debug(f"Stored secret via keyring: {key}")
    except Exception as e:
        logger.warning(f"Keyring failed for {key}: {e}, falling back to DPAPI file")
        _dpapi_set(key, value)


def get_secret(key: str | None) -> str | None:
    """Retrieve a secret string."""
    if not key:
        return None
    try:
        value = _keyring().get_password(KEYRING_SERVICE, key)
        if value is not None:
            return value
    except Exception as e:
        logger.warning(f"Keyring get failed for {key}: {e}")

    # Fallback: try DPAPI file
    return _dpapi_get(key)


def delete_secret(key: str | None) -> None:
    """Delete a secret."""
    if not key:
        return
    try:
        _keyring().delete_password(KEYRING_SERVICE, key)
    except Exception as e:
        if e.__class__.__name__ == "PasswordDeleteError":
            pass
        else:
            logger.warning(f"Keyring delete failed for {key}: {e}")

    try:
        # Also clean up DPAPI fallback file.
        _dpapi_delete(key)
    except Exception as e:
        logger.warning(f"DPAPI delete failed for {key}: {e}")


def set_secret_json(key: str | None, data: dict) -> None:
    """Store a dict as compressed+base64 encoded secret (for large tokens)."""
    if not key:
        raise ValueError("Secret key is required")
    json_str = json.dumps(data, ensure_ascii=False)
    compressed = zlib.compress(json_str.encode("utf-8"))
    encoded = base64.b64encode(compressed).decode("ascii")
    set_secret(key, encoded)


def get_secret_json(key: str | None) -> dict | None:
    """Retrieve a dict stored as compressed+base64 secret."""
    encoded = get_secret(key)
    if encoded is None:
        return None
    try:
        compressed = base64.b64decode(encoded)
        json_str = zlib.decompress(compressed).decode("utf-8")
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"Failed to decode secret json for {key}: {e}")
        return None


# --- DPAPI fallback ---

def _ensure_secrets_dir() -> Path:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    return SECRETS_DIR


def _dpapi_key_path(key: str) -> Path:
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("._")[:80] or "secret"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    safe_name = f"{safe_prefix}_{digest}"
    return _ensure_secrets_dir() / f"{safe_name}.bin"


def _legacy_dpapi_key_path(key: str) -> Path:
    safe_name = key.replace(":", "_").replace("/", "_")
    return _ensure_secrets_dir() / f"{safe_name}.bin"


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _dpapi_set(key: str, value: str) -> None:
    """Encrypt and store using Windows DPAPI."""
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        # Encrypt
        data_in = value.encode("utf-8")
        blob_in = DATA_BLOB(len(data_in), ctypes.create_string_buffer(data_in, len(data_in)))
        blob_out = DATA_BLOB()

        if ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            path = _dpapi_key_path(key)
            path.write_bytes(encrypted)
            logger.debug(f"Stored secret via DPAPI: {key}")
        else:
            logger.error(f"DPAPI encryption failed for {key}")
    except Exception as e:
        logger.error(f"DPAPI set failed for {key}: {e}")


def _dpapi_get(key: str) -> str | None:
    """Decrypt from Windows DPAPI file."""
    path = _dpapi_key_path(key)
    if not _path_exists(path):
        legacy_path = _legacy_dpapi_key_path(key)
        if _path_exists(legacy_path):
            path = legacy_path
    if not _path_exists(path):
        return None
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        encrypted = path.read_bytes()
        blob_in = DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
        blob_out = DATA_BLOB()

        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return decrypted.decode("utf-8")
        else:
            logger.error(f"DPAPI decryption failed for {key}")
            return None
    except Exception as e:
        logger.error(f"DPAPI get failed for {key}: {e}")
        return None


def _dpapi_delete(key: str) -> None:
    for path in {_dpapi_key_path(key), _legacy_dpapi_key_path(key)}:
        if _path_exists(path):
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete DPAPI fallback file {path}: {e}")
