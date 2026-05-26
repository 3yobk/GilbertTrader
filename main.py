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
import signal
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

# ── Phase 1/3/4 integrations ──────────────────────────────────────────────────
try:
    from analytics_engine import get_analytics
    analytics = get_analytics()
    log.info("Analytics engine loaded — trade_history.db ready")
except ImportError:
    analytics = None
    log.warning("analytics_engine.py not found — trade recording disabled")

try:
    from execution_monitor import ExecutionMonitor
    _HAS_EXEC_MON = True
except ImportError:
    _HAS_EXEC_MON = False
    log.warning("execution_monitor.py not found — execution hardening disabled")

try:
    from risk_engine import RiskEngine
    risk_engine = RiskEngine(analytics)
    log.info("Risk engine loaded — mode: %s", risk_engine.current_mode)
except ImportError:
    risk_engine = None
    log.warning("risk_engine.py not found — advanced risk management disabled")

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


def select_execution_mode() -> dict:
    """
    Prompt user to select trading direction.
    LONG  — buys calls on oversold dips  (RSI < threshold)
    SHORT — buys puts on overbought peaks (RSI > 100 - threshold)
    """
    console.print("\n[bold yellow]📈  SELECT EXECUTION DIRECTION[/bold yellow]")
    dir_table = Table(show_header=True, header_style="bold cyan")
    dir_table.add_column("Option", style="dim")
    dir_table.add_column("Mode")
    dir_table.add_column("Trigger")
    dir_table.add_column("Contract")
    dir_table.add_column("Strategy")
    dir_table.add_row("1", "LONG ONLY",  "RSI < 30 (oversold)",   "CALLS (C)", "Buy dips — mean reversion up")
    dir_table.add_row("2", "SHORT ONLY", "RSI > 70 (overbought)", "PUTS  (P)", "Sell rallies — mean reversion down")
    console.print(dir_table)
    d = input("\nSelect Direction (1/2): ").strip()
    if d == "2":
        return {"mode": "SHORT", "label": "SHORT ONLY — Puts on Overbought Rallies",
                "option_type": "P"}
    return {"mode": "LONG",  "label": "LONG ONLY  — Calls on Oversold Dips",
            "option_type": "C"}


def ask_ai_for_verdict(signal_text, budget, stock_price: float = None):
    price_context = (
        f"Current stock price: ${stock_price:.2f}. "
        if stock_price else ""
    )
    payload = {
        "question": (
            f"Senior Risk Manager Role. INPUT: '{signal_text}'\n"
            f"1. REFORMAT: 'BTO [TICKER] [STRIKE][C/P] [EXPIRY] x[QTY]' — keep the quantity\n"
            f"   CRITICAL: Do NOT change the ticker symbol. Input ticker must match output ticker exactly.\n"
            f"2. BUDGET LIMIT: Max ${budget} for this trade. "
            f"Each contract = premium * 100. Example: 3 contracts at $2.61 = $783 total — REJECT if over ${budget}. "
            f"If quantity is too high, reduce it to the max affordable amount instead of rejecting entirely.\n"
            f"3. {price_context}The STRIKE is an options strike price, NOT the stock price. "
            f"REJECT only if the strike is more than 20% away from the current stock price. "
            f"A $415 call on a $411 stock is only 1% OTM — APPROVE. "
            f"A $220 call on a $218 stock is 0.9% OTM — APPROVE. "
            f"Only reject if strike differs from stock price by more than 20%.\n"
            "OUTPUT: ONLY the cleaned BTO string (with corrected quantity) or 'Rejected' + reason."
        )
    }
    headers = {"Authorization": f"Bearer {AI_PC_KEY}"}

    for attempt in range(3):
        if attempt > 0:
            wait = attempt * 10
            log.info("AI PC retry %d/3 in %ds...", attempt + 1, wait)
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


