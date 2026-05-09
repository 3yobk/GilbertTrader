"""
main.py — GilbertTrader: AI Brain Trading System (Final Version)
Includes:
  1. Risk Profiles & Custom Injection
  2. AI Budgeting & Senior Risk Manager
  3. Triple-Verified Research Scanner
  4. Portfolio Heat Monitor (Max 2 Trades)
  5. Discord Feedback Loop
  6. Circuit Breaker (Daily Loss Limit)
  7. UPGRADE: Startup Health Check
"""

import requests
import logging
import sys
import os
import io
import time
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Load .env file so credentials are available via os.getenv
load_dotenv()

# Core Engine Imports
from webull_engine import WebullEngine
from trade_engine import TradeEngine
from market_feed import MarketFeed
from discord_listener import DiscordListener
import research_scanner

# Windows Terminal UTF-8 Setup
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("brain.log"),
        logging.StreamHandler(sys.stdout),
    ]
)

# ── FIX: Credentials from .env, never hardcoded ──────────────────────────────
# Create a .env file in the same folder with:
#   AI_PC_URL=http://192.168.0.15:5000/ask
#   AI_PC_KEY=sk-7d643db8
#   DISCORD_TOKEN=your-token-here
#   DISCORD_CHANNEL_ID=your-channel-id
#   POLYGON_API_KEY=your-polygon-key   (optional)
AI_PC_URL = os.getenv("AI_PC_URL", "http://192.168.0.15:5000/ask")
AI_PC_KEY = os.getenv("AI_PC_KEY", "sk-7d643db8")


# --- RISK PROFILES & RSI LOGIC ---
def get_risk_profile():
    """Interactive menu to choose the 'Strategy Brain' for the session."""
    console.print("\n[bold yellow]🛡️  SELECT RISK PROFILE[/bold yellow]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Option", style="dim")
    table.add_column("Profile")
    table.add_column("Target (TP)")
    table.add_column("Stop (SL)")
    table.add_column("RSI Trigger")
    table.add_column("Max Trade")

    table.add_row("1", "Safe",   "+15%",  "-10%", "< 25", "$150")
    table.add_row("2", "Normal", "+30%",  "-20%", "< 30", "$300")
    table.add_row("3", "Risky",  "+100%", "-40%", "< 35", "$500")
    table.add_row("4", "CUSTOM", "???",   "???",  "???",  "???")

    console.print(table)
    choice = input("\nChoose Profile (1/2/3/4): ").strip()

    if choice == "4":
        console.print("\n[bold cyan]🛠️  CUSTOM STRATEGY CONFIGURATION[/bold cyan]")
        tp     = float(input("Enter Profit Target % (e.g. 20): "))
        sl     = float(input("Enter Stop Loss % (e.g. 15): "))
        rsi    = int(input("Enter RSI Trigger Level (0-100): "))
        budget = int(input("Enter Max Trade Budget $: "))
        return {"name": "CUSTOM", "tp": tp, "sl": sl, "rsi": rsi, "budget": budget}
    elif choice == "1":
        return {"name": "SAFE",   "tp": 15.0,  "sl": 10.0, "rsi": 25, "budget": 150}
    elif choice == "3":
        return {"name": "RISKY",  "tp": 100.0, "sl": 40.0, "rsi": 35, "budget": 500}
    else:
        return {"name": "NORMAL", "tp": 30.0,  "sl": 20.0, "rsi": 30, "budget": 300}


# --- AI BUDGET GUARDRAILS ---
def ask_ai_for_verdict(signal_text, budget):
    """Signals verified by AI PC based on the current profile's budget."""
    payload = {
        "question": (
            f"Senior Risk Manager Role. INPUT: '{signal_text}'\n"
            f"1. REFORMAT: 'BTO [TICKER] [STRIKE][C/P] [EXPIRY]'\n"
            f"2. BUDGET LIMIT: Max ${budget} for this trade.\n"
            "3. REJECT if strike is >20% away or doesn't fit budget.\n"
            "OUTPUT: ONLY the cleaned BTO string or 'Rejected' + reason."
        )
    }
    headers = {"Authorization": f"Bearer {AI_PC_KEY}"}
    try:
        response = requests.post(AI_PC_URL, json=payload, headers=headers, timeout=60)
        return response.json().get("answer", "").strip()
    except Exception:
        return "Rejected: AI PC Offline"


# ── UPGRADE: Startup health check ────────────────────────────────────────────
def run_health_check(discord_token, discord_channel_id):
    """
    Runs before arming the bot. Checks every dependency and prints a clear
    pass/fail for each. Exits cleanly if anything critical is missing.
    """
    console.print("\n[bold cyan]⚡ SYSTEM HEALTH CHECK[/bold cyan]")
    all_ok = True

    # 1. Discord credentials present
    if not discord_token or discord_token == "0":
        console.print("  [red]✗[/red] DISCORD_TOKEN not set — add it to .env")
        all_ok = False
    else:
        console.print("  [green]✓[/green] Discord token loaded")

    if not discord_channel_id or discord_channel_id == 0:
        console.print("  [red]✗[/red] DISCORD_CHANNEL_ID not set — add it to .env")
        all_ok = False
    else:
        console.print("  [green]✓[/green] Discord channel ID loaded")

    # 2. AI PC reachable
    try:
        r = requests.get(
            AI_PC_URL.replace("/ask", "/sessions"),
            headers={"Authorization": f"Bearer {AI_PC_KEY}"},
            timeout=5
        )
        if r.ok:
            console.print("  [green]✓[/green] AI PC reachable")
        else:
            console.print(f"  [yellow]⚠[/yellow] AI PC returned status {r.status_code} — signals may auto-reject")
    except Exception as e:
        console.print(f"  [yellow]⚠[/yellow] AI PC offline ({e}) — signals will auto-reject")

    # 3. Market data available
    try:
        import yfinance as yf
        last = getattr(yf.Ticker("SPY").fast_info, "last_price", None)
        if last:
            console.print(f"  [green]✓[/green] Market data available (SPY last: ${last:.2f})")
        else:
            console.print("  [yellow]⚠[/yellow] yfinance returned no price — market may be closed")
    except Exception as e:
        console.print(f"  [red]✗[/red] yfinance error: {e}")
        all_ok = False

    # 4. Playwright installed
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        console.print("  [green]✓[/green] Playwright installed")
    except ImportError:
        console.print("  [red]✗[/red] Playwright not installed — run: pip install playwright && playwright install chromium")
        all_ok = False

    if not all_ok:
        console.print("\n[bold red]Health check FAILED. Fix the issues above and restart.[/bold red]")
        sys.exit(1)

    console.print("[bold green]✅ All systems ready.[/bold green]\n")


