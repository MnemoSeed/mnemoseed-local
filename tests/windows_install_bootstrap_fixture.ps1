#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$HelperModule,
    [Parameter(Mandatory = $true)][string]$LogonScript,
    [Parameter(Mandatory = $true)][string]$TaskScript,
    [Parameter(Mandatory = $true)][ValidateSet('remote-success', 'local-siblings', 'module-syntax-failure', 'partial-download')][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Root
)

$ErrorActionPreference = 'Stop'
$env:TEMP = $Root
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "installer parse failed: $($errors[0].Message)" }
$definitions = $ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in @('Assert-PowerShellSyntax', 'Resolve-MnemoSeedHelperModule', 'Initialize-MnemoSeedHelperModule')
}, $true)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }

$scriptRoot = ''
$siblingRoot = Join-Path $Root 'remote-siblings'
$staging = Join-Path $Root ("staging-{0}" -f [guid]::NewGuid().ToString('N'))
$events = [Collections.Generic.List[string]]::new()
$downloader = {
    param([string]$Uri, [string]$OutFile)
    $events.Add("download:$Uri")
    if ($Scenario -eq 'module-syntax-failure' -and $Uri.EndsWith('windows-install-helpers.ps1')) {
        [IO.File]::WriteAllText($OutFile, 'function {')
        return
    }
    if ($Scenario -eq 'partial-download' -and $Uri.EndsWith('windows-logon-task.ps1')) {
        [IO.File]::WriteAllText($OutFile, 'function {')
        throw 'partial helper download'
    }
    $source = if ($Uri.EndsWith('windows-install-helpers.ps1')) { $HelperModule }
        elseif ($Uri.EndsWith('windows-logon.ps1')) { $LogonScript }
        else { $TaskScript }
    [IO.File]::Copy($source, $OutFile, $true)
}
$validator = {
    param([string]$Path)
    $events.Add("parse:$Path")
    $parseTokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($Path, [ref]$parseTokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw "invalid helper syntax: $Path" }
}
$success = $false
$errorMessage = ''
$modulePath = ''
$activeStaging = $staging
try {
    if ($Scenario -eq 'local-siblings') {
        $scriptRoot = Join-Path $Root 'local'
        $siblingRoot = Join-Path $scriptRoot 'scripts'
        [void][IO.Directory]::CreateDirectory($siblingRoot)
        [IO.File]::Copy($HelperModule, (Join-Path $siblingRoot 'windows-install-helpers.ps1'), $true)
        [IO.File]::Copy($LogonScript, (Join-Path $siblingRoot 'windows-logon.ps1'), $true)
        [IO.File]::Copy($TaskScript, (Join-Path $siblingRoot 'windows-logon-task.ps1'), $true)
    }
    $modulePath = Initialize-MnemoSeedHelperModule -ScriptRoot $scriptRoot `
        -BaseUri 'https://example.invalid/scripts/' -Downloader $downloader -SyntaxValidator $validator
    $activeStaging = Split-Path -Parent $modulePath
    . $modulePath
    $resolved = Resolve-MnemoSeedTaskHelpers -SiblingRoot $siblingRoot -StagingRoot $staging `
        -BaseUri 'https://example.invalid/scripts/' -Downloader $downloader
    $events.Add("resolved:$($resolved.BootstrapSource)")
    $events.Add("resolved:$($resolved.TaskRegistration)")
    $registration = {
        param([string]$Path, [hashtable]$Arguments)
        $null = $events.Add("register:$($Arguments.TaskName)")
    }
    $lookup = {
        param([string]$Name)
        $null = $events.Add("lookup:$Name")
        return
    }
    $unregister = { param($Definition, [string]$Name) $null = $events.Add("unregister:$Name") }
    Invoke-MnemoSeedTaskInstallation -TaskScript $resolved.TaskRegistration -BootstrapSource $resolved.BootstrapSource `
        -TaskName 'MnemoSeedLocalDaemon' -User $env:USERNAME -TaskLookup $lookup -TaskUnregister $unregister `
        -TaskInvoker $registration
    $success = $true
} catch {
    $errorMessage = $_.Exception.Message
} finally {
    foreach ($path in @($staging, $activeStaging)) {
        if ((Test-Path -LiteralPath $path) -and ($path -ne $siblingRoot)) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
}
[pscustomobject]@{
    Success = $success
    Error = $errorMessage
    ModulePath = $modulePath
    Events = @($events)
    StagingExists = (Test-Path -LiteralPath $staging)
} | ConvertTo-Json -Compress -Depth 4
