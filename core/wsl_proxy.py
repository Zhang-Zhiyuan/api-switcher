"""Safe WSL access to the Windows-managed local AI proxy.

The Windows proxy normally listens only on loopback.  WSL 1 and WSL 2 in
mirrored mode can use that endpoint directly.  WSL 2 NAT/virtioproxy needs the
Windows-side gateway plus a narrowly scoped mihomo LAN listener.  This module
keeps the Linux environment integration and any firewall exception explicitly
owned, reversible, and independent from Codex/Claude credentials.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core.atomic_io import atomic_copy_file, atomic_write_text
from core.redaction import redact_sensitive_text


logger = logging.getLogger(__name__)

WSL_SOURCE_BEGIN = "# >>> API_SWITCHER_WSL_PROXY >>>"
WSL_SOURCE_END = "# <<< API_SWITCHER_WSL_PROXY <<<"
WSL_MANAGED_FILE_HEADER = "# Managed by API切换器. Do not place credentials in this file."
WSL_MANAGED_DIR = ".config/api-switcher"
WSL_PROXY_SCRIPT = f"{WSL_MANAGED_DIR}/proxy.sh"
WSL_FISH_SCRIPT = ".config/fish/conf.d/api-switcher-proxy.fish"
WSL_FIREWALL_RULE_PREFIX = "API-Switcher-WSL-Proxy-"
WSL_NETWORK_MODES = frozenset({"nat", "mirrored", "virtioproxy", "bridged", "none"})
WSL_PROFILE_CANDIDATES = (
    (".profile", True),
    (".bashrc", False),
    (".bash_profile", False),
    (".bash_login", False),
    (".zprofile", False),
    (".zshrc", False),
    (".vscode-server/server-env-setup", False),
    (".vscode-server-insiders/server-env-setup", False),
)
WSL_LOOPBACK_ALLOWED_CIDRS = ("127.0.0.0/8", "::1/128")
WSL_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
WSL_CONFIG_KEYS = {
    "networkingmode": ("networkingMode", "mirrored"),
    "dnstunneling": ("dnsTunneling", "true"),
    "autoproxy": ("autoProxy", "true"),
}
WSL_CONFIG_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WSLProxyTarget:
    distro: str
    version: int
    network_mode: str
    home: str
    shell: str
    gateway: str = ""
    guest_cidr: str = ""

    @property
    def uses_loopback(self) -> bool:
        return self.version == 1 or self.network_mode == "mirrored"

    @property
    def proxy_host(self) -> str:
        return "127.0.0.1" if self.uses_loopback else self.gateway

    @property
    def requires_lan_listener(self) -> bool:
        return self.version == 2 and not self.uses_loopback

    @property
    def lan_allowed_ips(self) -> tuple[str, ...]:
        if not self.requires_lan_listener:
            return ()
        network = _validated_wsl_guest_network(self.guest_cidr)
        return (*WSL_LOOPBACK_ALLOWED_CIDRS, str(network))

    def display_mode(self) -> str:
        if self.version == 1:
            return "WSL1 共享网络"
        labels = {
            "mirrored": "WSL2 镜像网络",
            "nat": "WSL2 NAT",
            "virtioproxy": "WSL2 VirtioProxy",
            "bridged": "WSL2 桥接网络",
            "none": "WSL2 无网络",
        }
        return labels.get(self.network_mode, f"WSL2 {self.network_mode}")


@dataclass(frozen=True)
class WSLProxyIntegrationResult:
    target: WSLProxyTarget
    mixed_port: int
    configured_profiles: tuple[str, ...] = ()
    created_profiles: tuple[str, ...] = ()
    firewall_rule: str = ""
    tcp_reachable: bool = False
    detail: str = ""

    def state(self) -> dict:
        return {
            "owner": "api-switcher",
            "enabled": True,
            "distro": self.target.distro,
            "version": self.target.version,
            "network_mode": self.target.network_mode,
            "gateway": self.target.gateway,
            "guest_cidr": self.target.guest_cidr,
            "mixed_port": self.mixed_port,
            "configured_profiles": list(self.configured_profiles),
            "created_profiles": list(self.created_profiles),
            "firewall_rule": self.firewall_rule,
        }

    def summary(self) -> str:
        access = "回环入口" if self.target.uses_loopback else "受限 WSL 虚拟子网入口"
        return (
            f"WSL 已接入（{self.target.distro}，{self.target.display_mode()}，{access}）；"
            "Codex/Claude Code 代理环境已写入，新开的 WSL 终端生效"
        )


@dataclass(frozen=True)
class WSLProxyInspection:
    available: bool
    configured: bool
    tcp_reachable: bool
    codex_reachable: bool | None = None
    claude_reachable: bool | None = None
    target: WSLProxyTarget | None = None
    detail: str = ""
    http_statuses: dict[str, int] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        api_ready = self.codex_reachable is not False and self.claude_reachable is not False
        return self.available and self.configured and self.tcp_reachable and api_ready

    def summary(self) -> str:
        if not self.available:
            return self.detail or "未检测到可用的默认 WSL 发行版"
        target = self.target
        prefix = (
            f"{target.distro}（{target.display_mode()}）" if target else "默认 WSL 发行版"
        )
        pieces = [prefix]
        pieces.append("环境入口已配置" if self.configured else "环境入口未配置")
        pieces.append("Windows 代理端口可达" if self.tcp_reachable else "Windows 代理端口不可达")
        if self.codex_reachable is not None:
            pieces.append("Codex 路径可达" if self.codex_reachable else "Codex 路径失败")
        if self.claude_reachable is not None:
            pieces.append("Claude Code 路径可达" if self.claude_reachable else "Claude Code 路径失败")
        if self.detail:
            pieces.append(self.detail)
        return "；".join(pieces)


def discover_default_wsl_target(timeout: float = 8.0) -> WSLProxyTarget:
    """Inspect the default WSL distribution without reading user credentials."""

    script = r'''
set -eu
mode=""
version=""
if command -v wslinfo >/dev/null 2>&1; then
  mode="$(wslinfo --networking-mode 2>/dev/null || true)"
  version="$(wslinfo --wsl-version 2>/dev/null || true)"
fi
kernel="$(uname -r 2>/dev/null || true)"
route="$(ip route show default 2>/dev/null | head -n 1 || true)"
gateway="$(printf '%s\n' "$route" | awk '{for(i=1;i<=NF;i++) if($i=="via" && i<NF){print $(i+1); exit}}')"
device="$(printf '%s\n' "$route" | awk '{for(i=1;i<=NF;i++) if($i=="dev" && i<NF){print $(i+1); exit}}')"
guest_cidr=""
if [ -n "$device" ]; then
  guest_cidr="$(ip -o -4 addr show dev "$device" scope global 2>/dev/null | awk 'NR==1{print $4}')"
fi
printf 'distro=%s\n' "${WSL_DISTRO_NAME:-}"
printf 'version=%s\n' "$version"
printf 'mode=%s\n' "$mode"
printf 'kernel=%s\n' "$kernel"
printf 'home=%s\n' "${HOME:-}"
printf 'shell=%s\n' "${SHELL:-}"
printf 'gateway=%s\n' "$gateway"
printf 'guest_cidr=%s\n' "$guest_cidr"
'''.strip()
    result = _run_wsl_command("", script, timeout=timeout)
    if result.returncode != 0:
        detail = _safe_process_error(result, "无法启动默认 WSL 发行版")
        raise RuntimeError(detail)
    values = _parse_key_value_output(result.stdout)
    distro = _validated_text_field(values.get("distro"), "WSL 发行版名称", 128)
    home = _validated_wsl_home(values.get("home"))
    shell = _validated_text_field(values.get("shell") or "/bin/sh", "WSL shell", 256)
    kernel = str(values.get("kernel") or "").strip()
    version_text = re.sub(r"[^0-9]", "", str(values.get("version") or ""))
    if version_text in {"1", "2"}:
        version = int(version_text)
    else:
        version = 2 if "wsl2" in kernel.casefold() or "microsoft-standard" in kernel.casefold() else 1
    mode = str(values.get("mode") or "").strip().casefold()
    if version == 1:
        mode = "mirrored"
    elif mode not in WSL_NETWORK_MODES:
        mode = "nat"
    if mode == "none":
        raise RuntimeError("默认 WSL 发行版当前处于无网络模式，无法接入 Windows 代理")
    if version == 2 and mode == "bridged":
        # In deprecated bridged mode the default route normally points at the
        # physical LAN router, not at the Windows host.  Treating that address
        # as our proxy endpoint would be both incorrect and potentially unsafe.
        raise RuntimeError(
            "默认 WSL 发行版正在使用已弃用的桥接网络，无法安全识别 Windows 宿主地址；"
            "请使用“优化 WSL 网络”切换到镜像模式"
        )

    gateway = str(values.get("gateway") or "").strip()
    guest_cidr = str(values.get("guest_cidr") or "").strip()
    if version == 2 and mode != "mirrored":
        gateway_address = _validated_ipv4(gateway, "WSL 看到的 Windows 宿主地址")
        network = _validated_wsl_guest_network(guest_cidr)
        if gateway_address not in network:
            raise RuntimeError("WSL 默认网关不在当前虚拟子网内，已拒绝开放不确定的代理入口")
        gateway = str(gateway_address)
        guest_cidr = str(network)
    else:
        gateway = ""
        guest_cidr = ""
    return WSLProxyTarget(
        distro=distro,
        version=version,
        network_mode=mode,
        home=home,
        shell=shell,
        gateway=gateway,
        guest_cidr=guest_cidr,
    )


def install_proxy_integration(
    target: WSLProxyTarget,
    mixed_port: int,
    binary_path: str | Path,
    *,
    previous_state: dict | None = None,
    timeout: float = 12.0,
) -> WSLProxyIntegrationResult:
    """Install marked user-level WSL environment hooks and verify the TCP path."""

    port = _validated_port(mixed_port)
    install_result = _run_wsl_command(
        target.distro,
        _build_install_script(port),
        timeout=timeout,
    )
    values = _parse_key_value_output(install_result.stdout)
    configured_profiles = _parse_relative_path_list(values.get("profiles"))
    created_profiles = _parse_relative_path_list(values.get("created"))
    if install_result.returncode != 0:
        # The installer reports incremental ownership on failure.  Remove only
        # our marked blocks/files so an I/O error cannot leave a half-installed
        # proxy environment behind.
        _remove_profile_hooks(
            target.distro,
            created_profiles=created_profiles,
            strict=False,
        )
        raise RuntimeError(_safe_process_error(install_result, "写入 WSL 代理环境入口失败"))
    previous = previous_state if isinstance(previous_state, dict) else {}
    previous_rule = ""
    if str(previous.get("owner") or "").casefold() == "api-switcher":
        candidate = str(previous.get("firewall_rule") or "").strip()
        if _owned_firewall_rule_name(candidate):
            previous_rule = candidate
    expected_rule = f"{WSL_FIREWALL_RULE_PREFIX}{port}"
    same_firewall_scope = bool(
        target.requires_lan_listener
        and previous_rule == expected_rule
        and str(previous.get("guest_cidr") or "").strip() == target.guest_cidr
        and _state_port(previous.get("mixed_port")) == port
    )
    firewall_rule = previous_rule if same_firewall_scope else ""
    try:
        if previous_rule and not same_firewall_scope:
            _delete_firewall_rule(previous_rule, strict=True)
        reachable = probe_wsl_proxy_tcp(target, port)
        if not reachable and target.requires_lan_listener:
            firewall_rule = _ensure_scoped_firewall_rule(target, port, Path(binary_path))
            reachable = probe_wsl_proxy_tcp(target, port)
        if not reachable:
            if target.requires_lan_listener:
                raise RuntimeError(
                    "WSL NAT 无法连接受限代理入口；请以管理员身份运行一次以创建精确防火墙规则，"
                    "或使用“优化 WSL 网络”切换到微软推荐的镜像模式"
                )
            raise RuntimeError("WSL 无法连接 Windows 回环代理，请确认 WSL 镜像网络已实际生效")
    except Exception:
        if firewall_rule:
            _delete_firewall_rule(firewall_rule, strict=False)
        _remove_profile_hooks(target.distro, created_profiles=created_profiles, strict=False)
        raise

    return WSLProxyIntegrationResult(
        target=target,
        mixed_port=port,
        configured_profiles=configured_profiles,
        created_profiles=created_profiles,
        firewall_rule=firewall_rule,
        tcp_reachable=True,
    )


def remove_proxy_integration(state: dict | None, *, strict: bool = True) -> str:
    """Remove only hooks and firewall state explicitly owned by this app."""

    data = state if isinstance(state, dict) else {}
    if data and str(data.get("owner") or "").casefold() != "api-switcher":
        if strict:
            raise RuntimeError("WSL 代理记录不属于本工具，未执行清理")
        return ""
    distro = str(data.get("distro") or "").strip()
    created_profiles = _parse_relative_path_list(data.get("created_profiles"))
    errors = []
    if distro:
        try:
            _remove_profile_hooks(distro, created_profiles=created_profiles, strict=True)
        except Exception as exc:
            errors.append(str(exc))
    firewall_rule = str(data.get("firewall_rule") or "").strip()
    if firewall_rule:
        try:
            _delete_firewall_rule(firewall_rule, strict=True)
        except Exception as exc:
            errors.append(str(exc))
    if errors and strict:
        raise RuntimeError("；".join(errors))
    return "WSL 用户环境入口和受管防火墙规则已清理" if distro or firewall_rule else ""


def inspect_proxy_integration(
    mixed_port: int,
    *,
    full_probe: bool = True,
    timeout: float = 10.0,
) -> WSLProxyInspection:
    """Check marked hooks plus Codex and Claude endpoints without using tokens."""

    try:
        target = discover_default_wsl_target(timeout=min(timeout, 8.0))
    except Exception as exc:
        return WSLProxyInspection(False, False, False, detail=str(exc))
    port = _validated_port(mixed_port)
    check = _run_wsl_command(
        target.distro,
        _build_profile_check_script(),
        timeout=min(timeout, 8.0),
    )
    values = _parse_key_value_output(check.stdout)
    configured = check.returncode == 0 and values.get("configured") == "1"
    tcp_reachable = probe_wsl_proxy_tcp(target, port)
    if not full_probe or not configured or not tcp_reachable:
        detail = ""
        if target.requires_lan_listener and not tcp_reachable:
            detail = "NAT/防火墙入口未打通，可尝试管理员修复或镜像网络"
        return WSLProxyInspection(
            True,
            configured,
            tcp_reachable,
            target=target,
            detail=detail,
        )
    api_result = _run_wsl_command(
        target.distro,
        _build_api_probe_script(target.proxy_host, port),
        timeout=max(12.0, timeout + 2.0),
    )
    statuses = _parse_probe_statuses(api_result.stdout)
    codex_ok = _http_probe_ok(statuses.get("codex"))
    claude_ok = _http_probe_ok(statuses.get("claude"))
    detail = ""
    if api_result.returncode != 0 and not statuses:
        detail = _safe_process_error(api_result, "WSL API 探测未完成")
    return WSLProxyInspection(
        True,
        configured,
        tcp_reachable,
        codex_reachable=codex_ok,
        claude_reachable=claude_ok,
        target=target,
        detail=detail,
        http_statuses=statuses,
    )


def probe_wsl_proxy_tcp(target: WSLProxyTarget, mixed_port: int, timeout: float = 4.0) -> bool:
    host = target.proxy_host
    port = _validated_port(mixed_port)
    if not host:
        return False
    script = (
        "if command -v python3 >/dev/null 2>&1; then "
        "python3 -c "
        + shlex.quote(
            "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); s.close()"
        )
        + f" {shlex.quote(host)} {port}; "
        "elif command -v bash >/dev/null 2>&1; then "
        f"timeout 4 bash -c {shlex.quote(f'</dev/tcp/{host}/{port}')} >/dev/null 2>&1; "
        "else exit 127; fi"
    )
    result = _run_wsl_command(target.distro, script, timeout=max(4.0, timeout))
    return result.returncode == 0


def optimize_wsl_networking(*, shutdown: bool = True) -> str:
    """Merge Microsoft's mirrored/DNS/auto-proxy settings into .wslconfig."""

    if os.name != "nt":
        raise RuntimeError("WSL 网络优化只支持 Windows 11")
    wsl_executable = _wsl_executable() if shutdown else ""
    path = Path.home() / ".wslconfig"
    if path.exists() and not path.is_file():
        raise RuntimeError(f"WSL 全局配置路径不是普通文件: {path}")
    if path.is_symlink():
        raise RuntimeError(f"WSL 全局配置是符号链接，已拒绝自动改写: {path}")
    existed = path.exists()
    original_snapshot = _wslconfig_file_snapshot(path) if existed else None
    original = _read_wslconfig_text(path) if existed else ""
    updated = _merge_wslconfig(original)
    changed = updated != original
    backup_path = None
    if changed:
        _assert_wslconfig_unchanged(path, original_snapshot)
    if changed and existed:
        backup_path = _next_wslconfig_backup_path(path)
        atomic_copy_file(path, backup_path)
        _assert_wslconfig_unchanged(path, original_snapshot)
    if changed:
        atomic_write_text(path, updated, encoding="utf-8")
    shutdown_detail = ""
    if shutdown:
        result = _run_windows_command([wsl_executable, "--shutdown"], timeout=20.0)
        if result.returncode != 0:
            raise RuntimeError(
                _safe_process_error(
                    result,
                    "镜像网络配置已保存，但 WSL 未能自动关闭；请手动执行 wsl --shutdown",
                )
            )
        shutdown_detail = "；已安全关闭当前 WSL 实例，重新打开发行版后生效"
    backup_detail = f"；原文件备份为 {backup_path.name}" if backup_path else ""
    changed_detail = "已写入" if changed else "原配置已符合要求"
    return f"{changed_detail} WSL 镜像网络、DNS 隧道和 Windows 自动代理设置{backup_detail}{shutdown_detail}"


