#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)]
    [string]$Bootstrap,
    [Parameter(Mandatory = $true)]
    [ValidateSet('off-before', 'off-during', 'healthy', 'unknown-healthy', 'unstable-pid', 'ollama-timeout', 'unknown-port', 'mnemoseed-owned-unhealthy', 'foreign-label', 'mismatched-executable', 'launch')]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$Result
)

$ErrorActionPreference = 'Stop'
$fixtureConfigHome = Join-Path $env:MNEMOSEED_LOCAL_HOME ''
[IO.Directory]::CreateDirectory($fixtureConfigHome) | Out-Null
$marked = $false
$state = @{ portProbeCount = 0 }

$pathProbe = {
    param([string]$Path)
    return [IO.File]::Exists($Path)
}
$healthProbe = {
    param([string]$Url)
    if ($Scenario -eq 'ollama-timeout') { return $false }
    if ($Scenario -eq 'off-during' -and $Url -like '*11434*' -and -not $marked) {
        $marked = $true
        [IO.File]::WriteAllText((Join-Path $env:MNEMOSEED_LOCAL_HOME 'daemon.off'), 'off')
        [IO.File]::WriteAllText($Result, 'marked')
    }
    if ($Scenario -eq 'healthy') { return $true }
    if ($Scenario -eq 'unknown-healthy') { return $true }
    if ($Scenario -in @('unstable-pid', 'foreign-label', 'mismatched-executable') -and $Url -like '*7788*') { return $true }
    if ($Url -like '*11434*') { return $true }
    if ($Scenario -eq 'mnemoseed-owned-unhealthy' -or $Scenario -eq 'unknown-port') { return $false }
    return $false
}
$portProbe = {
    param([int]$Port)
    if ($Scenario -eq 'unknown-port' -or $Scenario -eq 'mnemoseed-owned-unhealthy') {
        return [pscustomobject]@{ OwningProcess = 4242 }
    }
    if ($Scenario -in @('healthy', 'unstable-pid', 'foreign-label', 'mismatched-executable')) {
        $state.portProbeCount++
        if ($Scenario -eq 'unstable-pid' -and $state.portProbeCount -gt 1) {
            return [pscustomobject]@{ OwningProcess = 4343 }
        }
        return [pscustomobject]@{ OwningProcess = 4242 }
    }
    return $null
}
$expectedExecutable = 'C:\Program Files\MnemoSeed\mnemoseed-local.exe'
$processProbe = {
    param([int]$ProcessId)
    $executable = if ($Scenario -in @('unknown-port')) {
        'C:\Other\other.exe'
    } elseif ($Scenario -eq 'mismatched-executable') {
        'C:\Other\mnemoseed-local.exe'
    } elseif ($Scenario -eq 'foreign-label') {
        'C:\Other\other.exe'
    } else {
        $expectedExecutable
    }
    $commandLine = if ($Scenario -eq 'foreign-label') {
        'C:\Other\other.exe --label mnemoseed-local'
    } elseif ($Scenario -eq 'unknown-port') {
        'C:\Other\other.exe serve'
    } else {
        '"C:\Program Files\MnemoSeed\mnemoseed-local.exe" up'
    }
    return [pscustomobject]@{
        ProcessId = $ProcessId
        ExecutablePath = $executable
        CommandLine = $commandLine
    }
}
$commandProbe = { param([string]$Name) return $expectedExecutable }
$launcher = {
    [IO.File]::AppendAllText($Result, "launch`n")
    if ($Scenario -eq 'launch') { Start-Sleep -Milliseconds 1500 }
}
$sleeper = { param([int]$Milliseconds) }

$arguments = @{
    HealthProbe = $healthProbe
    PathProbe = $pathProbe
    PortProbe = $portProbe
    ProcessProbe = $processProbe
    CommandProbe = $commandProbe
    Launcher = $launcher
    TaskIdentity = 'urn:mnemoseed-local:task:windows-logon-daemon:v1'
    Sleeper = $sleeper
}
if ($Scenario -eq 'ollama-timeout') { $arguments.OllamaTimeoutSeconds = 0 }
& $Bootstrap @arguments
$exitCode = $LASTEXITCODE
exit $exitCode
