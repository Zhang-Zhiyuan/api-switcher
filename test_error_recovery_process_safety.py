import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from core.auto_continue.error_recovery_script import (
    generate_codex_error_recovery_script,
    generate_error_recovery_script,
)
from core.auto_continue.powershell_runtime_helpers import (
    POWERSHELL_HARD_WATCHDOG_HELPER,
    POWERSHELL_REDIRECTED_PROCESS_HELPER,
)
from core.auto_continue.script_generator import generate_hook_script


GENERATORS = (
    pytest.param(generate_error_recovery_script, id="claude"),
    pytest.param(generate_codex_error_recovery_script, id="codex"),
)


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_generated_main_hook_uses_watchdog_safe_in_memory_process_output(provider):
    script = generate_hook_script(
        rf"C:\Users\Test\.{provider}\auto_continue_settings.json",
        provider_name=provider,
    )

    assert script.count("function Start-ApiSwitcherHookWatchdog") == 1
    assert "Start-ApiSwitcherHookWatchdog -TimeoutMilliseconds 25000" in script
    assert script.count("function Start-ApiSwitcherRedirectedProcess") == 1
    assert "$startInfo.RedirectStandardOutput = $true" in script
    assert "$startInfo.RedirectStandardError = $true" in script
    assert "$startInfo.WorkingDirectory = $WorkingDirectory" in script
    assert "-WorkingDirectory $gitWorkingDirectory" in script
    assert "$process.StandardOutput.ReadToEndAsync()" in script
    assert "$process.StandardError.ReadToEndAsync()" in script
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in script
    assert "AssignProcessToJobObject" in script
    assert "Add-Type" not in script
    assert "api-switcher-git-command-" not in script
    assert "api-switcher-taskkill-" not in script


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        pytest.skip("Windows PowerShell is not available")
    return executable


