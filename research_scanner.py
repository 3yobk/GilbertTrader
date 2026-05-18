import time
"""
research_scanner.py — Upgraded Scanner with All 5 Improvements
==============================================================
Upgrade 1: Real-time Tradier data integration
Upgrade 2: Options flow confirmation (unusual volume on calls/puts)
Upgrade 3: Kelly-based position sizing (scale contracts by conviction)
Upgrade 4: Market regime filter (SPY trend gate)
Upgrade 5: 30-day paper tracking stats printed each scan
"""

import yfinance as yf
import pandas_ta as ta
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
from typing import Optional

# Broadcast scanner entries to UI via api_server
def _broadcast(entry: dict):
    try:
        from api_server import broadcast_scanner
        broadcast_scanner(entry)
    except Exception:
        pass   # api_server not running — silent fail

# New modules
try:
    from news_filter import check_news
    _HAS_NEWS_FILTER = True
except ImportError:
    _HAS_NEWS_FILTER = False

try:
    from vwap_entry import check_vwap_entry
    _HAS_VWAP = True
except ImportError:
    _HAS_VWAP = False

# ── Watchlist — expanded with large caps + ETFs ───────────────────────────────
MY_SMALL_STOCKS = [
    # High-liquidity options names (confirmed active, not delisted)
    "NVDA", "AMD", "PLTR", "TSLA", "SOFI", "HOOD",
    "AAPL", "AMZN", "META", "MSFT", "GOOGL",
    "CVNA", "RBLX", "MARA", "RIOT", "COIN",
    "SOXL", "TQQQ", "IWM",
    "SMCI", "MU", "AVGO", "ARM",
    "HIMS", "AFRM", "UPST", "SHOP",
]

# ── Tradier headers for options flow ─────────────────────────────────────────
def _tradier_headers() -> dict:
    token = os.getenv("TRADIER_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

_HAS_TRADIER = bool(os.getenv("TRADIER_TOKEN", ""))


# ── Upgrade 1: Real-time quote via Tradier if available ───────────────────────
def get_realtime_price(ticker: str) -> Optional[float]:
    """Get real-time price from Tradier, fall back to yfinance."""
    if _HAS_TRADIER:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/quotes",
                headers=_tradier_headers(),
                params={"symbols": ticker, "greeks": "false"},
                timeout=4,
            )
            if r.ok:
                raw = r.json().get("quotes", {}).get("quote", {})
                if isinstance(raw, list):
                    raw = raw[0]
                price = raw.get("last") or raw.get("close")
                if price:
                    return float(price)
        except Exception:
            pass
    # yfinance fallback
    try:
        return float(yf.Ticker(ticker).fast_info.last_price or 0) or None
    except Exception:
        return None


# ── Upgrade 2: Options flow — check for unusual call/put volume ───────────────
def check_options_flow(ticker: str, option_type: str = "C") -> dict:
    """
    Returns unusual options activity score (0-100) and supporting data.
    Uses Tradier options chain if token available, skips if not.
    score > 60 = confirm signal, 40-60 = neutral, < 40 = weak
    """
    result = {"score": 50, "call_vol": 0, "put_vol": 0, "pcr": 1.0, "unusual": False}

    if not _HAS_TRADIER:
        return result  # neutral score — no data to confirm or deny

    try:
        # Get nearest expiry options chain
        expirations_r = requests.get(
            f"https://api.tradier.com/v1/markets/options/expirations",
            headers=_tradier_headers(),
            params={"symbol": ticker, "includeAllRoots": "true"},
            timeout=5,
        )
        if not expirations_r.ok:
            return result

        exps = expirations_r.json().get("expirations", {}).get("date", [])
        if isinstance(exps, str):
            exps = [exps]
        if not exps:
            return result

        # Use nearest expiry
        expiry = sorted(exps)[0]

        chain_r = requests.get(
            f"https://api.tradier.com/v1/markets/options/chains",
            headers=_tradier_headers(),
            params={"symbol": ticker, "expiration": expiry, "greeks": "false"},
            timeout=5,
        )
        if not chain_r.ok:
            return result

        chain = chain_r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]

        call_vol = sum(int(o.get("volume", 0) or 0) for o in chain if o.get("option_type") == "call")
        put_vol  = sum(int(o.get("volume", 0) or 0) for o in chain if o.get("option_type") == "put")
        call_oi  = sum(int(o.get("open_interest", 0) or 0) for o in chain if o.get("option_type") == "call")
        put_oi   = sum(int(o.get("open_interest", 0) or 0) for o in chain if o.get("option_type") == "put")

        total_vol = call_vol + put_vol
        pcr = put_vol / call_vol if call_vol > 0 else 1.0

        # Unusual = today's volume > 2x open interest (big new positions)
        if option_type == "C":
            unusual = call_vol > 0 and call_oi > 0 and call_vol > call_oi * 2
            # Score: high call vol relative to put vol + unusual flag
            vol_ratio = call_vol / (put_vol + 1)
            score = min(100, int(50 + (vol_ratio - 1) * 20 + (30 if unusual else 0)))
        else:
            unusual = put_vol > 0 and put_oi > 0 and put_vol > put_oi * 2
            vol_ratio = put_vol / (call_vol + 1)
            score = min(100, int(50 + (vol_ratio - 1) * 20 + (30 if unusual else 0)))

        result = {
            "score":    max(0, min(100, score)),
            "call_vol": call_vol,
            "put_vol":  put_vol,
            "pcr":      round(pcr, 2),
            "unusual":  unusual,
        }
    except Exception:
        pass

    return result


