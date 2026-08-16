#Requires -Version 5.1
<#
.SYNOPSIS
  MnemoSeed Local - zero-dependency install orchestrator (Windows, PowerShell 5.1+).

.DESCRIPTION
  One-line entry:
    irm https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.ps1 | iex
  Or run as a file:
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 [-DryRun] [-Yes] [-Tier <lite|standard|advanced>]

  Orchestration order (identical to install.sh):
    1. detect / install ollama   (winget Ollama.Ollama; when winget is absent,
       print a manual-download hint and exit non-zero)
    2. detect / install uv       (official installer; well-known install dir is
       prepended to the current process PATH)
    3. install / upgrade the CLI (uv tool install | uv tool upgrade)
    4. mnemoseed-local init      (skipped when ~/.mnemoseed-local/config.toml exists)
    5. mnemoseed-local doctor    (verbatim) + hardware-tier hint; hint-only,
       the script never changes config keys itself
    6. ollama pull <dream model> (the dream route's model from config.toml
       [dream.llm.dream] `model`, else the built-in default qwen3.5:9b; runs
       only after an explicit [y/N] confirmation; -Yes skips the prompt;
       a model is NEVER pulled without that confirmation)
    7. final mnemoseed-local doctor re-check + next steps
       (mnemoseed-local up; mnemoseed-local hook install for the OpenCode
       host adapter)

  Idempotent: every step skips when already satisfied. Every failed external
  install operation prints a one-line reason to stderr and exits non-zero.
  Doctor verdicts are readiness reports, not install operations: a failed
  check is noted on stderr but never aborts the orchestration (on a fresh
  machine the model check fails until step 6 pulls the model).

  -Tier is a convenience hint only: it makes the tier-adjust hint explicit
  without ever changing config keys.

  -DryRun prints the numbered plan with command-existence probe results and
  performs ZERO side effects (no installers, no init, no doctor, no pull).
#>
param(
    [switch]$DryRun,
    [switch]$Yes,
    [ValidateSet('', 'lite', 'standard', 'advanced')]
    [string]$Tier = ''
)

# Unknown flags are silently bindable to $args under -File invocation — a
# misspelled -DryRunn would otherwise fall through into the REAL install path.
# Reject any leftover argument hard, before any side effect.
if ($args.Count -gt 0) {
    [void][Console]::Error.WriteLineAsync("install.ps1: unrecognized argument(s): $($args -join ', ')")
    exit 1
}

Set-StrictMode -Off

# --- paths shared by every step -------------------------------------------

if ([string]::IsNullOrEmpty($env:MNEMOSEED_LOCAL_HOME)) {
    $ConfigHome = Join-Path $env:USERPROFILE '.mnemoseed-local'
} else {
    $ConfigHome = $env:MNEMOSEED_LOCAL_HOME
}
$ConfigPath = Join-Path $ConfigHome 'config.toml'
$UvBinDir = Join-Path $env:USERPROFILE '.local\bin'
$OllamaBinDir = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
$DefaultModel = 'qwen3.5:9b'

# --- helpers ---------------------------------------------------------------

function Exit-WithError([string]$Reason) {
    [Console]::Error.WriteLine("install.ps1: error: $Reason")
    exit 1
}

function Test-CommandExists([string]$Name) {
    return ($null -ne (Get-Command $Name -ErrorAction SilentlyContinue))
}

function Add-ToProcessPath([string]$Dir) {
    if ((Test-Path -LiteralPath $Dir) -and (-not ($env:PATH.Split(';') -contains $Dir))) {
        $env:PATH = "$Dir;$env:PATH"
        Write-Host "added $Dir to the current process PATH"
    }
}

# Get-DreamModel: the dream route's model, from the source doctor/up check
# against ("pull what will be checked"): the ACTIVE [dream.llm.dream] table's
# `model = "..."` key. Commented template lines are ignored; fall back to the
# built-in default when the key (or the file) is absent.
function Get-DreamModel([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $DefaultModel }
    $inDreamTable = $false
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '') { continue }
        if ($trimmed.StartsWith('#')) { continue }
        if ($trimmed -match '^\[([^\]]+)\]') {
            $inDreamTable = ($Matches[1].Trim() -eq 'dream.llm.dream')
            continue
        }
        if ($inDreamTable -and $trimmed -match '^model\s*=\s*(.+)$') {
            $value = $Matches[1].Trim()
            if ($value -match '^"([^"]*)"' -or $value -match "^'([^']*)'") {
                if ($Matches[1] -ne '') { return $Matches[1] }
            }
        }
    }
    return $DefaultModel
}