def _write_hook(
    tmp_path: Path,
    generator,
    *,
    git_enabled: bool,
) -> tuple[Path, Path]:
    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "error_recovery.ps1"
    settings_path.write_text(
        json.dumps(
            {
                "error_recovery_enabled": True,
                "max_error_recoveries": 3,
                "git_auto_snapshot": git_enabled,
                "git_snapshot_on_recovery": git_enabled,
                "git_auto_push": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script_path.write_text(
        generator(str(settings_path), enable_git=git_enabled),
        encoding="utf-8-sig",
    )
    return settings_path, script_path


def _error_payload(session_id: str) -> dict:
    return {
        "hook_event_name": "ResponseError",
        "session_id": session_id,
        "error_code": "content_length_exceeds_threshold",
        "error_message": "context window limit exceeded",
        "status": 400,
    }


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                f"$process = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                "if ($null -ne $process -and -not $process.HasExited) { exit 0 }; "
                "exit 1"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _wait_for_process_exit(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return True
        time.sleep(0.1)
    return not _process_is_running(pid)


def _api_switcher_temp_outputs() -> set[Path]:
    temp_dir = Path(tempfile.gettempdir())
    return {
        *temp_dir.glob("api-switcher-git-command-*.out"),
        *temp_dir.glob("api-switcher-git-command-*.out.err"),
        *temp_dir.glob("api-switcher-taskkill-*.out"),
        *temp_dir.glob("api-switcher-taskkill-*.out.err"),
    }


def _wait_for_no_new_temp_outputs(before: set[Path], timeout: float = 2.0) -> set[Path]:
    deadline = time.monotonic() + timeout
    remaining = _api_switcher_temp_outputs() - before
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = _api_switcher_temp_outputs() - before
    return remaining


@pytest.mark.parametrize("generator", GENERATORS)
def test_generated_error_hooks_share_bounded_input_and_git_safety(generator):
    script = generator(r"C:\Users\Test\.codex\auto_continue_settings.json")

    assert script.count("function Read-HookInputWithinBudget") == 1
    assert "$stdin = Read-HookInputWithinBudget" in script
    assert "ReadToEndAsync()" in script
    assert ".Wait($TimeoutMilliseconds)" in script
    assert "[Console]::In.ReadToEnd()" not in script

    assert script.count("function Start-ApiSwitcherHookWatchdog") == 1
    assert "New-Object System.Reflection.Emit.DynamicMethod(" in script
    assert "Start-ApiSwitcherHookWatchdog -TimeoutMilliseconds 25000" in script
    assert "Start-Process" not in script.split(
        "function Start-ApiSwitcherHookWatchdog", 1
    )[1].split("function Initialize-Utf8Console", 1)[0]

    assert script.count("function Start-ApiSwitcherRedirectedProcess") == 1
    assert script.count("function ConvertTo-ApiSwitcherNativeArgument") == 1
    assert "$startInfo.RedirectStandardOutput = $true" in script
    assert "$startInfo.RedirectStandardError = $true" in script
    assert "$startInfo.WorkingDirectory = $WorkingDirectory" in script
    assert "-WorkingDirectory $gitWorkingDirectory" in script
    assert "$process.StandardOutput.ReadToEndAsync()" in script
    assert "$process.StandardError.ReadToEndAsync()" in script
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in script
    assert "AssignProcessToJobObject" in script
    assert "Add-Type" not in script
    assert "api-switcher-git-command-" not in script
    assert "api-switcher-taskkill-" not in script

    assert script.count("function Invoke-GitCommandWithinBudget") == 1
    assert "$gitBudgetMilliseconds = 5000" in script
    assert '@("status", "--porcelain")' in script
    assert '@("add", "-A")' in script
    assert '@("config", "user.name")' in script
    assert '@("config", "user.email")' in script
    assert '@("push") + $pushArguments' in script
    assert '"commit.gpgSign=false"' in script
    assert '"--no-gpg-sign"' in script
    assert re.search(r"(?m)^\s*(?:\$[^=]+\s*=\s*)?git(?:\.exe)?\s", script) is None

    cleanup_helper = script.split("function Stop-GitProcessTree", 1)[1].split(
        "function Invoke-GitCommandWithTimeout",
        1,
    )[0]
    invoke_helper = script.split("function Invoke-GitCommandWithTimeout", 1)[1].split(
        "function Invoke-GitCommandWithinBudget",
        1,
    )[0]
    assert "Start-ApiSwitcherRedirectedProcess" in cleanup_helper
    assert '-FilePath "taskkill.exe"' in cleanup_helper
    assert "$taskkillProcess.WaitForExit(2000)" in cleanup_helper
    assert "$taskkillProcess.WaitForExit(250)" in cleanup_helper
    assert "if (-not $taskkillHasExited)" in cleanup_helper
    assert "$taskkillProcess.Kill()" in cleanup_helper
    assert "& taskkill.exe" not in cleanup_helper
    assert "$Invocation.JobHandle.Dispose()" in cleanup_helper
    assert "using taskkill fallback" in cleanup_helper
    assert "$cleanupSucceeded = (" in script
    assert "-not $taskkillTimedOut -and" in script
    assert "if ($null -ne $process -and -not (Test-GitProcessHasExited -Process $process))" in invoke_helper
    assert (
        'Stop-GitProcessTree -Process $process -Invocation $invocation '
        '-Reason "exception"'
    ) in invoke_helper
    assert (
        'Stop-GitProcessTree -Process $process -Invocation $invocation '
        '-Reason "finalization"'
    ) in invoke_helper
    assert "if (-not $captured.Succeeded)" in invoke_helper
    assert 'ExitCode = -1' in invoke_helper
    assert 'CleanupSucceeded = $false' in invoke_helper
    assert 'GIT_TERMINAL_PROMPT = "0"' in script
    assert 'GCM_INTERACTIVE = "Never"' in script
    assert "SetEnvironmentVariable(" not in invoke_helper


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(path, id=Path(path).stem)
        for path in (shutil.which("powershell.exe"), shutil.which("pwsh.exe"))
        if path
    ],
)
def test_in_process_watchdog_hard_stops_a_blocked_hook(powershell):
    script = f'''function Write-Log {{ param([string]$Message, [string]$Level = "INFO") }}
{POWERSHELL_HARD_WATCHDOG_HELPER}
$script:watchdog = Start-ApiSwitcherHookWatchdog -TimeoutMilliseconds 300
Start-Sleep -Seconds 30
Write-Output "watchdog failed"
'''

    started = time.monotonic()
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=6,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert "watchdog failed" not in result.stdout
    assert elapsed < 5


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(path, id=Path(path).stem)
        for path in (shutil.which("powershell.exe"), shutil.which("pwsh.exe"))
        if path
    ],
)
def test_redirected_process_helper_handles_large_output_and_native_arguments(
    powershell,
    tmp_path,
):
    python_path = str(Path(sys.executable)).replace("'", "''")
    child_working_directory = tmp_path / "child working directory"
    child_working_directory.mkdir()
    working_directory_literal = str(child_working_directory).replace("'", "''")
    arguments = (
        "plain",
        "two words",
        'quote"inside',
        "trailing\\",
        'slashes\\\\before"quote',
    )
    powershell_arguments = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in arguments
    )
    child_code = (
        "import json,os,sys;"
        "print(json.dumps({'arguments':sys.argv[1:],'cwd':os.getcwd()}));"
        "sys.stderr.write('x'*131072)"
    ).replace("'", "''")
    script = f'''function Write-Log {{ param([string]$Message, [string]$Level = "INFO") }}
{POWERSHELL_REDIRECTED_PROCESS_HELPER}
Set-Location -LiteralPath '{working_directory_literal}'
$workingDirectory = [string](
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(".")
)
$invocation = Start-ApiSwitcherRedirectedProcess `
    -FilePath '{python_path}' `
    -Arguments @('-c', '{child_code}', {powershell_arguments}) `
    -WorkingDirectory $workingDirectory
try {{
    if (-not $invocation.Process.WaitForExit(5000)) {{ throw "child timed out" }}
    $captured = Get-ApiSwitcherRedirectedOutput -Invocation $invocation -TimeoutMilliseconds 1000
        [pscustomobject]@{{
            ExitCode = [int]$invocation.Process.ExitCode
            Stdout = [string]$captured.Stdout
            StderrLength = ([string]$captured.Stderr).Length
    }} | ConvertTo-Json -Compress
}} finally {{
    Close-ApiSwitcherRedirectedProcess -Invocation $invocation
}}
'''

    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ExitCode"] == 0
    child_payload = json.loads(payload["Stdout"])
    assert child_payload["arguments"] == list(arguments)
    assert Path(child_payload["cwd"]).resolve() == child_working_directory.resolve()
    assert payload["StderrLength"] == 131072


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(path, id=Path(path).stem)
        for path in (shutil.which("powershell.exe"), shutil.which("pwsh.exe"))
        if path
    ],
)
def test_watchdog_kill_during_redirected_child_leaves_no_temp_output_files(
    powershell,
    tmp_path,
):
    before = _api_switcher_temp_outputs()
    python_path = str(Path(sys.executable)).replace("'", "''")
    child_pid_path = tmp_path / "watchdog-child.pid"
    child_code = (
        "import os,time;"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
        "time.sleep(30)"
    ).replace("'", "''")
    script = f'''function Write-Log {{ param([string]$Message, [string]$Level = "INFO") }}
{POWERSHELL_HARD_WATCHDOG_HELPER}
{POWERSHELL_REDIRECTED_PROCESS_HELPER}
$script:watchdog = Start-ApiSwitcherHookWatchdog -TimeoutMilliseconds 500
$invocation = Start-ApiSwitcherRedirectedProcess `
    -FilePath '{python_path}' `
    -Arguments @('-c', '{child_code}')
$invocation.Process.WaitForExit(10000) | Out-Null
Write-Output "watchdog failed"
'''

    started = time.monotonic()
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=6,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert "watchdog failed" not in result.stdout
    assert elapsed < 5
    assert child_pid_path.exists(), result.stderr
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    assert _wait_for_process_exit(child_pid, timeout=2)
    assert not _wait_for_no_new_temp_outputs(before)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(path, id=Path(path).stem)
        for path in (shutil.which("powershell.exe"), shutil.which("pwsh.exe"))
        if path
    ],
)
def test_inherited_pipe_drain_timeout_is_failure_and_job_close_kills_descendant(
    tmp_path,
    powershell,
):
    child_pid_path = tmp_path / "inherited-pipe-child.pid"
    python_path = str(Path(sys.executable)).replace("'", "''")
    root_code = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"open({str(child_pid_path)!r},'w').write(str(p.pid))"
    ).replace("'", "''")
    script = f'''function Write-Log {{ param([string]$Message, [string]$Level = "INFO") }}
{POWERSHELL_REDIRECTED_PROCESS_HELPER}
$invocation = Start-ApiSwitcherRedirectedProcess `
    -FilePath '{python_path}' `
    -Arguments @('-c', '{root_code}')
try {{
    if (-not $invocation.Process.WaitForExit(5000)) {{ throw "root timed out" }}
    $captured = Get-ApiSwitcherRedirectedOutput `
        -Invocation $invocation `
        -TimeoutMilliseconds 200
    [pscustomobject]@{{ Succeeded = [bool]$captured.Succeeded }} |
        ConvertTo-Json -Compress
}} finally {{
    Close-ApiSwitcherRedirectedProcess -Invocation $invocation
}}
'''

    started = time.monotonic()
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Succeeded"] is False
    assert elapsed < 5
    assert child_pid_path.exists(), result.stderr
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    assert _wait_for_process_exit(child_pid, timeout=2)


