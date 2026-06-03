"""
reinforce_brain.py — Meta-Learning Parameter Optimizer
======================================================
Ingests trade logs, scores session performance metrics, and calculates
optimized shifts for RSI thresholds, profit targets, and stop losses.

Runs as a standalone module called by optimize_strategy_thresholds()
in main.py every Friday after market close.
"""
import json
import logging
import pandas as pd
from pathlib import Path
from datetime import datetime

log = logging.getLogger("reinforce_brain")
_BOUNDS_PATH = Path("adaptive_bounds.json")


def process_session_feedback(trade_log_path: str = "trade_log.csv") -> dict:
    """
    Computes a parameter mutation matrix by gradient-stepping performance.

    Policy adjustments:
    - RSI threshold → mean winning entry RSI (clamped 20–35)
    - SL % → tightened if average loss exceeds 25%
    - TP % → shifted to faster scalp target on deep drawdowns
    - Writes all mutations to adaptive_bounds.json for next session load
    """
    if not Path(trade_log_path).exists():
        log.info("[Reinforce] No trade log found at %s — skipping", trade_log_path)
        return {}

    try:
        df = pd.read_csv(trade_log_path)
        if df.empty or len(df) < 10:
            log.info("[Reinforce] Insufficient data (%d rows, need 10) — skipping", len(df))
            return {}

        # Parse numeric types safely
        df["profit_amt"] = pd.to_numeric(df["profit_amt"], errors="coerce").fillna(0)
        df["profit_pct"] = pd.to_numeric(df["profit_pct"], errors="coerce").fillna(0)
        df["entry_rsi"]  = pd.to_numeric(df["entry_rsi"],  errors="coerce").fillna(25)

        wins   = df[df["profit_amt"] > 0]
        losses = df[df["profit_amt"] <= 0]

        updates = {
            "recommended_tp_pct":       30.0,
            "recommended_sl_pct":       20.0,
            "recommended_rsi_threshold": 30,
        }

        # ── Policy 1: RSI threshold → mean winning RSI ────────────────────────
        if not wins.empty:
            avg_win_rsi = float(wins["entry_rsi"].mean())
            rec_rsi = int(max(20, min(35, round(avg_win_rsi))))
            updates["recommended_rsi_threshold"] = rec_rsi
            log.info("[Reinforce] Avg winning RSI=%.1f → threshold %d", avg_win_rsi, rec_rsi)

        # ── Policy 2: SL/TP → tighten on deep average losses ─────────────────
        if len(losses) >= 3:
            avg_loss_pct = abs(float(losses["profit_pct"].mean()))
            if avg_loss_pct > 25.0:
                updates["recommended_sl_pct"] = 15.0
                updates["recommended_tp_pct"] = 25.0
                log.info("[Reinforce] Deep avg loss %.1f%% → tightened SL=15%% TP=25%%",
                         avg_loss_pct)
            elif avg_loss_pct < 10.0 and not wins.empty:
                # Losses are small — can afford wider TP target
                win_rate = len(wins) / len(df)
                if win_rate > 0.55:
                    updates["recommended_tp_pct"] = 35.0
                    log.info("[Reinforce] Win rate %.0f%%, small losses → TP raised to 35%%",
                             win_rate * 100)

        # ── Policy 3: Win rate annotation ─────────────────────────────────────
        if len(df) > 0:
            win_rate = round(len(wins) / len(df) * 100, 1)
            total_pnl = round(float(df["profit_amt"].sum()), 2)
            updates["session_win_rate_pct"] = win_rate
            updates["session_total_pnl"]    = total_pnl
            log.info("[Reinforce] Session: %d trades | Win rate %.1f%% | P&L $%.2f",
                     len(df), win_rate, total_pnl)

        updates["last_reinforced"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        # ── Write state overlay to adaptive_bounds.json ───────────────────────
        existing = {}
        if _BOUNDS_PATH.exists():
            try:
                existing = json.loads(_BOUNDS_PATH.read_text())
            except Exception:
                pass

        existing.update(updates)
        _BOUNDS_PATH.write_text(json.dumps(existing, indent=2))
        log.info("[Reinforce] Parameter mutations written: %s", updates)
        return existing

    except Exception as e:
        log.error("[Reinforce] Loop failed: %s", e)
        return {}
