Set-StrictMode -Version 2.0

function Test-PowerShellSyntax {
    param([Parameter(Mandatory = $true)][string]$Path)
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) {
        throw "invalid PowerShell syntax in Windows autostart helper $Path ($($errors[0].Message))"
    }
}

function Resolve-MnemoSeedTaskHelpers {
    param(
        [Parameter(Mandatory = $true)][string]$SiblingRoot,
        [Parameter(Mandatory = $true)][string]$StagingRoot,
        [Parameter(Mandatory = $true)][string]$BaseUri,
        [scriptblock]$Downloader,
        [scriptblock]$SyntaxValidator
    )
    $bootstrap = Join-Path $SiblingRoot 'windows-logon.ps1'
    $task = Join-Path $SiblingRoot 'windows-logon-task.ps1'
    if ((Test-Path -LiteralPath $bootstrap -PathType Leaf) -and
        (Test-Path -LiteralPath $task -PathType Leaf)) {
        if ($null -eq $SyntaxValidator) { $SyntaxValidator = { param($Path) Test-PowerShellSyntax $Path } }
        & $SyntaxValidator $bootstrap
        & $SyntaxValidator $task
        return @{ BootstrapSource = $bootstrap; TaskRegistration = $task }
    }

    if ($null -eq $Downloader) {
        $Downloader = {
            param([string]$Uri, [string]$OutFile)
            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
        }
    }
    if ($null -eq $SyntaxValidator) { $SyntaxValidator = { param($Path) Test-PowerShellSyntax $Path } }
    [void][IO.Directory]::CreateDirectory($StagingRoot)
    try {
        $stagedBootstrap = Join-Path $StagingRoot 'windows-logon.ps1'
        $stagedTask = Join-Path $StagingRoot 'windows-logon-task.ps1'
        & $Downloader ($BaseUri + 'windows-logon.ps1') $stagedBootstrap
        & $Downloader ($BaseUri + 'windows-logon-task.ps1') $stagedTask
        & $SyntaxValidator $stagedBootstrap
        & $SyntaxValidator $stagedTask
        return @{ BootstrapSource = $stagedBootstrap; TaskRegistration = $stagedTask }
    } catch {
        if (Test-Path -LiteralPath $StagingRoot) {
            Remove-Item -LiteralPath $StagingRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Invoke-MnemoSeedTaskInstallation {
    param(
        [Parameter(Mandatory = $true)][string]$TaskScript,
        [Parameter(Mandatory = $true)][string]$BootstrapSource,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$User,
        [scriptblock]$TaskLookup,
        [scriptblock]$TaskUnregister,
        [scriptblock]$TaskInvoker
    )
    $migrationArguments = @{
        BootstrapSource = $BootstrapSource
        TaskName = $TaskName
        User = $User
        MigrateLegacyOllamaTask = $true
    }
    if ($null -ne $TaskLookup) { $migrationArguments.TaskLookup = $TaskLookup }
    if ($null -ne $TaskUnregister) { $migrationArguments.TaskUnregister = $TaskUnregister }
    [void](& $TaskScript @migrationArguments)
    $exitVariable = Get-Variable LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -ne $exitVariable -and $exitVariable.Value -ne 0) {
        throw "legacy task migration failed with exit code $LASTEXITCODE"
    }

    $registrationArguments = @{ BootstrapSource = $BootstrapSource; TaskName = $TaskName; User = $User }
    if ($null -eq $TaskInvoker) {
        [void](& $TaskScript @registrationArguments)
        $exitVariable = Get-Variable LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -ne $exitVariable -and $exitVariable.Value -ne 0) {
            throw "daemon task registration failed with exit code $LASTEXITCODE"
        }
    } else {
        & $TaskInvoker $TaskScript $registrationArguments
    }
}
