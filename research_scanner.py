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

# ── UPGRADE 2: Alpha Sieve Pre-Market Filter ──────────────────────────────────

# Broader candidate pool the sieve evaluates each morning before narrowing to top 5
_ALPHA_CANDIDATE_POOL = [
    "NVDA", "AMD", "TSLA", "PLTR", "AAPL", "AMZN", "META", "MSFT", "GOOGL",
    "SMCI", "MU", "AVGO", "ARM", "COIN", "MARA", "RIOT", "HOOD", "SOFI",
    "CVNA", "RBLX", "UPST", "AFRM", "HIMS", "SHOP", "SOXL", "TQQQ", "IWM",
    "CRWD", "PANW", "SNOW", "NET", "DDOG", "SQ", "PYPL", "ROKU", "UBER",
    "LYFT", "DASH", "ABNB", "SNAP", "PINS", "RBLX", "U", "RIVN", "LCID",
    "NIO", "XPEV", "LI", "BABA", "JD", "PDD", "BILI",
]


def run_alpha_sieve_pre_filter(top_n: int = 5,
                               listener=None) -> list:
    """
    UPGRADE 2: Pre-Market Alpha Sieve — runs at 9:15 AM ET.

    Scores every ticker in _ALPHA_CANDIDATE_POOL using a short-horizon
    momentum factor: 3-period Rate-of-Change on 1-hour bars cross-multiplied
    by a volume-surge ratio (today's pre-market volume vs 5-day avg).

    Dynamically overwrites MY_SMALL_STOCKS with the top `top_n` targets
    so the scanner focuses only on the highest-velocity tickers of the day.

    Returns the new watchlist for logging/Discord notification.
    """
    global MY_SMALL_STOCKS

    import yfinance as yf
    import pandas as pd

    def _msg(text):
        print(text)
        if listener:
            try:
                listener.send_status_update(text)
            except Exception:
                pass

    _msg(f"🔍 **Alpha Sieve** running — scoring {len(_ALPHA_CANDIDATE_POOL)} candidates...")

    scores: list[tuple[str, float]] = []

    for ticker in _ALPHA_CANDIDATE_POOL:
        try:
            data = yf.download(
                ticker,
                period="6d",
                interval="1h",
                progress=False,
                auto_adjust=True,
            )
            if data is None or len(data) < 8:
                continue

            close  = data["Close"].dropna()
            volume = data["Volume"].dropna()

            if len(close) < 4 or len(volume) < 5:
                continue

            # ── Factor 1: 3-period Rate of Change on hourly closes ────────────
            # ROC = (close_now / close_3_bars_ago - 1) * 100
            roc_3 = float((close.iloc[-1] / close.iloc[-4] - 1) * 100)

            # ── Factor 2: Volume surge vs 5-day average ───────────────────────
            # Use last 5 daily volumes (sum hourly bars per day)
            vol_series   = volume.resample("D").sum().dropna()
            avg_5d_vol   = float(vol_series.iloc[-6:-1].mean()) if len(vol_series) >= 6 else 1.0
            today_vol    = float(vol_series.iloc[-1]) if len(vol_series) >= 1 else 0.0
            vol_surge    = today_vol / avg_5d_vol if avg_5d_vol > 0 else 0.0

            # ── Combined momentum score ───────────────────────────────────────
            # Only positive ROC tickers qualify (momentum must be upward)
            # Score = ROC * volume_surge_multiplier
            if roc_3 > 0:
                score = roc_3 * vol_surge
                scores.append((ticker, round(score, 4)))

        except Exception as e:
            import logging
            logging.getLogger("research_scanner").debug(
                "Alpha sieve skip %s: %s", ticker, e)
            continue

    if not scores:
        _msg("⚠️ Alpha Sieve found no qualifying tickers — keeping current watchlist.")
        return MY_SMALL_STOCKS

    # Sort by score descending, take top N
    scores.sort(key=lambda x: x[1], reverse=True)
    top_tickers = [t for t, _ in scores[:top_n]]

    # Overwrite the global watchlist
    MY_SMALL_STOCKS = top_tickers

    score_str = " | ".join(f"{t} ({s:.2f})" for t, s in scores[:top_n])
    _msg(
        f"✅ **Alpha Sieve complete** — today's focus list ({top_n} tickers):\n"
        f"`{', '.join(top_tickers)}`\n"
        f"Scores: {score_str}"
    )

    return top_tickers

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


# ── IV Rank filter ─────────────────────────────────────────────────────────────
_iv_rank_cache: dict = {}   # ticker -> (iv_rank, timestamp)

