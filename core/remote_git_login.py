"""Synchronize local Git/GitHub CLI login context to an SSH server."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import platform
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
    remote_os: str = "unknown"
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
        return f"本机: {local_identity}，{local_gh} | 远端({self.remote_os or 'unknown'}): {remote_identity}，{remote_gh}"


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


def _install_local_gh_windows() -> str:
    """Install GitHub CLI on the local Windows machine using common package managers."""
    if platform.system().lower() != "windows":
        raise RuntimeError("本机自动安装 GitHub CLI 目前仅支持 Windows")

    installers = [
        (["winget", "install", "--id", "GitHub.cli", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements"], "winget"),
        (["choco", "install", "gh", "-y"], "choco"),
        (["scoop", "install", "gh"], "scoop"),
    ]
    errors = []
    for command, label in installers:
        if not _local_output([command[0], "--version"], timeout=8):
            continue
        try:
            result = _run_local(command, timeout=300)
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
        if result.returncode == 0:
            version = _local_output(["gh", "--version"], timeout=8)
            if version:
                return version.splitlines()[0]
            errors.append(f"{label}: 安装完成但 gh 仍不可用，请重新打开程序或检查 PATH")
        else:
            errors.append(f"{label}: {(result.stderr or result.stdout).strip()}")

    detail = "；".join(error for error in errors if error) or "未找到 winget/choco/scoop"
    raise RuntimeError(f"本机 GitHub CLI 自动安装失败: {detail}")


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


def _collect_local_status_for_sync() -> tuple[GitLoginStatus, str, str]:
    status, token = _collect_local_status(include_token=True)
    install_summary = ""
    if not status.local_gh_available and platform.system().lower() == "windows":
        install_summary = _install_local_gh_windows()
        status, token = _collect_local_status(include_token=True)
    return status, token, install_summary


def _find_ssh_profile(ssh_name: str):
    profile = next((p for p in profile_manager.list_ssh_profiles() if p.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile


def _ps_single_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _remote_probe_posix(client) -> dict[str, str]:
    command = r"""
set +e
echo "__os=$(uname -s 2>/dev/null || echo unknown)"
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
    return _parse_probe_output(stdout)


def _remote_probe_windows(client) -> dict[str, str]:
    ps_script = r"""
$ErrorActionPreference = 'SilentlyContinue'
Write-Output '__os=windows'
$git = Get-Command git -ErrorAction SilentlyContinue
Write-Output ('__git_available=' + ($(if ($git) {'1'} else {'0'})))
if ($git) {
  Write-Output ('__user_name=' + ((git config --global --get user.name) 2>$null))
  Write-Output ('__user_email=' + ((git config --global --get user.email) 2>$null))
} else {
  Write-Output '__user_name='
  Write-Output '__user_email='
}
$gh = Get-Command gh -ErrorAction SilentlyContinue
Write-Output ('__gh_available=' + ($(if ($gh) {'1'} else {'0'})))
if ($gh) {
  $statusText = (gh auth status -h github.com 2>&1 | Select-Object -First 1) -join ' '
  Write-Output ('__gh_logged_in=' + ($(if ($LASTEXITCODE -eq 0) {'1'} else {'0'})))
  Write-Output ('__gh_summary=' + $statusText)
} else {
  Write-Output '__gh_logged_in=0'
  Write-Output '__gh_summary=gh not installed'
}
"""
    command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " + _ps_single_quote(ps_script)
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, command, timeout=25)
    if status != 0 and not stdout:
        raise RuntimeError(stderr.strip() or f"远端 Windows Git 状态检查失败: exit {status}")
    return _parse_probe_output(stdout)


def _parse_probe_output(stdout: str) -> dict[str, str]:
    data = {}
    for line in stdout.splitlines():
        if line.startswith("__") and "=" in line:
            key, value = line.split("=", 1)
            data[key[2:]] = value.strip()
    return data


def _remote_probe(client) -> dict[str, str]:
    try:
        data = _remote_probe_posix(client)
        if data.get("git_available") or data.get("gh_available") or data.get("os"):
            return data
    except Exception as e:
        logger.debug("POSIX remote Git probe failed, trying Windows probe: %s", e)
    return _remote_probe_windows(client)


def _is_windows_remote(remote: dict[str, str]) -> bool:
    return str(remote.get("os") or "").strip().lower().startswith(("windows", "mingw", "msys"))


def _install_remote_gh_posix(client) -> str:
    """Install GitHub CLI on common Linux distributions when it is missing."""
    command = r"""
set -e
if command -v gh >/dev/null 2>&1; then
  gh --version | head -n 1
  exit 0
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to install gh for non-root users" >&2
    exit 90
  fi
  SUDO="sudo"
fi

if command -v apt >/dev/null 2>&1; then
  $SUDO apt update
  $SUDO apt install -y curl ca-certificates
  $SUDO install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  $SUDO apt update
  $SUDO apt install -y gh
elif command -v dnf >/dev/null 2>&1; then
  $SUDO dnf install -y 'dnf-command(config-manager)' || true
  $SUDO dnf config-manager addrepo --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo || $SUDO dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
  $SUDO dnf install -y gh
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y yum-utils
  $SUDO yum-config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
  $SUDO yum install -y gh
elif command -v pacman >/dev/null 2>&1; then
  $SUDO pacman -Sy --noconfirm github-cli
elif command -v zypper >/dev/null 2>&1; then
  $SUDO zypper --non-interactive install gh
else
  echo "unsupported package manager: need apt, dnf, yum, pacman, or zypper" >&2
  exit 91
fi

gh --version | head -n 1
"""
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=240,
        log_command=False,
    )
    if status != 0:
        raise RuntimeError((stderr or stdout or f"远端 gh 安装失败: exit {status}").strip())
    return (stdout.strip().splitlines() or ["gh 已安装"])[-1]


