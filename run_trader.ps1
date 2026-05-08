# 1. Logic Check for Virtual Environment
if (!(Test-Path -Path ".\venv")) {
    Write-Host "--- Virtual Environment not found. Creating... ---" -ForegroundColor Cyan
    python -m venv venv

    # Activate and install dependencies
    .\venv\Scripts\Activate.ps1
    Write-Host "--- Installing requirements... ---" -ForegroundColor Cyan
    pip install -r requirements.txt

    Write-Host "--- Installing Playwright browsers... ---" -ForegroundColor Cyan
    playwright install chromium
} else {
    Write-Host "--- Virtual Environment detected. Activating... ---" -ForegroundColor Green
    .\venv\Scripts\Activate.ps1
}

# 2. Set Environment Variables
$env:DISCORD_TOKEN="MTUwMjQyNDAyOTk4ODkxNzI1OA.GKbijo.jQpoBL1DGWo-GYdOjGhNZf3SAUs84JiiWbLJEY"
$env:DISCORD_CHANNEL_ID="1393749056672501874"

# 3. Launch the Bot
Write-Host "--- Launching WebullTrader... ---" -ForegroundColor Yellow
python main.py