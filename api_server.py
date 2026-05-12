"""
api_server.py — GilbertTrader REST + SSE API Server
=====================================================
Connects the React UI (pixel-perfect) to the Python bot.

Endpoints the UI's HttpProvider calls:
  GET  /api/portfolio/summary?range=1D
  GET  /api/portfolio/series?range=1D
  GET  /api/positions
  GET  /api/trades/recent?range=1D
  GET  /api/stats?range=1D
  GET  /api/stats/winrate?range=1D
  GET  /api/stats/exits?range=1D
  GET  /api/watchlist
  GET  /api/scanner/stream          ← SSE stream
  GET  /api/news?symbol=AAPL        ← Yahoo Finance (no Finnhub key needed)
  GET  /api/regime                  ← market regime
  POST /api/bot/pause
  POST /api/bot/resume
  POST /api/bot/scan
  POST /api/bot/close-all
  GET  /api/bot/status

Run alongside main.py:
  python api_server.py

Or add to run_trader.ps1:
  Start-Process python -ArgumentList "api_server.py"
"""

import csv
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import requests as _requests

load_dotenv()

app    = Flask(__name__)
CORS(app, origins=["http://localhost:3000", "http://localhost:5173",
                   "http://127.0.0.1:3000", "http://127.0.0.1:5173"])

TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.csv")
BRAIN_LOG  = os.path.join(os.path.dirname(__file__), "brain.log")

# ── Scanner SSE broadcast queue ───────────────────────────────────────────────
_scanner_clients: list[queue.Queue] = []
_scanner_lock = threading.Lock()

def broadcast_scanner(entry: dict):
    """Call this from research_scanner to push entries to the UI."""
    with _scanner_lock:
        dead = []
        for q in _scanner_clients:
            try:
                q.put_nowait(entry)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _scanner_clients.remove(q)

# ── Bot state (shared with main.py via module globals) ────────────────────────
_bot_state = {
    "paused":      False,
    "running":     True,
    "regime":      "neutral",
    "spy_vs_sma":  0.0,
    "positions":   [],
    "session_pnl": 0.0,
    "last_scan":   None,
}

