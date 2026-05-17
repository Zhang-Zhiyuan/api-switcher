"""
测试错误恢复功能
验证所有组件是否正常工作
"""
import json
import shutil
import sys
import pytest
from core.auto_continue.error_parser import error_parser, ErrorType, RecoveryStrategy
from core.auto_continue.error_analyzer import get_analyzer
from core.auto_continue.manager import auto_continue_manager

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def test_error_parser():
    """测试错误解析器"""
    print("=" * 80)
    print("测试错误解析器")
    print("=" * 80)

    test_cases = [
        {
            "name": "内容超长错误",
            "data": {
                "error_code": "CONTENT_LENGTH_EXCEEDS_THRESHOLD",
                "error_message": "对话内容超出长度限制",
                "status": 400
            },
            "expected_type": ErrorType.CONTENT_LENGTH_EXCEEDED,
            "expected_strategy": RecoveryStrategy.COMPACT_AND_CONTINUE
        },
        {
            "name": "context window limit 错误",
            "data": {
                "error_message": "API Error: The model has reached its context window limit.",
                "status": 400
            },
            "expected_type": ErrorType.CONTENT_LENGTH_EXCEEDED,
            "expected_strategy": RecoveryStrategy.COMPACT_AND_CONTINUE
        },
        {
            "name": "wrapped context window limit 错误",
            "data": {
                "response": {
                    "data": {
                        "error": {
                            "message": "API Error: The model has reached its context window limit."
                        }
                    }
                },
                "status": 400
            },
            "expected_type": ErrorType.CONTENT_LENGTH_EXCEEDED,
            "expected_strategy": RecoveryStrategy.COMPACT_AND_CONTINUE
        },
        {
            "name": "prompt too long 错误",
            "data": {
                "error": {
                    "message": "Prompt is too long for this model."
                },
                "status": 400
            },
            "expected_type": ErrorType.CONTENT_LENGTH_EXCEEDED,
            "expected_strategy": RecoveryStrategy.COMPACT_AND_CONTINUE
        },
        {
            "name": "速率限制错误",
            "data": {
                "error_message": "rate limit exceeded, please retry after 60 seconds",
                "status": 429
            },
            "expected_type": ErrorType.RATE_LIMIT_EXCEEDED,
            "expected_strategy": RecoveryStrategy.WAIT_AND_RETRY
        },
        {
            "name": "认证错误",
            "data": {
                "error_code": "invalid_api_key",
                "error_message": "Authentication failed",
                "status": 401
            },
            "expected_type": ErrorType.AUTHENTICATION_ERROR,
            "expected_strategy": RecoveryStrategy.NOTIFY_USER
        },
        {
            "name": "模型过载",
            "data": {
                "error_message": "model overloaded, please try again later",
                "status": 503
            },
            "expected_type": ErrorType.MODEL_OVERLOADED,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "超时错误",
            "data": {
                "error_message": "request timed out",
                "status": 504
            },
            "expected_type": ErrorType.TIMEOUT_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "Codex compact stream 断开错误",
            "data": {
                "error_message": (
                    "Error running remote compact task: stream disconnected before completion: "
                    "error sending request for url "
                    "(https://chatgpt.com/backend-api/codex/responses/compact)"
                )
            },
            "expected_type": ErrorType.NETWORK_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "Codex compact upstream 503 reset",
            "data": {
                "error_message": (
                    "Error running remote compact task: unexpected status 503 Service Unavailable: "
                    "upstream connect error or disconnect/reset before headers. "
                    "reset reason: connection termination, url: "
                    "https://chatgpt.com/backend-api/codex/responses/compact, "
                    "cf-ray: 9fb0944f59b1269d-NRT"
                ),
                "status": 503
            },
            "expected_type": ErrorType.NETWORK_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "Codex responses reconnect exhausted",
            "data": {
                "error_message": (
                    "Reconnecting... 1/5\nReconnecting... 5/5\n"
                    "stream disconnected before completion: error sending request for url "
                    "(https://layer4.cc/v1/responses)"
                )
            },
            "expected_type": ErrorType.NETWORK_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "配额超限",
            "data": {
                "error_message": "quota exceeded, insufficient balance",
                "status": 402
            },
            "expected_type": ErrorType.QUOTA_EXCEEDED,
            "expected_strategy": RecoveryStrategy.NOTIFY_USER
        }
    ]

    passed = 0
    failed = 0

    for test in test_cases:
        print(f"\n测试: {test['name']}")
        parsed = error_parser.parse(test['data'])

        print(f"  错误类型: {parsed.error_type.value}")
        print(f"  恢复策略: {parsed.recovery_strategy.value}")
        print(f"  用户消息: {parsed.user_message}")

        if parsed.error_type == test['expected_type'] and \
           parsed.recovery_strategy == test['expected_strategy']:
            print("  ✓ 通过")
            passed += 1
        else:
            print("  ✗ 失败")
            print(f"    期望类型: {test['expected_type'].value}")
            print(f"    期望策略: {test['expected_strategy'].value}")
            failed += 1

    print(f"\n总结: {passed} 通过, {failed} 失败")
    assert failed == 0


