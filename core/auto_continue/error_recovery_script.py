"""
生成 API 错误自动恢复的 Hook 脚本
用于检测各种 API 错误并采取相应的恢复策略
"""

from core.auto_continue.error_patterns import CONTENT_LENGTH_PATTERNS


def _powershell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_array(values: list[str], indent: int = 8) -> str:
    prefix = " " * indent
    return ",\n".join(prefix + _powershell_single_quoted(value) for value in values)


def _powershell_regex_union(values: list[str]) -> str:
    return _powershell_single_quoted("|".join(values))


POWERSHELL_STATE_DIR_HELPER = r'''function Get-RecoveryStateDir {
    param([string]$SettingsPath)

    $configDir = Split-Path -Parent $SettingsPath
    $stateDir = Join-Path $configDir "tmp"
    try {
        if (-not (Test-Path -LiteralPath $stateDir)) {
            New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
        }
    } catch {
        Write-Log "Failed to create tmp state directory, falling back to config dir: $_" "WARN"
        $stateDir = $configDir
    }
    return $stateDir
}'''


POWERSHELL_RETRY_AFTER_HELPERS = r'''function Get-ClampedSeconds {
    param(
        [int]$Seconds,
        [int]$DefaultSeconds = 60,
        [int]$MaxSeconds = 600
    )

    if ($Seconds -lt 1) { $Seconds = $DefaultSeconds }
    if ($Seconds -lt 1) { $Seconds = 1 }
    if ($Seconds -gt $MaxSeconds) { $Seconds = $MaxSeconds }
    return $Seconds
}

function Get-RetryAfter {
    param(
        [string]$ErrorMessage,
        [string]$RetryAfterText = "",
        [int]$DefaultSeconds = 60,
        [int]$MaxSeconds = 600
    )

    $candidates = @($RetryAfterText, $ErrorMessage)
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $text = [string]$candidate

        if ($text -match '^\s*(\d+)\s*$') {
            return Get-ClampedSeconds -Seconds ([int]$Matches[1]) -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }
        if ($text -match '(\d+)\s*(ms|millisecond|milliseconds)\b') {
            $seconds = [int][Math]::Ceiling(([double]$Matches[1]) / 1000.0)
            return Get-ClampedSeconds -Seconds $seconds -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }
        if ($text -match '(\d+)\s*(s|sec|secs|second|seconds|\u79d2)(?:\b|\s|\u540e|$)') {
            return Get-ClampedSeconds -Seconds ([int]$Matches[1]) -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }
        if ($text -match '(\d+)\s*(m|min|mins|minute|minutes|\u5206\u949f)(?:\b|\s|\u540e|$)') {
            return Get-ClampedSeconds -Seconds ([int]$Matches[1] * 60) -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }
        if ($text -match '(retry|try again|wait|\u91cd\u8bd5|\u7b49\u5f85|\u7a0d\u540e).{0,80}?(\d+)\s*(s|sec|secs|second|seconds|\u79d2)?(?:\b|\s|$)') {
            return Get-ClampedSeconds -Seconds ([int]$Matches[2]) -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }

        $date = [DateTimeOffset]::MinValue
        if ([DateTimeOffset]::TryParse($text, [ref]$date)) {
            $seconds = [int][Math]::Ceiling(($date - [DateTimeOffset]::Now).TotalSeconds)
            return Get-ClampedSeconds -Seconds $seconds -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
        }
    }

    return Get-ClampedSeconds -Seconds $DefaultSeconds -DefaultSeconds $DefaultSeconds -MaxSeconds $MaxSeconds
}'''


POWERSHELL_BOOL_HELPERS = r'''function ConvertTo-Bool {
    param($Value, [bool]$Default = $false)

    if ($null -eq $Value) { return $Default }
    if ($Value -is [bool]) { return $Value }
    if ($Value -is [byte] -or $Value -is [int] -or $Value -is [long] -or $Value -is [double]) {
        return [bool]$Value
    }

    $text = ([string]$Value).Trim().ToLowerInvariant()
    if ($text -in @("1", "true", "yes", "on")) { return $true }
    if ($text -in @("0", "false", "no", "off")) { return $false }
    return $Default
}

function Get-BoolSetting {
    param($Settings, [string]$Name, [bool]$Default = $false)

    if ($null -eq $Settings -or $null -eq $Settings.PSObject.Properties[$Name]) {
        return $Default
    }
    return ConvertTo-Bool -Value $Settings.PSObject.Properties[$Name].Value -Default $Default
}'''


POWERSHELL_STATE_LOCK_HELPERS = r'''function Acquire-StateLock {
    param(
        [string]$LockPath,
        [int]$TimeoutMilliseconds = 2000,
        [int]$StaleSeconds = 60
    )

    $lockStream = $null
    $deadline = [DateTimeOffset]::Now.AddMilliseconds($TimeoutMilliseconds)
    while ($null -eq $lockStream -and [DateTimeOffset]::Now -lt $deadline) {
        try {
            $lockStream = [System.IO.File]::Open(
                $LockPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        } catch [System.IO.IOException] {
            try {
                if ((Test-Path -LiteralPath $LockPath) -and ((Get-Date) - (Get-Item -LiteralPath $LockPath).LastWriteTime).TotalSeconds -gt $StaleSeconds) {
                    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
                    continue
                }
            } catch {
                # Ignore stale-lock inspection errors and wait.
            }
            Start-Sleep -Milliseconds 100
        } catch {
            Write-Log "Failed to create recovery state lock: $_" "WARN"
            return $null
        }
    }

    return $lockStream
}

function Release-StateLock {
    param($LockStream, [string]$LockPath)

    if ($null -ne $LockStream) {
        try {
            $LockStream.Dispose()
        } catch {
            # Ignore lock stream disposal errors.
        }
    }
    try {
        if (Test-Path -LiteralPath $LockPath) {
            Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # Ignore lock cleanup errors.
    }
}

function Load-RecoveryState {
    param([string]$Path)

    $state = @{}
    if (Test-Path -LiteralPath $Path) {
        try {
            $stateContent = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
            $state = ConvertTo-Hashtable ($stateContent | ConvertFrom-Json -ErrorAction Stop)
        } catch {
            Write-Log "Failed to parse recovery state JSON, resetting state: $_" "WARN"
            $state = @{}
        }
    }
    return $state
}

function Save-RecoveryState {
    param([string]$Path, [hashtable]$State)

    try {
        $tempPath = "$Path.tmp"
        $State | ConvertTo-Json | Set-Content -LiteralPath $tempPath -Encoding UTF8 -ErrorAction Stop
        Move-Item -LiteralPath $tempPath -Destination $Path -Force -ErrorAction Stop
        return $true
    } catch {
        Write-Log "Failed to save recovery state: $_" "ERROR"
        return $false
    }
}'''