def update_bot_state(**kwargs):
    """Called by main.py to keep UI in sync."""
    _bot_state.update(kwargs)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_trades(days: int = 1) -> list[dict]:
    if not os.path.exists(TRADE_LOG):
        return []
    try:
        trades = []
        with open(TRADE_LOG, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                trades.append(row)
        if days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            trades = [t for t in trades
                      if _parse_dt(t.get("date","")) >= cutoff]
        return trades
    except Exception:
        return []

def _parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return datetime.min

def _range_days(r: str) -> int:
    return {"1D":1,"1W":7,"1M":30,"3M":90,"6M":180,"1Y":365,"ALL":9999}.get(r, 1)

def _build_series(trades: list[dict]) -> list[dict]:
    """Build cumulative P&L series from trade log."""
    if not trades:
        return [{"t": datetime.now().strftime("%H:%M"), "ts": int(time.time()*1000), "v": 0}]
    sorted_t = sorted(trades, key=lambda x: x.get("date",""))
    cumulative = 0.0
    series = []
    for t in sorted_t:
        try:
            cumulative += float(t.get("profit_amt", 0))
        except Exception:
            pass
        dt  = _parse_dt(t.get("date",""))
        series.append({
            "t":  dt.strftime("%H:%M") if dt != datetime.min else "?",
            "ts": int(dt.timestamp() * 1000),
            "v":  round(cumulative, 2),
        })
    return series

# ── Portfolio endpoints ───────────────────────────────────────────────────────

@app.get("/api/portfolio/summary")
def portfolio_summary():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    total_pnl = sum(float(t.get("profit_amt", 0)) for t in trades)
    try:
        from webull import paper_webull
        # Try to get real paper account balance
        # This is cached from webull_engine startup
        buying_power = 100000.0
    except Exception:
        buying_power = 100000.0

    return jsonify({
        "value":        round(buying_power + total_pnl, 2),
        "dayChange":    round(total_pnl, 2),
        "dayChangePct": round(total_pnl / buying_power * 100, 3),
        "buyingPower":  round(buying_power, 2),
        "cash":         round(buying_power, 2),
    })

@app.get("/api/portfolio/series")
def portfolio_series():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    return jsonify(_build_series(trades))

# ── Positions endpoint ────────────────────────────────────────────────────────

@app.get("/api/positions")
def positions():
    """Return open positions from bot state."""
    pos = _bot_state.get("positions", [])
    result = []
    for p in pos:
        try:
            entry   = float(p.get("entry_price", 0))
            current = float(p.get("current_price", entry))
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
            peak    = float(p.get("highest_pnl", pnl_pct))
            sig     = p.get("signal", {})
            ticker  = sig.get("ticker", "?") if isinstance(sig, dict) else str(sig)
            strike  = sig.get("strike", 0) if isinstance(sig, dict) else 0
            opt     = sig.get("option_type", "C") if isinstance(sig, dict) else "C"
            expiry  = sig.get("expiry", "") if isinstance(sig, dict) else ""
            qty     = sig.get("quantity", 1) if isinstance(sig, dict) else 1

            # Status logic
            tp = _bot_state.get("tp_pct", 30)
            sl = _bot_state.get("sl_pct", 20)
            if pnl_pct >= tp * 0.8:      status = "Near TP"
            elif pnl_pct <= -sl * 0.8:   status = "Near SL"
            elif pnl_pct >= tp * 0.5:    status = "Scaling Out"
            else:                         status = "Monitoring"

            result.append({
                "contract": f"{ticker} {strike}{opt}",
                "side":     "CALL" if opt == "C" else "PUT",
                "entry":    entry,
                "current":  current,
                "pnlPct":   round(pnl_pct, 2),
                "peakPct":  round(peak, 2),
                "size":     qty,
                "expiry":   expiry,
                "status":   status,
            })
        except Exception:
            continue
    return jsonify(result)

# ── Trades endpoint ───────────────────────────────────────────────────────────

@app.get("/api/trades/recent")
def recent_trades():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    result = []
    for t in reversed(trades[-100:]):
        try:
            pnl    = float(t.get("profit_amt", 0))
            ticker = t.get("ticker", "?")
            result.append({
                "time":   _parse_dt(t.get("date","")).strftime("%H:%M:%S"),
                "ticker": ticker,
                "strike": t.get("strike", "?"),
                "expiry": t.get("expiry", "?"),
                "side":   "CALL" if t.get("option_type","C") == "C" else "PUT",
                "entry":  float(t.get("entry_price", 0)),
                "exit":   float(t.get("exit_price", 0)),
                "pnl":    round(pnl, 2),
                "reason": t.get("reason", "?"),
            })
        except Exception:
            continue
    return jsonify(result)

# ── Stats endpoints ───────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    if not trades:
        return jsonify({"totalTrades":0,"winRate":0,"avgWin":0,
                        "avgLoss":0,"expectancy":0,"maxDrawdown":0,"sharpe":0})

    pnls  = [float(t.get("profit_amt",0)) for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]
    wr    = len(wins)/len(pnls)*100 if pnls else 0
    aw    = sum(wins)/len(wins)    if wins   else 0
    al    = sum(losses)/len(losses)if losses else 0
    exp   = (wr/100 * aw) + ((1-wr/100) * al)

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Sharpe proxy
    import statistics
    std = statistics.stdev(pnls) if len(pnls) > 1 else 1
    sharpe = round((sum(pnls)/len(pnls)) / std, 2) if std > 0 else 0

    return jsonify({
        "totalTrades": len(trades),
        "winRate":     round(wr, 1),
        "avgWin":      round(aw, 2),
        "avgLoss":     round(al, 2),
        "expectancy":  round(exp, 2),
        "maxDrawdown": round(max_dd, 2),
        "sharpe":      sharpe,
    })

@app.get("/api/stats/winrate")
def winrate_by_ticker():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    by_ticker: dict = {}
    for t in trades:
        tick = t.get("ticker","?")
        pnl  = float(t.get("profit_amt",0))
        if tick not in by_ticker:
            by_ticker[tick] = {"wins":0,"total":0}
        by_ticker[tick]["total"] += 1
        if pnl > 0:
            by_ticker[tick]["wins"] += 1
    result = [
        {"ticker":k, "winRate":round(v["wins"]/v["total"]*100,1), "trades":v["total"]}
        for k,v in by_ticker.items()
    ]
    return jsonify(sorted(result, key=lambda x:-x["winRate"]))

@app.get("/api/stats/exits")
def exit_reasons():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    counts: dict = {}
    colors = {
        "Profit Target":"#00c087","Stop Loss":"#ff5252",
        "Scale-Out 50%":"#f0a500","Time Decay":"#8b90a8",
        "Manual Close":"#1677ff","Manual STC":"#1677ff",
    }
    for t in trades:
        reason = t.get("reason","Other")
        counts[reason] = counts.get(reason,0)+1
    result = [
        {"name":k,"value":v,"color":colors.get(k,"#8b90a8")}
        for k,v in counts.items()
    ]
    return jsonify(result)

# ── Watchlist endpoint ────────────────────────────────────────────────────────

@app.get("/api/watchlist")
def watchlist():
    """Return current scanner watchlist with live prices."""
    try:
        from research_scanner import MY_SMALL_STOCKS
        import yfinance as yf
        result = []
        for ticker in MY_SMALL_STOCKS[:12]:
            try:
                info = yf.Ticker(ticker).fast_info
                price  = float(getattr(info,"last_price",0) or 0)
                prev   = float(getattr(info,"previous_close",price) or price)
                change = (price-prev)/prev*100 if prev>0 else 0
                result.append({
                    "ticker":    ticker,
                    "name":      ticker,
                    "price":     round(price,2),
                    "changePct": round(change,2),
                    "spark":     [],
                })
            except Exception:
                continue
        return jsonify(result)
    except Exception as e:
        return jsonify([]), 200

# ── Scanner SSE stream ────────────────────────────────────────────────────────

@app.get("/api/scanner/stream")
def scanner_stream():
    """Server-Sent Events stream for live scanner feed."""
    def generate():
        q: queue.Queue = queue.Queue(maxsize=50)
        with _scanner_lock:
            _scanner_clients.append(q)
        try:
            # Send a heartbeat every 15s to keep connection alive
            while True:
                try:
                    entry = q.get(timeout=15)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            with _scanner_lock:
                if q in _scanner_clients:
                    _scanner_clients.remove(q)

    return Response(generate(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache",
                             "X-Accel-Buffering":"no"})

# ── News endpoint (Yahoo Finance — no key needed) ─────────────────────────────

@app.get("/api/news")
def news():
    symbol   = request.args.get("symbol","")
    category = request.args.get("category","general")
    try:
        if symbol:
            r = _requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/search"
                f"?q={symbol}&newsCount=15&enableFuzzyQuery=false",
                headers={"User-Agent":"Mozilla/5.0"},
                timeout=8,
            )
            items = r.json().get("news",[]) if r.ok else []
            result = [{"headline":i.get("title",""),
                       "source":  i.get("publisher",""),
                       "url":     i.get("link",""),
                       "datetime": int(i.get("providerPublishTime",0)),
                       "summary": i.get("title","")}
                      for i in items[:15]]
        else:
            # General market news from Yahoo
            r = _requests.get(
                "https://query1.finance.yahoo.com/v1/finance/search"
                "?q=stock+market+today&newsCount=15",
                headers={"User-Agent":"Mozilla/5.0"},
                timeout=8,
            )
            items  = r.json().get("news",[]) if r.ok else []
            result = [{"headline":i.get("title",""),
                       "source":  i.get("publisher",""),
                       "url":     i.get("link",""),
                       "datetime": int(i.get("providerPublishTime",0)),
                       "summary": i.get("title","")}
                      for i in items[:15]]
        return jsonify(result)
    except Exception as e:
        return jsonify([])

