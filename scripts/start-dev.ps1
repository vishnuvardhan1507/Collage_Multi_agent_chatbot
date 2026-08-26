$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendLog = Join-Path $root "backend\flask-dev.log"
$backendErr = Join-Path $root "backend\flask-dev.err.log"
$frontendLog = Join-Path $root "frontend\vite-dev.log"
$frontendErr = Join-Path $root "frontend\vite-dev.err.log"

if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
  python -m venv (Join-Path $root ".venv")
}

Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
  -ArgumentList "backend\app.py" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $backendLog `
  -RedirectStandardError $backendErr

Start-Process -FilePath "npm.cmd" `
  -ArgumentList "run dev -- --host 127.0.0.1" `
  -WorkingDirectory (Join-Path $root "frontend") `
  -WindowStyle Hidden `
  -RedirectStandardOutput $frontendLog `
  -RedirectStandardError $frontendErr

Write-Host "Backend:  http://127.0.0.1:5000"
Write-Host "Frontend: http://127.0.0.1:5173"
