#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$TaskScript,
    [Parameter(Mandatory = $true)][string]$Bootstrap,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$LocalAppData,
    [Parameter(Mandatory = $true)][ValidateSet('owned', 'foreign', 'drifted')][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Result
)

$ErrorActionPreference = 'Stop'
$action = [pscustomobject]@{ Execute = (Join-Path $LocalAppData 'Programs\Ollama\ollama.exe'); Arguments = 'serve' }
$principal = [pscustomobject]@{ UserId = $User; LogonType = 'Interactive' }
$trigger = [pscustomobject]@{ CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }; UserId = $User }
$task = [pscustomobject]@{
    TaskName = 'OllamaHeadlessServe'
    TaskPath = '\'
    Description = 'Headless ollama API server (mnemoseed-local dream engine): background-only, no desktop app required.'
    Principal = $principal
    Triggers = @($trigger)
    Actions = @($action)
    Settings = [pscustomobject]@{ MultipleInstances = 'IgnoreNew' }
}
if ($Scenario -eq 'foreign') { $task.Description = 'Unrelated Ollama maintenance task.' }
if ($Scenario -eq 'drifted') { $task.Actions[0].Arguments = 'serve --verbose' }
$unregistered = $false
$lookup = { param([string]$Name) if ($Name -eq 'OllamaHeadlessServe') { return $task }; return $null }
$unregister = { param($Definition, [string]$Name) $script:unregistered = $true }
& $TaskScript -BootstrapSource $Bootstrap -TaskName 'MnemoSeedLocalDaemon' -User $User -MigrateLegacyOllamaTask -TaskLookup $lookup -TaskUnregister $unregister -MigrationResult $Result
exit 0
