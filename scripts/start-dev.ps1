$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backendLog = Join-Path $root "backend\flask-dev.log"
$backendErr = Join-Path $root "backend\flask-dev.err.log"
$frontendLog = Join-Path $root "frontend\vite-dev.log"
$frontendErr = Join-Path $root "frontend\vite-dev.err.log"

if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
  python -m venv (Join-Path $root ".venv")
}

& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "backend\db\migrate.py")
if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }

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

Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
  -ArgumentList "-m flask --app app notification-worker" `
  -WorkingDirectory (Join-Path $root "backend") `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root "backend\notification-worker.log") `
  -RedirectStandardError (Join-Path $root "backend\notification-worker.err.log")

Write-Host "Backend:  http://127.0.0.1:5001"
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "Email worker started. Delivery requires valid SMTP settings; failed deliveries remain recorded in the private outbox."