def test_retry_after_extraction():
    """测试重试时间提取"""
    print("\n" + "=" * 80)
    print("测试重试时间提取")
    print("=" * 80)

    test_cases = [
        ("请在 60 秒后重试", 60),
        ("retry after 30 seconds", 30),
        ("wait 120 seconds before retrying", 120),
        ("no time specified", None)
    ]

    passed = 0
    failed = 0

    for message, expected in test_cases:
        data = {"error_message": message}
        parsed = error_parser.parse(data)

        print(f"\n消息: {message}")
        print(f"  提取时间: {parsed.retry_after}")

        if parsed.retry_after == expected:
            print("  ✓ 通过")
            passed += 1
        else:
            print(f"  ✗ 失败 (期望: {expected})")
            failed += 1

    print(f"\n总结: {passed} 通过, {failed} 失败")
    assert failed == 0


def test_provider_status():
    """测试 Provider 状态"""
    print("\n" + "=" * 80)
    print("测试 Provider 状态")
    print("=" * 80)

    for provider_name in ["claude", "codex"]:
        print(f"\n{provider_name.upper()} Provider:")
        try:
            status = auto_continue_manager.get_status(provider_name)
            print(f"  Hook 脚本存在: {status.hook_script_exists}")
            print(f"  Hook 已注册: {status.hook_registered}")
            print(f"  错误恢复已安装: {status.error_recovery_installed}")
            print(f"  已启用: {status.enabled}")

            settings = auto_continue_manager.get_settings(provider_name)
            if settings:
                print(f"  错误恢复已启用: {settings.error_recovery_enabled}")
                print(f"  最大恢复次数: {settings.max_error_recoveries}")
        except Exception as e:
            print(f"  错误: {e}")


def test_error_analyzer():
    """测试错误分析器"""
    print("\n" + "=" * 80)
    print("测试错误分析器")
    print("=" * 80)

    for provider_name in ["claude", "codex"]:
        print(f"\n{provider_name.upper()} 错误日志:")
        try:
            analyzer = get_analyzer(provider_name)
            stats = analyzer.analyze(days=7)

            print(f"  总错误数: {stats.total_errors}")
            print(f"  成功恢复数: {stats.total_recoveries}")
            print(f"  恢复成功率: {stats.recovery_success_rate:.1f}%")
            print(f"  平均恢复次数: {stats.avg_recovery_count:.1f}")

            if stats.errors_by_type:
                print("  错误类型分布:")
                for error_type, count in sorted(stats.errors_by_type.items(),
                                               key=lambda x: x[1], reverse=True):
                    print(f"    - {error_type}: {count}")
            else:
                print("  暂无错误记录")

        except Exception as e:
            print(f"  错误: {e}")


