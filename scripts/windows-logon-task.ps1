#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)]
    [string]$BootstrapSource,
    [string]$TaskName = 'MnemoSeedLocalDaemon',
    [string]$User = $env:USERNAME,
    [switch]$Uninstall,
    [switch]$MigrateLegacyOllamaTask,
    [scriptblock]$TaskLookup,
    [scriptblock]$TaskUnregister,
    [string]$MigrationResult = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskDescription = 'Start one MnemoSeed daemon at user logon; no periodic restart.'
$TaskIdentityUri = 'urn:mnemoseed-local:task:windows-logon-daemon:v1'
$TaskPath = '\MnemoSeedLocal\'

function Get-PropertyValue($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-ZeroExecutionLimit($Value) {
    if ($null -eq $Value) { return $false }
    try {
        return ([TimeSpan]$Value) -eq [TimeSpan]::Zero
    } catch {
        return $false
    }
}

function Test-CurrentUserTask($Task) {
    if ($null -eq $Task) { return $false }
    $principal = Get-PropertyValue $Task 'Principal'
    if ($null -eq $principal) { return $false }
    $principalUser = [string](Get-PropertyValue $principal 'UserId')
    if (($principalUser -ne $User) -and ($principalUser -notmatch "[\\/]$([regex]::Escape($User))$")) { return $false }
    $logonType = [string](Get-PropertyValue $principal 'LogonType')
    if ($logonType -notin @('Interactive', 'InteractiveToken')) { return $false }
    return $true
}

function Test-NoRestartPolicy($Settings) {
    $count = Get-PropertyValue $Settings 'RestartCount'
    $interval = Get-PropertyValue $Settings 'RestartInterval'
    if ($null -eq $count -or $null -eq $interval) { return $false }
    try {
        return ([int]$count -eq 0) -and (([TimeSpan]$interval) -eq [TimeSpan]::Zero)
    } catch {
        return $false
    }
}

function Get-NormalizedBootstrapSource([string]$Source) {
    if ([string]::IsNullOrEmpty($Source)) { return $null }
    return ($Source -replace "`r`n", "`n") -replace "`r", "`n"
}

function Test-OwnedTask($Task, [string]$ExpectedBootstrapSource) {
    if ($null -eq $Task -or @($Task).Count -ne 1) { return $false }
    if (-not (Test-CurrentUserTask $Task)) { return $false }
    $taskPath = [string](Get-PropertyValue $Task 'TaskPath')
    if ($taskPath -ne $TaskPath) { return $false }
    if ((Get-PropertyValue $Task 'Description') -ne $TaskDescription) { return $false }
    $actions = @(Get-PropertyValue $Task 'Actions')
    if ($actions.Count -ne 1 -or (Get-PropertyValue $actions[0] 'Execute') -ne 'powershell.exe') {
        return $false
    }
    $arguments = [string](Get-PropertyValue $actions[0] 'Arguments')
    $escapedIdentity = [regex]::Escape($TaskIdentityUri)
    $match = [regex]::Match(
        $arguments,
        "^-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand (?<source>[A-Za-z0-9+/=]+) -TaskIdentity $escapedIdentity$"
    )
    if (-not $match.Success) { return $false }
    try {
        $decodedSource = [Text.Encoding]::Unicode.GetString(
            [Convert]::FromBase64String($match.Groups['source'].Value)
        )
    } catch {
        return $false
    }
    $expectedSource = Get-NormalizedBootstrapSource $ExpectedBootstrapSource
    if ($null -eq $expectedSource) { return $false }
    return (Get-NormalizedBootstrapSource $decodedSource) -ceq $expectedSource
}

function Test-TaskContract($Task, [string]$ExpectedBootstrapSource) {
    if (-not (Test-OwnedTask $Task $ExpectedBootstrapSource)) { return $false }
    $actions = @(Get-PropertyValue $Task 'Actions')
    $triggers = @(Get-PropertyValue $Task 'Triggers')
    $settings = Get-PropertyValue $Task 'Settings'
    if ($actions.Count -ne 1 -or $triggers.Count -ne 1 -or $null -eq $settings) { return $false }

    $action = $actions[0]
    $expectedArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $script:encodedBootstrap -TaskIdentity $TaskIdentityUri"
    if ((Get-PropertyValue $action 'Arguments') -ne $expectedArguments) { return $false }

    $trigger = $triggers[0]
    $triggerClass = Get-PropertyValue (Get-PropertyValue $trigger 'CimClass') 'CimClassName'
    $triggerUser = [string](Get-PropertyValue $trigger 'UserId')
    if ($triggerClass -ne 'MSFT_TaskLogonTrigger') { return $false }
    if (($triggerUser -ne $User) -and ($triggerUser -notmatch "[\\/]$([regex]::Escape($User))$")) { return $false }

    if ((Get-PropertyValue $settings 'Hidden') -ne $true) { return $false }
    if ((Get-PropertyValue $settings 'MultipleInstances') -ne 'IgnoreNew') { return $false }
    if (-not (Test-ZeroExecutionLimit (Get-PropertyValue $settings 'ExecutionTimeLimit'))) { return $false }
    if (-not (Test-NoRestartPolicy $settings)) { return $false }
    if ((Get-PropertyValue $settings 'AllowStartIfOnBatteries') -ne $true) { return $false }
    if ((Get-PropertyValue $settings 'DontStopIfGoingOnBatteries') -ne $true) { return $false }
    if ((Get-PropertyValue $settings 'DisallowStartIfOnBatteries') -ne $false) { return $false }
    if ((Get-PropertyValue $settings 'Enabled') -ne $true) { return $false }
    if ((Get-PropertyValue $settings 'StartWhenAvailable') -ne $false) { return $false }
    if ((Get-PropertyValue $settings 'WakeToRun') -ne $false) { return $false }
    if ((Get-PropertyValue $settings 'RunOnlyIfNetworkAvailable') -ne $false) { return $false }
    return $true
}

function Test-ExactLegacyOllamaTask($Task) {
    if ($null -eq $Task -or @($Task).Count -ne 1) { return $false }
    $task = @($Task)[0]
    if ((Get-PropertyValue $task 'TaskName') -ne 'OllamaHeadlessServe') { return $false }
    if ((Get-PropertyValue $task 'TaskPath') -ne '\') { return $false }
    if ((Get-PropertyValue $task 'Description') -ne 'Headless ollama API server (mnemoseed-local dream engine): background-only, no desktop app required.') { return $false }
    if (-not (Test-CurrentUserTask $task)) { return $false }
    $triggers = @(Get-PropertyValue $task 'Triggers')
    if ($triggers.Count -ne 1) { return $false }
    $trigger = $triggers[0]
    if ((Get-PropertyValue (Get-PropertyValue $trigger 'CimClass') 'CimClassName') -ne 'MSFT_TaskLogonTrigger') { return $false }
    $triggerUser = [string](Get-PropertyValue $trigger 'UserId')
    if (($triggerUser -ne $User) -and ($triggerUser -notmatch "[\\/]$([regex]::Escape($User))$")) { return $false }
    $actions = @(Get-PropertyValue $task 'Actions')
    if ($actions.Count -ne 1) { return $false }
    $expectedPath = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if ((Get-PropertyValue $actions[0] 'Execute') -ne $expectedPath) { return $false }
    return (Get-PropertyValue $actions[0] 'Arguments') -ceq 'serve'
}

if ($MigrateLegacyOllamaTask) {
    $lookup = if ($null -eq $TaskLookup) { { param($Name) Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue } } else { $TaskLookup }
    $existing = @(& $lookup 'OllamaHeadlessServe')
    if ($existing.Count -eq 0) {
        $migration = @{ Present = $false; Unregistered = $false }
    } elseif (Test-ExactLegacyOllamaTask $existing) {
        $unregister = if ($null -eq $TaskUnregister) { { param($Definition, $Name) Unregister-ScheduledTask -InputObject $Definition -Confirm:$false } } else { $TaskUnregister }
        & $unregister $existing[0] 'OllamaHeadlessServe'
        $migration = @{ Present = $true; Unregistered = $true }
    } else {
        throw "scheduled task OllamaHeadlessServe exists but does not match the exact task created by the prior MnemoSeed installer; it was preserved; inspect it manually and remove or rename it before retrying"
    }
    if (-not [string]::IsNullOrEmpty($MigrationResult)) { $migration | ConvertTo-Json -Compress | Set-Content -LiteralPath $MigrationResult }
    return $migration
}

if (-not (Test-Path -LiteralPath $BootstrapSource -PathType Leaf)) {
    throw "logon bootstrap not found at $BootstrapSource"
}
$script:bootstrapSource = Get-Content -LiteralPath $BootstrapSource -Raw
$script:normalizedBootstrapSource = Get-NormalizedBootstrapSource $script:bootstrapSource
if ($null -eq $script:normalizedBootstrapSource) {
    throw "logon bootstrap is empty at $BootstrapSource"
}
$script:encodedBootstrap = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($script:normalizedBootstrapSource)
)

if ($Uninstall) {
    $existing = @(Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue)
    $sameName = @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
    $outsideOwnedPath = @($sameName | Where-Object {
        ([string](Get-PropertyValue $_ 'TaskPath')) -ne $TaskPath
    })
    if ($outsideOwnedPath.Count -gt 0) {
        $outsidePaths = ($outsideOwnedPath | ForEach-Object { [string](Get-PropertyValue $_ 'TaskPath') }) -join ', '
        throw "scheduled task $TaskName is ambiguous: same-name entries exist outside $TaskPath ($outsidePaths); none were removed"
    }
    if ($existing.Count -eq 0) {
        Write-Host "scheduled task $TaskName is not registered at $TaskPath"
        return
    }
    if ($existing.Count -ne 1) {
        throw "scheduled task $TaskName is ambiguous at $TaskPath; none were removed"
    }
    if (-not (Test-OwnedTask $existing[0] $script:normalizedBootstrapSource)) {
        throw "scheduled task $TaskName at $TaskPath exists but is not provably owned by MnemoSeed; inspect it manually and remove or rename it"
    }
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
    Write-Host "removed current-user MnemoSeed logon task $TaskName"
    return
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $script:encodedBootstrap -TaskIdentity $TaskIdentityUri"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -RestartCount 0 -RestartInterval ([TimeSpan]::Zero)
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not (Test-OwnedTask $existing $script:normalizedBootstrapSource)) {
    throw "scheduled task $TaskName exists but is not provably owned by MnemoSeed; inspect it manually and remove or rename it before retrying"
}
if (Test-TaskContract $existing $script:normalizedBootstrapSource) {
    Write-Host "scheduled task $TaskName already registered with the expected contract"
    return
}

$registration = @{
    TaskName = $TaskName
    TaskPath = $TaskPath
    Action = $action
    Trigger = $trigger
    Settings = $settings
    Description = $TaskDescription
}
if ($null -ne $existing) { $registration.Force = $true }
[void](Register-ScheduledTask @registration)
Write-Host "registered current-user logon task $TaskName (hidden, single instance, no restart policy)"
