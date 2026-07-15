"""Helpers for validating and building SSH profiles from UI/form data."""
from __future__ import annotations

from dataclasses import dataclass, field

from models.profile import SSHProfile


@dataclass
class SSHProfileSavePlan:
    """Validated SSH metadata plus secret writes deferred until commit."""

    profile: SSHProfile
    secret_updates: dict[str, str] = field(default_factory=dict)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _secret_ref(name: str, suffix: str) -> str:
    return f"ssh:{name}:{suffix}"


def _copy_or_preserve_secret(
    existing_ref: str | None,
    name: str,
    suffix: str,
) -> tuple[str | None, dict[str, str]]:
    if not existing_ref:
        return None, {}

    new_ref = _secret_ref(name, suffix)
    if existing_ref == new_ref:
        return existing_ref, {}

    from core import security

    value = security.get_secret(existing_ref)
    if value:
        return new_ref, {new_ref: value}

    # Keep the old ref if it cannot be read, so editing metadata does not
    # silently drop a possibly recoverable credential.
    return existing_ref, {}


def _parse_port(value: object) -> int:
    text = _clean(value)
    try:
        port = int(text)
    except (TypeError, ValueError) as e:
        raise ValueError("SSH 端口必须是 1-65535 之间的数字") from e
    if port <= 0 or port > 65535:
        raise ValueError("SSH 端口必须是 1-65535 之间的数字")
    return port


def _remote_dir(value: object) -> str | None:
    text = _clean(value).replace("\\", "/").rstrip("/")
    if not text:
        return None
    if "\x00" in text:
        raise ValueError("远程配置目录不能包含非法字符")
    valid = (
        text.startswith("/")
        or text == "~"
        or text.startswith("~/")
        or text == "$HOME"
        or text.startswith("$HOME/")
        or text == "${HOME}"
        or text.startswith("${HOME}/")
    )
    if not valid:
        raise ValueError("远程配置目录需要使用 ~、$HOME 或绝对路径")
    return text


def prepare_ssh_profile_from_data(
    data: dict,
    existing: SSHProfile | None = None,
) -> SSHProfileSavePlan:
    """Validate form data without mutating the secret store."""
    name = _clean(data.get("name"))
    host = _clean(data.get("host"))
    username = _clean(data.get("username"))
    auth_type = _clean(data.get("auth_type")) or "key"
    private_key_path = _clean(data.get("private_key_path"))
    password = str(data.get("password") or "")
    key_passphrase = str(data.get("key_passphrase") or "")
    remote_claude_dir = _remote_dir(data.get("remote_claude_dir"))
    remote_codex_dir = _remote_dir(data.get("remote_codex_dir"))

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
    secret_updates: dict[str, str] = {}

    if auth_type == "password":
        if password:
            password_ref = _secret_ref(name, "password")
            secret_updates[password_ref] = password
        else:
            password_ref, copied_updates = _copy_or_preserve_secret(
                existing.password_ref if existing else None,
                name,
                "password",
            )
            secret_updates.update(copied_updates)
        if not password_ref:
            raise ValueError("密码认证需要填写登录密码")
        private_key_path = ""

    if auth_type == "key":
        if not private_key_path:
            raise ValueError("密钥认证需要填写私钥路径")
        if key_passphrase:
            key_passphrase_ref = _secret_ref(name, "key_passphrase")
            secret_updates[key_passphrase_ref] = key_passphrase
        else:
            key_passphrase_ref, copied_updates = _copy_or_preserve_secret(
                existing.private_key_passphrase_ref if existing else None,
                name,
                "key_passphrase",
            )
            secret_updates.update(copied_updates)

    return SSHProfileSavePlan(
        profile=SSHProfile(
            name=name,
            host=host,
            port=port,
            username=username,
            auth_type=auth_type,
            password_ref=password_ref,
            private_key_path=private_key_path or None,
            private_key_passphrase_ref=key_passphrase_ref,
            remote_claude_dir=remote_claude_dir,
            remote_codex_dir=remote_codex_dir,
        ),
        secret_updates=secret_updates,
    )


def build_ssh_profile_from_data(data: dict, existing: SSHProfile | None = None) -> SSHProfile:
    """Compatibility helper that builds a Profile and materializes secrets.

    New save and connection-test paths should use
    :func:`prepare_ssh_profile_from_data` so they can pass the returned secret
    updates to a transaction or an ephemeral connection override.
    """
    plan = prepare_ssh_profile_from_data(data, existing)
    if plan.secret_updates:
        from core import security

        for ref, value in plan.secret_updates.items():
            security.set_secret(ref, value)
    return plan.profile
