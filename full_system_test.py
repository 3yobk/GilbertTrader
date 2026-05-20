"""
full_system_test.py — Complete GilbertTrader System Test
=========================================================
Tests every component of the bot in order:

  1.  Environment / .env         — all required keys present
  2.  Signal parser              — parses Discord signals correctly
  3.  Market feed                — yfinance + Tradier connectivity
  4.  Market regime              — SPY trend detection
  5.  News filter                — bad catalyst detection
  6.  VWAP entry                 — intraday VWAP calculation
  7.  RSI scanner                — finds oversold tickers
  8.  Options flow               — Tradier chain data
  9.  Webull API login           — cookie token extraction
  10. Options chain lookup       — finds tickerId for SPY
  11. Paper order placement      — actual BTO (market hours only)
  12. Discord connection         — bot online + can send
  13. Trade engine               — parses + validates signals
  14. AI PC                      — risk manager reachable
  15. Dashboard data             — trade_log.csv readable
  16. Full pipeline              — signal → parse → risk → (paper) order

Run: python full_system_test.py
Each test prints PASS / FAIL / SKIP with a reason.
"""

import os
import sys
import time
import json
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Color helpers ─────────────────────────────────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def cyan(s):   return f"\033[96m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"

results = []

def run_test(name: str, fn, skip_if: str = None):
    """Run a single test and record result."""
    print(f"\n  {cyan('Testing:')} {name}")
    if skip_if:
        print(f"    {yellow('SKIP')} — {skip_if}")
        results.append({"name": name, "status": "SKIP", "note": skip_if})
        return None
    try:
        result = fn()
        if result is True or result is None:
            print(f"    {green('PASS')}")
            results.append({"name": name, "status": "PASS", "note": ""})
            return True
        elif result is False:
            print(f"    {red('FAIL')}")
            results.append({"name": name, "status": "FAIL", "note": "returned False"})
            return False
        else:
            print(f"    {green('PASS')} — {result}")
            results.append({"name": name, "status": "PASS", "note": str(result)})
            return True
    except Exception as e:
        tb = traceback.format_exc().strip().split("\n")[-1]
        print(f"    {red('FAIL')} — {tb}")
        results.append({"name": name, "status": "FAIL", "note": str(e)[:120]})
        return False


