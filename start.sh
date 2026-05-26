#!/bin/bash
# ============================================================
#  GilbertTrader -- macOS ONE-CLICK STARTUP
#  Version: v2.3 Mac Launcher
#  First run: ~2 min. Every run after: seconds.
#  Purpose: Mac-safe setup + API + bot/watchdog + React dashboard.
# ============================================================

set -e

# Terminal window properties
printf "\033]0;GilbertTrader Setup and Launch\007"
clear_screen() { printf "\033[H\033[2J"; }

# Text styling
info() { echo -e "  \033[36m$1\033[0m"; }
ok()   { echo -e "  \033[32mOK  $1\033[0m"; }
warn() { echo -e "  \033[33m!!  $1\033[0m"; }
fail() { echo -e "  \033[31mXX  $1\033[0m"; exit 1; }
head() { echo -e "\n\033[35m===  $1  ===\033[0m"; }

clear_screen
echo -e "\n  \033[36mGILBERT TRADER v2.3 (macOS)\033[0m\n"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
UI_DIR="$SCRIPT_DIR/pixel-perfect"

# ============================================================
# STEP 0 -- macOS / Architecture Info
# ============================================================
head "STEP 0 -- System"

ARCH="$(uname -m)"
OS_VER="$(sw_vers -productVersion 2>/dev/null || echo "unknown")"
ok "macOS $OS_VER detected"
ok "Architecture: $ARCH"

if [ "$ARCH" = "arm64" ]; then
    info "Apple Silicon detected"
else
    info "Intel Mac detected"
fi

# ============================================================
# STEP 1 -- Credentials
# ============================================================
head "STEP 1 -- Credentials"

if [ ! -f ".env" ]; then
    warn ".env not found -- creating template..."
    cat << EOF > .env
DISCORD_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE
DISCORD_CHANNEL_ID=PASTE_YOUR_CHANNEL_ID_HERE
DISCORD_SIGNAL_CHANNEL_ID=PASTE_YOUR_SIGNAL_CHANNEL_ID_HERE
AI_PC_URL=http://YOUR_AI_PC_IP:5000/ask
AI_PC_KEY=sk-your-key-here
TRADIER_TOKEN=PASTE_YOUR_TRADIER_TOKEN_HERE
TRADIER_SANDBOX=false
WEBULL_EMAIL=your@email.com
WEBULL_TRADING_PIN=123456

# Safety gate. Keep false until you intentionally go live.
LIVE_TRADING_ENABLED=false
TEST_BUDGET=300

# Optional alerts
ALERT_EMAIL_FROM=
ALERT_EMAIL_TO=
ALERT_EMAIL_PASSWORD=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM=
TWILIO_TO=
EOF
    read -p "  Open .env in Nano now? (y/n): " choice
    if [ "$choice" = "y" ]; then nano .env; fi
    exit 0
fi

if grep -q "PASTE_YOUR\|YOUR_AI_PC_IP\|your@email.com" .env; then
    warn ".env still has placeholder values."
    read -p "  Open .env now? (y/n): " choice
    if [ "$choice" = "y" ]; then nano .env; fi
    exit 0
fi

ok ".env ready"

# Live trading guard
if grep -Eiq '^LIVE_TRADING_ENABLED[[:space:]]*=[[:space:]]*true' .env; then
    warn "LIVE_TRADING_ENABLED=true detected"
    warn "This can place REAL Webull orders if the bot is configured for live mode."
    read -p "  Type YES to continue launching live-capable mode: " confirm
    if [ "$confirm" != "YES" ]; then
        fail "Launch aborted by live-trading safety guard."
    fi
else
    ok "Paper/safe mode: LIVE_TRADING_ENABLED is not true"
fi

# ============================================================
# STEP 2 -- Python Check
# ============================================================
head "STEP 2 -- Python"

PYTHON_CMD=""
PYTHON_VER=""

# Prefer 3.11 or 3.12 for trading/data package stability.
CANDIDATES=(
    "python3.11"
    "python3.12"
    "python3.10"
    "python3"
    "python"
)

for cmd in "${CANDIDATES[@]}"; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)

        if [ "$major" = "3" ] && [ "$minor" -ge 10 ] && [ "$minor" -le 12 ]; then
            PYTHON_CMD="$cmd"
            PYTHON_VER="$ver"
            ok "Found supported Python $ver using $cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    fail "Supported Python not found. Install Python 3.11 or 3.12. Recommended: brew install python@3.11"
fi