def _build_posix_proxy_script(mixed_port: int) -> str:
    port = _validated_port(mixed_port)
    return f'''{WSL_MANAGED_FILE_HEADER}
_api_switcher_proxy_host="127.0.0.1"
_api_switcher_wsl_mode="$(wslinfo --networking-mode 2>/dev/null || true)"
case "$(uname -r 2>/dev/null || true)" in
  *WSL2*|*wsl2*|*microsoft-standard*) _api_switcher_wsl_version="2" ;;
  *) _api_switcher_wsl_version="1" ;;
esac
[ -n "$_api_switcher_wsl_mode" ] || {{
  [ "$_api_switcher_wsl_version" = "2" ] && _api_switcher_wsl_mode="nat" || _api_switcher_wsl_mode="mirrored"
}}
if [ "$_api_switcher_wsl_version" = "2" ] && [ "$_api_switcher_wsl_mode" != "mirrored" ]; then
  _api_switcher_gateway="$(ip route show default 2>/dev/null | awk '{{for(i=1;i<=NF;i++) if($i=="via" && i<NF){{print $(i+1); exit}}}}')"
  case "$_api_switcher_gateway" in
    ""|*[!0-9.]*) _api_switcher_proxy_host="" ;;
    *) _api_switcher_proxy_host="$_api_switcher_gateway" ;;
  esac
fi
if [ -n "$_api_switcher_proxy_host" ]; then
  API_SWITCHER_AI_PROXY_URL="http://$_api_switcher_proxy_host:{port}"
  HTTP_PROXY="$API_SWITCHER_AI_PROXY_URL"
  HTTPS_PROXY="$API_SWITCHER_AI_PROXY_URL"
  ALL_PROXY="$API_SWITCHER_AI_PROXY_URL"
  http_proxy="$API_SWITCHER_AI_PROXY_URL"
  https_proxy="$API_SWITCHER_AI_PROXY_URL"
  all_proxy="$API_SWITCHER_AI_PROXY_URL"
  [ -z "${{NO_PROXY:-}}" ] && [ -n "${{no_proxy:-}}" ] && NO_PROXY="$no_proxy"
  _api_switcher_add_no_proxy() {{
    case ",${{NO_PROXY:-}}," in
      *,"$1",*) ;;
      *) NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}$1" ;;
    esac
  }}
  _api_switcher_add_no_proxy "127.0.0.1"
  _api_switcher_add_no_proxy "localhost"
  _api_switcher_add_no_proxy "::1"
  _api_switcher_add_no_proxy "*.local"
  _api_switcher_add_no_proxy "$_api_switcher_proxy_host"
  no_proxy="$NO_PROXY"
  export API_SWITCHER_AI_PROXY_URL HTTP_PROXY HTTPS_PROXY ALL_PROXY
  export http_proxy https_proxy all_proxy NO_PROXY no_proxy
  unset -f _api_switcher_add_no_proxy 2>/dev/null || true
fi
unset _api_switcher_proxy_host _api_switcher_wsl_version _api_switcher_wsl_mode _api_switcher_gateway
'''