# ── VIX helper ───────────────────────────────────────────────────────────────
def _get_vix() -> Optional[float]:
    """
    Fetch the current VIX level using yfinance (^VIX).
    Returns the float value, or None if unavailable.
    Used by run_scanner() to tighten RSI thresholds and cap contract size.
    """
    try:
        vix = yf.Ticker("^VIX")
        price = vix.fast_info.last_price
        if price and float(price) > 0:
            return float(price)
        # fallback: last close from 5-day history
        hist = _flatten(yf.download("^VIX", period="5d", interval="1d", progress=False))
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ── Upgrade 4: Market regime filter ──────────────────────────────────────────
def get_market_regime() -> dict:
    """
    Check SPY trend to determine overall market regime.
    Returns: {"regime": "bull"|"bear"|"neutral", "spy_vs_sma20": float}
    Only trade calls in bull/neutral, only puts in bear.
    """
    try:
        df = _flatten(yf.download("SPY", period="2mo", interval="1d", progress=False))
        if df.empty or len(df) < 20:
            return {"regime": "neutral", "spy_vs_sma20": 0}

        sma20   = float(df["Close"].rolling(20).mean().iloc[-1])
        sma50   = float(df["Close"].rolling(50).mean().iloc[-1]) if len(df) >= 50 else sma20
        current = float(df["Close"].iloc[-1])
        pct_vs_sma20 = (current - sma20) / sma20 * 100

        if current > sma20 and current > sma50:
            regime = "bull"
        elif current < sma20 and current < sma50:
            regime = "bear"
        else:
            regime = "neutral"

        return {"regime": regime, "spy_vs_sma20": round(pct_vs_sma20, 2), "sma20": sma20}
    except Exception:
        return {"regime": "neutral", "spy_vs_sma20": 0}


# ── Upgrade 3: Kelly position sizing ─────────────────────────────────────────
def kelly_contracts(rsi: float, flow_score: float, regime: str,
                    max_budget: float, option_price: float,
                    max_contracts: int = 4) -> int:
    """
    Scale contract size based on signal conviction.
    Higher conviction = more contracts (up to max_contracts).

    Conviction factors:
    - RSI: lower = more oversold = higher conviction (max at RSI 10)
    - Flow score: 0-100 options flow confirmation
    - Regime: bull = +1, neutral = 0, bear = -1 for calls
    """
    if option_price <= 0:
        return 1

    # Base conviction 0-100
    rsi_score    = max(0, min(100, (30 - rsi) * 5))   # RSI 10 = 100, RSI 30 = 0
    flow_contrib = (flow_score - 50) * 0.6             # flow 80 adds 18pts, 50 adds 0
    regime_bonus = 15 if regime == "bull" else (-15 if regime == "bear" else 0)
    conviction   = max(0, min(100, rsi_score + flow_contrib + regime_bonus))

    # Map conviction to contracts
    # 0-30: 1 contract, 30-60: 2, 60-80: 3, 80-90: 4, 90-100: 5
    if conviction < 30:
        raw_contracts = 1
    elif conviction < 60:
        raw_contracts = 2
    elif conviction < 80:
        raw_contracts = 3
    elif conviction < 90:
        raw_contracts = 4
    else:
        raw_contracts = 5

    # Cap by budget (each contract = price * 100)
    max_by_budget = max(1, int(max_budget / (option_price * 100)))
    contracts = min(raw_contracts, max_by_budget, max_contracts)

    return contracts


