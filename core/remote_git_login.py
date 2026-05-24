"""Synchronize local Git/GitHub CLI login context to an SSH server."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import shlex
import subprocess

from core import profile_manager
from core.ssh_manager import ssh_manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitLoginStatus:
    local_git_available: bool = False
    local_user_name: str = ""
    local_user_email: str = ""
    local_gh_available: bool = False
    local_gh_logged_in: bool = False
    local_gh_user: str = ""
    remote_git_available: bool = False
    remote_user_name: str = ""
    remote_user_email: str = ""
    remote_gh_available: bool = False
    remote_gh_logged_in: bool = False
    remote_gh_summary: str = ""

    def summary(self) -> str:
        local_identity = (
            f"{self.local_user_name or '-'} <{self.local_user_email or '-'}>"
            if self.local_git_available
            else "Git 不可用"
        )
        remote_identity = (
            f"{self.remote_user_name or '-'} <{self.remote_user_email or '-'}>"
            if self.remote_git_available
            else "Git 不可用"
        )
        local_gh = (
            f"gh {'已登录 ' + self.local_gh_user if self.local_gh_logged_in else '未登录'}"
            if self.local_gh_available
            else "gh 不可用"
        )
        remote_gh = (
            "gh 已登录" if self.remote_gh_logged_in else "gh 未登录"
            if self.remote_gh_available
            else "gh 不可用"
        )
        return f"本机: {local_identity}，{local_gh} | 远端: {remote_identity}，{remote_gh}"


def _run_local(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _local_output(args: list[str], timeout: int = 8) -> str:
    try:
        result = _run_local(args, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception as e:
        logger.debug("Local command failed: %s: %s", args, e)
        return ""


def _local_gh_token() -> str:
    token = _local_output(["gh", "auth", "token"], timeout=10)
    return token.strip()


def _collect_local_status(include_token: bool = False) -> tuple[GitLoginStatus, str]:
    git_version = _local_output(["git", "--version"])
    user_name = _local_output(["git", "config", "--global", "--get", "user.name"]) or _local_output(["git", "config", "--get", "user.name"])
    user_email = _local_output(["git", "config", "--global", "--get", "user.email"]) or _local_output(["git", "config", "--get", "user.email"])

    gh_version = _local_output(["gh", "--version"])
    gh_user = ""
    gh_logged_in = False
    token = ""
    if gh_version:
        gh_user = _local_output(["gh", "api", "user", "--jq", ".login"], timeout=12)
        token = _local_gh_token() if include_token else ""
        gh_logged_in = bool(gh_user or token or _local_output(["gh", "auth", "status", "-h", "github.com"], timeout=12))

    return (
        GitLoginStatus(
            local_git_available=bool(git_version),
            local_user_name=user_name,
            local_user_email=user_email,
            local_gh_available=bool(gh_version),
            local_gh_logged_in=gh_logged_in,
            local_gh_user=gh_user,
        ),
        token,
    )


def _find_ssh_profile(ssh_name: str):
    profile = next((p for p in profile_manager.list_ssh_profiles() if p.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile


def _remote_probe(client) -> dict[str, str]:
    command = r"""
set +e
echo "__git_available=$(command -v git >/dev/null 2>&1 && echo 1 || echo 0)"
echo "__user_name=$(git config --global --get user.name 2>/dev/null)"
echo "__user_email=$(git config --global --get user.email 2>/dev/null)"
echo "__gh_available=$(command -v gh >/dev/null 2>&1 && echo 1 || echo 0)"
if command -v gh >/dev/null 2>&1; then
  gh auth status -h github.com >/tmp/api_switcher_gh_status.$$ 2>&1
  rc=$?
  echo "__gh_logged_in=$([ "$rc" -eq 0 ] && echo 1 || echo 0)"
  echo "__gh_summary=$(head -n 1 /tmp/api_switcher_gh_status.$$ | tr '\n' ' ')"
  rm -f /tmp/api_switcher_gh_status.$$
else
  echo "__gh_logged_in=0"
  echo "__gh_summary=gh not installed"
fi
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=20)
    if status != 0 and not stdout:
        raise RuntimeError(stderr.strip() or f"远端 Git 状态检查失败: exit {status}")
    data = {}
    for line in stdout.splitlines():
        if line.startswith("__") and "=" in line:
            key, value = line.split("=", 1)
            data[key[2:]] = value.strip()
    return data


def inspect_git_login(ssh_name: str) -> GitLoginStatus:
    """Inspect local and remote Git identity plus GitHub CLI login state."""
    local_status, _token = _collect_local_status(include_token=False)
    profile = _find_ssh_profile(ssh_name)
    client = ssh_manager.connect(profile)
    remote = _remote_probe(client)
    return GitLoginStatus(
        local_git_available=local_status.local_git_available,
        local_user_name=local_status.local_user_name,
        local_user_email=local_status.local_user_email,
        local_gh_available=local_status.local_gh_available,
        local_gh_logged_in=local_status.local_gh_logged_in,
        local_gh_user=local_status.local_gh_user,
        remote_git_available=remote.get("git_available") == "1",
        remote_user_name=remote.get("user_name", ""),
        remote_user_email=remote.get("user_email", ""),
        remote_gh_available=remote.get("gh_available") == "1",
        remote_gh_logged_in=remote.get("gh_logged_in") == "1",
        remote_gh_summary=remote.get("gh_summary", ""),
    )


def sync_git_login_to_server(ssh_name: str) -> str:
    """Configure remote Git identity and, when possible, GitHub CLI auth."""
    local_status, token = _collect_local_status(include_token=True)
    if not local_status.local_git_available:
        raise RuntimeError("本机未找到 git 命令")
    if not local_status.local_user_name and not local_status.local_user_email and not token:
        raise RuntimeError("本机没有可同步的 Git 身份或 GitHub CLI 登录")

    profile = _find_ssh_profile(ssh_name)
    client = ssh_manager.connect(profile)
    remote = _remote_probe(client)
    if remote.get("git_available") != "1":
        raise RuntimeError("远端未安装 git，请先在服务器上安装 git")

    commands = []
    if local_status.local_user_name:
        commands.append(f"git config --global user.name {shlex.quote(local_status.local_user_name)}")
    if local_status.local_user_email:
        commands.append(f"git config --global user.email {shlex.quote(local_status.local_user_email)}")
    if commands:
        status, _stdout, stderr = ssh_manager.execute_command_with_status(
            client,
            " && ".join(commands),
            timeout=20,
            log_command=False,
        )
        if status != 0:
            raise RuntimeError(stderr.strip() or "远端 Git 身份配置失败")

    parts = []
    if commands:
        parts.append(f"已同步 Git 身份 {local_status.local_user_name or '-'} <{local_status.local_user_email or '-'}>")
    else:
        parts.append("本机未配置 Git 用户名/邮箱，已跳过身份同步")

    if token:
        if remote.get("gh_available") != "1":
            parts.append("远端未安装 GitHub CLI，已跳过 gh 登录")
        else:
            command = "gh auth login --hostname github.com --git-protocol https --with-token && gh auth setup-git --hostname github.com"
            status, stdout, stderr = ssh_manager.execute_command_with_status(
                client,
                command,
                timeout=60,
                input_data=token + "\n",
                log_command=False,
            )
            if status != 0:
                raise RuntimeError((stderr or stdout or "远端 GitHub CLI 登录失败").strip())
            parts.append("已同步 GitHub CLI 登录")
    else:
        parts.append("本机未检测到 GitHub CLI 登录 token，已跳过 gh 登录")

    return "；".join(parts)