def _build_fish_proxy_script(mixed_port: int) -> str:
    port = _validated_port(mixed_port)
    return f'''{WSL_MANAGED_FILE_HEADER}
set -l api_switcher_proxy_host 127.0.0.1
set -l api_switcher_wsl_mode ""
command -q wslinfo; and set api_switcher_wsl_mode (wslinfo --networking-mode 2>/dev/null)
set -l api_switcher_wsl_version 1
if string match -qr 'WSL2|wsl2|microsoft-standard' -- (uname -r 2>/dev/null)
    set api_switcher_wsl_version 2
end
if test -z "$api_switcher_wsl_mode"
    test "$api_switcher_wsl_version" = 2; and set api_switcher_wsl_mode nat; or set api_switcher_wsl_mode mirrored
end
if test "$api_switcher_wsl_version" = "2"; and test "$api_switcher_wsl_mode" != "mirrored"
    set -l api_switcher_gateway (ip route show default 2>/dev/null | awk '{{for(i=1;i<=NF;i++) if($i=="via" && i<NF){{print $(i+1); exit}}}}')
    if string match -rq '^[0-9.]+$' -- "$api_switcher_gateway"
        set api_switcher_proxy_host "$api_switcher_gateway"
    else
        set api_switcher_proxy_host ""
    end
end
if test -n "$api_switcher_proxy_host"
    set -gx API_SWITCHER_AI_PROXY_URL "http://$api_switcher_proxy_host:{port}"
    for key in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
        set -gx $key "$API_SWITCHER_AI_PROXY_URL"
    end
    set -l api_switcher_no_proxy $NO_PROXY
    test -z "$api_switcher_no_proxy"; and set api_switcher_no_proxy $no_proxy
    for item in 127.0.0.1 localhost ::1 '*.local' "$api_switcher_proxy_host"
        if not contains -- "$item" (string split ',' -- "$api_switcher_no_proxy")
            if test -n "$api_switcher_no_proxy"
                set api_switcher_no_proxy (string join ',' -- (string trim -r -c ',' -- "$api_switcher_no_proxy") $item)
            else
                set api_switcher_no_proxy $item
            end
        end
    end
    set -gx NO_PROXY "$api_switcher_no_proxy"
    set -gx no_proxy "$api_switcher_no_proxy"
end
'''