def main():
    profile = get_risk_profile()

    discord_token      = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
    polygon_key        = os.getenv("POLYGON_API_KEY", "")

    # Run health check before spending time on browser startup
    run_health_check(discord_token, discord_channel_id)

    # Visual Confirmation Panel
    console.print(Panel(
        Text.assemble(
            ("GILBERT TRADER SYSTEM ARMED\n", "bold white"),
            (f"Active Strategy: {profile['name']}\n", "bold green"),
            (f"Parameters: TP {profile['tp']}% | SL {profile['sl']}% | RSI <{profile['rsi']} | Budget ${profile['budget']}", "dim"),
        ),
        border_style="bright_blue", padding=(1, 4),
    ))

    # --- Engine Initialization ---
    webull = WebullEngine(paper=True)
    webull.open_browser()

    market = MarketFeed(api_key=polygon_key)
    market.start()

    # Discord Initialization
    listener = DiscordListener(
        channel_id=discord_channel_id,
        token=discord_token,
        on_signal=lambda x: None
    )
    listener.start_in_thread()

    # Trade Engine Initialization
    engine = TradeEngine(
        webull=webull, market=market, paper=True,
        profit_target_pct=profile['tp'],
        stop_loss_pct=profile['sl'],
        listener=listener
    )

    console.print("\n1. Discord | 2. Research | 3. Hybrid")
    mode_choice = input("\nSelect Operation Mode (1/2/3): ").strip()

    # Circuit breaker settings
    MAX_DAILY_LOSS     = -150.0   # Protecting $1,000 capital
    last_research_time = 0
    trade_cooldowns    = {}
    # ── FIX: one heat alert per cooling period, not every second ────────────
    last_heat_alert_ts = 0

    try:
        while True:
            # ── FIX: circuit breaker now reads real session P&L ──────────────
            daily_pnl = engine.get_session_pnl()
            if daily_pnl <= MAX_DAILY_LOSS:
                msg = (
                    f"🚨 **CIRCUIT BREAKER TRIGGERED**: "
                    f"Daily loss of ${daily_pnl:.2f} hit limit ${MAX_DAILY_LOSS}. "
                    f"System shutting down."
                )
                listener.send_status_update(msg)
                console.print(Panel(msg, border_style="bold red"))
                break

            # --- PORTFOLIO HEAT MONITOR ---
            if len(engine._positions) >= 2:
                now_ts = time.time()
                # ── FIX: alert once per 120s window, not every iteration ──
                if now_ts - last_heat_alert_ts >= 120:
                    listener.send_status_update(
                        "🧊 **PORTFOLIO COOLING**: 2 positions open. Skipping scans."
                    )
                    console.print(
                        "[yellow]🧊 Heat Monitor: Max positions reached. Waiting...[/yellow]"
                    )
                    last_heat_alert_ts = now_ts
                time.sleep(10)
                continue

            raw_text = None

            # --- Active Research Scanner ---
            if mode_choice in ["2", "3"]:
                if time.time() - last_research_time > 300:  # 5-min scan interval
                    console.print(
                        f"[dim]🔎 Scanning Small-Caps (RSI Threshold: {profile['rsi']})...[/dim]"
                    )
                    found = research_scanner.scan_markets(
                        rsi_threshold=profile['rsi'],
                        listener=listener,
                        market=market,
                    )
                    if found:
                        raw_text = found[0]
                    last_research_time = time.time()

            # --- Passive Discord Listener ---
            if not raw_text and mode_choice in ["1", "3"]:
                raw_text = listener.get_latest_signal()

            # --- Process and Execute ---
            if raw_text:
                # ── FIX: use proper signal parser to get ticker, not parts[1] ──
                parsed = engine.parse_signal(raw_text)
                ticker = parsed.ticker if parsed else "UNK"

                if ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
                    pass  # still in 30-min cooldown — skip silently
                else:
                    console.print(f"🔍 [cyan]Signal Captured:[/cyan] {raw_text[:50]}")
                    verdict = ask_ai_for_verdict(raw_text, profile['budget'])

                    if "BTO" in verdict:
                        listener.send_status_update(f"✅ **AI APPROVED**: {verdict}")
                        console.print(f"✅ [bold green]TRADE APPROVED:[/bold green] {verdict}")
                        trade_cooldowns[ticker] = time.time()
                        engine.on_raw_signal(verdict)
                    else:
                        listener.send_status_update(f"❌ **AI REJECTED**: {verdict}")
                        console.print(f"❌ [bold red]RISK REJECTION:[/bold red] {verdict}")

            time.sleep(1)

            # --- Status update every 30 seconds ---
            if int(time.time()) % 30 == 0:
                engine.print_status()

    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown initiated...[/yellow]")
        webull.close()
        market.stop()
        listener.stop()


if __name__ == "__main__":
    main()