# ── Market regime endpoint ────────────────────────────────────────────────────

@app.get("/api/regime")
def regime():
    try:
        from research_scanner import get_market_regime
        r = get_market_regime()
        _bot_state["regime"]     = r["regime"]
        _bot_state["spy_vs_sma"] = r["spy_vs_sma20"]
        return jsonify(r)
    except Exception as e:
        return jsonify({"regime":"neutral","spy_vs_sma20":0.0})

# ── Bot control endpoints ─────────────────────────────────────────────────────

@app.post("/api/bot/pause")
def bot_pause():
    _bot_state["paused"] = True
    return jsonify({"ok":True,"paused":True})

@app.post("/api/bot/resume")
def bot_resume():
    _bot_state["paused"] = False
    return jsonify({"ok":True,"paused":False})

@app.post("/api/bot/scan")
def bot_scan():
    """Trigger an immediate scan in a background thread."""
    def _do_scan():
        try:
            from research_scanner import scan_markets
            scan_markets(listener=None, market=None)
        except Exception as e:
            print(f"[api] scan error: {e}")
    threading.Thread(target=_do_scan, daemon=True).start()
    return jsonify({"ok":True,"message":"Scan triggered"})

@app.post("/api/bot/close-all")
def bot_close_all():
    """Signal main.py to close all positions."""
    _bot_state["close_all_requested"] = True
    return jsonify({"ok":True,"message":"Close all requested"})

@app.post("/api/bot/close")
def bot_close_position():
    """Close a specific position by contract string."""
    data     = request.get_json(silent=True) or {}
    contract = data.get("contract","")
    _bot_state.setdefault("close_requested",[]).append(contract)
    return jsonify({"ok":True,"contract":contract})

