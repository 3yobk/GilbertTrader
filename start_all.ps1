# start_all.ps1 — Start the full GilbertTrader system
# Run this from the WebullTrader folder:
#   .\start_all.ps1

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  GILBERT TRADER — FULL SYSTEM STARTUP" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Activate venv
$venvActivate = ".\\.venv\\Scripts\\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
    Write-Host "[OK] Virtual environment activated" -ForegroundColor Green
} else {
    Write-Host "[WARN] .venv not found — using system Python" -ForegroundColor Yellow
}

# Terminal 1: API Server
Write-Host "[1/3] Starting API server on port 8000..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PWD'; .\.venv\Scripts\Activate.ps1; python api_server.py" -WindowStyle Normal

Start-Sleep -Seconds 2

# Terminal 2: Main bot
Write-Host "[2/3] Starting Gilbert Trader bot..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PWD'; .\.venv\Scripts\Activate.ps1; python main.py" -WindowStyle Normal

Start-Sleep -Seconds 2

# Terminal 3: UI
$uiPath = "..\pixel-perfect"
if (Test-Path $uiPath) {
    Write-Host "[3/3] Starting UI at http://localhost:3000..." -ForegroundColor Cyan
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$uiPath'; bun run dev" -WindowStyle Normal
} else {
    Write-Host "[SKIP] pixel-perfect folder not found at $uiPath" -ForegroundColor Yellow
    Write-Host "       Run 'bun run dev' manually from the pixel-perfect folder" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  ALL SERVICES STARTED" -ForegroundColor Green
Write-Host "  Bot:        check the bot terminal" -ForegroundColor White
Write-Host "  API:        http://localhost:8000" -ForegroundColor White
Write-Host "  Dashboard:  http://localhost:3000" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