# Get-TierValue: extract the tier token out of the doctor hardware-tier detail
# line (pinned contract, emitted by the CLI: `recommended tier "standard"
# (vram=12GB, ram=32GB); current tier "standard"`).
function Get-TierValue([string]$Text, [string]$Which) {
    $m = [regex]::Match($Text, $Which + ' tier "(\w+)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

function Show-TierHints([string]$DoctorText) {
    $recommended = Get-TierValue $DoctorText 'recommended'
    $current = Get-TierValue $DoctorText 'current'
    if (($recommended -ne '') -and ($current -ne '') -and ($recommended -ne $current)) {
        Write-Host "hint: doctor recommends hardware tier `"$recommended`" but the current tier is `"$current`". This installer"
        Write-Host "      never changes config keys; you may adjust them yourself with:"
        Write-Host "        mnemoseed-local config set dream.hardware_tier $recommended"
        Write-Host "      and the matching model under dream.llm.dream.model ([dream.llm.dream] table, key `"model`")."
    }
    if ($Tier -ne '') {
        Write-Host "hint: -Tier $Tier was requested; this installer never changes config keys. To apply it, run:"
        Write-Host "        mnemoseed-local config set dream.hardware_tier $Tier"
    }
}

# Invoke-Doctor: run `mnemoseed-local doctor`, echo output verbatim, and
# return both the exit code and the captured text for tier parsing. The
# doctor exit code is a readiness report, so callers decide what it means.
function Invoke-Doctor {
    $lines = @(& mnemoseed-local doctor 2>&1 | ForEach-Object { "$_" })
    $code = $LASTEXITCODE
    foreach ($line in $lines) { [Console]::Out.WriteLine($line) }
    return @{ Code = $code; Text = ($lines -join "`n") }
}

# --- dry-run plan ----------------------------------------------------------

if ($DryRun) {
    Write-Host 'mnemoseed-local install plan (DRY-RUN: zero side effects; probes are command-existence checks only)'
    Write-Host ''
    Write-Host '[1] ollama'
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $ollamaCmd) {
        Write-Host "    probe: ollama command FOUND at $($ollamaCmd.Source)"
        Write-Host '    plan:  skip install (already present)'
    } else {
        Write-Host '    probe: ollama command NOT FOUND'
        if (Test-CommandExists 'winget') {
            Write-Host '    plan:  install via `winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements`'
        } else {
            Write-Host '    plan:  winget NOT FOUND - would print a manual-download hint (https://ollama.com/download) and exit non-zero'
        }
    }
    Write-Host '[2] uv'
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $uvCmd) {
        Write-Host "    probe: uv command FOUND at $($uvCmd.Source)"
        Write-Host "    plan:  skip install; prepend $UvBinDir to the current process PATH when present"
    } else {
        Write-Host '    probe: uv command NOT FOUND'
        Write-Host "    plan:  install via the official installer (https://astral.sh/uv/install.ps1), then prepend $UvBinDir to the current process PATH"
    }
    Write-Host '[3] mnemoseed-local CLI'
    $cliCmd = Get-Command mnemoseed-local -ErrorAction SilentlyContinue
    if ($null -ne $cliCmd) {
        Write-Host "    probe: mnemoseed-local command FOUND at $($cliCmd.Source)"
        Write-Host '    plan:  would run `uv tool upgrade mnemoseed-local`'
    } else {
        Write-Host '    probe: mnemoseed-local command NOT FOUND'
        Write-Host '    plan:  would run `uv tool install mnemoseed-local`'
    }
    Write-Host '[4] init'
    Write-Host "    plan:  would run ``mnemoseed-local init`` when $ConfigPath does not exist; skipped when present"
    Write-Host '[5] doctor (first pass)'
    Write-Host '    plan:  would run `mnemoseed-local doctor` (output shown verbatim), then compare the hardware-tier detail'
    Write-Host '           (`recommended tier "<tier>"` vs `current tier "<tier>"`; a hint is printed when they differ; config is never changed)'
    if ($Tier -ne '') {
        Write-Host "    plan:  -Tier $Tier given - would print the hint-only ``config set dream.hardware_tier $Tier`` instruction"
    }
    Write-Host '[6] model pull (requires confirmation)'
    Write-Host "    plan:  would resolve the dream model from $ConfigPath (an ACTIVE [dream.llm.dream] ``model`` key; default $DefaultModel),"
    Write-Host '           prompt [y/N] (skipped by -Yes), then run `ollama pull <model>` - NEVER without that confirmation'
    Write-Host '[7] final doctor + guidance'
    Write-Host '    plan:  would re-run `mnemoseed-local doctor` verbatim, then print next steps'
    Write-Host '           (`mnemoseed-local up`; `mnemoseed-local hook install` for the OpenCode host adapter)'
    Write-Host ''
    Write-Host 'dry-run complete: no installers ran, no init, no doctor, no pull - nothing changed'
    exit 0
}

# --- step 1: ollama ---------------------------------------------------------

Write-Host '[1/7] ollama'
if (Test-CommandExists 'ollama') {
    Write-Host '      found - skipping install'
} else {
    if (-not (Test-CommandExists 'winget')) {
        Exit-WithError "ollama is not installed and winget is unavailable; download ollama manually from https://ollama.com/download and re-run"
    }
    Write-Host '      not found - installing via winget...'
    & winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Exit-WithError "winget failed to install Ollama.Ollama (exit $LASTEXITCODE); retry manually from https://ollama.com/download"
    }
    Add-ToProcessPath $OllamaBinDir
    if (-not (Test-CommandExists 'ollama')) {
        Exit-WithError "ollama is still not on PATH after the winget install; open a new shell or download it from https://ollama.com/download"
    }
    Write-Host '      installed'
}

