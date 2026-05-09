# 1. Logic Check for Virtual Environment
if (!(Test-Path -Path ".\.venv")) {
    Write-Host "--- Virtual Environment not found. Creating with Python 3.13... ---" -ForegroundColor Cyan

    # Target 3.13 specifically to avoid 3.14 "Bleeding Edge" bugs
    py -3.13 -m venv .venv

    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Python 3.13 not found. Please install it or use the full path." -ForegroundColor Red
        exit
    }

    # Activate
    .\.venv\Scripts\Activate.ps1

    Write-Host "--- Installing Core Requirements... ---" -ForegroundColor Cyan
    # Added tqdm, numpy, and discord.py to cover all bases
    pip install requests rich playwright playwright-stealth yfinance pandas tqdm numpy discord.py

    Write-Host "--- Installing Research Tools (Safe Mode)... ---" -ForegroundColor Cyan
    # --no-deps prevents the broken 'numba' wheel build crash
    pip install pandas-ta --no-deps

    Write-Host "--- Applying Numba Compatibility Patch... ---" -ForegroundColor Cyan
    # This creates a 'mock' numba module so pandas-ta imports successfully
    python -c "import os; p=r'.\.venv\Lib\site-packages\numba'; os.makedirs(p, exist_ok=True); open(os.path.join(p, '__init__.py'), 'w').write('def njit(f=None, **kwargs): return f if f else lambda x: x')"

    Write-Host "--- Installing Playwright browsers... ---" -ForegroundColor Cyan
    playwright install chromium
} else {
    Write-Host "--- Virtual Environment detected. Activating... ---" -ForegroundColor Green
    .\.venv\Scripts\Activate.ps1
}

# 2. Set Environment Variables
$env:DISCORD_TOKEN="MTUwMjQyNDAyOTk4ODkxNzI1OA.GKbijo.jQpoBL1DGWo-GYdOjGhNZf3SAUs84JiiWbLJEY"
$env:DISCORD_CHANNEL_ID="1393749056672501874"

# 3. Launch the Bot
Write-Host "--- Launching WebullTrader... ---" -ForegroundColor Yellow
python main.py