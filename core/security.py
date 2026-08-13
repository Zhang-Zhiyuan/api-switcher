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
from core.atomic_io import atomic_write_bytes

logger = logging.getLogger(__name__)
_KEYRING = None
_KEYRING_LOCK = threading.RLock()
_SECRET_OPERATION_LOCK = threading.RLock()
MAX_SECRET_JSON_BYTES = 16 * 1024 * 1024
MAX_SECRET_JSON_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_DPAPI_SECRET_BYTES = 32 * 1024 * 1024


class SecretReadError(RuntimeError):
    """Raised when secret absence cannot be distinguished from backend failure."""


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
    with _SECRET_OPERATION_LOCK:
        try:
            _keyring().set_password(KEYRING_SERVICE, key, value)
        except Exception as e:
            logger.warning(f"Keyring failed for {key}: {e}, falling back to DPAPI file")
        else:
            # A previous keyring outage may have left a newer DPAPI fallback.
            # Remove it before reporting success so reads cannot split across
            # two backends with different values.
            try:
                _dpapi_delete(key)
            except Exception as cleanup_error:
                raise RuntimeError("系统密钥环已更新，但旧 DPAPI 密钥清理失败") from cleanup_error
            logger.debug(f"Stored secret via keyring: {key}")
            return
        try:
            _dpapi_set(key, value)
        except Exception as fallback_error:
            raise RuntimeError("无法保存密钥：系统密钥环和 DPAPI 均不可用") from fallback_error


def get_secret(key: str | None) -> str | None:
    """Retrieve a secret string."""
    if not key:
        return None
    with _SECRET_OPERATION_LOCK:
        # New fallback writes use a distinct active filename. It must win over
        # a stale keyring value left behind by a temporary keyring outage.
        active_value = _dpapi_get_active(key)
        if active_value is not None:
            return active_value
        # The active file is a durable marker that a fallback write is newer
        # than keyring.  If it exists but cannot be decrypted, never return a
        # stale keyring value.
        if _path_exists(_active_dpapi_key_path(key)):
            return None
        try:
            value = _keyring().get_password(KEYRING_SERVICE, key)
            if value is not None:
                return value
        except Exception as e:
            logger.warning(f"Keyring get failed for {key}: {e}")

        # Fallback: try DPAPI file
        return _dpapi_get(key)


def get_secret_strict(key: str | None) -> str | None:
    """Read a secret while distinguishing absence from backend failure.

    Transactions must use this API for snapshots; treating an unreadable
    existing value as absent could make rollback delete or lose that value.
    """
    if not key:
        return None
    with _SECRET_OPERATION_LOCK:
        active_path = _active_dpapi_key_path(key)
        if _path_exists(active_path):
            try:
                return _dpapi_get_from_paths(key, [active_path], strict=True)
            except Exception as exc:
                raise SecretReadError(f"DPAPI 活动密钥无法读取: {key}") from exc

        keyring_error: Exception | None = None
        try:
            value = _keyring().get_password(KEYRING_SERVICE, key)
            if value is not None:
                return value
        except Exception as exc:
            keyring_error = exc

        fallback_paths = [_dpapi_key_path(key), _legacy_dpapi_key_path(key)]
        if any(_path_exists(path) for path in fallback_paths):
            try:
                return _dpapi_get_from_paths(key, fallback_paths, strict=True)
            except Exception as exc:
                raise SecretReadError(f"DPAPI 密钥无法读取: {key}") from exc
        if keyring_error is not None:
            raise SecretReadError(f"系统密钥环无法读取: {key}") from keyring_error
        return None


def delete_secret(key: str | None) -> None:
    """Delete a secret."""
    if not key:
        return
    errors: list[str] = []
    with _SECRET_OPERATION_LOCK:
        try:
            _keyring().delete_password(KEYRING_SERVICE, key)
        except Exception as e:
            if e.__class__.__name__ != "PasswordDeleteError":
                logger.warning(f"Keyring delete failed for {key}: {e}")
                errors.append(f"系统密钥环: {e}")

        try:
            # Also clean up DPAPI fallback file.
            _dpapi_delete(key)
        except Exception as e:
            logger.warning(f"DPAPI delete failed for {key}: {e}")
            errors.append(f"DPAPI: {e}")
    if errors:
        raise RuntimeError("密钥删除不完整：" + "；".join(errors))


def set_secret_json(key: str | None, data: dict) -> None:
    """Store a dict as compressed+base64 encoded secret (for large tokens)."""
    if not key:
        raise ValueError("Secret key is required")
    json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(json_bytes) > MAX_SECRET_JSON_BYTES:
        raise ValueError("密钥 JSON 内容过大")
    compressed = zlib.compress(json_bytes)
    if len(compressed) > MAX_SECRET_JSON_COMPRESSED_BYTES:
        raise ValueError("密钥 JSON 压缩内容过大")
    encoded = base64.b64encode(compressed).decode("ascii")
    set_secret(key, encoded)