def generate_error_recovery_script(settings_path: str, enable_git: bool = True) -> str:
    """生成错误恢复 Hook 脚本（增强版）"""
    git_enabled = "$true" if enable_git else "$false"

    script = f'''# API Error Recovery Hook Script (Enhanced)
# 自动检测和处理各种 API 错误
# Generated by API切换器

$ErrorActionPreference = "Stop"
$gitSnapshotEnabled = {git_enabled}

# 日志函数
function Write-Log {{
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "$timestamp [$Level] $Message"
    [Console]::Error.WriteLine($logMessage)
}}

function Initialize-Utf8Console {{
    try {{
        $utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [Console]::InputEncoding = $utf8NoBom
        [Console]::OutputEncoding = $utf8NoBom
        $script:OutputEncoding = $utf8NoBom
        $global:OutputEncoding = $utf8NoBom
    }} catch {{
        Write-Log "Failed to configure UTF-8 console encoding: $_" "WARN"
    }}
}}

Initialize-Utf8Console

function ConvertTo-Hashtable {{
    param($Value)

    $result = @{{}}
    if ($null -eq $Value) {{ return $result }}
    if ($Value -is [System.Collections.IDictionary]) {{ return $Value }}
    if ($Value.PSObject -and $Value.PSObject.Properties) {{
        foreach ($prop in $Value.PSObject.Properties) {{
            $result[$prop.Name] = $prop.Value
        }}
    }}
    return $result
}}

# Git快照函数
{POWERSHELL_BOOL_HELPERS}

{POWERSHELL_STATE_DIR_HELPER}

function Get-IntSetting {{
    param(
        $Settings,
        [string]$Name,
        [int]$Default,
        [int]$Min,
        [int]$Max
    )

    $value = $Default
    try {{
        if ($null -ne $Settings.PSObject.Properties[$Name]) {{
            $value = [int]$Settings.$Name
        }}
    }} catch {{
        $value = $Default
    }}
    if ($value -lt $Min) {{ $value = $Min }}
    if ($value -gt $Max) {{ $value = $Max }}
    return $value
}}

function Get-BackoffSeconds {{
    param(
        [int]$Attempt,
        [int]$InitialDelay,
        [int]$MaxDelay
    )

    if ($Attempt -lt 1) {{ $Attempt = 1 }}
    $seconds = [Math]::Min($InitialDelay * [Math]::Pow(2, $Attempt - 1), $MaxDelay)
    return [int][Math]::Ceiling($seconds)
}}

{POWERSHELL_RETRY_AFTER_HELPERS}

{POWERSHELL_STATE_LOCK_HELPERS}

function Ensure-LocalGitIgnore {{
    try {{
        $gitignorePath = Join-Path (Get-Location) ".gitignore"
        if (Test-Path $gitignorePath) {{
            return
        }}

        @(
            "# Python",
            "__pycache__/",
            "*.py[cod]",
            "build/",
            "dist/",
            ".venv/",
            "venv/",
            "env/",
            "",
            "# Dependency caches / generated output",
            "node_modules/",
            ".next/",
            ".nuxt/",
            "target/",
            ".cache/",
            ".pytest_cache/",
            ".ruff_cache/",
            ".mypy_cache/",
            "coverage/",
            ".coverage",
            "",
            "# Local secrets",
            ".env",
            ".env.*",
            "!.env.example",
            "!.env.sample",
            "",
            "# Logs",
            "*.log",
            "logs/"
        ) | Set-Content -Path $gitignorePath -Encoding UTF8
        Write-Log "Created local .gitignore for Git snapshots" "INFO"
    }} catch {{
        Write-Log "Failed to create local .gitignore: $_" "WARN"
    }}
}}

function Create-GitSnapshot {{
    param([string]$Message = "Auto snapshot before error recovery")

    try {{
        # 检查是否是git仓库
        $isGitRepo = git rev-parse --git-dir 2>$null
        $initializedRepo = $false
        if (-not $isGitRepo) {{
            # 初始化git仓库
            git init 2>&1 | Out-Null
            $initializedRepo = $true
            Write-Log "Initialized git repository" "INFO"
        }}

        if ($initializedRepo) {{
            Ensure-LocalGitIgnore
        }}

        # 检查是否有更改
        $status = git status --porcelain 2>$null
        if ([string]::IsNullOrWhiteSpace($status)) {{
            Write-Log "No changes to commit" "INFO"
            return ""
        }}

        # 添加所有更改
        git add -A 2>&1 | Out-Null

        # 检查git配置
        $userName = git config user.name 2>$null
        $userEmail = git config user.email 2>$null
        if ([string]::IsNullOrWhiteSpace($userName) -or [string]::IsNullOrWhiteSpace($userEmail)) {{
            git config user.name "API-Switcher-Auto" 2>&1 | Out-Null
            git config user.email "auto@api-switcher.local" 2>&1 | Out-Null
            Write-Log "Configured git user" "INFO"
        }}

        # 提交
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $commitMsg = "[$Message] $timestamp"
        git commit -m $commitMsg 2>&1 | Out-Null

        # 获取commit hash
        $commitHash = git rev-parse --short HEAD 2>$null
        Write-Log "Created git snapshot: $commitHash" "INFO"
        return [string]$commitHash

    }} catch {{
        Write-Log "Failed to create git snapshot: $_" "WARN"
        return ""
    }}
}}

# 错误类型枚举
$ErrorTypes = @{{
    CONTENT_LENGTH_EXCEEDED = "content_length_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    AUTHENTICATION_ERROR = "authentication_error"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODEL_OVERLOADED = "model_overloaded"
    TIMEOUT_ERROR = "timeout_error"
    NETWORK_ERROR = "network_error"
    SERVER_ERROR = "server_error"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"
}}

# 恢复策略枚举
$RecoveryStrategies = @{{
    COMPACT_AND_CONTINUE = "compact_and_continue"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    WAIT_AND_RETRY = "wait_and_retry"
    NOTIFY_USER = "notify_user"
    ABORT = "abort"
    NONE = "none"
}}

# 错误分类函数
function Get-ErrorType {{
    param([string]$ErrorCode, [string]$ErrorMessage, [int]$HttpStatus)

    $combined = "$ErrorCode $ErrorMessage".ToLower()

    # 内容超长
    $contentPatterns = @(
{_powershell_array(CONTENT_LENGTH_PATTERNS, 8)}
    )
    foreach ($pattern in $contentPatterns) {{
        if ($combined -match $pattern) {{
            return $ErrorTypes.CONTENT_LENGTH_EXCEEDED
        }}
    }}

    # 速率限制
    $ratePatterns = @(
        "rate.*limit.*exceeded",
        "too.*many.*requests",
        "请求过于频繁",
        "速率限制"
    )
    if ($HttpStatus -eq 429) {{
        return $ErrorTypes.RATE_LIMIT_EXCEEDED
    }}
    foreach ($pattern in $ratePatterns) {{
        if ($combined -match $pattern) {{
            return $ErrorTypes.RATE_LIMIT_EXCEEDED
        }}
    }}

    # 认证错误
    if ($HttpStatus -eq 401 -or $combined -match "authentication.*failed|invalid.*api.*key|unauthorized|认证失败") {{
        return $ErrorTypes.AUTHENTICATION_ERROR
    }}

    # 权限错误
    if ($HttpStatus -eq 403 -or $combined -match "permission.*denied|access.*denied|forbidden|权限不足") {{
        return $ErrorTypes.PERMISSION_DENIED
    }}

    # 配额超限
    if ($combined -match "quota.*exceeded|insufficient.*quota|配额.*超出|余额不足") {{
        return $ErrorTypes.QUOTA_EXCEEDED
    }}

    # Compact transport failures can arrive with HTTP 503, but they are retryable network resets.
    if ($combined -match "upstream connect error|disconnect/reset before headers|reset reason.*connection termination|connection termination|remote compact task|backend-api/codex/responses/compact|responses/compact|\\b(?:ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ECONNREFUSED)\\b|fetch failed|tls handshake timeout|temporary failure in name resolution|dns.*(failed|failure|timeout)|connection.*(closed|lost|terminated|timed out)|network.*(unreachable|timeout|reset|disconnect)|\\u8fde\\u63a5.*(\\u4e2d\\u65ad|\\u91cd\\u7f6e|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6)|\\u7f51\\u7edc.*(\\u4e2d\\u65ad|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6|\\u9519\\u8bef)") {{
        return $ErrorTypes.NETWORK_ERROR
    }}

    # 模型过载
    if ($HttpStatus -eq 503 -or $combined -match "model.*overloaded|server.*overloaded|capacity.*exceeded|服务器.*繁忙") {{
        return $ErrorTypes.MODEL_OVERLOADED
    }}

    # 超时
    if ($HttpStatus -eq 504 -or $combined -match "timeout|timed.*out|超时") {{
        return $ErrorTypes.TIMEOUT_ERROR
    }}

    # 网络错误
    if ($combined -match "network.*error|connection.*failed|connection.*refused|connection.*(reset|aborted|closed|lost|terminated|timed out)|stream.*disconnect|reconnecting\\.\\.\\.\\s*\\d+/\\d+|upstream connect error|disconnect/reset before headers|reset reason.*connection termination|connection termination|error sending request for url|remote compact task|backend-api/codex/responses/compact|responses/compact|broken.*pipe|socket.*hang.*up|fetch failed|tls handshake timeout|temporary failure in name resolution|dns.*(failed|failure|timeout)|\\b(?:ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ECONNREFUSED)\\b|\\u8fde\\u63a5.*(\\u4e2d\\u65ad|\\u91cd\\u7f6e|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6)|\\u7f51\\u7edc.*(\\u4e2d\\u65ad|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6|\\u9519\\u8bef)") {{
        return $ErrorTypes.NETWORK_ERROR
    }}

    # 服务器错误
    if (($HttpStatus -ge 500 -and $HttpStatus -lt 600) -or $combined -match "internal.*server.*error|server.*error|服务器.*错误") {{
        return $ErrorTypes.SERVER_ERROR
    }}

    # 无效请求
    if ($HttpStatus -ge 400 -and $HttpStatus -lt 500) {{
        return $ErrorTypes.INVALID_REQUEST
    }}

    return $ErrorTypes.UNKNOWN
}}

# 获取恢复策略
function Get-RecoveryStrategy {{
    param([string]$ErrorType)

    $strategyMap = @{{
        $ErrorTypes.CONTENT_LENGTH_EXCEEDED = $RecoveryStrategies.COMPACT_AND_CONTINUE
        $ErrorTypes.RATE_LIMIT_EXCEEDED = $RecoveryStrategies.WAIT_AND_RETRY
        $ErrorTypes.MODEL_OVERLOADED = $RecoveryStrategies.RETRY_WITH_BACKOFF
        $ErrorTypes.TIMEOUT_ERROR = $RecoveryStrategies.RETRY_WITH_BACKOFF
        $ErrorTypes.NETWORK_ERROR = $RecoveryStrategies.RETRY_WITH_BACKOFF
        $ErrorTypes.SERVER_ERROR = $RecoveryStrategies.RETRY_WITH_BACKOFF
        $ErrorTypes.AUTHENTICATION_ERROR = $RecoveryStrategies.NOTIFY_USER
        $ErrorTypes.PERMISSION_DENIED = $RecoveryStrategies.NOTIFY_USER
        $ErrorTypes.QUOTA_EXCEEDED = $RecoveryStrategies.NOTIFY_USER
        $ErrorTypes.INVALID_REQUEST = $RecoveryStrategies.ABORT
        $ErrorTypes.UNKNOWN = $RecoveryStrategies.NONE
    }}

    if ($strategyMap.ContainsKey($ErrorType)) {{
        return $strategyMap[$ErrorType]
    }}
    return $RecoveryStrategies.NONE
}}

function Get-TextValue {{
    param($Value)

    if ($null -eq $Value) {{ return $null }}
    if ($Value -is [string]) {{
        if ([string]::IsNullOrWhiteSpace($Value)) {{ return $null }}
        return $Value
    }}
    if ($Value -is [System.Array]) {{
        foreach ($item in $Value) {{
            $text = Get-TextValue $item
            if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
        }}
        return $null
    }}
    if ($Value.PSObject -and $Value.PSObject.Properties) {{
        foreach ($name in @("message", "error_message", "errorMessage", "detail", "hint", "text", "content", "body", "error", "errors", "data")) {{
            $prop = $Value.PSObject.Properties[$name]
            if ($null -ne $prop) {{
                $text = Get-TextValue $prop.Value
                if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
            }}
        }}
    }}
    try {{
        $json = $Value | ConvertTo-Json -Compress -Depth 10
        if (-not [string]::IsNullOrWhiteSpace($json)) {{ return $json }}
    }} catch {{
        $text = [string]$Value
        if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
    }}
    return $null
}}

function Get-FirstTextField {{
    param($Object, [string[]]$Names)

    if ($null -eq $Object) {{ return $null }}
    foreach ($name in $Names) {{
        $prop = $Object.PSObject.Properties[$name]
        if ($null -ne $prop) {{
            $text = Get-TextValue $prop.Value
            if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
        }}
    }}
    return $null
}}

try {{
    # 读取配置
    $settingsPath = "{settings_path}"
    if (-not (Test-Path $settingsPath)) {{
        Write-Log "Settings file not found: $settingsPath" "WARN"
        exit 0
    }}

    try {{
        $settings = Get-Content $settingsPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    }} catch {{
        Write-Log "Failed to parse settings JSON: $_" "ERROR"
        exit 0
    }}

    # 检查是否启用错误恢复
    if (-not (Get-BoolSetting -Settings $settings -Name "error_recovery_enabled" -Default $false)) {{
        exit 0
    }}

    $gitAutoSnapshot = Get-BoolSetting -Settings $settings -Name "git_auto_snapshot" -Default $gitSnapshotEnabled
    $gitSnapshotOnRecovery = Get-BoolSetting -Settings $settings -Name "git_snapshot_on_recovery" -Default $gitSnapshotEnabled
    $retryInitialDelay = Get-IntSetting -Settings $settings -Name "error_retry_initial_delay_seconds" -Default 5 -Min 1 -Max 300
    $retryMaxDelay = Get-IntSetting -Settings $settings -Name "error_retry_max_delay_seconds" -Default 60 -Min 1 -Max 600
    if ($retryInitialDelay -gt $retryMaxDelay) {{ $retryInitialDelay = $retryMaxDelay }}

    # 读取 stdin (Hook 输入)
    $stdin = [Console]::In.ReadToEnd()

    if ([string]::IsNullOrWhiteSpace($stdin)) {{
        exit 0
    }}

    # 解析输入 JSON
    try {{
        $hookInput = $stdin | ConvertFrom-Json -ErrorAction Stop
    }} catch {{
        Write-Log "Failed to parse input JSON: $_" "ERROR"
        exit 0
    }}

    # 提取错误信息
    $errorCode = Get-FirstTextField $hookInput @("error_code", "code", "errorCode", "error_type", "type")
    $errorMessage = Get-FirstTextField $hookInput @("error_message", "message", "error", "errorMessage", "hint", "detail", "response", "body", "data", "errors", "stderr", "stdout")
    $httpStatusText = Get-FirstTextField $hookInput @("status", "http_status", "status_code", "statusCode")
    [int]$httpStatus = 0
    if (-not [string]::IsNullOrWhiteSpace($httpStatusText)) {{
        [int]::TryParse([string]$httpStatusText, [ref]$httpStatus) | Out-Null
    }}
    $sessionId = Get-FirstTextField $hookInput @("session_id", "sessionId", "conversation_id", "conversationId")
    $retryAfterText = Get-FirstTextField $hookInput @("retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After")
    if ($hookInput.headers) {{
        $headerRetryAfter = Get-FirstTextField $hookInput.headers @("retry-after", "Retry-After", "retry_after", "retryAfter")
        if (-not [string]::IsNullOrWhiteSpace($headerRetryAfter)) {{ $retryAfterText = $headerRetryAfter }}
    }}

    # 尝试从嵌套的 error 对象中提取
    if ($hookInput.error) {{
        $nestedCode = Get-FirstTextField $hookInput.error @("code", "type", "error_code", "errorCode")
        $nestedMessage = Get-FirstTextField $hookInput.error @("message", "error_message", "errorMessage", "detail", "hint", "response", "body", "data", "errors")
        $nestedStatusText = Get-FirstTextField $hookInput.error @("status", "status_code", "statusCode", "http_status")
        $nestedRetryAfter = Get-FirstTextField $hookInput.error @("retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After")
        if (-not [string]::IsNullOrWhiteSpace($nestedCode)) {{ $errorCode = $nestedCode }}
        if (-not [string]::IsNullOrWhiteSpace($nestedMessage)) {{ $errorMessage = $nestedMessage }}
        if (-not [string]::IsNullOrWhiteSpace($nestedStatusText)) {{
            [int]::TryParse([string]$nestedStatusText, [ref]$httpStatus) | Out-Null
        }}
        if (-not [string]::IsNullOrWhiteSpace($nestedRetryAfter)) {{ $retryAfterText = $nestedRetryAfter }}
        if ($hookInput.error.headers) {{
            $nestedHeaderRetryAfter = Get-FirstTextField $hookInput.error.headers @("retry-after", "Retry-After", "retry_after", "retryAfter")
            if (-not [string]::IsNullOrWhiteSpace($nestedHeaderRetryAfter)) {{ $retryAfterText = $nestedHeaderRetryAfter }}
        }}
    }}

    if ([string]::IsNullOrWhiteSpace($sessionId)) {{
        $sessionId = Get-FirstTextField $hookInput @("transcript_path", "transcriptPath", "cwd")
    }}
    if ([string]::IsNullOrWhiteSpace($sessionId)) {{
        $sessionId = [System.IO.Directory]::GetCurrentDirectory()
    }}

    if ([string]::IsNullOrWhiteSpace($errorCode) -and [string]::IsNullOrWhiteSpace($errorMessage)) {{
        exit 0  # 不是错误，正常退出
    }}

    Write-Log "Detected API error: [$errorCode] $errorMessage (HTTP $httpStatus)" "INFO"

    # 分类错误类型
    $errorType = Get-ErrorType -ErrorCode $errorCode -ErrorMessage $errorMessage -HttpStatus $httpStatus
    Write-Log "Error type: $errorType" "INFO"

    # 获取恢复策略
    $recoveryStrategy = Get-RecoveryStrategy -ErrorType $errorType
    Write-Log "Recovery strategy: $recoveryStrategy" "INFO"

    # 如果没有恢复策略，退出
    if ($recoveryStrategy -eq $RecoveryStrategies.NONE -or $recoveryStrategy -eq $RecoveryStrategies.ABORT) {{
        Write-Log "No recovery strategy available for this error type" "INFO"
        exit 0
    }}

    # 检查恢复次数限制
    $stateDir = Get-RecoveryStateDir -SettingsPath $settingsPath
    $statePath = Join-Path $stateDir "error_recovery_state.json"
    $lockPath = "$statePath.lock"
    $lockStream = Acquire-StateLock -LockPath $lockPath
    if ($null -eq $lockStream) {{
        Write-Log "Failed to acquire recovery state lock" "WARN"
        exit 0
    }}

    $maxRecoveriesReached = $false
    try {{
        $state = Load-RecoveryState -Path $statePath

        $recoveryKey = "$sessionId-$errorType"
        $recoveryCount = if ($state.ContainsKey($recoveryKey)) {{ $state[$recoveryKey] }} else {{ 0 }}
        $maxRecoveries = Get-IntSetting -Settings $settings -Name "max_error_recoveries" -Default 3 -Min 0 -Max 10

        if ($recoveryCount -ge $maxRecoveries) {{
            Write-Log "Max recovery attempts reached ($maxRecoveries) for $errorType" "WARN"

            # 记录日志
            $recoveryLogPath = Join-Path $stateDir "error_recovery_log.jsonl"
            $logEntry = @{{
                timestamp = (Get-Date -Format "o")
                session_id = $sessionId
                error_type = $errorType
                error_code = $errorCode
                error_message = $errorMessage
                http_status = $httpStatus
                recovery_strategy = $recoveryStrategy
                action = "max_recoveries_reached"
                recovery_count = $recoveryCount
            }} | ConvertTo-Json -Compress
            Add-Content -LiteralPath $recoveryLogPath -Value $logEntry -Encoding UTF8 -ErrorAction SilentlyContinue

            $maxRecoveriesReached = $true
        }} else {{

            # 更新恢复次数
            $recoveryCount++
            $state[$recoveryKey] = $recoveryCount
            Save-RecoveryState -Path $statePath -State $state | Out-Null
        }}
    }} finally {{
        Release-StateLock -LockStream $lockStream -LockPath $lockPath
    }}
    if ($maxRecoveriesReached) {{
        exit 0
    }}

    $gitCommitHash = ""

    # 创建Git快照（如果启用）
    if ($gitAutoSnapshot -and $gitSnapshotOnRecovery) {{
        Write-Log "Creating git snapshot before recovery..." "INFO"
        $gitCommitHash = Create-GitSnapshot -Message "error-recovery"
    }}

    # 记录恢复尝试
    $recoveryLogPath = Join-Path $stateDir "error_recovery_log.jsonl"
    $logEntryData = @{{
        timestamp = (Get-Date -Format "o")
        session_id = $sessionId
        error_type = $errorType
        error_code = $errorCode
        error_message = $errorMessage
        http_status = $httpStatus
        recovery_strategy = $recoveryStrategy
        action = "attempting_recovery"
        recovery_count = $recoveryCount
    }}
    if (-not [string]::IsNullOrWhiteSpace($gitCommitHash)) {{
        $logEntryData.git_commit_hash = $gitCommitHash
    }}
    $logEntry = $logEntryData | ConvertTo-Json -Compress
    Add-Content -LiteralPath $recoveryLogPath -Value $logEntry -Encoding UTF8 -ErrorAction SilentlyContinue

    # 根据恢复策略生成响应
    $output = $null

    if ($recoveryStrategy -eq $RecoveryStrategies.COMPACT_AND_CONTINUE) {{
        # 压缩并继续
        $output = @{{
            decision = "recover"
            commands = @(
                @{{
                    type = "slash_command"
                    command = "compact"
                }},
                @{{
                    type = "user_message"
                    message = "继续"
                }}
            )
            suppressOutput = $true
            userMessage = "对话内容过长，正在自动压缩并继续..."
        }} | ConvertTo-Json -Depth 10

    }} elseif ($recoveryStrategy -eq $RecoveryStrategies.WAIT_AND_RETRY) {{
        # 等待后重试
        $retryAfter = Get-RetryAfter -ErrorMessage $errorMessage -RetryAfterText $retryAfterText -DefaultSeconds 60 -MaxSeconds 600

        $output = @{{
            decision = "recover"
            commands = @(
                @{{
                    type = "wait"
                    seconds = $retryAfter
                }},
                @{{
                    type = "user_message"
                    message = "继续"
                }}
            )
            suppressOutput = $true
            userMessage = "请求过于频繁，等待 $retryAfter 秒后重试..."
        }} | ConvertTo-Json -Depth 10

    }} elseif ($recoveryStrategy -eq $RecoveryStrategies.RETRY_WITH_BACKOFF) {{
        # 指数退避重试
        $backoffSeconds = Get-BackoffSeconds -Attempt $recoveryCount -InitialDelay $retryInitialDelay -MaxDelay $retryMaxDelay
        $isCompactTransportError = $errorMessage -match "remote compact task|backend-api/codex/responses/compact|responses/compact"

        if ($isCompactTransportError) {{
            $output = @{{
                decision = "recover"
                commands = @(
                    @{{
                        type = "wait"
                        seconds = $backoffSeconds
                    }},
                    @{{
                        type = "slash_command"
                        command = "compact"
                    }},
                    @{{
                        type = "user_message"
                        message = "继续"
                    }}
                )
                suppressOutput = $true
                userMessage = "压缩任务连接中断，等待 $backoffSeconds 秒后重新压缩并继续..."
            }} | ConvertTo-Json -Depth 10
        }} else {{
            $output = @{{
                decision = "recover"
                commands = @(
                    @{{
                        type = "wait"
                        seconds = $backoffSeconds
                    }},
                    @{{
                        type = "user_message"
                        message = "继续"
                    }}
                )
                suppressOutput = $true
                userMessage = "服务暂时不可用，等待 $backoffSeconds 秒后重试..."
            }} | ConvertTo-Json -Depth 10
        }}

    }} elseif ($recoveryStrategy -eq $RecoveryStrategies.NOTIFY_USER) {{
        # 通知用户
        $userMsg = switch ($errorType) {{
            $ErrorTypes.AUTHENTICATION_ERROR {{ "认证失败，请检查 API 密钥" }}
            $ErrorTypes.PERMISSION_DENIED {{ "权限不足，请检查账户权限" }}
            $ErrorTypes.QUOTA_EXCEEDED {{ "配额已用完，请充值或等待配额重置" }}
            default {{ "发生错误: $errorMessage" }}
        }}

        $output = @{{
            decision = "notify"
            userMessage = $userMsg
            suppressOutput = $false
        }} | ConvertTo-Json -Depth 10
    }}

    if ($output) {{
        Write-Output $output
        Write-Log "Recovery initiated: $recoveryStrategy (attempt $recoveryCount/$maxRecoveries)" "INFO"
    }}

}} catch {{
    Write-Log "Unexpected error in recovery hook: $_" "ERROR"
    exit 0
}}

exit 0
'''

    return script


