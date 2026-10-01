"""
outcome_tracker.py — Forward Outcome Tracker  (v2, full spec)
=============================================================
For every candidate in candidates.csv (fired OR rejected), records:
  - Unique candidate_id (UUID, stable across reruns)
  - signal_episode_id (groups consecutive same-ticker/direction signals)
  - Underlying forward prices at +5/+15/+30/+60/+120 min (null after 4 PM)
  - MFE and MAE (direction-normalized: always "in our favor" / "against us")
  - Hypothetical contract snapshot (bid/ask/IV/delta at signal time — no buy)
  - tracking_status, tracking_attempts, data_complete flag
  - Market-hours truncation (no tomorrow's open as "+120m intraday result")
  - Halt/gap detection

Deduplication: by candidate_id (UUID in candidates.csv), not timestamp+ticker.
Processing: entries ≥ 2 hours old, retried up to 5× on API failure.
"""

import csv
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

log = logging.getLogger("outcome_tracker")

CANDIDATES_FILE  = "candidates.csv"
OUTCOMES_FILE    = "candidate_outcomes.csv"
CHECK_INTERVAL   = 300          # check every 5 min
HORIZONS_MIN     = [5, 15, 30, 60, 120]
MAX_ATTEMPTS     = 5
MIN_AGE_SECONDS  = 7200         # 2 hours before processing
MARKET_CLOSE_HH  = 16           # 4:00 PM ET hard cutoff
_lock            = threading.Lock()


# ── Gate config for auto-hash ─────────────────────────────────────────────────
GATE_CONFIG = {
    "RSI_CALL_BASE":     25,
    "RSI_PUT_MIN":       65,
    "IV_MAX_CALL":       60,
    "IV_MAX_PUT":        80,
    "ADX_MAX":           30,
    "FLOW_MIN":          35,
    "NEWS_BLOCK_CALL":   40,
    "NEWS_BLOCK_PUT":    80,
    "RVOL_MIN":          0.3,
    "DTE_MAX":           5,
    "SPREAD_MAX_PCT":    15.0,
    "SMA200_PUT_RSI_MIN":75,
    "HOURLY_RSI_PUT_MAX":70,
    "SECTOR_PUT_MAX":    1.5,
    "STRATEGY_VERSION":  "v2.4.1",
}

CONFIG_HASH = hashlib.md5(
    json.dumps(GATE_CONFIG, sort_keys=True).encode()
).hexdigest()[:8]


def _market_close_today(signal_date: date) -> datetime:
    """Return 4:00 PM ET on the same date as the signal."""
    return datetime(signal_date.year, signal_date.month, signal_date.day, 16, 0, 0)


def _is_in_market_hours(dt: datetime) -> bool:
    return 9 <= dt.hour < 16


def _get_1m_bars(ticker: str, signal_dt: datetime):
    """Fetch 1-minute bars for the signal date. Returns DataFrame or None."""
    # Don't fetch after-hours — yfinance returns errors for small caps after 4 PM
    now = datetime.now()
    if now.hour >= 16 and now.date() == signal_dt.date():
        return None   # will retry tomorrow when data is available
    try:
        import yfinance as yf
        df = yf.download(ticker, period="5d", interval="1m",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)
        df.index = df.index.tz_localize(None) if df.index.tzinfo else df.index
        return df
    except Exception as e:
        log.debug("1m bars error %s: %s", ticker, e)
        return None


def _price_at(df, target_dt: datetime, max_gap_minutes: int = 3) -> Optional[float]:
    """
    Get price at target_dt from a pre-fetched 1m DataFrame.
    Returns None if the nearest bar is >max_gap_minutes away (possible halt).
    Returns "HALT" string if gap detected.
    """
    if df is None:
        return None
    future = df[df.index >= target_dt]
    if future.empty:
        return None   # future data not available yet
    nearest = future.index[0]
    gap_min = (nearest - target_dt).total_seconds() / 60
    if gap_min > max_gap_minutes:
        return "HALT"  # data gap — possible halt
    return float(future["Close"].iloc[0])


def _compute_mfe_mae(df, signal_dt: datetime, direction: str,
                     entry_price: float, window_min: int = 120,
                     market_close: datetime = None) -> tuple:
    """
    MFE and MAE — always direction-normalized:
      PUT:  MFE = max decline (favorable), MAE = max rise (adverse)
      CALL: MFE = max rise   (favorable), MAE = max decline (adverse)
    Both expressed as % relative to entry_price.
    Truncated at market close.
    """
    if df is None or entry_price <= 0:
        return None, None
    end_dt  = signal_dt + timedelta(minutes=window_min)
    if market_close:
        end_dt = min(end_dt, market_close)
    window  = df[(df.index >= signal_dt) & (df.index <= end_dt)]["Close"].dropna()
    if window.empty:
        return None, None
    high = float(window.max())
    low  = float(window.min())
    if direction == "P":
        mfe = round((entry_price - low)  / entry_price * 100, 3)
        mae = round((entry_price - high) / entry_price * 100, 3)
    else:
        mfe = round((high - entry_price) / entry_price * 100, 3)
        mae = round((low  - entry_price) / entry_price * 100, 3)
    return mfe, mae