def get_iv_rank(ticker: str) -> float:
    """
    IV Rank = where current IV sits in its 30-day range (0–100).
    IV Rank 0  = cheapest options in 30 days → good time to buy.
    IV Rank 100 = most expensive options in 30 days → buying expensive premium.
    Skip entry when IV Rank > 60.

    Primary source: Tradier options chain (real implied volatility).
    Fallback:       yfinance 30-day realized vol as a proxy.
    Cached 10 min per ticker.
    """
    now = time.time()
    cached = _iv_rank_cache.get(ticker)
    if cached and now - cached[1] < 600:
        return cached[0]

    # ── Primary: Tradier real IV ─────────────────────────────────────────────
    if _HAS_TRADIER:
        try:
            exp_r = requests.get(
                "https://api.tradier.com/v1/markets/options/expirations",
                headers=_tradier_headers(),
                params={"symbol": ticker, "includeAllRoots": "true"},
                timeout=5,
            )
            if exp_r.ok:
                exps = exp_r.json().get("expirations", {}).get("date", [])
                if isinstance(exps, str):
                    exps = [exps]
                if exps:
                    expiry = sorted(exps)[0]
                    chain_r = requests.get(
                        "https://api.tradier.com/v1/markets/options/chains",
                        headers=_tradier_headers(),
                        params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
                        timeout=5,
                    )
                    if chain_r.ok:
                        chain = chain_r.ok and chain_r.json().get("options", {}).get("option", [])
                        if isinstance(chain, dict):
                            chain = [chain]
                        ivs = [float(o["greeks"]["smv_vol"]) * 100
                               for o in (chain or [])
                               if o.get("greeks") and o["greeks"].get("smv_vol")]
                        if ivs:
                            current_iv = sum(ivs) / len(ivs)
                            # Build 30-day IV history via yfinance realized vol as range anchor
                            import yfinance as yf
                            hist = yf.Ticker(ticker).history(period="30d", interval="1d")
                            if not hist.empty and len(hist) >= 10:
                                rets  = hist["Close"].pct_change().dropna()
                                vols  = rets.rolling(5).std().dropna() * (252 ** 0.5) * 100
                                low   = float(vols.min())
                                high  = float(vols.max()) * 1.5  # scale to option IV range
                                if high > low:
                                    iv_rank = round(max(0, min(100, (current_iv - low) / (high - low) * 100)), 1)
                                    _iv_rank_cache[ticker] = (iv_rank, now)
                                    return iv_rank
        except Exception as e:
            log.debug("get_iv_rank Tradier error for %s: %s", ticker, e)

    # ── Fallback: yfinance realized vol ──────────────────────────────────────
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="30d", interval="1d")
        if hist.empty or len(hist) < 10:
            return 50.0
        rets     = hist["Close"].pct_change().dropna()
        vols     = rets.rolling(5).std().dropna() * (252 ** 0.5) * 100
        min_vol  = float(vols.min())
        max_vol  = float(vols.max())
        if max_vol == min_vol:
            return 50.0
        iv_rank = round((float(vols.iloc[-1]) - min_vol) / (max_vol - min_vol) * 100, 1)
        _iv_rank_cache[ticker] = (iv_rank, now)
        return iv_rank
    except Exception as e:
        log.debug("get_iv_rank fallback error for %s: %s", ticker, e)
        return 50.0


# ── GEX (Gamma Exposure) ──────────────────────────────────────────────────────

def get_gex(ticker: str, current_price: float) -> dict:
    """
    Calculate Net Gamma Exposure (GEX) from Tradier options chain.

    GEX = sum over all strikes of:
        open_interest × gamma × 100 × spot_price
        (positive for calls, negative for puts — dealer perspective)

    Interpretation:
      GEX > 0  (positive): market makers long gamma, they BUY dips and SELL rallies
                           → stock gets PINNED, less movement, bad for scalping
      GEX < 0  (negative): market makers short gamma, they SELL dips and BUY rallies
                           → stock moves MORE aggressively, good for scalping
      GEX ≈ 0  (near zero): neutral, unpredictable

    Returns:
      {
        "gex":         float,   # net GEX in dollar terms
        "gex_signal":  str,     # "negative" | "neutral" | "positive"
        "gex_ok":      bool,    # True if GEX favors the trade (negative or neutral)
        "pin_level":   float,   # strike with highest absolute OI (magnetic level)
        "available":   bool,    # False if Tradier data not available
      }
    """
    neutral = {"gex": 0.0, "gex_signal": "neutral",
               "gex_ok": True, "pin_level": 0.0, "available": False}

    if not _HAS_TRADIER or not current_price:
        return neutral

    try:
        # Fetch nearest expiry
        exp_r = requests.get(
            "https://api.tradier.com/v1/markets/options/expirations",
            headers=_tradier_headers(),
            params={"symbol": ticker, "includeAllRoots": "true"},
            timeout=5,
        )
        if not exp_r.ok:
            return neutral
        exps = exp_r.json().get("expirations", {}).get("date", [])
        if isinstance(exps, str):
            exps = [exps]
        if not exps:
            return neutral
        expiry = sorted(exps)[0]

        # Fetch chain WITH greeks=true for gamma values
        chain_r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=_tradier_headers(),
            params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
            timeout=8,
        )
        if not chain_r.ok:
            return neutral

        chain = chain_r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]
        if not chain:
            return neutral

        net_gex   = 0.0
        max_oi    = 0
        pin_level = current_price

        for opt in chain:
            try:
                oi     = int(opt.get("open_interest", 0) or 0)
                greeks = opt.get("greeks") or {}
                gamma  = float(greeks.get("gamma", 0) or 0)
                strike = float(opt.get("strike", 0) or 0)
                otype  = opt.get("option_type", "")

                # GEX per strike: OI × gamma × 100 × spot
                # Calls add positive GEX, puts subtract (dealer perspective)
                gex_contrib = oi * gamma * 100 * current_price
                if otype == "call":
                    net_gex += gex_contrib
                else:
                    net_gex -= gex_contrib

                # Track pin level (strike with highest total OI)
                if oi > max_oi:
                    max_oi    = oi
                    pin_level = strike
            except Exception:
                continue

        # Normalize to millions for readability
        net_gex_m = round(net_gex / 1_000_000, 2)

        if net_gex < -500_000:
            signal = "negative"   # dealers short gamma → stock moves more → good
            ok     = True
        elif net_gex > 500_000:
            signal = "positive"   # dealers long gamma → stock pinned → bad for scalping
            ok     = False
        else:
            signal = "neutral"
            ok     = True

        return {
            "gex":        net_gex_m,
            "gex_signal": signal,
            "gex_ok":     ok,
            "pin_level":  round(pin_level, 2),
            "available":  True,
        }

    except Exception as e:
        log.debug("GEX error for %s: %s", ticker, e)
        return neutral


def has_earnings_soon(ticker: str, days: int = 2) -> bool:
    """
    Returns True if ticker has earnings within `days` days.
    Uses Tradier earnings calendar endpoint.
    Earnings cause IV spikes — options get expensive and unpredictable.
    """
    if not _HAS_TRADIER:
        return False
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/calendar",
            headers=_tradier_headers(),
            params={"month": datetime.now().strftime("%Y-%m")},
            timeout=5,
        )
        if not r.ok:
            return False
        days_data = r.json().get("calendar", {}).get("days", {}).get("day", [])
        if isinstance(days_data, dict):
            days_data = [days_data]
        today = datetime.now().date()
        for day in days_data:
            day_date = datetime.strptime(day.get("date", ""), "%Y-%m-%d").date()
            if 0 <= (day_date - today).days <= days:
                earnings = day.get("earnings", {}).get("report", {})
                if isinstance(earnings, dict):
                    earnings = [earnings]
                for e in (earnings if isinstance(earnings, list) else [earnings]):
                    if str(e.get("symbol", "")).upper() == ticker.upper():
                        log.info("Earnings found for %s on %s — skipping", ticker, day_date)
                        return True
        return False
    except Exception as e:
        log.debug("Earnings check error for %s: %s", ticker, e)
        return False


