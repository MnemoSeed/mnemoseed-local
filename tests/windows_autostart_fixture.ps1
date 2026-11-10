#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)]
    [string]$Bootstrap,
    [Parameter(Mandatory = $true)]
    [ValidateSet('off-before', 'healthy', 'unknown-healthy', 'unstable-pid', 'unknown-port', 'mnemoseed-owned-unhealthy', 'foreign-label', 'mismatched-executable', 'launch')]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$Result
)

$ErrorActionPreference = 'Stop'
$fixtureConfigHome = Join-Path $env:MNEMOSEED_LOCAL_HOME ''
[IO.Directory]::CreateDirectory($fixtureConfigHome) | Out-Null
$state = @{ portProbeCount = 0 }

$pathProbe = {
    param([string]$Path)
    return [IO.File]::Exists($Path)
}
$healthProbe = {
    param([string]$Url)
    if ($Url -notlike '*7788*') { return $false }
    if ($Scenario -in @('healthy', 'unknown-healthy', 'unstable-pid', 'foreign-label', 'mismatched-executable')) { return $true }
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
$fixtureRoot = [IO.Path]::GetTempPath()
$expectedExecutable = [IO.Path]::GetFullPath((Join-Path $fixtureRoot 'MnemoSeed/mnemoseed-local.exe'))
$foreignExecutable = [IO.Path]::GetFullPath((Join-Path $fixtureRoot 'Other/other.exe'))
$foreignProductExecutable = [IO.Path]::GetFullPath((Join-Path $fixtureRoot 'Other/mnemoseed-local.exe'))
$processProbe = {
    param([int]$ProcessId)
    $executable = if ($Scenario -in @('unknown-port')) {
        $foreignExecutable
    } elseif ($Scenario -eq 'mismatched-executable') {
        $foreignProductExecutable
    } elseif ($Scenario -eq 'foreign-label') {
        $foreignExecutable
    } else {
        $expectedExecutable
    }
    $commandLine = if ($Scenario -eq 'foreign-label') {
        '"' + $foreignExecutable + '" --label mnemoseed-local'
    } elseif ($Scenario -eq 'unknown-port') {
        '"' + $foreignExecutable + '" serve'
    } else {
        '"' + $expectedExecutable + '" up'
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

$arguments = @{
    HealthProbe = $healthProbe
    PathProbe = $pathProbe
    PortProbe = $portProbe
    ProcessProbe = $processProbe
    CommandProbe = $commandProbe
    Launcher = $launcher
    TaskIdentity = 'urn:mnemoseed-local:task:windows-logon-daemon:v1'
}
& $Bootstrap @arguments
$exitCode = $LASTEXITCODE
exit $exitCode
