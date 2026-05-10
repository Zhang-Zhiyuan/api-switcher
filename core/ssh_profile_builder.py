"""Helpers for validating and building SSH profiles from UI/form data."""
from __future__ import annotations

from models.profile import SSHProfile


def _clean(value: object) -> str:
    return str(value or "").strip()


def _secret_ref(name: str, suffix: str) -> str:
    return f"ssh:{name}:{suffix}"


def _set_secret_ref(name: str, suffix: str, value: str) -> str:
    from core import security

    ref = _secret_ref(name, suffix)
    security.set_secret(ref, value)
    return ref


def _copy_or_preserve_secret(existing_ref: str | None, name: str, suffix: str) -> str | None:
    if not existing_ref:
        return None

    new_ref = _secret_ref(name, suffix)
    if existing_ref == new_ref:
        return existing_ref

    from core import security

    value = security.get_secret(existing_ref)
    if value:
        security.set_secret(new_ref, value)
        return new_ref

    # Keep the old ref if it cannot be read, so editing metadata does not
    # silently drop a possibly recoverable credential.
    return existing_ref


def _parse_port(value: object) -> int:
    text = _clean(value)
    try:
        port = int(text)
    except (TypeError, ValueError) as e:
        raise ValueError("SSH 端口必须是 1-65535 之间的数字") from e
    if port <= 0 or port > 65535:
        raise ValueError("SSH 端口必须是 1-65535 之间的数字")
    return port


def build_ssh_profile_from_data(data: dict, existing: SSHProfile | None = None) -> SSHProfile:
    """Validate editor data and return an SSHProfile while preserving secrets."""
    name = _clean(data.get("name"))
    host = _clean(data.get("host"))
    username = _clean(data.get("username"))
    auth_type = _clean(data.get("auth_type")) or "key"
    private_key_path = _clean(data.get("private_key_path"))
    password = str(data.get("password") or "")
    key_passphrase = str(data.get("key_passphrase") or "")

    if not name:
        raise ValueError("SSH 服务器名称不能为空")
    if not host:
        raise ValueError("SSH 主机地址不能为空")
    if not username:
        raise ValueError("SSH 用户名不能为空")
    if auth_type not in {"key", "password"}:
        raise ValueError("SSH 认证方式必须是 key 或 password")

    port = _parse_port(data.get("port"))
    password_ref = None
    key_passphrase_ref = None

    if auth_type == "password":
        if password:
            password_ref = _set_secret_ref(name, "password", password)
        else:
            password_ref = _copy_or_preserve_secret(
                existing.password_ref if existing else None,
                name,
                "password",
            )
        if not password_ref:
            raise ValueError("密码认证需要填写登录密码")
        private_key_path = ""

    if auth_type == "key":
        if not private_key_path:
            raise ValueError("密钥认证需要填写私钥路径")
        if key_passphrase:
            key_passphrase_ref = _set_secret_ref(name, "key_passphrase", key_passphrase)
        else:
            key_passphrase_ref = _copy_or_preserve_secret(
                existing.private_key_passphrase_ref if existing else None,
                name,
                "key_passphrase",
            )

    return SSHProfile(
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password_ref=password_ref,
        private_key_path=private_key_path or None,
        private_key_passphrase_ref=key_passphrase_ref,
    )
