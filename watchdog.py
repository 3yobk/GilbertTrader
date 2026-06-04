"""
watchdog.py — Auto-restart GilbertTrader if it crashes
========================================================
Optimizations Applied:
  - Fixed standard output blocking by injecting the unbuffered python binary flag (-u).
  - Extended market hour logic to 5:00 PM ET to fully wrap and protect the Friday reinforcement run.
  - Added strict execution directory anchors to prevent cross-relative path issues.
"""

import subprocess
import sys
import os
import time
import logging
import requests
from datetime import datetime, date

# ── Config ────────────────────────────────────────────────────────────────────
MAIN_SCRIPT      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchdog.log")
RESTART_COOLDOWN = 30      # seconds between restarts
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
    """Send a message to Discord channel on crash."""
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
    """
    Returns True during pre-market, market hours, and post-close reinforcement.
    Extended to 5:00 PM ET so the Friday 4:30 PM reinforce_brain run
    is never interrupted by the watchdog sleeping early.
    """
    try:
        import pytz
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        if now.weekday() >= 5:   # Saturday/Sunday
            return False
        hour = now.hour + now.minute / 60
        return 8.0 <= hour <= 17.0
    except Exception:
        return True   # assume open on error


def run():
    restarts_today    = 0
    last_restart_date = date.today()
    crash_count       = 0

    log.info("Watchdog started — monitoring main.py")
    print("🐕 GilbertTrader Watchdog started")
    print(f"   Script:  {MAIN_SCRIPT}")
    print(f"   Max restarts/day: {MAX_RESTARTS_PER_DAY}")
    print(f"   Cooldown: {RESTART_COOLDOWN}s between restarts")
    print(f"   Active window: 8:00 AM – 5:00 PM ET (Mon–Fri)\n")

    while True:
        # Reset daily counter at midnight
        if date.today() != last_restart_date:
            restarts_today    = 0
            last_restart_date = date.today()

        # Don't restart if market window is closed
        if not is_market_hours():
            print("⏰ Outside market window — watchdog sleeping (30 min)")
            log.info("Outside market window — sleeping")
            time.sleep(1800)
            continue

        # Safety limit — too many crashes today
        if restarts_today >= MAX_RESTARTS_PER_DAY:
            msg = (f"⛔ **WATCHDOG STOPPED**: {restarts_today} crashes today — "
                   f"too many restarts. Check watchdog.log.")
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
            # -u: unbuffered stdout — prevents stream deadlock with Chromium
            # cwd: explicit working directory anchor — prevents relative path errors
            proc = subprocess.Popen(
                [sys.executable, "-u", MAIN_SCRIPT],
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            # Poll every 5s — keeps watchdog responsive without busy-waiting
            while proc.poll() is None:
                time.sleep(5)
            exit_code = proc.returncode

        except Exception as e:
            exit_code = -1
            log.error("Failed to start main.py: %s", e)

        runtime     = time.time() - start_time
        runtime_str = f"{runtime:.0f}s"

        if exit_code == 0:
            print("✅ GilbertTrader exited cleanly (code 0) — not restarting")
            log.info("Clean exit after %s — stopping watchdog", runtime_str)
            break

        # Crash detected
        crash_count    += 1
        restarts_today += 1

        msg = (
            f"⚠️ **CRASH DETECTED** — GilbertTrader crashed after {runtime_str} "
            f"(exit code {exit_code}). Restart #{restarts_today} in "
            f"{RESTART_COOLDOWN}s. Check watchdog.log."
        )
        print(f"\n{msg}\n")
        log.error("Crash #%d: exit_code=%d runtime=%s", crash_count, exit_code, runtime_str)
        send_discord_alert(msg)

        print(f"⏳ Waiting {RESTART_COOLDOWN}s before restart...")
        time.sleep(RESTART_COOLDOWN)


if __name__ == "__main__":
    run()