# Updates the checking libraries and the axe-core rules engine to their latest versions.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Write-Host 'Run Setup.bat first.' -ForegroundColor Red; exit 1 }

Write-Host 'Updating checking libraries...' -ForegroundColor Cyan
& $venvPy -m pip install --upgrade -r (Join-Path $root 'requirements.txt') --only-binary=:all: --disable-pip-version-check --quiet

Write-Host 'Updating the axe-core web accessibility rules...' -ForegroundColor Cyan
$axe = Join-Path $root 'adachecker\vendor\axe.min.js'
$tmp = "$axe.download"
try {
    Invoke-WebRequest -Uri 'https://cdn.jsdelivr.net/npm/axe-core@4/axe.min.js' -OutFile $tmp -UseBasicParsing
    if ((Get-Item $tmp).Length -gt 200000) {
        Copy-Item $axe "$axe.previous" -Force
        Move-Item $tmp $axe -Force
        $ver = (Select-String -Path $axe -Pattern 'axe v([0-9.]+)' | Select-Object -First 1).Matches.Groups[1].Value
        Write-Host "axe-core is now version $ver" -ForegroundColor Green
    } else { Remove-Item $tmp; Write-Host 'Download looked incomplete, kept the current rules.' -ForegroundColor Yellow }
} catch { Write-Host 'Could not download new rules, kept the current ones.' -ForegroundColor Yellow }
Write-Host 'Done.' -ForegroundColor Green