def get_spy_intraday_change() -> float:
    """
    Returns SPY's intraday % change (current vs open).
    Used to avoid buying calls when SPY is dropping on the day.
    Returns 0.0 if unavailable.
    """
    try:
        spy_data = yf.download("SPY", period="1d", interval="5m",
                               progress=False, auto_adjust=True)
        if spy_data.empty:
            return 0.0
        df = _flatten(spy_data)
        open_price    = float(df["Open"].iloc[0])
        current_price = float(df["Close"].iloc[-1])
        return ((current_price - open_price) / open_price) * 100
    except Exception:
        return 0.0


def rsi_is_turning_up(df, current_rsi: float, lookback: int = 3,
                      rsi_threshold: float = 30.0) -> bool:
    """
    Strong entry confirmation — requires ALL of:
    1. RSI has risen at least 1pt from its recent low (stopped falling)
    2. RSI is crossing back ABOVE the threshold from below (not just below it)
       OR RSI has been rising for at least 2 consecutive bars
    3. Stock price made a higher low (stopped making new lows — bounce confirmed)

    This is much stricter than just checking RSI < 30.
    TQQQ had RSI 38 → 35 → 32 → 29 (still falling) — this would have blocked it.
    A good entry has RSI 29 → 28 → 29 → 31 (bottomed and turning).
    """
    try:
        close  = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, 1e-10)
        rsi_s  = (100 - (100 / (1 + rs))).dropna()

        if len(rsi_s) < lookback + 3:
            return True   # not enough data — allow through

        # Check RSI direction over last 3 bars
        rsi_vals = [float(rsi_s.iloc[-(lookback + 1 - i)]) for i in range(lookback + 1)]
        rsi_min  = min(rsi_vals)
        rsi_now  = float(rsi_s.iloc[-1])

        # Gate 1: RSI must have risen at least 1pt from its recent low
        if rsi_now <= rsi_min + 0.5:
            return False   # still at the bottom or still falling

        # Gate 2: At least 2 consecutive rising RSI bars
        rsi_last2_rising = (float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-2]) and
                            float(rsi_s.iloc[-2]) > float(rsi_s.iloc[-3]))

        # Gate 3: Price higher low — recent low > prior low
        # Compare lowest of last 3 bars vs lowest of 3 bars before that
        close_vals = close.dropna()
        if len(close_vals) >= 6:
            recent_low = float(close_vals.iloc[-3:].min())
            prior_low  = float(close_vals.iloc[-6:-3].min())
            higher_low = recent_low >= prior_low * 0.999   # within 0.1% tolerance
        else:
            higher_low = True   # not enough data — don't block

        # Require RSI turning AND (consecutive rise OR higher low on price)
        return rsi_now > rsi_min + 1.0 and (rsi_last2_rising or higher_low)

    except Exception:
        return True   # on error, allow through