def test_script_generation():
    """测试脚本生成"""
    print("\n" + "=" * 80)
    print("测试脚本生成")
    print("=" * 80)

    from core.auto_continue.error_recovery_script import (
        generate_error_recovery_script,
        generate_codex_error_recovery_script
    )

    settings_path = "C:\\Users\\Test\\.claude\\auto_continue_settings.json"

    print("\n生成 Claude Code 错误恢复脚本...")
    claude_script = generate_error_recovery_script(settings_path)
    print(f"  脚本长度: {len(claude_script)} 字符")
    print(f"  包含错误类型枚举: {'ErrorTypes' in claude_script}")
    print(f"  包含恢复策略: {'RecoveryStrategies' in claude_script}")
    print(f"  包含压缩命令: {'compact' in claude_script}")
    assert "context.*window.*(limit|full|exceed|overflow)" in claude_script
    assert "model.*reached.*context.*window.*limit" in claude_script
    assert "prompt|request|messages?" in claude_script
    assert "Get-FirstTextField" in claude_script
    assert '"message", "error", "errorMessage"' in claude_script
    assert '"body", "data", "errors"' in claude_script
    assert "stream.*disconnect" in claude_script
    assert "reconnecting\\.\\.\\.\\s*\\d+/\\d+" in claude_script
    assert "upstream connect error" in claude_script
    assert "disconnect/reset before headers" in claude_script
    assert "connection termination" in claude_script
    assert "backend-api/codex/responses/compact" in claude_script
    assert "压缩任务连接中断" in claude_script
    assert "git config user.email" in claude_script
    assert "Ensure-LocalGitIgnore" in claude_script
    assert "$initializedRepo = $false" in claude_script
    assert "if ($initializedRepo)" in claude_script
    assert "node_modules/" in claude_script
    assert ".env.*" in claude_script

    print("\n生成 Codex CLI 错误恢复脚本...")
    codex_script = generate_codex_error_recovery_script(settings_path)
    print(f"  脚本长度: {len(codex_script)} 字符")
    print(f"  包含错误分类: {'Get-ErrorType' in codex_script}")
    print(f"  包含压缩命令: {'compress' in codex_script}")
    assert "context.*window.*(limit|full|exceed|overflow)" in codex_script
    assert "model.*reached.*context.*window.*limit" in codex_script
    assert "prompt|request|messages?" in codex_script
    assert "Get-FirstTextField" in codex_script
    assert '"message", "error", "errorMessage"' in codex_script
    assert '"body", "data", "errors"' in codex_script
    assert 'return "network"' in codex_script
    assert '"timeout", "overload", "network"' in codex_script
    assert "stream.*disconnect" in codex_script
    assert "reconnecting\\.\\.\\.\\s*\\d+/\\d+" in codex_script
    assert "upstream connect error" in codex_script
    assert "disconnect/reset before headers" in codex_script
    assert "connection termination" in codex_script
    assert "backend-api/codex/responses/compact" in codex_script
    assert 'commands = @("/compress", "继续")' in codex_script
    assert "压缩任务连接中断" in codex_script
    assert "git config user.email" in codex_script
    assert "Ensure-LocalGitIgnore" in codex_script
    assert "$initializedRepo = $false" in codex_script
    assert "if ($initializedRepo)" in codex_script
    assert "node_modules/" in codex_script
    assert ".env.*" in codex_script


