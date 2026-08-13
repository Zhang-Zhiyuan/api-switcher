"""Shared PowerShell runtime guards used by generated local hooks."""


POWERSHELL_HARD_WATCHDOG_HELPER = r'''function Start-ApiSwitcherHookWatchdog {
    param([int]$TimeoutMilliseconds = 25000)

    if ($TimeoutMilliseconds -lt 1) {
        return $null
    }

    try {
        # Emit a tiny pure .NET callback instead of a PowerShell script block.
        # Timer callbacks run on a thread-pool thread without a PowerShell
        # runspace, so a script-block callback would not fire reliably while
        # the main runspace is blocked in synchronous filesystem I/O. A
        # DynamicMethod also avoids the multi-second expression-tree JIT cost
        # seen in Windows PowerShell 5.1.
        $method = New-Object System.Reflection.Emit.DynamicMethod(
            "ApiSwitcherWatchdogKill",
            [void],
            [Type[]]@([object]),
            [object].Module
        )
        $generator = $method.GetILGenerator()
        $getCurrentMethod = [System.Diagnostics.Process].GetMethod(
            "GetCurrentProcess",
            [Type[]]@()
        )
        $killMethod = [System.Diagnostics.Process].GetMethod(
            "Kill",
            [Type[]]@()
        )
        $generator.Emit(
            [System.Reflection.Emit.OpCodes]::Call,
            $getCurrentMethod
        )
        $generator.Emit(
            [System.Reflection.Emit.OpCodes]::Callvirt,
            $killMethod
        )
        $generator.Emit([System.Reflection.Emit.OpCodes]::Ret)
        $callback = [System.Threading.TimerCallback]$method.CreateDelegate(
            [System.Threading.TimerCallback]
        )
        return New-Object System.Threading.Timer(
            $callback,
            $null,
            $TimeoutMilliseconds,
            [System.Threading.Timeout]::Infinite
        )
    } catch {
        Write-Log "Failed to install in-process hook watchdog: $_" "WARN"
        return $null
    }
}'''


