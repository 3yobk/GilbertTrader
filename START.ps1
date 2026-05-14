# ============================================================
#  GilbertTrader -- ONE-CLICK STARTUP
#  First run: ~2 min. Every run after: ~5 seconds.
#  Checks presence by folder -- never calls pip if installed.
# ============================================================

$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "GilbertTrader Setup and Launch"

function Info  { param($m) Write-Host "  $m" -ForegroundColor Cyan }
function OK    { param($m) Write-Host "  OK  $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "  !!  $m" -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "  XX  $m" -ForegroundColor Red }
function Head  { param($m) Write-Host "`n===  $m  ===" -ForegroundColor Magenta }

Clear-Host
Write-Host ""
Write-Host "  GILBERT TRADER v2.0.1" -ForegroundColor DarkCyan
Write-Host ""

$SCRIPT_DIR    = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $SCRIPT_DIR
$VENV_PY       = Join-Path $SCRIPT_DIR ".venv\Scripts\python.exe"
$SITE          = Join-Path $SCRIPT_DIR ".venv\Lib\site-packages"

# ============================================================
# STEP 1 -- Credentials
# ============================================================
Head "STEP 1 -- Credentials"

if (-not (Test-Path ".env")) {
    Warn ".env not found -- creating template..."
    @(
        "DISCORD_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE",
        "DISCORD_CHANNEL_ID=PASTE_YOUR_CHANNEL_ID_HERE",
        "AI_PC_URL=http://YOUR_AI_PC_IP:5000/ask",
        "AI_PC_KEY=sk-your-key-here",
        "TRADIER_TOKEN=PASTE_YOUR_TRADIER_TOKEN_HERE",
        "TRADIER_SANDBOX=false",
        "WEBULL_EMAIL=your@email.com",
        "WEBULL_TRADING_PIN=123456",
        "LIVE_TRADING_ENABLED=false",
        "TEST_BUDGET=300"
    ) | Out-File ".env" -Encoding UTF8
    Write-Host "  Open .env in Notepad now? (y/n): " -NoNewline
    if ((Read-Host) -eq "y") { notepad ".env" }
    exit 0
}
if ((Get-Content ".env" -Raw) -match "PASTE_YOUR") {
    Warn ".env has placeholder values -- fill it in first."
    Write-Host "  Open .env now? (y/n): " -NoNewline
    if ((Read-Host) -eq "y") { notepad ".env" }
    exit 0
}
OK ".env ready"

# ============================================================
# STEP 2 -- Python
# ============================================================
Head "STEP 2 -- Python"

$PYTHON_CMD = ""; $PYTHON_ARGS = ""
foreach ($c in @(@("py","-3.13"),@("py","-3.12"),@("py","-3.11"),@("py","-3.10"),@("python",""),@("python3",""))) {
    try {
        $v = if ($c[1]) { & $c[0] $c[1] --version 2>&1 } else { & $c[0] --version 2>&1 }
        if ("$v" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
            $PYTHON_CMD = $c[0]; $PYTHON_ARGS = $c[1]
            OK "Found: $v"; break
        }
    } catch { }
}
if (-not $PYTHON_CMD) {
    Fail "Python 3.10+ not found. Install from https://www.python.org/downloads/"
    Read-Host "Press Enter to exit"; exit 1
}

# ============================================================
# STEP 3 -- Virtual environment
# ============================================================
Head "STEP 3 -- Virtual Environment"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Info "Creating .venv ..."
    if ($PYTHON_ARGS) { & $PYTHON_CMD $PYTHON_ARGS -m venv .venv }
    else              { & $PYTHON_CMD -m venv .venv }
    # Upgrade pip once, right after creation
    cmd /c "`"$VENV_PY`" -m pip install --upgrade pip --quiet" 2>&1 | Out-Null
    OK ".venv created"
} else {
    OK ".venv exists"
}

# ============================================================
# STEP 4 -- Python packages (check by folder, never call pip if present)
# ============================================================
Head "STEP 4 -- Python Packages"

# Map: package name to install -> folder name in site-packages to check
# Checking the folder is instant (no network, no pip overhead)
$PACKAGES = [ordered]@{
    "playwright==1.59.0"      = "playwright"
    "playwright-stealth"      = "playwright_stealth"
    "webull"                  = "webull"
    "yfinance"                = "yfinance"
    "pandas"                  = "pandas"
    "pandas-ta>=0.4.67b0"     = "pandas_ta"
    "numpy<2.0"               = "numpy"
    "pytz"                    = "pytz"
    "streamlit"               = "streamlit"
    "plotly"                  = "plotly"
    "rich"                    = "rich"
    "discord.py"              = "discord"
    "Flask"                   = "flask"
    "flask-cors"              = "flask_cors"
    "requests"                = "requests"
    "python-dotenv"           = "dotenv"
    "websocket-client"        = "websocket"
    "tqdm"                    = "tqdm"
}

$missing = @()
foreach ($pkg in $PACKAGES.Keys) {
    $folder = $PACKAGES[$pkg]
    if (-not (Test-Path "$SITE\$folder")) {
        $missing += $pkg
    }
}

if ($missing.Count -eq 0) {
    OK "All packages present"
} else {
    Info "Installing $($missing.Count) missing package(s): $($missing -join ', ')"
    foreach ($pkg in $missing) {
        $r = & $VENV_PY -m pip install $pkg --quiet 2>&1
        $r | Where-Object { $_ -match "^ERROR" } | ForEach-Object { Warn $_ }
    }
    OK "Done installing"
}

# numba shim for pandas-ta (check folder, write if missing)
$numba_path = "$SITE\numba"
if (-not (Test-Path $numba_path)) {
    New-Item -ItemType Directory -Path $numba_path -Force | Out-Null
    @(
        "def njit(f=None, **kw):",
        "    return f if f else (lambda x: x)"
    ) | Out-File "$numba_path\__init__.py" -Encoding UTF8
    OK "numba shim written"
} else {
    OK "numba shim present"
}

# ============================================================
# STEP 5 -- Playwright browser (check folder, skip if present)
# ============================================================
Head "STEP 5 -- Playwright Browser"

$pwCache = Join-Path $env:LOCALAPPDATA "ms-playwright"
if (Test-Path $pwCache) {
    OK "Chromium already installed"
} else {
    Info "Installing Chromium (first run only) ..."
    cmd /c "`"$VENV_PY`" -m playwright install chromium" 2>&1 | Out-Null
    OK "Chromium ready"
}