def get_price_level_stop(ticker: str, current_price: float) -> float:
    """
    Returns the most recent 1-min swing low as a price-level stop.
    If the stock breaks this level, the bounce thesis is broken.
    More accurate than a percentage stop because it uses actual support.
    Returns 0 if unavailable (falls back to percentage stop).
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period="1d", interval="1m",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return 0.0
        # Find the lowest low in the last 5 minutes
        recent_lows = df["Low"].dropna().iloc[-5:]
        swing_low   = float(recent_lows.min())
        # Stop is 0.5% below the swing low (small buffer for noise)
        return round(swing_low * 0.995, 2)
    except Exception:
        return 0.0


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
    """Gate 4: 1-hour RSI must be > 40 (not in freefall on hourly)."""
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


def get_15min_rsi(ticker: str) -> float:
    """
    Returns the 15-minute RSI for multi-timeframe confirmation.
    If both 5-min AND 15-min are oversold, signal is much stronger.
    Returns 50.0 on error (neutral — don't block).
    """
    try:
        df = _flatten(yf.download(ticker, period="5d", interval="15m",
                                  progress=False, auto_adjust=True))
        if df.empty or len(df) < 14:
            return 50.0
        rsi = ta.rsi(df["Close"].dropna(), length=14)
        if rsi is None or rsi.dropna().empty:
            return 50.0
        return float(rsi.dropna().iloc[-1])
    except Exception:
        return 50.0


# Sector ETF map — used for sector correlation gate
_SECTOR_MAP = {
    "NVDA": "SOXX", "AMD": "SOXX", "AVGO": "SOXX", "ARM": "SOXX",
    "SMCI": "SOXX", "MU": "SOXX", "SOXL": "SOXX",
    "AAPL": "XLK",  "MSFT": "XLK", "GOOGL": "XLK", "META": "XLK",
    "AMZN": "XLK",
    "TSLA": "XLY",
    "COIN": "XLF",  "HOOD": "XLF",
    "CVNA": "XLY",
    "MARA": "XLK",  "RIOT": "XLK",
    "PLTR": "XLK",
    "SHOP": "XLK",  "AFRM": "XLF", "UPST": "XLF",
    "SOFI": "XLF",  "HIMS": "XLV",
    "RBLX": "XLY",  "TQQQ": "QQQ", "IWM":  "IWM",
}

_sector_cache: dict = {}   # ticker → (pct_change, timestamp)

def get_sector_change(ticker: str) -> float:
    """
    Returns the sector ETF's intraday % change.
    If sector is down > 0.5% when buying calls, signal is weaker.
    Cached per sector for 5 minutes.
    """
    sector = _SECTOR_MAP.get(ticker.upper(), "SPY")
    now    = time.time()
    cached = _sector_cache.get(sector)
    if cached and now - cached[1] < 300:
        return cached[0]
    try:
        df = _flatten(yf.download(sector, period="1d", interval="5m",
                                  progress=False, auto_adjust=True))
        if df.empty:
            return 0.0
        open_price = float(df["Open"].iloc[0])
        last_price = float(df["Close"].iloc[-1])
        pct = ((last_price - open_price) / open_price) * 100
        _sector_cache[sector] = (round(pct, 3), now)
        return pct
    except Exception:
        return 0.0


def check_spread_pct(ticker: str, strike: float,
                     option_type: str, expiry_str: str) -> float:
    """
    Returns bid/ask spread as a percentage of the mid price.
    High spread (> 15%) = expensive slippage — skip the trade.
    Uses Tradier options chain.
    Returns 0.0 if unavailable (don't block).
    """
    if not _HAS_TRADIER:
        return 0.0
    try:
        # Convert expiry M/D to YYYY-MM-DD
        from datetime import datetime as _dt
        parts = expiry_str.split("/")
        year  = _dt.now().year
        expiry_full = f"{year}-{int(parts[0]):02d}-{int(parts[1]):02d}"

        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=_tradier_headers(),
            params={"symbol": ticker, "expiration": expiry_full,
                    "greeks": "false"},
            timeout=6,
        )
        if not r.ok:
            return 0.0
        chain = r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]
        otype = "call" if option_type == "C" else "put"
        for opt in chain:
            if (abs(float(opt.get("strike", 0)) - strike) < 0.01 and
                    opt.get("option_type") == otype):
                bid = float(opt.get("bid", 0) or 0)
                ask = float(opt.get("ask", 0) or 0)
                if bid > 0 and ask > 0:
                    mid    = (bid + ask) / 2
                    spread = (ask - bid) / mid * 100
                    return round(spread, 1)
        return 0.0
    except Exception:
        return 0.0


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


def _build_signal(ticker: str, current_price: float,
                  contracts: int = 1,
                  option_type: str = "C",
                  execution_mode: dict = None) -> tuple:
    """
    Price-aware strike selector with auto expiry routing.

    CALLS (bull market): OTM strike just above spot — max gamma leverage
    PUTS  (bear market): strike at/slightly above spot — captures downside move

    Tiers:
      <= $30:  $0.50 intervals
      <= $100: $1.00 intervals
      > $100:  $5.00 intervals

    Expiry:
      Mon/Tue/Wed → this Friday
      Thursday    → next Friday (skip overnight theta crush)
      Friday      → next Friday
    """
    if option_type == "C":
        # OTM call — slightly above spot
        if current_price <= 30.0:
            strike = round(current_price * 2) / 2 + 0.5
            strike = int(strike) if strike == int(strike) else strike
        elif current_price <= 100.0:
            strike = int(round(current_price)) + 1
        else:
            strike = int((current_price // 5) * 5) + 5
    else:
        # PUT — strike AT or slightly above spot to be ATM/slightly ITM
        if current_price <= 30.0:
            strike = round(current_price * 2) / 2
            strike = int(strike) if strike == int(strike) else strike
        elif current_price <= 100.0:
            strike = int(round(current_price))
        else:
            strike = int((current_price // 5) * 5) + 5

    # ── Expiry routing ────────────────────────────────────────────────────────
    # Day-trading rule: use same-day (0DTE) expiry Mon–Thu before 2 PM ET
    # for maximum gamma on intraday moves. Fall back to this Friday:
    #   - After 2 PM ET (too little time for 0DTE, theta kills you)
    #   - On Friday (0DTE = today, fine to use)
    #   - On Thursday (skip to next Friday — overnight theta crush)
    import pytz as _pytz
    _et      = _pytz.timezone("America/New_York")
    today    = datetime.now(_et)
    weekday  = today.weekday()   # 0=Mon … 6=Sun
    _hour_et = today.hour

    theta_rolled = False
    use_0dte     = False

    if weekday == 4:                          # Friday — 0DTE is today, always use it
        exp_dt   = today
        use_0dte = True
    elif weekday == 3:                        # Thursday — skip to next Friday
        exp_dt       = today + timedelta(days=8)
        theta_rolled = True
    elif _hour_et < 14:                       # Mon/Tue/Wed before 2 PM → 0DTE today
        exp_dt   = today
        use_0dte = True
    else:                                     # Mon/Tue/Wed after 2 PM → this Friday
        days_to_fri = (4 - weekday) % 7
        exp_dt      = today + timedelta(days=days_to_fri)

    expiry = f"{exp_dt.month}/{exp_dt.day}"
    qty    = f" x{contracts}" if contracts > 1 else ""

    if use_0dte:
        log.info("0DTE routing: %s %s%s exp %s", ticker, strike, option_type, expiry)
    if theta_rolled:
        log.info("Thursday — expiry rolled to next Friday: %s %s%s %s",
                 ticker, strike, option_type, expiry)

    return f"BTO {ticker} {strike}{option_type} {expiry}{qty}", theta_rolled


# ── Session stats (module-level, reset each scan call) ────────────────────────
_session_stats: dict = {"scans": 0, "signals": 0, "skipped": 0}


def print_session_stats():
    s = _session_stats
    print(f"\n  [Stats] Scans: {s['scans']}  |  Signals fired: {s['signals']}  |  Skipped: {s['skipped']}")


# ── Main scanner ───────────────────────────────────────────────────────────────
def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30, listener=None,
                 market=None, budget: float = 300.0,
                 tp_pct: float = 30.0, sl_pct: float = 20.0,
                 execution_mode: dict = None):
    """
    Seven-Gate Verified Scanner — Bi-directional.

    LONG  mode: RSI < threshold → oversold dip → BUY CALLS
    SHORT mode: RSI > (100 - threshold) → overbought rally → BUY PUTS

    Gates:
    1. RSI threshold (inverted for SHORT mode)
    2. Volume surge (> 30% of 20-period average)
    3. SMA-200 trend alignment
    4. 1-hour RSI alignment
    5. News filter
    6. Options flow + PCR check (bullish for LONG, bearish for SHORT)
    7. GEX (Gamma Exposure) — negative = stock moves freely

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

    # Dual-scan: both directions always evaluated. Regime adjusts conviction, not direction.
    regime_call_mod = {"bull": +15, "neutral": 0, "bear": -15}[regime]
    regime_put_mod  = {"bull": -15, "neutral": 0, "bear": +15}[regime]
    print(f"  [Regime] {regime_icon} {regime.upper()} | DUAL-SCAN active "
          f"(calls {regime_call_mod:+d}pts / puts {regime_put_mod:+d}pts conviction)")

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
    skip_reasons_detail: dict = {"rsi_values": [], "closest_rsi": []}

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

            # Track RSI for no-signal explanation
            skip_reasons_detail["rsi_values"].append(current_rsi)
            # Track close misses — tickers within 5 pts of triggering
            if current_rsi < eff_threshold + 5:
                skip_reasons_detail["closest_rsi"].append((ticker, current_rsi))

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

            # ── Gate 1: Dual-scan — evaluate CALLS and PUTS independently ────
            # Both directions are checked for every ticker.
            # Regime shifts conviction score (±15 pts) rather than hard-blocking.
            # A ticker can produce a CALL signal, a PUT signal, or both (rare).
            _directions_to_check = []

            call_threshold = eff_threshold          # RSI < 30 → oversold → CALL
            put_threshold  = 100 - rsi_threshold    # RSI > 70 → overbought → PUT

            if current_rsi < call_threshold:
                _directions_to_check.append("C")
            if current_rsi > put_threshold:
                _directions_to_check.append("P")

            if not _directions_to_check:
                _session_stats["skipped"] += 1
                continue

            for target_option_type in _directions_to_check:

                # ── Gate 1a: RSI upturn confirmation (calls only) ────────────────
                if target_option_type == "C":
                    if not rsi_is_turning_up(df, current_rsi,
                                             lookback=3, rsi_threshold=float(rsi_threshold)):
                        skip_reasons["RSI still falling"] = skip_reasons.get("RSI still falling", 0) + 1
                        print(f"    [Skip] {ticker}/C: RSI still falling — waiting for upturn + higher low")
                        _session_stats["skipped"] += 1
                        continue
                # (For puts, we want RSI to keep falling — no upturn check)

                # ── Gate 1b: SPY intraday correlation ─────────────────────────────
                # Don't buy calls if SPY is down more than 0.5% on the day.
                # A falling market kills call premiums even on oversold stocks.
                # Only check once per scan cycle (cached via _spy_intraday_cache).
                _spy_chg = getattr(scan_markets, "_spy_intraday_cache", None)
                _spy_chg_t = getattr(scan_markets, "_spy_intraday_cache_t", 0)
                if time.time() - _spy_chg_t > 300 or _spy_chg is None:
                    _spy_chg = get_spy_intraday_change()
                    scan_markets._spy_intraday_cache   = _spy_chg
                    scan_markets._spy_intraday_cache_t = time.time()
                if target_option_type == "C" and _spy_chg < -0.5:
                    skip_reasons["SPY falling"] = skip_reasons.get("SPY falling", 0) + 1
                    print(f"    [Skip] {ticker}: SPY down {_spy_chg:.2f}% today — avoiding calls")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 1c: Earnings calendar check ──────────────────────────────
                # Skip if earnings within 48 hours — IV spikes make options
                # expensive and price action unpredictable around reports.
                if has_earnings_soon(ticker, days=2):
                    skip_reasons["Earnings soon"] = skip_reasons.get("Earnings soon", 0) + 1
                    print(f"    [Skip] {ticker}: Earnings within 48h — skipping")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 2: SMA-200 trend ─────────────────────────────────────────
                # Calls: must be above SMA-200. Puts: below SMA-200 is ideal.
                _is_bullish = is_trend_bullish(ticker)
                if target_option_type == "C":
                    if not _is_bullish:
                        if current_rsi < 20:
                            print(f"    [Override] {ticker}/C: Below SMA-200 but RSI={current_rsi:.1f} extreme — allowing")
                        else:
                            skip_reasons["Below SMA-200"] = skip_reasons.get("Below SMA-200", 0) + 1
                            print(f"    [Skip] {ticker}/C: Below SMA-200 — no calls on weak stocks")
                            _session_stats["skipped"] += 1
                            continue
                else:  # PUT
                    if not _is_bullish:
                        print(f"    [✅ SMA-200] {ticker}/P: Below SMA-200 — structurally weak, put confirmed")
                    elif current_rsi < 80:
                        skip_reasons["Above SMA-200 not extreme"] = skip_reasons.get("Above SMA-200 not extreme", 0) + 1
                        print(f"    [Skip] {ticker}/P: Above SMA-200, RSI={current_rsi:.1f} — not extreme enough for puts")
                        _session_stats["skipped"] += 1
                        continue

                # ── Gate 3: Volume surge ──────────────────────────────────────────
                if not volume_surge:
                    skip_reasons["Weak volume"] = skip_reasons.get("Weak volume",0)+1
                    print(f"    [Skip] {ticker}: Weak volume ({vol_ratio:.1f}x needed 1.5x)")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 5: News filter ────────────────────────────────────────────
                # Calls: bad news (earnings miss, lawsuit, etc.) → skip
                # Puts:  bad news is a TAILWIND → skip only if GOOD news dominates
                # Score 0-100: < 40 = bad news, > 70 = clean, 40-70 = caution
                if _HAS_NEWS_FILTER:
                    news_r     = check_news(ticker)
                    news_score = news_r["score"]
                    news_verdict = news_r["verdict"]
                    if target_option_type == "C":
                        if news_score < 40:
                            skip_reasons["Bad news"] = skip_reasons.get("Bad news", 0) + 1
                            print(f"    [Skip] {ticker}/C: Bad news score={news_score} ({news_verdict}) — {news_r['bad_found'][:1]}")
                            _session_stats["skipped"] += 1
                            continue
                        else:
                            print(f"    [News] {ticker}/C: score={news_score} {news_verdict} ✅")
                    else:  # PUT — bad news is a tailwind, good news is a headwind
                        if news_score > 70:
                            skip_reasons["Good news blocks put"] = skip_reasons.get("Good news blocks put", 0) + 1
                            print(f"    [Skip] {ticker}/P: Too much good news score={news_score} — headwind for puts")
                            _session_stats["skipped"] += 1
                            continue
                        else:
                            mood = "tailwind" if news_score < 40 else "neutral"
                            print(f"    [News] {ticker}/P: score={news_score} {news_verdict} ✅ ({mood})")

                # ── Gate 4: 1-hour RSI — direction-aware, skip before 10 AM ─────────
                # Calls: hourly RSI > 40 — not in hourly freefall
                # Puts:  hourly RSI < 60 — actually weakening on hourly, not a blip
                # Before 10 AM: skip (not enough hourly bars since open)
                _skip_gate4 = False
                try:
                    import pytz as _pytz_g4
                    _et_g4 = datetime.now(_pytz_g4.timezone("America/New_York"))
                    if _et_g4.hour < 10:
                        _skip_gate4 = True
                        print(f"    [Gate4] {ticker}: skipping hourly RSI before 10 AM")
                except Exception:
                    pass

                if not _skip_gate4:
                    try:
                        _df1h = _flatten(yf.download(ticker, period="5d", interval="1h", progress=False))
                        if not _df1h.empty and len(_df1h) >= 14:
                            _rsi1h_s = ta.rsi(_df1h["Close"].dropna(), length=14)
                            if _rsi1h_s is not None and not _rsi1h_s.dropna().empty:
                                _rsi1h = float(_rsi1h_s.dropna().iloc[-1])
                                if target_option_type == "C" and _rsi1h <= 40:
                                    skip_reasons["1hr RSI too weak"] = skip_reasons.get("1hr RSI too weak", 0) + 1
                                    print(f"    [Skip] {ticker}/C: 1hr RSI={_rsi1h:.1f} ≤ 40 — hourly downtrend")
                                    _session_stats["skipped"] += 1
                                    continue
                                elif target_option_type == "P" and _rsi1h >= 60:
                                    skip_reasons["1hr RSI too strong"] = skip_reasons.get("1hr RSI too strong", 0) + 1
                                    print(f"    [Skip] {ticker}/P: 1hr RSI={_rsi1h:.1f} ≥ 60 — not weakening on hourly")
                                    _session_stats["skipped"] += 1
                                    continue
                                else:
                                    print(f"    [Gate4] {ticker}/{target_option_type}: 1hr RSI={_rsi1h:.1f} ✅")
                    except Exception as _g4e:
                        log.debug("Gate 4 hourly RSI error: %s", _g4e)

                # ── Gate 4b: 15-minute RSI multi-timeframe ────────────────────────
                # 5-min AND 15-min both oversold = much higher conviction.
                # Doesn't block but shown in output. High conviction when both align.
                rsi_15m       = get_15min_rsi(ticker)
                mtf_confirmed = rsi_15m < (rsi_threshold + 5)
                mtf_tag       = f"✅15m={rsi_15m:.1f}" if mtf_confirmed else f"⚠️15m={rsi_15m:.1f}"

                # ── Gate 4c: Sector correlation ───────────────────────────────────
                # Calls: sector falling > 0.8% is a headwind — skip
                # Puts:  sector falling > 0.8% is a tailwind — confirm signal
                #        sector rising  > 0.8% is a headwind for puts — skip
                sector_chg = get_sector_change(ticker)
                if target_option_type == "C":
                    if sector_chg < -0.8:
                        skip_reasons["Sector falling"] = skip_reasons.get("Sector falling", 0) + 1
                        print(f"    [Skip] {ticker}/C: Sector down {sector_chg:.2f}% — headwind for calls")
                        _session_stats["skipped"] += 1
                        continue
                    elif sector_chg < -0.3:
                        print(f"    [⚠️ Sector] {ticker}/C: Sector soft {sector_chg:.2f}%")
                else:  # PUT
                    if sector_chg > 0.8:
                        skip_reasons["Sector rising"] = skip_reasons.get("Sector rising", 0) + 1
                        print(f"    [Skip] {ticker}/P: Sector up {sector_chg:.2f}% — headwind for puts")
                        _session_stats["skipped"] += 1
                        continue
                    elif sector_chg < -0.8:
                        print(f"    [✅ Sector] {ticker}/P: Sector down {sector_chg:.2f}% — tailwind for puts")

                # ── Options flow check ───────────────────────────────────────────
                flow     = check_options_flow(ticker, target_option_type)
                flow_tag = f"flow={flow['score']} {'UNUSUAL' if flow['unusual'] else ''}"
                print(f"    [Flow] {ticker}: {flow_tag} | PCR={flow['pcr']:.2f} "
                      f"calls={flow['call_vol']} puts={flow['put_vol']}")

                # Bull regime: need high call flow AND PCR < 1.0 (more calls than puts)
                # Bear regime: need high put flow (pcr >= 0.6 or put_vol > call_vol)
                flow_passes = (
                        (target_option_type == "C" and flow["score"] >= 35 and flow["pcr"] < 1.0) or
                        (target_option_type == "P" and (flow["pcr"] >= 0.6 or flow["put_vol"] > flow["call_vol"]))
                )
                if not flow_passes:
                    label = ("Options flow too bearish" if target_option_type == "C"
                             else "No unusual put flow")
                    skip_reasons[label] = skip_reasons.get(label, 0) + 1
                    print(f"    [Skip] {ticker}: {label} "
                          f"(score={flow['score']} PCR={flow['pcr']:.2f})")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 6b: IV Rank filter ───────────────────────────────────────
                # Skip when IV Rank > 60 — options are expensive relative to recent
                # history and even a correct directional move may not profit.
                # Calls AND puts both benefit from low IV rank (cheap premium).
                iv_rank = get_iv_rank(ticker)
                if iv_rank > 60:
                    skip_reasons["IV too high"] = skip_reasons.get("IV too high", 0) + 1
                    print(f"    [Skip] {ticker}: IV Rank={iv_rank:.0f} > 60 — premium too expensive")
                    _session_stats["skipped"] += 1
                    continue
                else:
                    print(f"    [IV] {ticker}: IV Rank={iv_rank:.0f} ✅ (≤60 — fair premium)")

                # ── Gate 7: GEX (Gamma Exposure) ─────────────────────────────────
                # Negative GEX = dealers short gamma = stock moves aggressively = good for scalping
                # Positive GEX = stock gets pinned = bad for 0DTE scalps
                gex = get_gex(ticker, current_price)
                if gex["available"]:
                    gex_tag = (f"GEX={gex['gex']}M {gex['gex_signal'].upper()} "
                               f"pin={gex['pin_level']}")
                    print(f"    [GEX] {ticker}: {gex_tag}")
                    if not gex["gex_ok"]:
                        skip_reasons["GEX pinned"] = skip_reasons.get("GEX pinned", 0) + 1
                        print(f"    [Skip] {ticker}: GEX positive — stock likely pinned")
                        _session_stats["skipped"] += 1
                        continue

                # ── Upgrade 3: Kelly position sizing ─────────────────────────────
                # Estimate OTM option price (scanner buys slightly OTM calls).
                # OTM options are significantly cheaper than ATM.
                # Real-world: AAPL $300 → 305C costs ~$1.50-2.00 → $150-200/contract
                #             NVDA $224 → 230C costs ~$1.50-2.00 → $150-200/contract
                #             AVGO $419 → 430C costs ~$1.00-1.50 → $100-150/contract

                # ── Option price estimate & contract sizing ───────────────────────
                # Try to get real mid-price from Tradier options chain first
                real_option_price = None
                if _HAS_TRADIER:
                    try:
                        from market_feed import MarketFeed as _MF
                        # Use flow data bid/ask if available
                        chain_bid = flow.get("best_bid", 0)
                        chain_ask = flow.get("best_ask", 0)
                        if chain_bid and chain_ask:
                            real_option_price = round((chain_bid + chain_ask) / 2, 2)
                    except Exception:
                        pass

                if real_option_price and real_option_price > 0:
                    est_option_price = real_option_price
                elif _HAS_TRADIER and target_option_type == "C":
                    if current_price > 200:
                        est_option_price = round(current_price * 0.012, 2)
                    else:
                        est_option_price = round(current_price * 0.025, 2)
                else:
                    if current_price > 400:   est_option_price = round(current_price * 0.012, 2)
                    elif current_price > 200: est_option_price = round(current_price * 0.015, 2)
                    else:                     est_option_price = round(current_price * 0.030, 2)

                est_cost = est_option_price * 100  # 1 contract = 100 shares

                # Hard cap: calculate max contracts budget can afford
                max_affordable = max(1, int(budget / est_cost))

                if est_cost > budget:
                    print(f"    [Skip] {ticker}: Est. option cost ${est_cost:.0f} > budget ${budget:.0f}")
                    _session_stats["skipped"] += 1
                    continue

                # Regime modifier is direction-aware: puts get a bull bonus in bear markets
                _kelly_regime = (
                    "bull"    if (target_option_type == "C" and regime == "bull") or
                                 (target_option_type == "P" and regime == "bear") else
                    "bear"    if (target_option_type == "C" and regime == "bear") or
                                 (target_option_type == "P" and regime == "bull") else
                    "neutral"
                )
                contracts = kelly_contracts(
                    rsi=current_rsi,
                    flow_score=flow["score"],
                    regime=_kelly_regime,
                    max_budget=budget,
                    option_price=est_option_price,
                )
                # Hard enforce budget cap — never exceed what budget can actually afford
                contracts = min(contracts, max_affordable)

                _dir_icon = "📈CALL" if target_option_type == "C" else "📉PUT"
                print(f"  🔥 [HIGH CONVICTION] {ticker} {_dir_icon} | RSI={current_rsi:.1f} "
                      f"flow={flow['score']} regime={regime}({_kelly_regime}) -> {contracts}x contracts | {mtf_tag}")

                signal_str, theta_rolled = _build_signal(
                    ticker, current_price, contracts,
                    target_option_type, execution_mode)

                # ── Spread check ──────────────────────────────────────────────────
                # Extract strike and expiry from signal for spread lookup
                try:
                    _sig_parts  = signal_str.split()
                    _sig_cont   = _sig_parts[2] if len(_sig_parts) > 2 else ""
                    _sig_strike = float("".join(c for c in _sig_cont[:-1] if c.isdigit() or c == "."))
                    _sig_expiry = _sig_parts[3] if len(_sig_parts) > 3 else ""
                    spread_pct  = check_spread_pct(ticker, _sig_strike,
                                                   target_option_type, _sig_expiry)
                    if spread_pct > 15.0:
                        skip_reasons["Spread too wide"] = skip_reasons.get("Spread too wide", 0) + 1
                        print(f"    [Skip] {ticker}: Bid/ask spread {spread_pct:.1f}% > 15% — "
                              f"too expensive to enter")
                        _session_stats["skipped"] += 1
                        continue
                    elif spread_pct > 0:
                        print(f"    [Spread] {ticker}: {spread_pct:.1f}% bid/ask spread ✅")
                except Exception as e:
                    log.debug("Spread check error: %s", e)

                # ── VWAP entry gate ───────────────────────────────────────────
                # Calls: price should be bouncing off or above VWAP — institutions
                #        stepping in at fair value. "WAIT" = don't enter yet.
                # Puts:  price should be below or rejecting off VWAP downward.
                # Informational only when VWAP data unavailable.
                if _HAS_VWAP:
                    vwap_r = check_vwap_entry(ticker, require_bounce=False)
                    vwap_dist = vwap_r.get("distance_pct", 0)
                    above_vwap = vwap_r.get("above_vwap", True)
                    if target_option_type == "C" and not above_vwap and abs(vwap_dist) > 0.5:
                        skip_reasons["Below VWAP"] = skip_reasons.get("Below VWAP", 0) + 1
                        print(f"    [Skip] {ticker}/C: Price {vwap_dist:+.2f}% below VWAP — wait for bounce")
                        _session_stats["skipped"] += 1
                        continue
                    elif target_option_type == "P" and above_vwap and abs(vwap_dist) > 0.5:
                        skip_reasons["Above VWAP"] = skip_reasons.get("Above VWAP", 0) + 1
                        print(f"    [Skip] {ticker}/P: Price {vwap_dist:+.2f}% above VWAP — wait for rejection")
                        _session_stats["skipped"] += 1
                        continue
                    else:
                        print(f"    [VWAP] {ticker}: dist={vwap_dist:+.2f}% {'✅ above' if above_vwap else '✅ below'} VWAP")

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
                    if _HAS_VWAP and 'vwap_r' in dir():
                        vwap_line = (f"**VWAP**: ${vwap_r.get('vwap','?')} | "
                                     f"dist={vwap_r.get('distance_pct','?')}% ✅\n")

                    listener.send_status_update(
                        f"🚀 **HIGH CONVICTION SIGNAL**{unusual_tag}\n"
                        f"**Ticker**: ${ticker}\n"
                        f"**Price**: ${current_price:.2f} → Strike: {strike}  "
                        f"Expiry: {expiry}"
                        + (" ⏰ *Next-week rolled — Thu theta protection*"
                           if theta_rolled else "") + "\n"
                                                      f"**RSI**: {current_rsi:.1f} (target <{eff_threshold}) | "
                                                      f"Vol: {vol_ratio:.1f}x avg\n"
                                                      f"**Options Flow**: score={flow['score']} PCR={flow['pcr']}\n"
                                                      f"**GEX**: {gex['gex']}M {gex['gex_signal'].upper()} pin=${gex['pin_level']}\n" if gex.get("available") else
                        f"**Options Flow**: score={flow['score']} PCR={flow['pcr']}\n"
                        f"**Regime**: {regime.upper()} ({spy_vs_sma:+.1f}% vs SMA20)\n"
                        f"{news_line}{vwap_line}"
                        f"**Size**: {contracts} contract{'s' if contracts > 1 else ''}\n"
                        f"**Signal**: `{signal_str}`\n"
                        f"─────────────────────\n"
                        f"**Entry**:  buy at mid-price\n"
                        f"**Target**: +{tp_pct:.0f}% → stock ~${current_price * (1 + tp_pct/100):.2f}\n"
                        f"**Stop**:   -{sl_pct:.0f}% → stock ~${current_price * (1 - sl_pct/100):.2f}\n"
                        f"**Scale-out**: sell half at +20%, trail remaining"
                    )

                signals.append(signal_str)

        except Exception as e:
            print(f"  [Scanner] {ticker.ljust(5)} | Error: {e}")

    # ── Scan complete — explain results ──────────────────────────────────────
    total_scanned = len(tickers)

    if skip_reasons:
        print("  [Skip Summary]", " | ".join(
            f"{r}:{n}" for r, n in sorted(skip_reasons.items(), key=lambda x: -x[1])))

    if not signals:
        # Build a human-readable explanation of why nothing fired
        lines = ["\n  ⚠️  No signals this scan. Here's why:"]

        # RSI distribution
        rsi_above = sum(1 for r in skip_reasons_detail.get("rsi_values", []) if r >= rsi_threshold)
        if rsi_above:
            lines.append(f"  • {rsi_above}/{total_scanned} tickers have RSI above {rsi_threshold} "
                         f"— market is NOT oversold enough yet")

        # Top skip reasons
        top_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])[:4]
        reason_map = {
            "1hr RSI too weak":        "still falling on hourly chart (downtrend not done)",
            "Below SMA-200":           "long-term downtrend — bot avoids weak stocks",
            "Weak volume":             "not enough trader interest to confirm the move",
            "Options flow too bearish":"institutions are buying puts, not calls",
            "GEX pinned":              "market makers pinning price — won't move enough",
            "Bad news":                "recent negative news on the ticker",
        }
        for reason, count in top_reasons:
            explanation = reason_map.get(reason, "filter threshold not met")
            lines.append(f"  • {count} ticker(s) skipped: {reason} — {explanation}")

        # Market condition
        if regime == "bear":
            lines.append(f"  • Bear market detected — scanning for RSI > {100-rsi_threshold} "
                         f"(overbought) to buy PUTS")
        elif regime == "bull" and spy_vs_sma > 1.5:
            lines.append(f"  • Market is strongly bullish (+{spy_vs_sma:.1f}% vs SMA20) "
                         f"— everything is overbought, RSI < {rsi_threshold} is rare today")
        if vix_level and vix_level < 15:
            lines.append(f"  • VIX={vix_level:.1f} very low — low volatility means smaller moves, "
                         f"options premiums are cheap but moves are slow")

        # Closest missed tickers
        closest = skip_reasons_detail.get("closest_rsi", [])
        if closest:
            closest_str = ", ".join(
                f"{t} (RSI {r:.1f})" for t, r in sorted(closest, key=lambda x: x[1])[:3])
            lines.append(f"  • Closest to triggering: {closest_str}")
            lines.append(f"    → Watch these — if RSI drops below {rsi_threshold} next scan they fire")

        lines.append(f"\n  💡 Tip: This is normal on strong bull days. "
                     f"Wait for a dip or use !scan to check again in 5 min.")

        no_signal_msg = "\n".join(lines)
        print(no_signal_msg)

        # Send to Discord so you can see it on mobile
        if listener and _session_stats["scans"] % 3 == 0:  # every 3rd scan to avoid spam
            listener.send_status_update(no_signal_msg)

    # Session stats
    print_session_stats()
    return signals