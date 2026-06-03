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


def _remote_hook_python_path(tmp_path, body: str):
    script_path = tmp_path / "remote_hook_body.py"
    script_path.write_text(body, encoding="utf-8")
    return script_path


def _write_stale_lock(path):
    import os
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale", encoding="utf-8")
    old_time = time.time() - 120
    os.utime(path, (old_time, old_time))


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
            "name": "上游 JSON 内容长度超限错误",
            "data": {
                "error_message": (
                    'API Error: 400 {"code":"CONTENT_LENGTH_EXCEEDS_THRESHOLD",'
                    '"error":"对话内容超出长度限制啦",'
                    '"hint":"老板您好～当前会话积累的内容太长了，'
                    '已超出上游服务的处理能力。建议您新建一个会话或者使用/compact命令压缩再继续。"}'
                ),
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
                    "(https://openai.cc/v1/responses)"
                )
            },
            "expected_type": ErrorType.NETWORK_ERROR,
            "expected_strategy": RecoveryStrategy.RETRY_WITH_BACKOFF
        },
        {
            "name": "Codex official responses reconnect exhausted",
            "data": {
                "error_message": (
                    "Reconnecting... 2/5\nReconnecting... 3/5\nReconnecting... 4/5\n"
                    "Reconnecting... 5/5\nReconnecting... 1/5\nReconnecting... 2/5\n"
                    "Reconnecting... 3/5\nReconnecting... 4/5\nReconnecting... 5/5\n"
                    "stream disconnected before completion: error sending request for url "
                    "(https://chatgpt.com/backend-api/codex/responses)"
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
        ("retry after 2 minutes", 120),
        ("retry after 1500ms", 2),
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
    assert error_parser.parse({"headers": {"Retry-After": "2 minutes"}}).retry_after == 120
    assert error_parser.parse({"error": {"headers": {"retry-after": "1500ms"}}}).retry_after == 2


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
    assert "content_length_exceeds_threshold" in claude_script
    assert "内容.*超出.*长度" in claude_script
    assert "上游服务.*处理能力" in claude_script
    assert "prompt|request|messages?" in claude_script
    assert "Get-FirstTextField" in claude_script
    assert "Get-BoolSetting" in claude_script
    assert '"message", "error", "errorMessage"' in claude_script
    assert '"body", "data", "errors"' in claude_script
    assert "stream.*disconnect" in claude_script
    assert "reconnecting\\.\\.\\.\\s*\\d+/\\d+" in claude_script
    assert "upstream connect error" in claude_script
    assert "disconnect/reset before headers" in claude_script
    assert "connection termination" in claude_script
    assert "backend-api/codex/responses/compact" in claude_script
    assert "压缩任务连接中断" in claude_script
    assert "error_retry_initial_delay_seconds" in claude_script
    assert "error_retry_max_delay_seconds" in claude_script
    assert "Get-BackoffSeconds" in claude_script
    assert "Get-ClampedSeconds" in claude_script
    assert "Retry-After" in claude_script
    assert "Acquire-StateLock" in claude_script
    assert "Release-StateLock" in claude_script
    assert "Save-RecoveryState" in claude_script
    assert '"$statePath.lock"' in claude_script
    assert 'Join-Path $configDir "tmp"' in claude_script
    assert "git config user.email" in claude_script
    assert "Ensure-LocalGitIgnore" in claude_script
    assert "Get-Command git" in claude_script
    assert "$initializedRepo = $false" in claude_script
    assert "if ($initializedRepo)" in claude_script
    assert "\n        git add -A 2>&1 | Out-Null\n" in claude_script
    assert "git commit --no-verify" in claude_script
    assert "node_modules/" in claude_script
    assert ".env.*" in claude_script

    print("\n生成 Codex CLI 错误恢复脚本...")
    codex_script = generate_codex_error_recovery_script(settings_path)
    print(f"  脚本长度: {len(codex_script)} 字符")
    print(f"  包含错误分类: {'Get-ErrorType' in codex_script}")
    print(f"  包含压缩命令: {'compact' in codex_script}")
    assert "context.*window.*(limit|full|exceed|overflow)" in codex_script
    assert "model.*reached.*context.*window.*limit" in codex_script
    assert "content_length_exceeds_threshold" in codex_script
    assert "内容.*超出.*长度" in codex_script
    assert "上游服务.*处理能力" in codex_script
    assert "prompt|request|messages?" in codex_script
    assert "Get-FirstTextField" in codex_script
    assert "Get-BoolSetting" in codex_script
    assert '"message", "error", "errorMessage"' in codex_script
    assert '"body", "data", "errors"' in codex_script
    assert 'return "network"' in codex_script
    assert '"timeout", "overload", "network", "server"' in codex_script
    assert "stream.*disconnect" in codex_script
    assert "reconnecting\\.\\.\\.\\s*\\d+/\\d+" in codex_script
    assert "upstream connect error" in codex_script
    assert "disconnect/reset before headers" in codex_script
    assert "connection termination" in codex_script
    assert "backend-api/codex/responses/compact" in codex_script
    assert 'command = "compact"' in codex_script
    assert "压缩任务连接中断" in codex_script
    assert "error_retry_initial_delay_seconds" in codex_script
    assert "error_retry_max_delay_seconds" in codex_script
    assert "Get-BackoffSeconds" in codex_script
    assert "Get-ClampedSeconds" in codex_script
    assert "Retry-After" in codex_script
    assert "Acquire-StateLock" in codex_script
    assert "Release-StateLock" in codex_script
    assert "Save-RecoveryState" in codex_script
    assert '"$statePath.lock"' in codex_script
    assert 'Join-Path $configDir "tmp"' in codex_script
    assert "HttpStatus" in codex_script
    assert "git config user.email" in codex_script
    assert "Ensure-LocalGitIgnore" in codex_script
    assert "Get-Command git" in codex_script
    assert "$initializedRepo = $false" in codex_script
    assert "if ($initializedRepo)" in codex_script
    assert "\n        git add -A 2>&1 | Out-Null\n" in codex_script
    assert "git commit --no-verify" in codex_script
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
        assert "content_length_exceeds_threshold" in script
        assert "对话内容超出长度限制" in script
        assert "上游服务.*处理能力" in script
        assert "backend-api/codex/responses/compact" in script

    assert '$toolName -ieq "Bash"' not in local_script
    assert "tool_name.lower() == \"bash\"" not in remote_script
    assert '["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]' in remote_script
    assert "git config user.email" in local_script
    assert "Ensure-LocalGitIgnore" in local_script
    assert "Get-Command git" in local_script
    assert "Get-BoolSetting" in local_script
    assert "Read-HookInput" in local_script
    assert "$initializedRepo = $false" in local_script
    assert "if ($initializedRepo)" in local_script
    assert "\n        git add -A 2>&1 | Out-Null\n" in local_script
    assert "git commit --no-verify" in local_script
    assert "Convert-HookDirectoryCandidateToPath" in local_script
    assert '"uri"' in local_script
    assert '"UserPromptSubmit", "SessionStart"' in local_script
    assert '$stopSnapshotEvents = @("Stop", "SubagentStop")' in local_script
    assert "Creating git snapshot on stop hook" in local_script
    assert "Creating git snapshot before auto-continue" in local_script
    assert 'permissionDecision = "allow"' in local_script
    assert "auto_continue_permission_state.json" in local_script
    assert "node_modules/" in local_script
    assert ".env.*" in local_script
    assert '["git", "config", "user.email"]' in remote_script
    assert '["git", "commit", "--no-verify", "-m"' in remote_script
    assert "DEFAULT_GITIGNORE_LINES" in remote_script
    assert "initialized_repo = False" in remote_script
    assert "if initialized_repo:" in remote_script
    assert "normalize_project_dir_candidate" in remote_script
    assert '"uri"' in remote_script
    assert 'PROMPT_SNAPSHOT_EVENTS = {"UserPromptSubmit", "SessionStart"}' in remote_script
    assert 'STOP_SNAPSHOT_EVENTS = {"Stop", "SubagentStop"}' in remote_script
    assert "if hook_event in STOP_SNAPSHOT_EVENTS and git_snapshot_enabled:" in remote_script
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
    body_path = _remote_hook_python_path(tmp_path, body)

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
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "compact and continue"
    assert output["suppressOutput"] is True
    assert "continue" not in output
    assert "message" not in output
    log_path = state_dir / "auto_continue_stop_log.jsonl"
    assert "recoverable_api_error_detected" in log_path.read_text(encoding="utf-8")


def test_remote_stop_hook_treats_content_length_json_api_error_as_recoverable(tmp_path):
    import subprocess

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

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
            "session_id": "session-content-length-json",
            "last_message": (
                'API Error: 400 {"code":"CONTENT_LENGTH_EXCEEDS_THRESHOLD",'
                '"error":"对话内容超出长度限制啦",'
                '"hint":"当前会话积累的内容太长了，已超出上游服务的处理能力。'
                '建议您新建一个会话或者使用/compact命令压缩再继续。"}'
            ),
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "compact and continue"
    assert output["suppressOutput"] is True
    assert "continue" not in output
    assert "message" not in output
    log_path = state_dir / "auto_continue_stop_log.jsonl"
    assert "recoverable_api_error_detected" in log_path.read_text(encoding="utf-8")


def test_remote_stop_hook_handles_bilingual_patterns_and_logs_decisions(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        continuation_prompt="continue remote bilingual",
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")

    def run_hook(session_id: str, message: str):
        input_path.write_text(
            json.dumps({
                "session_id": session_id,
                "last_assistant_message": message,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    english_continue = run_hook(
        "remote-en-continue",
        "Reply with continue to continue the implementation.",
    )
    assert english_continue.returncode == 0, english_continue.stderr
    english_output = json.loads(english_continue.stdout)
    assert english_output["decision"] == "block"
    assert english_output["reason"] == "continue remote bilingual"

    chinese_continue = run_hook(
        "remote-cn-continue",
        "\u63a5\u4e0b\u6765\u9700\u8981\u4fee\u590d\u4e2d\u6587\u8bc6\u522b\u89c4\u5219\u3002",
    )
    assert chinese_continue.returncode == 0, chinese_continue.stderr
    assert json.loads(chinese_continue.stdout)["decision"] == "block"

    reply_continue = run_hook(
        "remote-cn-reply-continue",
        "如果你回复“继续”，我就进入下一步。要继续直接实现吗？",
    )
    assert reply_continue.returncode == 0, reply_continue.stderr
    assert json.loads(reply_continue.stdout)["decision"] == "block"

    chinese_project_continue = run_hook(
        "remote-cn-project-not-done",
        "项目实际上没有完成，可以继续跑。",
    )
    assert chinese_project_continue.returncode == 0, chinese_project_continue.stderr
    assert json.loads(chinese_project_continue.stdout)["decision"] == "block"

    english_project_continue = run_hook(
        "remote-en-project-not-done",
        "The project is not actually complete, so it can keep running.",
    )
    assert english_project_continue.returncode == 0, english_project_continue.stderr
    assert json.loads(english_project_continue.stdout)["decision"] == "block"

    blocker = run_hook(
        "remote-blocker",
        "Please choose which configuration profile to use.",
    )
    assert blocker.returncode == 0, blocker.stderr
    assert blocker.stdout.strip() == ""

    complete = run_hook(
        "remote-complete",
        "Completed implementation and verified tests pass.",
    )
    assert complete.returncode == 0, complete.stderr
    assert complete.stdout.strip() == ""

    log_text = (state_dir / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "incomplete_work_detected" in log_text
    assert "blocker_detected" in log_text
    assert "no_incomplete_match" in log_text


def test_remote_training_guard_continues_independently_and_stops_when_target_met(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=True,
        training_continue_prompt="AUC >= 0.90 and F1 >= 0.85",
        max_continuations=3,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")

    def run_hook(message: str):
        input_path.write_text(
            json.dumps({
                "session_id": "remote-training-session",
                "last_assistant_message": message,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    non_training_result = run_hook("Completed docs cleanup and tests pass.")
    assert non_training_result.returncode == 0, non_training_result.stderr
    assert non_training_result.stdout.strip() == ""

    skip_result = run_hook("TRAINING_NOT_APPLICABLE: this is not a training task.")
    assert skip_result.returncode == 0, skip_result.stderr
    assert skip_result.stdout.strip() == ""

    continue_result = run_hook("Current eval: AUC=0.87, F1=0.82.")
    assert continue_result.returncode == 0, continue_result.stderr
    output = json.loads(continue_result.stdout)
    assert output["decision"] == "block"
    assert "AUC >= 0.90" in output["reason"]
    assert "TRAINING_TARGET_MET" in output["reason"]

    stop_result = run_hook("训练目标已达成：AUC=0.91, F1=0.86.")
    assert stop_result.returncode == 0, stop_result.stderr
    assert stop_result.stdout.strip() == ""

    log_text = (state_dir / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "training_context_not_detected" in log_text
    assert "training_not_applicable" in log_text
    assert "training_guard_continue" in log_text
    assert "training_target_met" in log_text


def test_remote_session_start_hook_uses_payload_cwd_for_initial_git_snapshot(tmp_path):
    import subprocess

    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    hook_cwd = tmp_path / "hook-cwd"
    project_dir = tmp_path / "project"
    hook_cwd.mkdir()
    project_dir.mkdir()
    (project_dir / "model.py").write_text("print('train')\n", encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-session-start-git-cwd",
            "hook_event_name": "SessionStart",
            "cwd": str(project_dir),
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=hook_cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert (project_dir / ".git").exists()
    assert not (hook_cwd / ".git").exists()

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=project_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert log.returncode == 0, log.stderr
    assert "git-snapshot" in log.stdout


def test_remote_session_start_hook_accepts_workspace_file_uri(tmp_path):
    import subprocess

    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    hook_cwd = tmp_path / "hook-cwd"
    project_dir = tmp_path / "project uri"
    hook_cwd.mkdir()
    project_dir.mkdir()
    (project_dir / "model.py").write_text("print('train uri')\n", encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-session-start-git-uri",
            "hook_event_name": "SessionStart",
            "workspaceFolders": [{"uri": project_dir.as_uri()}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=hook_cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert (project_dir / ".git").exists()
    assert not (hook_cwd / ".git").exists()


def test_remote_error_hook_recovers_codex_disconnect_with_backoff(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=2,
        error_retry_initial_delay_seconds=4,
        error_retry_max_delay_seconds=6,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-codex-disconnect",
            "error_message": (
                "Error running remote compact task: unexpected status 503 Service Unavailable: "
                "upstream connect error or disconnect/reset before headers. "
                "reset reason: connection termination, url: backend-api/codex/responses/compact"
            ),
            "status": 503,
        }),
        encoding="utf-8",
    )

    def run_hook():
        return subprocess.run(
            [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    first = run_hook()
    assert first.returncode == 0, first.stderr
    first_output = json.loads(first.stdout)
    assert first_output["recover"] is True
    assert first_output["wait"] == 4
    assert first_output["commands"][0] == "/compress"

    second = run_hook()
    assert second.returncode == 0, second.stderr
    second_output = json.loads(second.stdout)
    assert second_output["wait"] == 6

    third = run_hook()
    assert third.returncode == 0, third.stderr
    assert third.stdout.strip() == ""
    log_text = (state_dir / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert "max_recoveries_reached" in log_text


def test_remote_error_hook_retries_official_responses_disconnect_without_compress(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=2,
        error_retry_initial_delay_seconds=5,
        error_retry_max_delay_seconds=60,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-codex-official-responses-disconnect",
            "error_message": (
                "Reconnecting... 2/5\nReconnecting... 3/5\nReconnecting... 4/5\n"
                "Reconnecting... 5/5\nReconnecting... 1/5\nReconnecting... 2/5\n"
                "Reconnecting... 3/5\nReconnecting... 4/5\nReconnecting... 5/5\n"
                "stream disconnected before completion: error sending request for url "
                "(https://chatgpt.com/backend-api/codex/responses)"
            ),
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["recover"] is True
    assert output["wait"] == 5
    assert "/compress" not in output["commands"]


def test_remote_error_hook_uses_retry_after_for_claude(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=2,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "hook_event_name": "ResponseError",
            "session_id": "remote-claude-rate",
            "status": 429,
            "error": {
                "message": "rate limit exceeded",
                "headers": {"retry-after": "1500ms"},
            },
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["decision"] == "recover"
    assert output["commands"][0]["type"] == "wait"
    assert output["commands"][0]["seconds"] == 2


def test_remote_error_hook_respects_zero_max_recoveries(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        max_error_recoveries=0,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-zero-recoveries",
            "error_message": "stream disconnected before completion",
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    log_text = (state_dir / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert "max_recoveries_reached" in log_text


def test_remote_stop_hook_does_not_treat_status_only_payload_as_error(tmp_path):
    import subprocess

    from core import remote_auto_continue
    from models.auto_continue import AutoContinueSettings

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings = AutoContinueSettings(
        enabled=True,
        error_recovery_enabled=True,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        continuation_prompt="continue normal stop",
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    input_path.write_text(
        json.dumps({
            "session_id": "remote-status-only",
            "status": 503,
            "last_assistant_message": "I still need to continue the implementation.",
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "continue normal stop"


def test_local_stop_hook_outputs_clean_json_and_persists_state(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=1,
        continuation_prompt="continue cleanly",
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-clean-json",
        "last_message": (
            'API Error: 400 {"code":"CONTENT_LENGTH_EXCEEDS_THRESHOLD",'
            '"error":"对话内容超出长度限制啦",'
            '"hint":"当前会话积累的内容太长了，已超出上游服务的处理能力。"}'
        ),
    }

    first = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert not first.stdout.lstrip().startswith(("False", "True"))
    assert "Invalid incomplete pattern" not in first.stderr
    output = json.loads(first.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "continue cleanly"
    assert output["suppressOutput"] is True
    assert "continue" not in output
    assert "message" not in output

    second = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == ""
    assert "AsHashtable" not in second.stderr


def test_local_stop_hook_reads_utf8_stdin_bytes(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        continuation_prompt="continue utf8",
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-utf8-bytes",
        "hook_event_name": "Stop",
        "last_assistant_message": "如果你回复“继续”，我就进入下一步。要继续直接实现吗？",
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    output = json.loads(result.stdout.decode("utf-8-sig"))
    assert output["decision"] == "block"
    assert output["reason"] == "continue utf8"
    log_text = (tmp_path / "tmp" / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "????" not in log_text


def test_local_stop_hook_reads_claude_transcript_path(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    transcript_path = tmp_path / "claude-transcript.jsonl"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        continuation_prompt="continue from transcript",
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    transcript_lines = [
        {"message": {"role": "user", "content": "please implement it"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I still need to finish tests and verification."}
                ],
            }
        },
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in transcript_lines),
        encoding="utf-8",
    )

    payload = {
        "session_id": "session-transcript",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript_path),
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "continue from transcript"


def test_local_stop_hook_snapshots_completed_manual_turn(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")
    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        continuation_prompt="continue after snapshot",
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-no-continuation-no-snapshot",
        "last_assistant_message": "All implementation and verification are complete.",
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert (tmp_path / ".git").exists()
    assert "Creating git snapshot on stop hook" in result.stderr

    rev_parse = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert rev_parse.returncode == 0, rev_parse.stderr


def test_local_session_start_hook_uses_payload_cwd_for_initial_git_snapshot(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")
    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    hook_cwd = tmp_path / "hook-cwd"
    project_dir = tmp_path / "project"
    hook_cwd.mkdir()
    project_dir.mkdir()
    (project_dir / "train.py").write_text("print('hello')\n", encoding="utf-8")

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-start-git-cwd",
        "hook_event_name": "SessionStart",
        "cwd": str(project_dir),
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=hook_cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert (project_dir / ".git").exists()
    assert not (hook_cwd / ".git").exists()
    assert "Using hook project directory" in result.stderr

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=project_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert log.returncode == 0, log.stderr
    assert "git-snapshot" in log.stdout


def test_local_session_start_hook_accepts_workspace_file_uri(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")
    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    hook_cwd = tmp_path / "hook-cwd"
    project_dir = tmp_path / "project uri"
    hook_cwd.mkdir()
    project_dir.mkdir()
    (project_dir / "train.py").write_text("print('hello uri')\n", encoding="utf-8")

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-start-git-uri",
        "hook_event_name": "SessionStart",
        "workspaceFolders": [{"uri": project_dir.as_uri()}],
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=hook_cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert (project_dir / ".git").exists()
    assert not (hook_cwd / ".git").exists()
    assert "Using hook project directory" in result.stderr


def test_local_stop_hook_handles_bilingual_continue_and_blocker_patterns(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=True,
        max_continuations=10,
        continuation_prompt="continue bilingual",
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    def run_hook(session_id: str, message: str):
        payload = {
            "session_id": session_id,
            "hook_event_name": "Stop",
            "last_assistant_message": message,
        }
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            input=json.dumps(payload, ensure_ascii=False),
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    english_continue = run_hook(
        "session-en-continue",
        "Would you like me to continue with the remaining tests?",
    )
    assert english_continue.returncode == 0, english_continue.stderr
    english_output = json.loads(english_continue.stdout)
    assert english_output["decision"] == "block"
    assert english_output["reason"] == "continue bilingual"

    chinese_continue = run_hook(
        "session-cn-continue",
        "\u5f53\u524d\u8fd8\u6ca1\u6709\u5b8c\u6574\u9a8c\u8bc1\uff0c\u8981\u4e0d\u8981\u7ee7\u7eed\uff1f",
    )
    assert chinese_continue.returncode == 0, chinese_continue.stderr
    chinese_output = json.loads(chinese_continue.stdout)
    assert chinese_output["decision"] == "block"

    reply_continue = run_hook(
        "session-cn-reply-continue",
        "如果你回复“继续”，我就进入下一步。要继续直接实现吗？",
    )
    assert reply_continue.returncode == 0, reply_continue.stderr
    reply_output = json.loads(reply_continue.stdout)
    assert reply_output["decision"] == "block"

    chinese_project_continue = run_hook(
        "session-cn-project-not-done",
        "项目实际上没有完成，可以继续跑。",
    )
    assert chinese_project_continue.returncode == 0, chinese_project_continue.stderr
    assert json.loads(chinese_project_continue.stdout)["decision"] == "block"

    english_project_continue = run_hook(
        "session-en-project-not-done",
        "The implementation is actually not finished; we can continue running it.",
    )
    assert english_project_continue.returncode == 0, english_project_continue.stderr
    assert json.loads(english_project_continue.stdout)["decision"] == "block"

    english_blocker = run_hook(
        "session-en-blocker",
        "I need your confirmation before deploying to production.",
    )
    assert english_blocker.returncode == 0, english_blocker.stderr
    assert english_blocker.stdout.strip() == ""

    chinese_blocker = run_hook(
        "session-cn-blocker",
        "\u8bf7\u9009\u62e9\u4e0b\u4e00\u6b65\u64cd\u4f5c\u3002",
    )
    assert chinese_blocker.returncode == 0, chinese_blocker.stderr
    assert chinese_blocker.stdout.strip() == ""

    complete = run_hook(
        "session-complete",
        "Completed implementation and verified tests pass.",
    )
    assert complete.returncode == 0, complete.stderr
    assert complete.stdout.strip() == ""

    log_path = tmp_path / "tmp" / "auto_continue_stop_log.jsonl"
    log_text = log_path.read_text(encoding="utf-8")
    assert "incomplete_work_detected" in log_text
    assert "blocker_detected" in log_text
    assert "no_incomplete_match" in log_text
    assert not (tmp_path / "auto_continue_stop_state.json").exists()
    assert (tmp_path / "tmp" / "auto_continue_stop_state.json").exists()


def test_local_training_guard_continues_independently_and_stops_when_target_met(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=True,
        training_continue_prompt="val_acc >= 0.95 and loss <= 0.2",
        max_continuations=3,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    def run_hook(message: str):
        payload = {
            "session_id": "training-session",
            "hook_event_name": "Stop",
            "last_assistant_message": message,
        }
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            input=json.dumps(payload, ensure_ascii=False),
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    non_training_result = run_hook("Completed docs cleanup and tests pass.")
    assert non_training_result.returncode == 0, non_training_result.stderr
    assert non_training_result.stdout.strip() == ""

    skip_result = run_hook("TRAINING_NOT_APPLICABLE: this is not a training task.")
    assert skip_result.returncode == 0, skip_result.stderr
    assert skip_result.stdout.strip() == ""

    continue_result = run_hook("Finished epoch 8. Current val_acc is 0.91, loss is 0.28.")
    assert continue_result.returncode == 0, continue_result.stderr
    output = json.loads(continue_result.stdout)
    assert output["decision"] == "block"
    assert "val_acc >= 0.95" in output["reason"]
    assert "TRAINING_TARGET_MET" in output["reason"]

    stop_result = run_hook("TRAINING_TARGET_MET: val_acc=0.956 and loss=0.18. Model saved.")
    assert stop_result.returncode == 0, stop_result.stderr
    assert stop_result.stdout.strip() == ""

    log_text = (tmp_path / "tmp" / "auto_continue_stop_log.jsonl").read_text(encoding="utf-8")
    assert "training_context_not_detected" in log_text
    assert "training_not_applicable" in log_text
    assert "training_guard_continue" in log_text
    assert "training_target_met" in log_text


def test_local_codex_error_hook_outputs_clean_json_with_git_snapshot(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "error_recovery.ps1"
    settings = AutoContinueSettings(
        error_recovery_enabled=True,
        max_error_recoveries=1,
        git_auto_snapshot=True,
        git_snapshot_on_recovery=True,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_codex_error_recovery_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-clean-error-json",
        "error_message": (
            'API Error: 400 {"code":"CONTENT_LENGTH_EXCEEDS_THRESHOLD",'
            '"error":"对话内容超出长度限制啦"}'
        ),
        "status": 400,
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stdout.lstrip().startswith(("False", "True"))
    output = json.loads(result.stdout)
    assert output["recover"] is True
    assert output["decision"] == "recover"
    assert output["commands"][0]["type"] == "slash_command"
    assert output["commands"][0]["command"] == "compact"
    assert output["commands"][1]["type"] == "user_message"
    assert "AsHashtable" not in result.stderr

    second = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == ""
    assert "AsHashtable" not in second.stderr


def test_local_codex_error_hook_uses_configured_backoff_for_disconnects(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "error_recovery.ps1"
    settings = AutoContinueSettings(
        error_recovery_enabled=True,
        max_error_recoveries=2,
        error_retry_initial_delay_seconds=7,
        error_retry_max_delay_seconds=10,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_codex_error_recovery_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    lock_path = tmp_path / "tmp" / "error_recovery_state.json.lock"
    _write_stale_lock(lock_path)

    payload = {
        "session_id": "session-disconnect-backoff",
        "error_message": (
            "Error running remote compact task: unexpected status 503 Service Unavailable: "
            "disconnect/reset before headers, reset reason: connection termination, "
            "url: backend-api/codex/responses/compact"
        ),
        "status": 503,
    }

    def run_hook():
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            input=json.dumps(payload),
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    first = run_hook()
    assert first.returncode == 0, first.stderr
    assert not lock_path.exists()
    first_output = json.loads(first.stdout)
    assert first_output["recover"] is True
    assert first_output["wait"] == 7
    assert first_output["decision"] == "recover"
    assert first_output["commands"][0]["type"] == "slash_command"
    assert first_output["commands"][0]["command"] == "compact"

    second = run_hook()
    assert second.returncode == 0, second.stderr
    second_output = json.loads(second.stdout)
    assert second_output["recover"] is True
    assert second_output["wait"] == 10
    assert second_output["commands"][0]["type"] == "slash_command"
    assert second_output["commands"][0]["command"] == "compact"

    third = run_hook()
    assert third.returncode == 0, third.stderr
    assert third.stdout.strip() == ""

    log_text = (tmp_path / "tmp" / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert "max_recoveries_reached" in log_text


def test_local_claude_error_hook_uses_configured_backoff_for_disconnects(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.error_recovery_script import generate_error_recovery_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "error_recovery.ps1"
    settings = AutoContinueSettings(
        error_recovery_enabled=True,
        max_error_recoveries=2,
        error_retry_initial_delay_seconds=4,
        error_retry_max_delay_seconds=6,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_error_recovery_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    lock_path = tmp_path / "tmp" / "error_recovery_state.json.lock"
    _write_stale_lock(lock_path)

    payload = {
        "session_id": "claude-disconnect-backoff",
        "hook_event_name": "ResponseError",
        "error_message": (
            "upstream connect error or disconnect/reset before headers. "
            "reset reason: connection termination"
        ),
        "status": 503,
    }

    def run_hook():
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            input=json.dumps(payload),
            cwd=tmp_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    first = run_hook()
    assert first.returncode == 0, first.stderr
    assert not lock_path.exists()
    first_output = json.loads(first.stdout)
    assert first_output["decision"] == "recover"
    assert first_output["commands"][0]["type"] == "wait"
    assert first_output["commands"][0]["seconds"] == 4

    second = run_hook()
    assert second.returncode == 0, second.stderr
    second_output = json.loads(second.stdout)
    assert second_output["decision"] == "recover"
    assert second_output["commands"][0]["seconds"] == 6

    third = run_hook()
    assert third.returncode == 0, third.stderr
    assert third.stdout.strip() == ""

    log_text = (tmp_path / "tmp" / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert "max_recoveries_reached" in log_text


def test_error_hooks_use_retry_after_headers(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.error_recovery_script import (
        generate_codex_error_recovery_script,
        generate_error_recovery_script,
    )
    from models.auto_continue import AutoContinueSettings

    settings = AutoContinueSettings(
        error_recovery_enabled=True,
        max_error_recoveries=2,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )

    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    codex_settings_path = codex_dir / "auto_continue_settings.json"
    codex_script_path = codex_dir / "error_recovery.ps1"
    codex_settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    codex_script_path.write_text(
        generate_codex_error_recovery_script(str(codex_settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    codex_payload = {
        "session_id": "codex-retry-after",
        "status": 429,
        "headers": {"Retry-After": "2 minutes"},
        "error_message": "rate limit exceeded",
    }
    codex_result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(codex_script_path)],
        input=json.dumps(codex_payload),
        cwd=codex_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert codex_result.returncode == 0, codex_result.stderr
    codex_output = json.loads(codex_result.stdout)
    assert codex_output["recover"] is True
    assert codex_output["wait"] == 120

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    claude_settings_path = claude_dir / "auto_continue_settings.json"
    claude_script_path = claude_dir / "error_recovery.ps1"
    claude_settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    claude_script_path.write_text(
        generate_error_recovery_script(str(claude_settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )
    claude_payload = {
        "session_id": "claude-retry-after",
        "status": 429,
        "error": {
            "message": "rate limit exceeded",
            "headers": {"retry-after": "1500ms"},
        },
    }
    claude_result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(claude_script_path)],
        input=json.dumps(claude_payload),
        cwd=claude_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert claude_result.returncode == 0, claude_result.stderr
    claude_output = json.loads(claude_result.stdout)
    assert claude_output["decision"] == "recover"
    assert claude_output["commands"][0]["type"] == "wait"
    assert claude_output["commands"][0]["seconds"] == 2


def test_local_codex_error_hook_respects_zero_max_recoveries(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.error_recovery_script import generate_codex_error_recovery_script
    from models.auto_continue import AutoContinueSettings

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "error_recovery.ps1"
    settings = AutoContinueSettings(
        error_recovery_enabled=True,
        max_error_recoveries=0,
        git_auto_snapshot=False,
        git_snapshot_on_recovery=False,
    )
    settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")
    script_path.write_text(
        generate_codex_error_recovery_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    payload = {
        "session_id": "session-zero-recoveries",
        "error_message": "stream disconnected before completion",
    }
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps(payload),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    log_text = (tmp_path / "tmp" / "error_recovery_log.jsonl").read_text(encoding="utf-8")
    assert "max_recoveries_reached" in log_text


def test_auto_continue_settings_permission_auto_approve_validation():
    from models.auto_continue import (
        DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY,
        TRAINING_PROMPT_TEMPLATES,
        AutoContinueSettings,
        training_prompt_template_by_key,
    )

    assert AutoContinueSettings().auto_approve_max_per_session == 0
    assert AutoContinueSettings().error_retry_initial_delay_seconds == 5
    assert AutoContinueSettings().error_retry_max_delay_seconds == 60
    assert AutoContinueSettings().training_auto_continue_enabled is False
    assert AutoContinueSettings().training_prompt_template_key == DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY
    assert "TRAINING_TARGET_MET" in AutoContinueSettings().training_continue_prompt
    assert len(TRAINING_PROMPT_TEMPLATES) >= 5
    assert training_prompt_template_by_key(DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY)["key"] == "general"
    assert training_prompt_template_by_key("分类/表格模型")["key"] == "classification"
    assert training_prompt_template_by_key("CLASSIFICATION")["key"] == "classification"
    assert training_prompt_template_by_key("missing")["key"] == "general"
    assert len({template["key"] for template in TRAINING_PROMPT_TEMPLATES}) == len(TRAINING_PROMPT_TEMPLATES)
    for template in TRAINING_PROMPT_TEMPLATES:
        assert template["name"].strip()
        assert template["description"].strip()
        assert "TRAINING_TARGET_MET" in template["prompt"]

    settings = AutoContinueSettings(
        auto_approve_permission_requests=True,
        auto_approve_max_per_session=0,
        auto_approve_bash=False,
        auto_approve_tools=["Edit", "MultiEdit", "Write"],
        error_retry_initial_delay_seconds=3,
        error_retry_max_delay_seconds=30,
    )

    ok, error = settings.validate()
    assert ok, error

    invalid_backoff = AutoContinueSettings(
        error_retry_initial_delay_seconds=30,
        error_retry_max_delay_seconds=3,
    )
    ok, error = invalid_backoff.validate()
    assert not ok
    assert "cannot exceed" in error

    invalid_training_prompt = AutoContinueSettings(
        training_auto_continue_enabled=True,
        training_continue_prompt="",
    )
    ok, error = invalid_training_prompt.validate()
    assert not ok
    assert "training_continue_prompt" in error

    invalid_training_template = AutoContinueSettings(
        training_prompt_template_key="does-not-exist",
    )
    ok, error = invalid_training_template.validate()
    assert not ok
    assert "training_prompt_template_key" in error

    restored = AutoContinueSettings.from_dict({
        "auto_approve_permission_requests": True,
        "auto_approve_max_per_session": 5,
        "auto_approve_tools": ["Edit", "edit", "Write"],
        "training_auto_continue_enabled": "true",
        "training_prompt_template_key": "classification",
        "training_continue_prompt": "accuracy >= 0.95",
    })

    assert restored.auto_approve_permission_requests is True
    assert restored.auto_approve_max_per_session == 5
    assert restored.auto_approve_bash is True
    assert restored.auto_approve_tools == ["Bash", "Edit", "Write"]
    assert restored.training_auto_continue_enabled is True
    assert restored.training_prompt_template_key == "classification"
    assert restored.training_continue_prompt == "accuracy >= 0.95"

    fallback_template = AutoContinueSettings.from_dict({
        "training_prompt_template_key": "unknown-template",
    })
    assert fallback_template.training_prompt_template_key == DEFAULT_TRAINING_PROMPT_TEMPLATE_KEY

    named_template = AutoContinueSettings.from_dict({
        "training_prompt_template_key": "分类/表格模型",
    })
    assert named_template.training_prompt_template_key == "classification"

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
    assert '$updatedPermissions += @{' in local_script
    assert 'destination = "session"' in local_script
    assert 'mode = "dontAsk"' in local_script
    assert 'permission_suggestions' in local_script
    assert 'if "auto_approve_tools" in settings:' in remote_script
    assert "tools = allowed_tools if isinstance(allowed_tools, list) else []" in remote_script
    assert 'if legacy_bash_allowed and "auto_approve_tools" in settings and result' in remote_script
    assert '"updatedPermissions": permission_decision_updates(data, tool_name)' in remote_script
    assert '"destination": "session"' in remote_script
    assert '"mode": "dontAsk"' in remote_script
    assert 'permission_suggestions_from_input' in remote_script
    assert "else DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS" not in remote_script
    assert "$toolName -ieq \"Bash\"" not in local_script
    assert 'tool_name.lower() == "bash"' not in remote_script


def test_git_auto_push_is_wired_into_generated_hooks():
    from core import remote_auto_continue
    from core.auto_continue.error_recovery_script import generate_error_recovery_script
    from core.auto_continue.script_generator import generate_hook_script

    local_script = generate_hook_script("C:\\Users\\Test\\.claude\\auto_continue_settings.json")
    error_script = generate_error_recovery_script("C:\\Users\\Test\\.claude\\auto_continue_settings.json")
    remote_script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )

    assert '$gitAutoPush = Get-BoolSetting -Settings $settings -Name "git_auto_push" -Default $false' in local_script
    assert "function Push-GitSnapshot" in local_script
    assert "Push-GitSnapshot" in local_script
    assert "git push -u $remote $branch" in local_script
    assert "function Push-GitSnapshot" in error_script
    assert "git_auto_push" in error_script
    assert "def push_git_snapshot(auto_push=False):" in remote_script
    assert 'run_git_snapshot(as_bool(settings.get("git_auto_push"), False))' in remote_script
    assert '["git", "push", "-u", remote_name, branch_name]' in remote_script


def test_auto_continue_settings_coerces_string_boolean_values():
    from models.auto_continue import AutoContinueSettings

    settings = AutoContinueSettings.from_dict({
        "enabled": "true",
        "max_continuations": "12",
        "conservative_mode": "false",
        "error_recovery_enabled": "1",
        "max_error_recoveries": "4",
        "error_retry_initial_delay_seconds": "8",
        "error_retry_max_delay_seconds": 40.0,
        "git_auto_snapshot": "0",
        "git_auto_push": "yes",
        "git_snapshot_on_start": "off",
        "git_snapshot_on_recovery": "yes",
        "auto_approve_permission_requests": "on",
        "auto_approve_max_per_session": "6",
        "auto_approve_bash": "no",
        "continuation_prompt": 12345,
        "training_continue_prompt": 67890,
        "training_prompt_template_key": "LLM 微调/评测",
    })

    assert settings.enabled is True
    assert settings.max_continuations == 12
    assert settings.conservative_mode is False
    assert settings.error_recovery_enabled is True
    assert settings.max_error_recoveries == 4
    assert settings.error_retry_initial_delay_seconds == 8
    assert settings.error_retry_max_delay_seconds == 40
    assert settings.git_auto_snapshot is False
    assert settings.git_auto_push is True
    assert settings.git_snapshot_on_start is False
    assert settings.git_snapshot_on_recovery is True
    assert settings.auto_approve_permission_requests is True
    assert settings.auto_approve_max_per_session == 6
    assert settings.auto_approve_bash is False
    assert settings.continuation_prompt == "12345"
    assert settings.training_continue_prompt == "67890"
    assert settings.training_prompt_template_key == "llm_finetune"

    fallback = AutoContinueSettings.from_dict({
        "max_continuations": "not-a-number",
        "max_error_recoveries": None,
        "error_retry_initial_delay_seconds": "",
        "error_retry_max_delay_seconds": object(),
        "auto_approve_max_per_session": True,
        "continuation_prompt": "",
        "training_continue_prompt": None,
    })
    assert fallback.max_continuations == AutoContinueSettings().max_continuations
    assert fallback.max_error_recoveries == AutoContinueSettings().max_error_recoveries
    assert fallback.error_retry_initial_delay_seconds == AutoContinueSettings().error_retry_initial_delay_seconds
    assert fallback.error_retry_max_delay_seconds == AutoContinueSettings().error_retry_max_delay_seconds
    assert fallback.auto_approve_max_per_session == AutoContinueSettings().auto_approve_max_per_session
    assert fallback.continuation_prompt == AutoContinueSettings().continuation_prompt
    assert fallback.training_continue_prompt == AutoContinueSettings().training_continue_prompt

    assert AutoContinueSettings.from_dict(None).to_dict() == AutoContinueSettings().to_dict()


def test_remote_permission_hook_respects_explicit_empty_tools(tmp_path):
    import subprocess

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.claude/auto_continue_settings.json",
        "/home/test/.claude/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

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
            [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
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
    assert edit_decision["updatedPermissions"][-1] == {
        "type": "setMode",
        "mode": "dontAsk",
        "destination": "session",
    }

    suggestion_allowed = run_hook(
        {},
        "Bash",
        {
            "permission_suggestions": [
                {
                    "type": "addRules",
                    "rules": [{"toolName": "Bash", "ruleContent": "git status:*"}],
                    "behavior": "allow",
                    "destination": "localSettings",
                }
            ],
        },
    )
    assert suggestion_allowed.returncode == 0
    suggestion_output = json.loads(suggestion_allowed.stdout)
    suggestion_updates = suggestion_output["hookSpecificOutput"]["decision"]["updatedPermissions"]
    assert suggestion_updates[0]["rules"] == [{"toolName": "Bash", "ruleContent": "git status:*"}]
    assert suggestion_updates[0]["destination"] == "localSettings"
    assert suggestion_updates[-1] == {
        "type": "setMode",
        "mode": "dontAsk",
        "destination": "session",
    }

    nested_suggestion_allowed = run_hook(
        {},
        "Bash",
        {
            "permissionRequest": {
                "toolName": "Bash",
                "permissionSuggestions": [
                    {
                        "type": "addRules",
                        "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
                        "behavior": "allow",
                        "destination": "session",
                    }
                ],
            },
        },
    )
    assert nested_suggestion_allowed.returncode == 0
    nested_output = json.loads(nested_suggestion_allowed.stdout)
    nested_updates = nested_output["hookSpecificOutput"]["decision"]["updatedPermissions"]
    assert nested_updates[0]["rules"] == [{"toolName": "Bash", "ruleContent": "npm test:*"}]

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


def test_local_stop_hook_does_not_continue_when_disabled_but_permission_auto_approve_enabled(tmp_path):
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not available")

    from core.auto_continue.script_generator import generate_hook_script

    settings_path = tmp_path / "auto_continue_settings.json"
    script_path = tmp_path / "auto_continue_stop.ps1"
    settings_path.write_text(
        json.dumps({
            "enabled": "false",
            "git_auto_snapshot": "false",
            "git_snapshot_on_start": "false",
            "auto_approve_permission_requests": "true",
            "max_continuations": 5,
            "continuation_prompt": "continue unexpectedly",
            "incomplete_patterns": ["continue"],
            "blocker_patterns": [],
        }),
        encoding="utf-8",
    )
    script_path.write_text(
        generate_hook_script(str(settings_path).replace("\\", "\\\\")),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        input=json.dumps({
            "hook_event_name": "Stop",
            "session_id": "session-disabled-stop",
            "last_assistant_message": "Reply with continue to continue the implementation.",
        }),
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert not (tmp_path / "tmp" / "auto_continue_stop_state.json").exists()


def test_remote_prompt_hook_snapshots_without_auto_continue(tmp_path):
    import subprocess

    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings_path.write_text(
        json.dumps({
            "enabled": True,
            "git_auto_snapshot": True,
            "git_snapshot_on_start": True,
            "continuation_prompt": "should not continue from prompt hook",
            "incomplete_patterns": ["continue"],
            "blocker_patterns": [],
        }),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-prompt-snapshot",
            "message": "continue the implementation",
        }),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert (tmp_path / ".git").exists()
    assert not (state_dir / "auto_continue_stop_state.json").exists()


def test_remote_git_snapshot_logs_status_failure(tmp_path):
    import subprocess

    if not shutil.which("git"):
        pytest.skip("Git is not available")

    from core import remote_auto_continue

    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    body = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    body_path = _remote_hook_python_path(tmp_path, body)

    init = subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert init.returncode == 0, init.stderr
    (tmp_path / ".git" / "index").write_bytes(b"bad-index")

    settings_path = tmp_path / "settings.json"
    input_path = tmp_path / "input.json"
    state_dir = tmp_path / "state"
    settings_path.write_text(
        json.dumps({
            "enabled": False,
            "git_auto_snapshot": True,
            "git_snapshot_on_start": True,
        }),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-bad-git",
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "Git status failed; skipping git snapshot:" in result.stderr


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
    body_path = _remote_hook_python_path(tmp_path, body)

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
        [sys.executable, str(body_path), str(settings_path), str(state_dir), str(input_path)],
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

    def run_hook(event_name: str, camel_case: bool = False, extra: dict | None = None):
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
        if extra:
            payload.update(extra)
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
    assert decision["updatedPermissions"][-1] == {
        "type": "setMode",
        "mode": "dontAsk",
        "destination": "session",
    }
    assert not (tmp_path / "auto_continue_permission_state.json").exists()
    assert not (tmp_path / "auto_continue_stop_state.json").exists()

    suggested_result = run_hook(
        "PermissionRequest",
        extra={
            "permissionSuggestions": [
                {
                    "type": "addRules",
                    "rules": [{"toolName": "Bash", "ruleContent": "git status:*"}],
                    "behavior": "allow",
                    "destination": "localSettings",
                }
            ],
        },
    )
    assert suggested_result.returncode == 0, suggested_result.stderr
    suggested_output = json.loads(suggested_result.stdout)
    suggested_updates = suggested_output["hookSpecificOutput"]["decision"]["updatedPermissions"]
    assert suggested_updates[0]["rules"] == [{"toolName": "Bash", "ruleContent": "git status:*"}]
    assert suggested_updates[0]["destination"] == "localSettings"
    assert suggested_updates[-1]["type"] == "setMode"
    assert suggested_updates[-1]["mode"] == "dontAsk"

    nested_suggested_result = run_hook(
        "PermissionRequest",
        extra={
            "permissionRequest": {
                "toolName": "Bash",
                "permission_suggestions": [
                    {
                        "type": "addRules",
                        "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
                        "behavior": "allow",
                        "destination": "session",
                    }
                ],
            },
        },
    )
    assert nested_suggested_result.returncode == 0, nested_suggested_result.stderr
    nested_suggested_output = json.loads(nested_suggested_result.stdout)
    nested_updates = nested_suggested_output["hookSpecificOutput"]["decision"]["updatedPermissions"]
    assert nested_updates[0]["rules"] == [{"toolName": "Bash", "ruleContent": "npm test:*"}]

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

    claude_settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    hooks = claude_settings["hooks"]
    assert claude_settings["permissions"]["defaultMode"] == "dontAsk"
    assert claude_settings["skipDangerousModePermissionPrompt"] is False
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
    assert claude_settings["permissions"]["defaultMode"] == "dontAsk"
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


def test_auto_continue_manager_enable_syncs_error_recovery(monkeypatch):
    from core.auto_continue.manager import AutoContinueManager
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider:
        def enable(self, settings):
            calls.append(("enable", settings.enabled, settings.error_recovery_enabled))

        def install_error_recovery(self):
            calls.append("install_error_recovery")

        def uninstall_error_recovery(self):
            calls.append("uninstall_error_recovery")

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: FakeProvider())

    manager.enable("codex", AutoContinueSettings(error_recovery_enabled=True))

    assert calls == [("enable", True, True), "install_error_recovery"]


def test_auto_continue_manager_repair_handles_standalone_features(monkeypatch):
    from core.auto_continue.manager import AutoContinueManager
    from models.auto_continue import AutoContinueSettings

    calls = []
    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=True,
    )

    class FakeProvider:
        def load_settings(self):
            return settings

        def _settings_require_hook(self, value):
            return bool(value.git_auto_snapshot and value.git_snapshot_on_start)

        def install_hook_script(self):
            calls.append("install_hook")

        def register_hook_for_settings(self, value):
            calls.append(("register", value.enabled))

        def install_error_recovery(self):
            calls.append("install_error_recovery")

        def uninstall_error_recovery(self):
            calls.append("uninstall_error_recovery")

        def install_guidance(self):
            calls.append("guidance")

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: FakeProvider())

    manager.repair("codex")

    assert calls == ["install_hook", ("register", False), "install_error_recovery"]


def test_auto_continue_manager_update_settings_syncs_error_recovery(monkeypatch):
    from core.auto_continue.manager import AutoContinueManager
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider:
        def update_settings(self, settings):
            calls.append(("update", settings.error_recovery_enabled))

        def install_error_recovery(self):
            calls.append("install_error_recovery")

        def uninstall_error_recovery(self):
            calls.append("uninstall_error_recovery")

    manager = AutoContinueManager()
    monkeypatch.setattr(manager, "get_provider", lambda _name: FakeProvider())

    manager.update_settings("codex", AutoContinueSettings(enabled=False, error_recovery_enabled=True))
    manager.update_settings("codex", AutoContinueSettings(enabled=False, error_recovery_enabled=False))

    assert calls == [
        ("update", True),
        "install_error_recovery",
        ("update", False),
        "uninstall_error_recovery",
    ]


def test_update_settings_honors_enabled_switch(tmp_path):
    from core.auto_continue.base import AutoContinueProvider
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider(AutoContinueProvider):
        def get_config_dir(self):
            return tmp_path

        def get_hook_script_path(self):
            return tmp_path / "hook.ps1"

        def get_settings_path(self):
            return tmp_path / "auto_continue_settings.json"

        def is_hook_registered(self):
            return False

        def register_hook(self):
            calls.append("register")

        def unregister_hook(self):
            calls.append("unregister")

        def install_hook_script(self):
            calls.append("install")

        def uninstall_hook_script(self):
            calls.append("uninstall")

    provider = FakeProvider("codex")
    provider.save_settings(AutoContinueSettings(enabled=False, git_auto_snapshot=False))

    provider.update_settings(AutoContinueSettings(enabled=True, git_auto_snapshot=False))

    assert provider.load_settings().enabled is True
    assert calls == ["install", "register"]


def test_update_settings_rolls_back_when_hook_registration_fails(tmp_path):
    from core.auto_continue.base import AutoContinueProvider
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider(AutoContinueProvider):
        def get_config_dir(self):
            return tmp_path

        def get_hook_script_path(self):
            return tmp_path / "hook.ps1"

        def get_settings_path(self):
            return tmp_path / "auto_continue_settings.json"

        def is_hook_registered(self):
            return False

        def register_hook(self):
            calls.append("register")
            raise RuntimeError("hook write failed")

        def unregister_hook(self):
            calls.append("unregister")

        def install_hook_script(self):
            calls.append("install")

        def uninstall_hook_script(self):
            calls.append("uninstall")

    provider = FakeProvider("codex")
    provider.save_settings(AutoContinueSettings(enabled=False, git_auto_snapshot=False))

    with pytest.raises(RuntimeError, match="hook write failed"):
        provider.update_settings(AutoContinueSettings(enabled=True, git_auto_snapshot=False))

    assert provider.load_settings().enabled is False
    assert calls == ["install", "register", "unregister"]


def test_update_settings_keeps_previous_hook_when_save_fails(tmp_path, monkeypatch):
    from core.auto_continue.base import AutoContinueProvider
    from models.auto_continue import AutoContinueSettings

    calls = []

    class FakeProvider(AutoContinueProvider):
        def get_config_dir(self):
            return tmp_path

        def get_hook_script_path(self):
            return tmp_path / "hook.ps1"

        def get_settings_path(self):
            return tmp_path / "auto_continue_settings.json"

        def is_hook_registered(self):
            return True

        def register_hook(self):
            calls.append("register")

        def unregister_hook(self):
            calls.append("unregister")

        def install_hook_script(self):
            calls.append("install")

        def uninstall_hook_script(self):
            calls.append("uninstall")

    provider = FakeProvider("codex")
    provider.save_settings(AutoContinueSettings(enabled=True, git_auto_snapshot=False))
    original_save = provider.save_settings

    def fail_saving_new_settings(settings):
        if settings.enabled is False:
            raise RuntimeError("settings disk failed")
        original_save(settings)

    monkeypatch.setattr(provider, "save_settings", fail_saving_new_settings)

    with pytest.raises(RuntimeError, match="settings disk failed"):
        provider.update_settings(AutoContinueSettings(enabled=False, git_auto_snapshot=False))

    assert provider.load_settings().enabled is True
    assert calls == ["unregister", "install", "register"]


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

    def event_commands(event_name: str) -> list[str]:
        nested_commands = [
            hook["command"]
            for group in hooks["hooks"].get(event_name, [])
            for hook in group["hooks"]
        ]
        legacy = hooks.get(event_name, {})
        if isinstance(legacy, dict) and legacy.get("command"):
            nested_commands.append(legacy["command"])
        if isinstance(legacy, dict) and isinstance(legacy.get("hooks"), list):
            nested_commands.extend(
                hook["command"]
                for hook in legacy["hooks"]
                if isinstance(hook, dict) and hook.get("command")
            )
        return nested_commands

    stop_commands = event_commands("Stop")
    prompt_commands = event_commands("UserPromptSubmit")
    session_commands = event_commands("SessionStart")
    error_commands = event_commands("Error")
    response_error_commands = event_commands("ResponseError")
    assert "powershell.exe -File user_stop.ps1" in stop_commands
    assert any("auto_continue_stop.ps1" in command for command in stop_commands)
    assert any("auto_continue_stop.ps1" in command for command in prompt_commands)
    assert any("auto_continue_stop.ps1" in command for command in session_commands)
    assert "powershell.exe -File user_error.ps1" in error_commands
    assert any("error_recovery.ps1" in command for command in error_commands)
    assert any("error_recovery.ps1" in command for command in response_error_commands)
    assert provider.is_hook_registered()
    assert provider.is_error_recovery_installed() is False
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["features"]["codex_hooks"] is True

    provider.get_error_recovery_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")
    assert provider.is_error_recovery_installed()

    provider.unregister_hook()
    provider.uninstall_error_recovery()

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = event_commands("Stop")
    prompt_commands = event_commands("UserPromptSubmit")
    session_commands = event_commands("SessionStart")
    error_commands = event_commands("Error")
    response_error_commands = event_commands("ResponseError")
    assert stop_commands == ["powershell.exe -File user_stop.ps1"]
    assert prompt_commands == []
    assert session_commands == []
    assert error_commands == ["powershell.exe -File user_error.ps1"]
    assert response_error_commands == []
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["features"]["codex_hooks"] is True


def test_local_codex_hook_repair_backs_up_invalid_hooks_json(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{not valid json", encoding="utf-8")

    provider = CodexProvider()
    provider.register_hook()

    backups = list(tmp_path.glob("hooks.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert any("auto_continue_stop.ps1" in command for command in [
        hook["command"]
        for group in hooks["hooks"]["Stop"]
        for hook in group["hooks"]
    ])


def test_local_claude_hook_repair_backs_up_invalid_settings_json(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from models.auto_continue import AutoContinueSettings

    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not valid json", encoding="utf-8")

    provider.register_hook(settings=AutoContinueSettings())

    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for group in settings["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert any("auto_continue_stop.ps1" in command for command in commands)


def test_local_status_requires_prompt_snapshot_hooks_when_git_snapshot_enabled(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from core.auto_continue.codex_provider import CodexProvider
    from models.auto_continue import AutoContinueSettings

    auto_settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
    )

    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    codex = CodexProvider()
    codex.save_settings(auto_settings)
    codex.get_hooks_json_path().write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"command": "powershell.exe -File auto_continue_stop.ps1"}]}]
        }
    }), encoding="utf-8")
    assert not codex.is_hook_registered()
    codex.register_hook(settings=auto_settings)
    assert codex.is_hook_registered()

    claude_home = tmp_path / "claude"
    claude = ClaudeProvider()
    monkeypatch.setattr(claude, "get_config_dir", lambda: claude_home)
    claude.save_settings(auto_settings)
    claude.get_claude_settings_path().write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"command": "powershell.exe -File auto_continue_stop.ps1"}]}]
        }
    }), encoding="utf-8")
    assert not claude.is_hook_registered()
    claude.register_hook(settings=auto_settings)
    assert claude.is_hook_registered()


def test_local_status_requires_stop_hook_when_training_guard_enabled(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from core.auto_continue.codex_provider import CodexProvider
    from models.auto_continue import AutoContinueSettings

    auto_settings = AutoContinueSettings(
        enabled=False,
        training_auto_continue_enabled=True,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )

    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    codex = CodexProvider()
    codex.save_settings(auto_settings)
    codex.get_hooks_json_path().write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert not codex.is_hook_registered()
    codex.register_hook(settings=auto_settings)
    assert codex.is_hook_registered()

    claude_home = tmp_path / "claude"
    claude = ClaudeProvider()
    monkeypatch.setattr(claude, "get_config_dir", lambda: claude_home)
    claude.save_settings(auto_settings)
    claude.get_claude_settings_path().write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert not claude.is_hook_registered()
    claude.register_hook(settings=auto_settings)
    assert claude.is_hook_registered()


def test_local_claude_permission_only_status_does_not_require_stop(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider
    from models.auto_continue import AutoContinueSettings

    settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        auto_approve_permission_requests=True,
    )
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    provider.save_settings(settings)
    provider.get_claude_settings_path().write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"hooks": [{"command": "powershell.exe -File auto_continue_stop.ps1"}]}],
            "PermissionRequest": [{"hooks": [{"command": "powershell.exe -File auto_continue_stop.ps1"}]}],
        }
    }), encoding="utf-8")

    assert provider.is_hook_registered()

    provider.get_claude_settings_path().write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"hooks": [{"command": "powershell.exe -File auto_continue_stop.ps1"}]}],
        }
    }), encoding="utf-8")

    assert not provider.is_hook_registered()


def test_local_claude_error_recovery_hook_is_deduped(tmp_path, monkeypatch):
    from core.auto_continue.claude_provider import ClaudeProvider

    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_config_dir", lambda: tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {
            "ResponseError": [
                {
                    "hooks": [
                        {"command": "powershell.exe -File user_response_error.ps1"},
                        {"command": "powershell.exe -File error_recovery.ps1"},
                    ]
                }
            ]
        }
    }), encoding="utf-8")

    provider._register_error_recovery_hook()
    provider._register_error_recovery_hook()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for group in settings["hooks"]["ResponseError"]
        for hook in group["hooks"]
    ]
    assert "powershell.exe -File user_response_error.ps1" in commands
    assert sum("error_recovery.ps1" in command for command in commands) == 1


def test_local_codex_hooks_toggle_config_when_no_hooks_remain(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    provider.register_hook()

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["features"]["codex_hooks"] is True
    provider.unregister_hook()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["features"]["codex_hooks"] is False

    provider._register_error_recovery_hook()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["features"]["codex_hooks"] is True
    provider.get_error_recovery_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")
    provider.uninstall_error_recovery()
    assert tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["features"]["codex_hooks"] is False


def test_local_uninstall_removes_error_recovery_hook(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    provider.register_hook()
    provider._register_error_recovery_hook()
    provider.get_hook_script_path().parent.mkdir(parents=True, exist_ok=True)
    provider.get_hook_script_path().write_text("", encoding="utf-8")
    provider.get_error_recovery_script_path().write_text("", encoding="utf-8")

    provider.uninstall()

    hooks = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
    assert not hooks.get("hooks")
    assert not provider.get_hook_script_path().exists()
    assert not provider.get_error_recovery_script_path().exists()


def test_local_codex_hooks_sync_legacy_root_flag(tmp_path, monkeypatch):
    from core.auto_continue.codex_provider import CodexProvider

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "model = \"gpt-5.5\"\n"
        "codex_hooks = false\n"
        "\n"
        "[projects]\n",
        encoding="utf-8",
    )

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    provider = CodexProvider()
    provider.register_hook()
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["codex_hooks"] is True
    assert config["features"]["codex_hooks"] is True

    provider.unregister_hook()
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["codex_hooks"] is False
    assert config["features"]["codex_hooks"] is False


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
    assert "\\\\u4e0b\\\\u4e00\\\\u6b65" in raw_saved
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