POWERSHELL_REDIRECTED_PROCESS_HELPER = r'''function Initialize-ApiSwitcherJobObjectNativeMethods {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return }
    if ($null -ne ("ApiSwitcher.Runtime.JobNative" -as [type])) { return }

    # Reflection.Emit avoids a compiler subprocess and its startup cost in
    # every short-lived hook process.
    $assemblyName = New-Object System.Reflection.AssemblyName(
        "ApiSwitcher.JobObjectNativeMethods"
    )
    if ($PSVersionTable.PSEdition -eq "Core") {
        $assembly = [System.Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly(
            $assemblyName,
            [System.Reflection.Emit.AssemblyBuilderAccess]::Run
        )
    } else {
        $assembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly(
            $assemblyName,
            [System.Reflection.Emit.AssemblyBuilderAccess]::Run
        )
    }
    $module = $assembly.DefineDynamicModule("ApiSwitcher.JobObjectNativeMethods")
    $typeAttributes = [System.Reflection.TypeAttributes](
        [System.Reflection.TypeAttributes]::Public -bor
        [System.Reflection.TypeAttributes]::Sealed -bor
        [System.Reflection.TypeAttributes]::Abstract
    )
    $nativeType = $module.DefineType(
        "ApiSwitcher.Runtime.JobNative",
        $typeAttributes
    )
    $methodAttributes = [System.Reflection.MethodAttributes](
        [System.Reflection.MethodAttributes]::Public -bor
        [System.Reflection.MethodAttributes]::Static -bor
        [System.Reflection.MethodAttributes]::PinvokeImpl
    )
    $preserveSignature = [System.Reflection.MethodImplAttributes]::PreserveSig

    $createJob = $nativeType.DefinePInvokeMethod(
        "CreateJobObjectW",
        "kernel32.dll",
        $methodAttributes,
        [System.Reflection.CallingConventions]::Standard,
        [IntPtr],
        [Type[]]@([IntPtr], [string]),
        [Runtime.InteropServices.CallingConvention]::Winapi,
        [Runtime.InteropServices.CharSet]::Unicode
    )
    $createJob.SetImplementationFlags(
        $createJob.GetMethodImplementationFlags() -bor $preserveSignature
    )
    $setInformation = $nativeType.DefinePInvokeMethod(
        "SetInformationJobObject",
        "kernel32.dll",
        $methodAttributes,
        [System.Reflection.CallingConventions]::Standard,
        [bool],
        [Type[]]@([IntPtr], [int], [IntPtr], [uint32]),
        [Runtime.InteropServices.CallingConvention]::Winapi,
        [Runtime.InteropServices.CharSet]::None
    )
    $setInformation.SetImplementationFlags(
        $setInformation.GetMethodImplementationFlags() -bor $preserveSignature
    )
    $assignProcess = $nativeType.DefinePInvokeMethod(
        "AssignProcessToJobObject",
        "kernel32.dll",
        $methodAttributes,
        [System.Reflection.CallingConventions]::Standard,
        [bool],
        [Type[]]@([IntPtr], [IntPtr]),
        [Runtime.InteropServices.CallingConvention]::Winapi,
        [Runtime.InteropServices.CharSet]::None
    )
    $assignProcess.SetImplementationFlags(
        $assignProcess.GetMethodImplementationFlags() -bor $preserveSignature
    )
    [void]$nativeType.CreateType()
}

function New-ApiSwitcherKillOnCloseJob {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        return $null
    }

    Initialize-ApiSwitcherJobObjectNativeMethods
    $nativeHandle = [ApiSwitcher.Runtime.JobNative]::CreateJobObjectW(
        [IntPtr]::Zero,
        $null
    )
    if ($nativeHandle -eq [IntPtr]::Zero) {
        throw New-Object System.ComponentModel.Win32Exception(
            [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
            "Could not create child-process Job Object"
        )
    }

    $safeHandle = New-Object Microsoft.Win32.SafeHandles.SafeFileHandle(
        $nativeHandle,
        $true
    )
    $informationPointer = [IntPtr]::Zero
    try {
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on x64 and 112 on
        # x86. LimitFlags is a DWORD at byte offset 16 in both layouts.
        $informationLength = if ([IntPtr]::Size -eq 8) { 144 } else { 112 }
        $informationPointer = [Runtime.InteropServices.Marshal]::AllocHGlobal(
            $informationLength
        )
        $zeroes = New-Object byte[] $informationLength
        [Runtime.InteropServices.Marshal]::Copy(
            $zeroes,
            0,
            $informationPointer,
            $informationLength
        )
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        [Runtime.InteropServices.Marshal]::WriteInt32(
            $informationPointer,
            16,
            0x00002000
        )
        $configured = [ApiSwitcher.Runtime.JobNative]::SetInformationJobObject(
            $safeHandle.DangerousGetHandle(),
            9,
            $informationPointer,
            $informationLength
        )
        if (-not $configured) {
            throw New-Object System.ComponentModel.Win32Exception(
                [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
                "Could not configure child-process Job Object"
            )
        }
        return $safeHandle
    } catch {
        try { $safeHandle.Dispose() } catch { }
        throw
    } finally {
        if ($informationPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($informationPointer)
        }
    }
}

function ConvertTo-ApiSwitcherNativeArgument {
    param([AllowNull()][string]$Argument)

    # ProcessStartInfo.ArgumentList is unavailable on Windows PowerShell 5.1's
    # .NET Framework. Implement the CommandLineToArgvW quoting rules so every
    # argument remains a single native argv entry on both supported runtimes.
    if ($null -eq $Argument -or $Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }

    $quoted = New-Object System.Text.StringBuilder
    [void]$quoted.Append([char]34)
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            if ($backslashes -gt 0) {
                [void]$quoted.Append((('\' * (2 * $backslashes)) -join ''))
            }
            [void]$quoted.Append([char]92)
            [void]$quoted.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$quoted.Append((('\' * $backslashes) -join ''))
            $backslashes = 0
        }
        [void]$quoted.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$quoted.Append((('\' * (2 * $backslashes)) -join ''))
    }
    [void]$quoted.Append([char]34)
    return $quoted.ToString()
}

function Start-ApiSwitcherRedirectedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [hashtable]$EnvironmentOverrides = @{},
        [string]$WorkingDirectory = ""
    )

    $jobHandle = New-ApiSwitcherKillOnCloseJob
    if (
        [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and
        $null -eq $jobHandle
    ) {
        throw "A managed child process cannot start without a Job Object"
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $nativeArguments = @(
        foreach ($argument in $Arguments) {
            ConvertTo-ApiSwitcherNativeArgument -Argument $argument
        }
    )
    $startInfo.Arguments = [string]::Join(' ', [string[]]$nativeArguments)
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $startInfo.WorkingDirectory = $WorkingDirectory
    }
    foreach ($name in $EnvironmentOverrides.Keys) {
        $startInfo.EnvironmentVariables[[string]$name] = [string]$EnvironmentOverrides[$name]
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $started = $false
    try {
        if (-not $process.Start()) {
            throw "Process.Start returned false for $FilePath"
        }
        $started = $true
        if ($null -ne $jobHandle) {
            $assigned = [ApiSwitcher.Runtime.JobNative]::AssignProcessToJobObject(
                $jobHandle.DangerousGetHandle(),
                $process.Handle
            )
            if (-not $assigned) {
                throw New-Object System.ComponentModel.Win32Exception(
                    [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
                    "Could not assign child process to its Job Object"
                )
            }
        }
        # Begin both reads before waiting. Reading them sequentially after the
        # child exits can deadlock once either OS pipe buffer becomes full.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        return [pscustomobject]@{
            Process = $process
            StdoutTask = $stdoutTask
            StderrTask = $stderrTask
            JobHandle = $jobHandle
        }
    } catch {
        if ($started) {
            try { $process.Kill() } catch { }
            try { $process.WaitForExit(250) | Out-Null } catch { }
        }
        try { if ($null -ne $jobHandle) { $jobHandle.Dispose() } } catch { }
        try { $process.Dispose() } catch { }
        throw
    }
}

function Read-ApiSwitcherRedirectedStream {
    param(
        $Task,
        [int]$TimeoutMilliseconds = 250
    )

    if ($null -eq $Task) {
        return [pscustomobject]@{ Succeeded = $false; Output = "" }
    }
    try {
        if ($Task.IsCompleted -or $Task.Wait([Math]::Max(0, $TimeoutMilliseconds))) {
            return [pscustomobject]@{
                Succeeded = $true
                Output = [string]$Task.GetAwaiter().GetResult()
            }
        }
        Write-Log "Timed out draining redirected process output" "WARN"
    } catch {
        Write-Log "Could not read redirected process output: $_" "WARN"
    }
    return [pscustomobject]@{ Succeeded = $false; Output = "" }
}

function Get-ApiSwitcherRedirectedOutput {
    param(
        $Invocation,
        [int]$TimeoutMilliseconds = 250
    )

    if ($null -eq $Invocation) {
        return [pscustomobject]@{
            Succeeded = $false
            Stdout = ""
            Stderr = ""
        }
    }
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $stdoutResult = Read-ApiSwitcherRedirectedStream `
        -Task $Invocation.StdoutTask `
        -TimeoutMilliseconds $TimeoutMilliseconds
    $remaining = [Math]::Max(
        0,
        $TimeoutMilliseconds - [int]$stopwatch.ElapsedMilliseconds
    )
    $stderrResult = Read-ApiSwitcherRedirectedStream `
        -Task $Invocation.StderrTask `
        -TimeoutMilliseconds $remaining
    return [pscustomobject]@{
        Succeeded = [bool]($stdoutResult.Succeeded -and $stderrResult.Succeeded)
        Stdout = [string]$stdoutResult.Output
        Stderr = [string]$stderrResult.Output
    }
}

function Close-ApiSwitcherRedirectedProcess {
    param($Invocation)

    if ($null -eq $Invocation) { return }
    # Closing the KILL_ON_JOB_CLOSE handle first synchronously terminates any
    # descendant that still owns an inherited pipe writer. The subsequent
    # bounded drain therefore cannot be held open by an escaped grandchild.
    try {
        if ($null -ne $Invocation.JobHandle) {
            $Invocation.JobHandle.Dispose()
        }
    } catch { }
    Get-ApiSwitcherRedirectedOutput -Invocation $Invocation -TimeoutMilliseconds 250 | Out-Null
    try { $Invocation.Process.StandardOutput.Dispose() } catch { }
    try { $Invocation.Process.StandardError.Dispose() } catch { }
    try { $Invocation.Process.Dispose() } catch { }
}'''
