import json
import logging
import posixpath
import threading
import weakref
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
    "codex_env": ("codex", ".env"),
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


_DEFAULT_CODEX_DIR_ALIASES = {
    "~/.codex",
    "$HOME/.codex",
    "${HOME}/.codex",
}
_DEFAULT_CLAUDE_DIR_ALIASES = {
    "~/.claude",
    "$HOME/.claude",
    "${HOME}/.claude",
}
_CLAUDE_CONFIG_DIR_MARKER = "__API_SWITCHER_CLAUDE_CONFIG_DIR__"
_CLAUDE_CONFIG_DIR_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CLAUDE_CONFIG_DIR_CACHE_LOCK = threading.RLock()
_CODEX_HOME_MARKER = "__API_SWITCHER_CODEX_HOME__"
_CODEX_HOME_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CODEX_HOME_CACHE_LOCK = threading.RLock()


def _remote_codex_dir(client: paramiko.SSHClient, profile: object | None = None) -> str:
    """Resolve Codex state using an explicit override or remote CODEX_HOME.

    Older UI versions persisted ``~/.codex`` even when the user had not
    selected a custom directory.  Treat those equivalent default spellings as
    automatic so a remote ``CODEX_HOME`` is still honoured.  A genuinely
    custom SSH-profile directory always wins.
    """
    configured = str(getattr(profile, "remote_codex_dir", "") or "").strip()
    configured = configured.replace("\\", "/").rstrip("/")
    if configured and configured not in _DEFAULT_CODEX_DIR_ALIASES:
        return configured

    try:
        with _CODEX_HOME_CACHE_LOCK:
            cached = _CODEX_HOME_CACHE.get(client)
        if isinstance(cached, str) and cached:
            return cached
    except TypeError:
        # A few small test/client adapters are intentionally not weakref-able.
        pass

    commands = (
        f"printf '{_CODEX_HOME_MARKER}%s' \"${{CODEX_HOME:-}}\"",
        f"sh -lc 'printf \"{_CODEX_HOME_MARKER}%s\" \"${{CODEX_HOME:-}}\"'",
    )
    for command in commands:
        output = _command_output(client, command, timeout=5)
        marker_index = output.rfind(_CODEX_HOME_MARKER)
        if marker_index < 0:
            continue
        # ``printf`` legitimately returns only the marker when CODEX_HOME is
        # unset.  ``str.splitlines()`` produces an empty list for that suffix,
        # so use ``partition`` to keep the normal ~/.codex fallback path safe.
        value = output[marker_index + len(_CODEX_HOME_MARKER):].partition("\n")[0].strip()
        if value:
            resolved = value.replace("\\", "/").rstrip("/")
            try:
                with _CODEX_HOME_CACHE_LOCK:
                    _CODEX_HOME_CACHE[client] = resolved
            except TypeError:
                pass
            return resolved

    resolved = DEFAULT_REMOTE_DIRS["codex"]
    try:
        with _CODEX_HOME_CACHE_LOCK:
            _CODEX_HOME_CACHE[client] = resolved
    except TypeError:
        pass
    return resolved


def _remote_claude_dir(client: paramiko.SSHClient, profile: object | None = None) -> str:
    """Resolve Claude Code state using an override or CLAUDE_CONFIG_DIR."""
    configured = str(getattr(profile, "remote_claude_dir", "") or "").strip()
    configured = configured.replace("\\", "/").rstrip("/")
    if configured and configured not in _DEFAULT_CLAUDE_DIR_ALIASES:
        return configured

    try:
        with _CLAUDE_CONFIG_DIR_CACHE_LOCK:
            cached = _CLAUDE_CONFIG_DIR_CACHE.get(client)
        if isinstance(cached, str) and cached:
            return cached
    except TypeError:
        pass

    commands = (
        f"printf '{_CLAUDE_CONFIG_DIR_MARKER}%s' \"${{CLAUDE_CONFIG_DIR:-}}\"",
        f"sh -lc 'printf \"{_CLAUDE_CONFIG_DIR_MARKER}%s\" \"${{CLAUDE_CONFIG_DIR:-}}\"'",
    )
    for command in commands:
        output = _command_output(client, command, timeout=5)
        marker_index = output.rfind(_CLAUDE_CONFIG_DIR_MARKER)
        if marker_index < 0:
            continue
        value = output[marker_index + len(_CLAUDE_CONFIG_DIR_MARKER):].partition("\n")[0].strip()
        if value:
            resolved = value.replace("\\", "/").rstrip("/")
            try:
                with _CLAUDE_CONFIG_DIR_CACHE_LOCK:
                    _CLAUDE_CONFIG_DIR_CACHE[client] = resolved
            except TypeError:
                pass
            return resolved

    resolved = DEFAULT_REMOTE_DIRS["claude"]
    try:
        with _CLAUDE_CONFIG_DIR_CACHE_LOCK:
            _CLAUDE_CONFIG_DIR_CACHE[client] = resolved
    except TypeError:
        pass
    return resolved


def _remote_dir(profile: object | None, kind: str, client: paramiko.SSHClient | None = None) -> str:
    if kind == "claude" and client is not None:
        return _remote_claude_dir(client, profile)
    if kind == "codex" and client is not None:
        return _remote_codex_dir(client, profile)
    attr = f"remote_{kind}_dir"
    value = str(getattr(profile, attr, "") or DEFAULT_REMOTE_DIRS[kind]).strip().replace("\\", "/").rstrip("/")
    return value or DEFAULT_REMOTE_DIRS[kind]


def _remote_path(
    key: str,
    profile: object | None = None,
    client: paramiko.SSHClient | None = None,
) -> str:
    kind, filename = REMOTE_FILENAMES[key]
    return posixpath.join(_remote_dir(profile, kind, client), filename)


