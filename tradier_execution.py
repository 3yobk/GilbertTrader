"""
tradier_execution.py — Tradier Options Execution Failover
==========================================================
Provides a backup execution path when Webull is unavailable.

Tradier supports options order placement via their brokerage API.
When webull_engine.place_order() fails or times out, main.py calls
tradier_execution.place_order() as a fallback.

Requires: TRADIER_TOKEN and TRADIER_ACCOUNT_ID in .env
          Tradier account with options trading enabled

Order flow:
  1. Parse BTO signal string into components
  2. Look up option symbol (OCC format) from Tradier chain
  3. Place limit order at mid-price via Tradier brokerage API
  4. Return fill price or None on failure
"""

import os
import re
import logging
import requests
from typing import Optional
from datetime import datetime, date

log = logging.getLogger("tradier_exec")

_TRADIER_TOKEN   = os.getenv("TRADIER_TOKEN", "")
_TRADIER_ACCOUNT = os.getenv("TRADIER_ACCOUNT_ID", "")
_BASE_URL        = "https://api.tradier.com/v1"

# Toggle: set False to disable Tradier failover without removing the module
FAILOVER_ENABLED = bool(_TRADIER_TOKEN and _TRADIER_ACCOUNT)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_TRADIER_TOKEN}",
        "Accept":        "application/json",
    }


def _get_option_symbol(ticker: str, strike: float,
                       option_type: str, expiry_str: str) -> Optional[str]:
    """
    Look up the OCC option symbol for a given contract.
    expiry_str format: "6/5" or "06/05" (month/day, current year)

    OCC format: TICKER + YYMMDD + C/P + 8-digit strike (5 before decimal, 3 after)
    Example: AAPL  2026-06-05  C  $312.50 → AAPL260605C00312500
    """
    try:
        parts = expiry_str.split("/")
        month = int(parts[0])
        day   = int(parts[1])
        year  = date.today().year
        exp_dt = f"{year}-{month:02d}-{day:02d}"

        r = requests.get(
            f"{_BASE_URL}/markets/options/chains",
            headers=_headers(),
            params={"symbol": ticker, "expiration": exp_dt, "greeks": "false"},
            timeout=6,
        )
        if not r.ok:
            log.warning("Tradier chain lookup failed: %s %s", r.status_code, r.text[:100])
            return None

        chain = r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]

        otype = "call" if option_type.upper() == "C" else "put"
        for opt in chain:
            if (opt.get("option_type") == otype and
                    abs(float(opt.get("strike", 0)) - strike) < 0.01):
                symbol = opt.get("symbol")
                log.info("Tradier OCC symbol: %s → %s", ticker, symbol)
                return symbol

        log.warning("No Tradier contract found: %s %s%s exp %s", ticker, strike, option_type, expiry_str)
        return None

    except Exception as e:
        log.debug("Tradier symbol lookup error: %s", e)
        return None


def _get_mid_price(ticker: str, strike: float,
                   option_type: str, expiry_str: str) -> Optional[float]:
    """Get bid/ask mid for limit order pricing."""
    try:
        parts = expiry_str.split("/")
        month = int(parts[0])
        day   = int(parts[1])
        year  = date.today().year
        exp_dt = f"{year}-{month:02d}-{day:02d}"

        r = requests.get(
            f"{_BASE_URL}/markets/options/chains",
            headers=_headers(),
            params={"symbol": ticker, "expiration": exp_dt, "greeks": "false"},
            timeout=6,
        )
        if not r.ok:
            return None

        chain = r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]

        otype = "call" if option_type.upper() == "C" else "put"
        for opt in chain:
            if (opt.get("option_type") == otype and
                    abs(float(opt.get("strike", 0)) - strike) < 0.01):
                bid = float(opt.get("bid") or 0)
                ask = float(opt.get("ask") or 0)
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
        return None
    except Exception:
        return None


def place_order(signal_str: str, paper: bool = False) -> Optional[float]:
    """
    Place a BTO options order via Tradier as Webull failover.

    signal_str format: "BTO AAPL 312.5C 6/5 x2"

    Returns: fill price (float) if successful, None if failed.
    paper=True: logs the order but does not send to Tradier API.
    """
    if not FAILOVER_ENABLED:
        log.warning("Tradier failover disabled — TRADIER_TOKEN or TRADIER_ACCOUNT_ID not set")
        return None

    try:
        # Parse signal string
        parts = signal_str.strip().split()
        if len(parts) < 4 or parts[0] != "BTO":
            log.error("Tradier failover: cannot parse signal '%s'", signal_str)
            return None

        ticker      = parts[1].upper()
        contract    = parts[2]                          # e.g. "312.5C"
        expiry_str  = parts[3]                          # e.g. "6/5"
        qty_str     = parts[4] if len(parts) > 4 else "x1"
        quantity    = int(re.sub(r"[^0-9]", "", qty_str) or "1")

        # Extract strike and type from contract
        opt_type    = contract[-1].upper()              # C or P
        strike_str  = contract[:-1]
        strike      = float(strike_str)

        # Get OCC symbol and mid price
        occ_symbol  = _get_option_symbol(ticker, strike, opt_type, expiry_str)
        mid_price   = _get_mid_price(ticker, strike, opt_type, expiry_str)

        if not occ_symbol:
            log.error("Tradier failover: no OCC symbol for %s %s%s exp %s",
                      ticker, strike, opt_type, expiry_str)
            return None

        if not mid_price:
            log.warning("Tradier failover: no mid price for %s — using $0.01 limit", ticker)
            mid_price = 0.01

        # Slight aggression on limit: mid + 1¢ to improve fill probability
        lmt_price = round(mid_price + 0.01, 2)

        log.info("Tradier failover ORDER: %s BTO %s x%d @ $%.2f limit",
                 occ_symbol, ticker, quantity, lmt_price)

        if paper:
            log.info("Tradier failover (PAPER): order logged, not sent")
            return lmt_price

        # Place the order
        r = requests.post(
            f"{_BASE_URL}/accounts/{_TRADIER_ACCOUNT}/orders",
            headers={**_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "class":        "option",
                "symbol":       ticker,
                "option_symbol": occ_symbol,
                "side":         "buy_to_open",
                "quantity":     str(quantity),
                "type":         "limit",
                "price":        str(lmt_price),
                "duration":     "day",
            },
            timeout=10,
        )

        if r.ok:
            resp = r.json()
            order_id = resp.get("order", {}).get("id")
            status   = resp.get("order", {}).get("status")
            log.info("Tradier order placed: id=%s status=%s", order_id, status)
            return lmt_price   # return limit price as estimated fill

        else:
            log.error("Tradier order failed %s: %s", r.status_code, r.text[:200])
            return None

    except Exception as e:
        log.error("Tradier failover exception: %s", e)
        return None


def is_available() -> bool:
    """Quick connectivity check — returns True if Tradier API is reachable."""
    if not FAILOVER_ENABLED:
        return False
    try:
        r = requests.get(
            f"{_BASE_URL}/user/profile",
            headers=_headers(),
            timeout=4,
        )
        return r.ok
    except Exception:
        return False
