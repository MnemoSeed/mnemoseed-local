#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Helpers,
    [Parameter(Mandatory = $true)][string]$TaskScript,
    [Parameter(Mandatory = $true)][string]$Bootstrap,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$LocalAppData,
    [Parameter(Mandatory = $true)][ValidateSet('registered')][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Result
)

$ErrorActionPreference = 'Stop'
. $Helpers
$events = [Collections.Generic.List[object]]::new()
$invoke = {
    param([string]$Path, [hashtable]$Arguments)
    $events.Add([pscustomobject]@{ Call = 'invoke'; TaskName = $Arguments['TaskName'] })
}
try {
    Invoke-MnemoSeedTaskInstallation -TaskScript $TaskScript -BootstrapSource $Bootstrap `
        -TaskName 'MnemoSeedLocalDaemon' -User $User -TaskInvoker $invoke
} finally {
    $events | ConvertTo-Json -Compress | Set-Content -LiteralPath $Result
}