def test_stop_hook_scripts_treat_compact_stream_disconnect_as_recoverable():
    from core import remote_auto_continue
    from core.auto_continue.script_generator import generate_hook_script

    local_script = generate_hook_script("C:\\Users\\Test\\.codex\\auto_continue_settings.json")
    remote_script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )

    for script in [local_script, remote_script]:
        assert "recoverable_api_error_detected" in script
        assert "PermissionRequest" in script
        assert "PreToolUse" in script
        assert "Bash" in script
        assert '"allow"' in script
        assert "stream disconnected before completion" in script
        assert "reconnecting\\.\\.\\.\\s*\\d+/\\d+" in script
        assert "upstream connect error" in script
        assert "disconnect/reset before headers" in script
        assert "connection termination" in script
        assert "api error:.*context.*window.*limit" in script
        assert "model.*reached.*context.*window.*limit" in script
        assert "backend-api/codex/responses/compact" in script

    assert '$toolName -ieq "Bash"' not in local_script
    assert "tool_name.lower() == \"bash\"" not in remote_script
    assert '["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]' in remote_script
    assert "git config user.email" in local_script
    assert "Ensure-LocalGitIgnore" in local_script
    assert "$initializedRepo = $false" in local_script
    assert "if ($initializedRepo)" in local_script
    assert '$hookEvent -ne "PermissionRequest" -and $hookEvent -ne "PreToolUse"' in local_script
    assert 'permissionDecision = "allow"' in local_script
    assert "auto_continue_permission_state.json" in local_script
    assert "node_modules/" in local_script
    assert ".env.*" in local_script
    assert '["git", "config", "user.email"]' in remote_script
    assert "DEFAULT_GITIGNORE_LINES" in remote_script
    assert "initialized_repo = False" in remote_script
    assert "if initialized_repo:" in remote_script
    assert 'if hook_event not in {"PermissionRequest", "PreToolUse"} and git_snapshot_enabled:' in remote_script
    assert '"permissionDecision": "allow"' in remote_script
    assert "auto_continue_permission_state.json" in remote_script
    assert "node_modules/" in remote_script
    assert ".env.*" in remote_script
    assert "[System.IO.FileMode]::CreateNew" in local_script
    assert "New-Item -Path $lockPath -ItemType File -Force" not in local_script
    assert "os.O_CREAT | os.O_EXCL | os.O_WRONLY" in remote_script
    assert "def replace_file(source, target):" in remote_script
    assert "def write_text_atomic(path, content):" in remote_script
    assert "write_text_atomic(path, json.dumps(data" in remote_script
    assert 'write_text_atomic(path, "\\n".join(DEFAULT_GITIGNORE_LINES) + "\\n")' in remote_script


def test_remote_stop_hook_treats_context_window_api_error_as_recoverable(tmp_path):
    import subprocess

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings_path.write_text(
        json.dumps({
            "enabled": True,
            "git_auto_snapshot": False,
            "max_continuations": 2,
            "blocker_patterns": ["api error"],
            "incomplete_patterns": [],
            "continuation_prompt": "compact and continue",
        }),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps({
            "session_id": "session-context-window",
            "last_message": "API Error: The model has reached its context window limit.",
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", body, str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output == {"continue": True, "message": "compact and continue"}
    log_path = state_dir / "auto_continue_stop_log.jsonl"
    assert "recoverable_api_error_detected" in log_path.read_text(encoding="utf-8")


def test_auto_continue_settings_permission_auto_approve_validation():
    from models.auto_continue import AutoContinueSettings

    assert AutoContinueSettings().auto_approve_max_per_session == 0

    settings = AutoContinueSettings(
        auto_approve_permission_requests=True,
        auto_approve_max_per_session=0,
        auto_approve_bash=False,
        auto_approve_tools=["Edit", "MultiEdit", "Write"],
    )

    ok, error = settings.validate()
    assert ok, error

    restored = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_max_per_session": 5,
        "auto_approve_tools": ["Edit", "edit", "Write"],
    })

    assert restored.auto_approve_permission_requests is True
    assert restored.auto_approve_max_per_session == 5
    assert restored.auto_approve_bash is True
    assert restored.auto_approve_tools == ["Bash", "Edit", "Write"]

    legacy_disabled = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_bash": False,
        "auto_approve_tools": ["Edit", "Write"],
    })
    assert legacy_disabled.auto_approve_tools == ["Edit", "Write"]

    legacy_disabled_without_tools = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_bash": False,
    })
    assert "Bash" not in legacy_disabled_without_tools.auto_approve_tools
    assert "Edit" in legacy_disabled_without_tools.auto_approve_tools

    explicit_empty = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_bash": False,
        "auto_approve_tools": [],
    })
    assert explicit_empty.auto_approve_tools == []

    explicit_empty_legacy_true = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_bash": True,
        "auto_approve_tools": [],
    })
    assert explicit_empty_legacy_true.auto_approve_tools == []