def _build_install_script(mixed_port: int) -> str:
    posix_payload = shlex.quote(_build_posix_proxy_script(mixed_port))
    fish_payload = shlex.quote(_build_fish_proxy_script(mixed_port))
    source_block = shlex.quote(
        f'{WSL_SOURCE_BEGIN}\n[ -r "$HOME/{WSL_PROXY_SCRIPT}" ] && . "$HOME/{WSL_PROXY_SCRIPT}"\n{WSL_SOURCE_END}\n'
    )
    begin = shlex.quote(WSL_SOURCE_BEGIN)
    end = shlex.quote(WSL_SOURCE_END)
    owner_header = shlex.quote(WSL_MANAGED_FILE_HEADER)
    return f'''
set -eu
umask 077
profiles=""
created=""
report_partial_install() {{
  rc=$?
  trap - EXIT
  printf 'profiles=%s\ncreated=%s\n' "$profiles" "$created"
  exit "$rc"
}}
trap report_partial_install EXIT
managed_dir="$HOME/{WSL_MANAGED_DIR}"
managed_file="$HOME/{WSL_PROXY_SCRIPT}"
owner_header={owner_header}
preflight_owned_file() {{
  path="$1"
  label="$2"
  [ -e "$path" ] || [ -L "$path" ] || return 0
  if [ -L "$path" ] || [ ! -f "$path" ]; then
    printf 'managed path is not a regular owned file: %s\n' "$label" >&2
    return 1
  fi
  first_line="$(sed -n '1p' "$path" 2>/dev/null || true)"
  if [ "$first_line" != "$owner_header" ]; then
    printf 'existing file is not owned by API Switcher: %s\n' "$label" >&2
    return 1
  fi
}}
preflight_owned_file "$managed_file" {shlex.quote(WSL_PROXY_SCRIPT)}
if command -v fish >/dev/null 2>&1 || [ -d "$HOME/.config/fish" ]; then
  preflight_owned_file "$HOME/{WSL_FISH_SCRIPT}" {shlex.quote(WSL_FISH_SCRIPT)}
fi
preflight_profile() {{
  relative="$1"
  create="$2"
  path="$HOME/$relative"
  if [ ! -e "$path" ] && [ "$create" != "1" ]; then
    return 0
  fi
  if [ ! -e "$path" ]; then
    return 0
  fi
  if [ -d "$path" ]; then
    printf 'profile path is a directory: %s\n' "$relative" >&2
    return 1
  fi
  if [ -L "$path" ]; then
    resolved="$(readlink -f "$path" 2>/dev/null || true)"
    case "$resolved" in
      "$HOME"/*) path="$resolved" ;;
      *) printf 'profile symlink leaves HOME: %s\n' "$relative" >&2; return 1 ;;
    esac
  fi
  [ -f "$path" ] || {{ printf 'profile path is not a regular file: %s\n' "$relative" >&2; return 1; }}
  [ -w "$path" ] || {{ printf 'profile path is not writable: %s\n' "$relative" >&2; return 1; }}
  begin_count="$(grep -Fxc {begin} "$path" 2>/dev/null || true)"
  end_count="$(grep -Fxc {end} "$path" 2>/dev/null || true)"
  if [ "$begin_count" != "$end_count" ] || [ "$begin_count" -gt 1 ]; then
    printf 'ambiguous API Switcher markers in %s\n' "$relative" >&2
    return 1
  fi
}}
preflight_profile .profile 1
for candidate in .bashrc .bash_profile .bash_login .zprofile .zshrc .vscode-server/server-env-setup .vscode-server-insiders/server-env-setup; do
  preflight_profile "$candidate" 0
done
mkdir -p "$managed_dir"
tmp="$managed_file.tmp.$$"
printf '%s' {posix_payload} > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$managed_file"
begin={begin}
end={end}
source_block={source_block}
update_profile() {{
  relative="$1"
  create="$2"
  path="$HOME/$relative"
  if [ ! -e "$path" ] && [ "$create" != "1" ]; then
    return 0
  fi
  if [ -d "$path" ]; then
    printf 'profile path is a directory: %s\n' "$relative" >&2
    return 1
  fi
  if [ -L "$path" ]; then
    resolved="$(readlink -f "$path" 2>/dev/null || true)"
    case "$resolved" in
      "$HOME"/*) path="$resolved" ;;
      *) printf 'profile symlink leaves HOME: %s\n' "$relative" >&2; return 1 ;;
    esac
  fi
  was_missing=0
  if [ ! -e "$path" ]; then
    was_missing=1
    mkdir -p "$(dirname "$path")"
    : > "$path"
    created="${{created:+$created,}}$relative"
    chmod 600 "$path"
  fi
  begin_count="$(grep -Fxc "$begin" "$path" 2>/dev/null || true)"
  end_count="$(grep -Fxc "$end" "$path" 2>/dev/null || true)"
  if [ "$begin_count" != "$end_count" ] || [ "$begin_count" -gt 1 ]; then
    printf 'ambiguous API Switcher markers in %s\n' "$relative" >&2
    return 1
  fi
  output="$path.api-switcher.$$"
  awk -v b="$begin" -v e="$end" '
    $0 == b {{ hidden=1; next }}
    $0 == e {{ hidden=0; next }}
    !hidden {{ print }}
  ' "$path" > "$output"
  if [ -s "$output" ]; then printf '\n' >> "$output"; fi
  printf '%s' "$source_block" >> "$output"
  chmod --reference="$path" "$output" 2>/dev/null || chmod 600 "$output"
  mv -f "$output" "$path"
  profiles="${{profiles:+$profiles,}}$relative"
}}
update_profile .profile 1
for candidate in .bashrc .bash_profile .bash_login .zprofile .zshrc .vscode-server/server-env-setup .vscode-server-insiders/server-env-setup; do
  update_profile "$candidate" 0
done
if command -v fish >/dev/null 2>&1 || [ -d "$HOME/.config/fish" ]; then
  fish_file="$HOME/{WSL_FISH_SCRIPT}"
  mkdir -p "$(dirname "$fish_file")"
  fish_tmp="$fish_file.tmp.$$"
  printf '%s' {fish_payload} > "$fish_tmp"
  chmod 600 "$fish_tmp"
  mv -f "$fish_tmp" "$fish_file"
fi
printf 'configured=1\nprofiles=%s\ncreated=%s\n' "$profiles" "$created"
trap - EXIT
'''.strip()


