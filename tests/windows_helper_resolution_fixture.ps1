#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Helpers,
    [Parameter(Mandatory = $true)][ValidateSet('success', 'invalid', 'second-download-fails', 'siblings')][string]$Scenario,
    [Parameter(Mandatory = $true)][string]$Root
)
$ErrorActionPreference = 'Stop'
. $Helpers
$siblings = Join-Path $Root 'siblings'
$staging = Join-Path $Root 'staging'
[void][IO.Directory]::CreateDirectory($siblings)
if ($Scenario -eq 'siblings') {
    [IO.File]::WriteAllText((Join-Path $siblings 'windows-logon.ps1'), "'local bootstrap'")
    [IO.File]::WriteAllText((Join-Path $siblings 'windows-logon-task.ps1'), "'local registrar'")
}
$downloads = [Collections.Generic.List[string]]::new()
$downloader = {
    param([string]$Uri, [string]$OutFile)
    $downloads.Add($Uri)
    $content = if ($Uri.EndsWith('windows-logon.ps1')) { "'downloaded bootstrap'" } else { "'downloaded registrar'" }
    if ($Scenario -eq 'invalid') { $content = 'function {' }
    if ($Scenario -eq 'second-download-fails' -and $Uri.EndsWith('windows-logon-task.ps1')) { throw 'download failed' }
    [IO.File]::WriteAllText($OutFile, $content)
}
$validator = {
    param([string]$Path)
    $content = [IO.File]::ReadAllText($Path)
    if ($content -eq 'function {') { throw 'invalid syntax' }
}
try {
    $resolved = Resolve-MnemoSeedTaskHelpers -SiblingRoot $siblings -StagingRoot $staging `
        -BaseUri 'https://example.invalid/' -Downloader $downloader -SyntaxValidator $validator
    $result = [pscustomobject]@{ Success = $true; DownloadCount = $downloads.Count; Bootstrap = $resolved.BootstrapSource; Task = $resolved.TaskRegistration }
} catch {
    $result = [pscustomobject]@{ Success = $false; Error = $_.Exception.Message }
}
[pscustomobject]@{ Result = $result; StagingExists = (Test-Path -LiteralPath $staging) } |
    ConvertTo-Json -Compress -Depth 3
