"""
paper_trade_test.py -- Standalone paper order test
Run this at market hours (9:30-4:00 PM ET) to confirm the full
paper order pipeline works before running main.py live.

Usage:
    python paper_trade_test.py
    python paper_trade_test.py "BTO SPY 580C 5/23 x1"
"""

import sys
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

DEFAULT_SIGNAL = "BTO SPY 580C 5/23 x1"
signal_str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SIGNAL

print("\n" + "=" * 60)
print("  GilbertTrader -- Paper Trade Test")
print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
print("=" * 60)

# Step 1: Parse signal
print(f"\n[1] Parsing: {signal_str}")
from signal_parser import parse_signal_safe
parsed = parse_signal_safe(signal_str)
if not parsed:
    print("  FAIL -- Could not parse signal")
    sys.exit(1)
print(f"  OK  ticker={parsed.ticker}  strike={parsed.strike}  "
      f"type={parsed.option_type}  expiry={parsed.expiry}  qty={parsed.quantity}")

# Step 2: Market open
print("\n[2] Checking market hours...")
from market_feed import MarketFeed
market = MarketFeed()
market.start()
time.sleep(2)
if not market.is_market_open():
    mins = market.minutes_to_open()
    print(f"  FAIL -- Market CLOSED. Opens in ~{mins} min (9:30-4:00 PM ET).")
    market.stop()
    sys.exit(1)
print("  OK  Market is open")

# Step 3: Live quote
print(f"\n[3] Live quote for {parsed.ticker}...")
quote = market.get_quote(parsed.ticker)
if not quote:
    print(f"  WARN -- No quote for {parsed.ticker}, continuing anyway")
else:
    print(f"  OK  last=${quote.get('last')}  bid=${quote.get('bid')}  ask=${quote.get('ask')}")

# Step 4: Webull login
print("\n[4] Connecting to Webull (paper mode)...")
from webull_engine import WebullEngine
webull = WebullEngine(paper=True)
webull.open_browser()
time.sleep(3)
print("  OK  Webull ready")

# Step 5: Option lookup
print(f"\n[5] Looking up option: {parsed.ticker} {parsed.strike}{parsed.option_type} {parsed.expiry}...")
ok = webull.navigate_to_option(parsed.ticker, parsed.expiry, parsed.strike, parsed.option_type)
if not ok:
    print("  FAIL -- Option not found.")
    if webull._last_available_expiries:
        print(f"  Available expiries: {', '.join(webull._last_available_expiries)}")
        nearest = webull._last_available_expiries[0]
        mm_dd = nearest[5:].replace("-", "/")
        print(f"  Try: python paper_trade_test.py "
              f"\"BTO {parsed.ticker} {int(parsed.strike)}{parsed.option_type} {mm_dd} x1\"")
    webull.close()
    market.stop()
    sys.exit(1)
print(f"  OK  tickerId={webull._pending_option_id}")

# Step 6: Option bid/ask
print("\n[6] Option bid/ask...")
opt_quote = webull.get_option_quote(parsed.ticker)
limit_price = None
if not opt_quote:
    print("  WARN -- No option quote (market may have just opened)")
else:
    bid = opt_quote.get("bid", 0)
    ask = opt_quote.get("ask", 0)
    limit_price = round((bid + ask) / 2, 2) if bid and ask else None
    est_cost = limit_price * 100 * parsed.quantity if limit_price else None
    print(f"  OK  bid=${bid}  ask=${ask}  mid=${limit_price}  est_cost=${est_cost}")

# Step 7: Place paper order
print("\n[7] Placing paper BTO order...")
fill = webull.place_order(action="BTO", quantity=parsed.quantity,
                          limit_price=limit_price, paper=True)

confirmed = webull.last_order_confirmed
if confirmed:
    print(f"  OK  Order CONFIRMED -- fill=${fill}")
    print("\n  Check Webull app -> Paper Account -> Orders tab to verify.")
else:
    print("  FAIL -- Order NOT confirmed by Webull API")
    print("  Common causes:")
    print("    - Market closed (417 error)")
    print("    - Webull session expired -- log in via browser and restart")
    print("    - Strike/expiry invalid for this ticker")

print("\n" + "=" * 60)
print(f"  Result: {'PASS' if confirmed else 'FAIL'}")
print("=" * 60 + "\n")

webull.close()
market.stop()
sys.exit(0 if confirmed else 1)
