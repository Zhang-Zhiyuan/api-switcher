"""Run release readiness checks for API Switcher."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


RUNTIME_DEPENDENCY_IMPORTS = (
    "customtkinter",
    "keyring",
    "tomli_w",
    "tomllib" if sys.version_info >= (3, 11) else "tomli",
    "PIL",
    "paramiko",
    "pystray",
    "cryptography",
    "yaml",
)

APP_NAME = "API切换器"
RELEASE_REQUIREMENTS_PATH = Path("requirements-release.txt")
PYTEST_ISOLATION_ROOT_ENV = "API_SWITCHER_RELEASE_TEST_ROOT"

# Release tests must not inherit live credentials, API routing, model overrides,
# or network proxy settings from the operator's shell. Keep this list local to
# the release runner so building it does not import application modules before
# pytest has installed its test doubles.
PYTEST_REMOVED_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "DEEPSEEK_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "DASHSCOPE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ZHIPU_API_KEY",
        "ZHIPUAI_API_KEY",
        "HF_TOKEN",
        "HF_ENDPOINT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_DRIVE_API_KEY",
        "GOOGLE_DRIVE_CLIENT_ID",
        "GOOGLE_DRIVE_CLIENT_SECRET",
        "GOOGLE_DRIVE_REFRESH_TOKEN",
        "GDRIVE_TOKEN",
        "PING0_API_KEY",
        "PROXYCHECK_API_KEY",
        "IPAPI_IS_API_KEY",
        "IPAPI_API_KEY",
        "IPQS_API_KEY",
        "IPQUALITYSCORE_API_KEY",
        "VPNAPI_KEY",
        "VPNAPI_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
)


def _release_pytest_basetemp() -> Path:
    """Return a stable system-temp path isolated for this checkout."""
    workspace = Path(__file__).resolve().parent
    workspace_key = hashlib.sha256(
        os.path.normcase(str(workspace)).encode("utf-8")
    ).hexdigest()[:12]
    return (
        Path(tempfile.gettempdir()).resolve()
        / "api-switcher-release-check"
        / workspace_key
        / "pytest"
    )


PYTEST_BASETEMP = _release_pytest_basetemp()
CHECKS = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    (
        "pytest",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            PYTEST_BASETEMP.as_posix(),
        ],
    ),
]

TEXT_EXTENSIONS = {".py", ".md", ".bat", ".txt", ".json", ".toml", ".ps1"}
SKIPPED_TEXT_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "backups",
    "build",
    "data",
    "dist",
    "env",
    "logs",
    "storage",
    "tmp_ui_screens",
    "venv",
}
SKIPPED_CLEAN_DIRS = {
    ".git",
    ".venv",
    "backups",
    "build",
    "data",
    "dist",
    "env",
    "storage",
    "venv",
}
MOJIBAKE_SOURCE_PHRASES = (
    "正在",
    "配置",
    "切换",
    "切换器",
    "文件",
    "启动",
    "加载",
    "创建",
    "完成",
    "失败",
    "压缩",
    "连接",
    "等待",
    "重试",
    "会话",
    "迁移",
    "错误",
    "恢复",
    "继续",
    "输入",
    "检测",
    "服务器",
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _pytest_environment_parent() -> Path:
    return PYTEST_BASETEMP.resolve().parent


def _is_safe_pytest_isolation_root(path: Path) -> bool:
    """Only accept direct ``environment-PID`` children of our temp parent."""
    try:
        resolved = Path(path).resolve()
        parent = _pytest_environment_parent()
    except OSError:
        return False
    suffix = resolved.name.removeprefix("environment-")
    return resolved.parent == parent and suffix.isdigit() and bool(suffix)


def _pytest_isolation_root() -> Path:
    inherited = os.environ.get(PYTEST_ISOLATION_ROOT_ENV, "").strip()
    if inherited:
        candidate = Path(inherited)
        if _is_safe_pytest_isolation_root(candidate):
            return candidate.resolve()
    return _pytest_environment_parent() / f"environment-{os.getpid()}"


def _remove_pytest_isolation_root(path: Path) -> bool:
    path = Path(path)
    if not _is_safe_pytest_isolation_root(path):
        print(f"pytest cleanup: skipped unexpected isolation path {path}", flush=True)
        return False
    if not path.exists():
        return True
    try:
        if path.is_symlink():
            print(f"pytest cleanup: skipped symlink isolation path {path}", flush=True)
            return False
        if path.is_dir():
            shutil.rmtree(path, onexc=_make_writable_and_retry)
        else:
            path.unlink()
        print(f"pytest cleanup: removed {path}", flush=True)
        return True
    except Exception as exc:
        print(f"pytest cleanup: failed to remove {path}: {exc}", flush=True)
        return False


def cleanup_pytest_isolation_directories(*, force=()) -> bool:
    """Remove this run and inactive stale pytest environments, never arbitrary paths."""
    parent = _pytest_environment_parent()
    forced = {Path(path).resolve() for path in force if _is_safe_pytest_isolation_root(path)}
    try:
        candidates = tuple(parent.glob("environment-*")) if parent.exists() else ()
    except OSError as exc:
        print(f"pytest cleanup: failed to inspect {parent}: {exc}", flush=True)
        return False

    ok = True
    for path in candidates:
        if not _is_safe_pytest_isolation_root(path):
            continue
        resolved = path.resolve()
        suffix = resolved.name.removeprefix("environment-")
        # A different live process may be running a concurrent release check.
        # Its directory must not be removed merely because both runs share the
        # stable per-checkout temp parent.
        if resolved not in forced and _process_id_is_running(int(suffix)):
            continue
        ok = _remove_pytest_isolation_root(resolved) and ok
    return ok


def _process_id_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            handle = open_process(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
            finally:
                close_handle(handle)
        except Exception:
            # Fail closed for cleanup: an uncertain PID is treated as live.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _command_env(label: str) -> dict[str, str]:
    env = _utf8_subprocess_env()
    if label == "pytest":
        temp_dir = PYTEST_BASETEMP.resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        isolation_root = _pytest_isolation_root()
        isolated_paths = {
            "CODEX_HOME": isolation_root / "codex",
            "CLAUDE_CONFIG_DIR": isolation_root / "claude",
            "API_SWITCHER_DATA_DIR": isolation_root / "api-switcher-data",
            "HOME": isolation_root / "home",
            "USERPROFILE": isolation_root / "home",
            "APPDATA": isolation_root / "appdata" / "roaming",
            "LOCALAPPDATA": isolation_root / "appdata" / "local",
            "XDG_CONFIG_HOME": isolation_root / "xdg" / "config",
            "XDG_DATA_HOME": isolation_root / "xdg" / "data",
            "XDG_CACHE_HOME": isolation_root / "xdg" / "cache",
        }
        for path in set(isolated_paths.values()):
            path.mkdir(parents=True, exist_ok=True)
        removed_names = {name.casefold() for name in PYTEST_REMOVED_ENV_NAMES}
        for name in tuple(env):
            if name.casefold() in removed_names:
                env.pop(name, None)
        env.update({name: str(path) for name, path in isolated_paths.items()})
        env[PYTEST_ISOLATION_ROOT_ENV] = str(isolation_root)
        env["API_SWITCHER_PORTABLE"] = "0"
        # Never let a missed test double read/write the user's real Windows
        # Credential Manager. The null backend makes the app exercise its
        # isolated DPAPI fallback instead.
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        env["TMP"] = str(temp_dir)
        env["TEMP"] = str(temp_dir)
        env["GIT_CEILING_DIRECTORIES"] = str(temp_dir)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
    return env


def _is_safe_workspace_path(path: Path) -> bool:
    workspace = Path.cwd().resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved != workspace and workspace in resolved.parents


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return True
    if not _is_safe_workspace_path(path):
        print(f"cleanup: skipped unexpected path {path}", flush=True)
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, onexc=_make_writable_and_retry)
        else:
            path.unlink()
        print(f"cleanup: removed {path}", flush=True)
        return True
    except Exception as exc:
        print(f"cleanup: failed to remove {path}: {exc}", flush=True)
        return False


def _make_writable_and_retry(function, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _iter_project_python_caches():
    """Yield project caches while pruning environments, user data, and build output."""
    skipped_dirs = {name.lower() for name in SKIPPED_CLEAN_DIRS}
    for directory, child_dirs, _filenames in os.walk(Path("."), topdown=True, followlinks=False):
        root = Path(directory)
        cache_names = [name for name in child_dirs if name.lower() == "__pycache__"]
        for name in sorted(cache_names):
            yield root / name
        child_dirs[:] = sorted(
            name
            for name in child_dirs
            if name.lower() != "__pycache__" and name.lower() not in skipped_dirs
        )


def cleanup_intermediate_files() -> bool:
    paths = [
        Path("build"),
        Path(".pytest_cache"),
        Path(".ruff_cache"),
        Path(f"{APP_NAME}.spec"),
    ]
    paths.extend(_iter_project_python_caches())
    ok = True
    for path in paths:
        ok = _remove_path(path) and ok
    return cleanup_pytest_isolation_directories() and ok


def _to_common_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gbk", errors="ignore").replace("?", "")


def _mojibake_terms() -> tuple[str, ...]:
    terms = {
        term
        for phrase in MOJIBAKE_SOURCE_PHRASES
        if (term := _to_common_mojibake(phrase)) and len(term) >= 2
    }
    return tuple(sorted(terms, key=len, reverse=True))


def _iter_workspace_files(extensions: set[str]):
    """Yield source files without descending into generated or user-data trees."""
    normalized_extensions = {suffix.lower() for suffix in extensions}
    skipped_dirs = {name.lower() for name in SKIPPED_TEXT_DIRS}
    for directory, child_dirs, filenames in os.walk(Path("."), topdown=True, followlinks=False):
        child_dirs[:] = sorted(name for name in child_dirs if name.lower() not in skipped_dirs)
        root = Path(directory)
        for filename in sorted(filenames):
            path = root / filename
            if path.suffix.lower() in normalized_extensions:
                yield path


def check_python_syntax() -> bool:
    """Compile project Python sources in memory without creating __pycache__."""
    print("\n== syntax ==", flush=True)
    findings: list[tuple[Path, str]] = []
    for path in _iter_workspace_files({".py"}):
        try:
            source = path.read_bytes()
            compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, ValueError) as exc:
            findings.append((path, str(exc)))

    if findings:
        print("Python source syntax errors found:", flush=True)
        for path, detail in findings[:20]:
            print(f"  - {path}: {detail}", flush=True)
        if len(findings) > 20:
            print(f"  ... and {len(findings) - 20} more", flush=True)
        print("syntax: FAILED", flush=True)
        return False

    print("Python source syntax: OK", flush=True)
    print("syntax: OK", flush=True)
    return True


def check_runtime_dependencies() -> bool:
    print("\n== dependencies ==", flush=True)
    missing = []
    for name in RUNTIME_DEPENDENCY_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:
            missing.append(f"{name}: {exc}")

    if missing:
        print("Missing runtime dependencies:", flush=True)
        for item in missing:
            print(f"  - {item}", flush=True)
        print("dependencies: FAILED", flush=True)
        return False

    print("Runtime dependencies: " + ", ".join(RUNTIME_DEPENDENCY_IMPORTS), flush=True)
    print("dependencies: OK", flush=True)
    return True


def _release_dependency_pins() -> dict[str, str]:
    """Return the exact direct dependency versions used for release builds."""
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        RELEASE_REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" not in line:
            raise ValueError(
                f"{RELEASE_REQUIREMENTS_PATH}:{line_number} 必须使用精确的 == 版本"
            )
        name, version = (part.strip() for part in line.split("==", 1))
        if not name or not version:
            raise ValueError(f"{RELEASE_REQUIREMENTS_PATH}:{line_number} 格式无效")
        pins[name] = version
    if not pins:
        raise ValueError(f"{RELEASE_REQUIREMENTS_PATH} 没有依赖版本")
    return pins


def check_release_dependency_versions() -> bool:
    """Ensure release builds use the dependency versions audited for this version."""
    print("\n== release-dependencies ==", flush=True)
    try:
        pins = _release_dependency_pins()
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"release-dependencies: FAILED ({exc})", flush=True)
        return False

    mismatches = []
    for name, expected in pins.items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{name}: 未安装（要求 {expected}）")
            continue
        if installed != expected:
            mismatches.append(f"{name}: 已安装 {installed}，要求 {expected}")

    if mismatches:
        print("Release dependency versions do not match:", flush=True)
        for item in mismatches:
            print(f"  - {item}", flush=True)
        print(
            f"Run: {sys.executable} -m pip install -r {RELEASE_REQUIREMENTS_PATH}",
            flush=True,
        )
        print("release-dependencies: FAILED", flush=True)
        return False

    print(f"Pinned release dependencies: {len(pins)} packages", flush=True)
    print("release-dependencies: OK", flush=True)
    return True


def run_command(label: str, command: list[str]) -> bool:
    print(f"\n== {label} ==", flush=True)
    print(" ".join(command), flush=True)
    isolation_root = _pytest_isolation_root() if label == "pytest" else None
    cleanup_ok = True
    try:
        result = subprocess.run(command, stderr=subprocess.STDOUT, env=_command_env(label))
    except OSError as exc:
        print(f"{label}: FAILED ({exc})", flush=True)
        return False
    finally:
        if isolation_root is not None:
            cleanup_ok = cleanup_pytest_isolation_directories(force=(isolation_root,))
    if result.returncode == 0 and cleanup_ok:
        print(f"{label}: OK", flush=True)
        return True
    if not cleanup_ok:
        print(f"{label}: FAILED (isolated test state cleanup failed)", flush=True)
        return False
    print(f"{label}: FAILED ({result.returncode})", flush=True)
    return False


def check_git_diff() -> bool:
    """Check repository whitespace when Git metadata and the executable exist."""
    if not Path(".git").exists():
        print("\n== diff-check ==", flush=True)
        print("diff-check: SKIPPED (source tree has no local Git metadata)", flush=True)
        return True
    if shutil.which("git") is None:
        print("\n== diff-check ==", flush=True)
        print("diff-check: SKIPPED (Git is not installed)", flush=True)
        return True
    return run_command("diff-check", ["git", "diff", "--check", "HEAD", "--"])


def check_source_mojibake() -> bool:
    print("\n== mojibake ==", flush=True)
    terms = _mojibake_terms()
    findings = []

    for path in _iter_workspace_files(TEXT_EXTENSIONS):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            findings.append((path, 0, f"not valid UTF-8: {exc}"))
            continue

        for line_number, line in enumerate(text.splitlines(), 1):
            matched = next((term for term in terms if term in line), None)
            if matched:
                findings.append((path, line_number, f"possible mojibake token {matched!r}"))
                break

    if findings:
        print("Potential Chinese mojibake found:", flush=True)
        for path, line_number, detail in findings[:20]:
            location = f"{path}:{line_number}" if line_number else str(path)
            print(f"  - {location}: {detail}", flush=True)
        if len(findings) > 20:
            print(f"  ... and {len(findings) - 20} more", flush=True)
        print("mojibake: FAILED", flush=True)
        return False

    print("No source mojibake markers found.", flush=True)
    print("mojibake: OK", flush=True)
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def check_artifacts() -> bool:
    dist_dir = Path("dist")
    exe_files = sorted(dist_dir.rglob("*.exe")) if dist_dir.exists() else []
    if not exe_files:
        print("\nNo dist/**/*.exe file found.", flush=True)
        print("artifacts: FAILED - release artifact is missing", flush=True)
        return False
    print("\n== artifacts ==", flush=True)
    for path in exe_files:
        print(f"{path}  {path.stat().st_size} bytes  SHA256 {sha256_file(path)}", flush=True)

    onefile_exe = dist_dir / f"{APP_NAME}.exe"
    if not onefile_exe.exists():
        print(
            f"artifacts: FAILED - single-file artifact is missing: {onefile_exe}",
            flush=True,
        )
        return False

    stale_onedir = dist_dir / APP_NAME
    if stale_onedir.exists():
        print(
            f"artifacts: FAILED - stale folder artifact still exists: {stale_onedir}",
            flush=True,
        )
        return False

    stale_zip = dist_dir / f"{APP_NAME}.zip"
    if stale_zip.exists():
        print(
            f"artifacts: FAILED - stale folder archive still exists: {stale_zip}",
            flush=True,
        )
        return False

    print("artifacts: OK", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release checks.")
    parser.add_argument("--build", action="store_true", help="Run build_exe.py after checks pass.")
    args = parser.parse_args()

    if not Path("main.py").exists():
        print("main.py was not found. Run this script from the project root.", flush=True)
        return 2

    failed = []
    if not check_runtime_dependencies():
        failed.append("dependencies")
    if not check_release_dependency_versions():
        failed.append("release-dependencies")
    if not check_source_mojibake():
        failed.append("mojibake")
    if not check_python_syntax():
        failed.append("syntax")
    failed.extend(label for label, command in CHECKS if not run_command(label, command))
    if not check_git_diff():
        failed.append("diff-check")
    if failed:
        print("\nRelease check failed: " + ", ".join(failed), flush=True)
        return 1

    if args.build and not run_command("build", [sys.executable, "build_exe.py"]):
        return 1

    if not check_artifacts():
        return 1

    if not cleanup_intermediate_files():
        return 1

    print("\nRelease check passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