def test_permission_auto_approve_treats_bash_as_regular_tool():
    from core import remote_auto_continue
    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings = AutoContinueSettings()
    assert settings.auto_approve_tools[0] == "Bash"

    local_script = generate_hook_script("C:\\Users\\Test\\.claude\\auto_continue_settings.json")
    remote_script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )
    assert '@("Bash", "Edit", "MultiEdit", "Write", "NotebookEdit")' in local_script
    assert '["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]' in remote_script
    assert '$null -eq $settings.PSObject.Properties["auto_approve_tools"]' in local_script
    assert '$legacyBashAllowed -and $allowedTools.Count -gt 0' in local_script
    assert "$autoApproveMax = 0" in local_script
    assert 'updatedPermissions = @(' in local_script
    assert 'destination = "session"' in local_script
    assert 'if "auto_approve_tools" in settings:' in remote_script
    assert "tools = allowed_tools if isinstance(allowed_tools, list) else []" in remote_script
    assert 'if legacy_bash_allowed and "auto_approve_tools" in settings and result' in remote_script
    assert '"updatedPermissions": [' in remote_script
    assert '"destination": "session"' in remote_script
    assert "else DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS" not in remote_script
    assert "$toolName -ieq \"Bash\"" not in local_script
    assert 'tool_name.lower() == "bash"' not in remote_script


