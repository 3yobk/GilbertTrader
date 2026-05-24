"""
watchdog.py — Auto-restart GilbertTrader if it crashes
========================================================
Run this INSTEAD of main.py:
    python watchdog.py

It starts main.py as a subprocess and restarts it automatically
within 30 seconds if it crashes. Logs crash times to watchdog.log.

Features:
- Restarts up to 10 times per day (prevents infinite crash loops)
- 30-second cooldown between restarts (lets Webull session settle)
- Sends Discord alert on crash + restart
- Stops restarting if market is closed (no point)
- Logs all crashes to watchdog.log

Usage:
    python watchdog.py
    # or run via Task Scheduler / startup script
"""

import subprocess
import sys
import os
import time
import logging
import json
import requests
from datetime import datetime, date

# ── Config ────────────────────────────────────────────────────────────────────
MAIN_SCRIPT     = os.path.join(os.path.dirname(__file__), "main.py")
LOG_FILE        = os.path.join(os.path.dirname(__file__), "watchdog.log")
RESTART_COOLDOWN = 30     # seconds between restarts
MAX_RESTARTS_PER_DAY = 10  # safety limit

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("watchdog")

# ── Load Discord token for crash alerts ───────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL = os.getenv("DISCORD_CHANNEL_ID", "")


def send_discord_alert(message: str):
    """Send a message to Discord channel."""
    if not DISCORD_TOKEN or not DISCORD_CHANNEL:
        return
    try:
        requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}",
                     "Content-Type": "application/json"},
            json={"content": message},
            timeout=5,
        )
    except Exception:
        pass


def is_market_hours() -> bool:
    """Returns True during pre-market + market hours (8 AM - 4:30 PM ET Mon-Fri)."""
    try:
        import pytz
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        if now.weekday() >= 5:   # Saturday/Sunday
            return False
        hour = now.hour + now.minute / 60
        return 8.0 <= hour <= 16.5
    except Exception:
        return True   # assume open on error


def run():
    restarts_today = 0
    last_restart_date = date.today()
    crash_count = 0

    log.info("Watchdog started — monitoring main.py")
    print("🐕 GilbertTrader Watchdog started")
    print(f"   Script: {MAIN_SCRIPT}")
    print(f"   Max restarts/day: {MAX_RESTARTS_PER_DAY}")
    print(f"   Cooldown: {RESTART_COOLDOWN}s between restarts\n")

    while True:
        # Reset daily counter at midnight
        if date.today() != last_restart_date:
            restarts_today    = 0
            last_restart_date = date.today()

        # Don't restart if market is closed
        if not is_market_hours():
            print("⏰ Market closed — watchdog sleeping until 8 AM ET")
            log.info("Market closed — sleeping")
            # Sleep until 8 AM ET next business day
            time.sleep(3600)   # check again in 1 hour
            continue

        # Don't restart if too many crashes
        if restarts_today >= MAX_RESTARTS_PER_DAY:
            msg = (f"⛔ **WATCHDOG STOPPED**: {restarts_today} crashes today — "
                   f"too many restarts. Check logs.")
            print(msg)
            log.error(msg)
            send_discord_alert(msg)
            time.sleep(3600)
            continue

        # Launch main.py
        start_time = time.time()
        print(f"🚀 Starting main.py (restart #{restarts_today}) at "
              f"{datetime.now().strftime('%H:%M:%S')}")
        log.info("Starting main.py (restart #%d)", restarts_today)

        try:
            proc = subprocess.run(
                [sys.executable, MAIN_SCRIPT],
                cwd=os.path.dirname(__file__),
            )
            exit_code = proc.returncode
        except Exception as e:
            exit_code = -1
            log.error("Failed to start main.py: %s", e)

        runtime = time.time() - start_time

        if exit_code == 0:
            # Clean exit — user pressed Ctrl+C or bot shut down gracefully
            print("✅ GilbertTrader exited cleanly (code 0) — not restarting")
            log.info("Clean exit — stopping watchdog")
            break

        # Crash detected
        crash_count    += 1
        restarts_today += 1
        runtime_str = f"{runtime:.0f}s"

        msg = (
            f"⚠️ **CRASH DETECTED** — GilbertTrader crashed after {runtime_str} "
            f"(exit code {exit_code}). Restart #{restarts_today} in "
            f"{RESTART_COOLDOWN}s. Check watchdog.log for details."
        )
        print(f"\n{msg}\n")
        log.error("Crash #%d: exit_code=%d runtime=%s", crash_count, exit_code, runtime_str)
        send_discord_alert(msg)

        # Cooldown before restart
        print(f"⏳ Waiting {RESTART_COOLDOWN}s before restart...")
        time.sleep(RESTART_COOLDOWN)


if __name__ == "__main__":
    run()
