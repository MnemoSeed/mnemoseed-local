# Runs the four mandatory quality gates (pytest, ruff check, ruff format --check, mypy src) from the repo root and stops at the first failure.
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$gates = [ordered]@{
    'pytest'      = @('run', 'pytest', '-q')
    'ruff check'  = @('run', 'ruff', 'check')
    'ruff format' = @('run', 'ruff', 'format', '--check')
    'mypy'        = @('run', 'mypy', 'src')
}

Write-Host "Repo root: $repoRoot"
Push-Location $repoRoot
try {
    foreach ($gate in $gates.GetEnumerator()) {
        Write-Host ""
        Write-Host "==> gate: $($gate.Key)" -ForegroundColor Cyan
        $gateArgs = $gate.Value
        & uv @gateArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host "GATE FAILED: $($gate.Key) (exit $LASTEXITCODE)" -ForegroundColor Red
            exit $LASTEXITCODE
        }
    }
    Write-Host ""
    Write-Host "ALL GATES PASSED" -ForegroundColor Green
}
finally {
    Pop-Location
}