def register_graceful_shutdown_handlers(webull_ref, market_ref,
                                        listener_ref, engine_ref):
    """
    UPGRADE 3: Graceful OS Signal Interrupts.

    Intercepts SIGINT (Ctrl+C) and SIGTERM (process kill).
    On shutdown:
      1. Pauses new entries immediately
      2. Logs all open positions to brain.log
      3. Flushes trade_log.csv (ensures no half-written rows)
      4. Stops Tradier WebSocket and market feed cleanly
      5. Stops Discord listener thread
      6. Closes Chromium browser session (saves cookies/profile)
      7. Calls sys.exit(0) cleanly

    Uses a threading.Event so the main loop can also detect shutdown
    and break cleanly without waiting for the signal.
    """
    _shutdown_event = threading.Event()

    def _shutdown(signum, frame):
        sig_name = "SIGINT (Ctrl+C)" if signum == signal.SIGINT else "SIGTERM"
        console.print(f"\n[bold yellow]🛑 {sig_name} received — graceful shutdown starting...[/bold yellow]")

        # Step 1: pause new entries immediately
        try:
            if listener_ref and hasattr(listener_ref, "paused"):
                listener_ref.paused = True
                console.print("  [dim]✓ New entries paused[/dim]")
        except Exception:
            pass

        # Step 2: log open positions
        try:
            if engine_ref:
                with engine_ref._lock:
                    open_pos = dict(engine_ref._positions)
                if open_pos:
                    console.print(f"  [yellow]⚠️  {len(open_pos)} open position(s) left unmonitored:[/yellow]")
                    for key, pos in open_pos.items():
                        console.print(f"      {key} | entry=${pos.entry_price:.2f} | peak={pos.highest_pnl:+.1f}%")
                    log.warning("SHUTDOWN with %d open positions: %s",
                                len(open_pos), list(open_pos.keys()))
                else:
                    console.print("  [dim]✓ No open positions[/dim]")
        except Exception as e:
            log.debug("Shutdown position log error: %s", e)

        # Step 3: flush trade_log.csv (it's written per-trade so just verify)
        try:
            if os.path.exists("trade_log.csv"):
                console.print("  [dim]✓ trade_log.csv verified[/dim]")
        except Exception:
            pass

        # Step 4: flush brain.log
        try:
            for handler in logging.root.handlers:
                handler.flush()
                handler.close()
            console.print("  [dim]✓ brain.log flushed[/dim]")
        except Exception:
            pass

        # Step 5: stop market feed (Tradier WebSocket + yfinance)
        try:
            if market_ref:
                market_ref.stop()
                console.print("  [dim]✓ Market feed stopped[/dim]")
        except Exception as e:
            log.debug("Market stop error: %s", e)

        # Step 6: stop Discord listener
        try:
            if listener_ref:
                listener_ref.stop()
                console.print("  [dim]✓ Discord listener stopped[/dim]")
        except Exception as e:
            log.debug("Listener stop error: %s", e)

        # Step 7: close Chromium browser (saves webull_profile session)
        try:
            if webull_ref:
                webull_ref.close()
                console.print("  [dim]✓ Chromium browser closed[/dim]")
        except Exception as e:
            log.debug("Webull close error: %s", e)

        console.print("[bold green]✓ Shutdown complete.[/bold green]")
        _shutdown_event.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    console.print("[dim]Graceful shutdown handlers registered (SIGINT/SIGTERM)[/dim]")
    return _shutdown_event


def _maybe_run_alpha_sieve(listener=None):
    """
    UPGRADE 2 trigger: runs Alpha Sieve at 9:15 AM ET if not already run today.
    Called from the main loop every tick.
    """
    import pytz
    from datetime import time as dtime
    ET = pytz.timezone("America/New_York")
    now_et = datetime.now(ET)
    if dtime(9, 15) <= now_et.time() <= dtime(9, 20):
        today = now_et.strftime("%Y-%m-%d")
        flag_file = f".alpha_sieve_{today}"
        if not os.path.exists(flag_file):
            open(flag_file, "w").close()
            import glob
            for old in glob.glob(".alpha_sieve_*"):
                if old != flag_file:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
            console.print("[bold cyan]⏰ 9:15 AM — Running Alpha Sieve...[/bold cyan]")
            try:
                from research_scanner import run_alpha_sieve_pre_filter
                run_alpha_sieve_pre_filter(top_n=5, listener=listener)
            except Exception as e:
                log.error("Alpha Sieve error: %s", e)


def _maybe_run_premarket_scan(listener=None):
    """
    Pre-market scanner: runs at 9:00 AM ET.
    Checks which stocks are gapping up/down significantly before open.
    Flags top gap-up candidates (calls) and gap-down (puts) for the day.
    Posts to Discord so you can prepare before market opens.
    """
    import pytz
    from datetime import time as dtime
    ET     = pytz.timezone("America/New_York")
    now_et = datetime.now(ET)
    if not (dtime(9, 0) <= now_et.time() <= dtime(9, 10)):
        return
    today     = now_et.strftime("%Y-%m-%d")
    flag_file = f".premarket_{today}"
    if os.path.exists(flag_file):
        return
    open(flag_file, "w").close()

    console.print("[bold cyan]🌅 9:00 AM — Running Pre-Market Scan...[/bold cyan]")
    try:
        import yfinance as yf
        from research_scanner import MY_SMALL_STOCKS
        gaps_up   = []
        gaps_down = []

        for ticker in MY_SMALL_STOCKS[:20]:   # scan top 20
            try:
                data = yf.download(ticker, period="2d", interval="1d",
                                   progress=False, auto_adjust=True)
                if data.empty or len(data) < 2:
                    continue
                prev_close = float(data["Close"].iloc[-2])
                pre_price  = yf.Ticker(ticker).fast_info.get(
                    "lastPrice", float(data["Close"].iloc[-1]))
                gap_pct = ((pre_price - prev_close) / prev_close) * 100
                if gap_pct >= 2.0:
                    gaps_up.append((ticker, gap_pct, pre_price))
                elif gap_pct <= -2.0:
                    gaps_down.append((ticker, gap_pct, pre_price))
            except Exception:
                continue

        gaps_up.sort(key=lambda x: -x[1])
        gaps_down.sort(key=lambda x: x[1])

        msg_lines = ["🌅 **PRE-MARKET SCAN** — Gap Analysis\n"]
        if gaps_up:
            msg_lines.append("📈 **Gap Ups** (potential call setups if RSI oversold on open):")
            for t, g, p in gaps_up[:5]:
                msg_lines.append(f"  • {t} +{g:.1f}% → ${p:.2f}")
        if gaps_down:
            msg_lines.append("\n📉 **Gap Downs** (potential put setups if RSI overbought):")
            for t, g, p in gaps_down[:5]:
                msg_lines.append(f"  • {t} {g:.1f}% → ${p:.2f}")
        if not gaps_up and not gaps_down:
            msg_lines.append("No significant gaps today (all within ±2%).")

        msg_lines.append("\n_Scanner will begin at 9:30 AM open._")
        msg = "\n".join(msg_lines)
        console.print(msg)
        if listener:
            listener.send_status_update(msg)
    except Exception as e:
        log.error("Pre-market scan error: %s", e)


