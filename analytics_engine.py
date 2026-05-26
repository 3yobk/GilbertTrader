"""
analytics_engine.py — GilbertTrader Statistical Validation Engine (Phase 1)
============================================================================
Records rich context on every trade and generates:
  - expectancy, win rate, profit factor, Sharpe-like score
  - regime/VIX/RSI/time-of-day performance breakdowns
  - edge decay detection (rolling 20 vs rolling 100)
  - ticker ranking by expectancy
  - daily summary dict for Discord report

Usage:
    from analytics_engine import AnalyticsEngine
    analytics = AnalyticsEngine()

    # At signal fire time — record context
    analytics.record_signal_context(ticker, regime, vix, rsi_5m, rsi_1h,
                                     iv_rank, flow_score, news_score,
                                     spread_pct, conviction_score)

    # At trade close time — complete the record
    analytics.record_trade_close(ticker, strike, option_type, expiry,
                                  entry_time, exit_time, entry_price,
                                  exit_price, quantity, reason, regime,
                                  peak_pnl_pct, max_dd_pct,
                                  scale_out_triggered, smart_stop_delayed,
                                  paper)
"""

import os
import csv
import json
import math
import sqlite3
import logging
import threading
from datetime import datetime, date
from typing import Optional
from contextlib import contextmanager

log = logging.getLogger("analytics")

DB_PATH  = os.path.join(os.path.dirname(__file__), "trade_history.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "trade_log_rich.csv")


# ── Schema ─────────────────────────────────────────────────────────────────────
_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    date                  TEXT,
    ticker                TEXT,
    strike                REAL,
    option_type           TEXT,
    expiry                TEXT,
    side                  TEXT,
    entry_time            TEXT,
    exit_time             TEXT,
    hold_minutes          REAL,
    entry_premium         REAL,
    exit_premium          REAL,
    slippage_pct          REAL,
    realized_pnl          REAL,
    realized_pnl_pct      REAL,
    peak_pnl_pct          REAL,
    max_drawdown_pct      REAL,
    regime                TEXT,
    spy_change_pct        REAL,
    vix_at_entry          REAL,
    rsi_5m                REAL,
    rsi_1h                REAL,
    iv_rank               REAL,
    flow_score            REAL,
    spread_pct            REAL,
    news_score            REAL,
    conviction_score      REAL,
    gates_passed          TEXT,
    scale_out_triggered   INTEGER,
    smart_stop_delayed    INTEGER,
    exit_reason           TEXT,
    quantity              INTEGER,
    mode                  TEXT
);
"""

_CREATE_SCANNER = """
CREATE TABLE IF NOT EXISTS scanner_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT,
    ticker           TEXT,
    direction        TEXT,
    rsi_5m           REAL,
    rsi_1h           REAL,
    iv_rank          REAL,
    flow_score       REAL,
    spread_pct       REAL,
    news_score       REAL,
    conviction_score REAL,
    signal_fired     INTEGER,
    regime           TEXT
);
"""

_CREATE_EDGE = """
CREATE TABLE IF NOT EXISTS edge_metrics (
    date            TEXT PRIMARY KEY,
    expectancy      REAL,
    win_rate        REAL,
    profit_factor   REAL,
    sharpe          REAL,
    trades_count    INTEGER,
    best_ticker     TEXT,
    worst_ticker    TEXT,
    regime_bull_wr  REAL,
    regime_bear_wr  REAL,
    vix_low_wr      REAL,
    vix_high_wr     REAL,
    edge_decay      INTEGER
);
"""

_CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts               TEXT PRIMARY KEY,
    net_value        REAL,
    settled_cash     REAL,
    deployed_capital REAL,
    open_positions   INTEGER,
    session_pnl      REAL,
    regime           TEXT,
    vix              REAL
);
"""

