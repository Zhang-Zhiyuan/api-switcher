import json
import logging
import paramiko

logger = logging.getLogger(__name__)

REMOTE_PATHS = {
    "claude_settings": "~/.claude/settings.json",
    "claude_config": "~/.claude/config.json",
    "codex_config": "~/.codex/config.toml",
    "codex_auth": "~/.codex/auth.json",
}


def _expand_remote_path(client: paramiko.SSHClient, path: str) -> str:
    """Expand ~ to actual home directory."""
    if path.startswith("~/"):
        stdin, stdout, stderr = client.exec_command("echo $HOME")
        home = stdout.read().decode("utf-8").strip()
        return home + path[1:]
    return path


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


def write_remote_json(client: paramiko.SSHClient, remote_path: str, data: dict):
    """Write a JSON file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    ssh_manager.write_remote_file(client, expanded, content)


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


def write_remote_toml(client: paramiko.SSHClient, remote_path: str, data: dict):
    """Write a TOML file to remote server."""
    from core.ssh_manager import ssh_manager
    expanded = _expand_remote_path(client, remote_path)
    import tomli_w
    content = tomli_w.dumps(data)
    ssh_manager.write_remote_file(client, expanded, content)


def read_remote_claude_settings(client: paramiko.SSHClient) -> dict | None:
    return read_remote_json(client, REMOTE_PATHS["claude_settings"])


def write_remote_claude_settings(client: paramiko.SSHClient, data: dict):
    write_remote_json(client, REMOTE_PATHS["claude_settings"], data)


def read_remote_codex_config(client: paramiko.SSHClient) -> dict | None:
    return read_remote_toml(client, REMOTE_PATHS["codex_config"])


def write_remote_codex_config(client: paramiko.SSHClient, data: dict):
    write_remote_toml(client, REMOTE_PATHS["codex_config"], data)


def read_remote_codex_auth(client: paramiko.SSHClient) -> dict | None:
    return read_remote_json(client, REMOTE_PATHS["codex_auth"])


def write_remote_codex_auth(client: paramiko.SSHClient, data: dict):
    write_remote_json(client, REMOTE_PATHS["codex_auth"], data)