def _build_remove_script(created_profiles: tuple[str, ...]) -> str:
    created = " ".join(shlex.quote(item) for item in created_profiles)
    begin = shlex.quote(WSL_SOURCE_BEGIN)
    end = shlex.quote(WSL_SOURCE_END)
    owner_header = shlex.quote(WSL_MANAGED_FILE_HEADER)
    return f'''
set -eu
begin={begin}
end={end}
owner_header={owner_header}
created_profiles={shlex.quote(created)}
is_created() {{
  case " $created_profiles " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}}
clean_profile() {{
  relative="$1"
  path="$HOME/$relative"
  [ -e "$path" ] || return 0
  [ ! -d "$path" ] || return 0
  if [ -L "$path" ]; then
    resolved="$(readlink -f "$path" 2>/dev/null || true)"
    case "$resolved" in "$HOME"/*) path="$resolved" ;; *) return 0 ;; esac
  fi
  begin_count="$(grep -Fxc "$begin" "$path" 2>/dev/null || true)"
  end_count="$(grep -Fxc "$end" "$path" 2>/dev/null || true)"
  if [ "$begin_count" != "$end_count" ] || [ "$begin_count" -gt 1 ]; then
    printf 'ambiguous API Switcher markers in %s\n' "$relative" >&2
    return 1
  fi
  [ "$begin_count" = "1" ] || return 0
  output="$path.api-switcher.$$"
  awk -v b="$begin" -v e="$end" '
    $0 == b {{ hidden=1; next }}
    $0 == e {{ hidden=0; next }}
    !hidden {{ print }}
  ' "$path" > "$output"
  chmod --reference="$path" "$output" 2>/dev/null || chmod 600 "$output"
  mv -f "$output" "$path"
  if is_created "$relative" && ! grep -q '[^[:space:]]' "$path"; then rm -f "$path"; fi
}}
for candidate in .profile .bashrc .bash_profile .bash_login .zprofile .zshrc .vscode-server/server-env-setup .vscode-server-insiders/server-env-setup; do
  clean_profile "$candidate"
done
remove_owned_file() {{
  path="$1"
  [ -e "$path" ] || [ -L "$path" ] || return 0
  [ ! -L "$path" ] || return 0
  [ -f "$path" ] || return 0
  first_line="$(sed -n '1p' "$path" 2>/dev/null || true)"
  [ "$first_line" = "$owner_header" ] && rm -f "$path"
}}
remove_owned_file "$HOME/{WSL_PROXY_SCRIPT}"
remove_owned_file "$HOME/{WSL_FISH_SCRIPT}"
rmdir "$HOME/{WSL_MANAGED_DIR}" 2>/dev/null || true
printf 'removed=1\n'
'''.strip()