def _snapshot_contract(ticker: str, direction: str, signal_dt: datetime) -> dict:
    """
    Snapshot the hypothetical contract at signal time — NO ORDER PLACED.
    Records bid/ask/IV/delta for shadow portfolio analysis.
    """
    snap = {
        "snap_bid": None, "snap_ask": None, "snap_mid": None,
        "snap_spread_pct": None, "snap_iv": None, "snap_delta": None,
        "snap_strike": None, "snap_dte": None,
    }
    try:
        import yfinance as yf
        tk     = yf.Ticker(ticker)
        exps   = tk.options
        if not exps:
            return snap
        # Find nearest expiry ≤ DTE_MAX days out
        today  = signal_dt.date()
        valid  = []
        for e in exps:
            try:
                ed = datetime.strptime(e, "%Y-%m-%d").date()
                dte = (ed - today).days
                if 0 <= dte <= GATE_CONFIG["DTE_MAX"]:
                    valid.append((dte, e))
            except Exception:
                pass
        if not valid:
            return snap
        dte, exp = min(valid, key=lambda x: x[0])
        chain   = tk.option_chain(exp)
        opts    = chain.puts if direction == "P" else chain.calls
        if opts.empty:
            return snap
        # ATM strike
        spot    = float(tk.fast_info.get("lastPrice") or 0)
        if spot <= 0:
            return snap
        row     = opts.iloc[(opts["strike"] - spot).abs().argsort()[:1]]
        if row.empty:
            return snap
        r       = row.iloc[0]
        bid     = float(r.get("bid") or 0)
        ask     = float(r.get("ask") or 0)
        mid     = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else None
        snap.update({
            "snap_bid":        bid,
            "snap_ask":        ask,
            "snap_mid":        mid,
            "snap_spread_pct": round((ask - bid) / mid * 100, 1) if mid else None,
            "snap_iv":         round(float(r.get("impliedVolatility") or 0) * 100, 1),
            "snap_delta":      round(float(r.get("delta") or 0), 4),
            "snap_strike":     float(r.get("strike") or 0),
            "snap_dte":        dte,
        })
    except Exception as e:
        log.debug("Contract snapshot error %s: %s", ticker, e)
    return snap


