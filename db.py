"""
db.py — Thread-Safe SQLite Telemetry Layer
============================================
Replaces flat CSV writes with a proper database.
All writes are thread-safe via a single connection lock.

Tables:
  trades       — filled live and paper trades
  paper_shadow — signals that passed gates but were skipped/paper-only
  slippage     — per-ticker fill friction history
  sessions     — daily session summaries

Usage:
  from db import DB
  DB.log_trade(ticker="AAPL", strike=315.0, ...)
  DB.get_win_rate_by_hour()
  DB.get_slippage_by_ticker()

CSV files are still written alongside for backward compatibility
with reinforce_brain.py and ml_sieve.py.
"""

import sqlite3
import threading
import logging
import os
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("db")

_DB_PATH = Path(os.getenv("GILBERT_DB_PATH", "gilbert_trader.db"))
# ── Thread-safe connection model ─────────────────────────────────────────────
# Single shared connection with:
#   - WAL journal mode: readers don't block writers, writers don't block readers
#   - Busy timeout: waits up to 5s instead of immediately raising OperationalError
#   - Write queue: serializes all writes through a single lock to prevent corruption
_lock:   threading.Lock            = threading.Lock()
_conn:   Optional[sqlite3.Connection] = None
_write_queue: list                 = []
_queue_lock:  threading.Lock       = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Get or create the shared WAL-mode database connection."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False,
                                timeout=5.0)
        _conn.row_factory = sqlite3.Row
        # WAL mode: concurrent reads while writing, no database-locked errors
        _conn.execute("PRAGMA journal_mode=WAL")
        # Busy handler: retry for 5 seconds before raising
        _conn.execute("PRAGMA busy_timeout=5000")
        # Synchronous=NORMAL: safe with WAL, faster than FULL
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.commit()
        _init_schema(_conn)
        log.info("SQLite DB opened (WAL mode): %s", _DB_PATH)
    return _conn


def _safe_execute(sql: str, params: tuple = ()):
    """
    Thread-safe write executor. All INSERT/UPDATE operations go through here.
    Retries once on OperationalError (database locked) before giving up.
    """
    with _lock:
        try:
            conn = _get_conn()
            conn.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                log.warning("DB locked — retrying in 0.1s: %s", e)
                import time as _t
                _t.sleep(0.1)
                try:
                    conn = _get_conn()
                    conn.execute(sql, params)
                    conn.commit()
                    return True
                except Exception as e2:
                    log.error("DB retry failed: %s", e2)
                    return False
            log.error("DB execute error: %s", e)
            return False
        except Exception as e:
            log.error("DB execute error: %s", e)
            return False