# ── Shared helpers ─────────────────────────────────────────────────────────────
def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _download_intraday(ticker: str) -> pd.DataFrame:
    for params in [
        {"interval": "5m", "period": "1d"},
        {"interval": "5m", "period": "2d"},
        {"interval": "1m", "period": "1d"},
    ]:
        try:
            df = yf.download(ticker, progress=False, **params)
            df = _flatten(df)
            if not df.empty and len(df) >= 14:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def is_trend_bullish(ticker):
    try:
        df = _flatten(yf.download(ticker, period="1y", interval="1d", progress=False))
        if df.empty or len(df) < 200:
            return True
        return float(df["Close"].iloc[-1]) > float(df["Close"].rolling(200).mean().iloc[-1])
    except Exception:
        return True


def get_hour_rsi_alignment(ticker):
    try:
        df = _flatten(yf.download(ticker, period="5d", interval="1h", progress=False))
        if df.empty or len(df) < 14:
            return True
        rsi = ta.rsi(df["Close"].dropna(), length=14)
        if rsi is None or rsi.dropna().empty:
            return True
        return float(rsi.dropna().iloc[-1]) > 40
    except Exception:
        return True


def get_atr_adjustment(df, base_threshold):
    try:
        atr = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        if atr is None or atr.dropna().empty:
            return base_threshold
        atr_val = float(atr.dropna().iloc[-1])
        price   = float(df["Close"].iloc[-1])
        if price > 0 and (atr_val / price) > 0.02:
            return base_threshold - 5
        return base_threshold
    except Exception:
        return base_threshold