def generate_codex_error_recovery_script(settings_path: str, enable_git: bool = True) -> str:
    """生成 Codex CLI 的错误恢复脚本（增强版）"""
    git_enabled = "$true" if enable_git else "$false"

    script = f'''# Codex CLI Error Recovery Hook Script (Enhanced)
# 自动检测和处理各种 API 错误

$ErrorActionPreference = "Stop"
$gitSnapshotEnabled = {git_enabled}

function Write-Log {{
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "$timestamp [$Level] $Message"
    [Console]::Error.WriteLine($logMessage)
}}

function Initialize-Utf8Console {{
    try {{
        $utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [Console]::InputEncoding = $utf8NoBom
        [Console]::OutputEncoding = $utf8NoBom
        $script:OutputEncoding = $utf8NoBom
        $global:OutputEncoding = $utf8NoBom
    }} catch {{
        Write-Log "Failed to configure UTF-8 console encoding: $_" "WARN"
    }}
}}

Initialize-Utf8Console

function ConvertTo-Hashtable {{
    param($Value)

    $result = @{{}}
    if ($null -eq $Value) {{ return $result }}
    if ($Value -is [System.Collections.IDictionary]) {{ return $Value }}
    if ($Value.PSObject -and $Value.PSObject.Properties) {{
        foreach ($prop in $Value.PSObject.Properties) {{
            $result[$prop.Name] = $prop.Value
        }}
    }}
    return $result
}}

{POWERSHELL_BOOL_HELPERS}

{POWERSHELL_STATE_DIR_HELPER}

function Get-IntSetting {{
    param(
        $Settings,
        [string]$Name,
        [int]$Default,
        [int]$Min,
        [int]$Max
    )

    $value = $Default
    try {{
        if ($null -ne $Settings.PSObject.Properties[$Name]) {{
            $value = [int]$Settings.$Name
        }}
    }} catch {{
        $value = $Default
    }}
    if ($value -lt $Min) {{ $value = $Min }}
    if ($value -gt $Max) {{ $value = $Max }}
    return $value
}}

function Get-BackoffSeconds {{
    param(
        [int]$Attempt,
        [int]$InitialDelay,
        [int]$MaxDelay
    )

    if ($Attempt -lt 1) {{ $Attempt = 1 }}
    $seconds = [Math]::Min($InitialDelay * [Math]::Pow(2, $Attempt - 1), $MaxDelay)
    return [int][Math]::Ceiling($seconds)
}}

{POWERSHELL_RETRY_AFTER_HELPERS}

# Git快照函数
{POWERSHELL_STATE_LOCK_HELPERS}

function Ensure-LocalGitIgnore {{
    try {{
        $gitignorePath = Join-Path (Get-Location) ".gitignore"
        if (Test-Path $gitignorePath) {{
            return
        }}

        @(
            "# Python",
            "__pycache__/",
            "*.py[cod]",
            "build/",
            "dist/",
            ".venv/",
            "venv/",
            "env/",
            "",
            "# Dependency caches / generated output",
            "node_modules/",
            ".next/",
            ".nuxt/",
            "target/",
            ".cache/",
            ".pytest_cache/",
            ".ruff_cache/",
            ".mypy_cache/",
            "coverage/",
            ".coverage",
            "",
            "# Local secrets",
            ".env",
            ".env.*",
            "!.env.example",
            "!.env.sample",
            "",
            "# Logs",
            "*.log",
            "logs/"
        ) | Set-Content -Path $gitignorePath -Encoding UTF8
        Write-Log "Created local .gitignore for Git snapshots" "INFO"
    }} catch {{
        Write-Log "Failed to create local .gitignore: $_" "WARN"
    }}
}}

function Create-GitSnapshot {{
    param([string]$Message = "Auto snapshot before error recovery")

    try {{
        $isGitRepo = git rev-parse --git-dir 2>$null
        $initializedRepo = $false
        if (-not $isGitRepo) {{
            git init 2>&1 | Out-Null
            $initializedRepo = $true
            Write-Log "Initialized git repository" "INFO"
        }}

        if ($initializedRepo) {{
            Ensure-LocalGitIgnore
        }}

        $status = git status --porcelain 2>$null
        if ([string]::IsNullOrWhiteSpace($status)) {{
            Write-Log "No changes to commit" "INFO"
            return ""
        }}

        git add -A 2>&1 | Out-Null

        $userName = git config user.name 2>$null
        $userEmail = git config user.email 2>$null
        if ([string]::IsNullOrWhiteSpace($userName) -or [string]::IsNullOrWhiteSpace($userEmail)) {{
            git config user.name "API-Switcher-Auto" 2>&1 | Out-Null
            git config user.email "auto@api-switcher.local" 2>&1 | Out-Null
            Write-Log "Configured git user" "INFO"
        }}

        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $commitMsg = "[$Message] $timestamp"
        git commit -m $commitMsg 2>&1 | Out-Null

        $commitHash = git rev-parse --short HEAD 2>$null
        Write-Log "Created git snapshot: $commitHash" "INFO"
        return [string]$commitHash

    }} catch {{
        Write-Log "Failed to create git snapshot: $_" "WARN"
        return ""
    }}
}}

# 错误分类函数
function Get-ErrorType {{
    param([string]$ErrorCode, [string]$ErrorMessage, [int]$HttpStatus)
    $combined = "$ErrorCode $ErrorMessage".ToLower()

    if ($combined -match {_powershell_regex_union(CONTENT_LENGTH_PATTERNS)}) {{
        return "content_length"
    }}
    if ($HttpStatus -eq 429 -or $combined -match "rate.*limit|too.*many|retry.*after|\\u8bf7\\u6c42.*\\u9891\\u7e41|\\u901f\\u7387|\\u9891\\u7387") {{
        return "rate_limit"
    }}
    if ($HttpStatus -eq 401 -or $combined -match "authentication.*failed|invalid.*api.*key|unauthorized|auth|\\u8ba4\\u8bc1|\\u5bc6\\u94a5") {{
        return "auth"
    }}
    if ($HttpStatus -eq 403 -or $combined -match "permission.*denied|access.*denied|forbidden|\\u6743\\u9650") {{
        return "permission"
    }}
    if ($combined -match "quota|insufficient.*balance|insufficient.*quota|\\u914d\\u989d|\\u4f59\\u989d") {{
        return "quota"
    }}
    if ($combined -match "remote compact task|backend-api/codex/responses/compact|responses/compact|upstream connect error|disconnect/reset before headers|reset reason.*connection termination|connection termination") {{
        return "network"
    }}
    if ($HttpStatus -eq 504 -or $combined -match "timeout|timed.*out|request timed out|\\u8bf7\\u6c42.*\\u8d85\\u65f6|\\u8d85\\u65f6") {{
        return "timeout"
    }}
    if ($combined -match "\\b(?:ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ECONNREFUSED)\\b|network.*(error|unreachable|timeout|reset|disconnect)|connection.*(failed|refused|reset|aborted|closed|lost|terminated|timed out)|stream.*disconnect|reconnecting\\.\\.\\.\\s*\\d+/\\d+|error sending request for url|broken.*pipe|socket.*hang.*up|fetch failed|tls handshake timeout|temporary failure in name resolution|dns.*(failed|failure|timeout)|\\u8fde\\u63a5.*(\\u4e2d\\u65ad|\\u91cd\\u7f6e|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6)|\\u7f51\\u7edc.*(\\u4e2d\\u65ad|\\u65ad\\u5f00|\\u5931\\u8d25|\\u8d85\\u65f6|\\u9519\\u8bef)") {{
        return "network"
    }}
    if ($HttpStatus -eq 503 -or $combined -match "overload|capacity.*exceeded|service unavailable|503|\\u7e41\\u5fd9|\\u8fc7\\u8f7d") {{
        return "overload"
    }}
    if ($HttpStatus -ge 500 -and $HttpStatus -lt 600) {{
        return "server"
    }}
    if ($HttpStatus -ge 400 -and $HttpStatus -lt 500) {{
        return "invalid"
    }}

    return "unknown"
}}

function Get-TextValue {{
    param($Value)

    if ($null -eq $Value) {{ return $null }}
    if ($Value -is [string]) {{
        if ([string]::IsNullOrWhiteSpace($Value)) {{ return $null }}
        return $Value
    }}
    if ($Value -is [System.Array]) {{
        foreach ($item in $Value) {{
            $text = Get-TextValue $item
            if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
        }}
        return $null
    }}
    if ($Value.PSObject -and $Value.PSObject.Properties) {{
        foreach ($name in @("message", "error_message", "errorMessage", "detail", "hint", "text", "content", "body", "error", "errors", "data")) {{
            $prop = $Value.PSObject.Properties[$name]
            if ($null -ne $prop) {{
                $text = Get-TextValue $prop.Value
                if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
            }}
        }}
    }}
    try {{
        $json = $Value | ConvertTo-Json -Compress -Depth 10
        if (-not [string]::IsNullOrWhiteSpace($json)) {{ return $json }}
    }} catch {{
        $text = [string]$Value
        if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
    }}
    return $null
}}

function Get-FirstTextField {{
    param($Object, [string[]]$Names)

    if ($null -eq $Object) {{ return $null }}
    foreach ($name in $Names) {{
        $prop = $Object.PSObject.Properties[$name]
        if ($null -ne $prop) {{
            $text = Get-TextValue $prop.Value
            if (-not [string]::IsNullOrWhiteSpace($text)) {{ return $text }}
        }}
    }}
    return $null
}}

try {{
    $settingsPath = "{settings_path}"
    if (-not (Test-Path $settingsPath)) {{
        exit 0
    }}

    $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not (Get-BoolSetting -Settings $settings -Name "error_recovery_enabled" -Default $false)) {{
        exit 0
    }}

    $gitAutoSnapshot = Get-BoolSetting -Settings $settings -Name "git_auto_snapshot" -Default $gitSnapshotEnabled
    $gitSnapshotOnRecovery = Get-BoolSetting -Settings $settings -Name "git_snapshot_on_recovery" -Default $gitSnapshotEnabled
    $retryInitialDelay = Get-IntSetting -Settings $settings -Name "error_retry_initial_delay_seconds" -Default 5 -Min 1 -Max 300
    $retryMaxDelay = Get-IntSetting -Settings $settings -Name "error_retry_max_delay_seconds" -Default 60 -Min 1 -Max 600
    if ($retryInitialDelay -gt $retryMaxDelay) {{ $retryInitialDelay = $retryMaxDelay }}

    # 读取输入
    $stdin = [Console]::In.ReadToEnd()

    if ([string]::IsNullOrWhiteSpace($stdin)) {{
        exit 0
    }}

    $hookInput = $stdin | ConvertFrom-Json
    $errorCode = Get-FirstTextField $hookInput @("error_code", "code", "errorCode", "error_type", "type")
    $errorMessage = Get-FirstTextField $hookInput @("error_message", "message", "error", "errorMessage", "hint", "detail", "response", "body", "data", "errors", "stderr", "stdout")
    $httpStatusText = Get-FirstTextField $hookInput @("status", "http_status", "status_code", "statusCode")
    [int]$httpStatus = 0
    if (-not [string]::IsNullOrWhiteSpace($httpStatusText)) {{
        [int]::TryParse([string]$httpStatusText, [ref]$httpStatus) | Out-Null
    }}
    $sessionId = Get-FirstTextField $hookInput @("session_id", "sessionId", "conversation_id", "conversationId")
    $retryAfterText = Get-FirstTextField $hookInput @("retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After")
    if ($hookInput.headers) {{
        $headerRetryAfter = Get-FirstTextField $hookInput.headers @("retry-after", "Retry-After", "retry_after", "retryAfter")
        if (-not [string]::IsNullOrWhiteSpace($headerRetryAfter)) {{ $retryAfterText = $headerRetryAfter }}
    }}

    # 尝试从嵌套对象提取
    if ($hookInput.error) {{
        $nestedCode = Get-FirstTextField $hookInput.error @("code", "type", "error_code", "errorCode")
        $nestedMessage = Get-FirstTextField $hookInput.error @("message", "error_message", "errorMessage", "detail", "hint", "response", "body", "data", "errors")
        $nestedStatusText = Get-FirstTextField $hookInput.error @("status", "status_code", "statusCode", "http_status")
        $nestedRetryAfter = Get-FirstTextField $hookInput.error @("retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After")
        if (-not [string]::IsNullOrWhiteSpace($nestedCode)) {{ $errorCode = $nestedCode }}
        if (-not [string]::IsNullOrWhiteSpace($nestedMessage)) {{ $errorMessage = $nestedMessage }}
        if (-not [string]::IsNullOrWhiteSpace($nestedStatusText)) {{
            [int]::TryParse([string]$nestedStatusText, [ref]$httpStatus) | Out-Null
        }}
        if (-not [string]::IsNullOrWhiteSpace($nestedRetryAfter)) {{ $retryAfterText = $nestedRetryAfter }}
        if ($hookInput.error.headers) {{
            $nestedHeaderRetryAfter = Get-FirstTextField $hookInput.error.headers @("retry-after", "Retry-After", "retry_after", "retryAfter")
            if (-not [string]::IsNullOrWhiteSpace($nestedHeaderRetryAfter)) {{ $retryAfterText = $nestedHeaderRetryAfter }}
        }}
    }}

    if ([string]::IsNullOrWhiteSpace($sessionId)) {{
        $sessionId = Get-FirstTextField $hookInput @("transcript_path", "transcriptPath", "cwd")
    }}
    if ([string]::IsNullOrWhiteSpace($sessionId)) {{
        $sessionId = [System.IO.Directory]::GetCurrentDirectory()
    }}

    if ([string]::IsNullOrWhiteSpace($errorCode) -and [string]::IsNullOrWhiteSpace($errorMessage)) {{
        exit 0
    }}

    Write-Log "Codex error detected: [$errorCode] $errorMessage (HTTP $httpStatus)" "INFO"

    # 分类错误
    $errorType = Get-ErrorType -ErrorCode $errorCode -ErrorMessage $errorMessage -HttpStatus $httpStatus
    Write-Log "Error type: $errorType" "INFO"

    # 检查恢复次数
    $stateDir = Get-RecoveryStateDir -SettingsPath $settingsPath
    $statePath = Join-Path $stateDir "error_recovery_state.json"
    $lockPath = "$statePath.lock"
    $lockStream = Acquire-StateLock -LockPath $lockPath
    if ($null -eq $lockStream) {{
        Write-Log "Failed to acquire recovery state lock" "WARN"
        exit 0
    }}

    $maxRecoveriesReached = $false
    try {{
        $state = Load-RecoveryState -Path $statePath

        $recoveryKey = "$sessionId-$errorType"
        $recoveryCount = if ($state.ContainsKey($recoveryKey)) {{ $state[$recoveryKey] }} else {{ 0 }}
        $maxRecoveries = Get-IntSetting -Settings $settings -Name "max_error_recoveries" -Default 3 -Min 0 -Max 10

        if ($recoveryCount -ge $maxRecoveries) {{
            Write-Log "Max recoveries reached for $errorType" "WARN"

            # 记录日志
            $logPath = Join-Path $stateDir "error_recovery_log.jsonl"
            $logEntry = @{{
                timestamp = (Get-Date -Format "o")
                session_id = $sessionId
                error_type = $errorType
                error_code = $errorCode
                error_message = $errorMessage
                http_status = $httpStatus
                action = "max_recoveries_reached"
                recovery_count = $recoveryCount
            }} | ConvertTo-Json -Compress
            Add-Content -LiteralPath $logPath -Value $logEntry -Encoding UTF8 -ErrorAction SilentlyContinue

            $maxRecoveriesReached = $true
        }} else {{

            # 更新恢复次数
            $recoveryCount++
            $state[$recoveryKey] = $recoveryCount
            Save-RecoveryState -Path $statePath -State $state | Out-Null
        }}
    }} finally {{
        Release-StateLock -LockStream $lockStream -LockPath $lockPath
    }}
    if ($maxRecoveriesReached) {{
        exit 0
    }}

    $gitCommitHash = ""

    # 创建Git快照（如果启用）
    if ($gitAutoSnapshot -and $gitSnapshotOnRecovery) {{
        Write-Log "Creating git snapshot before recovery..." "INFO"
        $gitCommitHash = Create-GitSnapshot -Message "codex-error-recovery"
    }}

    # 记录恢复尝试
    $logPath = Join-Path $stateDir "error_recovery_log.jsonl"
    $logEntryData = @{{
        timestamp = (Get-Date -Format "o")
        session_id = $sessionId
        error_type = $errorType
        error_code = $errorCode
        error_message = $errorMessage
        http_status = $httpStatus
        action = "attempting_recovery"
        recovery_count = $recoveryCount
    }}
    if (-not [string]::IsNullOrWhiteSpace($gitCommitHash)) {{
        $logEntryData.git_commit_hash = $gitCommitHash
    }}
    $logEntry = $logEntryData | ConvertTo-Json -Compress
    Add-Content -LiteralPath $logPath -Value $logEntry -Encoding UTF8 -ErrorAction SilentlyContinue

    # 根据错误类型选择恢复策略
    $output = $null

    if ($errorType -eq "content_length") {{
        # Codex CLI 使用 /compress 命令
        $output = @{{
            recover = $true
            commands = @("/compress", "继续")
            userMessage = "对话内容过长，正在自动压缩并继续..."
        }} | ConvertTo-Json
        Write-Log "Recovery: /compress + continue" "INFO"

    }} elseif ($errorType -eq "rate_limit") {{
        # 等待后重试
        $waitSeconds = Get-RetryAfter -ErrorMessage $errorMessage -RetryAfterText $retryAfterText -DefaultSeconds 60 -MaxSeconds 600
        $output = @{{
            recover = $true
            wait = $waitSeconds
            commands = @("继续")
            userMessage = "请求过于频繁，等待 $waitSeconds 秒后重试..."
        }} | ConvertTo-Json
        Write-Log "Recovery: wait $waitSeconds seconds + continue" "INFO"

    }} elseif ($errorType -in @("timeout", "overload", "network", "server")) {{
        # 指数退避
        $backoffSeconds = Get-BackoffSeconds -Attempt $recoveryCount -InitialDelay $retryInitialDelay -MaxDelay $retryMaxDelay
        $commands = @("继续")
        $userMessage = "服务暂时不可用，等待 $backoffSeconds 秒后重试..."
        if ($errorMessage -match "remote compact task|backend-api/codex/responses/compact|responses/compact") {{
            $commands = @("/compress", "继续")
            $userMessage = "压缩任务连接中断，等待 $backoffSeconds 秒后重新压缩并继续..."
        }}
        $output = @{{
            recover = $true
            wait = $backoffSeconds
            commands = $commands
            userMessage = $userMessage
        }} | ConvertTo-Json
        Write-Log "Recovery: backoff $backoffSeconds seconds + continue" "INFO"

    }} elseif ($errorType -in @("auth", "quota", "permission")) {{
        # 通知用户
        $userMsg = switch ($errorType) {{
            "auth" {{ "认证失败，请检查 API 密钥" }}
            "permission" {{ "权限不足，请检查账户权限" }}
            default {{ "配额已用完，请充值" }}
        }}
        $output = @{{
            recover = $false
            notify = $true
            userMessage = $userMsg
        }} | ConvertTo-Json
        Write-Log "Notify user: $userMsg" "INFO"
    }}

    if ($output) {{
        Write-Output $output
    }}

}} catch {{
    Write-Log "Error in Codex recovery hook: $_" "ERROR"
    exit 0
}}

exit 0
'''

    return script
