#!/bin/bash
# ============================================================
#  GilbertTrader -- MAC OS ONE-CLICK STARTUP
#  First run: ~2 min. Every run after: ~2 seconds.
#  Checks presence by folder -- never calls pip if installed.
# ============================================================

set -e

# Terminal window properties
printf "\033]0;GilbertTrader Setup and Launch\007"
Clear-Host() { printf "\033[H\033[2J"; }

# Text Styling
info() { echo -e "  \033[36m$1\033[0m"; }
ok()   { echo -e "  \033[32mOK  $1\033[0m"; }
warn() { echo -e "  \033[33m!!  $1\033[0m"; }
fail() { echo -e "  \033[31mXX  $1\033[0m"; exit 1; }
head() { echo -e "\n\033[35m===  $1  ===\033[0m"; }

Clear-Host
echo -e "\n  \033[36mGILBERT TRADER v2.0.1 (macOS)\033[0m\n"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
SITE="$VENV_DIR/lib/python3."*"/site-packages"

# ============================================================
# STEP 1 -- Credentials
# ============================================================
head "STEP 1 -- Credentials"

if [ ! -f ".env" ]; then
    warn ".env not found -- creating template..."
    cat << EOF > .env
DISCORD_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE
DISCORD_CHANNEL_ID=PASTE_YOUR_CHANNEL_ID_HERE
AI_PC_URL=http://YOUR_AI_PC_IP:5000/ask
AI_PC_KEY=sk-your-key-here
TRADIER_TOKEN=PASTE_YOUR_TRADIER_TOKEN_HERE
TRADIER_SANDBOX=false
WEBULL_EMAIL=your@email.com
WEBULL_TRADING_PIN=123456
LIVE_TRADING_ENABLED=false
TEST_BUDGET=300
EOF
    read -p "  Open .env in Nano text editor now? (y/n): " choice
    if [ "$choice" = "y" ]; then nano .env; fi
    exit 0
fi

if grep -q "PASTE_YOUR" .env; then
    warn ".env has placeholder values -- fill it in first."
    read -p "  Open .env now? (y/n): " choice
    if [ "$choice" = "y" ]; then nano .env; fi
    exit 0
fi
ok ".env ready"

# ============================================================
# STEP 2 -- Python Check
# ============================================================
head "STEP 2 -- Python"

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v $cmd &> /dev/null; then
        ver=$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo $ver | cut -d. -f1)
        minor=$(echo $ver | cut -d. -f2)
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD=$cmd
            ok "Found: Python $ver"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    fail "Python 3.10+ not found. Install via Homebrew ('brew install python') or python.org"
fi

# ============================================================
# STEP 3 -- Virtual Environment
# ============================================================
head "STEP 3 -- Virtual Environment"

if [ ! -f ".venv/bin/python" ]; then
    info "Creating .venv ..."
    $PYTHON_CMD -m venv .venv
    # Dynamic link setup for package path resolution
    SITE=$(ls -d $SCRIPT_DIR/.venv/lib/python3.*/site-packages)
    "$VENV_PY" -m pip install --upgrade pip --quiet
    ok ".venv created"
else
    SITE=$(ls -d $SCRIPT_DIR/.venv/lib/python3.*/site-packages)
    ok ".venv exists"
fi

# ============================================================
# STEP 4 -- Instant Package Folder Scans
# ============================================================
head "STEP 4 -- Python Packages"

declare -A PACKAGES=(
    ["playwright==1.59.0"]="playwright"
    ["playwright-stealth"]="playwright_stealth"
    ["webull"]="webull"
    ["yfinance"]="yfinance"
    ["pandas"]="pandas"
    ["pandas-ta>=0.4.67b0"]="pandas_ta"
    ["numpy<2.0"]="numpy"
    ["pytz"]="pytz"
    ["streamlit"]="streamlit"
    ["plotly"]="plotly"
    ["rich"]="rich"
    ["discord.py"]="discord"
    ["Flask"]="flask"
    ["flask-cors"]="flask_cors"
    ["requests"]="requests"
    ["python-dotenv"]="dotenv"
    ["websocket-client"]="websocket"
    ["tqdm"]="tqdm"
)