@pytest.mark.parametrize("generator", GENERATORS)
def test_error_hook_stdin_read_is_bounded_when_writer_stays_open(tmp_path, generator):
    _, script_path = _write_hook(tmp_path, generator, git_enabled=False)
    process = subprocess.Popen(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    started = time.monotonic()
    try:
        returncode = process.wait(timeout=5)
        elapsed = time.monotonic() - started
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if process.stdin is not None:
            process.stdin.close()

    assert returncode == 0, stderr
    assert stdout.strip() == ""
    assert elapsed < 4.75
    assert "Hook input read timed out after 1000 ms" in stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process-tree test")
@pytest.mark.parametrize("generator", GENERATORS)
def test_slow_git_command_kills_nested_child_and_preserves_recovery_protocol(
    tmp_path,
    generator,
):
    temp_outputs_before = _api_switcher_temp_outputs()
    fake_bin = tmp_path / "fake-bin"
    project_dir = tmp_path / "project"
    fake_bin.mkdir()
    project_dir.mkdir()
    (project_dir / ".git").mkdir()
    process_probe_dir = Path(tempfile.mkdtemp(prefix="api_switcher_nested_child_"))
    nested_pid_path = process_probe_dir / "nested-child.pid"
    nested_script_path = process_probe_dir / "sleep_child.py"
    python_path = str(Path(sys.executable)).replace('"', '""')
    nested_script_path.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(__file__).with_name('nested-child.pid').write_text(\n"
        "    str(os.getpid()),\n"
        "    encoding='ascii',\n"
        ")\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    (fake_bin / "git.cmd").write_text(
        "@echo off\n"
        "chcp 65001 >nul\n"
        'if /I "%~1"=="rev-parse" (\n'
        "  echo .git\n"
        "  exit /b 0\n"
        ")\n"
        'if /I "%~1"=="status" (\n'
        f'  "{python_path}" "{nested_script_path}"\n'
        "  exit /b 0\n"
        ")\n"
        "exit /b 0\n",
        encoding="utf-8",
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _, script_path = _write_hook(config_dir, generator, git_enabled=True)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    started = time.monotonic()
    codex_command = (
        f'cmd.exe /D /S /C ""{_powershell()}" '
        f'-NoProfile -ExecutionPolicy Bypass -File "{script_path}""'
    )
    nested_pid = None
    nested_exited = False
    try:
        result = subprocess.run(
            codex_command,
            input=json.dumps(_error_payload(f"slow-git-{generator.__name__}")),
            cwd=project_dir,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        assert nested_pid_path.exists(), result.stderr
        nested_pid = int(nested_pid_path.read_text(encoding="ascii").strip())
        nested_exited = _wait_for_process_exit(nested_pid)
    finally:
        if nested_pid is None and nested_pid_path.exists():
            nested_pid = int(nested_pid_path.read_text(encoding="ascii").strip())
        if nested_pid is not None and _process_is_running(nested_pid):
            subprocess.run(
                ["taskkill.exe", "/PID", str(nested_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        shutil.rmtree(process_probe_dir, ignore_errors=True)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    # Git is capped at 5 s; bounded taskkill and root-exit verification can
    # legitimately extend total hook runtime under system load.
    assert elapsed < 12
    assert "Git snapshot timed out after 5 seconds" in result.stderr
    assert "Git process-tree cleanup was not confirmed" not in result.stderr
    assert nested_exited, f"nested child PID {nested_pid} survived the Git timeout"
    assert not _wait_for_no_new_temp_outputs(temp_outputs_before)

    output = json.loads(result.stdout)
    assert output["decision"] == "recover"
    assert output["recover"] is True
    assert output["commands"][0] == {
        "type": "slash_command",
        "command": "compact",
    }