def _build_profile_check_script() -> str:
    begin = shlex.quote(WSL_SOURCE_BEGIN)
    owner_header = shlex.quote(WSL_MANAGED_FILE_HEADER)
    return f'''
set -eu
managed="$HOME/{WSL_PROXY_SCRIPT}"
[ -r "$managed" ] || {{ printf 'configured=0\n'; exit 0; }}
[ ! -L "$managed" ] || {{ printf 'configured=0\n'; exit 0; }}
[ "$(sed -n '1p' "$managed" 2>/dev/null || true)" = {owner_header} ] || {{ printf 'configured=0\n'; exit 0; }}
found=0
for candidate in .profile .bashrc .bash_profile .bash_login .zprofile .zshrc .vscode-server/server-env-setup .vscode-server-insiders/server-env-setup; do
  path="$HOME/$candidate"
  if [ -f "$path" ] && grep -Fqx {begin} "$path" 2>/dev/null; then found=1; break; fi
done
printf 'configured=%s\n' "$found"
'''.strip()


def _build_api_probe_script(host: str, mixed_port: int) -> str:
    port = _validated_port(mixed_port)
    proxy_url = f"http://{host}:{port}"
    return f'''
set -u
if ! command -v curl >/dev/null 2>&1; then
  printf 'error=curl-missing\n'
  exit 2
fi
tmp="${{TMPDIR:-/tmp}}/api-switcher-wsl-probe.$$"
mkdir -m 700 "$tmp" || exit 3
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
probe() {{
  name="$1"
  url="$2"
  code="$(curl --silent --show-error --output /dev/null --write-out '%{{http_code}}' \
    --connect-timeout 4 --max-time 9 --proxy {shlex.quote(proxy_url)} --noproxy '' "$url" 2>/dev/null)"
  rc=$?
  printf '%s=%s:%s\n' "$name" "$rc" "$code" > "$tmp/$name"
}}
probe codex https://api.openai.com/v1/models &
p1=$!
probe claude https://api.anthropic.com/v1/messages &
p2=$!
wait "$p1" || true
wait "$p2" || true
cat "$tmp/codex" "$tmp/claude"
'''.strip()


