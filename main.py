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
from datetime import datetime
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv()

log = logging.getLogger("main")

from webull_engine   import WebullEngine
from trade_engine    import TradeEngine
from market_feed     import MarketFeed
from discord_listener import DiscordListener
from signal_parser   import parse_signal_safe
import research_scanner
from alerts import (alert_circuit_breaker, alert_big_winner,
                    alert_streak_pause, alert_daily_summary, alert_vix_spike)

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
    """Call AI PC risk manager with retry on failure."""
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

    for attempt in range(3):   # try up to 3 times
        if attempt > 0:
            wait = attempt * 10   # 10s, then 20s between retries
            log.info("AI PC retry %d/%d in %ds...", attempt + 1, 3, wait)
            time.sleep(wait)
        result = {"answer": None}
        def _call():
            try:
                r = requests.post(AI_PC_URL, json=payload, headers=headers, timeout=90)
                if r.ok:
                    result["answer"] = r.json().get("answer", "").strip()
                else:
                    log.warning("AI PC HTTP %d on attempt %d", r.status_code, attempt + 1)
            except Exception as e:
                log.warning("AI PC call failed attempt %d: %s", attempt + 1, e)
        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(timeout=95)
        if result["answer"]:
            return result["answer"]

    return "Rejected: AI PC Offline after 3 attempts"


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

    # ── Webull engine — auto-detect paper vs live from .env ──────────────────
    live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    paper_mode   = not live_enabled
    if live_enabled:
        console.print("[bold red]⚠️  LIVE TRADING ENABLED — real money at risk![/bold red]")
    else:
        console.print("[bold yellow]Paper mode active (LIVE_TRADING_ENABLED=false)[/bold yellow]")
    webull = WebullEngine(paper=paper_mode)   # listener assigned below after Discord starts
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
            pnl        = engine.get_session_pnl()
            paused_str = "⏸ PAUSED" if listener.paused else "▶ ACTIVE"
            if consecutive_losses >= 1:
                streak_str = f"🔴 {consecutive_losses} loss streak"
            elif consecutive_wins >= 1:
                streak_str = f"🟢 {consecutive_wins} win streak"
            else:
                streak_str = "➖ No streak"
            header = f"**{paused_str}** | {streak_str} | P&L: ${pnl:.2f}"
            if not positions:
                listener.send_status_update(f"{header}\nNo open positions.")
            else:
                lines = [header, "**Open Positions:**"]
                for key, pos in positions.items():
                    lines.append(
                        f"  {pos.signal.ticker} {pos.signal.strike}{pos.signal.option_type} "
                        f"exp {pos.signal.expiry} | entry ${pos.entry_price:.2f} | "
                        f"peak {pos.highest_pnl:+.1f}%"
                    )
                listener.send_status_update("\n".join(lines))

        elif cmd == "scan":
            listener.send_status_update("Forcing immediate scan...")
            found = research_scanner.scan_markets(
                rsi_threshold=profile["rsi"], listener=listener,
                market=market, budget=float(profile["budget"]),
                tp_pct=profile["tp"], sl_pct=profile["sl"])
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
    webull._listener = listener   # now wire up expiry alerts to Discord

    engine = TradeEngine(
        webull=webull, market=market, paper=paper_mode,
        profit_target_pct=profile['tp'],
        stop_loss_pct=profile['sl'],
        listener=listener
    )

    # Sync paper/live mode and webull engine reference to API server dashboard
    try:
        from api_server import _bot_state
        _bot_state["paper_mode"]    = paper_mode
        _bot_state["webull_engine"] = webull
    except ImportError:
        pass

    console.print("\n1. Discord | 2. Research | 3. Hybrid")
    mode_choice = input("\nSelect Operation Mode (1/2/3): ").strip()

    MAX_DAILY_LOSS      = -150.0
    last_research_time  = 0
    trade_cooldowns     = {}   # shared across scanner + Discord
    last_heat_alert_ts  = 0
    consecutive_losses  = 0
    consecutive_wins    = 0
    last_daily_summary  = ""   # YYYY-MM-DD of last summary sent
    last_vix_alert_lvl  = 0    # last VIX level we alerted on
    webull_refresh_fails = 0   # consecutive Webull API failures

    # ── Position sync: rebuild engine state from Webull on startup ───────────
    # If bot crashed mid-session, this picks up any open positions
    try:
        open_pos = webull._wb.get_positions() if webull._wb else []
        if open_pos:
            console.print(f"[yellow]Found {len(open_pos)} existing positions from Webull — "
                          f"monitoring will resume.[/yellow]")
            listener.send_status_update(
                f"⚠️ Startup: Found {len(open_pos)} open positions from previous session. "
                f"Monitoring resumed."
            )
    except Exception:
        pass  # No existing positions or not connected yet

    try:
        while True:
            daily_pnl = engine.get_session_pnl()
            if daily_pnl <= MAX_DAILY_LOSS:
                msg = (f"🚨 CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} hit limit "
                       f"${MAX_DAILY_LOSS:.0f}. Closing all positions and stopping.")
                listener.send_status_update(msg)
                console.print(Panel(msg, border_style="bold red"))
                alert_circuit_breaker(daily_pnl, MAX_DAILY_LOSS)
                with engine._lock:
                    positions = dict(engine._positions)
                for key, pos in positions.items():
                    engine._auto_close(key, pos, reason="Circuit Breaker")
                break

            # ── Streak check: auto-pause after 2 consecutive losses ──────────
            if consecutive_losses >= 2 and not listener.paused:
                listener.paused = True
                listener.send_status_update(
                    f"⚠️ STREAK PAUSE: {consecutive_losses} losses in a row. "
                    f"New entries paused. Type !resume to continue."
                )
                alert_streak_pause(consecutive_losses)
                console.print(
                    f"[bold red]STREAK PAUSE: {consecutive_losses} consecutive losses — "
                    f"pausing new entries.[/bold red]"
                )

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
                        market=market, budget=float(profile['budget']),
                        tp_pct=profile['tp'], sl_pct=profile['sl'])
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
                        pnl_before = engine.get_session_pnl()
                        engine.on_raw_signal(verdict)
                        # Give position a moment to open then sample P&L change
                        time.sleep(2)
                        pnl_after = engine.get_session_pnl()
                        if pnl_after < pnl_before:
                            consecutive_losses += 1
                            consecutive_wins    = 0
                        elif pnl_after > pnl_before:
                            consecutive_wins   += 1
                            consecutive_losses  = 0
                        # Track Webull API health for auto-refresh
                        if webull.last_order_confirmed:
                            webull_refresh_fails = 0
                        else:
                            webull_refresh_fails += 1
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
                    consecutive_losses = 0   # manual resume resets streak
                # Sync streak, P&L, and live positions to dashboard
                _bot_state["consecutive_losses"] = consecutive_losses
                _bot_state["consecutive_wins"]   = consecutive_wins
                _bot_state["session_pnl"]        = engine.get_session_pnl()
                _bot_state["tp_pct"]             = profile["tp"]
                _bot_state["sl_pct"]             = profile["sl"]
                # Sync open positions so dashboard shows them
                with engine._lock:
                    eng_positions = dict(engine._positions)
                _bot_state["positions"] = [
                    {
                        "entry_price":  pos.entry_price,
                        "current_price": pos.entry_price,  # updated by monitor thread
                        "highest_pnl":  pos.highest_pnl,
                        "signal": {
                            "ticker":      pos.signal.ticker,
                            "strike":      pos.signal.strike,
                            "option_type": pos.signal.option_type,
                            "expiry":      pos.signal.expiry,
                            "quantity":    pos.signal.quantity,
                        }
                    }
                    for pos in eng_positions.values()
                ]
            except ImportError:
                pass

            time.sleep(1)
            if int(time.time()) % 30 == 0:
                engine.print_status()
                # Refresh account balance and positions cache for dashboard
                try:
                    webull._write_account_cache()
                except Exception:
                    pass

            # ── Webull session auto-refresh on repeated failures ─────────────
            if webull_refresh_fails >= 3:
                console.print("[yellow]Webull session appears stale — refreshing...[/yellow]")
                try:
                    webull.open_browser()
                    webull_refresh_fails = 0
                    listener.send_status_update("🔄 Webull session refreshed automatically.")
                except Exception as e:
                    log.error("Webull refresh failed: %s", e)

            # ── VIX spike alert (check every 5 min) ──────────────────────────
            if int(time.time()) % 300 == 0:
                try:
                    from research_scanner import _get_vix
                    vix = _get_vix()
                    if vix:
                        if vix >= 30 and last_vix_alert_lvl < 30:
                            alert_vix_spike(vix)
                            listener.send_status_update(
                                f"⚠️ VIX EXTREME: {vix:.1f} — sizing reduced to 1 contract max."
                            )
                            last_vix_alert_lvl = 30
                        elif vix >= 25 and last_vix_alert_lvl < 25:
                            alert_vix_spike(vix)
                            listener.send_status_update(
                                f"⚠️ VIX ELEVATED: {vix:.1f} — RSI thresholds tightened."
                            )
                            last_vix_alert_lvl = 25
                        elif vix < 20:
                            last_vix_alert_lvl = 0  # reset so next spike alerts again
                except Exception:
                    pass

            # ── Daily summary at market close (4:00 PM ET) ───────────────────
            try:
                import pytz
                from datetime import time as dtime
                ET  = pytz.timezone("America/New_York")
                now_et = datetime.now(ET)
                today_str = now_et.strftime("%Y-%m-%d")
                # Send once per day between 16:00-16:05 ET
                if (now_et.time() >= dtime(16, 0) and now_et.time() <= dtime(16, 5)
                        and last_daily_summary != today_str):
                    last_daily_summary = today_str
                    import pandas as pd
                    if os.path.exists("trade_log.csv"):
                        df = pd.read_csv("trade_log.csv")
                        todays = df[df["date"].str.startswith(today_str)]
                        if not todays.empty:
                            wins   = int((todays["profit_amt"] > 0).sum())
                            losses = int((todays["profit_amt"] <= 0).sum())
                            total  = len(todays)
                            pnl    = float(todays["profit_amt"].sum())
                            best   = todays.loc[todays["profit_amt"].idxmax()]
                            worst  = todays.loc[todays["profit_amt"].idxmin()]
                            best_str  = f"{best['ticker']} +${best['profit_amt']:.2f}"
                            worst_str = f"{worst['ticker']} -${abs(worst['profit_amt']):.2f}"
                            wr = round(wins / total * 100) if total else 0
                            summary = (
                                f"📊 **Daily Summary — {today_str}**\n"
                                f"Trades: {total} | Wins: {wins} | Losses: {losses} | "
                                f"Win Rate: {wr}%\n"
                                f"P&L: ${pnl:+.2f} | Best: {best_str} | Worst: {worst_str}"
                            )
                            listener.send_status_update(summary)
                            alert_daily_summary(total, wins, losses, pnl, best_str, worst_str)
            except Exception as e:
                log.debug("Daily summary error: %s", e)

    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown...[/yellow]")
        webull.close()
        market.stop()
        listener.stop()


if __name__ == "__main__":
    main()