# ── CSV columns (rich version, separate from existing trade_log.csv) ───────────
_CSV_FIELDS = [
    "date", "ticker", "strike", "option_type", "expiry", "side",
    "entry_time", "exit_time", "hold_minutes",
    "entry_premium", "exit_premium", "slippage_pct",
    "realized_pnl", "realized_pnl_pct", "peak_pnl_pct", "max_drawdown_pct",
    "regime", "spy_change_pct", "vix_at_entry",
    "rsi_5m", "rsi_1h", "iv_rank", "flow_score",
    "spread_pct", "news_score", "conviction_score",
    "scale_out_triggered", "smart_stop_delayed",
    "exit_reason", "quantity", "mode",
]


class AnalyticsEngine:
    """
    Thread-safe analytics recorder and stats generator.
    Writes every trade to SQLite (primary) and CSV (backup/dashboard).
    """

    def __init__(self, db_path: str = DB_PATH, csv_path: str = CSV_PATH):
        self.db_path  = db_path
        self.csv_path = csv_path
        self._lock    = threading.Lock()
        self._signal_ctx: dict = {}   # ticker -> context dict (set at signal time)
        self._init_db()

    # ── DB init ───────────────────────────────────────────────────────────────

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(_CREATE_TRADES)
            conn.execute(_CREATE_SCANNER)
            conn.execute(_CREATE_EDGE)
            conn.execute(_CREATE_SNAPSHOTS)
            conn.commit()

    @contextmanager
    def _conn(self):
        # For :memory: databases use a persistent connection (shared across calls)
        if self.db_path == ":memory:":
            if not hasattr(self, "_mem_conn") or self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            yield self._mem_conn
        else:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    # ── Record signal context (call from scanner before trade fires) ──────────

    def record_signal_context(self, ticker: str, direction: str,
                               regime: str, vix: float,
                               rsi_5m: float, rsi_1h: float,
                               iv_rank: float, flow_score: float,
                               news_score: float, spread_pct: float,
                               conviction_score: float,
                               signal_fired: bool = True):
        """
        Store scanner context at signal time.
        Called from research_scanner.py when a signal passes all gates.
        """
        ctx = {
            "direction": direction, "regime": regime, "vix": vix,
            "rsi_5m": rsi_5m, "rsi_1h": rsi_1h, "iv_rank": iv_rank,
            "flow_score": flow_score, "news_score": news_score,
            "spread_pct": spread_pct, "conviction_score": conviction_score,
            "signal_fired": signal_fired,
        }
        with self._lock:
            self._signal_ctx[ticker.upper()] = ctx

        # Also log to scanner_decisions table
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO scanner_decisions
                    (ts, ticker, direction, rsi_5m, rsi_1h, iv_rank,
                     flow_score, spread_pct, news_score, conviction_score,
                     signal_fired, regime)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.now().isoformat(), ticker.upper(), direction,
                    rsi_5m, rsi_1h, iv_rank, flow_score, spread_pct,
                    news_score, conviction_score, int(signal_fired), regime,
                ))
                conn.commit()
        except Exception as e:
            log.debug("scanner_decisions insert error: %s", e)

    # ── Record trade close ────────────────────────────────────────────────────

    def record_trade_close(self,
                           ticker: str, strike: float, option_type: str,
                           expiry: str, entry_time: str, exit_time: str,
                           entry_price: float, exit_price: float,
                           quantity: int, exit_reason: str,
                           regime: str,
                           peak_pnl_pct: float = 0.0,
                           max_dd_pct: float = 0.0,
                           scale_out_triggered: bool = False,
                           smart_stop_delayed: bool = False,
                           paper: bool = True,
                           spy_change_pct: float = 0.0):
        """
        Complete a trade record and write to DB + CSV.
        Call from trade_engine.py inside log_trade().
        """
        ticker = ticker.upper()

        # Retrieve scanner context saved at signal time
        with self._lock:
            ctx = self._signal_ctx.pop(ticker, {})

        # Compute derived fields
        realized_pnl     = (exit_price - entry_price) * 100 * quantity
        realized_pnl_pct = ((exit_price - entry_price) / entry_price * 100
                            if entry_price > 0 else 0.0)

        # Slippage: difference between mid-market at entry and actual fill
        # (approximated as 0 unless we track pre-fill quote separately)
        slippage_pct = 0.0

        # Hold time
        hold_minutes = 0.0
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in entry_time else "%Y-%m-%dT%H:%M:%S"
            t0  = datetime.strptime(entry_time[:26], fmt)
            fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in exit_time else "%Y-%m-%dT%H:%M:%S"
            t1  = datetime.strptime(exit_time[:26], fmt)
            hold_minutes = (t1 - t0).total_seconds() / 60
        except Exception:
            pass

        row = {
            "date":               datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ticker":             ticker,
            "strike":             strike,
            "option_type":        option_type,
            "expiry":             expiry,
            "side":               "CALL" if option_type.upper() == "C" else "PUT",
            "entry_time":         entry_time,
            "exit_time":          exit_time or datetime.now().isoformat(),
            "hold_minutes":       round(hold_minutes, 1),
            "entry_premium":      round(entry_price, 4),
            "exit_premium":       round(exit_price, 4),
            "slippage_pct":       round(slippage_pct, 2),
            "realized_pnl":       round(realized_pnl, 2),
            "realized_pnl_pct":   round(realized_pnl_pct, 2),
            "peak_pnl_pct":       round(peak_pnl_pct, 2),
            "max_drawdown_pct":   round(max_dd_pct, 2),
            "regime":             regime or ctx.get("regime", "unknown"),
            "spy_change_pct":     round(spy_change_pct, 3),
            "vix_at_entry":       ctx.get("vix", 0.0),
            "rsi_5m":             ctx.get("rsi_5m", 0.0),
            "rsi_1h":             ctx.get("rsi_1h", 0.0),
            "iv_rank":            ctx.get("iv_rank", 0.0),
            "flow_score":         ctx.get("flow_score", 0.0),
            "spread_pct":         ctx.get("spread_pct", 0.0),
            "news_score":         ctx.get("news_score", 0.0),
            "conviction_score":   ctx.get("conviction_score", 0.0),
            "gates_passed":       json.dumps(ctx.get("gates_passed", [])),
            "scale_out_triggered": int(scale_out_triggered),
            "smart_stop_delayed": int(smart_stop_delayed),
            "exit_reason":        exit_reason,
            "quantity":           quantity,
            "mode":               "paper" if paper else "live",
        }

        # Write to SQLite
        try:
            with self._conn() as conn:
                cols   = ", ".join(row.keys())
                placeh = ", ".join(["?"] * len(row))
                conn.execute(
                    f"INSERT INTO trades ({cols}) VALUES ({placeh})",
                    list(row.values())
                )
                conn.commit()
        except Exception as e:
            log.error("analytics DB write error: %s", e)

        # Write to rich CSV
        try:
            file_exists = os.path.isfile(self.csv_path)
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
                if not file_exists:
                    writer.writeheader()
                writer.writerow({k: row.get(k, "") for k in _CSV_FIELDS})
        except Exception as e:
            log.debug("analytics CSV write error: %s", e)

        log.info(
            "Trade recorded: %s %s%s P&L=$%.2f (%.1f%%) reason=%s",
            ticker, strike, option_type, realized_pnl, realized_pnl_pct, exit_reason
        )

    # ── Record account snapshot ───────────────────────────────────────────────

    def record_snapshot(self, net_value: float, settled_cash: float,
                        deployed_capital: float, open_positions: int,
                        session_pnl: float, regime: str, vix: float = 0.0):
        """Call every 5 minutes from main.py to track equity curve."""
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO account_snapshots
                    (ts, net_value, settled_cash, deployed_capital,
                     open_positions, session_pnl, regime, vix)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    datetime.now().isoformat(),
                    net_value, settled_cash, deployed_capital,
                    open_positions, session_pnl, regime, vix,
                ))
                conn.commit()
        except Exception as e:
            log.debug("snapshot write error: %s", e)

    # ── Core stats ────────────────────────────────────────────────────────────

    def _fetch_trades(self, days: int = None, n: int = None,
                      mode: str = None) -> list:
        """Fetch trades from DB, optionally filtered."""
        conditions = []
        params = []
        if days:
            cutoff = datetime.now().strftime("%Y-%m-%d")
            # approximate: last N calendar days
            conditions.append("date >= date('now', ?)")
            params.append(f"-{days} days")
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        order = "ORDER BY id DESC"
        limit = f"LIMIT {n}" if n else ""
        sql = f"SELECT * FROM trades {where} {order} {limit}"
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug("_fetch_trades error: %s", e)
            return []

    def compute_stats(self, days: int = None, n: int = None,
                      mode: str = None) -> dict:
        """
        Compute full stats over a set of trades.
        Returns dict with expectancy, win_rate, profit_factor, sharpe,
        avg_hold_min, max_drawdown, ticker_ranking, regime_ranking,
        tod_ranking (time-of-day), rsi_bucket_ranking, vix_bucket_ranking.
        """
        trades = self._fetch_trades(days=days, n=n, mode=mode)
        if not trades:
            return {"trades": 0}

        pnls      = [t["realized_pnl"] for t in trades]
        wins      = [p for p in pnls if p > 0]
        losses    = [p for p in pnls if p <= 0]
        n_total   = len(pnls)
        n_wins    = len(wins)

        win_rate      = n_wins / n_total if n_total else 0
        avg_win       = sum(wins) / len(wins) if wins else 0
        avg_loss      = sum(losses) / len(losses) if losses else 0
        expectancy    = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
        gross_wins    = sum(wins)
        gross_losses  = abs(sum(losses)) if losses else 1e-9
        profit_factor = gross_wins / gross_losses

        # Sharpe-like (daily P&L std dev)
        sharpe = 0.0
        if n_total > 2:
            mean_pnl = sum(pnls) / n_total
            std_pnl  = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / n_total)
            sharpe   = (mean_pnl / std_pnl * math.sqrt(252)) if std_pnl > 0 else 0.0

        avg_hold = (sum(t["hold_minutes"] for t in trades) / n_total
                    if n_total else 0)

        # Max drawdown from cumulative P&L curve
        cumulative = 0.0
        peak_cum   = 0.0
        max_dd     = 0.0
        for t in reversed(trades):   # reversed = chronological
            cumulative += t["realized_pnl"]
            if cumulative > peak_cum:
                peak_cum = cumulative
            dd = peak_cum - cumulative
            if dd > max_dd:
                max_dd = dd

        # Ticker ranking by expectancy
        ticker_stats = {}
        for t in trades:
            tk = t["ticker"]
            if tk not in ticker_stats:
                ticker_stats[tk] = {"pnls": [], "wins": 0}
            ticker_stats[tk]["pnls"].append(t["realized_pnl"])
            if t["realized_pnl"] > 0:
                ticker_stats[tk]["wins"] += 1

        ticker_ranking = []
        for tk, s in ticker_stats.items():
            n_tk   = len(s["pnls"])
            wr_tk  = s["wins"] / n_tk
            aw_tk  = sum(p for p in s["pnls"] if p > 0) / max(1, sum(1 for p in s["pnls"] if p > 0))
            al_tk  = sum(p for p in s["pnls"] if p <= 0) / max(1, sum(1 for p in s["pnls"] if p <= 0))
            exp_tk = wr_tk * aw_tk + (1 - wr_tk) * al_tk
            ticker_ranking.append({
                "ticker": tk, "trades": n_tk, "win_rate": round(wr_tk, 3),
                "expectancy": round(exp_tk, 2),
            })
        ticker_ranking.sort(key=lambda x: x["expectancy"], reverse=True)

        # Regime ranking
        regime_stats = {}
        for t in trades:
            r = t.get("regime", "unknown") or "unknown"
            regime_stats.setdefault(r, {"pnls": [], "wins": 0})
            regime_stats[r]["pnls"].append(t["realized_pnl"])
            if t["realized_pnl"] > 0:
                regime_stats[r]["wins"] += 1
        regime_ranking = {
            r: {
                "win_rate": round(s["wins"] / len(s["pnls"]), 3),
                "expectancy": round(
                    (s["wins"] / len(s["pnls"])) *
                    (sum(p for p in s["pnls"] if p > 0) / max(1, sum(1 for p in s["pnls"] if p > 0)))
                    + (1 - s["wins"] / len(s["pnls"])) *
                    (sum(p for p in s["pnls"] if p <= 0) / max(1, sum(1 for p in s["pnls"] if p <= 0))),
                    2),
                "trades": len(s["pnls"]),
            }
            for r, s in regime_stats.items()
        }

        # Time-of-day ranking (by entry hour ET)
        tod_stats = {}
        for t in trades:
            try:
                hour = int((t.get("entry_time") or "")[:13].split("T")[1].split(":")[0])
            except Exception:
                hour = 10
            tod_stats.setdefault(hour, {"pnls": [], "wins": 0})
            tod_stats[hour]["pnls"].append(t["realized_pnl"])
            if t["realized_pnl"] > 0:
                tod_stats[hour]["wins"] += 1
        tod_ranking = {
            f"{h}:00": {
                "win_rate": round(s["wins"] / len(s["pnls"]), 3),
                "avg_pnl":  round(sum(s["pnls"]) / len(s["pnls"]), 2),
                "trades":   len(s["pnls"]),
            }
            for h, s in sorted(tod_stats.items())
        }

        # RSI bucket: <20, 20-25, 25-30, 30+
        rsi_buckets = {"<20": [], "20-25": [], "25-30": [], "30+": []}
        for t in trades:
            rsi = t.get("rsi_5m") or 0
            if rsi < 20:
                rsi_buckets["<20"].append(t["realized_pnl"])
            elif rsi < 25:
                rsi_buckets["20-25"].append(t["realized_pnl"])
            elif rsi < 30:
                rsi_buckets["25-30"].append(t["realized_pnl"])
            else:
                rsi_buckets["30+"].append(t["realized_pnl"])
        rsi_ranking = {
            bucket: {
                "avg_pnl": round(sum(ps) / len(ps), 2) if ps else 0,
                "trades":  len(ps),
            }
            for bucket, ps in rsi_buckets.items()
        }

        # VIX bucket: <15, 15-20, 20-25, 25+
        vix_buckets = {"<15": [], "15-20": [], "20-25": [], "25+": []}
        for t in trades:
            vix = t.get("vix_at_entry") or 0
            if vix < 15:
                vix_buckets["<15"].append(t["realized_pnl"])
            elif vix < 20:
                vix_buckets["15-20"].append(t["realized_pnl"])
            elif vix < 25:
                vix_buckets["20-25"].append(t["realized_pnl"])
            else:
                vix_buckets["25+"].append(t["realized_pnl"])
        vix_ranking = {
            bucket: {
                "avg_pnl": round(sum(ps) / len(ps), 2) if ps else 0,
                "trades":  len(ps),
            }
            for bucket, ps in vix_buckets.items()
        }

        return {
            "trades":         n_total,
            "win_rate":       round(win_rate, 4),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "expectancy":     round(expectancy, 2),
            "profit_factor":  round(profit_factor, 3),
            "sharpe":         round(sharpe, 3),
            "avg_hold_min":   round(avg_hold, 1),
            "max_drawdown":   round(max_dd, 2),
            "gross_pnl":      round(sum(pnls), 2),
            "ticker_ranking": ticker_ranking[:10],
            "regime_ranking": regime_ranking,
            "tod_ranking":    tod_ranking,
            "rsi_ranking":    rsi_ranking,
            "vix_ranking":    vix_ranking,
        }

    # ── Edge decay detection ──────────────────────────────────────────────────

    def check_edge_decay(self) -> dict:
        """
        Compare rolling 20-trade expectancy to rolling 100-trade baseline.
        Returns: {decaying: bool, recent_exp: float, baseline_exp: float, pct_drop: float}
        """
        recent   = self._fetch_trades(n=20)
        baseline = self._fetch_trades(n=100)

        def _exp(trades):
            if not trades:
                return None
            pnls  = [t["realized_pnl"] for t in trades]
            wins  = [p for p in pnls if p > 0]
            n     = len(pnls)
            wr    = len(wins) / n
            aw    = sum(wins) / len(wins) if wins else 0
            al    = sum(p for p in pnls if p <= 0) / max(1, sum(1 for p in pnls if p <= 0))
            return wr * aw + (1 - wr) * al

        recent_exp   = _exp(recent)
        baseline_exp = _exp(baseline)

        if recent_exp is None or baseline_exp is None or baseline_exp <= 0:
            return {"decaying": False, "recent_exp": recent_exp,
                    "baseline_exp": baseline_exp, "pct_drop": 0}

        pct_drop = (baseline_exp - recent_exp) / abs(baseline_exp) * 100
        decaying = pct_drop > 40   # more than 40% drop from baseline

        return {
            "decaying":     decaying,
            "recent_exp":   round(recent_exp, 2),
            "baseline_exp": round(baseline_exp, 2),
            "pct_drop":     round(pct_drop, 1),
        }

    # ── Adaptive conviction modifier (Phase 2 seed) ───────────────────────────

    def get_conviction_modifier(self, ticker: str, regime: str,
                                vix: float, rsi: float) -> float:
        """
        Returns a multiplier (0.5–1.3) for kelly_contracts output.
        Based on rolling 30-day performance for this ticker/regime/VIX combo.
        Requires at least 5 trades in the combo to adjust.
        """
        try:
            vix_bucket = ("<15" if vix < 15 else "15-20" if vix < 20
                          else "20-25" if vix < 25 else "25+")
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT realized_pnl FROM trades
                    WHERE ticker = ?
                      AND regime = ?
                      AND date >= date('now', '-30 days')
                    ORDER BY id DESC LIMIT 30
                """, (ticker.upper(), regime)).fetchall()

            if len(rows) < 5:
                return 1.0   # not enough data

            pnls  = [r["realized_pnl"] for r in rows]
            wins  = [p for p in pnls if p > 0]
            n     = len(pnls)
            wr    = len(wins) / n
            aw    = sum(wins) / len(wins) if wins else 0
            al    = sum(p for p in pnls if p <= 0) / max(1, sum(1 for p in pnls if p <= 0))
            exp   = wr * aw + (1 - wr) * al
            pf    = abs(sum(wins)) / max(1, abs(sum(p for p in pnls if p <= 0)))

            if exp < 0:
                return 0.5   # losing setup — halve sizing
            if wr >= 0.75 and pf >= 2.0:
                return 1.3   # strong proven edge — modest size up
            if wr >= 0.60 and pf >= 1.5:
                return 1.15
            return 1.0

        except Exception as e:
            log.debug("get_conviction_modifier error: %s", e)
            return 1.0

    # ── Fetch VIX ─────────────────────────────────────────────────────────────

    @staticmethod
    def get_current_vix() -> float:
        """Fetch current VIX from yfinance. Returns 0.0 on failure."""
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="1d", interval="1m")
            if not hist.empty:
                return round(float(hist["Close"].iloc[-1]), 2)
        except Exception as e:
            log.debug("VIX fetch error: %s", e)
        return 0.0

    @staticmethod
    def get_spy_change_pct() -> float:
        """Fetch SPY day change % from yfinance."""
        try:
            import yfinance as yf
            spy  = yf.Ticker("SPY")
            hist = spy.history(period="2d", interval="1d")
            if len(hist) >= 2:
                prev  = float(hist["Close"].iloc[-2])
                today = float(hist["Close"].iloc[-1])
                return round((today - prev) / prev * 100, 3)
        except Exception as e:
            log.debug("SPY change fetch error: %s", e)
        return 0.0

    # ── Daily edge report ─────────────────────────────────────────────────────

    def generate_daily_report(self) -> dict:
        """
        Generate the full daily analytics report.
        Called by main.py at 4:15 PM ET alongside existing daily summary.
        Returns dict for Discord formatting.
        """
        today_stats = self.compute_stats(days=1)
        week_stats  = self.compute_stats(days=7)
        decay       = self.check_edge_decay()

        # Best/worst ticker today
        best_ticker  = ""
        worst_ticker = ""
        if today_stats.get("ticker_ranking"):
            best_ticker  = today_stats["ticker_ranking"][0]["ticker"]
            worst_ticker = today_stats["ticker_ranking"][-1]["ticker"]

        # Save to edge_metrics table
        try:
            regime_r = today_stats.get("regime_ranking", {})
            with self._conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO edge_metrics
                    (date, expectancy, win_rate, profit_factor, sharpe,
                     trades_count, best_ticker, worst_ticker,
                     regime_bull_wr, regime_bear_wr,
                     vix_low_wr, vix_high_wr, edge_decay)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    date.today().isoformat(),
                    today_stats.get("expectancy", 0),
                    today_stats.get("win_rate", 0),
                    today_stats.get("profit_factor", 0),
                    today_stats.get("sharpe", 0),
                    today_stats.get("trades", 0),
                    best_ticker, worst_ticker,
                    regime_r.get("bull", {}).get("win_rate", 0),
                    regime_r.get("bear", {}).get("win_rate", 0),
                    today_stats.get("vix_ranking", {}).get("<15", {}).get("avg_pnl", 0),
                    today_stats.get("vix_ranking", {}).get("25+", {}).get("avg_pnl", 0),
                    int(decay.get("decaying", False)),
                ))
                conn.commit()
        except Exception as e:
            log.debug("edge_metrics save error: %s", e)

        return {
            "today":    today_stats,
            "week":     week_stats,
            "decay":    decay,
        }

    def format_discord_report(self, report: dict) -> str:
        """Format the daily analytics report for Discord."""
        t   = report.get("today", {})
        w   = report.get("week", {})
        dec = report.get("decay", {})

        lines = [
            "📊 **Daily Analytics Report**",
            "",
            f"**Today** ({t.get('trades', 0)} trades)",
            f"  Win rate:      {t.get('win_rate', 0):.1%}",
            f"  Expectancy:    ${t.get('expectancy', 0):.2f}/trade",
            f"  Profit factor: {t.get('profit_factor', 0):.2f}",
            f"  Gross P&L:     ${t.get('gross_pnl', 0):.2f}",
            f"  Avg hold:      {t.get('avg_hold_min', 0):.0f} min",
            "",
            f"**7-Day** ({w.get('trades', 0)} trades)",
            f"  Win rate:      {w.get('win_rate', 0):.1%}",
            f"  Expectancy:    ${w.get('expectancy', 0):.2f}/trade",
            f"  Sharpe:        {w.get('sharpe', 0):.2f}",
            f"  Max drawdown:  ${w.get('max_drawdown', 0):.2f}",
        ]

        if t.get("ticker_ranking"):
            best = t["ticker_ranking"][0]
            lines += ["", f"**Best ticker today**: {best['ticker']} "
                      f"(exp ${best['expectancy']:.2f}, {best['trades']} trades)"]

        if t.get("regime_ranking"):
            lines.append("")
            lines.append("**By regime today:**")
            for regime, rs in t["regime_ranking"].items():
                lines.append(f"  {regime}: WR={rs['win_rate']:.1%} "
                              f"exp=${rs['expectancy']:.2f} ({rs['trades']} trades)")

        if t.get("tod_ranking"):
            lines.append("")
            lines.append("**Best hour:**")
            best_hour = max(t["tod_ranking"].items(),
                            key=lambda x: x[1]["avg_pnl"], default=None)
            if best_hour:
                lines.append(f"  {best_hour[0]} ET → avg ${best_hour[1]['avg_pnl']:.2f}")

        if dec.get("decaying"):
            lines += [
                "",
                f"⚠️ **EDGE DECAY DETECTED**",
                f"  Recent 20-trade expectancy: ${dec['recent_exp']:.2f}",
                f"  Baseline (100-trade):        ${dec['baseline_exp']:.2f}",
                f"  Drop: {dec['pct_drop']:.1f}% — review strategy",
            ]

        return "\n".join(lines)


# ── Module-level singleton ─────────────────────────────────────────────────────
_instance: Optional[AnalyticsEngine] = None

def get_analytics() -> AnalyticsEngine:
    """Get or create the global AnalyticsEngine singleton."""
    global _instance
    if _instance is None:
        _instance = AnalyticsEngine()
    return _instance