def _init_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT    NOT NULL,
            ticker          TEXT    NOT NULL,
            strike          REAL,
            option_type     TEXT,
            expiry          TEXT,
            quantity        INTEGER,
            entry_price     REAL,
            exit_price      REAL,
            profit_amt      REAL,
            profit_pct      REAL,
            entry_rsi       REAL,
            vol_ratio       REAL,
            flow_score      REAL,
            put_call_ratio  REAL,
            gex_significance REAL,
            spy_vs_sma20    REAL,
            mode            TEXT    DEFAULT 'live',
            is_tester       INTEGER DEFAULT 0,
            adx_at_entry    REAL,
            vix_at_entry    REAL,
            regime          TEXT,
            hour_of_day     INTEGER,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS paper_shadow (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            signal      TEXT,
            ticker      TEXT,
            strike      REAL,
            option_type TEXT,
            expiry      TEXT,
            quantity    INTEGER,
            entry_rsi   REAL,
            mode        TEXT    DEFAULT 'paper_shadow',
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS slippage (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT    NOT NULL,
            ticker          TEXT    NOT NULL,
            est_mid         REAL,
            fill_price      REAL,
            slip_pct        REAL,
            multiplier      REAL,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    UNIQUE,
            trades      INTEGER DEFAULT 0,
            wins        INTEGER DEFAULT 0,
            losses      INTEGER DEFAULT 0,
            total_pnl   REAL    DEFAULT 0,
            win_rate    REAL    DEFAULT 0,
            best_trade  TEXT,
            worst_trade TEXT,
            vix_open    REAL,
            regime      TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_date   ON trades(date);
        CREATE INDEX IF NOT EXISTS idx_trades_hour   ON trades(hour_of_day);
    """)
    conn.commit()


class DB:
    """Static interface — all methods are class-level for simple imports."""

    @staticmethod
    def log_trade(ticker: str, strike: float = None, option_type: str = None,
                  expiry: str = None, quantity: int = 1,
                  entry_price: float = None, exit_price: float = None,
                  profit_amt: float = None, profit_pct: float = None,
                  entry_rsi: float = None, vol_ratio: float = None,
                  flow_score: float = None, put_call_ratio: float = None,
                  gex_significance: float = None, spy_vs_sma20: float = None,
                  mode: str = "live", is_tester: bool = False,
                  adx_at_entry: float = None, vix_at_entry: float = None,
                  regime: str = None):
        """Write a filled trade to the database and trade_log.csv."""
        now        = datetime.now()
        date_str   = now.strftime("%Y-%m-%d %H:%M")
        hour       = now.hour

        with _lock:
            try:
                return _safe_execute("""
                    INSERT INTO trades
                    (date, ticker, strike, option_type, expiry, quantity,
                     entry_price, exit_price, profit_amt, profit_pct,
                     entry_rsi, vol_ratio, flow_score, put_call_ratio,
                     gex_significance, spy_vs_sma20, mode, is_tester,
                     adx_at_entry, vix_at_entry, regime, hour_of_day)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (date_str, ticker, strike, option_type, expiry, quantity,
                      entry_price, exit_price, profit_amt, profit_pct,
                      entry_rsi, vol_ratio, flow_score, put_call_ratio,
                      gex_significance, spy_vs_sma20, mode, int(is_tester),
                      adx_at_entry, vix_at_entry, regime, hour))
            except Exception as e:
                log.error("DB log_trade error: %s", e)

        # Also append to trade_log.csv for backward compatibility
        DB._append_csv("trade_log.csv", {
            "date": date_str, "ticker": ticker, "strike": strike,
            "option_type": option_type, "expiry": expiry, "quantity": quantity,
            "entry_price": entry_price, "exit_price": exit_price,
            "profit_amt": profit_amt, "profit_pct": profit_pct,
            "entry_rsi": entry_rsi, "vol_ratio": vol_ratio,
            "flow_score": flow_score, "put_call_ratio": put_call_ratio,
            "mode": mode,
        })

    @staticmethod
    def log_paper_shadow(signal: str, ticker: str, strike: float = None,
                         option_type: str = None, expiry: str = None,
                         quantity: int = 1, entry_rsi: float = None):
        """Log a signal that passed gates but wasn't executed live."""
        now      = datetime.now()
        date_str = now.strftime("%Y-%m-%d %H:%M")
        with _lock:
            try:
                return _safe_execute("""
                    INSERT INTO paper_shadow
                    (date, signal, ticker, strike, option_type, expiry, quantity, entry_rsi)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (date_str, signal, ticker, strike, option_type, expiry, quantity, entry_rsi))
            except Exception as e:
                log.error("DB log_paper_shadow error: %s", e)

        DB._append_csv("paper_shadow.csv", {
            "date": date_str, "signal": signal, "ticker": ticker,
            "strike": strike, "option_type": option_type,
            "expiry": expiry, "quantity": quantity, "mode": "paper_shadow",
        })

    @staticmethod
    def log_slippage(ticker: str, est_mid: float, fill_price: float,
                     slip_pct: float, multiplier: float):
        """Record a slippage observation after a fill."""
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        with _lock:
            try:
                return _safe_execute("""
                    INSERT INTO slippage (date, ticker, est_mid, fill_price, slip_pct, multiplier)
                    VALUES (?,?,?,?,?,?)
                """, (date_str, ticker, est_mid, fill_price, slip_pct, multiplier))
            except Exception as e:
                log.error("DB log_slippage error: %s", e)

    # ── Analytics queries ─────────────────────────────────────────────────────

    @staticmethod
    def get_win_rate_by_hour() -> list:
        """Returns win rate per hour of the trading day."""
        with _lock:
            try:
                conn = _get_conn()
                rows = conn.execute("""
                    SELECT hour_of_day,
                           COUNT(*) as total,
                           SUM(CASE WHEN profit_amt > 0 THEN 1 ELSE 0 END) as wins,
                           ROUND(AVG(profit_pct), 2) as avg_pct,
                           ROUND(SUM(profit_amt), 2) as total_pnl
                    FROM trades
                    WHERE mode = 'live' AND profit_amt IS NOT NULL
                    GROUP BY hour_of_day
                    ORDER BY hour_of_day
                """).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.error("DB get_win_rate_by_hour error: %s", e)
                return []

    @staticmethod
    def get_slippage_by_ticker() -> list:
        """Returns average slippage per ticker, worst first."""
        with _lock:
            try:
                conn = _get_conn()
                rows = conn.execute("""
                    SELECT ticker,
                           COUNT(*) as fills,
                           ROUND(AVG(slip_pct), 2) as avg_slip_pct,
                           ROUND(MAX(slip_pct), 2) as worst_slip_pct,
                           ROUND(AVG(multiplier), 4) as avg_multiplier
                    FROM slippage
                    GROUP BY ticker
                    ORDER BY avg_slip_pct DESC
                """).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.error("DB get_slippage_by_ticker error: %s", e)
                return []

    @staticmethod
    def get_best_tickers(min_trades: int = 3) -> list:
        """Returns tickers ranked by win rate and total P&L."""
        with _lock:
            try:
                conn = _get_conn()
                rows = conn.execute("""
                    SELECT ticker,
                           COUNT(*) as trades,
                           SUM(CASE WHEN profit_amt > 0 THEN 1 ELSE 0 END) as wins,
                           ROUND(100.0 * SUM(CASE WHEN profit_amt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
                           ROUND(SUM(profit_amt), 2) as total_pnl,
                           ROUND(AVG(profit_pct), 2) as avg_pct
                    FROM trades
                    WHERE mode = 'live' AND profit_amt IS NOT NULL
                    GROUP BY ticker
                    HAVING COUNT(*) >= ?
                    ORDER BY total_pnl DESC
                """, (min_trades,)).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.error("DB get_best_tickers error: %s", e)
                return []

    @staticmethod
    def get_session_summary(date_str: str = None) -> dict:
        """Returns today's session summary from the database."""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        with _lock:
            try:
                conn = _get_conn()
                row = conn.execute("""
                    SELECT COUNT(*) as trades,
                           SUM(CASE WHEN profit_amt > 0 THEN 1 ELSE 0 END) as wins,
                           ROUND(SUM(profit_amt), 2) as total_pnl,
                           ROUND(MAX(profit_amt), 2) as best,
                           ROUND(MIN(profit_amt), 2) as worst
                    FROM trades
                    WHERE date LIKE ? AND mode = 'live' AND profit_amt IS NOT NULL
                """, (f"{date_str}%",)).fetchone()
                return dict(row) if row else {}
            except Exception as e:
                log.error("DB get_session_summary error: %s", e)
                return {}

    @staticmethod
    def _append_csv(path: str, row: dict):
        """Append a row to a CSV file, creating headers if new."""
        try:
            file_exists = Path(path).exists()
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            log.debug("CSV append error (%s): %s", path, e)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing DB module...")
    DB.log_trade("AAPL", strike=315.0, option_type="C", expiry="6/6",
                 quantity=1, entry_price=2.50, exit_price=2.90,
                 profit_amt=40.0, profit_pct=16.0, entry_rsi=22.5,
                 mode="paper", is_tester=True)
    summary = DB.get_session_summary()
    print(f"Session: {summary}")
    hours = DB.get_win_rate_by_hour()
    print(f"Win by hour: {hours}")
    print("✅ DB test complete")