# ============================================================
# STEP 6 -- Node / frontend (check node_modules folder)
# ============================================================
Head "STEP 6 -- Frontend"

$USE_NPM = $false; $USE_BUN = $false; $SKIP_UI = $false; $DEV_CMD = ""
try { if ((cmd /c "bun --version" 2>&1) -match "\d") { $USE_BUN=$true; $DEV_CMD="bun run dev" } } catch {}
if (-not $USE_BUN) {
    try { if ((cmd /c "node --version" 2>&1) -match "v\d") { $USE_NPM=$true; $DEV_CMD="npm run dev" } } catch {}
}
if (-not $USE_BUN -and -not $USE_NPM) {
    Warn "Node.js not found -- dashboard skipped. Install from https://nodejs.org"
    $SKIP_UI = $true
}

if (-not $SKIP_UI) {
    $UI_DIR = Join-Path $SCRIPT_DIR "pixel-perfect"
    if (-not (Test-Path "$UI_DIR\node_modules")) {
        Info "Installing React deps (first run only) ..."
        if ($USE_BUN) { cmd /c "cd /d `"$UI_DIR`" && bun install" 2>&1 | Out-Null }
        else          { cmd /c "cd /d `"$UI_DIR`" && npm install"  2>&1 | Out-Null }
        OK "React deps installed"
    } else { OK "node_modules present" }

    $envLocal = "$UI_DIR\.env.local"
    if (-not (Test-Path $envLocal)) {
        if (Test-Path "$UI_DIR\.env") { Copy-Item "$UI_DIR\.env" $envLocal }
        else { @("VITE_DATA_PROVIDER=http","VITE_API_BASE=http://localhost:8000") | Out-File $envLocal -Encoding UTF8 }
        OK "Created .env.local"
    } else { OK ".env.local present" }
}

# ============================================================
# STEP 7 -- Launch
# ============================================================
Head "STEP 7 -- Launch"

# Write .bat files with fully baked-in paths (no quoting issues)
$apiBat = "$SCRIPT_DIR\_launch_api.bat"
@"
@echo off
title GilbertTrader -- API Server
cd /d "$SCRIPT_DIR"
"$VENV_PY" api_server.py
pause
"@ | Out-File $apiBat -Encoding ASCII

$botBat = "$SCRIPT_DIR\_launch_bot.bat"
@"
@echo off
title GilbertTrader -- Bot
cd /d "$SCRIPT_DIR"
"$VENV_PY" main.py
pause
"@ | Out-File $botBat -Encoding ASCII

Info "Starting API server  (port 8000) ..."
Start-Process "cmd.exe" -ArgumentList "/k `"$apiBat`"" -WindowStyle Normal
Start-Sleep -Seconds 3

Info "Starting bot ..."
Start-Process "cmd.exe" -ArgumentList "/k `"$botBat`"" -WindowStyle Normal
Start-Sleep -Seconds 2

if (-not $SKIP_UI) {
    $uiBat = "$SCRIPT_DIR\_launch_ui.bat"
    $UI_DIR = Join-Path $SCRIPT_DIR "pixel-perfect"
    @"
@echo off
title GilbertTrader -- Dashboard
cd /d "$UI_DIR"
    $DEV_CMD
pause
"@ | Out-File $uiBat -Encoding ASCII

    Info "Starting dashboard  (port 8080) ..."
    Start-Process "cmd.exe" -ArgumentList "/k `"$uiBat`"" -WindowStyle Normal
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "  +----------------------------------------------+" -ForegroundColor Green
Write-Host "  |       GilbertTrader is running!              |" -ForegroundColor Green
Write-Host "  +----------------------------------------------+" -ForegroundColor Green
Write-Host "  |  API     ->  http://localhost:8000           |" -ForegroundColor Cyan
if (-not $SKIP_UI) {
    Write-Host "  |  Dashboard -> http://localhost:8080          |" -ForegroundColor Cyan
}
Write-Host "  +----------------------------------------------+" -ForegroundColor Green
Write-Host "  |  paper=True  |  LIVE_TRADING=false           |" -ForegroundColor Yellow
Write-Host "  +----------------------------------------------+" -ForegroundColor Green
Write-Host ""

if (-not $SKIP_UI) {
    Info "Opening browser in 5 seconds..."
    Start-Sleep -Seconds 5
    Start-Process "http://localhost:8080"
}

Write-Host "  Done. This window will close in 3 seconds." -ForegroundColor DarkGray
Start-Sleep -Seconds 3