if [[ "$PYTHON_VER" == "3.13"* ]]; then
    fail "Python 3.13 detected, but this bot should use Python 3.11 or 3.12 for package stability."
fi

# ============================================================
# STEP 3 -- Virtual Environment
# ============================================================
head "STEP 3 -- Virtual Environment"

if [ ! -f "$VENV_PY" ]; then
    info "Creating .venv with $PYTHON_CMD ..."
    "$PYTHON_CMD" -m venv .venv
    "$VENV_PY" -m pip install --upgrade pip setuptools wheel --quiet
    ok ".venv created"
else
    ok ".venv exists"
fi

SITE="$(ls -d "$SCRIPT_DIR"/.venv/lib/python3.*/site-packages 2>/dev/null | head -n 1)"
if [ -z "$SITE" ]; then
    fail "Could not find site-packages inside .venv"
fi

# ============================================================
# STEP 4 -- Python Packages
# ============================================================
head "STEP 4 -- Python Packages"

PACKAGES=(
    "playwright"
    "playwright_stealth"
    "webull"
    "yfinance"
    "pandas"
    "pandas_ta"
    "numpy"
    "pytz"
    "plotly"
    "rich"
    "discord"
    "flask"
    "flask_cors"
    "requests"
    "dotenv"
    "websocket"
    "tqdm"
)

INSTALL_MAP=(
    "playwright==1.59.0"
    "playwright-stealth"
    "webull"
    "yfinance==0.2.54"
    "pandas==2.2.3"
    "pandas-ta-classic"
    "numpy<2.0"
    "pytz"
    "plotly==6.0.0"
    "rich==13.9.4"
    "discord.py==2.4.0"
    "Flask==3.1.3"
    "flask-cors==6.0.2"
    "requests==2.32.3"
    "python-dotenv==1.0.1"
    "websocket-client"
    "tqdm"
)

missing_pkgs=()
for i in "${!PACKAGES[@]}"; do
    folder="${PACKAGES[$i]}"
    if [ ! -d "$SITE/$folder" ]; then
        missing_pkgs+=("${INSTALL_MAP[$i]}")
    fi
done