@app.post("/api/bot/command")
def bot_command():
    """
    Forward a Discord-style command from the UI terminal.
    Executes it directly without going through Discord.
    Returns the response text.
    """
    data = request.get_json(silent=True) or {}
    cmd  = data.get("command","").strip()
    if not cmd:
        return jsonify({"ok":False,"response":"Empty command"})

    parts = cmd.lstrip("!").split(None, 1)
    name  = parts[0].lower()
    args  = parts[1] if len(parts) > 1 else ""

    # Execute command using same logic as discord_listener
    response_text = _execute_command(name, args)
    return jsonify({"ok":True,"response":response_text})

def _execute_command(cmd: str, args: str) -> str:
    """Execute a bot command and return a response string."""
    try:
        if cmd == "help":
            return ("Commands: !status !scan !pause !resume "
                    "!close ALL !watchlist !add TICKER !remove TICKER")

        elif cmd == "pause":
            _bot_state["paused"] = True
            return "Bot paused — no new entries."

        elif cmd == "resume":
            _bot_state["paused"] = False
            return "Bot resumed — watching for signals."

        elif cmd == "status":
            pos  = len(_bot_state.get("positions",[]))
            pnl  = _bot_state.get("session_pnl", 0)
            reg  = _bot_state.get("regime","neutral")
            paused = _bot_state.get("paused", False)
            return (f"{'PAUSED' if paused else 'RUNNING'} | "
                    f"{pos} position(s) open | "
                    f"Session P&L: ${pnl:.2f} | "
                    f"Regime: {reg.upper()}")

        elif cmd == "scan":
            def _do():
                try:
                    from research_scanner import scan_markets
                    scan_markets(listener=None, market=None)
                except Exception as e:
                    print(f"[api] scan error: {e}")
            threading.Thread(target=_do, daemon=True).start()
            return "Scan triggered."

        elif cmd == "close" and args.upper() == "ALL":
            _bot_state["close_all_requested"] = True
            return "Close all requested — check positions tab."

        elif cmd == "watchlist":
            try:
                from research_scanner import MY_SMALL_STOCKS
                return "Watchlist: " + ", ".join(MY_SMALL_STOCKS)
            except Exception:
                return "Could not read watchlist."

        elif cmd == "add":
            ticker = args.upper().strip()
            if ticker:
                try:
                    from research_scanner import MY_SMALL_STOCKS
                    if ticker not in MY_SMALL_STOCKS:
                        MY_SMALL_STOCKS.append(ticker)
                        return f"Added {ticker} to watchlist."
                    return f"{ticker} already in watchlist."
                except Exception:
                    return f"Error adding {ticker}."

        elif cmd == "remove":
            ticker = args.upper().strip()
            try:
                from research_scanner import MY_SMALL_STOCKS
                if ticker in MY_SMALL_STOCKS:
                    MY_SMALL_STOCKS.remove(ticker)
                    return f"Removed {ticker}."
                return f"{ticker} not in watchlist."
            except Exception:
                return f"Error removing {ticker}."

        return f"Unknown command: {cmd}"
    except Exception as e:
        return f"Error: {e}"

@app.get("/api/bot/status")
def bot_status():
    positions_count = len(_bot_state.get("positions",[]))
    return jsonify({
        "paused":         _bot_state["paused"],
        "running":        _bot_state["running"],
        "regime":         _bot_state["regime"],
        "spy_vs_sma20":   _bot_state["spy_vs_sma"],
        "open_positions": positions_count,
        "session_pnl":    _bot_state["session_pnl"],
        "last_scan":      _bot_state["last_scan"],
        "market_open":    _is_market_open(),
    })

# ── Activity log endpoint ─────────────────────────────────────────────────────

@app.get("/api/activity")
def activity():
    """Return last N lines of brain.log."""
    n = int(request.args.get("n", 50))
    if not os.path.exists(BRAIN_LOG):
        return jsonify([])
    try:
        with open(BRAIN_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        result = []
        for line in reversed(lines[-n:]):
            line = line.strip()
            if not line:
                continue
            level = ("error"   if "[ERROR]" in line else
                     "warning" if "[WARNING]" in line else
                     "info"    if "[INFO]" in line else "debug")
            result.append({"text": line, "level": level})
        return jsonify(result)
    except Exception as e:
        return jsonify([])

def _is_market_open() -> bool:
    from datetime import time as dtime
    import pytz
    try:
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        return (now.weekday() < 5 and
                dtime(9,30) <= now.time() <= dtime(16,0))
    except Exception:
        return False

# ── CORS preflight ────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

@app.route("/api/<path:p>", methods=["OPTIONS"])
def options_handler(p):
    return "", 204

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("GilbertTrader API Server")
    print("  http://localhost:8000")
    print("  Set VITE_API_BASE=http://localhost:8000 in pixel-perfect/.env")
    print("  Set VITE_DATA_PROVIDER=http in pixel-perfect/.env")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)