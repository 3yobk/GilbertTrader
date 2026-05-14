"""
test_mode.py — GilbertTrader Test Mode (E. from debug notes)
=============================================================
Runs a full dry-run of the signal pipeline:
  1. Parse the signal
  2. Look up exact optionId + expiry
  3. Fetch real bid/ask from Webull
  4. Calculate midpoint limit price
  5. Check budget + spread
  6. Show full decision — WOULD TRADE or BLOCKED

Never sends an actual order. Safe to run any time.

Usage:
  python test_mode.py "BTO NVDA 220C 5/22 x1"
  python test_mode.py "BTO HOOD 80C 5/22"
  python test_mode.py          <- prompts for signal
"""

import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

def run_test(signal_text: str = None):
    if not signal_text:
        signal_text = input("Enter signal to test: ").strip()
    if not signal_text:
        print("No signal provided.")
        return

    print(f"\n{'='*60}")
    print(f"  TEST MODE — dry run, no orders sent")
    print(f"  Signal: {signal_text}")
    print(f"{'='*60}\n")

    # Step 1: Parse
    from signal_parser import parse_signal_safe
    sig = parse_signal_safe(signal_text)
    if not sig:
        print("  FAIL: Could not parse signal")
        return
    print(f"  [1] Parsed:   {sig.action} {sig.ticker} {sig.strike}{sig.option_type}")
    print(f"               Expiry: {sig.expiry} | Qty: {sig.quantity}")

    # Step 2: Open browser + API
    print(f"\n  [2] Connecting to Webull API...")
    from webull_engine import WebullEngine
    engine = WebullEngine(paper=True)
    engine.open_browser()
    if not engine._api_ready:
        print("  FAIL: Webull API not ready")
        engine.close()
        return
    print(f"       API ready | account={engine._wb._account_id}")

    # Step 3: Option lookup (exact expiry only)
    print(f"\n  [3] Looking up option chain...")
    ok = engine.navigate_to_option(
        sig.ticker, sig.expiry, sig.strike, sig.option_type)
    avail = getattr(engine, "_last_available_expiries", [])
    if not ok or not engine._pending_option_id:
        print(f"  BLOCKED: Expiry {sig.expiry} not available for {sig.ticker}")
        if avail:
            print(f"           Available expiries: {', '.join(avail)}")
            print(f"           Try one of these in your signal instead.")
        engine.close()
        return
    print(f"       tickerId: {engine._pending_option_id}")
    print(f"       Expiry:   {sig.expiry} ✓ (exact match)")
    if avail:
        print(f"       All available: {', '.join(avail)}")

    # Step 4: Get real quote
    print(f"\n  [4] Fetching live bid/ask...")
    quote = engine.get_option_quote(sig.ticker)
    if not quote:
        print("  BLOCKED: No quote returned — market closed or API error")
        print("           (This is expected outside market hours 9:30-4PM ET)")
        engine.close()
        return

    bid  = float(quote.get("bid") or 0)
    ask  = float(quote.get("ask") or 0)
    last = float(quote.get("last") or 0)
    print(f"       Bid:  ${bid:.2f}")
    print(f"       Ask:  ${ask:.2f}")
    print(f"       Last: ${last:.2f}")

    if bid == 0 or ask == 0:
        print("  BLOCKED: Bid or ask is $0 — market closed or no liquidity")
        print("           Run this again during market hours to see real prices")
        engine.close()
        return

    # Step 5: Calculate limit price + checks
    print(f"\n  [5] Checks...")
    mid        = round((bid + ask) / 2, 2)
    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
    est_cost   = mid * 100 * sig.quantity
    budget     = float(os.getenv("TEST_BUDGET", "300"))
    max_spread = 0.20

    print(f"       Mid price:  ${mid:.2f}")
    print(f"       Spread:     {spread_pct:.1%} (max {max_spread:.0%})")
    print(f"       Est. cost:  ${est_cost:.0f} ({sig.quantity} contract(s))")
    print(f"       Budget:     ${budget:.0f}")

    blocked = []
    if spread_pct > max_spread:
        blocked.append(f"Spread {spread_pct:.1%} > {max_spread:.0%}")
    if est_cost > budget:
        blocked.append(f"Cost ${est_cost:.0f} > budget ${budget:.0f}")

    # Step 6: Decision
    print(f"\n{'='*60}")
    if blocked:
        print(f"  RESULT: ❌ BLOCKED")
        for b in blocked:
            print(f"          Reason: {b}")
    else:
        print(f"  RESULT: ✅ WOULD TRADE")
        print(f"          BTO {sig.ticker} {sig.strike}{sig.option_type} {sig.expiry}")
        print(f"          Limit: ${mid:.2f} | Cost: ${est_cost:.0f} | tickerId: {engine._pending_option_id}")
    print(f"{'='*60}\n")

    engine.close()

if __name__ == "__main__":
    signal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    run_test(signal)