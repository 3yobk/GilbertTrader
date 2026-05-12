"""
main.py — GilbertTrader: AI Brain Trading System
=================================================
Fixes applied:
  - Tradier token passed to MarketFeed (real-time data)
  - signal_parser imported and used alongside trade_engine parser
  - webull package added to requirements
"""

import requests
import logging
import sys
import os
import io
import time
import threading
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv()

from webull_engine   import WebullEngine
from trade_engine    import TradeEngine
from market_feed     import MarketFeed
from discord_listener import DiscordListener
from signal_parser   import parse_signal_safe
import research_scanner

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stdout.reconfigure(line_buffering=True)
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s \u2014 %(message)s",
    handlers=[
        logging.FileHandler("brain.log"),
        logging.StreamHandler(sys.stdout),
    ]
)

AI_PC_URL = os.getenv("AI_PC_URL", "http://192.168.0.15:5000/ask")
AI_PC_KEY = os.getenv("AI_PC_KEY", "sk-7d643db8")


def get_risk_profile():
    console.print("\n[bold yellow]\U0001f6e1\ufe0f  SELECT RISK PROFILE[/bold yellow]")
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
        console.print("\n[bold cyan]Custom Strategy[/bold cyan]")
        tp     = float(input("Profit Target % (e.g. 20): "))
        sl     = float(input("Stop Loss % (e.g. 15): "))
        rsi    = int(input("RSI Trigger (0-100): "))
        budget = int(input("Max Budget $: "))
        return {"name": "CUSTOM", "tp": tp, "sl": sl, "rsi": rsi, "budget": budget}
    elif choice == "1":
        return {"name": "SAFE",   "tp": 15.0,  "sl": 10.0, "rsi": 25, "budget": 150}
    elif choice == "3":
        return {"name": "RISKY",  "tp": 100.0, "sl": 40.0, "rsi": 35, "budget": 500}
    else:
        return {"name": "NORMAL", "tp": 30.0,  "sl": 20.0, "rsi": 30, "budget": 300}


def ask_ai_for_verdict(signal_text, budget):
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
        response = requests.post(AI_PC_URL, json=payload, headers=headers, timeout=45)
        return response.json().get("answer", "").strip()
    except Exception:
        return "Rejected: AI PC Offline or Timeout"


def run_health_check(discord_token, discord_channel_id):
    console.print("\n[bold cyan]System Health Check[/bold cyan]")
    all_ok = True

    if not discord_token:
        console.print("  [red]x[/red] DISCORD_TOKEN not set")
        all_ok = False
    else:
        console.print("  [green]v[/green] Discord token loaded")

    if not discord_channel_id:
        console.print("  [red]x[/red] DISCORD_CHANNEL_ID not set")
        all_ok = False
    else:
        console.print("  [green]v[/green] Discord channel ID loaded")

    try:
        r = requests.get(
            AI_PC_URL.replace("/ask", "/sessions"),
            headers={"Authorization": f"Bearer {AI_PC_KEY}"},
            timeout=5
        )
        console.print(f"  [green]v[/green] AI PC reachable" if r.ok
                      else f"  [yellow]![/yellow] AI PC status {r.status_code}")
    except Exception:
        console.print("  [yellow]![/yellow] AI PC offline")

    try:
        import yfinance as yf
        last = getattr(yf.Ticker("SPY").fast_info, "last_price", None)
        if last:
            console.print("  [green]v[/green] Market data available")
    except Exception:
        console.print("  [red]x[/red] yfinance error")
        all_ok = False

    tradier_token = os.getenv("TRADIER_TOKEN", "")
    if tradier_token:
        console.print("  [green]v[/green] Tradier token loaded (live options feed)")
    else:
        console.print("  [yellow]![/yellow] No TRADIER_TOKEN — using yfinance (delayed)")

    webull_email = os.getenv("WEBULL_EMAIL", "")
    if webull_email:
        console.print("  [green]v[/green] Webull API credentials set")
    else:
        console.print("  [yellow]![/yellow] No WEBULL_EMAIL — browser-only mode")

    if not all_ok:
        console.print("\n[bold red]Health check FAILED.[/bold red]")
        sys.exit(1)

    console.print("[bold green]All systems ready.[/bold green]\n")


