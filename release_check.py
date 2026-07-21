"""Run release readiness checks for API Switcher."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import shutil
import stat
import subprocess
import sys
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
PYTEST_BASETEMP = Path("build") / "pytest-tmp"
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


def _command_env(label: str) -> dict[str, str]:
    env = _utf8_subprocess_env()
    if label == "pytest":
        temp_dir = PYTEST_BASETEMP.resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        env["TMP"] = str(temp_dir)
        env["TEMP"] = str(temp_dir)
        env["GIT_CEILING_DIRECTORIES"] = str(temp_dir)
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
    return ok


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


def run_command(label: str, command: list[str]) -> bool:
    print(f"\n== {label} ==", flush=True)
    print(" ".join(command), flush=True)
    try:
        result = subprocess.run(command, stderr=subprocess.STDOUT, env=_command_env(label))
    except OSError as exc:
        print(f"{label}: FAILED ({exc})", flush=True)
        return False
    if result.returncode == 0:
        print(f"{label}: OK", flush=True)
        return True
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
        return True
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
