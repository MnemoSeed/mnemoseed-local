#Requires -Version 5.1
param(
    [int]$OllamaTimeoutSeconds = 30,
    [scriptblock]$HealthProbe,
    [scriptblock]$PathProbe,
    [scriptblock]$PortProbe,
    [scriptblock]$ProcessProbe,
    [scriptblock]$CommandProbe,
    [scriptblock]$Launcher,
    [scriptblock]$Sleeper,
    [string]$TaskIdentity
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskIdentityUri = 'urn:mnemoseed-local:task:windows-logon-daemon:v1'
if ($TaskIdentity -ne $TaskIdentityUri) {
    throw 'MnemoSeed logon start: missing or malformed scheduled-task identity.'
}

$ConfigHome = if ([string]::IsNullOrEmpty($env:MNEMOSEED_LOCAL_HOME)) {
    Join-Path $env:USERPROFILE '.mnemoseed-local'
} else {
    $env:MNEMOSEED_LOCAL_HOME
}
$DisabledMarker = Join-Path $ConfigHome 'daemon.off'
$OllamaUrl = 'http://127.0.0.1:11434'
$DaemonUrl = 'http://127.0.0.1:7788'

function Get-PropertyValue($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-DaemonDisabled {
    if ($null -ne $PathProbe) {
        return [bool](& $PathProbe $DisabledMarker)
    }
    return Test-Path -LiteralPath $DisabledMarker
}

function Test-Health([string]$Url) {
    if ($null -ne $HealthProbe) {
        return [bool](& $HealthProbe $Url)
    }
    try {
        $null = Invoke-RestMethod -Uri $Url -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

function Test-OllamaReady {
    return Test-Health "$OllamaUrl/api/tags"
}

function Test-DaemonHealthy {
    return Test-Health "$DaemonUrl/healthz"
}

function Get-Listener([int]$Port) {
    if ($null -ne $PortProbe) {
        return & $PortProbe $Port
    }
    return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-ProcessDetails([int]$ProcessId) {
    if ($null -ne $ProcessProbe) {
        return & $ProcessProbe $ProcessId
    }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Get-CanonicalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        return [IO.Path]::GetFullPath($Path)
    } catch {
        return $null
    }
}

function Get-ExpectedExecutable {
    if ($null -ne $CommandProbe) {
        return Get-CanonicalPath ([string](& $CommandProbe 'mnemoseed-local'))
    }
    $commands = @(Get-Command -Name 'mnemoseed-local' -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -ne 1) { return $null }
    $path = Get-PropertyValue $commands[0] 'Source'
    if ([string]::IsNullOrWhiteSpace($path)) {
        $path = Get-PropertyValue $commands[0] 'Path'
    }
    return Get-CanonicalPath ([string]$path)
}

function Test-MnemoseedDaemonProcess($Process, [int]$ProcessId, [string]$ExpectedExecutable) {
    if ($null -eq $Process -or [string]::IsNullOrWhiteSpace($ExpectedExecutable)) { return $false }
    if ([int](Get-PropertyValue $Process 'ProcessId') -ne $ProcessId) { return $false }
    $actualExecutable = Get-CanonicalPath ([string](Get-PropertyValue $Process 'ExecutablePath'))
    if ($null -eq $actualExecutable -or
        -not $actualExecutable.Equals($ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $commandLine = [string](Get-PropertyValue $Process 'CommandLine')
    $escapedExecutable = [regex]::Escape($ExpectedExecutable)
    return $commandLine -match "(?i)^(`"$escapedExecutable`"|$escapedExecutable) up$"
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\MnemoSeedLocalLogonStart')
$hasLock = $false
try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) { exit 0 }
    if (Test-DaemonDisabled) { exit 0 }

    if (-not (Test-OllamaReady)) {
        $deadline = (Get-Date).AddSeconds($OllamaTimeoutSeconds)
        while ((Get-Date) -lt $deadline -and -not (Test-OllamaReady)) {
            if ($null -ne $Sleeper) {
                & $Sleeper 500
            } else {
                Start-Sleep -Milliseconds 500
            }
        }
    }
    if (-not (Test-OllamaReady)) {
        [Console]::Error.WriteLine(
            "MnemoSeed logon start: Ollama API is unavailable at $OllamaUrl; starting the MnemoSeed daemon anyway. Dream work will defer until Ollama is ready."
        )
    }
    if (Test-DaemonDisabled) { exit 0 }
    $daemonListener = Get-Listener 7788
    if ($null -ne $daemonListener) {
        $pidBefore = [int](Get-PropertyValue $daemonListener 'OwningProcess')
        $expectedExecutable = Get-ExpectedExecutable
        $process = Get-ProcessDetails $pidBefore
        if (-not (Test-MnemoseedDaemonProcess $process $pidBefore $expectedExecutable)) {
            [Console]::Error.WriteLine('MnemoSeed logon start: unknown owner occupies daemon port 7788.')
            exit 1
        }
        if (-not (Test-DaemonHealthy)) {
            [Console]::Error.WriteLine('MnemoSeed logon start: daemon process exists but health check failed.')
            exit 1
        }
        $daemonListenerAfter = Get-Listener 7788
        if ($null -eq $daemonListenerAfter) {
            [Console]::Error.WriteLine('MnemoSeed logon start: daemon listener disappeared during health check.')
            exit 1
        }
        $pidAfter = [int](Get-PropertyValue $daemonListenerAfter 'OwningProcess')
        if ($pidAfter -ne $pidBefore) {
            [Console]::Error.WriteLine('MnemoSeed logon start: daemon listener changed PID during health check.')
            exit 1
        }
        exit 0
    }
    if (Test-DaemonHealthy) {
        [Console]::Error.WriteLine('MnemoSeed logon start: healthy response has no inspectable listener PID.')
        exit 1
    }

    if ($null -ne $Launcher) {
        & $Launcher
        exit 0
    }
    & mnemoseed-local up
    exit $LASTEXITCODE
} finally {
    if ($hasLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