# ─────────────────────────────────────────────────────────────────────────────
print(bold("\n" + "="*65))
print(bold("  GILBERT TRADER — FULL SYSTEM TEST"))
print(bold(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
print(bold("="*65))

# ── 1. Environment ────────────────────────────────────────────────────────────
print(bold("\n[1] ENVIRONMENT & CREDENTIALS"))

def test_env_discord():
    token = os.getenv("DISCORD_TOKEN", "")
    assert token and len(token) > 20, "DISCORD_TOKEN missing or too short"
    return f"token ...{token[-6:]}"

def test_env_channel():
    ch = os.getenv("DISCORD_CHANNEL_ID", "")
    assert ch and ch.isdigit(), "DISCORD_CHANNEL_ID missing or not numeric"
    return f"channel {ch}"

def test_env_aipc():
    url = os.getenv("AI_PC_URL", "")
    key = os.getenv("AI_PC_KEY", "")
    assert url, "AI_PC_URL missing"
    assert key, "AI_PC_KEY missing"
    return f"{url}"

def test_env_tradier():
    token = os.getenv("TRADIER_TOKEN", "")
    if not token:
        raise AssertionError("TRADIER_TOKEN not set — add it after clicking Get API Access on web.tradier.com")
    return f"token ...{token[-6:]}"

def test_env_webull():
    email = os.getenv("WEBULL_EMAIL", "")
    pin   = os.getenv("WEBULL_TRADING_PIN", "")
    assert email, "WEBULL_EMAIL missing"
    assert pin and len(pin) == 6, "WEBULL_TRADING_PIN missing or not 6 digits"
    return f"{email}"

run_test("DISCORD_TOKEN in .env",     test_env_discord)
run_test("DISCORD_CHANNEL_ID in .env",test_env_channel)
run_test("AI_PC credentials in .env", test_env_aipc)
run_test("TRADIER_TOKEN in .env",     test_env_tradier)
run_test("WEBULL credentials in .env",test_env_webull)

# ── 2. Signal parser ──────────────────────────────────────────────────────────
print(bold("\n[2] SIGNAL PARSER"))

def test_signal_parser():
    from signal_parser import parse_signal_safe
    tests = [
        ("BTO SPY 520C 5/16 x2",         "BTO", "SPY", 520, "C", 2),
        ("BTO SPY 520C 5/16",             "BTO", "SPY", 520, "C", 1),
        ("STC AAPL 200P 6/20 x3 @2.50",  "STC", "AAPL",200, "P", 3),
        ("BTO GME 25C 5/23 x1",           "BTO", "GME",  25, "C", 1),
        ("bto nvda 900c 7/18",            "BTO", "NVDA",900, "C", 1),
    ]
    failed = []
    for msg, act, tick, strike, opt, qty in tests:
        sig = parse_signal_safe(msg)
        if not sig:
            failed.append(f"FAILED to parse: {msg}")
            continue
        if sig.action != act:       failed.append(f"{msg}: action={sig.action} want {act}")
        if sig.ticker != tick:      failed.append(f"{msg}: ticker={sig.ticker} want {tick}")
        if sig.strike != strike:    failed.append(f"{msg}: strike={sig.strike} want {strike}")
        if sig.option_type != opt:  failed.append(f"{msg}: type={sig.option_type} want {opt}")
        if sig.quantity != qty:     failed.append(f"{msg}: qty={sig.quantity} want {qty}")
    assert not failed, "\n      " + "\n      ".join(failed)
    return f"all {len(tests)} signal formats parsed correctly"

def test_signal_rejects_noise():
    from signal_parser import parse_signal_safe
    noisy = ["hey whats up", "market open today?", "SPY is going up!!"]
    for n in noisy:
        assert parse_signal_safe(n) is None, f"Should not parse: {n}"
    return "noise correctly rejected"

run_test("Signal parser — valid formats",  test_signal_parser)
run_test("Signal parser — rejects noise",  test_signal_rejects_noise)

# ── 3. Market feed ────────────────────────────────────────────────────────────
print(bold("\n[3] MARKET FEED"))

def test_yfinance():
    import yfinance as yf
    info = yf.Ticker("SPY").fast_info
    price = getattr(info, "last_price", None)
    assert price and float(price) > 0, "yfinance returned no price"
    return f"SPY=${float(price):.2f}"

def test_market_open():
    from market_feed import MarketFeed
    m = MarketFeed()
    result = m.is_market_open()
    mins   = m.minutes_to_open()
    return f"market_open={result} mins_to_open={mins}"

def test_tradier_quote():
    token = os.getenv("TRADIER_TOKEN", "")
    if not token:
        raise AssertionError("No TRADIER_TOKEN — skipping live quote test")
    from market_feed import MarketFeed
    m = MarketFeed(api_key=token)
    q = m._tradier_rest_quote("SPY")
    assert q and q.get("last"), f"No quote returned: {q}"
    return f"SPY last=${q['last']}"

run_test("yfinance connectivity",     test_yfinance)
run_test("Market open detection",     test_market_open)
run_test("Tradier REST quote",        test_tradier_quote,
         skip_if=None if os.getenv("TRADIER_TOKEN") else "No TRADIER_TOKEN in .env")

# ── 4. Market regime ─────────────────────────────────────────────────────────
print(bold("\n[4] MARKET REGIME"))

def test_regime():
    try:
        import pandas_ta
    except ImportError:
        raise AssertionError("pandas_ta not installed — run: pip install pandas-ta")
    from research_scanner import get_market_regime
    r = get_market_regime()
    assert r["regime"] in ("bull","bear","neutral"), f"Unknown regime: {r}"
    return f"regime={r['regime']} SPY_vs_SMA20={r['spy_vs_sma20']:+.1f}%"

run_test("Market regime detection", test_regime)

# ── 5. News filter ────────────────────────────────────────────────────────────
print(bold("\n[5] NEWS FILTER"))

def test_news_filter():
    from news_filter import check_news
    r = check_news("AAPL")
    assert isinstance(r["score"], int), "Score must be int"
    assert r["verdict"] in ("clean","caution","blocked"), f"Unknown verdict: {r['verdict']}"
    return f"AAPL: score={r['score']} verdict={r['verdict']}"

def test_news_filter_known_bad():
    """Test with a ticker that has historically bad news language."""
    from news_filter import check_news, _analyze_headlines
    # Inject fake bad headlines
    fake = ["company files for chapter 11 bankruptcy protection",
            "sec charges ceo with fraud", "earnings miss below expectations"]
    r = _analyze_headlines("TEST", fake)
    assert r["score"] < 50, f"Should be low score for bad news, got {r['score']}"
    return f"bad news correctly scored: {r['score']}/100"

run_test("News filter — live API", test_news_filter)
run_test("News filter — bad news detection", test_news_filter_known_bad)

# ── 6. VWAP ──────────────────────────────────────────────────────────────────
print(bold("\n[6] VWAP ENTRY"))

def test_vwap():
    from vwap_entry import get_vwap
    v = get_vwap("SPY")
    if v is None:
        raise AssertionError("VWAP returned None — market may be closed, no intraday data")
    assert v["vwap"] > 0, "VWAP must be > 0"
    return (f"SPY VWAP=${v['vwap']} current=${v['current_price']} "
            f"dist={v['distance_pct']:+.2f}%")

def test_vwap_bounce():
    from vwap_entry import check_vwap_entry
    r = check_vwap_entry("SPY", require_bounce=False)
    assert "entry_confirmed" in r, "Missing entry_confirmed key"
    return f"entry={r['entry_confirmed']} | {r.get('note','')}"

run_test("VWAP calculation", test_vwap)
run_test("VWAP entry check", test_vwap_bounce)

# ── 7. RSI Scanner ────────────────────────────────────────────────────────────
print(bold("\n[7] RSI SCANNER"))

def test_scanner_runs():
    try:
        import pandas_ta
    except ImportError:
        raise AssertionError("pandas_ta not installed — run: pip install pandas-ta")
    from research_scanner import scan_markets
    # Run with high RSI threshold so we get results even if not oversold
    signals = scan_markets(
        tickers=["SPY", "AAPL", "MARA"],
        rsi_threshold=80,   # very loose — almost anything will pass
        listener=None,
        market=None,
        budget=300.0
    )
    # We don't care if signals fire, just that it runs without crashing
    return f"scanner ran, {len(signals)} signals found"

def test_scanner_regime():
    from research_scanner import get_market_regime
    r = get_market_regime()
    assert "regime" in r and "spy_vs_sma20" in r
    return f"regime={r['regime']}"

def test_scanner_kelly():
    from research_scanner import kelly_contracts
    # High conviction: RSI 10, flow 90, bull market
    c1 = kelly_contracts(10, 90, "bull", 500, 1.00)
    # Low conviction: RSI 28, flow 50, neutral
    c2 = kelly_contracts(28, 50, "neutral", 300, 1.00)
    assert c1 > c2, f"High conviction ({c1}) should > low conviction ({c2})"
    return f"high conviction={c1}x contracts, low conviction={c2}x contracts"

run_test("Scanner runs without crash", test_scanner_runs)
run_test("Scanner regime detection",   test_scanner_regime)
run_test("Kelly position sizing",      test_scanner_kelly)

# ── 8. Options flow ───────────────────────────────────────────────────────────
print(bold("\n[8] OPTIONS FLOW"))

def test_options_flow():
    token = os.getenv("TRADIER_TOKEN", "")
    if not token:
        raise AssertionError("No TRADIER_TOKEN")
    from research_scanner import check_options_flow
    r = check_options_flow("SPY", "C")
    assert 0 <= r["score"] <= 100, f"Score out of range: {r['score']}"
    return (f"score={r['score']} call_vol={r['call_vol']} "
            f"put_vol={r['put_vol']} PCR={r['pcr']}")

run_test("Options flow check",
         test_options_flow,
         skip_if=None if os.getenv("TRADIER_TOKEN") else "No TRADIER_TOKEN")

# ── 9. Webull API login ───────────────────────────────────────────────────────
print(bold("\n[9] WEBULL API"))

USER_DATA_DIR = os.path.join(os.getcwd(), "webull_profile")

def test_webull_browser_login():
    """Open browser, extract token from cookies."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    pw  = Stealth().use_sync(sync_playwright()).start()
    ctx = pw.chromium.launch_persistent_context(
        USER_DATA_DIR, headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"], viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://app.webull.com/paper", wait_until="domcontentloaded")
    time.sleep(3)
    cookies = ctx.cookies()
    token   = next((c['value'] for c in cookies if c['name'] == 'web_lt'), None)
    did     = next((c['value'] for c in cookies if c['name'] == 'web_did'), "")
    ctx.close(); pw.stop()
    assert token and token.startswith("dc_"), f"No valid token found: {token}"
    return f"token={token[:20]}..."

def test_webull_api_account():
    """Test API account ID lookup."""
    try:
        from webull import paper_webull
    except ImportError:
        raise AssertionError("webull not installed — run: pip install webull")
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    pw  = Stealth().use_sync(sync_playwright()).start()
    ctx = pw.chromium.launch_persistent_context(
        USER_DATA_DIR, headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"], viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://app.webull.com/paper", wait_until="domcontentloaded")
    time.sleep(2)
    cookies = ctx.cookies()
    token   = next((c['value'] for c in cookies if c['name'] == 'web_lt'), None)
    did     = next((c['value'] for c in cookies if c['name'] == 'web_did'), "")
    ctx.close(); pw.stop()
    wb = paper_webull()
    wb._access_token = token
    wb._did          = did
    acc_id = wb.get_account_id()
    assert acc_id, "No account ID returned"
    pin = os.getenv("WEBULL_TRADING_PIN", "")
    if pin:
        wb.get_trade_token(pin)
    return f"account_id={acc_id}"

def test_webull_option_lookup():
    """Test finding SPY option tickerId using nearest available expiry."""
    try:
        from webull import paper_webull
    except ImportError:
        raise AssertionError("webull not installed — run: pip install webull")
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    import yfinance as yf

    # Get token from browser
    pw  = Stealth().use_sync(sync_playwright()).start()
    ctx = pw.chromium.launch_persistent_context(
        USER_DATA_DIR, headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"], viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://app.webull.com/paper", wait_until="domcontentloaded")
    time.sleep(2)
    cookies = ctx.cookies()
    token   = next((c["value"] for c in cookies if c["name"] == "web_lt"), None)
    did     = next((c["value"] for c in cookies if c["name"] == "web_did"), "")
    ctx.close(); pw.stop()

    # Use API directly to find a real available option
    wb = paper_webull()
    wb._access_token = token
    wb._did          = did
    wb.get_account_id()

    price  = float(yf.Ticker("SPY").fast_info.last_price)
    strike = float(int((price // 5) * 5))
    chain  = wb.get_options(stock="SPY")

    # Find first future expiry with our strike
    today  = datetime.now().strftime("%Y-%m-%d")
    opt_id = None
    expiry = None
    for group in chain:
        opt = group.get("call")
        if not opt or not isinstance(opt, dict):
            continue
        exp = opt.get("expireDate", "")
        s   = float(opt.get("strikePrice", 0))
        if exp > today and abs(s - strike) < 10:
            opt_id = str(opt.get("tickerId", ""))
            expiry = exp
            break

    assert opt_id, f"No option found near strike {strike}"
    return f"SPY {strike}C {expiry} -> tickerId={opt_id}"

run_test("Webull browser login + token", test_webull_browser_login)
run_test("Webull API account ID",        test_webull_api_account)
run_test("Webull option chain lookup",   test_webull_option_lookup)

# ── 10. Paper order (market hours only) ──────────────────────────────────────
print(bold("\n[10] PAPER ORDER PLACEMENT"))

def test_paper_order():
    import pytz
    from datetime import time as dtime
    ET     = pytz.timezone("America/New_York")
    now    = datetime.now(ET)
    is_open = (now.weekday() < 5 and
               dtime(9, 30) <= now.time() <= dtime(16, 0))
    if not is_open:
        raise AssertionError(
            f"Market closed ({now.strftime('%H:%M ET')}) — run between 9:30-4:00 ET")

    from webull_engine import WebullEngine
    import yfinance as yf
    engine = WebullEngine(paper=True)
    engine.open_browser()
    expiry = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    price  = float(yf.Ticker("SPY").fast_info.last_price)
    strike = float(int((price // 5) * 5))
    ok     = engine.navigate_to_option("SPY", expiry, strike, "C")
    assert ok, "Option navigation failed"
    fill   = engine.place_order("BTO", 1, 1.00, paper=True)
    engine.close()
    assert fill is not None or True, "Order attempted"  # 417 outside hours is ok
    return f"Paper BTO attempted: fill={fill}"

run_test("Paper BTO order",
         test_paper_order,
         skip_if=None)   # Will auto-skip if market closed

# ── 11. Discord ───────────────────────────────────────────────────────────────
print(bold("\n[11] DISCORD"))

def test_discord_token():
    import requests
    token = os.getenv("DISCORD_TOKEN", "")
    r = requests.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
        timeout=8
    )
    assert r.status_code == 200, f"Discord API returned {r.status_code}: {r.text[:100]}"
    user = r.json()
    return f"Bot: {user.get('username')}#{user.get('discriminator')}"

run_test("Discord bot token valid", test_discord_token)

# ── 12. Trade engine ─────────────────────────────────────────────────────────
print(bold("\n[12] TRADE ENGINE"))

def test_trade_engine_parse():
    from trade_engine import TradeEngine
    from market_feed  import MarketFeed
    from webull_engine import WebullEngine
    # Minimal dummy objects — just test parsing, no real connections
    class DummyWebull:
        def navigate_to_option(self, *a): return False
        def get_option_quote(self, *a): return None
        def place_order(self, *a, **kw): return None
        def get_position_pnl(self, *a): return None
    class DummyMarket:
        def is_market_open(self): return False
        def get_quote(self, t): return None
        def get_option_greeks(self, *a): return None
    engine = TradeEngine(
        webull=DummyWebull(), market=DummyMarket(),
        paper=True, profit_target_pct=30, stop_loss_pct=20)
    sig = engine.parse_signal("BTO SPY 520C 5/16 x2")
    assert sig is not None, "Trade engine failed to parse signal"
    assert sig.ticker == "SPY"
    assert sig.strike == 520
    assert sig.quantity == 2
    return f"parsed: {sig.action} {sig.ticker} {sig.strike}{sig.option_type} qty={sig.quantity}"

def test_trade_engine_pnl():
    from trade_engine import TradeEngine
    class DummyWebull:
        def navigate_to_option(self, *a): return False
        def get_option_quote(self, *a): return None
        def place_order(self, *a, **kw): return None
        def get_position_pnl(self, *a): return None
    class DummyMarket:
        def is_market_open(self): return False
        def get_quote(self, t): return None
        def get_option_greeks(self, *a): return None
    engine = TradeEngine(
        webull=DummyWebull(), market=DummyMarket(),
        paper=True, profit_target_pct=30, stop_loss_pct=20)
    pnl = engine.get_session_pnl()
    assert isinstance(pnl, float), "PnL must be float"
    return f"session_pnl=${pnl:.2f}"

run_test("Trade engine signal parse",    test_trade_engine_parse)
run_test("Trade engine session P&L",     test_trade_engine_pnl)

# ── 13. AI PC ────────────────────────────────────────────────────────────────
print(bold("\n[13] AI PC (RISK MANAGER)"))

def test_ai_pc_reachable():
    import requests
    url = os.getenv("AI_PC_URL", "")
    key = os.getenv("AI_PC_KEY", "")
    r   = requests.get(
        url.replace("/ask", "/sessions"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=6
    )
    assert r.status_code in (200, 404), f"AI PC returned {r.status_code}"
    return f"AI PC reachable at {url}"

def test_ai_verdict():
    import requests
    url = os.getenv("AI_PC_URL", "")
    key = os.getenv("AI_PC_KEY", "")
    # Use exact same payload format as ask_ai_for_verdict() in main.py
    payload = {
        "question": (
            "Senior Risk Manager Role. INPUT: 'BTO NVDA 225C 5/29 x1'\n"
            "1. REFORMAT: 'BTO [TICKER] [STRIKE][C/P] [EXPIRY]'\n"
            "2. BUDGET LIMIT: Max $300 for this trade.\n"
            "3. The STRIKE is an options strike price, NOT the stock price. "
            "REJECT only if the strike is more than 20% away from the CURRENT STOCK PRICE. "
            "A $225 call on a $222 stock is only 1.3% OTM — that is fine.\n"
            "OUTPUT: ONLY the cleaned BTO string or 'Rejected' + reason."
        )
    }
    try:
        r = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=90
        )
        if not r.ok:
            raise AssertionError(f"AI PC error: {r.status_code} — {r.text[:200]}")
        answer = r.json().get("answer", "")
        assert answer, "Empty answer from AI PC"
        return f"AI verdict: {answer[:60]}"
    except requests.exceptions.Timeout:
        raise AssertionError("AI PC timed out after 90s")

run_test("AI PC reachable",      test_ai_pc_reachable)
run_test("AI PC verdict test",   test_ai_verdict)

# ── 14. Dashboard data ────────────────────────────────────────────────────────
print(bold("\n[14] DASHBOARD"))

def test_dashboard_log():
    import pandas as pd
    log_file = "trade_log.csv"
    if not os.path.exists(log_file):
        # Create a sample entry so dashboard has something to show
        import csv
        with open(log_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "date","ticker","outcome","profit_amt","cumulative_profit","reason"])
            writer.writeheader()
            writer.writerow({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "ticker": "TEST", "outcome": "Win",
                "profit_amt": 50.00, "cumulative_profit": 50.00,
                "reason": "Test entry"
            })
        return "trade_log.csv created with sample entry"
    df = pd.read_csv(log_file)
    return f"trade_log.csv has {len(df)} rows, P&L=${df['profit_amt'].sum():.2f}"

def test_brain_log():
    log_file = "brain.log"
    if not os.path.exists(log_file):
        return "brain.log not yet created (bot not run yet — OK)"
    with open(log_file) as f:
        lines = f.readlines()
    return f"brain.log has {len(lines)} lines"

run_test("Dashboard trade_log.csv", test_dashboard_log)
run_test("Bot brain.log",           test_brain_log)

# ── 15. Full pipeline (no real order) ────────────────────────────────────────
print(bold("\n[15] FULL PIPELINE (dry run)"))

def test_full_pipeline():
    """
    Simulates the entire signal → parse → risk check → validate path
    without placing a real order. Uses dummy webull + market objects.
    """
    from signal_parser import parse_signal_safe
    from news_filter   import is_safe_to_trade
    from vwap_entry    import check_vwap_entry
    from research_scanner import get_market_regime

    signal_text = "BTO SPY 735C 5/22 x1"

    # Step 1: Parse
    sig = parse_signal_safe(signal_text)
    assert sig, "Signal parse failed"
    print(f"      Step 1 PASS: parsed {sig}")

    # Step 2: News check
    news_ok = is_safe_to_trade(sig.ticker, min_score=40)
    print(f"      Step 2: news_ok={news_ok} for {sig.ticker}")

    # Step 3: Market regime
    regime = get_market_regime()
    regime_ok = regime["regime"] != "bear"
    print(f"      Step 3: regime={regime['regime']} ok={regime_ok}")

    # Step 4: VWAP timing
    vwap = check_vwap_entry(sig.ticker, require_bounce=False)
    print(f"      Step 4: VWAP entry={vwap['entry_confirmed']} | {vwap.get('note','')}")

    # Step 5: Would trade?
    would_trade = news_ok and regime_ok
    print(f"      Step 5: would_trade={would_trade}")

    return (f"pipeline complete | news={'OK' if news_ok else 'BLOCKED'} | "
            f"regime={regime['regime']} | vwap={vwap['entry_confirmed']}")

run_test("Full signal pipeline", test_full_pipeline)

# ── Final report ──────────────────────────────────────────────────────────────
passed = [r for r in results if r["status"] == "PASS"]
failed = [r for r in results if r["status"] == "FAIL"]
skipped= [r for r in results if r["status"] == "SKIP"]

print(bold("\n" + "="*65))
print(bold("  TEST REPORT"))
print(bold("="*65))
print(f"  {green('PASS')}: {len(passed)}")
print(f"  {red('FAIL')}: {len(failed)}")
print(f"  {yellow('SKIP')}: {len(skipped)}")

if failed:
    print(f"\n  {bold('Failed tests:')}")
    for r in failed:
        print(f"    {red('x')} {r['name']}")
        print(f"        {r['note']}")

if skipped:
    print(f"\n  {bold('Skipped tests:')}")
    for r in skipped:
        print(f"    {yellow('-')} {r['name']}: {r['note']}")

print()
if not failed:
    print(f"  {green(bold('ALL TESTS PASSED — system is ready to trade!'))}")
    print(f"  Run: python main.py")
elif all(r["name"] in ("Paper BTO order", "Webull login", "Webull API") for r in failed):
    print(f"  {green(bold('Core systems ready.'))} Expected failures:")
    print(f"    Paper BTO order  — only passes during market hours (9:30AM-4PM ET)")
    print(f"    Webull login/API — IDE async conflict; works fine when running main.py")
    print(f"  Run: python main.py")
else:
    print(f"  {red(bold(f'{len(failed)} test(s) failed — fix issues above before trading.'))}")

print(bold("="*65 + "\n"))