def main():
    profile = get_risk_profile()

    discord_token      = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
    tradier_token      = os.getenv("TRADIER_TOKEN", "")

    run_health_check(discord_token, discord_channel_id)

    console.print(Panel(
        Text.assemble(
            ("GILBERT TRADER SYSTEM ARMED\n", "bold white"),
            (f"Active Strategy: {profile['name']}\n", "bold green"),
            (f"Parameters: TP {profile['tp']}% | SL {profile['sl']}% | RSI <{profile['rsi']} | Budget ${profile['budget']}", "dim"),
        ),
        border_style="bright_blue", padding=(1, 4),
    ))

    # ── Webull engine (paper=True for safety, flip to False for real money) ──
    webull = WebullEngine(paper=True)
    webull.open_browser()

    # ── Market feed — Tradier if token set, yfinance fallback ───────────────
    market = MarketFeed(api_key=tradier_token)
    market.start()

    # ── Discord command handler ───────────────────────────────────────────────
    def handle_discord_command(cmd: str, args: str):
        """Handles !commands typed in Discord."""
        cmd = cmd.lower().strip()

        if cmd == "status":
            with engine._lock:
                positions = dict(engine._positions)
            if not positions:
                listener.send_status_update("No open positions.")
            else:
                lines = ["**Open Positions:**"]
                for key, pos in positions.items():
                    lines.append(
                        f"  {pos.signal.ticker} {pos.signal.strike}{pos.signal.option_type} "
                        f"exp {pos.signal.expiry} | entry ${pos.entry_price:.2f} | "
                        f"peak {pos.highest_pnl:+.1f}%"
                    )
                pnl = engine.get_session_pnl()
                lines.append(f"**Session P&L**: ${pnl:.2f}")
                listener.send_status_update("\n".join(lines))

        elif cmd == "scan":
            listener.send_status_update("Forcing immediate scan...")
            found = research_scanner.scan_markets(
                rsi_threshold=profile["rsi"], listener=listener,
                market=market, budget=float(profile["budget"]))
            if not found:
                listener.send_status_update("Scan complete — no signals found.")

        elif cmd == "close" and args.upper() == "ALL":
            with engine._lock:
                positions = dict(engine._positions)
            if not positions:
                listener.send_status_update("No positions to close.")
            else:
                listener.send_status_update(f"Emergency closing {len(positions)} positions...")
                for key, pos in positions.items():
                    engine._auto_close(key, pos, reason="Manual Close All")

        elif cmd == "profile":
            profiles = {
                "safe":   {"name":"SAFE",   "tp":15.0,  "sl":10.0, "rsi":25, "budget":150},
                "normal": {"name":"NORMAL", "tp":30.0,  "sl":20.0, "rsi":30, "budget":300},
                "risky":  {"name":"RISKY",  "tp":100.0, "sl":40.0, "rsi":35, "budget":500},
            }
            new_profile = profiles.get(args.lower())
            if new_profile:
                profile.update(new_profile)
                engine.profit_target_pct = new_profile["tp"]
                engine.stop_loss_pct     = new_profile["sl"]
                listener.send_status_update(
                    f"Profile switched to **{new_profile['name']}** | "
                    f"TP {new_profile['tp']}% | SL {new_profile['sl']}% | "
                    f"RSI <{new_profile['rsi']} | Budget ${new_profile['budget']}")
            else:
                listener.send_status_update("Usage: !profile safe|normal|risky")

        elif cmd == "watchlist":
            import research_scanner as rs
            tickers = ", ".join(rs.MY_SMALL_STOCKS)
            listener.send_status_update(f"**Watchlist**: {tickers}")

        elif cmd == "add":
            ticker = args.upper().strip()
            if ticker:
                import research_scanner as rs
                if ticker not in rs.MY_SMALL_STOCKS:
                    rs.MY_SMALL_STOCKS.append(ticker)
                    listener.send_status_update(f"Added **{ticker}** to watchlist.")
                else:
                    listener.send_status_update(f"{ticker} already in watchlist.")

        elif cmd == "remove":
            ticker = args.upper().strip()
            import research_scanner as rs
            if ticker in rs.MY_SMALL_STOCKS:
                rs.MY_SMALL_STOCKS.remove(ticker)
                listener.send_status_update(f"Removed **{ticker}** from watchlist.")
            else:
                listener.send_status_update(f"{ticker} not in watchlist.")

    listener = DiscordListener(
        channel_id=discord_channel_id,
        token=discord_token,
        on_signal=lambda x: None,
        on_command=handle_discord_command,
    )
    listener.start_in_thread()

    engine = TradeEngine(
        webull=webull, market=market, paper=True,
        profit_target_pct=profile['tp'],
        stop_loss_pct=profile['sl'],
        listener=listener
    )

    console.print("\n1. Discord | 2. Research | 3. Hybrid")
    mode_choice = input("\nSelect Operation Mode (1/2/3): ").strip()

    MAX_DAILY_LOSS     = -150.0
    last_research_time = 0
    trade_cooldowns    = {}
    last_heat_alert_ts = 0

    try:
        while True:
            daily_pnl = engine.get_session_pnl()
            if daily_pnl <= MAX_DAILY_LOSS:
                msg = "CIRCUIT BREAKER: Daily loss limit hit. Shutting down."
                listener.send_status_update(f"CIRCUIT BREAKER: {daily_pnl:.2f} hit. Stopping.")
                console.print(Panel(msg, border_style="bold red"))
                break

            if len(engine._positions) >= 2:
                now_ts = time.time()
                if now_ts - last_heat_alert_ts >= 120:
                    listener.send_status_update("Portfolio heat: 2 positions open. Waiting.")
                    last_heat_alert_ts = now_ts
                time.sleep(10)
                continue

            raw_text = None

            # Research / scanner mode
            if mode_choice in ["2", "3"]:
                if time.time() - last_research_time > 300:
                    console.print(f"[dim]Scanning Small-Caps (RSI < {profile['rsi']})...[/dim]")
                    found = research_scanner.scan_markets(
                        rsi_threshold=profile['rsi'], listener=listener,
                        market=market, budget=float(profile['budget']))
                    if found:
                        raw_text = found[0]
                    last_research_time = time.time()

            # Discord mode
            if not raw_text and mode_choice in ["1", "3"]:
                raw_text = listener.get_latest_signal()

            if raw_text:
                # Try signal_parser first (more robust), fall back to trade_engine parser
                parsed_sp = parse_signal_safe(raw_text)
                parsed_te = engine.parse_signal(raw_text)
                parsed    = parsed_te  # trade_engine format used by _execute()

                ticker = parsed.ticker if parsed else (parsed_sp.ticker if parsed_sp else "UNK")

                if ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
                    pass  # cooldown active
                else:
                    console.print(f"Signal: {raw_text[:60]}")
                    console.print("[dim]Checking with AI Risk Manager...[/dim]")

                    verdict = ask_ai_for_verdict(raw_text, profile['budget'])

                    if "BTO" in verdict:
                        listener.send_status_update(f"AI APPROVED: {verdict}")
                        console.print(f"[bold green]APPROVED: {verdict}[/bold green]")
                        trade_cooldowns[ticker] = time.time()
                        engine.on_raw_signal(verdict)
                    else:
                        listener.send_status_update(f"AI REJECTED: {verdict}")
                        console.print(f"[bold red]REJECTED: {verdict}[/bold red]")

            # Handle UI close requests
            try:
                from api_server import _bot_state
                if _bot_state.get("close_all_requested"):
                    _bot_state["close_all_requested"] = False
                    with engine._lock:
                        positions = dict(engine._positions)
                    for key, pos in positions.items():
                        engine._auto_close(key, pos, reason="UI Close All")
                for contract in list(_bot_state.get("close_requested", [])):
                    _bot_state["close_requested"].remove(contract)
                    with engine._lock:
                        positions = dict(engine._positions)
                    for key, pos in positions.items():
                        sig = pos.signal
                        if f"{sig.ticker} {sig.strike}{sig.option_type}" == contract:
                            engine._auto_close(key, pos, reason="UI Close")
                            break
                # Sync paused state from UI
                if _bot_state.get("paused") and not listener.paused:
                    listener.paused = True
                elif not _bot_state.get("paused") and listener.paused:
                    listener.paused = False
            except ImportError:
                pass

            time.sleep(1)
            if int(time.time()) % 30 == 0:
                engine.print_status()

    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown...[/yellow]")
        webull.close()
        market.stop()
        listener.stop()


if __name__ == "__main__":
    main()