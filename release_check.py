"""Run release readiness checks for API Switcher."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import subprocess
import sys
from pathlib import Path


RUNTIME_DEPENDENCY_IMPORTS = (
    "customtkinter",
    "keyring",
    "tomli_w",
    "PIL",
    "win32api",
    "paramiko",
    "pystray",
    "cryptography",
)

APP_NAME = "API切换器"
PYTEST_BASETEMP = Path("build") / "pytest-tmp"
CHECKS = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("compileall", [sys.executable, "-m", "compileall", "-q", "."]),
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
    ("diff-check", ["git", "diff", "--check"]),
]

TEXT_EXTENSIONS = {".py", ".md", ".bat", ".txt", ".json", ".toml", ".ps1"}
SKIPPED_TEXT_DIRS = {"build", "dist", ".git", ".pytest_cache", ".ruff_cache", "__pycache__"}
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


def _to_common_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gbk", errors="ignore").replace("?", "")


def _mojibake_terms() -> tuple[str, ...]:
    terms = {
        term
        for phrase in MOJIBAKE_SOURCE_PHRASES
        if (term := _to_common_mojibake(phrase)) and len(term) >= 2
    }
    return tuple(sorted(terms, key=len, reverse=True))


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
    result = subprocess.run(command, stderr=subprocess.STDOUT, env=_command_env(label))
    if result.returncode == 0:
        print(f"{label}: OK", flush=True)
        return True
    print(f"{label}: FAILED ({result.returncode})", flush=True)
    return False


def check_source_mojibake() -> bool:
    print("\n== mojibake ==", flush=True)
    terms = _mojibake_terms()
    findings = []

    for path in Path(".").rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if any(part in SKIPPED_TEXT_DIRS for part in path.parts):
            continue
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
    failed.extend(label for label, command in CHECKS if not run_command(label, command))
    if failed:
        print("\nRelease check failed: " + ", ".join(failed), flush=True)
        return 1

    if args.build and not run_command("build", [sys.executable, "build_exe.py"]):
        return 1

    if not check_artifacts():
        return 1

    print("\nRelease check passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