if [ ${#missing_pkgs[@]} -eq 0 ]; then
    ok "All Python packages present"
else
    info "Installing missing package(s): ${missing_pkgs[*]}"
    "$VENV_PY" -m pip install "${missing_pkgs[@]}"
    ok "Python packages installed"
fi

# pandas-ta compatibility alias.
# Some Mac installs use pandas-ta-classic, but code may import pandas_ta.
if [ ! -d "$SITE/pandas_ta" ]; then
    info "Writing pandas_ta compatibility shim"
    mkdir -p "$SITE/pandas_ta"
    cat << 'EOF' > "$SITE/pandas_ta/__init__.py"
try:
    from pandas_ta_classic import *  # noqa
except Exception:
    pass
EOF
    ok "pandas_ta shim written"
else
    ok "pandas_ta present"
fi

# Numba shim for packages that import numba.njit.
if [ ! -d "$SITE/numba" ]; then
    mkdir -p "$SITE/numba"
    cat << 'EOF' > "$SITE/numba/__init__.py"
def njit(f=None, **kw):
    return f if f else (lambda x: x)
EOF
    ok "numba shim written"
else
    ok "numba shim present"
fi

# ============================================================
# STEP 5 -- Playwright Browser
# ============================================================
head "STEP 5 -- Playwright Browser"

PW_CACHE="$HOME/Library/Caches/ms-playwright"
if [ -d "$PW_CACHE" ]; then
    ok "Chromium already installed"
else
    info "Installing Chromium (first run only) ..."
    "$VENV_PY" -m playwright install chromium
    ok "Chromium ready"
fi

# ============================================================
# STEP 6 -- Frontend
# ============================================================
head "STEP 6 -- Frontend"

USE_NPM=false
USE_BUN=false
SKIP_UI=false
DEV_CMD=""

if command -v bun >/dev/null 2>&1; then
    USE_BUN=true
    DEV_CMD="bun run dev"
elif command -v npm >/dev/null 2>&1; then
    USE_NPM=true
    DEV_CMD="npm run dev"
else
    warn "Node.js/Bun not found -- dashboard skipped. Install Node.js from https://nodejs.org"
    SKIP_UI=true
fi

if [ "$SKIP_UI" = false ]; then
    if [ ! -d "$UI_DIR" ]; then
        warn "pixel-perfect folder not found -- dashboard skipped"
        SKIP_UI=true
    else
        if [ ! -d "$UI_DIR/node_modules" ]; then
            info "Installing React deps (first run only) ..."
            if [ "$USE_BUN" = true ]; then
                (cd "$UI_DIR" && bun install)
            else
                (cd "$UI_DIR" && npm install)
            fi
            ok "React deps installed"
        else
            ok "node_modules present"
        fi

        if [ ! -f "$UI_DIR/.env.local" ]; then
            if [ -f "$UI_DIR/.env" ]; then
                cp "$UI_DIR/.env" "$UI_DIR/.env.local"
            else
                cat << EOF > "$UI_DIR/.env.local"
VITE_DATA_PROVIDER=http
VITE_API_BASE=http://localhost:8000
EOF
            fi
            ok "Created .env.local"
        else
            ok ".env.local present"
        fi
    fi
fi

# ============================================================
# STEP 6.5 -- Network Validation
# ============================================================
head "STEP 6.5 -- Network Validation"

if command -v curl >/dev/null 2>&1; then
    if curl -Is --connect-timeout 8 https://discord.com >/dev/null; then
        ok "Discord reachable"
    else
        warn "Discord is not reachable. Check Wi-Fi, DNS, VPN, or Tailscale."
    fi

    if curl -Is --connect-timeout 8 https://api.tradier.com >/dev/null; then
        ok "Tradier reachable"
    else
        warn "Tradier is not reachable. Market data may fail."
    fi

    if curl -Is --connect-timeout 8 https://app.webull.com >/dev/null; then
        ok "Webull reachable"
    else
        warn "Webull is not reachable. Browser login/order execution may fail."
    fi
else
    warn "curl not found -- skipping network validation"
fi

# ============================================================
# STEP 7 -- Launch
# ============================================================
head "STEP 7 -- Launch"

cd "$SCRIPT_DIR"

BOT_ENTRY="main.py"
if [ -f "$SCRIPT_DIR/watchdog.py" ]; then
    BOT_ENTRY="watchdog.py"
    ok "watchdog.py found -- launching watchdog for auto-recovery"
else
    warn "watchdog.py not found -- launching main.py directly"
fi

# Helper: escape paths for AppleScript.
SCRIPT_DIR_ESC="${SCRIPT_DIR//\'/\'\\\'\'}"
VENV_PY_ESC="${VENV_PY//\'/\'\\\'\'}"
UI_DIR_ESC="${UI_DIR//\'/\'\\\'\'}"

info "Starting API server (port 8000) ..."
osascript \
    -e "tell application \"Terminal\" to activate" \
    -e "tell application \"Terminal\" to do script \"cd '$SCRIPT_DIR_ESC' && '$VENV_PY_ESC' api_server.py\""
sleep 3

info "Starting bot ($BOT_ENTRY) ..."
osascript \
    -e "tell application \"Terminal\" to activate" \
    -e "tell application \"Terminal\" to do script \"cd '$SCRIPT_DIR_ESC' && '$VENV_PY_ESC' $BOT_ENTRY\""
sleep 2

if [ "$SKIP_UI" = false ]; then
    info "Starting React dashboard (port 8080) ..."
    osascript \
        -e "tell application \"Terminal\" to activate" \
        -e "tell application \"Terminal\" to do script \"cd '$UI_DIR_ESC' && $DEV_CMD\""
    sleep 2
fi

echo -e "\n  \033[32m+----------------------------------------------+\033[0m"
echo -e "  \033[32m|       GilbertTrader is running!              |\033[0m"
echo -e "  \033[32m+----------------------------------------------+\033[0m"
echo -e "  \033[36m|  API       -> http://localhost:8000           |\033[0m"
if [ "$SKIP_UI" = false ]; then
    echo -e "  \033[36m|  Dashboard -> http://localhost:8080           |\033[0m"
fi
echo -e "  \033[32m+----------------------------------------------+\033[0m"

if grep -Eiq '^LIVE_TRADING_ENABLED[[:space:]]*=[[:space:]]*true' .env; then
    echo -e "  \033[31m|  LIVE TRADING ENABLED -- REAL MONEY RISK      |\033[0m"
else
    echo -e "  \033[33m|  Paper/Safe mode: LIVE_TRADING_ENABLED=false  |\033[0m"
fi

echo -e "  \033[32m+----------------------------------------------+\033[0m\n"

if [ "$SKIP_UI" = false ]; then
    info "Opening dashboard in 5 seconds..."
    sleep 5
    open "http://localhost:8080"
fi

info "Done. This controller panel will shut down in 3 seconds."
sleep 3
exit 0
