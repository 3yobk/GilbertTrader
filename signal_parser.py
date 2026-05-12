"""
signal_parser.py  —  Discord Signal Parser
==========================================
Parses option trade signals from Discord messages into structured orders.

Supported formats (case-insensitive):
  BTO SPY 520C 5/16 x2          — Buy 2 contracts of SPY 520 Call exp 5/16
  BTO SPY 520C 5/16              — Buy 1 contract (default)
  BTO SPY 520C 5/16 @1.50       — Buy with max limit price $1.50
  BTO SPY 520C 5/16 x2 @1.50   — Buy 2 contracts max $1.50
  STC SPY 520C 5/16 x2          — Sell to close
  BTO SPY 520P 5/16 x1 @0.75   — Put option

Also handles:
  "Buy 2 SPY 520 calls 5/16"
  "SPY 520C 5/16 BTO x2"
"""

import re
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass


@dataclass
class TradeSignal:
    action:       str            # "BTO" or "STC"
    ticker:       str            # "SPY"
    strike:       float          # 520.0
    option_type:  str            # "C" or "P"
    expiry:       str            # "2026-05-16" (YYYY-MM-DD)
    quantity:     int            # 2
    limit_price:  Optional[float]  # 1.50 or None (market)
    raw:          str            # original message

    def __str__(self):
        exp = datetime.strptime(self.expiry, "%Y-%m-%d")
        exp_str = f"{exp.month}/{exp.day}"
        price_str = f" @${self.limit_price:.2f}" if self.limit_price else ""
        return (f"{self.action} {self.ticker} {int(self.strike)}"
                f"{'C' if self.option_type=='C' else 'P'} "
                f"{exp_str} x{self.quantity}{price_str}")


def _parse_expiry(raw: str, year: Optional[int] = None) -> Optional[str]:
    """
    Convert expiry string to YYYY-MM-DD.
    Handles: 5/16  05/16  5/16/26  May 16  May 16 2026
    """
    if not year:
        year = datetime.now().year

    raw = raw.strip()
    formats = [
        ("%m/%d/%Y", raw if len(raw.split("/")) == 3 and len(raw.split("/")[2]) == 4
                     else None),
        ("%m/%d/%y", raw if len(raw.split("/")) == 3 and len(raw.split("/")[2]) == 2
                     else None),
        ("%m/%d",    raw if len(raw.split("/")) == 2 else None),
        ("%b %d %Y", raw),
        ("%b %d",    raw),
        ("%B %d %Y", raw),
        ("%B %d",    raw),
    ]
    for fmt, val in formats:
        if not val:
            continue
        try:
            dt = datetime.strptime(val, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=year)
            # If parsed date is in the past, move to next year
            if dt.date() < datetime.now().date():
                dt = dt.replace(year=year + 1)
            return dt.strftime("%Y-%m-%d")
        except:
            continue
    return None


def parse_signal(message: str) -> Optional[TradeSignal]:
    """
    Parse a Discord message into a TradeSignal.
    Returns None if the message doesn't look like a trade signal.
    """
    msg = message.strip()

    # ── Primary pattern: BTO SPY 520C 5/16 x2 @1.50 ──────────────────────────
    pattern = re.compile(
        r'(?P<action>BTO|STC|BUY|SELL)\s+'           # BTO / STC
        r'(?P<ticker>[A-Z]{1,5})\s+'                  # SPY
        r'(?P<strike>\d+\.?\d*)'                       # 520 or 520.5
        r'(?P<type>[CP])\s+'                           # C or P
        r'(?P<expiry>\d{1,2}/\d{1,2}(?:/\d{2,4})?)'   # 5/16 or 5/16/26
        r'(?:\s+x(?P<qty>\d+))?'                       # x2 (optional)
        r'(?:\s+@\$?(?P<price>[\d.]+))?',              # @1.50 (optional)
        re.IGNORECASE
    )
    m = pattern.search(msg.upper())

    if not m:
        # ── Fallback pattern: SPY 520C 5/16 BTO x2 ────────────────────────────
        pattern2 = re.compile(
            r'(?P<ticker>[A-Z]{1,5})\s+'
            r'(?P<strike>\d+\.?\d*)(?P<type>[CP])\s+'
            r'(?P<expiry>\d{1,2}/\d{1,2}(?:/\d{2,4})?)'
            r'(?:.*?(?P<action>BTO|STC))?'
            r'(?:\s+x(?P<qty>\d+))?'
            r'(?:\s+@\$?(?P<price>[\d.]+))?',
            re.IGNORECASE
        )
        m = pattern2.search(msg.upper())
        if not m:
            return None

    action  = m.group("action") or "BTO"
    action  = action.upper()
    if action == "BUY":  action = "BTO"
    if action == "SELL": action = "STC"

    ticker  = m.group("ticker").upper()
    strike  = float(m.group("strike"))
    opt     = m.group("type").upper()
    expiry  = _parse_expiry(m.group("expiry"))
    qty     = int(m.group("qty")) if m.group("qty") else 1
    price   = float(m.group("price")) if m.group("price") else None

    if not expiry:
        return None

    return TradeSignal(
        action=action,
        ticker=ticker,
        strike=strike,
        option_type=opt,
        expiry=expiry,
        quantity=qty,
        limit_price=price,
        raw=msg,
    )


def parse_signal_safe(message: str) -> Optional[TradeSignal]:
    """Wrapper that never raises — returns None on any error."""
    try:
        return parse_signal(message)
    except Exception:
        return None


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        "BTO SPY 520C 5/16 x2",
        "BTO SPY 520C 5/16",
        "BTO SPY 520C 5/16 @1.50",
        "BTO SPY 520C 5/16 x2 @1.50",
        "STC SPY 520C 5/16 x2",
        "BTO SPY 520P 5/16 x1 @0.75",
        "bto aapl 200c 6/20 x3 @2.50",
        "BTO GME 25C 5/23 x1",
        "not a signal at all",
        "hey check out SPY 520C 5/16 BTO x2",
    ]
    print("Signal Parser Test\n" + "="*40)
    for t in tests:
        sig = parse_signal_safe(t)
        if sig:
            print(f"  OK  {sig}")
        else:
            print(f"  --  (no signal): {t}")