def _remove_profile_hooks(
    distro: str,
    *,
    created_profiles: tuple[str, ...] = (),
    strict: bool,
) -> None:
    result = _run_wsl_command(
        distro,
        _build_remove_script(created_profiles),
        timeout=10.0,
    )
    if result.returncode != 0 and strict:
        raise RuntimeError(_safe_process_error(result, "清理 WSL 代理环境入口失败"))


def _ensure_scoped_firewall_rule(
    target: WSLProxyTarget,
    mixed_port: int,
    binary_path: Path,
) -> str:
    if not target.requires_lan_listener:
        return ""
    if not _is_windows_admin():
        raise RuntimeError("当前进程没有管理员权限，无法创建仅允许 WSL 子网的防火墙规则")
    binary = binary_path.resolve(strict=False)
    if not binary.is_file():
        raise RuntimeError("未找到受管 mihomo 程序，无法创建精确防火墙规则")
    port = _validated_port(mixed_port)
    rule_name = f"{WSL_FIREWALL_RULE_PREFIX}{port}"
    _delete_firewall_rule(rule_name, strict=False)
    args = [
        _netsh_executable(),
        "advfirewall",
        "firewall",
        "add",
        "rule",
        f"name={rule_name}",
        "dir=in",
        "action=allow",
        "protocol=TCP",
        f"localport={port}",
        f"remoteip={target.guest_cidr}",
        f"program={binary}",
        "profile=any",
        "edge=no",
        "enable=yes",
    ]
    result = _run_windows_command(args, timeout=15.0)
    if result.returncode != 0:
        raise RuntimeError(_safe_process_error(result, "创建 WSL 专用防火墙规则失败"))
    return rule_name


def _delete_firewall_rule(rule_name: str, *, strict: bool) -> None:
    clean = str(rule_name or "").strip()
    if not _owned_firewall_rule_name(clean):
        if strict:
            raise RuntimeError("防火墙规则名称不属于本工具，未执行删除")
        return
    result = _run_windows_command(
        [
            _netsh_executable(),
            "advfirewall",
            "firewall",
            "delete",
            "rule",
            f"name={clean}",
        ],
        timeout=12.0,
    )
    # netsh can return zero when no matching rule exists.  A non-zero result is
    # actionable only for strict cleanup of a rule recorded in our state.
    if result.returncode != 0 and strict:
        raise RuntimeError(_safe_process_error(result, "删除 WSL 专用防火墙规则失败"))


def _owned_firewall_rule_name(value: str) -> bool:
    clean = str(value or "").strip()
    suffix = clean[len(WSL_FIREWALL_RULE_PREFIX) :] if clean.startswith(WSL_FIREWALL_RULE_PREFIX) else ""
    if not suffix.isdigit():
        return False
    try:
        return _validated_port(suffix) == int(suffix)
    except (TypeError, ValueError):
        return False


def _state_port(value) -> int | None:
    try:
        return _validated_port(value)
    except (TypeError, ValueError):
        return None


def _merge_wslconfig(content: str) -> str:
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    section_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"\s*\[\s*wsl2\s*]\s*(?:[;#].*)?", line, re.IGNORECASE)
    ]
    if len(section_indexes) > 1:
        raise RuntimeError(".wslconfig 包含多个 [wsl2] 段，已拒绝自动合并")
    if not section_indexes:
        if lines and any(line.strip() for line in lines):
            lines.append("")
        lines.extend(["[wsl2]", *(f"{name}={value}" for name, value in WSL_CONFIG_KEYS.values())])
        return "\n".join(lines).rstrip() + "\n"

    section_start = section_indexes[0]
    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        if re.fullmatch(r"\s*\[[^]]+]\s*(?:[;#].*)?", lines[index]):
            section_end = index
            break
    found: dict[str, int] = {}
    for index in range(section_start + 1, section_end):
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=", lines[index])
        if not match:
            continue
        key = match.group(1).casefold()
        if key not in WSL_CONFIG_KEYS:
            continue
        if key in found:
            raise RuntimeError(f".wslconfig 的 [wsl2] 段包含重复设置 {match.group(1)}")
        found[key] = index
    for key, (display_name, desired) in WSL_CONFIG_KEYS.items():
        if key in found:
            line = lines[found[key]]
            match = re.match(
                r"^(\s*)[A-Za-z][A-Za-z0-9]*(\s*=\s*)[^#;]*?(\s*(?:[#;].*)?)$",
                line,
            )
            if not match:
                raise RuntimeError(f"无法安全解析 .wslconfig 设置 {display_name}")
            lines[found[key]] = f"{match.group(1)}{display_name}{match.group(2)}{desired}{match.group(3)}"
        else:
            lines.insert(section_end, f"{display_name}={desired}")
            section_end += 1
    return "\n".join(lines).rstrip() + "\n"


def _parse_key_value_output(output: str) -> dict[str, str]:
    values = {}
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip().replace("\x00", "")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip().casefold()
        if re.fullmatch(r"[a-z][a-z0-9_]*", clean_key):
            values[clean_key] = value.strip()
    return values


def _parse_relative_path_list(value) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = str(value or "").split(",")
    allowed = {path for path, _create in WSL_PROFILE_CANDIDATES}
    result = []
    for item in candidates:
        path = str(item or "").strip().replace("\\", "/")
        if path in allowed and path not in result:
            result.append(path)
    return tuple(result)


def _parse_probe_statuses(output: str) -> dict[str, int]:
    statuses = {}
    for name in ("codex", "claude"):
        match = re.search(rf"(?m)^{name}=(\d+):(\d{{3}})\s*$", str(output or ""))
        if not match:
            continue
        return_code = int(match.group(1))
        http_status = int(match.group(2))
        statuses[name] = http_status if return_code == 0 else 0
    return statuses


