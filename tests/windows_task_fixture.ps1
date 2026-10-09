#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskScript,
    [Parameter(Mandatory = $true)]
    [string]$Bootstrap,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)]
    [ValidateSet('missing', 'correct', 'drift', 'malformed', 'foreign', 'foreign-payload', 'owned-settings-drift', 'uninstall-owned', 'uninstall-foreign', 'uninstall-ambiguous')]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$Result,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$user = $User
$taskIdentity = 'urn:mnemoseed-local:task:windows-logon-daemon:v1'
$taskDescription = 'Start one MnemoSeed daemon at user logon; no periodic restart.'
$bootstrapSource = (Get-Content -LiteralPath $Bootstrap -Raw) -replace "`r`n", "`n" -replace "`r", "`n"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrapSource))
$expectedArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded -TaskIdentity $taskIdentity"
$ownedAction = [pscustomobject]@{ Execute = 'powershell.exe'; Arguments = $expectedArguments }
$expectedTrigger = [pscustomobject]@{
    CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }
    UserId = $user
}
$expectedSettings = [pscustomobject]@{
    Hidden = $true
    MultipleInstances = 'IgnoreNew'
    ExecutionTimeLimit = [TimeSpan]::Zero
    RestartCount = 0
    RestartInterval = [TimeSpan]::Zero
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    DisallowStartIfOnBatteries = $false
    Enabled = $true
    StartWhenAvailable = $false
    WakeToRun = $false
    RunOnlyIfNetworkAvailable = $false
}

function New-OwnedTask([string]$TaskPath, $Action, $Settings) {
    return [pscustomobject]@{
        TaskPath = $TaskPath
        Actions = @($Action)
        Triggers = @($expectedTrigger)
        Principal = [pscustomobject]@{ UserId = $user; LogonType = 'Interactive' }
        Settings = $Settings
        Description = $taskDescription
    }
}
function New-ForeignPayloadTask([string]$TaskPath) {
    $foreignEncoded = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes('Write-Output "foreign task payload"')
    )
    $foreignArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $foreignEncoded -TaskIdentity $taskIdentity"
    return New-OwnedTask $TaskPath ([pscustomobject]@{
        Execute = 'powershell.exe'
        Arguments = $foreignArguments
    }) $expectedSettings
}

function Get-ScheduledTask {
    [CmdletBinding()]
    param(
        [string]$TaskName,
        [string]$TaskPath
    )
    $ownedPath = '\MnemoSeedLocal\'
    if ($Uninstall -and [string]::IsNullOrEmpty($TaskPath)) {
        $tasks = @()
        if ($Scenario -eq 'uninstall-ambiguous') {
            $tasks += New-ForeignPayloadTask '\OtherProduct\'
        }
        if ($Scenario -ne 'uninstall-missing') {
            if ($Scenario -eq 'uninstall-foreign') {
                $tasks += New-ForeignPayloadTask $ownedPath
            } else {
                $tasks += New-OwnedTask $ownedPath $ownedAction $expectedSettings
            }
        }
        return $tasks
    }
    if ($Scenario -eq 'missing' -or $Scenario -eq 'uninstall-missing') { return $null }
    if ($TaskPath -ne $ownedPath) { return $null }
    if ($Scenario -in @('correct', 'uninstall-owned', 'uninstall-ambiguous')) {
        return New-OwnedTask $TaskPath $ownedAction $expectedSettings
    }
    if ($Scenario -in @('drift', 'owned-settings-drift')) {
        $settings = if ($Scenario -eq 'owned-settings-drift') {
            [pscustomobject]@{
                Hidden = $false
                MultipleInstances = 'Parallel'
                ExecutionTimeLimit = [TimeSpan]::FromMinutes(1)
                RestartCount = 3
                RestartInterval = [TimeSpan]::FromMinutes(1)
                AllowStartIfOnBatteries = $false
                DontStopIfGoingOnBatteries = $false
                DisallowStartIfOnBatteries = $true
                Enabled = $true
                StartWhenAvailable = $true
                WakeToRun = $true
                RunOnlyIfNetworkAvailable = $true
            }
        } else {
            [pscustomobject]@{ Hidden = $false; MultipleInstances = 'Parallel'; ExecutionTimeLimit = [TimeSpan]::FromMinutes(1); RestartCount = 0; RestartInterval = [TimeSpan]::Zero }
        }
        return New-OwnedTask $TaskPath $ownedAction $settings
    }
    if ($Scenario -in @('foreign-payload', 'uninstall-foreign')) {
        return New-ForeignPayloadTask $TaskPath
    }
    if ($Scenario -eq 'foreign') {
        return [pscustomobject]@{
            TaskPath = $TaskPath
            Actions = @([pscustomobject]@{ Execute = 'other.exe'; Arguments = 'run' })
            Triggers = @($expectedTrigger)
            Principal = [pscustomobject]@{ UserId = $user; LogonType = 'Interactive' }
            Settings = $expectedSettings
            Description = 'A foreign product task.'
        }
    }
    return [pscustomobject]@{ TaskPath = $TaskPath; Actions = 'malformed'; Triggers = $null; Settings = $null; Principal = $null; Description = 'foreign' }
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument)
    return [pscustomobject]@{ Execute = $Execute; Arguments = $Argument }
}
function New-ScheduledTaskTrigger {
    param([string]$User)
    return [pscustomobject]@{
        CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }
        UserId = $User
    }
}
function New-ScheduledTaskSettingsSet {
    param(
        [switch]$Hidden,
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [TimeSpan]$ExecutionTimeLimit,
        [string]$MultipleInstances,
        [int]$RestartCount,
        [TimeSpan]$RestartInterval
    )
    return [pscustomobject]@{
        Hidden = [bool]$Hidden
        MultipleInstances = $MultipleInstances
        ExecutionTimeLimit = $ExecutionTimeLimit
        RestartCount = $RestartCount
        RestartInterval = $RestartInterval
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        DisallowStartIfOnBatteries = $false
        Enabled = $true
        StartWhenAvailable = $false
        WakeToRun = $false
        RunOnlyIfNetworkAvailable = $false
    }
}
function Register-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$TaskPath,
        $Action,
        $Trigger,
        $Settings,
        [string]$Description,
        [switch]$Force
    )
    $record = [pscustomobject]@{
        TaskName = $TaskName
        TaskPath = $TaskPath
        Force = [bool]$Force
        Execute = $Action.Execute
        Arguments = $Action.Arguments
        Trigger = $Trigger.UserId
        Hidden = $Settings.Hidden
        MultipleInstances = $Settings.MultipleInstances
        ExecutionTimeLimit = $Settings.ExecutionTimeLimit.ToString()
        Description = $Description
    }
    $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $Result -Encoding UTF8
}
function Unregister-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [switch]$Confirm)
    Set-Content -LiteralPath $Result -Value ('{"Uninstalled":true,"TaskPath":"' + $TaskPath + '"}')
}

$arguments = @{
    BootstrapSource = $Bootstrap
    TaskName = 'MnemoSeedLocalDaemon'
    User = $user
}
if ($Uninstall) { $arguments.Uninstall = $true }
& $TaskScript @arguments
