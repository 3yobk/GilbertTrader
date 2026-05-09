# 1. Logic Check for Virtual Environment
if (!(Test-Path -Path ".\.venv")) {
    Write-Host "--- Creating Virtual Environment... ---" -ForegroundColor Cyan
    py -3.13 -m venv .venv
}

Write-Host "--- Activating Environment... ---" -ForegroundColor Green
.\.venv\Scripts\Activate.ps1

# 2. Maintenance Check (Ensures ALL dependencies including tqdm are present)
Write-Host "--- Checking for System Dependencies... ---" -ForegroundColor Cyan
# Added tqdm to resolve the ModuleNotFoundError in research_scanner
pip install requests rich playwright playwright-stealth yfinance pandas discord.py streamlit plotly python-dotenv tqdm --quiet

# 3. Handle the Numba/Pandas-TA Patch if missing
if (!(Test-Path -Path ".\.venv\Lib\site-packages\numba")) {
    Write-Host "--- Applying Numba Compatibility Patch... ---" -ForegroundColor Yellow
    pip install pandas-ta --no-deps --quiet
    python -c "import os; p=r'.\.venv\Lib\site-packages\numba'; os.makedirs(p, exist_ok=True); open(os.path.join(p, '__init__.py'), 'w').write('def njit(f=None, **kwargs): return f if f else lambda x: x')"
}

# 4. Environment Configuration
# main.py handles this via the .env file now.

# 5. Launch Dashboard & Bot
Write-Host "--- Starting Performance Dashboard... ---" -ForegroundColor Cyan
Start-Process streamlit -ArgumentList "run dashboard.py --server.port 8501"

Write-Host "--- Launching WebullTrader... ---" -ForegroundColor Yellow
python main.py