def _http_probe_ok(status: int | None) -> bool:
    # 401/403/404/405 prove that the remote API path was reached without using
    # credentials.  A 5xx response, especially 502/503/504, is commonly emitted
    # by the local proxy when the upstream route is broken and must not be
    # reported as healthy.
    return bool(status is not None and 100 <= int(status) < 500 and int(status) != 407)


def _read_wslconfig_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"无法读取 WSL 全局配置: {path}") from exc
    if size > WSL_CONFIG_MAX_BYTES:
        raise RuntimeError(".wslconfig 文件异常过大，已拒绝自动改写")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"无法读取 WSL 全局配置: {path}") from exc
    encodings = []
    if payload.startswith(b"\xff\xfe"):
        encodings.append("utf-16")
    elif payload.startswith(b"\xfe\xff"):
        encodings.append("utf-16")
    encodings.extend(("utf-8-sig", "utf-8"))
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(".wslconfig 不是受支持的 UTF-8/UTF-16 文本，已拒绝自动改写")


def _wslconfig_file_snapshot(path: Path) -> tuple[int, int, str]:
    try:
        stat = path.stat()
        if stat.st_size > WSL_CONFIG_MAX_BYTES:
            raise RuntimeError(".wslconfig 文件异常过大，已拒绝自动改写")
        payload = path.read_bytes()
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"无法读取 WSL 全局配置: {path}") from exc
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(payload).hexdigest()


def _assert_wslconfig_unchanged(
    path: Path,
    expected: tuple[int, int, str] | None,
) -> None:
    if expected is None:
        if path.exists() or path.is_symlink():
            raise RuntimeError(".wslconfig 在优化期间由其他程序创建，已拒绝覆盖")
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(".wslconfig 在优化期间被替换，已拒绝覆盖")
    if _wslconfig_file_snapshot(path) != expected:
        raise RuntimeError(".wslconfig 在优化期间已被其他程序修改，请检查后重试")


def _next_wslconfig_backup_path(path: Path) -> Path:
    first = path.with_name(".wslconfig.api-switcher.bak")
    if not first.exists():
        return first
    for index in range(1, 1000):
        candidate = path.with_name(f".wslconfig.api-switcher.bak.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(".wslconfig 备份文件过多，请先整理旧备份后重试")


def _validated_wsl_guest_network(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_interface(str(value or "").strip()).network
    except ValueError as exc:
        raise RuntimeError("未识别到可信的 WSL IPv4 虚拟子网") from exc
    if not isinstance(network, ipaddress.IPv4Network) or not any(
        network.subnet_of(parent) for parent in WSL_PRIVATE_IPV4_NETWORKS
    ):
        raise RuntimeError("WSL 虚拟子网不是私有 IPv4 网络，已拒绝开放代理入口")
    if network.prefixlen < 16:
        raise RuntimeError("WSL 虚拟子网范围过大，已拒绝开放代理入口")
    return network


def _validated_ipv4(value: str, label: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as exc:
        raise RuntimeError(f"未识别到有效的{label}") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise RuntimeError(f"{label}不是 IPv4 地址")
    return address


def _validated_port(value) -> int:
    if isinstance(value, bool):
        raise ValueError("WSL 代理端口无效")
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("WSL 代理端口无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("WSL 代理端口必须在 1-65535 之间")
    return port


def _validated_text_field(value, label: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or any(ord(char) < 32 for char in text):
        raise RuntimeError(f"未识别到有效的{label}")
    return text


def _validated_wsl_home(value) -> str:
    home = _validated_text_field(value, "WSL 用户目录", 512)
    if not home.startswith("/") or home == "/" or "/../" in f"{home}/":
        raise RuntimeError("WSL 用户目录不安全，已拒绝写入环境入口")
    return home.rstrip("/")


def _is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _wsl_executable() -> str:
    executable = shutil.which("wsl.exe") or shutil.which("wsl")
    if not executable:
        raise RuntimeError("未安装或未启用 Windows Subsystem for Linux")
    return executable


def _netsh_executable() -> str:
    executable = shutil.which("netsh.exe") or shutil.which("netsh")
    if not executable:
        raise RuntimeError("未找到 Windows netsh，无法管理 WSL 防火墙规则")
    return executable


def _run_wsl_command(distro: str, script: str, *, timeout: float) -> subprocess.CompletedProcess:
    args = [_wsl_executable()]
    clean_distro = str(distro or "").strip()
    if clean_distro:
        args.extend(["--distribution", clean_distro])
    args.extend(["--exec", "sh", "-lc", str(script)])
    return _run_windows_command(args, timeout=timeout)


def _run_windows_command(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            [str(item) for item in args],
            capture_output=True,
            timeout=max(1.0, float(timeout)),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args, 127, b"", str(exc).encode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args,
            124,
            exc.stdout or b"",
            exc.stderr or b"command timed out",
        )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        _decode_process_stream(completed.stdout),
        _decode_process_stream(completed.stderr),
    )


def _decode_process_stream(value) -> str:
    if isinstance(value, str):
        return value.replace("\x00", "")
    payload = bytes(value or b"")
    if not payload:
        return ""
    if b"\x00" in payload[:256]:
        try:
            return payload.decode("utf-16-le", errors="replace").lstrip("\ufeff")
        except Exception:
            pass
    return payload.decode("utf-8", errors="replace").replace("\x00", "")


def _safe_process_error(result: subprocess.CompletedProcess, prefix: str) -> str:
    detail = redact_sensitive_text(result.stderr or result.stdout, max_length=300).strip()
    return f"{prefix}（退出码 {result.returncode}）" + (f": {detail}" if detail else "")