def _install_remote_gh_windows(client) -> str:
    """Install GitHub CLI on Windows SSH hosts using common package managers."""
    ps_script = r"""
$ErrorActionPreference = 'Stop'
if (Get-Command gh -ErrorAction SilentlyContinue) {
  gh --version | Select-Object -First 1
  exit 0
}

if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id GitHub.cli -e --silent --accept-package-agreements --accept-source-agreements
} elseif (Get-Command choco -ErrorAction SilentlyContinue) {
  choco install gh -y
} elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
  scoop install gh
} else {
  Write-Error 'unsupported Windows package manager: need winget, choco, or scoop'
  exit 91
}

$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Write-Error 'gh installed but not available in PATH'
  exit 92
}
gh --version | Select-Object -First 1
"""
    command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " + _ps_single_quote(ps_script)
    status, stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=300,
        log_command=False,
    )
    if status != 0:
        raise RuntimeError((stderr or stdout or f"远端 Windows gh 安装失败: exit {status}").strip())
    return (stdout.strip().splitlines() or ["gh 已安装"])[-1]


def _install_remote_gh(client, remote: dict[str, str]) -> str:
    if _is_windows_remote(remote):
        return _install_remote_gh_windows(client)
    return _install_remote_gh_posix(client)


def _configure_remote_git_identity(client, remote: dict[str, str], user_name: str, user_email: str) -> None:
    if _is_windows_remote(remote):
        commands = []
        if user_name:
            commands.append(f"git config --global user.name {_ps_single_quote(user_name)}")
        if user_email:
            commands.append(f"git config --global user.email {_ps_single_quote(user_email)}")
        if not commands:
            return
        ps_script = "\n".join(commands)
        command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " + _ps_single_quote(ps_script)
    else:
        commands = []
        if user_name:
            commands.append(f"git config --global user.name {shlex.quote(user_name)}")
        if user_email:
            commands.append(f"git config --global user.email {shlex.quote(user_email)}")
        if not commands:
            return
        command = " && ".join(commands)

    status, _stdout, stderr = ssh_manager.execute_command_with_status(
        client,
        command,
        timeout=20,
        log_command=False,
    )
    if status != 0:
        raise RuntimeError(stderr.strip() or "远端 Git 身份配置失败")


def _remote_gh_login(client, remote: dict[str, str], token: str) -> None:
    if _is_windows_remote(remote):
        ps_script = (
            "gh auth login --hostname github.com --git-protocol https --with-token\n"
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "gh auth setup-git --hostname github.com"
        )
        command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " + _ps_single_quote(ps_script)
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
        remote_os=remote.get("os", "unknown"),
        remote_user_name=remote.get("user_name", ""),
        remote_user_email=remote.get("user_email", ""),
        remote_gh_available=remote.get("gh_available") == "1",
        remote_gh_logged_in=remote.get("gh_logged_in") == "1",
        remote_gh_summary=remote.get("gh_summary", ""),
    )


def sync_git_login_to_server(ssh_name: str) -> str:
    """Configure remote Git identity and, when possible, GitHub CLI auth."""
    local_status, token, local_install_summary = _collect_local_status_for_sync()
    if not local_status.local_git_available:
        raise RuntimeError("本机未找到 git 命令")
    if not local_status.local_user_name and not local_status.local_user_email and not token:
        raise RuntimeError("本机没有可同步的 Git 身份或 GitHub CLI 登录")

    profile = _find_ssh_profile(ssh_name)
    client = ssh_manager.connect(profile)
    remote = _remote_probe(client)
    if remote.get("git_available") != "1":
        raise RuntimeError("远端未安装 git，请先在服务器上安装 git")

    parts = []
    if local_install_summary:
        parts.append(f"已自动安装本机 GitHub CLI: {local_install_summary}")
    if local_status.local_user_name or local_status.local_user_email:
        _configure_remote_git_identity(
            client,
            remote,
            local_status.local_user_name,
            local_status.local_user_email,
        )
        parts.append(f"已同步 Git 身份 {local_status.local_user_name or '-'} <{local_status.local_user_email or '-'}>")
    else:
        parts.append("本机未配置 Git 用户名/邮箱，已跳过身份同步")

    if token:
        if remote.get("gh_available") != "1":
            install_summary = _install_remote_gh(client, remote)
            parts.append(f"已自动安装 GitHub CLI: {install_summary}")
            remote = _remote_probe(client)
            if remote.get("gh_available") != "1":
                raise RuntimeError("远端 GitHub CLI 安装后仍不可用")

        _remote_gh_login(client, remote, token)
        parts.append("已同步 GitHub CLI 登录")
    else:
        if local_status.local_gh_available:
            parts.append("本机未检测到 GitHub CLI 登录 token，请先在本机执行 gh auth login 后再同步")
        else:
            parts.append("本机未检测到 GitHub CLI，已跳过 gh 登录")

    return "；".join(parts)
