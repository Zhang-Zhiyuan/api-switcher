"""Regression coverage for watchdog cleanup of redirected child processes."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from core.auto_continue.powershell_runtime_helpers import (
    POWERSHELL_HARD_WATCHDOG_HELPER,
    POWERSHELL_REDIRECTED_PROCESS_HELPER,
)


def _powershell_executables() -> list[str]:
    return [
        executable
        for executable in (
            shutil.which("powershell.exe"),
            shutil.which("pwsh.exe"),
        )
        if executable
    ]


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(executable, id=Path(executable).stem)
        for executable in _powershell_executables()
    ],
)
def test_hard_watchdog_terminates_redirected_child_tree(tmp_path, powershell):
    """Killing the hook must not leave its redirected child running.

    stdout/stderr deliberately use DEVNULL here. If PIPE were used, the escaped
    child can inherit a writer and make ``communicate()`` hide the actual bug by
    waiting for EOF until the child's 30-second sleep ends.
    """

    child_pid_path = tmp_path / "redirected-child.pid"
    child_code = (
        "import os,time;"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
        "time.sleep(30)"
    )
    script = f"""function Write-Log {{ param([string]$Message, [string]$Level = 'INFO') }}
{POWERSHELL_HARD_WATCHDOG_HELPER}
{POWERSHELL_REDIRECTED_PROCESS_HELPER}
$script:watchdog = Start-ApiSwitcherHookWatchdog -TimeoutMilliseconds 500
$invocation = Start-ApiSwitcherRedirectedProcess `
    -FilePath {_powershell_literal(str(Path(sys.executable)))} `
    -Arguments @('-c', {_powershell_literal(child_code)})
$invocation.Process.WaitForExit(10000) | Out-Null
Write-Output 'watchdog failed'
"""

    started = time.monotonic()
    hook = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )
    elapsed = time.monotonic() - started

    deadline = time.monotonic() + 3
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert child_pid_path.exists(), "redirected child never wrote its PID"
    child_pid = int(child_pid_path.read_text(encoding="ascii"))

    synchronize = 0x00100000
    process_terminate = 0x0001
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    child_handle = kernel32.OpenProcess(
        synchronize | process_terminate,
        False,
        child_pid,
    )
    if not child_handle:
        child_exit = wait_object_0
    else:
        try:
            child_exit = kernel32.WaitForSingleObject(child_handle, 2000)
            if child_exit == wait_timeout:
                kernel32.TerminateProcess(child_handle, 1)
                kernel32.WaitForSingleObject(child_handle, 2000)
        finally:
            kernel32.CloseHandle(child_handle)

    assert hook.returncode != 0
    assert elapsed < 5
    assert child_exit == wait_object_0, (
        f"redirected child PID {child_pid} survived the hook watchdog"
    )

