"""Run release readiness checks for API Switcher."""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


CHECKS = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("compileall", [sys.executable, "-m", "compileall", "-q", "."]),
    ("pytest", [sys.executable, "-m", "pytest", "-q"]),
    ("diff-check", ["git", "diff", "--check"]),
]


def run_command(label: str, command: list[str]) -> bool:
    print(f"\n== {label} ==", flush=True)
    print(" ".join(command), flush=True)
    result = subprocess.run(command, stderr=subprocess.STDOUT)
    if result.returncode == 0:
        print(f"{label}: OK", flush=True)
        return True
    print(f"{label}: FAILED ({result.returncode})", flush=True)
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def print_exe_hashes() -> None:
    exe_files = sorted(Path("dist").glob("*.exe"))
    if not exe_files:
        print("\nNo dist/*.exe file found.", flush=True)
        return
    print("\n== artifacts ==", flush=True)
    for path in exe_files:
        print(f"{path}  {path.stat().st_size} bytes  SHA256 {sha256_file(path)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release checks.")
    parser.add_argument("--build", action="store_true", help="Run build_exe.py after checks pass.")
    args = parser.parse_args()

    if not Path("main.py").exists():
        print("main.py was not found. Run this script from the project root.", flush=True)
        return 2

    failed = [label for label, command in CHECKS if not run_command(label, command)]
    if failed:
        print("\nRelease check failed: " + ", ".join(failed), flush=True)
        return 1

    if args.build and not run_command("build", [sys.executable, "build_exe.py"]):
        return 1

    print_exe_hashes()
    print("\nRelease check passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