def read_remote_json(
    client: paramiko.SSHClient,
    remote_path: str,
    *,
    strict: bool = False,
) -> dict | None:
    """Read a JSON file from remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = ssh_manager.read_remote_file(client, expanded)
    if content is None:
        return None
    try:
        parsed = json.loads(content.lstrip("\ufeff"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse remote JSON {expanded}: {e}")
        if strict:
            raise RuntimeError(f"远程 JSON 格式损坏: {expanded}（{e.msg}）") from e
        return None
    if not isinstance(parsed, dict):
        logger.warning(f"Remote JSON {expanded} is not an object, ignoring")
        if strict:
            raise RuntimeError(f"远程 JSON 顶层必须是对象: {expanded}")
        return None
    return parsed


def write_remote_json(client: paramiko.SSHClient, remote_path: str, data: dict, file_mode: int | None = None):
    """Write a JSON file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    ssh_manager.write_remote_file(client, expanded, content, file_mode=file_mode)


def read_remote_text(client: paramiko.SSHClient, remote_path: str) -> str | None:
    """Read a text file from remote server."""
    from core.ssh_manager import ssh_manager

    expanded = _expand_remote_path(client, remote_path)
    return ssh_manager.read_remote_file(client, expanded)


def write_remote_text(client: paramiko.SSHClient, remote_path: str, content: str, file_mode: int | None = None):
    """Write a text file to remote server."""
    from core.ssh_manager import ssh_manager

    expanded = _expand_remote_path(client, remote_path)
    ssh_manager.write_remote_file(client, expanded, content, file_mode=file_mode)


def delete_remote_file(client: paramiko.SSHClient, remote_path: str) -> None:
    """Delete one remote file, treating an already-missing file as success."""
    from core.ssh_manager import ssh_manager

    expanded = _expand_remote_path(client, remote_path)
    sftp = None
    try:
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(30)
        try:
            sftp.remove(expanded)
        except Exception as error:
            if not ssh_manager._is_not_found_error(error):
                raise
    except Exception as error:
        raise RuntimeError(f"删除远程文件失败: {error}") from error
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _existing_remote_paths(client: paramiko.SSHClient, paths: tuple[str, ...]) -> list[str]:
    """Return expanded candidate paths that already exist on the remote host."""
    from core.ssh_manager import ssh_manager

    existing = []
    for path in paths:
        expanded = _expand_remote_path(client, path)
        if ssh_manager.read_remote_file(client, expanded) is not None:
            existing.append(expanded)
    return existing


def read_remote_toml(
    client: paramiko.SSHClient,
    remote_path: str,
    *,
    strict: bool = False,
) -> dict | None:
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
        parsed = tomllib.loads(content)
    except Exception as e:
        logger.error(f"Failed to parse remote TOML {expanded}: {e}")
        if strict:
            raise RuntimeError(f"远程 TOML 格式损坏: {expanded}（{e}）") from e
        return None
    if not isinstance(parsed, dict):
        logger.warning(f"Remote TOML {expanded} is not an object, ignoring")
        if strict:
            raise RuntimeError(f"远程 TOML 顶层必须是表: {expanded}")
        return None
    return parsed


def write_remote_toml(client: paramiko.SSHClient, remote_path: str, data: dict, file_mode: int | None = None):
    """Write a TOML file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    import tomli_w
    content = tomli_w.dumps(data)
    ssh_manager.write_remote_file(client, expanded, content, file_mode=file_mode)


def read_remote_claude_settings(
    client: paramiko.SSHClient,
    profile: object | None = None,
    *,
    strict: bool = False,
) -> dict | None:
    return read_remote_json(
        client,
        _remote_path("claude_settings", profile, client),
        strict=strict,
    )


def write_remote_claude_settings(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_settings", profile, client), data, file_mode=0o600)


def read_remote_claude_config(
    client: paramiko.SSHClient,
    profile: object | None = None,
    *,
    strict: bool = False,
) -> dict | None:
    return read_remote_json(
        client,
        _remote_path("claude_config", profile, client),
        strict=strict,
    )


def write_remote_claude_config(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_config", profile, client), data, file_mode=0o600)


def read_remote_claude_credentials(
    client: paramiko.SSHClient,
    profile: object | None = None,
    *,
    strict: bool = False,
) -> dict | None:
    return read_remote_json(
        client,
        _remote_path("claude_credentials", profile, client),
        strict=strict,
    )


def write_remote_claude_credentials(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("claude_credentials", profile, client), data, file_mode=0o600)


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


def read_remote_codex_config(
    client: paramiko.SSHClient,
    profile: object | None = None,
    *,
    strict: bool = False,
) -> dict | None:
    return read_remote_toml(
        client,
        _remote_path("codex_config", profile, client),
        strict=strict,
    )


def write_remote_codex_config(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_toml(client, _remote_path("codex_config", profile, client), data, file_mode=0o600)


def read_remote_codex_auth(
    client: paramiko.SSHClient,
    profile: object | None = None,
    *,
    strict: bool = False,
) -> dict | None:
    return read_remote_json(
        client,
        _remote_path("codex_auth", profile, client),
        strict=strict,
    )


def write_remote_codex_auth(client: paramiko.SSHClient, data: dict, profile: object | None = None):
    write_remote_json(client, _remote_path("codex_auth", profile, client), data, file_mode=0o600)


def read_remote_codex_env(client: paramiko.SSHClient, profile: object | None = None) -> str | None:
    return read_remote_text(client, _remote_path("codex_env", profile, client))


def write_remote_codex_env(client: paramiko.SSHClient, content: str, profile: object | None = None):
    write_remote_text(client, _remote_path("codex_env", profile, client), content, file_mode=0o600)