def get_secret_json(key: str | None) -> dict | None:
    """Retrieve a dict stored as compressed+base64 secret."""
    encoded = get_secret(key)
    if encoded is None:
        return None
    try:
        if len(encoded) > ((MAX_SECRET_JSON_COMPRESSED_BYTES + 2) // 3) * 4 + 4:
            raise ValueError("密钥 JSON 编码内容过大")
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > MAX_SECRET_JSON_COMPRESSED_BYTES:
            raise ValueError("密钥 JSON 压缩内容过大")
        decompressor = zlib.decompressobj()
        json_bytes = decompressor.decompress(compressed, MAX_SECRET_JSON_BYTES + 1)
        if len(json_bytes) <= MAX_SECRET_JSON_BYTES:
            json_bytes += decompressor.flush(MAX_SECRET_JSON_BYTES + 1 - len(json_bytes))
        if (
            len(json_bytes) > MAX_SECRET_JSON_BYTES
            or not decompressor.eof
            or decompressor.unused_data
        ):
            raise ValueError("密钥 JSON 压缩数据无效或解压后过大")
        data = json.loads(json_bytes.decode("utf-8"))
        return data if isinstance(data, dict) else None
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
    return _safe_secret_file_path(f"{safe_name}.bin")


def _active_dpapi_key_path(key: str) -> Path:
    old_path = _dpapi_key_path(key)
    return _safe_secret_file_path(f"{old_path.stem}.fallback.bin")


def _legacy_dpapi_key_path(key: str) -> Path:
    # Older versions used this readable filename. Keep the lookup for
    # compatibility, but sanitize both separator styles before constructing a
    # path; on Windows an unfiltered backslash could escape SECRETS_DIR.
    safe_name = re.sub(r"[:/\\]", "_", key)
    return _safe_secret_file_path(f"{safe_name}.bin")


def _safe_secret_file_path(filename: str) -> Path:
    root = _ensure_secrets_dir().resolve()
    candidate = (root / filename).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("DPAPI 密钥文件路径越界") from exc
    if candidate == root:
        raise ValueError("DPAPI 密钥文件路径无效")
    return candidate


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
            try:
                encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            path = _active_dpapi_key_path(key)
            atomic_write_bytes(path, encrypted)
            logger.debug(f"Stored secret via DPAPI: {key}")
        else:
            raise RuntimeError("Windows DPAPI 加密失败")
    except Exception as e:
        logger.error(f"DPAPI set failed for {key}: {e}")
        raise


def _dpapi_get_active(key: str) -> str | None:
    """Read only the active fallback written after a keyring failure."""
    try:
        return _dpapi_get_from_paths(key, [_active_dpapi_key_path(key)])
    except Exception as exc:
        logger.error(f"DPAPI active fallback lookup failed for {key}: {exc}")
        return None


def _dpapi_get(key: str) -> str | None:
    """Decrypt from current or legacy Windows DPAPI files."""
    try:
        paths = [
            _active_dpapi_key_path(key),
            _dpapi_key_path(key),
            _legacy_dpapi_key_path(key),
        ]
        return _dpapi_get_from_paths(key, paths)
    except Exception as exc:
        logger.error(f"DPAPI get failed for {key}: {exc}")
        return None


def _dpapi_get_from_paths(
    key: str,
    paths: list[Path],
    *,
    strict: bool = False,
) -> str | None:
    path = next((candidate for candidate in paths if _path_exists(candidate)), None)
    if path is None:
        return None
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        encrypted_size = path.stat().st_size
        if encrypted_size > MAX_DPAPI_SECRET_BYTES:
            raise ValueError(f"DPAPI 密钥文件过大: {encrypted_size} bytes")
        with path.open("rb") as handle:
            encrypted = handle.read(MAX_DPAPI_SECRET_BYTES + 1)
        if len(encrypted) > MAX_DPAPI_SECRET_BYTES:
            raise ValueError("DPAPI 密钥文件过大")
        blob_in = DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
        blob_out = DATA_BLOB()

        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            try:
                decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return decrypted.decode("utf-8")
        else:
            raise RuntimeError(f"DPAPI decryption failed for {key}")
    except Exception as e:
        logger.error(f"DPAPI get failed for {key}: {e}")
        if strict:
            raise
        return None


def _dpapi_delete(key: str) -> None:
    errors: list[str] = []
    for path in {
        _active_dpapi_key_path(key),
        _dpapi_key_path(key),
        _legacy_dpapi_key_path(key),
    }:
        if _path_exists(path):
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete DPAPI fallback file {path}: {e}")
                errors.append(f"{path.name}: {e}")
    if errors:
        raise OSError("；".join(errors))