def _assign_candidate_ids(candidates_path: str):
    """
    Ensure every row in candidates.csv has a unique candidate_id.
    Also assigns signal_episode_id (groups consecutive same-ticker/direction).
    Rewrites the file if any IDs are missing.
    """
    if not Path(candidates_path).exists():
        return

    try:
        with open(candidates_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return

    if not rows:
        return

    changed   = False
    # Episode tracking: ticker+direction -> (last_ts, episode_id)
    episodes: dict = {}

    for row in rows:
        if not row.get("candidate_id"):
            row["candidate_id"]      = str(uuid.uuid4())
            row["config_hash"]       = CONFIG_HASH
            changed = True

        if not row.get("config_hash"):
            row["config_hash"] = CONFIG_HASH
            changed = True

        # Assign episode IDs
        if not row.get("signal_episode_id"):
            key    = (row.get("ticker",""), row.get("direction",""))
            try:
                ts = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = datetime.now()

            if key in episodes:
                last_ts, ep_id = episodes[key]
                gap_min = (ts - last_ts).total_seconds() / 60
                if gap_min > 10:   # new episode after 10-min gap
                    ep_id = str(uuid.uuid4())
            else:
                ep_id = str(uuid.uuid4())

            episodes[key] = (ts, ep_id)
            row["signal_episode_id"] = ep_id
            changed = True

    if not changed:
        return

    fieldnames = list(rows[0].keys())
    for f in ["candidate_id", "signal_episode_id", "config_hash"]:
        if f not in fieldnames:
            fieldnames.insert(0, f)

    try:
        with open(candidates_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log.info("Assigned candidate/episode IDs to %d rows", len(rows))
    except Exception as e:
        log.error("ID assignment write error: %s", e)


def process_pending_outcomes(candidates_path: str, outcomes_path: str):
    """Read candidates, compute forward outcomes for mature entries."""
    if not Path(candidates_path).exists():
        return

    # First ensure all candidates have IDs
    _assign_candidate_ids(candidates_path)

    try:
        with open(candidates_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        log.debug("Candidates read error: %s", e)
        return

    # Load already-complete outcomes (by candidate_id)
    complete_ids   = set()
    pending_retry  = {}    # id -> {attempts, last_attempt}
    if Path(outcomes_path).exists():
        try:
            with open(outcomes_path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    cid = r.get("candidate_id", "")
                    _ts = r.get("tracking_status", "")
                    if r.get("data_complete") == "1" or _ts in ("complete", "partial_session_end"):
                        complete_ids.add(cid)
                    elif cid:
                        pending_retry[cid] = {
                            "attempts":       int(r.get("tracking_attempts") or 0),
                            "last_attempt":   r.get("last_attempt", ""),
                            "tracking_status": _ts,
                        }
        except Exception:
            pass

    now      = datetime.now()
    new_rows = []

    for row in rows:
        cid = row.get("candidate_id", "")
        if not cid:
            continue
        if cid in complete_ids:
            continue

        # Retry limit
        retry_info = pending_retry.get(cid, {})
        attempts   = retry_info.get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            continue

        try:
            signal_dt = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        # Only process entries >= MIN_AGE_SECONDS old
        if (now - signal_dt).total_seconds() < MIN_AGE_SECONDS:
            continue

        ticker    = row.get("ticker", "")
        direction = row.get("direction", "C")

        # Market close cutoff for this signal's date
        mkt_close = _market_close_today(signal_dt.date())

        # Stop retrying partial_session_end and complete entries -- they're done
        _status = pending_retry.get(cid, {}).get("tracking_status", "")
        if _status in ("complete", "partial_session_end"):
            continue
        # After close, if we've tried twice, mark as partial_session_end
        if signal_dt.date() == now.date() and now >= mkt_close:
            _attempts = pending_retry.get(cid, {}).get("attempts", 0)
            if _attempts >= 2:
                log.debug("Post-close complete: %s %s", ticker, cid[:8])
                continue

        # Fetch 1-minute bars once and reuse
        df        = _get_1m_bars(ticker, signal_dt)

        # Entry price
        entry_px  = _price_at(df, signal_dt)
        if not entry_px or entry_px == "HALT":
            new_rows.append(_make_status_row(
                row, cid, attempts + 1,
                "no_entry_price" if entry_px is None else "halt_at_entry"
            ))
            continue
        entry_px  = float(entry_px)

        outcome_row             = dict(row)
        outcome_row["candidate_id"]      = cid
        outcome_row["config_hash"]       = row.get("config_hash", CONFIG_HASH)
        outcome_row["underlying_entry"]  = round(entry_px, 4)
        outcome_row["tracking_attempts"] = attempts + 1
        outcome_row["last_attempt"]      = now.strftime("%Y-%m-%d %H:%M:%S")
        all_horizons_done                = True

        for h in HORIZONS_MIN:
            target = signal_dt + timedelta(minutes=h)

            # Truncate at market close — don't use next-day open
            if target > mkt_close:
                outcome_row[f"px_{h}m"]   = None
                outcome_row[f"chg_{h}m"]  = None
                outcome_row[f"note_{h}m"] = "after_close"
                continue

            # Target must also have passed (don't process future horizons)
            if target > now:
                all_horizons_done = False
                outcome_row[f"px_{h}m"]   = None
                outcome_row[f"chg_{h}m"]  = None
                outcome_row[f"note_{h}m"] = "future"
                continue

            px = _price_at(df, target)
            if px == "HALT":
                outcome_row[f"px_{h}m"]   = None
                outcome_row[f"chg_{h}m"]  = None
                outcome_row[f"note_{h}m"] = "halt"
                all_horizons_done = False
            elif px is None:
                outcome_row[f"px_{h}m"]   = None
                outcome_row[f"chg_{h}m"]  = None
                outcome_row[f"note_{h}m"] = "unavailable"
                all_horizons_done = False
            else:
                chg = round((float(px) - entry_px) / entry_px * 100, 3)
                outcome_row[f"px_{h}m"]   = round(float(px), 4)
                outcome_row[f"chg_{h}m"]  = chg
                outcome_row[f"note_{h}m"] = "ok"

        # MFE / MAE (direction-normalized, truncated at market close)
        mfe, mae = _compute_mfe_mae(df, signal_dt, direction, entry_px,
                                    window_min=120, market_close=mkt_close)
        outcome_row["mfe_120m"]    = mfe
        outcome_row["mae_120m"]    = mae

        # Hypothetical contract snapshot
        snap = _snapshot_contract(ticker, direction, signal_dt)
        outcome_row.update(snap)

        # Determine terminal state following GPT's state machine:
        # complete          -- all horizons have data
        # partial_session_end -- some horizons after market close (no more data coming)
        # retryable_error   -- API failure, will retry
        # pending           -- future horizons not yet available
        _after_close_ct = sum(
            1 for h in HORIZONS_MIN
            if outcome_row.get(f"note_{h}m") == "after_close"
        )
        _ok_ct = sum(
            1 for h in HORIZONS_MIN
            if outcome_row.get(f"note_{h}m") == "ok"
        )
        _unavail_ct = sum(
            1 for h in HORIZONS_MIN
            if outcome_row.get(f"note_{h}m") == "unavailable"
        )
        _future_ct = sum(
            1 for h in HORIZONS_MIN
            if outcome_row.get(f"note_{h}m") == "future"
        )

        if all_horizons_done and _after_close_ct == 0:
            _status       = "complete"
            _data_complete = "1"
        elif _after_close_ct > 0 and _future_ct == 0 and _unavail_ct == 0:
            # Some horizons past close but all obtainable data collected
            _status       = "partial_session_end"
            _data_complete = "1"   # done -- never retry
        elif _unavail_ct > 0 or _future_ct > 0:
            _status       = "pending"
            _data_complete = "0"
        else:
            _status       = "retryable_error"
            _data_complete = "0"

        outcome_row["data_complete"]   = _data_complete
        outcome_row["tracking_status"] = _status

        new_rows.append(outcome_row)

        log.info("Outcome: %s %s %s entry=%.2f mfe=%s mae=%s [%s]",
                 signal_dt.strftime("%H:%M"), ticker, direction,
                 entry_px, mfe, mae,
                 "COMPLETE" if all_horizons_done else "partial")

    if not new_rows:
        return

    # Merge into outcomes file (update existing by candidate_id, append new)
    _merge_outcomes(outcomes_path, new_rows)


def _make_status_row(source_row: dict, cid: str, attempts: int, status: str) -> dict:
    r = dict(source_row)
    r["candidate_id"]      = cid
    r["tracking_status"]   = status
    r["tracking_attempts"] = attempts
    r["last_attempt"]      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    r["data_complete"]     = "0"
    return r


def _merge_outcomes(outcomes_path: str, new_rows: list):
    """Write new outcome rows; update existing rows by candidate_id."""
    if not new_rows:
        return

    existing: dict = {}
    if Path(outcomes_path).exists():
        try:
            with open(outcomes_path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    cid = r.get("candidate_id", "")
                    if cid:
                        existing[cid] = r
        except Exception:
            pass

    for r in new_rows:
        existing[r["candidate_id"]] = r

    if not existing:
        return

    all_rows = list(existing.values())
    # Collect all fieldnames
    fieldnames = []
    seen = set()
    for r in all_rows:
        for k in r.keys():
            if k not in seen:
                fieldnames.append(k)
                seen.add(k)

    try:
        with open(outcomes_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in all_rows:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        log.info("Outcomes file updated: %d total rows", len(all_rows))
    except Exception as e:
        log.error("Outcome merge error: %s", e)


def _tracker_loop(candidates_path: str, outcomes_path: str):
    log.info("Outcome tracker running every %ds", CHECK_INTERVAL)
    while True:
        try:
            now_h = datetime.now().hour
            # Only process during market hours + 2 hour window after close
            # Skip overnight (6 PM - 8 AM) to avoid hammering yfinance
            if 8 <= now_h < 18:
                with _lock:
                    process_pending_outcomes(candidates_path, outcomes_path)
            else:
                log.debug("Outcome tracker sleeping (outside processing window)")
        except Exception as e:
            log.error("Outcome tracker loop error: %s", e)
        time.sleep(CHECK_INTERVAL)


def start(base_dir: str = "."):
    candidates_path = os.path.join(base_dir, CANDIDATES_FILE)
    outcomes_path   = os.path.join(base_dir, OUTCOMES_FILE)
    t = threading.Thread(
        target=_tracker_loop,
        args=(candidates_path, outcomes_path),
        name="outcome_tracker",
        daemon=True,
    )
    t.start()
    log.info("Outcome tracker started — writing to %s (config_hash=%s)",
             outcomes_path, CONFIG_HASH)
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    base = os.path.dirname(os.path.abspath(__file__))
    print(f"Config hash: {CONFIG_HASH}")
    print("Running outcome tracker manually...")
    process_pending_outcomes(
        os.path.join(base, CANDIDATES_FILE),
        os.path.join(base, OUTCOMES_FILE),
    )
    print(f"Done. Check {OUTCOMES_FILE}")