import json
import logging
import posixpath
import paramiko

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_DIRS = {
    "claude": "~/.claude",
    "codex": "~/.codex",
}

REMOTE_FILENAMES = {
    "claude_settings": ("claude", "settings.json"),
    "claude_config": ("claude", "config.json"),
    "claude_credentials": ("claude", ".credentials.json"),
    "codex_config": ("codex", "config.toml"),
    "codex_auth": ("codex", "auth.json"),
}

REMOTE_VSCODE_SETTINGS_PATHS = (
    "~/.vscode-server/data/Machine/settings.json",
    "~/.vscode-server-insiders/data/Machine/settings.json",
    "~/.cursor-server/data/Machine/settings.json",
)


def _decode_remote_output(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _command_output(client: paramiko.SSHClient, command: str, timeout: int = 10) -> str:
    try:
        _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
        return _decode_remote_output(stdout.read()).strip()
    except Exception as e:
        logger.debug(f"Remote command failed while resolving path: {command!r}: {e}")
        return ""


def _sftp_home(client: paramiko.SSHClient) -> str:
    sftp = None
    try:
        sftp = client.open_sftp()
        return str(sftp.normalize(".")).strip()
    except Exception as e:
        logger.debug(f"SFTP home fallback failed: {e}")
        return ""
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _remote_home(client: paramiko.SSHClient) -> str:
    for command in [
        'printf "%s" "$HOME"',
        'getent passwd "$(id -un)" 2>/dev/null | awk -F: \'{print $6}\'',
        "cd ~ 2>/dev/null && pwd -P",
    ]:
        home = _command_output(client, command)
        if home.startswith("/"):
            return posixpath.normpath(home)

    home = _sftp_home(client)
    if home.startswith("/"):
        return posixpath.normpath(home)

    raise RuntimeError("无法解析远程用户 HOME 目录")


def _expand_remote_path(client: paramiko.SSHClient, path: str) -> str:
    """Expand home-relative remote paths and normalize them as POSIX paths."""
    if not path or not str(path).strip():
        raise ValueError("远程路径不能为空")

    path = str(path).strip().replace("\\", "/")
    home_prefixes = ("~/", "$HOME/", "${HOME}/")
    if path in {"~", "$HOME", "${HOME}"}:
        return _remote_home(client)
    if path.startswith(home_prefixes):
        home = _remote_home(client)
        suffix = path.split("/", 1)[1]
        return posixpath.normpath(posixpath.join(home, suffix))
    if path.startswith("/"):
        return posixpath.normpath(path)

    home = _remote_home(client)
    return posixpath.normpath(posixpath.join(home, path))


def _remote_dir(profile: object | None, kind: str) -> str:
    attr = f"remote_{kind}_dir"
    value = str(getattr(profile, attr, "") or DEFAULT_REMOTE_DIRS[kind]).strip().replace("\\", "/").rstrip("/")
    return value or DEFAULT_REMOTE_DIRS[kind]


def _remote_path(key: str, profile: object | None = None) -> str:
    kind, filename = REMOTE_FILENAMES[key]
    return posixpath.join(_remote_dir(profile, kind), filename)


def read_remote_json(client: paramiko.SSHClient, remote_path: str) -> dict | None:
    """Read a JSON file from remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = ssh_manager.read_remote_file(client, expanded)
    if content is None:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse remote JSON {expanded}: {e}")
        return None


def write_remote_json(client: paramiko.SSHClient, remote_path: str, data: dict, file_mode: int | None = None):
    """Write a JSON file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    ssh_manager.write_remote_file(client, expanded, content, file_mode=file_mode)


def _existing_remote_paths(client: paramiko.SSHClient, paths: tuple[str, ...]) -> list[str]:
    """Return expanded candidate paths that already exist on the remote host."""
    from core.ssh_manager import ssh_manager

    existing = []
    for path in paths:
        expanded = _expand_remote_path(client, path)
        if ssh_manager.read_remote_file(client, expanded) is not None:
            existing.append(expanded)
    return existing


def read_remote_toml(client: paramiko.SSHClient, remote_path: str) -> dict | None:
    """Read a TOML file from remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = ssh_manager.read_remote_file(client, expanded)
    if content is None:
        return None
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        return tomllib.loads(content)
    except Exception as e:
        logger.error(f"Failed to parse remote TOML {expanded}: {e}")
        return None


def write_remote_toml(client: paramiko.SSHClient, remote_path: str, data: dict, file_mode: int | None = None):
    """Write a TOML file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    import tomli_w
    content = tomli_w.dumps(data)
    ssh_manager.write_remote_file(client, expanded, content, file_mode=file_mode)


def read_remote_claude_settings(client: paramiko.SSHClient, profile: object | None = None) -> dict | None:
    return read_remote_json(client, _remote_path("claude_settings", profile))


def write_remote_claude_settings(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_settings", profile), data, file_mode=0o600)


def read_remote_claude_config(client: paramiko.SSHClient, profile: object | None = None) -> dict | None:
    return read_remote_json(client, _remote_path("claude_config", profile))


def write_remote_claude_config(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_config", profile), data, file_mode=0o600)


def read_remote_claude_credentials(client: paramiko.SSHClient, profile: object | None = None) -> dict | None:
    return read_remote_json(client, _remote_path("claude_credentials", profile))


def write_remote_claude_credentials(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_credentials", profile), data, file_mode=0o600)


def read_remote_vscode_settings(client: paramiko.SSHClient) -> dict | None:
    """Read the first available remote VS Code Server Machine settings file."""
    for path in REMOTE_VSCODE_SETTINGS_PATHS:
        settings = read_remote_json(client, path)
        if settings is not None:
            return settings
    return None


def write_remote_vscode_settings(client: paramiko.SSHClient, data: dict):
    """Write VS Code Server Machine settings.

    Update every known existing server settings file so Stable/Insiders/Cursor
    stay consistent. If no file exists yet, create the regular VS Code Server
    path because that is where the remote extension reads Machine settings.
    """
    targets = _existing_remote_paths(client, REMOTE_VSCODE_SETTINGS_PATHS)
    if not targets:
        targets = [_expand_remote_path(client, REMOTE_VSCODE_SETTINGS_PATHS[0])]

    for path in targets:
        write_remote_json(client, path, data, file_mode=0o600)


def read_remote_codex_config(client: paramiko.SSHClient, profile: object | None = None) -> dict | None:
    return read_remote_toml(client, _remote_path("codex_config", profile))


def write_remote_codex_config(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_toml(client, _remote_path("codex_config", profile), data, file_mode=0o600)


def read_remote_codex_auth(client: paramiko.SSHClient, profile: object | None = None) -> dict | None:
    return read_remote_json(client, _remote_path("codex_auth", profile))


def write_remote_codex_auth(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("codex_auth", profile), data, file_mode=0o600)
