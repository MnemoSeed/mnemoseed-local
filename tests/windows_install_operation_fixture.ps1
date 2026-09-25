#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Helpers,
    [Parameter(Mandatory = $true)][string]$TaskScript,
    [Parameter(Mandatory = $true)][string]$Bootstrap,
    [Parameter(Mandatory = $true)][ValidateSet('owned', 'missing', 'foreign', 'drifted')][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Result
)

$ErrorActionPreference = 'Stop'
. $Helpers
$action = [pscustomobject]@{ Execute = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"; Arguments = 'serve' }
$legacyTask = [pscustomobject]@{
    TaskName = 'OllamaHeadlessServe'
    TaskPath = '\'
    Description = 'Headless ollama API server (mnemoseed-local dream engine): background-only, no desktop app required.'
    Principal = [pscustomobject]@{ UserId = $env:USERNAME; LogonType = 'Interactive' }
    Triggers = @([pscustomobject]@{ CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }; UserId = $env:USERNAME })
    Actions = @($action)
}
if ($Scenario -eq 'missing') { $legacyTask = $null }
if ($Scenario -eq 'foreign') { $legacyTask.Description = 'Unrelated task.' }
if ($Scenario -eq 'drifted') { $legacyTask.Actions[0].Arguments = 'serve --verbose' }

$events = [Collections.Generic.List[object]]::new()
$lookup = {
    param($name)
    $null = $events.Add([pscustomobject]@{ Call = 'lookup'; Migrate = $true; Task = $legacyTask })
    if ($null -eq $legacyTask) { return }
    return $legacyTask
}
$unregister = {
    param($definition, $name)
    $events.Add([pscustomobject]@{ Call = 'unregister'; Name = $name })
}
$invoke = {
    param([string]$Path, [hashtable]$Arguments)
    $events.Add([pscustomobject]@{ Call = 'invoke'; Migrate = $Arguments.ContainsKey('MigrateLegacyOllamaTask') })
    if ($null -eq $legacyTask) { return }
    if ($legacyTask.Description -ne 'Headless ollama API server (mnemoseed-local dream engine): background-only, no desktop app required.' -or
        $legacyTask.Actions[0].Arguments -ne 'serve') { throw 'legacy task was preserved' }
}
try {
    Invoke-MnemoSeedTaskInstallation -TaskScript $TaskScript -BootstrapSource $Bootstrap `
        -TaskName 'MnemoSeedLocalDaemon' -User $env:USERNAME -TaskLookup $lookup `
        -TaskUnregister $unregister -TaskInvoker $invoke
} finally {
    $events | ConvertTo-Json -Compress | Set-Content -LiteralPath $Result
}