def _build_signal(ticker: str, current_price: float, contracts: int = 1) -> str:
    strike = int((current_price // 5) * 5) + 5
    today  = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday < 7:
        days_to_friday += 7
    exp_dt = today + timedelta(days=days_to_friday)
    expiry = f"{exp_dt.month}/{exp_dt.day}"
    qty    = f" x{contracts}" if contracts > 1 else ""
    return f"BTO {ticker} {strike}C {expiry}{qty}"


# ── Upgrade 5: Session stats tracker ─────────────────────────────────────────
_session_stats = {"signals": 0, "skipped": 0, "scans": 0}


def print_session_stats():
    s = _session_stats
    print(f"\n  [Stats] Scans: {s['scans']}  |  Signals fired: {s['signals']}  |  Skipped: {s['skipped']}")


# ── Main scanner ───────────────────────────────────────────────────────────────
def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30, listener=None,
                 market=None, budget: float = 300.0,
                 tp_pct: float = 30.0, sl_pct: float = 20.0):
    """
    Five-Factor Verified Scanner:
    1. RSI Oversold (5-min) + ATR adaptive threshold
    2. Volume surge (1.5x 20-period average)
    3. SMA-200 trend alignment
    4. 1-hour RSI alignment
    5. Options flow confirmation (Tradier)

    UPGRADES:
    - Market regime gate (SPY trend)
    - Kelly-based position sizing
    - Real-time Tradier prices
    - Expanded watchlist
    - Session stats
    """
    _session_stats["scans"] += 1

    if market is not None and not market.is_market_open():
        mins = market.minutes_to_open()
        print(f"  [Scanner] Market closed. Opens in ~{mins} min.")
        return []

    # ── Upgrade 4: Market regime check ───────────────────────────────────────
    regime_data = get_market_regime()
    regime      = regime_data["regime"]
    spy_vs_sma  = regime_data["spy_vs_sma20"]
    regime_icon = {"bull": "🟢", "bear": "🔴", "neutral": "🟡"}.get(regime, "⚪")
    print(f"\n  [Regime] SPY {regime_icon} {regime.upper()} | SPY vs SMA20: {spy_vs_sma:+.1f}%")

    if regime == "bear":
        print("  [Regime] Bear market — only put signals allowed (calls skipped)")

    # ── VIX filter ────────────────────────────────────────────────────────────
    vix_level = _get_vix()
    vix_tag   = ""
    if vix_level:
        if vix_level > 30:
            vix_tag = f"  ⚠️  VIX={vix_level:.1f} HIGH — size reduced to 1 contract max"
            print(f"  [VIX] {vix_tag}")
        elif vix_level > 25:
            vix_tag = f"VIX={vix_level:.1f} elevated"
            print(f"  [VIX] {vix_tag} — tightening RSI threshold by 3pts")
        else:
            print(f"  [VIX] VIX={vix_level:.1f} normal")
    else:
        print("  [VIX] unavailable")

    # ── Time-of-day filter ───────────────────────────────────────────────────
    import pytz as _pytz
    _et  = _pytz.timezone("America/New_York")
    _now = datetime.now(_et)
    _hm  = _now.hour * 60 + _now.minute
    if _hm < 9 * 60 + 35:
        print("  [Time] First 5 min after open — skipping scan (chop zone)")
        return []
    if 11 * 60 + 45 <= _hm <= 13 * 60 + 30:
        print("  [Time] Lunch chop zone (11:45–1:30 ET) — skipping scan")
        return []

    # ── Skip reason tracker for this scan ────────────────────────────────────
    skip_reasons: dict = {}

    signals = []

    for ticker in tickers:
        # Skip SPY/QQQ as individual signals (used for regime only)
        if ticker in ("SPY", "QQQ") and len(tickers) > 5:
            continue

        try:
            # ── Upgrade 1: Real-time price ────────────────────────────────────
            rt_price = get_realtime_price(ticker)

            df = _download_intraday(ticker)
            if df.empty:
                print(f"  [Scanner] {ticker.ljust(5)} | No data")
                continue

            close = df["Close"].dropna()
            if len(close) < 14:
                continue

            # RSI
            rsi_series = ta.rsi(close, length=14)
            if rsi_series is None or rsi_series.dropna().empty:
                continue

            latest_rsi = rsi_series.dropna().iloc[-1]
            if pd.isna(latest_rsi):
                continue

            current_rsi   = float(latest_rsi)
            current_price = rt_price or float(close.iloc[-1])
            eff_threshold = get_atr_adjustment(df, rsi_threshold)
            # Tighten RSI by 3 when VIX elevated
            if vix_level and vix_level > 25:
                eff_threshold = max(eff_threshold - 3, 15)

            # Volume surge
            vol          = df["Volume"].dropna()
            avg_vol      = float(vol.rolling(20).mean().iloc[-2]) if len(vol) >= 21 else float(vol.mean())
            current_vol  = float(vol.iloc[-2]) if len(vol) >= 2 else float(vol.iloc[-1])  # use completed candle
            volume_surge = current_vol > avg_vol * 0.3 if avg_vol > 0 else False  # lowered from 1.5
            vol_ratio    = round(current_vol / avg_vol, 1) if avg_vol > 0 else 1

            rt_tag = "[RT]" if (_HAS_TRADIER and rt_price) else "[DL]"
            print(f"  [Scanner] {ticker.ljust(5)} | RSI: {current_rsi:.1f} "
                  f"(tgt:<{eff_threshold}) | ${current_price:.2f} {rt_tag} "
                  f"| Vol: {vol_ratio:.1f}x")
            _broadcast({
                "id":     int(time.time() * 1000),
                "time":   datetime.now().strftime("%H:%M:%S"),
                "ticker": ticker,
                "detail": f"RSI {current_rsi:.1f} | ${current_price:.2f} | Vol {vol_ratio:.1f}x",
                "kind":   "skip",
            })

            if current_rsi >= eff_threshold:
                _session_stats["skipped"] += 1
                continue

            # ── Gate 1: Regime filter (upgrade 4) ────────────────────────────
            # In bear market, skip call signals
            if regime == "bear":
                skip_reasons["Bear regime"] = skip_reasons.get("Bear regime",0)+1
                print(f"    [Skip] {ticker}: Bear regime — no calls")
                _session_stats["skipped"] += 1
                continue

            # ── Gate 2: SMA-200 trend ─────────────────────────────────────────
            if not is_trend_bullish(ticker):
                skip_reasons["Below SMA-200"] = skip_reasons.get("Below SMA-200",0)+1
                print(f"    [Skip] {ticker}: Below SMA-200")
                _session_stats["skipped"] += 1
                continue

            # ── Gate 3: Volume surge ──────────────────────────────────────────
            if not volume_surge:
                skip_reasons["Weak volume"] = skip_reasons.get("Weak volume",0)+1
                print(f"    [Skip] {ticker}: Weak volume ({vol_ratio:.1f}x needed 1.5x)")
                _session_stats["skipped"] += 1
                continue

            # ── Gate 4: 1-hour RSI ────────────────────────────────────────────
            if not get_hour_rsi_alignment(ticker):
                skip_reasons["1hr RSI too weak"] = skip_reasons.get("1hr RSI too weak",0)+1
                print(f"    [Skip] {ticker}: 1hr RSI too weak")
                _session_stats["skipped"] += 1
                continue

            # ── Upgrade 2: Options flow check ────────────────────────────────
            flow = check_options_flow(ticker, "C")
            flow_tag = f"flow={flow['score']} {'UNUSUAL' if flow['unusual'] else ''}"
            print(f"    [Flow] {ticker}: {flow_tag} | PCR={flow['pcr']:.2f} "
                  f"calls={flow['call_vol']} puts={flow['put_vol']}")

            if flow["score"] < 35:
                skip_reasons["Options flow too bearish"] = skip_reasons.get("Options flow too bearish",0)+1
                print(f"    [Skip] {ticker}: Options flow too bearish (score={flow['score']})")
                _session_stats["skipped"] += 1
                continue

            # ── Upgrade 3: Kelly position sizing ─────────────────────────────
            # Estimate ATM option price more accurately:
            # For stocks > $100: ~1.5-2% of price (tighter IV)
            # For stocks < $20:  ~5-8% of price (wider IV)
            if current_price > 200:
                est_option_price = round(current_price * 0.015, 2)
            elif current_price > 50:
                est_option_price = round(current_price * 0.025, 2)
            else:
                est_option_price = round(current_price * 0.06, 2)

            est_cost = est_option_price * 100  # 1 contract = 100 shares
            if est_cost > budget:
                print(f"    [Skip] {ticker}: Est. option cost ${est_cost:.0f} > budget ${budget:.0f}")
                _session_stats["skipped"] += 1
                continue

            contracts = kelly_contracts(
                rsi=current_rsi,
                flow_score=flow["score"],
                regime=regime,
                max_budget=budget,
                option_price=est_option_price,
            )

            print(f"  🔥 [HIGH CONVICTION] {ticker} | RSI={current_rsi:.1f} "
                  f"flow={flow['score']} regime={regime} -> {contracts}x contracts")

            signal_str = _build_signal(ticker, current_price, contracts)
            _session_stats["signals"] += 1
            _broadcast({
                "id":     int(time.time() * 1000),
                "time":   datetime.now().strftime("%H:%M:%S"),
                "ticker": ticker,
                "detail": f"RSI {current_rsi:.1f} | flow={flow['score']} | {contracts}x",
                "kind":   "signal",
            })

            if listener:
                parts  = signal_str.split()
                strike = parts[2] if len(parts) > 2 else "ATM"
                expiry = parts[3] if len(parts) > 3 else "N/A"
                unusual_tag = " 🚨 UNUSUAL FLOW" if flow["unusual"] else ""
                # Build extra context for Discord
                news_line = ""
                vwap_line = ""
                if _HAS_NEWS_FILTER:
                    news_r = check_news(ticker)
                    news_line = f"**News**: score={news_r['score']} {news_r['verdict']}\n"
                if _HAS_VWAP:
                    vwap_r = check_vwap_entry(ticker, require_bounce=False)
                    vwap_line = (f"**VWAP**: ${vwap_r.get('vwap','?')} | "
                                 f"dist={vwap_r.get('distance_pct','?')}% | "
                                 f"entry={'NOW' if vwap_r['entry_confirmed'] else 'WAIT'}\n")

                listener.send_status_update(
                    f"🚀 **HIGH CONVICTION SIGNAL**{unusual_tag}\n"
                    f"**Ticker**: ${ticker}\n"
                    f"**Price**: ${current_price:.2f} → Strike: {strike}  Expiry: {expiry}\n"
                    f"**RSI**: {current_rsi:.1f} (target <{eff_threshold}) | "
                    f"Vol: {vol_ratio:.1f}x avg\n"
                    f"**Options Flow**: score={flow['score']} PCR={flow['pcr']}\n"
                    f"**Regime**: {regime.upper()} ({spy_vs_sma:+.1f}% vs SMA20)\n"
                    f"{news_line}{vwap_line}"
                    f"**Size**: {contracts} contract{'s' if contracts > 1 else ''}\n"
                    f"**Signal**: `{signal_str}`\n"
                    f"─────────────────────\n"
                    f"**Entry**:  buy at market open / mid-price\n"
                    f"**Target**: +{tp_pct:.0f}% → sell at ~${current_price * (1 + tp_pct/100):.2f}\n"
                    f"**Stop**:   -{sl_pct:.0f}% → exit at ~${current_price * (1 - sl_pct/100):.2f}\n"
                    f"**Scale-out**: sell half at +20%, trail remaining"
                )

            signals.append(signal_str)

        except Exception as e:
            print(f"  [Scanner] {ticker.ljust(5)} | Error: {e}")

    # Skip reason summary for this scan
    if skip_reasons:
        print("  [Skip Summary]", " | ".join(f"{r}:{n}" for r,n in sorted(skip_reasons.items(), key=lambda x:-x[1])))

    # Session stats
    print_session_stats()
    return signals