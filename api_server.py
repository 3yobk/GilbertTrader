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

app = Flask(__name__)
CORS(app,
     origins="*",
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"])

def _is_paper_mode() -> bool:
    """Re-reads .env on every call so toggling takes effect without restart."""
    load_dotenv(override=True)
    return os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true"

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
    "paused":             False,
    "running":            True,
    "regime":             "neutral",
    "spy_vs_sma":         0.0,
    "positions":          [],
    "session_pnl":        0.0,
    "last_scan":          None,
    "consecutive_losses": 0,
    "consecutive_wins":   0,
    "tp_pct":             30.0,
    "sl_pct":             20.0,
    "webull_engine":      None,
}

def update_bot_state(**kwargs):
    """Called by main.py to keep UI in sync."""
    _bot_state.update(kwargs)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_trades(days: int = 1) -> list[dict]:
    if not os.path.exists(TRADE_LOG):
        return []
    try:
        mode   = "paper" if _is_paper_mode() else "live"
        trades = []
        with open(TRADE_LOG, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Filter by mode — old rows without mode column default to live
                if row.get("mode", "live") != mode:
                    continue
                trades.append(row)
        if days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            trades = [t for t in trades
                      if _parse_dt(t.get("date", "")) >= cutoff]
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

import sqlite3

HISTORY_DB = os.path.join(os.path.dirname(__file__), "trade_history.db")

def _get_db():
    """Get SQLite connection, create tables if needed."""
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id    TEXT PRIMARY KEY,
        date        TEXT,
        ticker      TEXT,
        strike      REAL,
        option_type TEXT,
        expiry      TEXT,
        action      TEXT,
        quantity    INTEGER,
        avg_price   REAL,
        total_cost  REAL,
        source      TEXT DEFAULT "webull",
        bot_placed  INTEGER DEFAULT 0,
        mode        TEXT DEFAULT "live"
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS account_snapshots (
        ts           INTEGER PRIMARY KEY,
        date         TEXT,
        net_value    REAL,
        buying_power REAL,
        day_pnl      REAL,
        mode         TEXT DEFAULT "live"
    )''')
    conn.commit()
    return conn

def _sync_webull_history():
    """
    Sync trade history from trade_log.csv into DB.
    This is reliable since main.py writes the CSV directly.
    """
    if not os.path.exists(TRADE_LOG):
        return
    try:
        mode = "paper" if _is_paper_mode() else "live"
        conn = _get_db()
        c    = conn.cursor()
        with open(TRADE_LOG, newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                if row.get("mode", "live") != mode:
                    continue
                # Use row index + ticker as synthetic order_id
                order_id = f"csv_{i}_{row.get('ticker','')}_{row.get('date','')}"
                try:
                    c.execute('''INSERT OR IGNORE INTO orders
                        (order_id, date, ticker, strike, option_type, expiry,
                         action, quantity, avg_price, total_cost, source, bot_placed, mode)
                        VALUES (?,?,?,?,?,?,?,?,?,?,"csv",1,?)''',
                              (order_id,
                               row.get("date", ""),
                               row.get("ticker", "?"),
                               float(row.get("strike", 0) or 0),
                               row.get("option_type", "C"),
                               row.get("expiry", ""),
                               "SELL",
                               int(row.get("quantity", 1) or 1),
                               float(row.get("exit_price", 0) or 0),
                               float(row.get("profit_amt", 0) or 0),
                               mode))
                except Exception:
                    continue
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("CSV sync error: %s", e)

def _save_account_snapshot():
    """Save current account value to DB for historical chart."""
    cache_file = os.path.join(os.path.dirname(__file__), "account_cache.json")
    if not os.path.exists(cache_file):
        return
    try:
        import json as _json
        with open(cache_file, encoding="utf-8") as f:
            acc = _json.load(f)
        mode = "paper" if _is_paper_mode() else "live"
        if acc.get("paper") != (mode == "paper"):
            return
        net = float(acc.get("net_value", 0))
        if not net:
            return
        conn = _get_db()
        c    = conn.cursor()
        now  = int(time.time())
        bucket = now - (now % 60)  # 1-minute buckets
        c.execute('''INSERT OR REPLACE INTO account_snapshots
            (ts, date, net_value, buying_power, day_pnl, mode)
            VALUES (?,?,?,?,?,?)''',
                  (bucket,
                   datetime.now().strftime("%Y-%m-%d %H:%M"),
                   net,
                   float(acc.get("buying_power", 0)),
                   float(acc.get("day_pnl", 0) or 0),
                   mode))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Snapshot save error: %s", e)

def _build_series(trades: list[dict], range_str: str = "1D") -> list[dict]:
    """
    Build P&L series for the chart.
    1D:  intraday cumulative P&L with timestamps (shows dips and rises)
    1W+: day-by-day cumulative P&L
    """
    import pytz
    ET     = pytz.timezone("America/New_York")
    now_et = datetime.now(ET)

    if not trades:
        if range_str == "1D":
            open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            return [{"t": "9:30", "ts": int(open_t.timestamp() * 1000), "v": 0}]
        return []

    sorted_t = sorted(trades, key=lambda x: x.get("date", ""))

    if range_str == "1D":
        # Intraday: show each trade as a point on the curve
        open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        series = [{"t": "9:30", "ts": int(open_t.timestamp() * 1000), "v": 0}]
        cumulative = 0.0
        for t in sorted_t:
            try:
                cumulative += float(t.get("profit_amt") or 0)
            except Exception:
                pass
            dt = _parse_dt(t.get("date", ""))
            series.append({
                "t":  dt.strftime("%H:%M") if dt != datetime.min else "?",
                "ts": int(dt.timestamp() * 1000),
                "v":  round(cumulative, 2),
            })
        return series

    else:
        # Multi-day: one point per day showing cumulative P&L
        from collections import defaultdict, OrderedDict
        by_day = OrderedDict()
        for t in sorted_t:
            day = t.get("date", "")[:10]
            if not day:
                continue
            if day not in by_day:
                by_day[day] = 0.0
            try:
                by_day[day] += float(t.get("profit_amt") or 0)
            except Exception:
                pass

        series = []
        cumulative = 0.0
        for day, pnl in by_day.items():
            cumulative += pnl
            dt = _parse_dt(day)
            series.append({
                "t":  day,
                "ts": int(dt.timestamp() * 1000) if dt != datetime.min else 0,
                "v":  round(cumulative, 2),
            })
        return series


# ── Portfolio endpoints ───────────────────────────────────────────────────────

@app.get("/api/portfolio/summary")
def portfolio_summary():
    r          = request.args.get("range", "1D")
    trades     = _read_trades(_range_days(r))

    # Save account snapshot and sync Webull history in background
    threading.Thread(target=_save_account_snapshot, daemon=True).start()
    threading.Thread(target=_sync_webull_history, daemon=True).start()

    net_value    = None
    buying_power = None
    market_value = 0.0
    cached_day_pnl = None   # real-time day P&L from Webull

    cache_file = os.path.join(os.path.dirname(__file__), "account_cache.json")
    if os.path.exists(cache_file):
        try:
            import json as _json
            with open(cache_file, encoding="utf-8") as f:
                acc = _json.load(f)
            if acc.get("paper") == _is_paper_mode():
                net_value      = float(acc.get("net_value", 0)) or None
                buying_power   = float(acc.get("buying_power", 0)) or None
                cached_day_pnl = float(acc.get("day_pnl", 0) or 0)
                for pos in acc.get("positions", []):
                    market_value += float(pos.get("marketValue", 0) or 0)
        except Exception:
            pass

    if not buying_power:
        buying_power = 100000.0 if _is_paper_mode() else 0.0
    if not net_value:
        net_value = buying_power

    # Use Webull's real-time day P&L if available, else fall back to trade_log sum
    if cached_day_pnl is not None and cached_day_pnl != 0:
        day_change = cached_day_pnl
    else:
        day_change = sum(float(t.get("profit_amt") or 0) for t in trades)

    return jsonify({
        "value":        round(net_value, 2),
        "dayChange":    round(day_change, 2),
        "dayChangePct": round(day_change / buying_power * 100, 3) if buying_power else 0,
        "buyingPower":  round(buying_power, 2),
        "cash":         round(buying_power, 2),
        "marketValue":  round(market_value, 2),
        "paper_mode":   _is_paper_mode(),
    })

@app.get("/api/portfolio/series")
def portfolio_series():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    series = _build_series(trades, range_str=r)

    # For 1D append live day_pnl point if available
    if r == "1D":
        cache_file = os.path.join(os.path.dirname(__file__), "account_cache.json")
        if os.path.exists(cache_file):
            try:
                import json as _json
                with open(cache_file, encoding="utf-8") as f:
                    acc = _json.load(f)
                day_pnl = float(acc.get("day_pnl", 0) or 0)
                if day_pnl != 0:
                    series.append({
                        "t":  datetime.now().strftime("%H:%M"),
                        "ts": int(time.time() * 1000),
                        "v":  round(day_pnl, 2),
                    })
            except Exception:
                pass

    return jsonify(series)

# ── Positions endpoint ────────────────────────────────────────────────────────

@app.get("/api/positions")
def positions():
    """Merge bot-managed positions with real Webull positions from account_cache.json."""
    result = []
    seen   = set()
    tp     = _bot_state.get("tp_pct", 30)
    sl     = _bot_state.get("sl_pct", 20)
    paper  = _is_paper_mode()

    # Source 1: bot-managed positions
    for p in _bot_state.get("positions", []):
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
            seen.add(ticker)
            if pnl_pct >= tp * 0.8:    status = "Near TP"
            elif pnl_pct <= -sl * 0.8: status = "Near SL"
            elif pnl_pct >= tp * 0.5:  status = "Scaling Out"
            else:                       status = "Monitoring"
            result.append({
                "contract": f"{ticker} {strike}{opt}",
                "side":     "CALL" if opt == "C" else "PUT",
                "entry":    entry, "current": current,
                "pnlPct":   round(pnl_pct, 2), "peakPct": round(peak, 2),
                "size":     qty, "expiry": expiry, "status": status,
            })
        except Exception:
            continue

    # Source 2: real Webull positions from account_cache.json
    cache_file = os.path.join(os.path.dirname(__file__), "account_cache.json")
    if os.path.exists(cache_file):
        try:
            import json as _json
            with open(cache_file, encoding="utf-8") as f:
                acc = _json.load(f)
            if acc.get("paper") == paper:
                for pos in acc.get("positions", []):
                    try:
                        tinfo   = pos.get("ticker", {})
                        ticker  = tinfo.get("symbol", "?")
                        name    = tinfo.get("name", ticker)
                        qty     = int(pos.get("quantity", 0))
                        entry   = float(pos.get("costPrice", pos.get("avgCost", 0)) or 0)
                        current = float(pos.get("lastPrice", entry) or entry)
                        unr     = float(pos.get("unrealizedProfitLoss", 0) or 0)
                        mkt     = float(pos.get("marketValue", 0) or 0)
                        pnl_pct = (unr / (entry * qty * 100) * 100) if entry and qty else 0
                        if ticker in seen:
                            continue
                        if pnl_pct >= tp * 0.8:    status = "Near TP"
                        elif pnl_pct <= -sl * 0.8: status = "Near SL"
                        else:                       status = "Manual"
                        result.append({
                            "contract":  name or ticker,
                            "side":      "CALL" if "Call" in name else "PUT" if "Put" in name else "?",
                            "entry":     round(entry, 4), "current": round(current, 4),
                            "pnlPct":    round(pnl_pct, 2), "peakPct": round(pnl_pct, 2),
                            "size":      qty, "expiry": "",
                            "unrealPnl": round(unr, 2), "mktValue": round(mkt, 2),
                            "status":    status,
                        })
                    except Exception:
                        continue
        except Exception:
            pass

    return jsonify(result)

# ── Trades endpoint ───────────────────────────────────────────────────────────

@app.get("/api/trades/recent")
def recent_trades():
    r    = request.args.get("range", "1D")
    days = _range_days(r)
    result = []

    # Primary source: trade_log.csv (has entry price, P&L, correct dates)
    csv_trades = _read_trades(days)
    for t in reversed(csv_trades):
        try:
            pnl = float(t.get("profit_amt") or 0)
            result.append({
                "time":   _parse_dt(t.get("date", "")).strftime("%H:%M"),
                "date":   t.get("date", "")[:10],
                "ticker": t.get("ticker", "?"),
                "strike": t.get("strike", "?"),
                "expiry": t.get("expiry", "?"),
                "side":   "CALL" if t.get("option_type", "C") == "C" else "PUT",
                "entry":  float(t.get("entry_price", 0)),
                "exit":   float(t.get("exit_price", 0)),
                "pnl":    round(pnl, 2),
                "reason": t.get("reason", "Bot"),
                "source": "bot",
            })
        except Exception:
            continue

    return jsonify(result[:100])

# ── Stats endpoints ───────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    r      = request.args.get("range", "1D")
    days   = _range_days(r)
    mode   = "paper" if _is_paper_mode() else "live"
    trades = _read_trades(days)

    # Supplement with DB orders that have P&L from Webull history
    pnls = [float(t.get("profit_amt") or 0) for t in trades]

    # Also count total trades from DB (includes manual trades)
    db_total = len(trades)
    try:
        conn   = _get_db()
        c      = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        c.execute('''SELECT COUNT(*) as cnt FROM orders
                     WHERE mode=? AND date>=? AND action="SELL"''', (mode, cutoff))
        row = c.fetchone()
        conn.close()
        db_total = max(db_total, row["cnt"] if row else 0)
    except Exception:
        pass

    if not pnls:
        return jsonify({"totalTrades": db_total, "winRate": 0, "avgWin": 0,
                        "avgLoss": 0, "expectancy": 0, "maxDrawdown": 0,
                        "sharpe": 0, "totalPnl": 0})

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr     = len(wins) / len(pnls) * 100 if pnls else 0
    aw     = sum(wins) / len(wins)       if wins   else 0
    al     = sum(losses) / len(losses)   if losses else 0
    exp    = (wr / 100 * aw) + ((1 - wr / 100) * al)

    cumulative = 0.0
    peak       = 0.0
    max_dd     = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    import statistics
    std    = statistics.stdev(pnls) if len(pnls) > 1 else 1
    sharpe = round((sum(pnls) / len(pnls)) / std, 2) if std > 0 else 0

    return jsonify({
        "totalTrades": db_total,
        "winRate":     round(wr, 1),
        "avgWin":      round(aw, 2),
        "avgLoss":     round(al, 2),
        "expectancy":  round(exp, 2),
        "maxDrawdown": round(max_dd, 2),
        "sharpe":      sharpe,
        "totalPnl":    round(sum(pnls), 2),
    })

@app.get("/api/stats/winrate")
def winrate_by_ticker():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    by_ticker: dict = {}
    for t in trades:
        tick = t.get("ticker", "?")
        pnl  = float(t.get("profit_amt") or 0)
        if tick not in by_ticker:
            by_ticker[tick] = {"wins": 0, "total": 0}
        by_ticker[tick]["total"] += 1
        if pnl > 0:
            by_ticker[tick]["wins"] += 1
    result = [
        {"ticker": k, "winRate": round(v["wins"] / v["total"] * 100, 1), "trades": v["total"]}
        for k, v in by_ticker.items()
    ]
    return jsonify(sorted(result, key=lambda x: -x["winRate"]))

@app.get("/api/stats/exits")
def exit_reasons():
    r      = request.args.get("range", "1D")
    trades = _read_trades(_range_days(r))
    counts: dict = {}
    colors = {
        "Profit Target": "#00c087", "Stop Loss": "#ff5252",
        "Scale-Out 50%": "#f0a500", "Time Decay": "#8b90a8",
        "Manual Close":  "#1677ff", "Manual STC": "#1677ff",
    }
    for t in trades:
        reason = t.get("reason", "Other")
        counts[reason] = counts.get(reason, 0) + 1
    result = [
        {"name": k, "value": v, "color": colors.get(k, "#8b90a8")}
        for k, v in counts.items()
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
                info   = yf.Ticker(ticker).fast_info
                price  = float(getattr(info, "last_price", 0) or 0)
                prev   = float(getattr(info, "previous_close", price) or price)
                change = (price - prev) / prev * 100 if prev > 0 else 0
                result.append({
                    "ticker":    ticker,
                    "name":      ticker,
                    "price":     round(price, 2),
                    "changePct": round(change, 2),
                    "spark":     [],
                })
            except Exception:
                continue
        return jsonify(result)
    except Exception:
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
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

# ── News endpoint (Yahoo Finance — no key needed) ─────────────────────────────

@app.get("/api/news")
def news():
    symbol = request.args.get("symbol", "")
    try:
        if symbol:
            r = _requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/search"
                f"?q={symbol}&newsCount=15&enableFuzzyQuery=false",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            items = r.json().get("news", []) if r.ok else []
        else:
            r = _requests.get(
                "https://query1.finance.yahoo.com/v1/finance/search"
                "?q=stock+market+today&newsCount=15",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            items = r.json().get("news", []) if r.ok else []
        result = [{"headline":  i.get("title", ""),
                   "source":    i.get("publisher", ""),
                   "url":       i.get("link", ""),
                   "datetime":  int(i.get("providerPublishTime", 0)),
                   "summary":   i.get("title", "")}
                  for i in items[:15]]
        return jsonify(result)
    except Exception:
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
    except Exception:
        return jsonify({"regime": "neutral", "spy_vs_sma20": 0.0})

# ── Bot control endpoints ─────────────────────────────────────────────────────

@app.post("/api/bot/pause")
def bot_pause():
    _bot_state["paused"] = True
    return jsonify({"ok": True, "paused": True})

@app.post("/api/bot/resume")
def bot_resume():
    _bot_state["paused"] = False
    return jsonify({"ok": True, "paused": False})

@app.post("/api/bot/scan")
def bot_scan():
    def _do_scan():
        try:
            from research_scanner import scan_markets
            scan_markets(listener=None, market=None)
        except Exception as e:
            print(f"[api] scan error: {e}")
    threading.Thread(target=_do_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan triggered"})

@app.post("/api/bot/close-all")
def bot_close_all():
    _bot_state["close_all_requested"] = True
    return jsonify({"ok": True, "message": "Close all requested"})

@app.post("/api/bot/close")
def bot_close_position():
    data     = request.get_json(silent=True) or {}
    contract = data.get("contract", "")
    _bot_state.setdefault("close_requested", []).append(contract)
    return jsonify({"ok": True, "contract": contract})

@app.post("/api/bot/command")
def bot_command():
    data = request.get_json(silent=True) or {}
    cmd  = data.get("command", "").strip()
    if not cmd:
        return jsonify({"ok": False, "response": "Empty command"})
    parts = cmd.lstrip("!").split(None, 1)
    name  = parts[0].lower()
    args  = parts[1] if len(parts) > 1 else ""
    response_text = _execute_command(name, args)
    return jsonify({"ok": True, "response": response_text})

def _execute_command(cmd: str, args: str) -> str:
    try:
        if cmd == "help":
            return ("**Commands:** !status !scan !pause !resume !close ALL !profile !watchlist !add !remove | **Limiters:** !sl off/on/<pct> !tp off/on/<pct> !scaleout off/on !limiters off/on !settings")
        elif cmd == "pause":
            _bot_state["paused"] = True
            return "Bot paused — no new entries."
        elif cmd == "resume":
            _bot_state["paused"] = False
            return "Bot resumed — watching for signals."
        elif cmd == "status":
            pos    = len(_bot_state.get("positions", []))
            pnl    = _bot_state.get("session_pnl", 0)
            reg    = _bot_state.get("regime", "neutral")
            paused = _bot_state.get("paused", False)
            mode   = "PAPER" if _is_paper_mode() else "LIVE"
            return (f"{'PAUSED' if paused else 'RUNNING'} | {mode} | "
                    f"{pos} position(s) open | Session P&L: ${pnl:.2f} | Regime: {reg.upper()}")
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
            from research_scanner import MY_SMALL_STOCKS
            return "Watchlist: " + ", ".join(MY_SMALL_STOCKS)
        elif cmd == "add":
            ticker = args.upper().strip()
            if ticker:
                from research_scanner import MY_SMALL_STOCKS
                if ticker not in MY_SMALL_STOCKS:
                    MY_SMALL_STOCKS.append(ticker)
                    return f"Added {ticker} to watchlist."
                return f"{ticker} already in watchlist."
        elif cmd == "remove":
            ticker = args.upper().strip()
            from research_scanner import MY_SMALL_STOCKS
            if ticker in MY_SMALL_STOCKS:
                MY_SMALL_STOCKS.remove(ticker)
                return f"Removed {ticker}."
            return f"{ticker} not in watchlist."
        return f"Unknown command: {cmd}"
    except Exception as e:
        return f"Error: {e}"

@app.get("/api/bot/status")
def bot_status():
    positions_count = len(_bot_state.get("positions", []))
    losses = _bot_state.get("consecutive_losses", 0)
    wins   = _bot_state.get("consecutive_wins", 0)
    if losses >= 1:
        streak = {"type": "loss", "count": losses}
    elif wins >= 1:
        streak = {"type": "win", "count": wins}
    else:
        streak = {"type": "none", "count": 0}
    return jsonify({
        "paused":         _bot_state["paused"],
        "running":        _bot_state["running"],
        "regime":         _bot_state["regime"],
        "spy_vs_sma20":   _bot_state["spy_vs_sma"],
        "open_positions": positions_count,
        "session_pnl":    _bot_state["session_pnl"],
        "last_scan":      _bot_state["last_scan"],
        "market_open":    _is_market_open(),
        "paper_mode":     _is_paper_mode(),
        "streak":         streak,
    })

# ── Activity log endpoint ─────────────────────────────────────────────────────

@app.get("/api/debug/history")
def debug_history():
    """Debug endpoint — shows what's in trade_history.db."""
    try:
        conn = _get_db()
        c    = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM account_snapshots")
        snap_count = c.fetchone()["cnt"]
        c.execute("SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 5")
        snaps = [dict(r) for r in c.fetchall()]
        c.execute("SELECT COUNT(*) as cnt FROM orders")
        order_count = c.fetchone()["cnt"]
        c.execute("SELECT * FROM orders ORDER BY date DESC LIMIT 5")
        orders = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({
            "snapshots": snap_count,
            "latest_snapshots": snaps,
            "orders": order_count,
            "latest_orders": orders,
            "cache_exists": os.path.exists(
                os.path.join(os.path.dirname(__file__), "account_cache.json")),
            "tradelog_exists": os.path.exists(TRADE_LOG),
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.get("/api/activity")
def activity():
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
    except Exception:
        return jsonify([])

def _is_market_open() -> bool:
    from datetime import time as dtime
    import pytz
    try:
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        return (now.weekday() < 5 and
                dtime(9, 30) <= now.time() <= dtime(16, 0))
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
    # Background thread: save account snapshot every 5 minutes
    def _snapshot_loop():
        while True:
            try:
                _save_account_snapshot()
                _sync_webull_history()
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=_snapshot_loop, daemon=True).start()

    print("GilbertTrader API Server")
    print("  Listening on all interfaces for Tailscale connectivity...")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)