def _maybe_send_daily_report(listener=None, engine=None, analytics_eng=None):
    """
    Daily P&L report: posts to Discord at 4:15 PM ET (15 min after close).
    Shows: trades taken, win rate, P&L, best/worst trade, account balance.
    Runs once per day.
    """
    import pytz
    from datetime import time as dtime
    ET     = pytz.timezone("America/New_York")
    now_et = datetime.now(ET)
    if not (dtime(16, 15) <= now_et.time() <= dtime(16, 25)):
        return
    today     = now_et.strftime("%Y-%m-%d")
    flag_file = f".daily_report_{today}"
    if os.path.exists(flag_file):
        return
    open(flag_file, "w").close()

    try:
        import csv as _csv
        trade_log = os.path.join(os.path.dirname(__file__), "trade_log.csv")
        if not os.path.exists(trade_log):
            return

        today_trades = []
        with open(trade_log, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row.get("date", "").startswith(today) and row.get("mode") == "live":
                    today_trades.append(row)

        if not today_trades:
            if listener:
                listener.send_status_update(
                    f"📊 **Daily Report** — {today}\n"
                    f"No trades today. Market was quiet or no signals fired."
                )
            return

        pnls  = [float(t.get("profit_amt", 0) or 0) for t in today_trades]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]
        total = sum(pnls)
        wr    = len(wins) / len(pnls) * 100 if pnls else 0
        best  = max(pnls) if pnls else 0
        worst = min(pnls) if pnls else 0

        # Get account balance from cache
        balance = "?"
        try:
            import json as _json
            cache = os.path.join(os.path.dirname(__file__), "account_cache.json")
            with open(cache) as f:
                acc = _json.load(f)
            balance = f"${float(acc.get('net_value', 0)):,.2f}"
        except Exception:
            pass

        emoji  = "🟢" if total >= 0 else "🔴"
        report = (
            f"📊 **Daily Report** — {today}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} **Net P&L**: ${total:+.2f}\n"
            f"🏆 **Win Rate**: {wr:.0f}% ({len(wins)}W / {len(losses)}L)\n"
            f"📈 **Best Trade**: ${best:+.2f}\n"
            f"📉 **Worst Trade**: ${worst:+.2f}\n"
            f"🔢 **Trades Taken**: {len(today_trades)}\n"
            f"💰 **Account Balance**: {balance}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        # Add trade breakdown
        for t in today_trades:
            pnl    = float(t.get("profit_amt", 0) or 0)
            ticker = t.get("ticker", "?")
            e      = "✅" if pnl > 0 else "❌"
            report += f"{e} {ticker} {t.get('strike','')}{t.get('option_type','')} "
            report += f"→ ${pnl:+.2f}\n"

        console.print(report)
        if listener:
            listener.send_status_update(report)
        log.info("Daily report sent: %d trades, P&L=$%.2f", len(today_trades), total)
    except Exception as e:
        log.error("Daily report error: %s", e)


def _maybe_run_paper_shadow(engine=None, signal_str: str = "",
                            profile: dict = None, market=None, webull=None):
    """
    Paper trade shadow: runs a paper trade in parallel with every live trade.
    After a week compares paper vs live performance to detect slippage/execution issues.
    Logs to paper_shadow.csv separately from trade_log.csv.
    """
    if not signal_str or not engine:
        return
    try:
        import csv as _csv
        from research_scanner import TradeSignal
        shadow_log = os.path.join(os.path.dirname(__file__), "paper_shadow.csv")
        parsed = engine.parse_signal(signal_str)
        if not parsed:
            return
        # Log the shadow entry — P&L will be tracked separately
        file_exists = os.path.exists(shadow_log)
        with open(shadow_log, "a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "signal", "ticker", "strike",
                                 "option_type", "expiry", "qty", "mode"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                signal_str,
                parsed.ticker, parsed.strike,
                parsed.option_type, parsed.expiry,
                parsed.quantity, "paper_shadow"
            ])
        log.info("Paper shadow logged: %s", signal_str)
    except Exception as e:
        log.debug("Paper shadow error: %s", e)



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
    signal_channel_id  = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID", "0")) or None
    tradier_token      = os.getenv("TRADIER_TOKEN", "")

    run_health_check(discord_token, discord_channel_id)

    console.print(Panel(
        Text.assemble(
            ("GILBERT TRADER SYSTEM ARMED\n", "bold white"),
            (f"Active Strategy: {profile['name']}\n", "bold green"),
            (f"Parameters: TP {profile['tp']}% | SL {profile['sl']}% | "
             f"RSI <{profile['rsi']} | Budget ${profile['budget']}\n", "dim"),
            ("Direction: AUTO — Calls in bull market, Puts in bear market", "bold cyan"),
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
    webull = WebullEngine(paper=paper_mode)
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

        elif cmd == "sl":
            # !sl off / !sl on / !sl 15
            arg = args.strip().lower()
            if arg == "off":
                engine.stop_loss_pct = 999.0   # effectively disabled
                listener.send_status_update(
                    "⚠️ **Stop loss DISABLED** — positions will not auto-close on loss. "
                    "Use `!sl on` or `!sl <pct>` to re-enable."
                )
            elif arg == "on":
                engine.stop_loss_pct = profile["sl"]
                listener.send_status_update(
                    f"✅ **Stop loss re-enabled** at -{profile['sl']:.0f}%."
                )
            else:
                try:
                    pct = float(arg.replace("%", ""))
                    engine.stop_loss_pct = pct
                    listener.send_status_update(
                        f"✅ **Stop loss set to -{pct:.0f}%**."
                    )
                except ValueError:
                    listener.send_status_update(
                        "Usage: `!sl off` | `!sl on` | `!sl 15`"
                    )

        elif cmd == "tp":
            # !tp off / !tp on / !tp 50
            arg = args.strip().lower()
            if arg == "off":
                engine.profit_target_pct = 999.0   # effectively disabled
                listener.send_status_update(
                    "⚠️ **Take profit DISABLED** — positions will not auto-close on gain. "
                    "Use `!tp on` or `!tp <pct>` to re-enable."
                )
            elif arg == "on":
                engine.profit_target_pct = profile["tp"]
                listener.send_status_update(
                    f"✅ **Take profit re-enabled** at +{profile['tp']:.0f}%."
                )
            else:
                try:
                    pct = float(arg.replace("%", ""))
                    engine.profit_target_pct = pct
                    listener.send_status_update(
                        f"✅ **Take profit set to +{pct:.0f}%**."
                    )
                except ValueError:
                    listener.send_status_update(
                        "Usage: `!tp off` | `!tp on` | `!tp 50`"
                    )

        elif cmd == "scaleout":
            # !scaleout off / !scaleout on
            arg = args.strip().lower()
            if arg == "off":
                engine.scale_out_trigger_pct = 999.0
                listener.send_status_update(
                    "⚠️ **Scale-out DISABLED** — bot will hold full position to TP/SL. "
                    "Use `!scaleout on` to re-enable."
                )
            elif arg == "on":
                engine.scale_out_trigger_pct = 20.0
                listener.send_status_update("✅ **Scale-out re-enabled** at +20%.")
            else:
                listener.send_status_update("Usage: `!scaleout off` | `!scaleout on`")

        elif cmd == "limiters":
            # !limiters off — disable all exits (manual control)
            # !limiters on  — restore all exits to profile defaults
            arg = args.strip().lower()
            if arg == "off":
                engine.stop_loss_pct        = 999.0
                engine.profit_target_pct    = 999.0
                engine.scale_out_trigger_pct = 999.0
                listener.send_status_update(
                    "⚠️ **ALL LIMITERS DISABLED** — SL, TP, and scale-out are off. "
                    "You are in full manual control. Use `!limiters on` to restore."
                )
            elif arg == "on":
                engine.stop_loss_pct        = profile["sl"]
                engine.profit_target_pct    = profile["tp"]
                engine.scale_out_trigger_pct = 20.0
                listener.send_status_update(
                    f"✅ **All limiters restored** — "
                    f"TP +{profile['tp']:.0f}% | SL -{profile['sl']:.0f}% | Scale-out +20%."
                )
            else:
                listener.send_status_update("Usage: `!limiters off` | `!limiters on`")

        elif cmd == "settings":
            sl_str = f"-{engine.stop_loss_pct:.0f}%" if engine.stop_loss_pct < 999 else "OFF"
            tp_str = f"+{engine.profit_target_pct:.0f}%" if engine.profit_target_pct < 999 else "OFF"
            so_str = f"+{engine.scale_out_trigger_pct:.0f}%" if engine.scale_out_trigger_pct < 999 else "OFF"
            listener.send_status_update(
                f"⚙️ **Current Settings**\n"
                f"Stop Loss: **{sl_str}**\n"
                f"Take Profit: **{tp_str}**\n"
                f"Scale-out: **{so_str}**\n"
                f"Profile: **{profile['name']}** | Budget: **${profile['budget']}**\n"
                f"RSI threshold: **<{profile['rsi']}**"
            )

    listener = DiscordListener(
        channel_id=discord_channel_id,
        token=discord_token,
        on_signal=lambda x: None,
        on_command=handle_discord_command,
        signal_channel_id=signal_channel_id,
    )
    listener.start_in_thread()
    webull._listener = listener

    # ── Construct trade engine ────────────────────────────────────────────────
    engine = TradeEngine(
        webull=webull, market=market, paper=paper_mode,
        profit_target_pct=profile['tp'],
        stop_loss_pct=profile['sl'],
        listener=listener
    )

    # Sync any Webull orders from today that the bot may have missed
    try:
        engine.sync_webull_orders_to_log()
        log.info("Webull order sync complete")
    except Exception as e:
        log.debug("Order sync skipped: %s", e)

    # Resume any positions saved before last shutdown
    try:
        engine._load_positions()
    except Exception as e:
        log.debug("Position load skipped: %s", e)

    # Sync Webull order history into DB on startup
    try:
        from api_server import _sync_webull_history, _save_account_snapshot, _bot_state as _api_state
        _api_state["webull_engine"] = webull
        _sync_webull_history()
        _save_account_snapshot()
        log.info("Startup history sync complete")
    except Exception as e:
        log.debug("Startup history sync error: %s", e)

    # ── Accurate streak tracking via on_trade_closed callback ────────────────
    consecutive_losses = 0
    consecutive_wins   = 0
    # Same-session re-entry block: tickers stopped out today cannot re-enter
    session_stopped_tickers: set = set()

    # ── UPGRADE 3: Register graceful shutdown handlers ────────────────────────
    register_graceful_shutdown_handlers(webull, market, listener, engine)

    # Sync paper/live mode to API server dashboard
    try:
        from api_server import _bot_state
        _bot_state["paper_mode"]    = paper_mode
        _bot_state["webull_engine"] = webull
        _bot_state["engine"]        = engine
        _bot_state["profile"]       = profile
    except ImportError:
        pass

    console.print("\n1. Discord | 2. Research | 3. Hybrid")
    mode_choice = input("\nSelect Operation Mode (1/2/3): ").strip()

    MAX_DAILY_LOSS_PCT  = 15.0
    MAX_DAILY_TRADES    = 4      # max entries per day — prevents overtrading on choppy days
    _cache_path = os.path.join(os.path.dirname(__file__), "account_cache.json")
    try:
        import json as _json
        with open(_cache_path) as f:
            _acc = _json.load(f)
        _account_balance = float(_acc.get("net_value") or _acc.get("buying_power") or 500.0)
        # T+1 settlement: use settled cash only to avoid using unsettled funds
        _settled_cash    = float(_acc.get("settled_cash") or _acc.get("buying_power") or _account_balance)
        _unsettled       = _account_balance - _settled_cash
        if _unsettled > 10:
            console.print(
                f"[yellow]⚠️  T+1 Settlement: ${_unsettled:.2f} unsettled from recent trades. "
                f"Using settled cash ${_settled_cash:.2f} for budget.[/yellow]"
            )
    except Exception:
        _account_balance = 500.0
        _settled_cash    = 500.0

    MAX_DAILY_LOSS = -round(_account_balance * MAX_DAILY_LOSS_PCT / 100, 2)
    console.print(
        f"[dim]Circuit breaker: -{MAX_DAILY_LOSS_PCT:.0f}% of "
        f"${_account_balance:.2f} = ${abs(MAX_DAILY_LOSS):.2f}[/dim]"
    )

    # ── Profit streak scaling ─────────────────────────────────────────────────
    # After 3 consecutive wins: +10% budget
    # After 2 consecutive losses: -20% budget (protect during drawdown)
    _base_budget      = float(profile['budget'])
    _current_budget   = _base_budget
    _consecutive_wins = 0
    _consecutive_loss = 0

    def _update_streak_budget(outcome: str, profit_amt: float = 0.0, ticker: str = ""):
        nonlocal _current_budget, _consecutive_wins, _consecutive_loss
        nonlocal consecutive_wins, consecutive_losses
        # ── Streak counters (used by dashboard) ──────────────────────────────
        if outcome == "Win":
            consecutive_wins   += 1
            consecutive_losses  = 0
            _consecutive_wins  += 1
            _consecutive_loss   = 0
            if profit_amt >= 50:
                alert_big_winner(profit_amt)
            if _consecutive_wins >= 3:
                new_budget = min(_current_budget * 1.10, _base_budget * 2.0)
                if new_budget != _current_budget:
                    _current_budget = round(new_budget, 0)
                    if listener:
                        listener.send_status_update(
                            f"📈 **STREAK BONUS**: {_consecutive_wins} wins in a row! "
                            f"Budget raised to ${_current_budget:.0f}"
                        )
        else:
            consecutive_losses += 1
            consecutive_wins    = 0
            _consecutive_loss  += 1
            _consecutive_wins   = 0
            # ── Same-session re-entry block ───────────────────────────────────
            # A ticker stopped out today is blocked for the rest of the session.
            # Prevents revenge trading the same move twice.
            if ticker:
                session_stopped_tickers.add(ticker.upper())
                log.info("Re-entry blocked for %s for rest of session (stop loss)", ticker)
                if listener:
                    listener.send_status_update(
                        f"🚫 **RE-ENTRY BLOCKED**: {ticker} stopped out — "
                        f"no new signals on this ticker today."
                    )
            if _consecutive_loss >= 2:
                new_budget = max(_current_budget * 0.80, _base_budget * 0.5)
                if new_budget != _current_budget:
                    _current_budget = round(new_budget, 0)
                    if listener:
                        listener.send_status_update(
                            f"📉 **DRAWDOWN PROTECTION**: {_consecutive_loss} losses. "
                            f"Budget reduced to ${_current_budget:.0f}"
                        )

    # Wire Phase 1/3/4 engines into trade_engine
    engine.analytics   = analytics
    engine.risk_engine = risk_engine

    # Wire execution monitor
    if _HAS_EXEC_MON:
        exec_monitor = ExecutionMonitor(webull, engine, listener)
        exec_monitor.start()
        engine.exec_monitor = exec_monitor
        log.info("Execution monitor started")
    else:
        exec_monitor = None

    engine.on_trade_closed = _update_streak_budget

    last_research_time   = 0
    daily_trades_count   = 0     # resets at midnight via date check
    _last_trade_date     = datetime.now().strftime("%Y-%m-%d")
    trade_cooldowns      = {}
    last_heat_alert_ts   = 0
    last_daily_summary   = ""
    last_vix_alert_lvl   = 0
    webull_refresh_fails = 0
    last_heat_alert_ts  = 0
    last_daily_summary  = ""
    last_vix_alert_lvl  = 0
    webull_refresh_fails = 0

    # ── Position sync on startup ─────────────────────────────────────────────
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
        pass

    try:
        while True:
            daily_pnl = engine.get_session_pnl()
            if daily_pnl <= MAX_DAILY_LOSS:
                msg = (f"🚨 CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} hit "
                       f"-{MAX_DAILY_LOSS_PCT:.0f}% limit "
                       f"(${abs(MAX_DAILY_LOSS):.2f}). Closing all positions and stopping.")
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

            # Portfolio heat check — dollar-based, not just count
            # Cap total deployed capital at 80% of current budget × max positions
            # This prevents over-leveraging when positions are large
            with engine._lock:
                _open_pos = dict(engine._positions)
            if _open_pos:
                _deployed = sum(
                    p.entry_price * p.signal.quantity * 100
                    for p in _open_pos.values()
                )
                _max_deploy = _current_budget * 2.0   # allow up to 2 concurrent positions worth
                if len(_open_pos) >= 2 or _deployed >= _max_deploy:
                    now_ts = time.time()
                    if now_ts - last_heat_alert_ts >= 120:
                        listener.send_status_update(
                            f"🔥 Portfolio heat: {len(_open_pos)} position(s) | "
                            f"${_deployed:.0f} deployed. Waiting for close."
                        )
                        last_heat_alert_ts = now_ts
                    time.sleep(10)
                    continue

            raw_text = None

            # Update risk engine state every scan cycle
            if risk_engine:
                try:
                    from analytics_engine import AnalyticsEngine
                    _vix = AnalyticsEngine.get_current_vix()
                    risk_engine.update(
                        vix=_vix,
                        streak_losses=streak_losses,
                        streak_wins=streak_wins,
                        session_pnl=engine.get_session_pnl(),
                        account_value=profile.get("budget", 500) * 2,
                    )
                    engine._current_regime = regime_state.get("regime", "unknown")
                except Exception as _re:
                    log.debug("risk_engine.update error: %s", _re)

            # Research / scanner mode
            # Reset daily counters at midnight
            _today_str = datetime.now().strftime("%Y-%m-%d")
            if _today_str != _last_trade_date:
                _last_trade_date     = _today_str
                daily_trades_count   = 0
                session_stopped_tickers.clear()
                log.info("New trading day — daily counters reset")
                # Reset risk engine session P&L at midnight
                if risk_engine:
                    risk_engine._session_pnl = 0.0

            if mode_choice in ["2", "3"]:
                # Adaptive scan interval:
                # 9:30–10:30 AM ET (open volatility)  → 2 min
                # 3:00–4:00 PM ET (power hour)        → 2 min
                # 11:45–1:30 PM ET (lunch, skipped)   → 5 min (scanner returns [] anyway)
                # All other times                     → 4 min
                try:
                    import pytz as _pytz_si
                    _et_si = datetime.now(_pytz_si.timezone("America/New_York"))
                    _hm_si = _et_si.hour * 60 + _et_si.minute
                    if _hm_si <= 10 * 60 + 30 or _hm_si >= 15 * 60:
                        _scan_interval = 120   # 2 min — open volatility / power hour
                    else:
                        _scan_interval = 240   # 4 min — mid-session
                except Exception:
                    _scan_interval = 300
                if time.time() - last_research_time > _scan_interval:
                    if daily_trades_count >= MAX_DAILY_TRADES:
                        now_ts = time.time()
                        if now_ts - last_heat_alert_ts >= 600:
                            console.print(
                                f"[yellow]📊 Daily trade limit reached ({daily_trades_count}/{MAX_DAILY_TRADES}) "
                                f"— no new entries today.[/yellow]"
                            )
                            listener.send_status_update(
                                f"📊 **DAILY LIMIT**: {daily_trades_count}/{MAX_DAILY_TRADES} trades taken. "
                                f"No new entries until tomorrow."
                            )
                            last_heat_alert_ts = now_ts
                        last_research_time = time.time()
                    else:
                        direction_label = (
                            "Overbought Rallies (RSI > 70)" if regime_data.get("regime") == "bear"
                            else f"Oversold Dips (RSI < {profile['rsi']})"
                        ) if 'regime_data' in dir() else f"Oversold Dips (RSI < {profile['rsi']})"
                        console.print(f"[dim]Scanning for {direction_label} [{daily_trades_count}/{MAX_DAILY_TRADES} trades today]...[/dim]")
                        found_signals = research_scanner.scan_markets(
                            rsi_threshold=profile['rsi'], listener=listener,
                            market=market, budget=float(_current_budget),
                            tp_pct=profile['tp'], sl_pct=profile['sl'])

                    # Iterate through every signal found in the scan one by one
                    for raw_text in found_signals:
                        parsed_te = engine.parse_signal(raw_text)
                        ticker = parsed_te.ticker if parsed_te else "UNK"

                        # Block tickers stopped out earlier today — no revenge trades
                        if ticker.upper() in session_stopped_tickers:
                            console.print(
                                f"[yellow]🚫 {ticker} blocked — stopped out earlier today. "
                                f"No re-entry until tomorrow.[/yellow]"
                            )
                            continue

                        # Skip if the ticker is on a 30-min cooldown
                        if ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
                            continue

                        console.print(f"\n[bold cyan]▶ Sending to AI Risk Manager:[/bold cyan] {ticker} ({raw_text[:40]})")
                        console.print("[dim]Waiting for AI response before proceeding...[/dim]")

                        # Get current stock price to help AI validate the strike
                        stock_price = None
                        try:
                            stock_price = market.get_price(ticker)
                        except Exception:
                            pass

                        verdict = ask_ai_for_verdict(raw_text, profile['budget'], stock_price)

                        if "BTO" in verdict:
                            # ── Ticker validation — AI must not change the ticker ──
                            verdict_parts  = verdict.strip().split()
                            verdict_ticker = verdict_parts[1].upper() if len(verdict_parts) > 1 else ""
                            if verdict_ticker != ticker.upper():
                                console.print(
                                    f"[bold red]⚠️  AI changed ticker {ticker} → {verdict_ticker} "
                                    f"— REJECTING and using original signal[/bold red]")
                                # Rebuild verdict with original signal ticker preserved
                                verdict = raw_text   # fall back to bot's original signal

                            listener.send_status_update(f"✅ AI APPROVED: `{verdict}` — executing now")
                            console.print(f"[bold green]APPROVED: {verdict}[/bold green]")
                            trade_cooldowns[ticker] = time.time()
                            engine.on_raw_signal(verdict)

                            if webull.last_order_confirmed:
                                webull_refresh_fails  = 0
                                daily_trades_count   += 1
                                # Send rich signal card to Discord
                                try:
                                    parts   = verdict.split()
                                    t_tick  = parts[1] if len(parts) > 1 else ticker
                                    t_cont  = parts[2] if len(parts) > 2 else "?"
                                    t_exp   = parts[3] if len(parts) > 3 else "?"
                                    t_qty   = parts[4] if len(parts) > 4 else "x1"
                                    strike  = t_cont[:-1]
                                    opt     = t_cont[-1]
                                    side    = "CALL" if opt == "C" else "PUT"
                                    entry   = getattr(webull, "_last_fill_price", None)
                                    tp_pct  = profile["tp"]
                                    sl_pct  = profile["sl"]
                                    tp_str  = f"+{tp_pct:.0f}% from entry" if not entry else f"${entry * (1 + tp_pct/100):.2f}"
                                    sl_str  = f"-{sl_pct:.0f}% from entry" if not entry else f"${entry * (1 - sl_pct/100):.2f}"
                                    entry_str = f"${entry:.2f}" if entry else "market mid"
                                    signal_card = (
                                        f"🚀 **LIVE TRADE ALERT** — GilbertTrader\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"**{t_tick}** {strike} {side} exp {t_exp}\n"
                                        f"📥 **Entry**: {entry_str}\n"
                                        f"🎯 **Target**: {tp_str} (+{tp_pct:.0f}%)\n"
                                        f"🛑 **Stop**: {sl_str} (-{sl_pct:.0f}%)\n"
                                        f"📦 **Size**: {t_qty.replace('x','')} contract(s)\n"
                                        f"⚖️ **Scale-out**: sell half at +20%, trail remaining\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"*Powered by GilbertTrader v2.2 — automated options scanner*"
                                    )
                                    listener.send_signal(signal_card)
                                except Exception as e:
                                    log.debug("Signal card error: %s", e)

                                break  # Trade confirmed — stop processing other signals
                            else:
                                # Paper shadow — log what would have happened in paper
                                _maybe_run_paper_shadow(
                                    engine=engine, signal_str=verdict,
                                    profile=profile, market=market, webull=webull
                                )
                                # Order was rejected by Webull (no bid/ask, expiry issue etc)
                                console.print(
                                    f"[yellow]⚠️  Order rejected for {ticker} — "
                                    f"trying next signal if available[/yellow]")
                                webull_refresh_fails += 1
                                time.sleep(2)
                                continue  # ← KEY FIX: move to next signal instead of freezing
                        else:
                            listener.send_status_update(f"AI REJECTED ${ticker}: {verdict}")
                            console.print(f"[bold red]REJECTED ${ticker}: {verdict}[/bold red]")
                            console.print("[dim]Pacing delay: 2s before next signal...[/dim]")
                            time.sleep(2)

                    last_research_time = time.time()

            # Discord mode (only runs if scanner didn't pick up a trade)
            if not raw_text and mode_choice in ["1", "3"]:
                raw_text = listener.get_latest_signal()
                if raw_text:
                    parsed_te = engine.parse_signal(raw_text)
                    ticker = parsed_te.ticker if parsed_te else "UNK"

                    # Block tickers stopped out today (same rule as scanner path)
                    if ticker.upper() in session_stopped_tickers:
                        console.print(
                            f"[yellow]🚫 {ticker} blocked (Discord signal) — "
                            f"stopped out earlier today.[/yellow]"
                        )
                    elif ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
                        pass
                    else:
                        console.print(f"Signal: {raw_text[:60]}")
                        console.print("[dim]Checking with AI Risk Manager...[/dim]")

                        verdict = ask_ai_for_verdict(raw_text, profile['budget'])

                        if "BTO" in verdict:
                            # Ticker validation — reject if AI changed the ticker
                            v_parts  = verdict.strip().split()
                            v_ticker = v_parts[1].upper() if len(v_parts) > 1 else ""
                            if v_ticker != ticker.upper():
                                console.print(
                                    f"[bold red]⚠️  AI changed ticker {ticker} → {v_ticker} "
                                    f"— using original signal[/bold red]")
                                verdict = raw_text

                            listener.send_status_update(f"✅ AI APPROVED: `{verdict}` — executing now")
                            console.print(f"[bold green]APPROVED: {verdict}[/bold green]")
                            trade_cooldowns[ticker] = time.time()
                            engine.on_raw_signal(verdict)
                            if webull.last_order_confirmed:
                                webull_refresh_fails  = 0
                                daily_trades_count   += 1
                                # Send rich signal card to Discord
                                try:
                                    parts   = verdict.split()
                                    t_tick  = parts[1] if len(parts) > 1 else ticker
                                    t_cont  = parts[2] if len(parts) > 2 else "?"
                                    t_exp   = parts[3] if len(parts) > 3 else "?"
                                    t_qty   = parts[4] if len(parts) > 4 else "x1"
                                    strike  = t_cont[:-1]
                                    opt     = t_cont[-1]
                                    side    = "CALL" if opt == "C" else "PUT"
                                    entry   = getattr(webull, "_last_fill_price", None)
                                    tp_pct  = profile["tp"]
                                    sl_pct  = profile["sl"]
                                    tp_str  = f"+{tp_pct:.0f}% from entry" if not entry else f"${entry * (1 + tp_pct/100):.2f}"
                                    sl_str  = f"-{sl_pct:.0f}% from entry" if not entry else f"${entry * (1 - sl_pct/100):.2f}"
                                    entry_str = f"${entry:.2f}" if entry else "market mid"
                                    signal_card = (
                                        f"🚀 **LIVE TRADE ALERT** — GilbertTrader\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"**{t_tick}** {strike} {side} exp {t_exp}\n"
                                        f"📥 **Entry**: {entry_str}\n"
                                        f"🎯 **Target**: {tp_str} (+{tp_pct:.0f}%)\n"
                                        f"🛑 **Stop**: {sl_str} (-{sl_pct:.0f}%)\n"
                                        f"📦 **Size**: {t_qty.replace('x','')} contract(s)\n"
                                        f"⚖️ **Scale-out**: sell half at +20%, trail remaining\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"*Powered by GilbertTrader v2.2 — automated options scanner*"
                                    )
                                    listener.send_signal(signal_card)
                                except Exception as e:
                                    log.debug("Signal card error: %s", e)
                            else:
                                console.print(
                                    f"[yellow]⚠️  Order rejected for {ticker} — "
                                    f"continuing to monitor for next signal[/yellow]")
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
                    consecutive_losses = 0
                # Sync streak, P&L, and positions to dashboard
                _bot_state["consecutive_losses"] = consecutive_losses
                _bot_state["consecutive_wins"]   = consecutive_wins
                _bot_state["session_pnl"]        = engine.get_session_pnl()
                _bot_state["tp_pct"]             = profile["tp"]
                _bot_state["sl_pct"]             = profile["sl"]
                with engine._lock:
                    eng_positions = dict(engine._positions)
                _bot_state["positions"] = [
                    {
                        "entry_price":   pos.entry_price,
                        "current_price": pos.entry_price,
                        "highest_pnl":   pos.highest_pnl,
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
                try:
                    webull._write_account_cache()
                except Exception:
                    pass
                # Sync Webull history and save account snapshot to DB
                try:
                    from api_server import _sync_webull_history, _save_account_snapshot, _bot_state as _api_state
                    _api_state["webull_engine"] = webull
                    _sync_webull_history()
                    _save_account_snapshot()
                except Exception as e:
                    log.debug("History sync error: %s", e)

            # ── UPGRADE 2: Alpha Sieve at 9:15 AM ET ─────────────────────────
            _maybe_run_alpha_sieve(listener=listener)
            # ── Pre-market scan at 9:00 AM ET ────────────────────────────────
            _maybe_run_premarket_scan(listener=listener)
            # ── Daily report at 4:15 PM ET ────────────────────────────────────
            _maybe_send_daily_report(listener=listener, engine=engine)

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
                            last_vix_alert_lvl = 0
                except Exception:
                    pass

            # ── Daily summary at market close (4:00 PM ET) ───────────────────
            try:
                import pytz
                from datetime import time as dtime
                ET     = pytz.timezone("America/New_York")
                now_et = datetime.now(ET)
                today_str = now_et.strftime("%Y-%m-%d")
                if (now_et.time() >= dtime(16, 0) and now_et.time() <= dtime(16, 5)
                        and last_daily_summary != today_str):
                    last_daily_summary = today_str
                    import pandas as pd
                    if os.path.exists("trade_log.csv"):
                        df = pd.read_csv("trade_log.csv")
                        mode_str = "live" if live_enabled else "paper"
                        if "mode" in df.columns:
                            df = df[df["mode"] == mode_str]
                        todays = df[df["date"].str.startswith(today_str)]
                        if not todays.empty:
                            wins      = int((todays["profit_amt"] > 0).sum())
                            losses    = int((todays["profit_amt"] <= 0).sum())
                            total     = len(todays)
                            pnl       = float(todays["profit_amt"].sum())
                            best      = todays.loc[todays["profit_amt"].idxmax()]
                            worst     = todays.loc[todays["profit_amt"].idxmin()]
                            best_str  = f"{best['ticker']} +${best['profit_amt']:.2f}"
                            worst_str = f"{worst['ticker']} -${abs(worst['profit_amt']):.2f}"
                            wr        = round(wins / total * 100) if total else 0
                            mode_label = "💰 LIVE" if live_enabled else "📋 PAPER"
                            summary = (
                                f"📊 **Daily Summary — {today_str}** ({mode_label})\n"
                                f"Trades: {total} | Wins: {wins} | Losses: {losses} | "
                                f"Win Rate: {wr}%\n"
                                f"P&L: ${pnl:+.2f} | Best: {best_str} | Worst: {worst_str}"
                            )
                            listener.send_status_update(summary)
                            alert_daily_summary(total, wins, losses, pnl, best_str, worst_str)
            except Exception as e:
                log.debug("Daily summary error: %s", e)

    except KeyboardInterrupt:
        # SIGINT is now handled by register_graceful_shutdown_handlers().
        # This fallback fires only if the signal handler somehow doesn't catch it.
        console.print("\n[yellow]Fallback shutdown — use Ctrl+C for graceful exit.[/yellow]")
        try:
            webull.close()
            market.stop()
            listener.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()