def test_remote_permission_hook_respects_explicit_empty_tools(tmp_path):
    import subprocess

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    def run_hook(settings: dict, tool_name: str, input_extra: dict | None = None, event_name: str | None = "PermissionRequest"):
        settings_path = tmp_path / f"settings_{tool_name}_{len(list(tmp_path.iterdir()))}.json"
        input_path = tmp_path / f"input_{tool_name}_{len(list(tmp_path.iterdir()))}.json"
        state_dir = tmp_path / "state"
        settings = {
            "enabled": False,
            "git_auto_snapshot": False,
            "auto_approve_permission_requests": True,
            **settings,
        }
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        payload = {
            "session_id": f"session-{tool_name}-{len(list(tmp_path.iterdir()))}",
            "tool_name": tool_name,
        }
        if event_name is not None:
            payload["hook_event_name"] = event_name
        if input_extra:
            payload.update(input_extra)
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-c", body, str(settings_path), str(state_dir), str(input_path)],
            cwd=tmp_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    empty_denied = run_hook({"auto_approve_tools": [], "auto_approve_bash": False}, "Edit")
    assert empty_denied.returncode == 0
    assert empty_denied.stdout.strip() == ""

    bash_disabled = run_hook({"auto_approve_bash": False}, "Bash")
    assert bash_disabled.returncode == 0
    assert bash_disabled.stdout.strip() == ""

    edit_allowed = run_hook({"auto_approve_bash": False}, "Edit")
    assert edit_allowed.returncode == 0
    assert '"behavior": "allow"' in edit_allowed.stdout
    edit_output = json.loads(edit_allowed.stdout)
    edit_decision = edit_output["hookSpecificOutput"]["decision"]
    assert edit_decision["updatedPermissions"][0]["destination"] == "session"
    assert edit_decision["updatedPermissions"][0]["rules"] == [{"toolName": "Edit"}]

    default_allowed = run_hook({}, "Bash")
    assert default_allowed.returncode == 0
    assert '"behavior": "allow"' in default_allowed.stdout

    nested_allowed = run_hook(
        {"auto_approve_tools": ["Bash(git status:*)"], "auto_approve_bash": False},
        "",
        {"tool_name": "", "permissionRequest": {"toolName": "Bash"}},
    )
    assert nested_allowed.returncode == 0
    assert '"behavior": "allow"' in nested_allowed.stdout

    pre_tool_allowed = run_hook({}, "Bash", event_name="PreToolUse")
    assert pre_tool_allowed.returncode == 0
    pre_tool_output = json.loads(pre_tool_allowed.stdout)
    pre_tool_specific = pre_tool_output["hookSpecificOutput"]
    assert pre_tool_specific["hookEventName"] == "PreToolUse"
    assert pre_tool_specific["permissionDecision"] == "allow"

    pre_tool_camel_allowed = run_hook({}, "Bash", {"hookEventName": "PreToolUse", "toolName": "Bash"}, event_name=None)
    assert pre_tool_camel_allowed.returncode == 0
    pre_tool_camel_output = json.loads(pre_tool_camel_allowed.stdout)
    assert pre_tool_camel_output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert pre_tool_camel_output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_remote_permission_hook_skips_git_snapshot_for_fast_approval(tmp_path, monkeypatch):
    import os
    import stat
    import subprocess

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    marker = tmp_path / "git_was_called"
    fake_git = tmp_path / ("git.cmd" if os.name == "nt" else "git")
    fake_git.write_text(
        f"@echo off\r\necho called>{marker}\r\nexit /b 0\r\n"
        if os.name == "nt"
        else f"#!/bin/sh\necho called > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings_path.write_text(
        json.dumps({
            "enabled": False,
            "git_auto_snapshot": True,
            "git_snapshot_on_start": True,
            "auto_approve_permission_requests": True,
            "auto_approve_tools": ["Bash"],
        }),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps({
            "hook_event_name": "PreToolUse",
            "session_id": "session-permission-fast",
            "tool_name": "Bash",
        }),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [sys.executable, "-c", body, str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert '"permissionDecision": "allow"' in result.stdout
    assert not marker.exists()
    assert not (state_dir / "auto_continue_permission_state.json").exists()
    assert not (state_dir / "auto_continue_stop_state.json").exists()


def test_local_permission_hook_outputs_structured_updated_permissions(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings_path.write_text(
        json.dumps({
            "enabled": False,
            "git_auto_snapshot": True,
            "git_snapshot_on_start": True,
            "auto_approve_permission_requests": True,
            "auto_approve_max_per_session": 0,
            "auto_approve_tools": ["Bash"],
        }),
        encoding="utf-8",
    )
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    def run_hook(event_name: str, camel_case: bool = False):
        payload = {
            "permissionRequest": {"toolName": "Bash"},
        }
        if camel_case:
            payload.update({
                "hookEventName": event_name,
                "sessionId": "session-local-fast",
            })
        else:
            payload.update({
                "hook_event_name": event_name,
                "session_id": "session-local-fast",
            })
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            input=json.dumps(payload),
            cwd=tmp_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    result = run_hook("PermissionRequest")

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    decision = output["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedPermissions"][0]["rules"] == [{"toolName": "Bash"}]
    assert not (tmp_path / "auto_continue_permission_state.json").exists()
    assert not (tmp_path / "auto_continue_stop_state.json").exists()

    pre_tool_result = run_hook("PreToolUse")
    assert pre_tool_result.returncode == 0, pre_tool_result.stderr
    pre_tool_output = json.loads(pre_tool_result.stdout)
    pre_tool_specific = pre_tool_output["hookSpecificOutput"]
    assert pre_tool_specific["hookEventName"] == "PreToolUse"
    assert pre_tool_specific["permissionDecision"] == "allow"

    pre_tool_camel_result = run_hook("PreToolUse", camel_case=True)
    assert pre_tool_camel_result.returncode == 0, pre_tool_camel_result.stderr
    pre_tool_camel_output = json.loads(pre_tool_camel_result.stdout)
    pre_tool_camel_specific = pre_tool_camel_output["hookSpecificOutput"]
    assert pre_tool_camel_specific["hookEventName"] == "PreToolUse"
    assert pre_tool_camel_specific["permissionDecision"] == "allow"


def test_claude_permission_request_hook_can_be_registered(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from models.auto_continue import AutoContinueSettings

    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    settings = AutoContinueSettings(auto_approve_permission_requests=True)

    provider.save_settings(settings)
    provider.install_hook_script()
    provider.register_hook_for_settings(settings)

    hooks = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert "PermissionRequest" in hooks
    assert "PreToolUse" in hooks
    commands = [
        hook["command"]
        for group in hooks["PermissionRequest"]
        for hook in group.get("hooks", [])
    ]
    assert any("auto_continue_stop.ps1" in command for command in commands)
    pre_tool_commands = [
        hook["command"]
        for group in hooks["PreToolUse"]
        for hook in group.get("hooks", [])
    ]
    assert any("auto_continue_stop.ps1" in command for command in pre_tool_commands)


def test_claude_auto_approve_preseeds_permission_allow_rules(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from models.auto_continue import AutoContinueSettings

    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({
            "permissions": {
                "allow": ["Read(/tmp/**)", "Edit"],
                "ask": ["Read", "Bash", "Write"],
            },
        }),
        encoding="utf-8",
    )

    provider.register_hook(
        settings=AutoContinueSettings(
            auto_approve_permission_requests=True,
            auto_approve_tools=["Bash", "Edit", "Write"],
        )
    )

    claude_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    allow_rules = claude_settings["permissions"]["allow"]
    assert allow_rules == ["Read(/tmp/**)", "Edit", "Bash", "Write"]
    assert claude_settings["permissions"]["ask"] == ["Read"]

    state = json.loads((tmp_path / "auto_continue_permission_rules.json").read_text(encoding="utf-8"))
    assert state["rules"] == ["Bash", "Write"]
    assert state["ask_rules"] == ["Bash", "Write"]

    provider.unregister_hook()

    claude_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert claude_settings["permissions"]["allow"] == ["Read(/tmp/**)", "Edit"]
    assert claude_settings["permissions"]["ask"] == ["Read", "Bash", "Write"]
    assert not (tmp_path / "auto_continue_permission_rules.json").exists()


def test_claude_unregister_cleans_permission_sidecar_without_settings(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider

    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    state_path = tmp_path / "auto_continue_permission_rules.json"
    state_path.write_text(json.dumps({"rules": ["Bash"], "ask_rules": ["Bash"]}), encoding="utf-8")

    provider.unregister_hook()

    assert not state_path.exists()


def test_auto_continue_manager_enable_uses_provider_enable_with_guidance(monkeypatch):
    from core.auto_continue.manager import AutoContinueManager
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider:
        def enable(self, settings):
            calls.append(("enable", settings.enabled, settings.apply_to_subagents))

        def install_guidance(self):
            calls.append(("guidance",))

    manager = AutoContinueManager()
    fake_provider = FakeProvider()
    monkeypatch.setattr(manager, "get_provider", lambda _name: fake_provider)

    settings = AutoContinueSettings()
    manager.enable("claude", settings, apply_to_subagents=True)

    assert calls == [("enable", True, True), ("guidance",)]


def test_permission_rule_helpers_detect_ask_conflicts_and_broad_allows():
    from core.auto_continue.permission_rules import (
        conflicting_permission_rules,
        missing_allow_rules,
    )

    assert missing_allow_rules(["Bash(git status:*)"], ["Bash"]) == []
    assert missing_allow_rules(["Bash"], ["Bash(git status:*)"]) == ["Bash"]
    assert conflicting_permission_rules(["Bash"], ["Bash(git push:*)", "Edit"]) == ["Bash(git push:*)"]
    assert conflicting_permission_rules(["Bash(git status:*)"], ["Bash"]) == ["Bash"]


def test_local_codex_hooks_preserve_existing_entries(tmp_path, monkeypatch):
    """Codex hooks.json install/uninstall should only replace API Switcher hooks."""
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(json.dumps({
        "Stop": {
            "command": "powershell.exe -File user_stop.ps1",
            "timeout": 4,
        },
        "Error": {
            "hooks": [
                {
                    "command": "powershell.exe -File user_error.ps1",
                    "timeout": 5,
                }
            ]
        },
    }), encoding="utf-8")

    provider = CodexProvider()
    provider.register_hook()
    provider._register_error_recovery_hook()

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = [hook["command"] for hook in hooks["Stop"]["hooks"]]
    error_commands = [hook["command"] for hook in hooks["Error"]["hooks"]]
    assert "powershell.exe -File user_stop.ps1" in stop_commands
    assert any("auto_continue_stop.ps1" in command for command in stop_commands)
    assert "powershell.exe -File user_error.ps1" in error_commands
    assert any("error_recovery.ps1" in command for command in error_commands)
    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed() is False
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is True

    provider.get_error_recovery_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")
    assert provider.is_error_recovery_installed()

    provider.unregister_hook()
    provider.uninstall_error_recovery()

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert hooks["Stop"]["command"] == "powershell.exe -File user_stop.ps1"
    assert hooks["Error"]["command"] == "powershell.exe -File user_error.ps1"
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is True


def test_local_codex_hooks_toggle_config_when_no_hooks_remain(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    provider.register_hook()

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is True
    provider.unregister_hook()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is False

    provider._register_error_recovery_hook()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is True
    provider.get_error_recovery_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")
    provider.uninstall_error_recovery()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["codex_hooks"] is False


def test_load_settings_migrates_chinese_incomplete_patterns(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider
    from models.auto_continue import DEFAULT_INCOMPLETE_PATTERNS

    old_patterns = [
        r"(?i)(still|remaining|todo|wip|work in progress|not (yet )?complete)",
        r"(?i)(will|need to|should|must).{0,50}(implement|add|create|fix|test|verify)",
        r"(?i)(next|following) steps?:",
        r"(?i)to be (done|completed|implemented)",
    ]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    settings_path = tmp_path / "auto_continue_settings.json"
    settings_path.write_text(
        json.dumps({
            "enabled": True,
            "max_continuations": 3,
            "continuation_prompt": "continue",
            "incomplete_patterns": old_patterns,
        }),
        encoding="utf-8",
    )

    settings = CodexProvider().load_settings()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    raw_saved = settings_path.read_text(encoding="utf-8")

    assert settings is not None
    assert "下一步" in raw_saved
    for pattern in DEFAULT_INCOMPLETE_PATTERNS:
        assert pattern in settings.incomplete_patterns
        assert pattern in saved["incomplete_patterns"]


def main():
    """运行所有测试"""
    print("API 错误恢复功能测试")
    print("=" * 80)

    checks = [
        ("错误解析器", test_error_parser),
        ("重试时间提取", test_retry_after_extraction),
        ("Provider 状态", test_provider_status),
        ("错误分析器", test_error_analyzer),
        ("脚本生成", test_script_generation),
    ]
    results = []

    # 运行测试
    for name, check in checks:
        try:
            check()
            results.append((name, True))
        except Exception as e:
            print(f"{name} 失败: {e}")
            results.append((name, False))

    # 总结
    print("\n" + "=" * 80)
    print("测试总结")
    print("=" * 80)

    for name, passed in results:
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name}: {status}")

    all_passed = all(result[1] for result in results)
    print("\n" + ("=" * 80))
    if all_passed:
        print("所有测试通过！✓")
    else:
        print("部分测试失败 ✗")
    print("=" * 80)


if __name__ == "__main__":
    main()
