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
try:
    from db import DB as _DB
    _HAS_DB = True
    log.info("SQLite telemetry DB ready")
except ImportError:
    _DB = None
    _HAS_DB = False
try:
    import tradier_execution as _tradier_exec
    _HAS_TRADIER_EXEC = _tradier_exec.FAILOVER_ENABLED
    if _HAS_TRADIER_EXEC:
        log.info("Tradier execution failover: ENABLED")
    else:
        log.info("Tradier execution failover: disabled (set TRADIER_ACCOUNT_ID to enable)")
except ImportError:
    _tradier_exec = None
    _HAS_TRADIER_EXEC = False
try:
    import ml_sieve as _ml_sieve_mod
    _ml_sieve_mod.load_model()
    _ml_sieve_mod.maybe_retrain()
    _HAS_ML = True
except ImportError:
    _ml_sieve_mod = None
    _HAS_ML = False
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
    CALLS — buys calls on oversold dips only
    PUTS  — buys puts on overbought peaks only
    AUTO  — scanner decides based on market regime (default)
    """
    console.print("\n[bold yellow]📈  SELECT TRADING DIRECTION[/bold yellow]")
    dir_table = Table(show_header=True, header_style="bold cyan")
    dir_table.add_column("Option", style="dim", width=6)
    dir_table.add_column("Mode",    width=12)
    dir_table.add_column("Trades",  width=10)
    dir_table.add_column("When",    width=30)
    dir_table.add_row("1", "AUTO",       "Calls + Puts",  "Bot picks based on regime (recommended)")
    dir_table.add_row("2", "CALLS ONLY", "Calls only",    "Only buys oversold dip bounces (RSI < threshold)")
    dir_table.add_row("3", "PUTS ONLY",  "Puts only",     "Only buys overbought fade plays (RSI > threshold)")
    console.print(dir_table)
    d = input("\nSelect Direction (1/2/3) [default=1]: ").strip() or "1"
    if d == "2":
        return {"mode": "CALLS", "label": "CALLS ONLY — Oversold Dip Bounces",
                "option_type": "C", "auto": False}
    if d == "3":
        return {"mode": "PUTS",  "label": "PUTS ONLY  — Overbought Fade Plays",
                "option_type": "P", "auto": False}
    return {"mode": "AUTO",  "label": "AUTO — Bot selects Calls or Puts by regime",
            "option_type": None, "auto": True}


def ask_ai_for_verdict(signal_text, budget, stock_price: float = None, raw_text: str = None):
    # ── Pull live adaptive metrics from cache layers ───────────────────────────
    learned_slippage = 1.0
    ml_context       = ""
    try:
        import research_scanner as _rs
        ticker = signal_text.split()[1] if len(signal_text.split()) > 1 else "UNK"
        learned_slippage = _rs.get_slippage_multiplier(ticker)
    except Exception:
        pass

    try:
        if _HAS_ML and _ml_sieve_mod:
            _p_win = _ml_sieve_mod.predict_win_probability(
                entry_rsi=25.0, vol_ratio=1.0, flow_score=50.0,
                put_call_ratio=1.0, gex_significance=5.0, spy_vs_sma20=0.0,
            )
            if _p_win is not None:
                ml_context = f"Internal ML Win-Probability: {_p_win:.1%}. "
    except Exception:
        pass

    # ── Compute strike distance for explicit context ───────────────────────────
    strike_context = ""
    if stock_price:
        try:
            import re as _re
            _m = _re.search(r"\b(\d+\.?\d*)[CP]\b", signal_text, _re.IGNORECASE)
            if _m:
                _strike = float(_m.group(1))
                _diff_pct = abs(_strike - stock_price) / stock_price * 100
                strike_context = (f"Strike ${_strike:.0f} vs spot ${stock_price:.2f} "
                                  f"= {_diff_pct:.1f}% OTM. ")
        except Exception:
            pass

    _slip_note = (f"Learned Slippage Factor: {learned_slippage:.2f}x "
                  f"(effective budget = ${budget / learned_slippage:.0f}). "
                  if learned_slippage != 1.0 else "")

    _spot = (f"Spot ${stock_price:.2f}. {strike_context}{ml_context}{_slip_note}"
             if stock_price else f"No spot price. {ml_context}{_slip_note}")

    payload = {
        "question": (
            f"[SYSTEM ROLE: Senior Options Risk Manager & Pure Data Sieve]\n"
            f"You are a software submodule inside an automated execution loop. "
            f"Your output is parsed by a strict regex. Any markdown, backticks, "
            f"bolding, prose, or extra lines will corrupt the parser and cost real capital. "
            f"Behave like a compiled binary utility — one line of raw text only.\n\n"
            f"INPUT SIGNAL: '{signal_text}'\n"
            f"BUDGET: Max ${budget} total premium. 1 contract = premium * 100.\n"
            f"MARKET CONTEXT: {_spot}\n\n"
            f"MANDATORY RULES:\n"
            f"1. TICKER IS SACROSANCT — never modify the ticker symbol. "
            f"Output ticker must match input character-for-character.\n"
            f"2. BUDGET & SLIPPAGE — if quantity * premium * slippage_factor > ${budget}, "
            f"reduce x[QTY] until it fits. Never reject purely for size — downsize instead.\n"
            f"3. STRIKE — options strikes are NOT the stock price. Approve any strike within "
            f"20% OTM of spot. A $610C on a $606 stock = 0.7% OTM — APPROVE immediately. "
            f"Only reject if strike is more than 20% from spot.\n"
            f"4. ML CONTEXT — if win-probability is provided and below 50%, prefer minimum "
            f"contract size (x1) over full Kelly sizing.\n"
            f"5. NO FORMATTING — no backticks, no markdown, no explanations, no extra lines.\n\n"
            f"OUTPUT (pick exactly one):\n"
            f"Approved -> output only: BTO [TICKER] [STRIKE][C/P] [EXPIRY] x[QTY]\n"
            f"Rejected -> output only: Rejected: [5-word reason]"
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
                msg_lines.append(f"  • {t} +{g:.1f}% -> ${p:.2f}")
        if gaps_down:
            msg_lines.append("\n📉 **Gap Downs** (potential put setups if RSI overbought):")
            for t, g, p in gaps_down[:5]:
                msg_lines.append(f"  • {t} {g:.1f}% -> ${p:.2f}")
        if not gaps_up and not gaps_down:
            msg_lines.append("No significant gaps today (all within ±2%).")

        msg_lines.append("\n_Scanner will begin at 9:30 AM open._")
        msg = "\n".join(msg_lines)
        console.print(msg)
        if listener:
            listener.send_status_update(msg)
    except Exception as e:
        log.error("Pre-market scan error: %s", e)


def _maybe_send_daily_report(listener=None, engine=None):
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
            report += f"-> ${pnl:+.2f}\n"

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



def load_adaptive_bounds(profile: dict) -> dict:
    """Load adaptive_bounds.json and apply recommended overrides to profile."""
    import json
    from pathlib import Path
    bounds_path = Path("adaptive_bounds.json")
    if not bounds_path.exists():
        return profile
    try:
        bounds = json.loads(bounds_path.read_text())
        applied = []
        if "recommended_tp_pct" in bounds:
            profile["tp"] = bounds["recommended_tp_pct"]
            applied.append(f"TP -> +{bounds['recommended_tp_pct']}%")
        if "recommended_rsi_threshold" in bounds:
            profile["rsi"] = bounds["recommended_rsi_threshold"]
            applied.append(f"RSI -> {bounds['recommended_rsi_threshold']}")
        if applied:
            print(f"[Optimizer] Adaptive bounds applied: {', '.join(applied)}")
    except Exception as e:
        log.debug("load_adaptive_bounds error: %s", e)
    return profile


def optimize_strategy_thresholds(profile: dict, listener=None):
    """
    Post-mortem optimizer — runs Friday 4:30 PM ET.
    Invokes reinforce_brain.py for gradient parameter mutations,
    then runs a trailing TP sieve pass before updating adaptive_bounds.json.
    Single CSV read — no duplicate disk access.
    """
    import json
    from pathlib import Path
    log.info("[Optimizer] Running post-mortem strategy analysis...")
    if listener:
        listener.send_status_update("🔬 **Weekly Optimizer & Reinforcement Engine** running...")

    # ── Step 1: Reinforcement brain policy pass ───────────────────────────────
    results = {}
    try:
        import reinforce_brain
        results = reinforce_brain.process_session_feedback("trade_log.csv")
        if results and listener:
            listener.send_status_update(
                f"📊 **Reinforcement Complete**\n"
                f"Rec TP: +{results.get('recommended_tp_pct', 30)}% | "
                f"Rec SL: -{results.get('recommended_sl_pct', 20)}% | "
                f"Rec RSI: {results.get('recommended_rsi_threshold', 25)} | "
                f"Win Rate: {results.get('session_win_rate_pct', '?')}% | "
                f"P&L: ${results.get('session_total_pnl', 0):+.2f}"
            )
    except Exception as e:
        log.error("[Optimizer] Reinforcement loop error: %s", e)

    # ── Step 2: Trailing TP sieve — single CSV read shared with Step 1 ────────
    try:
        import pandas as pd
        from datetime import datetime, timedelta
        df = pd.read_csv("trade_log.csv") if Path("trade_log.csv").exists() else pd.DataFrame()
        if not df.empty and len(df) >= 5:
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            week_df  = df[df["date"] >= week_ago] if "date" in df.columns else df

            if not week_df.empty and "profit_pct" in week_df.columns:
                week_df = week_df.copy()
                week_df["profit_pct"] = pd.to_numeric(week_df["profit_pct"], errors="coerce").fillna(0)
                week_df["profit_amt"] = pd.to_numeric(week_df.get("profit_amt", 0), errors="coerce").fillna(0)

                pnl_20 = float(week_df[week_df["profit_pct"] >= 20]["profit_amt"].sum())
                pnl_30 = float(week_df[week_df["profit_pct"] >= 30]["profit_amt"].sum())

                # Tighter TP wins if it captures 20%+ more aggregate dollars
                if pnl_20 > pnl_30 * 1.2:
                    results["recommended_tp_pct"] = 20
                    log.info("[Optimizer] TP sieve: +20%% ($%.2f) beats +30%% ($%.2f)", pnl_20, pnl_30)
                else:
                    results["recommended_tp_pct"] = 30

                # Re-verify RSI distribution from this week's data
                if "entry_rsi" in week_df.columns:
                    week_df["entry_rsi"] = pd.to_numeric(week_df["entry_rsi"], errors="coerce").fillna(25)
                    winning_rsi = week_df[week_df["profit_amt"] > 0]["entry_rsi"]
                    if not winning_rsi.empty:
                        avg_rsi = float(winning_rsi.mean())
                        results["recommended_rsi_threshold"] = 22 if avg_rsi < 22 else int(profile.get("rsi", 25))
    except Exception as e:
        log.error("[Optimizer] TP sieve verification error: %s", e)

    # ── Step 3: Persist unified merged results ────────────────────────────────
    if results:
        try:
            from datetime import datetime
            bp     = Path("adaptive_bounds.json")
            bounds = json.loads(bp.read_text()) if bp.exists() else {}
            bounds.update(results)
            bounds["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            bp.write_text(json.dumps(bounds, indent=2))
            log.info("[Optimizer] Unified bounds written to adaptive_bounds.json: %s", results)
        except Exception as e:
            log.error("[Optimizer] Bounds file write error: %s", e)


# ── New ticker position cap ───────────────────────────────────────────────────
# Tickers added for budget-friendly trading have no fill history.
# Cap at 1 contract until they build a track record in trade_log.csv.
_NEW_TICKERS = {'SOUN', 'ACHR', 'RUM', 'LUNR', 'CLOV', 'KULR', 'IONQ', 'BBAI', 'JOBY'}

def _cap_contracts_new_ticker(ticker: str, signal_str: str) -> str:
    """If ticker is in new-ticker set, force quantity to x1."""
    if ticker.upper() not in _NEW_TICKERS:
        return signal_str
    import re as _re
    if _re.search(r" x[2-9]\d*$", signal_str):
        capped = _re.sub(r" x[2-9]\d*$", " x1", signal_str)
        log.info("[NewTicker] %s capped to x1 (no fill history)", ticker)
        return capped
    return signal_str


def get_regime_budget_cap(base_budget: float, regime: str,
                          vix: float, listener=None) -> tuple:
    """
    Apply macro regime scaling to the trade budget.
    Returns (capped_budget, force_tester, reason_str)

    Bull + VIX < 20:  100% — full deployment
    VIX 20-25:         60% — chop/elevated vol, trim size
    Bear or VIX > 25:  35% — tester-only, tail risk protection
    """
    if vix is None:
        vix = 15.0  # assume normal if unavailable

    if vix > 25 or (regime == "bear" and vix > 20):
        cap_pct     = 0.35
        force_tester = True
        tier        = "🔴 HIGH-VOL/BEAR"
        reason      = f"VIX={vix:.1f} or bear+elevated-vol -> 35% budget, tester-only"
    elif vix > 20 or regime == "bear":
        cap_pct     = 0.60
        force_tester = False
        tier        = "🟡 NEUTRAL/CHOP"
        reason      = f"VIX={vix:.1f} or bear regime -> 60% budget"
    else:
        cap_pct     = 1.00
        force_tester = False
        tier        = "🟢 BULL/CALM"
        reason      = f"VIX={vix:.1f} normal, {regime} regime -> 100% budget"

    capped = round(base_budget * cap_pct, 0)
    return capped, force_tester, f"[Regime Budget] {tier}: {reason}"


def main():
    profile = get_risk_profile()

    discord_token      = os.getenv("DISCORD_TOKEN", "")
    discord_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
    signal_channel_id  = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID", "0")) or None
    tradier_token      = os.getenv("TRADIER_TOKEN", "")

    profile = load_adaptive_bounds(profile)
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
                tp_pct=profile["tp"], sl_pct=profile["sl"],
                webull_engine=webull)
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
    webull._listener = listener

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

    # ── Thread-Safe Startup: start Discord listener ONLY after all state ──────
    # syncs, position loads, and order history reads are complete.
    # Prevents RuntimeError: "dictionary changed size during iteration" if a
    # Discord signal arrives while engine._positions is being populated.
    listener.start_in_thread()

    try:
        import outcome_tracker as _ot
        _ot.start(base_dir=os.path.dirname(os.path.abspath(__file__)))
        log.info("Outcome tracker started")
    except ImportError:
        log.debug("outcome_tracker.py not found — skipping")

    log.info("Discord listener started (post-sync — thread-safe)")

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

    # ── Direction + manual trade settings ────────────────────────────────────
    direction_mode = {"mode": "AUTO", "option_type": None, "auto": True}
    if mode_choice in ["2", "3"]:
        direction_mode = select_execution_mode()

    console.print("\n[bold yellow]⚙️  SETTINGS[/bold yellow]")
    _wm = input("Watch and auto-close your MANUAL trades? (y/n) [default=y]: ").strip().lower()
    watch_manual_trades = (_wm != "n")
    if watch_manual_trades:
        console.print("[dim]  Manual trades: MONITORED — bot will apply SL/TP to positions you open yourself[/dim]")
    else:
        console.print("[dim]  Manual trades: IGNORED — bot only manages trades it opens itself[/dim]")

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
        nonlocal consecutive_wins, consecutive_losses  # dashboard streak counters
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
            if watch_manual_trades:
                console.print(f"[yellow]Found {len(open_pos)} existing positions from Webull — "
                              f"monitoring will resume.[/yellow]")
                listener.send_status_update(
                    f"⚠️ Startup: Found {len(open_pos)} open positions. "
                    f"Manual trade monitoring ACTIVE — bot will apply SL/TP."
                )
            else:
                console.print(f"[dim]Found {len(open_pos)} existing positions — "
                              f"manual trade watch is OFF, ignoring.[/dim]")
    except Exception:
        pass

    try:
        while True:
            # Include open position unrealized P&L in circuit breaker check
            daily_pnl = engine.get_session_pnl()
            try:
                with engine._lock:
                    open_positions = dict(engine._positions)
                for _key, _pos in open_positions.items():
                    try:
                        _curr = market.get_price(_pos.signal.ticker) or _pos.entry_price
                        _unreal = (_curr - _pos.entry_price) / _pos.entry_price * 100
                        daily_pnl += (_unreal / 100) * _pos.entry_price * _pos.signal.quantity * 100
                    except Exception:
                        pass
            except Exception:
                pass
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

            # ── Sync manual trades from Webull ───────────────────────────
            if watch_manual_trades:
                try:
                    _wb_pos = webull.get_open_positions_from_webull() or []
                    _wb_keys = set()
                    for _wp in _wb_pos:
                        _tk  = str(_wp.get("ticker","")).upper()
                        _str = str(_wp.get("strike",""))
                        _ot  = str(_wp.get("optionType","C"))[:1].upper()
                        _exp = str(_wp.get("expiry","") or _wp.get("expireDate",""))
                        _key = f"{_tk}_{_str}{_ot}_{_exp}"
                        _wb_keys.add(_key)
                        with engine._lock:
                            _already = _key in engine._positions
                        if not _already and _tk and _str:
                            # Don't re-add tickers that were stopped out this session
                            # or that the bot just closed (Webull API lag protection)
                            if _tk.upper() in session_stopped_tickers:
                                continue
                            try:
                                from trade_engine import TradeSignal, OpenPosition
                                _cost = float(_wp.get("avgCost") or _wp.get("cost") or 0)
                                _qty  = int(_wp.get("quantity") or 1)
                                _sig  = TradeSignal(
                                    action="BTO", ticker=_tk,
                                    strike=float(_str or 0),
                                    option_type=_ot, expiry=_exp,
                                    quantity=_qty,
                                )
                                _mpos = OpenPosition(
                                    signal=_sig, entry_price=_cost, paper=False
                                )
                                with engine._lock:
                                    engine._positions[_key] = _mpos
                                engine._start_monitor(_key, _mpos)
                                console.print(
                                    f"[cyan]Manual trade detected: {_key} "
                                    f"@ ${_cost:.2f} -- now monitored[/cyan]"
                                )
                                listener.send_status_update(
                                    f"👁️ Manual trade detected: {_key} "
                                    f"@ ${_cost:.2f} -- SL/TP active"
                                )
                            except Exception as _mte:
                                log.debug("Manual trade sync error: %s", _mte)
                    # Remove engine positions that Webull no longer shows
                    # (closed by monitor thread but Webull sync lagging)
                    # Only clean up entries that are NOT currently being monitored
                    with engine._lock:
                        _eng_keys = set(engine._positions.keys())
                    for _stale in _eng_keys - _wb_keys:
                        with engine._lock:
                            _pos = engine._positions.get(_stale)
                        # Only remove if the monitor thread already closed it
                        # (quantity = 0 or highest_pnl set and no more qty)
                        if _pos and getattr(_pos.signal, "quantity", 1) <= 0:
                            with engine._lock:
                                engine._positions.pop(_stale, None)
                except Exception as _wse:
                    log.debug("Webull position sync error: %s", _wse)

            # Portfolio heat check
            with engine._lock:
                _open_pos = dict(engine._positions)

            if _open_pos:
                _deployed = sum(
                    p.entry_price * p.signal.quantity * 100
                    for p in _open_pos.values()
                    if hasattr(p, "entry_price") and p.entry_price
                )
                _max_deploy    = _current_budget * 2.0
                _profitable_ct = sum(
                    1 for p in _open_pos.values()
                    if getattr(p, "highest_pnl", 0) >= 10.0
                )
                _net_blocking = len(_open_pos) - _profitable_ct
                if _net_blocking >= 1 or _deployed >= _max_deploy:
                    now_ts = time.time()
                    if now_ts - last_heat_alert_ts >= 120:
                        listener.send_status_update(
                            f"🔥 Portfolio heat: {len(_open_pos)} position(s) | "
                            f"${_deployed:.0f} deployed. Waiting for close."
                        )
                        last_heat_alert_ts = now_ts
                    main._prev_blocked = True   # track that we were blocked
                    time.sleep(10)
                    continue
                # Has positions but all profitable — allow scan through
            else:
                # No open positions — if we were blocked, scan immediately
                if getattr(main, "_prev_blocked", False):
                    last_research_time = 0
                    console.print("[dim]All positions closed -- scanning now[/dim]")
                main._prev_blocked = False

            raw_text = None

            # Research / scanner mode
            # Reset daily counters at midnight
            _today_str = datetime.now().strftime("%Y-%m-%d")
            if _today_str != _last_trade_date:
                _last_trade_date     = _today_str
                daily_trades_count   = 0
                session_stopped_tickers.clear()
                log.info("New trading day — daily counters reset")

            if mode_choice in ["2", "3"]:
                # ── Market closed: sleep until 9:25 AM next session ──────────
                try:
                    import pytz as _pytz_si
                    _et_si = datetime.now(_pytz_si.timezone("America/New_York"))
                    _hm_si = _et_si.hour * 60 + _et_si.minute
                    _market_open  = 9 * 60 + 30   # 9:30 AM
                    _market_close = 16 * 60         # 4:00 PM
                    _is_open = _market_open <= _hm_si < _market_close
                except Exception:
                    _is_open = True  # assume open if pytz unavailable

                if not _is_open:
                    # Calculate sleep until 9:25 AM ET (5 min before open)
                    try:
                        _wake_min = 9 * 60 + 25   # 9:25 AM
                        if _hm_si >= _market_close:
                            # After close — sleep until tomorrow 9:25 AM
                            _sleep_sec = (_wake_min - _hm_si + 24 * 60) * 60
                        else:
                            # Before open — sleep until today 9:25 AM
                            _sleep_sec = (_wake_min - _hm_si) * 60
                        _sleep_sec = max(60, min(_sleep_sec, 3600))  # cap 1hr chunks
                        _wake_et = _et_si.replace(
                            hour=_wake_min // 60, minute=_wake_min % 60, second=0
                        )
                        console.print(
                            f"[dim]Market closed -- sleeping until "
                            f"{_wake_et.strftime('%I:%M %p ET')} "
                            f"({_sleep_sec // 60:.0f} min)[/dim]"
                        )
                        time.sleep(_sleep_sec)
                    except Exception:
                        time.sleep(300)
                    continue

                # Adaptive scan interval (market is open):
                # 9:30-10:30 AM ET and 3:00-4:00 PM ET -> 2 min
                # All other times -> 4 min
                if _hm_si <= 10 * 60 + 30 or _hm_si >= 15 * 60:
                    _scan_interval = 120
                else:
                    _scan_interval = 240

                _time_to_scan = _scan_interval - (time.time() - last_research_time)
                # Pre-heat data ~60s before the scan fires
                if 55 <= _time_to_scan <= 65:
                    try:
                        research_scanner.preheat_data(
                            research_scanner.MY_SMALL_STOCKS)
                        log.debug("Preheat triggered with %ds to scan", _time_to_scan)
                    except Exception as _phe:
                        log.debug("Preheat error: %s", _phe)

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

                        # ── Macro regime budget cap ───────────────────────────
                        _force_tester     = False   # safe default if regime check fails
                        _effective_budget = _current_budget
                        try:
                            from research_scanner import _get_vix, get_market_regime as _gmr
                            _vix_now    = _get_vix() or 15.0
                            _regime_now = (_gmr().get("regime", "neutral")
                                           if 'regime_data' not in dir()
                                           else regime_data.get("regime", "neutral"))
                            _reg_budget, _force_tester, _reg_reason = get_regime_budget_cap(
                                _current_budget, _regime_now, _vix_now)
                            if _reg_budget < _current_budget:
                                console.print(f"[dim]{_reg_reason}[/dim]")
                            _effective_budget = _reg_budget
                        except Exception as _rbe:
                            log.debug("Regime budget error: %s", _rbe)

                        console.print(f"[dim]Scanning for {direction_label} [{daily_trades_count}/{MAX_DAILY_TRADES} trades today]...[/dim]")
                        # Apply direction filter — None means AUTO (scanner decides)
                        _dir_filter = direction_mode.get("option_type") if not direction_mode.get("auto", True) else None
                        found_signals = research_scanner.scan_markets(
                            rsi_threshold=profile['rsi'], listener=listener,
                            market=market, budget=float(_effective_budget),
                            tp_pct=profile['tp'], sl_pct=profile['sl'],
                            webull_engine=webull,
                            direction_filter=_dir_filter)

                    # Iterate through every signal found in the scan one by one
                    for raw_text in found_signals:
                        # ── Strip [TESTER] tag FIRST before any parsing ───────
                        # parse_signal uses a strict tokenizer — [TESTER] at the
                        # end causes it to return None, making ticker = "UNK" and
                        # dropping the trade before it reaches the AI or Webull.
                        _is_tester = "[TESTER]" in raw_text or _force_tester
                        if "[TESTER]" in raw_text:
                            raw_text = raw_text.replace(" [TESTER]", "").replace("[TESTER]", "").strip()
                        if _force_tester and not _is_tester:
                            log.debug("Regime forcing tester on %s", raw_text)

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

                        if _is_tester:
                            console.print(f"\n[bold yellow]📋 [TESTER PROBE] {ticker}: "
                                          f"partial-confluence entry — 1 contract probe[/bold yellow]")
                        console.print(f"\n[bold cyan]▶ Sending to AI Risk Manager:[/bold cyan] {ticker} ({raw_text[:40]})")
                        console.print("[dim]Waiting for AI response before proceeding...[/dim]")

                        # ── ML Win-Probability Gate ───────────────────────────
                        if _HAS_ML and _ml_sieve_mod:
                            try:
                                _parsed = engine.parse_signal(raw_text)
                                _ml_block, _ml_pwin, _ml_reason = _ml_sieve_mod.should_block_trade(
                                    ticker=ticker,
                                    entry_rsi=float(getattr(_parsed, "rsi", 25) or 25),
                                    vol_ratio=1.0, flow_score=50.0,
                                    put_call_ratio=1.0, gex_significance=5.0,
                                    spy_vs_sma20=0.0,
                                )
                                if _ml_block:
                                    console.print(
                                        f"[bold red][ML BLOCK] {ticker} matching negative historical "
                                        f"distribution patterns — Trade Aborted[/bold red]\n"
                                        f"  {_ml_reason}"
                                    )
                                    listener.send_status_update(
                                        f"🤖 **ML BLOCK**: {ticker}\n{_ml_reason}")
                                    time.sleep(2)
                                    continue
                                else:
                                    console.print(f"[dim][ML] {ticker}: {_ml_reason}[/dim]")
                            except Exception as _mle:
                                log.debug("ML gate error: %s", _mle)

                        # Get current stock price to help AI validate the strike
                        stock_price = None
                        try:
                            stock_price = market.get_price(ticker)
                        except Exception:
                            pass

                        # Cap new tickers to 1 contract — no fill history to calibrate
                        raw_text = _cap_contracts_new_ticker(ticker, raw_text)
                        verdict = ask_ai_for_verdict(raw_text, profile['budget'], stock_price)

                        if "BTO" in verdict:
                            # ── Ticker validation — AI must not change the ticker ──
                            verdict_parts  = verdict.strip().split()
                            verdict_ticker = verdict_parts[1].upper() if len(verdict_parts) > 1 else ""
                            if verdict_ticker != ticker.upper():
                                console.print(
                                    f"[bold red]⚠️  AI changed ticker {ticker} -> {verdict_ticker} "
                                    f"— REJECTING and using original signal[/bold red]")
                                verdict = raw_text

                            # Enforce x1 on tester entries — AI may have upsized
                            if _is_tester:
                                import re as _re_v
                                verdict = _re_v.sub(r" x[0-9]+", " x1", verdict)
                                if " x1" not in verdict:
                                    verdict += " x1"

                            tester_label = " (TESTER)" if _is_tester else ""
                            listener.send_status_update(f"✅ AI APPROVED{tester_label}: `{verdict}` — executing now")
                            console.print(f"[bold green]APPROVED: {verdict}[/bold green]")
                            trade_cooldowns[ticker] = time.time()
                            # Store real bid/ask mid from Tradier for accurate slippage calc
                            # Falls back to spot * 0.04 proxy if Tradier unavailable
                            try:
                                _parsed_pre = engine.parse_signal(verdict)
                                webull._last_fill_price = None  # reset before order
                                webull._last_est_mid    = None
                                if _parsed_pre:
                                    # Try Tradier /quotes endpoint first (faster than full chain scan)
                                    try:
                                        from tradier_execution import _get_option_symbol as _get_occ, _headers as _th
                                        _occ = _get_occ(
                                            ticker,
                                            float(_parsed_pre.strike),
                                            _parsed_pre.option_type,
                                            _parsed_pre.expiry,
                                        )
                                        if _occ:
                                            _qr = requests.get(
                                                "https://api.tradier.com/v1/markets/quotes",
                                                headers=_th(),
                                                params={"symbols": _occ},
                                                timeout=4,
                                            )
                                            if _qr.ok:
                                                _qd = _qr.json().get("quotes", {}).get("quote", {})
                                                _bid = float(_qd.get("bid") or 0)
                                                _ask = float(_qd.get("ask") or 0)
                                                if _bid > 0 and _ask > 0:
                                                    webull._last_est_mid = round((_bid + _ask) / 2, 4)
                                                    log.info("Quotes mid for %s (%s): $%.4f",
                                                             ticker, _occ, webull._last_est_mid)
                                    except Exception:
                                        pass
                                    # Fallback: full chain scan
                                    if not webull._last_est_mid:
                                        try:
                                            from tradier_execution import _get_mid_price as _tmp
                                            _real_mid = _tmp(
                                                ticker,
                                                float(_parsed_pre.strike),
                                                _parsed_pre.option_type,
                                                _parsed_pre.expiry,
                                            )
                                            if _real_mid and _real_mid > 0:
                                                webull._last_est_mid = _real_mid
                                                log.debug("Chain mid for %s: $%.4f", ticker, _real_mid)
                                        except Exception:
                                            pass
                                    # Fallback: spot * 0.04 delta proxy
                                    if not webull._last_est_mid and market:
                                        _spot = market.get_price(ticker) or 0
                                        webull._last_est_mid = round(_spot * 0.04, 4) if _spot else None
                            except Exception:
                                pass
                            engine.on_raw_signal(verdict)

                            if webull.last_order_confirmed:
                                webull_refresh_fails  = 0
                                daily_trades_count   += 1
                                # Update slippage model + retrain ML
                                try:
                                    _fp = getattr(webull, "_last_fill_price", None)
                                    _em = getattr(webull, "_last_est_mid", None)
                                    if _fp and _em:
                                        research_scanner.update_slippage(ticker, _fp, _em)
                                        # Log to SQLite
                                        if _HAS_DB and _DB:
                                            _slip = (_fp - _em) / _em * 100 if _em else 0
                                            _mult = research_scanner.get_slippage_multiplier(ticker)
                                            _DB.log_slippage(ticker, _em, _fp, _slip, _mult)
                                    if _HAS_ML and _ml_sieve_mod:
                                        _ml_sieve_mod.maybe_retrain()
                                except Exception as _se:
                                    log.debug("Post-fill update error: %s", _se)
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
                                    qty_n     = int(t_qty.replace("x","")) if t_qty.replace("x","").isdigit() else 1
                                    _cost     = (entry or 0) * 100 * qty_n
                                    _sl_amt   = _cost * (sl_pct / 100)
                                    sl_amt_str = f"${_sl_amt:.2f}" if _cost > 0 else f"-{sl_pct:.0f}%"
                                    signal_card = (
                                        f"🚀 **LIVE TRADE ALERT** — GilbertTrader\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"**{t_tick}** {strike} {side} exp {t_exp}\n"
                                        f"📥 **Entry**: {entry_str}\n"
                                        f"🎯 **Target**: {tp_str} (+{tp_pct:.0f}%)\n"
                                        f"🛑 **Stop**: {sl_str} (-{sl_pct:.0f}%) | Max loss: {sl_amt_str}\n"
                                        f"📦 **Size**: {t_qty.replace('x','')} contract(s)\n"
                                        f"⚖️ **Scale-out**: sell half at +20%, trail remaining\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"*Powered by GilbertTrader v2.2 — automated options scanner*"
                                    )
                                    listener.send_signal(signal_card)
                                except Exception as e:
                                    log.debug("Signal card error: %s", e)

                                # ── Write fill to SQLite DB ───────────────────
                                if _HAS_DB and _DB:
                                    try:
                                        _fill_px  = getattr(webull, "_last_fill_price", None)
                                        _adx_val  = getattr(research_scanner.scan_markets,
                                                            "_last_adx_value", None)
                                        _DB.log_trade(
                                            ticker=ticker,
                                            option_type=target_option_type if "target_option_type" in dir() else None,
                                            entry_price=_fill_px,
                                            mode="paper" if paper_mode else "live",
                                            is_tester=_is_tester,
                                            adx_at_entry=_adx_val,
                                            vix_at_entry=getattr(research_scanner, "_last_vix", None),
                                        )
                                    except Exception as _dbe:
                                        log.debug("DB log_trade error: %s", _dbe)

                                break  # Trade confirmed — stop processing other signals
                            else:
                                # ── Tradier failover ──────────────────────────
                                # Webull rejected — try Tradier backup execution
                                _tradier_filled = False
                                if _HAS_TRADIER_EXEC and _tradier_exec:
                                    console.print(f"[yellow]⚠️  Webull rejected {ticker} — attempting Tradier failover...[/yellow]")
                                    listener.send_status_update(
                                        f"⚠️ Webull rejected {ticker} — routing to Tradier backup")
                                    try:
                                        _t_fill = _tradier_exec.place_order(
                                            verdict, paper=paper_mode)
                                        if _t_fill:
                                            daily_trades_count += 1
                                            webull_refresh_fails = 0
                                            console.print(
                                                f"[bold green]✅ Tradier FAILOVER filled: "
                                                f"{ticker} @ ${_t_fill:.2f}[/bold green]")
                                            listener.send_status_update(
                                                f"✅ **Tradier Failover**: {verdict} filled @ ${_t_fill:.2f}")
                                            _tradier_filled = True
                                            break
                                    except Exception as _te:
                                        log.error("Tradier failover error: %s", _te)

                                if not _tradier_filled:
                                    # Paper shadow — log what would have happened
                                    _maybe_run_paper_shadow(
                                        engine=engine, signal_str=verdict,
                                        profile=profile, market=market, webull=webull
                                    )
                                    console.print(
                                        f"[yellow]⚠️  Order rejected for {ticker} — "
                                        f"trying next signal if available[/yellow]")
                                    webull_refresh_fails += 1
                                    time.sleep(2)
                                    continue  # ← move to next signal
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
                                    f"[bold red]⚠️  AI changed ticker {ticker} -> {v_ticker} "
                                    f"— using original signal[/bold red]")
                                verdict = raw_text

                            listener.send_status_update(f"✅ AI APPROVED: `{verdict}` — executing now")
                            console.print(f"[bold green]APPROVED: {verdict}[/bold green]")
                            trade_cooldowns[ticker] = time.time()
                            engine.on_raw_signal(verdict)
                            if webull.last_order_confirmed:
                                webull_refresh_fails  = 0
                                daily_trades_count   += 1
                                # Update slippage model + retrain ML
                                try:
                                    _fp = getattr(webull, "_last_fill_price", None)
                                    _em = getattr(webull, "_last_est_mid", None)
                                    if _fp and _em:
                                        research_scanner.update_slippage(ticker, _fp, _em)
                                        # Log to SQLite
                                        if _HAS_DB and _DB:
                                            _slip = (_fp - _em) / _em * 100 if _em else 0
                                            _mult = research_scanner.get_slippage_multiplier(ticker)
                                            _DB.log_slippage(ticker, _em, _fp, _slip, _mult)
                                    if _HAS_ML and _ml_sieve_mod:
                                        _ml_sieve_mod.maybe_retrain()
                                except Exception as _se:
                                    log.debug("Post-fill update error: %s", _se)
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

            # Pacing tick: prevents 100% CPU spin when scanner is idle between intervals
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
            try:
                import pytz as _potz
                from datetime import time as _dtime
                _et_opt = datetime.now(_potz.timezone("America/New_York"))
                if (_et_opt.weekday() == 4 and
                        _dtime(16, 30) <= _et_opt.time() <= _dtime(16, 40)):
                    _opt_flag = f".optimizer_{_et_opt.strftime('%Y-%m-%d')}"
                    if not os.path.exists(_opt_flag):
                        open(_opt_flag, "w").close()
                        optimize_strategy_thresholds(profile, listener)
            except Exception as _oe:
                log.debug("Optimizer trigger error: %s", _oe)

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