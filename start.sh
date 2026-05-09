#!/bin/bash
# start.sh — GilbertTrader launcher
# Run once: chmod +x start.sh
# Then every time: ./start.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "================================================"
echo "   GilbertTrader — Startup Script"
echo "================================================"
echo ""

# 1. Check .env exists
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found."
    echo "Copy .env.example to .env and fill in your credentials."
    echo "  cp .env.example .env"
    exit 1
fi

# 2. Install / upgrade Python deps silently
echo "Checking dependencies..."
pip install -q -r requirements.txt

# 3. Ensure Playwright Chromium is installed
echo "Checking Playwright browser..."
python -m playwright install chromium --quiet 2>/dev/null || true

# 4. Launch Streamlit dashboard in the background
echo "Starting dashboard on http://localhost:8501 ..."
streamlit run dashboard.py \
    --server.headless true \
    --server.port 8501 \
    --browser.gatherUsageStats false \
    > dashboard.log 2>&1 &
DASHBOARD_PID=$!
echo "Dashboard PID: $DASHBOARD_PID"

# Give Streamlit a moment to bind the port
sleep 2

# 5. Launch the main trading bot (foreground — Ctrl+C stops everything)
echo ""
echo "Launching GilbertTrader..."
echo "------------------------------------------------"
python main.py

# Cleanup: kill dashboard when main.py exits
kill $DASHBOARD_PID 2>/dev/null || true
echo "Dashboard stopped."