missing=()
for pkg in "${!PACKAGES[@]}"; do
    folder=${PACKAGES[$pkg]}
    if [ ! -d "$SITE/$folder" ]; then
        missing+=("$pkg")
    fi
done

if [ ${#missing[@]} -eq 0 ]; then
    ok "All packages present"
fi

# Numba shim layer for quick execution
if [ ! -d "$SITE/numba" ]; then
    mkdir -p "$SITE/numba"
    echo -e "def njit(f=None, **kw):\n    return f if f else (lambda x: x)" > "$SITE/numba/__init__.py"
    ok "numba shim written"
else
    ok "numba shim present"
fi

# ============================================================
# STEP 5 -- Playwright Core Elements
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
# STEP 6 -- React Environment / node_modules Checks
# ============================================================
head "STEP 6 -- Frontend"

USE_NPM=false; USE_BUN=false; SKIP_UI=false; DEV_CMD=""
if command -v bun &> /dev/null; then USE_BUN=true; DEV_CMD="bun run dev"; fi
if [ "$USE_BUN" = false ] && command -v npm &> /dev/null; then USE_NPM=true; DEV_CMD="npm run dev"; fi

if [ "$USE_BUN" = false ] && [ "$USE_NPM" = false ]; then
    warn "Node.js not found -- dashboard skipped. Install from https://nodejs.org"
    SKIP_UI=true
fi

if [ "$SKIP_UI" = false ]; then
    UI_DIR="$SCRIPT_DIR/pixel-perfect"
    if [ ! -d "$UI_DIR/node_modules" ]; then
        info "Installing React deps (first run only) ..."
        if [ "$USE_BUN" = true ]; then
            cd "$UI_DIR" && bun install
        else
            cd "$UI_DIR" && npm install
        fi
        ok "React deps installed"
    else
        ok "node_modules present"
    fi

    if [ ! -f "$UI_DIR/.env.local" ]; then
        if [ -f "$UI_DIR/.env" ]; then
            cp "$UI_DIR/.env" "$UI_DIR/.env.local"
        else
            echo -e "VITE_DATA_PROVIDER=http\nVITE_API_BASE=http://localhost:8000" > "$UI_DIR/.env.local"
        fi
        ok "Created .env.local"
    else
        ok ".env.local present"
    fi
fi

# ============================================================
# STEP 7 -- Spawn Split-Processes to macOS Terminal Tabs
# ============================================================
head "STEP 7 -- Launch"

cd "$SCRIPT_DIR"

info "Starting API server (port 8000) ..."
osascript -e "tell application \"Terminal\" to do script \"cd '$SCRIPT_DIR' && '$VENV_PY' api_server.py\""
sleep 3

info "Starting bot threads ..."
osascript -e "tell application \"Terminal\" to do script \"cd '$SCRIPT_DIR' && '$VENV_PY' main.py\""
sleep 2

if [ "$SKIP_UI" = false ]; then
    info "Starting React dashboard framework (port 8080) ..."
    osascript -e "tell application \"Terminal\" to do script \"cd '$UI_DIR' && $DEV_CMD\""
    sleep 2
fi

echo -e "\n  \033[32m+----------------------------------------------+\033[0m"
echo -e "  \033[32m|       GilbertTrader is running!              |\033[0m"
echo -e "  \033[32m+----------------------------------------------+\033[0m"
echo -e "  \033[36m|  API     ->  http://localhost:8000           |\033[0m"
if [ "$SKIP_UI" = false ]; then
    echo -e "  \033[36m|  Dashboard -> http://localhost:8080          |\033[0m"
fi
echo -e "  \033[32m+----------------------------------------------+\033[0m"
echo -e "  \033[33m|  paper=True  |  LIVE_TRADING=false           |\033[0m"
echo -e "  \033[32m+----------------------------------------------+\033[0m\n"

if [ "$SKIP_UI" = false ]; then
    info "Opening browser client canvas in 5 seconds..."
    sleep 5
    open "http://localhost:8080"
fi

info "Done. This controller panel will shut down in 3 seconds."
sleep 3
exit 0