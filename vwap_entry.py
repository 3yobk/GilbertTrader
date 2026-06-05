"""
vwap_entry.py — VWAP Entry Timing Module
==========================================
VWAP (Volume Weighted Average Price) is used as the actual entry trigger.

Pattern: Stock is oversold (RSI < threshold) → wait for price to touch
VWAP and bounce above it → that's the high-probability entry point.

Why: RSI tells you a stock is oversold but not WHEN to buy.
     VWAP tells you the institutional fair value for the day.
     A bounce off VWAP after oversold RSI = institutions are stepping in.

Usage:
  from vwap_entry import check_vwap_entry, get_vwap
  result = check_vwap_entry("AAPL")
  if result["entry_confirmed"]:
      # place the trade now — price just bounced off VWAP
"""

import pandas as pd
import numpy as np
import yfinance as yf
import logging
from typing import Optional

log = logging.getLogger("vwap_entry")


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_vwap(ticker: str) -> Optional[dict]:
    """
    Calculate intraday VWAP for today.
    VWAP = sum(price * volume) / sum(volume) from open to now.
    Returns: {vwap, current_price, distance_pct, above_vwap}
    """
    try:
        df = _flatten(yf.download(ticker, period="1d", interval="1m", progress=False))
        if df.empty or len(df) < 5:
            return None

        df = df.dropna(subset=["Close", "Volume", "High", "Low"])
        # Typical price = (High + Low + Close) / 3
        df["typical_price"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["tp_vol"]         = df["typical_price"] * df["Volume"]

        cumvol = df["Volume"].cumsum()
        if cumvol.iloc[-1] == 0:
            return None

        vwap          = df["tp_vol"].cumsum().iloc[-1] / cumvol.iloc[-1]
        current_price = float(df["Close"].iloc[-1])
        distance_pct  = (current_price - vwap) / vwap * 100

        return {
            "vwap":          round(float(vwap), 4),
            "current_price": current_price,
            "distance_pct":  round(distance_pct, 3),
            "above_vwap":    current_price > vwap,
            "bars":          len(df),
        }
    except Exception as e:
        log.debug("get_vwap error for %s: %s", ticker, e)
        return None


def check_vwap_bounce(ticker: str, lookback_bars: int = 5) -> dict:
    """
    Detect a VWAP bounce — price was below VWAP, came back up to touch it,
    and the last bar closed above VWAP with positive momentum.

    Returns: {entry_confirmed, vwap, current_price, bounce_detected, note}
    """
    try:
        df = _flatten(yf.download(ticker, period="1d", interval="1m", progress=False))
        if df.empty or len(df) < lookback_bars + 5:
            return {"entry_confirmed": False, "note": "Insufficient data"}

        df = df.dropna(subset=["Close", "Volume", "High", "Low"])
        df["typical_price"] = (df["High"] + df["Low"] + df["Close"]) / 3
        df["tp_vol"]         = df["typical_price"] * df["Volume"]
        df["vwap"]           = df["tp_vol"].cumsum() / df["Volume"].cumsum()

        recent         = df.tail(lookback_bars)
        current_close  = float(df["Close"].iloc[-1])
        current_vwap   = float(df["vwap"].iloc[-1])
        prev_close     = float(df["Close"].iloc[-2]) if len(df) >= 2 else current_close
        distance_pct   = (current_close - current_vwap) / current_vwap * 100

        # Bounce detection: at least one of the last N bars was below VWAP
        # and current bar is now AT or ABOVE VWAP
        any_below_vwap = any(
            float(recent["Close"].iloc[i]) < float(recent["vwap"].iloc[i])
            for i in range(len(recent) - 1)
        )
        crossed_up = current_close >= current_vwap and prev_close < current_vwap

        # Near VWAP = within 0.3%
        near_vwap = abs(distance_pct) < 0.3

        bounce_detected = any_below_vwap and (crossed_up or (near_vwap and current_close > prev_close))

        # Momentum confirmation: last bar is green (close > open)
        last_open  = float(df["Open"].iloc[-1]) if "Open" in df.columns else current_close
        green_bar  = current_close > last_open

        entry_confirmed = bounce_detected and green_bar

        note = []
        if any_below_vwap: note.append("was below VWAP")
        if crossed_up:     note.append("crossed above VWAP")
        if near_vwap:      note.append(f"near VWAP ({distance_pct:+.2f}%)")
        if green_bar:      note.append("green bar")
        if not note:       note.append(f"above VWAP by {distance_pct:+.2f}%")

        return {
            "entry_confirmed": entry_confirmed,
            "bounce_detected": bounce_detected,
            "vwap":            round(current_vwap, 4),
            "current_price":   current_close,
            "distance_pct":    round(distance_pct, 3),
            "above_vwap":      current_close >= current_vwap,
            "green_bar":       green_bar,
            "note":            " | ".join(note),
        }

    except Exception as e:
        log.debug("check_vwap_bounce error for %s: %s", ticker, e)
        return {"entry_confirmed": False, "note": str(e)}


def check_vwap_entry(ticker: str, require_bounce: bool = True) -> dict:
    """
    Main entry point. Returns entry timing recommendation.
    require_bounce=True: must see price touch and bounce off VWAP
    require_bounce=False: just needs to be near VWAP (less strict)
    """
    if require_bounce:
        return check_vwap_bounce(ticker)
    else:
        vwap_data = get_vwap(ticker)
        if not vwap_data:
            return {"entry_confirmed": False, "note": "No VWAP data"}
        near = abs(vwap_data["distance_pct"]) < 0.5
        return {
            **vwap_data,
            "entry_confirmed": near and vwap_data["above_vwap"],
            "note": f"Price {'near' if near else 'away from'} VWAP ({vwap_data['distance_pct']:+.2f}%)",
        }


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for t in ["SPY", "AAPL", "MARA"]:
        v = check_vwap_entry(t)
        print(f"\n{t}: entry={v['entry_confirmed']} | {v.get('note','')}")
        if "vwap" in v:
            print(f"  VWAP=${v['vwap']} | Price=${v['current_price']} | dist={v['distance_pct']:+.2f}%")