# --- step 2: uv -------------------------------------------------------------

Write-Host '[2/7] uv'
if (Test-CommandExists 'uv') {
    Write-Host '      found - skipping install'
} else {
    Write-Host '      not found - installing via the official installer (https://astral.sh/uv/install.ps1)...'
    $uvInstaller = Join-Path $env:TEMP "mnemoseed-uv-install-$PID.ps1"
    $downloaded = $false
    try {
        Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller -UseBasicParsing
        $downloaded = $true
    } catch {
        if (Test-CommandExists 'curl.exe') {
            & curl.exe -fsSL -o $uvInstaller 'https://astral.sh/uv/install.ps1'
            if ($LASTEXITCODE -eq 0) { $downloaded = $true }
        }
    }
    if (-not $downloaded) {
        Exit-WithError 'failed to download the official uv installer (https://astral.sh/uv/install.ps1); check the network and retry'
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
    $uvInstallCode = $LASTEXITCODE
    Remove-Item -LiteralPath $uvInstaller -ErrorAction SilentlyContinue
    if ($uvInstallCode -ne 0) {
        Exit-WithError "the official uv installer failed (exit $uvInstallCode); see https://docs.astral.sh/uv/getting-started/installation/"
    }
    Add-ToProcessPath $UvBinDir
    if (-not (Test-CommandExists 'uv')) {
        Exit-WithError "uv is still not on PATH after the install; add $UvBinDir to PATH and re-run"
    }
    Write-Host '      installed'
}
Add-ToProcessPath $UvBinDir

# --- step 3: the mnemoseed-local CLI ---------------------------------------

Write-Host '[3/7] mnemoseed-local CLI'
if (Test-CommandExists 'mnemoseed-local') {
    Write-Host '      found - upgrading via uv tool...'
    & uv tool upgrade mnemoseed-local
    if ($LASTEXITCODE -ne 0) { Exit-WithError "'uv tool upgrade mnemoseed-local' failed (exit $LASTEXITCODE)" }
} else {
    Write-Host '      not found - installing via uv tool...'
    & uv tool install mnemoseed-local
    if ($LASTEXITCODE -ne 0) { Exit-WithError "'uv tool install mnemoseed-local' failed (exit $LASTEXITCODE)" }
}
if (-not (Test-CommandExists 'mnemoseed-local')) {
    Exit-WithError "mnemoseed-local was installed but is not on PATH; add $UvBinDir to PATH and re-run"
}

# --- step 4: init ------------------------------------------------------------

Write-Host '[4/7] init'
if (Test-Path -LiteralPath $ConfigPath) {
    Write-Host "      $ConfigPath already exists - skipping"
} else {
    & mnemoseed-local init
    if ($LASTEXITCODE -ne 0) { Exit-WithError "'mnemoseed-local init' failed (exit $LASTEXITCODE)" }
}

# --- step 5: doctor (first pass) + hardware-tier hint ------------------------

Write-Host '[5/7] doctor (first pass)'
$firstDoctor = Invoke-Doctor
if ($firstDoctor.Code -ne 0) {
    [Console]::Error.WriteLine('install.ps1: note: doctor reported failures (expected before the model pull) - continuing')
}
Show-TierHints $firstDoctor.Text

# --- step 6: confirmation-gated model pull -----------------------------------

Write-Host '[6/7] dream model pull'
$model = Get-DreamModel $ConfigPath
if ($model -eq $DefaultModel) {
    Write-Host "      model to pull: $model (built-in default - the dream route's check target)"
} else {
    Write-Host "      model to pull: $model (from $ConfigPath [dream.llm.dream] ``model`` - the dream route's check target)"
}
$confirmed = $false
if ($Yes) {
    $confirmed = $true
    Write-Host '      -Yes given - skipping the confirmation prompt'
} else {
    $answer = 'n'
    try {
        $answer = Read-Host "      pull it now with 'ollama pull $model'? [y/N]"
    } catch {
        $answer = 'n'
    }
    if ($answer -match '^(y|yes)$') { $confirmed = $true }
}
if (-not $confirmed) {
    Write-Host "      skipped (no confirmation); pull it later with: ollama pull $model"
} else {
    & ollama pull $model
    if ($LASTEXITCODE -ne 0) {
        Exit-WithError "'ollama pull $model' failed (exit $LASTEXITCODE); is the ollama server running? retry with: ollama pull $model"
    }
    Write-Host '      pulled'
}

# --- step 7: final doctor + guidance -----------------------------------------

Write-Host '[7/7] doctor (final re-check)'
$finalDoctor = Invoke-Doctor
if ($finalDoctor.Code -ne 0) {
    [Console]::Error.WriteLine('install.ps1: note: doctor still reports failures; resolve them, then re-run `mnemoseed-local doctor`')
}
Write-Host ''
Write-Host 'installation complete.'
Write-Host 'next steps:'
Write-Host '  mnemoseed-local up            # start the daemon'
Write-Host '  mnemoseed-local hook install  # install the OpenCode host adapter'
exit 0
