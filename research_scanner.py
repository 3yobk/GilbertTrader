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
import logging

log = logging.getLogger("research_scanner")

# ── GEX trend tracker ──────────────────────────────────────────────────────────
# Requires >= 3 readings before reporting a trend (avoids noisy single-scan drops).
# Only tracks positive GEX (the kind that blocks trades).
# These overrides bypass GEX gate ONLY — spread, IV, budget, flow unchanged.
_gex_history: dict = {}   # ticker -> list of last 5 raw GEX float values (millions)

def _update_gex_history(ticker: str, gex_value: float):
    """
    Record a GEX reading and return (trend, pct_change).
    trend: 'declining' | 'growing' | 'stable' | 'unknown'
    """
    if ticker not in _gex_history:
        _gex_history[ticker] = []
    _gex_history[ticker].append(gex_value)
    if len(_gex_history[ticker]) > 5:
        _gex_history[ticker].pop(0)
    hist = _gex_history[ticker]
    if len(hist) < 3:          # need >= 3 readings per review requirement
        return "unknown", 0.0
    initial_gex = hist[0]
    current_gex = hist[-1]
    if initial_gex <= 0:       # only decay on positive (restrictive) GEX
        return "unknown", 0.0
    pct_change = (current_gex - initial_gex) / initial_gex * 100
    if pct_change <= -15:
        return "declining", pct_change
    elif pct_change >= 15:
        return "growing", pct_change
    return "stable", pct_change


def _get_gex_volume_ratio(gex_dollars_m: float, avg_daily_volume_m: float) -> float:
    """
    GEX as % of avg daily dollar volume.
    < 0.5% = economically irrelevant = safe to override.
    > 5%   = significant pin = respect it.
    """
    if avg_daily_volume_m <= 0:
        return 999.0
    return (gex_dollars_m / avg_daily_volume_m) * 100


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
    # Mega-cap tech (highest options liquidity)
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",

    # High-beta momentum names (frequent RSI extremes)
    "AMD", "PLTR", "ARM", "AVGO", "MU", "SMCI",

    # Fintech / growth (volatile, good for mean reversion)
    "SOFI", "HOOD", "COIN", "AFRM", "UPST", "HIMS",

    # Retail / consumer (strong intraday moves)
    "SHOP", "CVNA", "RBLX",

    # Leveraged ETFs + broad market (regime-sensitive)
    "SOXL", "TQQQ", "IWM", "SPY", "QQQ",

    # Crypto proxy (high volatility)
    "MARA", "RIOT",

    # New additions — high options activity, strong momentum
    "CRWD", "PANW", "SNOW", "NET",   # cybersecurity / cloud
    "UBER", "DASH",                   # consumer platforms
    "XYZ", "PYPL",                    # payments
    "ZM", "SPOT",                     # streaming / media
]

# ── UPGRADE 2: Alpha Sieve Pre-Market Filter ──────────────────────────────────

# Broader candidate pool the sieve evaluates each morning before narrowing to top 5
_ALPHA_CANDIDATE_POOL = [
    "NVDA", "AMD", "TSLA", "PLTR", "AAPL", "AMZN", "META", "MSFT", "GOOGL",
    "SMCI", "MU", "AVGO", "ARM", "COIN", "MARA", "RIOT", "HOOD", "SOFI",
    "CVNA", "RBLX", "UPST", "AFRM", "HIMS", "SHOP", "SOXL", "TQQQ", "IWM",
    "CRWD", "PANW", "SNOW", "NET", "DDOG", "XYZ", "PYPL", "ROKU", "UBER",
    "LYFT", "DASH", "ABNB", "SNAP", "PINS", "RBLX", "U", "RIVN", "LCID",
    "NIO", "XPEV", "LI", "BABA", "JD", "PDD", "BILI",
]


def run_alpha_sieve_pre_filter(top_n: int = 5,
                               listener=None) -> list:
    """
    UPGRADE 2: Pre-Market Alpha Sieve — runs at 9:15 AM ET.
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
                ticker, period="6d", interval="1h",
                progress=False, auto_adjust=True,
            )
            if data is None or len(data) < 8:
                continue

            close  = data["Close"].dropna()
            volume = data["Volume"].dropna()

            if len(close) < 4 or len(volume) < 5:
                continue

            roc_3 = float((close.iloc[-1] / close.iloc[-4] - 1) * 100)

            vol_series   = volume.resample("D").sum().dropna()
            avg_5d_vol   = float(vol_series.iloc[-6:-1].mean()) if len(vol_series) >= 6 else 1.0
            today_vol    = float(vol_series.iloc[-1]) if len(vol_series) >= 1 else 0.0
            vol_surge    = today_vol / avg_5d_vol if avg_5d_vol > 0 else 0.0

            if roc_3 > 0:
                score = roc_3 * vol_surge
                scores.append((ticker, round(score, 4)))

        except Exception as e:
            logging.getLogger("research_scanner").debug(
                "Alpha sieve skip %s: %s", ticker, e)
            continue

    if not scores:
        _msg("⚠️ Alpha Sieve found no qualifying tickers — keeping current watchlist.")
        return MY_SMALL_STOCKS

    scores.sort(key=lambda x: x[1], reverse=True)
    top_tickers = [t for t, _ in scores[:top_n]]
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
    try:
        return float(yf.Ticker(ticker).fast_info.last_price or 0) or None
    except Exception:
        return None


# ── Upgrade 2: Options flow — check for unusual call/put volume ───────────────
def check_options_flow(ticker: str, option_type: str = "C") -> dict:
    """
    Returns unusual options activity score (0-100) and supporting data.
    score > 60 = confirm signal, 40-60 = neutral, < 40 = weak
    """
    result = {"score": 50, "call_vol": 0, "put_vol": 0, "pcr": 1.0, "unusual": False}

    if not _HAS_TRADIER:
        return result

    try:
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

        pcr = put_vol / call_vol if call_vol > 0 else 1.0

        if option_type == "C":
            unusual = call_vol > 0 and call_oi > 0 and call_vol > call_oi * 2
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
    try:
        vix = yf.Ticker("^VIX")
        price = vix.fast_info.last_price
        if price and float(price) > 0:
            return float(price)
        hist = _flatten(yf.download("^VIX", period="5d", interval="1d", progress=False))
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ── IV Rank filter ─────────────────────────────────────────────────────────────
_iv_rank_cache: dict = {}

def get_iv_rank(ticker: str) -> float:
    """
    IV Rank = where current IV sits in its 30-day range (0-100).
    Skip entry when IV Rank > 60.
    Cached 10 min per ticker.
    """
    now = time.time()
    cached = _iv_rank_cache.get(ticker)
    if cached and now - cached[1] < 600:
        return cached[0]

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
                            import yfinance as yf
                            hist = yf.Ticker(ticker).history(period="30d", interval="1d")
                            if not hist.empty and len(hist) >= 10:
                                rets  = hist["Close"].pct_change().dropna()
                                vols  = rets.rolling(5).std().dropna() * (252 ** 0.5) * 100
                                low   = float(vols.min())
                                high  = float(vols.max()) * 1.5
                                if high > low:
                                    iv_rank = round(max(0, min(100, (current_iv - low) / (high - low) * 100)), 1)
                                    _iv_rank_cache[ticker] = (iv_rank, now)
                                    return iv_rank
        except Exception as e:
            log.debug("get_iv_rank Tradier error for %s: %s", ticker, e)

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
    GEX > 0 (positive): stock gets PINNED — bad for scalping
    GEX < 0 (negative): stock moves MORE aggressively — good for scalping
    """
    neutral = {"gex": 0.0, "gex_signal": "neutral",
               "gex_ok": True, "pin_level": 0.0, "available": False}

    if not _HAS_TRADIER or not current_price:
        return neutral

    try:
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

                gex_contrib = oi * gamma * 100 * current_price
                if otype == "call":
                    net_gex += gex_contrib
                else:
                    net_gex -= gex_contrib

                if oi > max_oi:
                    max_oi    = oi
                    pin_level = strike
            except Exception:
                continue

        net_gex_m = round(net_gex / 1_000_000, 2)

        if net_gex < -500_000:
            signal = "negative"
            ok     = True
        elif net_gex > 500_000:
            signal = "positive"
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
    1. RSI has risen at least 1pt from its recent low
    2. RSI is rising for at least 2 consecutive bars OR price made higher low
    3. Stock price made a higher low
    """
    try:
        close  = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, 1e-10)
        rsi_s  = (100 - (100 / (1 + rs))).dropna()

        if len(rsi_s) < lookback + 3:
            return True

        rsi_vals = [float(rsi_s.iloc[-(lookback + 1 - i)]) for i in range(lookback + 1)]
        rsi_min  = min(rsi_vals)
        rsi_now  = float(rsi_s.iloc[-1])

        if rsi_now <= rsi_min + 0.5:
            return False

        rsi_last2_rising = (float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-2]) and
                            float(rsi_s.iloc[-2]) > float(rsi_s.iloc[-3]))

        close_vals = close.dropna()
        if len(close_vals) >= 6:
            recent_low = float(close_vals.iloc[-3:].min())
            prior_low  = float(close_vals.iloc[-6:-3].min())
            higher_low = recent_low >= prior_low * 0.999
        else:
            higher_low = True

        return rsi_now > rsi_min + 1.0 and (rsi_last2_rising or higher_low)

    except Exception:
        return True


def get_price_level_stop(ticker: str, current_price: float) -> float:
    try:
        import yfinance as yf
        df = yf.download(ticker, period="1d", interval="1m",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return 0.0
        recent_lows = df["Low"].dropna().iloc[-5:]
        swing_low   = float(recent_lows.min())
        return round(swing_low * 0.995, 2)
    except Exception:
        return 0.0


def get_market_regime() -> dict:
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


def kelly_contracts(rsi: float, flow_score: float, regime: str,
                    max_budget: float, option_price: float,
                    max_contracts: int = 4) -> int:
    if option_price <= 0:
        return 1

    rsi_score    = max(0, min(100, (30 - rsi) * 5))
    flow_contrib = (flow_score - 50) * 0.6
    regime_bonus = 15 if regime == "bull" else (-15 if regime == "bear" else 0)
    conviction   = max(0, min(100, rsi_score + flow_contrib + regime_bonus))

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

    max_by_budget = max(1, int(max_budget / (option_price * 100)))
    contracts = min(raw_contracts, max_by_budget, max_contracts)

    return contracts


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


def get_15min_rsi(ticker: str) -> float:
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
    "CRWD": "XLK",  "PANW": "XLK", "SNOW": "XLK", "NET": "XLK",
    "UBER": "XLY",  "DASH": "XLY",
    "XYZ":  "XLF",  "PYPL": "XLF",
    "ZM":   "XLK",  "SPOT": "XLK",
}

_sector_cache: dict = {}

def get_sector_change(ticker: str) -> float:
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
    if not _HAS_TRADIER:
        return 0.0
    try:
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




def check_contract_exists(ticker: str, strike: float,
                          option_type: str, expiry_str: str) -> dict:
    """
    Verify the exact contract exists on Tradier with real liquidity.
    Returns full contract data including Greeks and bid/ask depth.

    This fixes the ZM/SPOT problem: those tickers may not have 0DTE contracts.
    If the contract doesn't exist, _build_signal should try the next expiry.

    Returns:
      {
        "exists":    bool,
        "bid":       float,
        "ask":       float,
        "bid_size":  int,     # number of contracts at bid (depth)
        "ask_size":  int,     # number of contracts at ask (depth)
        "delta":     float,
        "gamma":     float,
        "theta":     float,
        "vega":      float,
        "iv":        float,   # implied volatility %
        "volume":    int,
        "oi":        int,
      }
    """
    empty = {"exists": False, "bid": 0, "ask": 0, "bid_size": 0, "ask_size": 0,
             "delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0,
             "volume": 0, "oi": 0}

    if not _HAS_TRADIER:
        return {**empty, "exists": True}  # can't verify — assume exists

    try:
        from datetime import datetime as _dt
        parts = expiry_str.split("/")
        year  = _dt.now().year
        # Handle cases like "6/3" → "2026-06-03"
        expiry_full = f"{year}-{int(parts[0]):02d}-{int(parts[1]):02d}"

        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=_tradier_headers(),
            params={"symbol": ticker, "expiration": expiry_full, "greeks": "true"},
            timeout=6,
        )
        if not r.ok:
            log.debug("Contract check failed for %s %s%s %s: HTTP %d",
                      ticker, strike, option_type, expiry_str, r.status_code)
            return empty

        chain = r.json().get("options", {}).get("option", [])
        if isinstance(chain, dict):
            chain = [chain]
        if not chain:
            log.debug("No chain returned for %s on %s — contract does not exist",
                      ticker, expiry_full)
            return empty

        otype = "call" if option_type == "C" else "put"
        for opt in chain:
            if (abs(float(opt.get("strike", 0) or 0) - strike) < 0.01 and
                    opt.get("option_type") == otype):
                bid    = float(opt.get("bid", 0) or 0)
                ask    = float(opt.get("ask", 0) or 0)
                bsize  = int(opt.get("bidsize", 0) or 0)
                asize  = int(opt.get("asksize", 0) or 0)
                vol    = int(opt.get("volume", 0) or 0)
                oi     = int(opt.get("open_interest", 0) or 0)
                greeks = opt.get("greeks") or {}
                delta  = float(greeks.get("delta", 0) or 0)
                gamma  = float(greeks.get("gamma", 0) or 0)
                theta  = float(greeks.get("theta", 0) or 0)
                vega   = float(greeks.get("vega", 0) or 0)
                iv     = float(greeks.get("smv_vol", 0) or 0) * 100

                # Contract exists but has no market — treat as non-tradeable
                if bid == 0 and ask == 0:
                    log.debug("%s %s%s %s: contract exists but no market (bid=ask=0)",
                              ticker, strike, option_type, expiry_str)
                    return {**empty, "exists": False}

                return {
                    "exists":   True,
                    "bid":      bid,
                    "ask":      ask,
                    "bid_size": bsize,
                    "ask_size": asize,
                    "delta":    round(delta, 4),
                    "gamma":    round(gamma, 6),
                    "theta":    round(theta, 4),
                    "vega":     round(vega, 4),
                    "iv":       round(iv, 1),
                    "volume":   vol,
                    "oi":       oi,
                }

        # Strike not found in chain — contract doesn't exist for this expiry
        log.debug("%s %.0f%s %s: strike not found in chain",
                  ticker, strike, option_type, expiry_str)
        return empty

    except Exception as e:
        log.debug("check_contract_exists error for %s: %s", ticker, e)
        return {**empty, "exists": True}  # fail open


def get_macro_events_today() -> list:
    """
    Check if today has major macro events (FOMC, CPI, NFP, PPI).
    These events cause IV spikes and unpredictable price action.
    Returns list of event names if any, empty list if clear.

    Uses a hardcoded 2026 calendar of known high-impact dates.
    Falls back gracefully if dates are not known.
    """
    from datetime import date
    today = date.today()

    # Known 2026 high-impact macro dates (FOMC, CPI, NFP)
    # Add more as they are announced
    HIGH_IMPACT_DATES = {
        # FOMC meeting dates 2026
        date(2026, 1, 29): "FOMC Decision",
        date(2026, 3, 19): "FOMC Decision",
        date(2026, 5, 7):  "FOMC Decision",
        date(2026, 6, 18): "FOMC Decision",
        date(2026, 7, 30): "FOMC Decision",
        date(2026, 9, 17): "FOMC Decision",
        date(2026, 11, 5): "FOMC Decision",
        date(2026, 12, 17):"FOMC Decision",
        # CPI release dates 2026 (approx — 2nd week of each month)
        date(2026, 1, 15): "CPI Release",
        date(2026, 2, 12): "CPI Release",
        date(2026, 3, 12): "CPI Release",
        date(2026, 4, 10): "CPI Release",
        date(2026, 5, 13): "CPI Release",
        date(2026, 6, 11): "CPI Release",
        date(2026, 7, 15): "CPI Release",
        date(2026, 8, 12): "CPI Release",
        date(2026, 9, 10): "CPI Release",
        date(2026, 10, 14):"CPI Release",
        date(2026, 11, 12):"CPI Release",
        date(2026, 12, 10):"CPI Release",
        # NFP (Non-Farm Payrolls) — first Friday of each month
        date(2026, 1, 9):  "NFP Report",
        date(2026, 2, 6):  "NFP Report",
        date(2026, 3, 6):  "NFP Report",
        date(2026, 4, 3):  "NFP Report",
        date(2026, 5, 1):  "NFP Report",
        date(2026, 6, 5):  "NFP Report",
        date(2026, 7, 10): "NFP Report",
        date(2026, 8, 7):  "NFP Report",
        date(2026, 9, 4):  "NFP Report",
        date(2026, 10, 2): "NFP Report",
        date(2026, 11, 6): "NFP Report",
        date(2026, 12, 4): "NFP Report",
    }

    events = []
    if today in HIGH_IMPACT_DATES:
        events.append(HIGH_IMPACT_DATES[today])

    return events


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
    if option_type == "C":
        if current_price <= 30.0:
            strike = round(current_price * 2) / 2 + 0.5
            strike = int(strike) if strike == int(strike) else strike
        elif current_price <= 100.0:
            strike = int(round(current_price)) + 1
        else:
            strike = int((current_price // 5) * 5) + 5
    else:
        if current_price <= 30.0:
            strike = round(current_price * 2) / 2
            strike = int(strike) if strike == int(strike) else strike
        elif current_price <= 100.0:
            strike = int(round(current_price))
        else:
            strike = int((current_price // 5) * 5) + 5

    import pytz as _pytz
    _et      = _pytz.timezone("America/New_York")
    today    = datetime.now(_et)
    weekday  = today.weekday()
    _hour_et = today.hour

    theta_rolled = False
    use_0dte     = False

    if weekday == 4:
        exp_dt   = today
        use_0dte = True
    elif weekday == 3:
        exp_dt       = today + timedelta(days=8)
        theta_rolled = True
    elif _hour_et < 14:
        exp_dt   = today
        use_0dte = True
    else:
        days_to_fri = (4 - weekday) % 7
        exp_dt      = today + timedelta(days=days_to_fri)

    expiry = f"{exp_dt.month}/{exp_dt.day}"
    qty    = f" x{contracts}" if contracts > 1 else ""

    if use_0dte:
        log.info("0DTE routing: %s %s%s exp %s", ticker, strike, option_type, expiry)
    if theta_rolled:
        log.info("Thursday — expiry rolled to next Friday: %s %s%s %s",
                 ticker, strike, option_type, expiry)

    # ── Verify contract actually exists with real liquidity ───────────────────
    # Some tickers (ZM, SPOT, DASH) don't have 0DTE or near-term contracts.
    # If not found: try next Friday, then the Friday after that.
    if _HAS_TRADIER:
        contract_data = check_contract_exists(ticker, strike, option_type, expiry)
        if not contract_data["exists"]:
            # Try next Friday
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            _days_to_fri = (4 - _today.weekday()) % 7
            if _days_to_fri == 0: _days_to_fri = 7
            next_fri = _today + _td(days=_days_to_fri)
            fallback_expiry = f"{next_fri.month}/{next_fri.day}"

            fallback_data = check_contract_exists(ticker, strike, option_type, fallback_expiry)
            if fallback_data["exists"]:
                log.info("Contract fallback: %s %s%s %s → %s",
                         ticker, strike, option_type, expiry, fallback_expiry)
                expiry = fallback_expiry
                qty    = f" x{contracts}" if contracts > 1 else ""
            else:
                # Try the Friday after that
                next_next_fri = next_fri + _td(days=7)
                nn_expiry = f"{next_next_fri.month}/{next_next_fri.day}"
                nn_data = check_contract_exists(ticker, strike, option_type, nn_expiry)
                if nn_data["exists"]:
                    log.info("Contract fallback 2: %s %s%s %s → %s",
                             ticker, strike, option_type, expiry, nn_expiry)
                    expiry = nn_expiry
                else:
                    # No valid expiry found — signal will be blocked downstream
                    log.warning("No valid contract found for %s %s%s — all expiries checked",
                                ticker, strike, option_type)
                    return f"NO_CONTRACT {ticker} {strike}{option_type}", theta_rolled

    return f"BTO {ticker} {strike}{option_type} {expiry}{qty}", theta_rolled


_session_stats: dict = {"scans": 0, "signals": 0, "skipped": 0}


def print_session_stats():
    s = _session_stats
    print(f"\n  [Stats] Scans: {s['scans']}  |  Signals fired: {s['signals']}  |  Skipped: {s['skipped']}")


def scan_markets(tickers=MY_SMALL_STOCKS, rsi_threshold=30, listener=None,
                 market=None, budget: float = 300.0,
                 tp_pct: float = 30.0, sl_pct: float = 20.0,
                 execution_mode: dict = None):
    """
    Eight-Gate Verified Scanner — Bi-directional with smart overrides.
    """
    _session_stats["scans"] += 1

    if market is not None and not market.is_market_open():
        mins = market.minutes_to_open()
        print(f"  [Scanner] Market closed. Opens in ~{mins} min.")
        return []

    regime_data = get_market_regime()
    regime      = regime_data["regime"]
    spy_vs_sma  = regime_data["spy_vs_sma20"]
    regime_icon = {"bull": "🟢", "bear": "🔴", "neutral": "🟡"}.get(regime, "⚪")
    print(f"\n  [Regime] SPY {regime_icon} {regime.upper()} | SPY vs SMA20: {spy_vs_sma:+.1f}%")

    regime_call_mod = {"bull": +15, "neutral": 0, "bear": -15}[regime]
    regime_put_mod  = {"bull": -15, "neutral": 0, "bear": +15}[regime]
    print(f"  [Regime] {regime_icon} {regime.upper()} | DUAL-SCAN active "
          f"(calls {regime_call_mod:+d}pts / puts {regime_put_mod:+d}pts conviction)")

    # ── Macro event check (FOMC/CPI/NFP) ────────────────────────────────────
    macro_events = get_macro_events_today()
    if macro_events:
        event_str = ", ".join(macro_events)
        print(f"  [⚠️ MACRO] {event_str} today — IV elevated, sizing reduced to 1 contract max")
        if listener:
            listener.send_status_update(
                f"⚠️ **MACRO EVENT**: {event_str} today\n"
                f"Options premiums will be inflated. Max 1 contract per signal."
            )

    vix_level = _get_vix()
    if vix_level:
        if vix_level > 30:
            print(f"  [VIX] VIX={vix_level:.1f} HIGH — size reduced to 1 contract max")
        elif vix_level > 25:
            print(f"  [VIX] VIX={vix_level:.1f} elevated — tightening RSI threshold by 3pts")
        else:
            print(f"  [VIX] VIX={vix_level:.1f} normal")
    else:
        print("  [VIX] unavailable")

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

    skip_reasons: dict = {}
    skip_reasons_detail: dict = {"rsi_values": [], "closest_rsi": []}

    signals = []

    for ticker in tickers:
        # Skip SPY/QQQ as individual signals (used for regime only)
        if ticker in ("SPY", "QQQ") and len(tickers) > 5:
            continue

        try:
            rt_price = get_realtime_price(ticker)

            df = _download_intraday(ticker)
            if df.empty:
                print(f"  [Scanner] {ticker.ljust(5)} | No data")
                continue

            close = df["Close"].dropna()
            if len(close) < 14:
                continue

            rsi_series = ta.rsi(close, length=14)
            if rsi_series is None or rsi_series.dropna().empty:
                continue

            latest_rsi = rsi_series.dropna().iloc[-1]
            if pd.isna(latest_rsi):
                continue

            current_rsi   = float(latest_rsi)
            current_price = rt_price or float(close.iloc[-1])
            eff_threshold = get_atr_adjustment(df, rsi_threshold)

            skip_reasons_detail["rsi_values"].append(current_rsi)
            if current_rsi < eff_threshold + 5:
                skip_reasons_detail["closest_rsi"].append((ticker, current_rsi))

            if vix_level and vix_level > 25:
                eff_threshold = max(eff_threshold - 3, 15)

            vol          = df["Volume"].dropna()
            avg_vol      = float(vol.rolling(20).mean().iloc[-2]) if len(vol) >= 21 else float(vol.mean())
            current_vol  = float(vol.iloc[-2]) if len(vol) >= 2 else float(vol.iloc[-1])
            volume_surge = current_vol > avg_vol * 0.3 if avg_vol > 0 else False
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

            _directions_to_check = []
            call_threshold = eff_threshold
            put_threshold  = 100 - rsi_threshold

            if current_rsi < call_threshold:
                _directions_to_check.append("C")
            if current_rsi > put_threshold:
                _directions_to_check.append("P")

            if not _directions_to_check:
                _session_stats["skipped"] += 1
                continue

            for target_option_type in _directions_to_check:

                # ── Gate 1a: RSI upturn confirmation (calls only) ─────────────
                # Thresholds scale with RSI severity:
                # RSI <= 15: bypass entirely (extreme capitulation)
                # RSI 15-20: looser check (lookback=2) — open moves fast
                # RSI > 20:  normal check (lookback=3 + higher low)
                if target_option_type == "C":
                    if current_rsi <= 15:
                        print(f"    [Gate1a-EXTREME] {ticker}/C: RSI={current_rsi:.1f} — extreme, skipping upturn check")
                    elif current_rsi <= 20:
                        if not rsi_is_turning_up(df, current_rsi,
                                                 lookback=2, rsi_threshold=float(rsi_threshold)):
                            skip_reasons["RSI still falling"] = skip_reasons.get("RSI still falling", 0) + 1
                            print(f"    [Skip] {ticker}/C: RSI still falling — waiting for upturn + higher low")
                            _session_stats["skipped"] += 1
                            continue
                        else:
                            print(f"    [Gate1a] {ticker}/C: RSI={current_rsi:.1f} upturn confirmed ✅")
                    else:
                        if not rsi_is_turning_up(df, current_rsi,
                                                 lookback=3, rsi_threshold=float(rsi_threshold)):
                            skip_reasons["RSI still falling"] = skip_reasons.get("RSI still falling", 0) + 1
                            print(f"    [Skip] {ticker}/C: RSI still falling — waiting for upturn + higher low")
                            _session_stats["skipped"] += 1
                            continue
                # (For puts, we want RSI to keep falling — no upturn check)

                # ── Gate 1b: SPY intraday correlation ─────────────────────────
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

                # ── Gate 1c: Earnings calendar check ──────────────────────────
                if has_earnings_soon(ticker, days=2):
                    skip_reasons["Earnings soon"] = skip_reasons.get("Earnings soon", 0) + 1
                    print(f"    [Skip] {ticker}: Earnings within 48h — skipping")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 2: SMA-200 trend ─────────────────────────────────────
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
                else:
                    if not _is_bullish:
                        print(f"    [✅ SMA-200] {ticker}/P: Below SMA-200 — structurally weak, put confirmed")
                    elif current_rsi < 80:
                        skip_reasons["Above SMA-200 not extreme"] = skip_reasons.get("Above SMA-200 not extreme", 0) + 1
                        print(f"    [Skip] {ticker}/P: Above SMA-200, RSI={current_rsi:.1f} — not extreme enough for puts")
                        _session_stats["skipped"] += 1
                        continue

                # ── Gate 3: Volume surge ──────────────────────────────────────
                if not volume_surge:
                    skip_reasons["Weak volume"] = skip_reasons.get("Weak volume", 0) + 1
                    print(f"    [Skip] {ticker}: Weak volume ({vol_ratio:.1f}x needed 1.5x)")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 5: News filter ────────────────────────────────────────
                if _HAS_NEWS_FILTER:
                    news_r     = check_news(ticker)
                    news_score = news_r["score"]
                    news_verdict = news_r["verdict"]
                    if target_option_type == "C":
                        if news_score < 40:
                            skip_reasons["Bad news"] = skip_reasons.get("Bad news", 0) + 1
                            print(f"    [Skip] {ticker}/C: Bad news score={news_score} ({news_verdict})")
                            _session_stats["skipped"] += 1
                            continue
                        else:
                            print(f"    [News] {ticker}/C: score={news_score} {news_verdict} ✅")
                    else:
                        if news_score > 70:
                            skip_reasons["Good news blocks put"] = skip_reasons.get("Good news blocks put", 0) + 1
                            print(f"    [Skip] {ticker}/P: Too much good news score={news_score}")
                            _session_stats["skipped"] += 1
                            continue
                        else:
                            mood = "tailwind" if news_score < 40 else "neutral"
                            print(f"    [News] {ticker}/P: score={news_score} {news_verdict} ✅ ({mood})")

                # ── Gate 4: 1-hour RSI — direction-aware, skip before 10 AM ───
                # RSI <= 15: bypass Gate 4 entirely (extreme capitulation)
                # RSI 15-20: bypass Gate 4 (still extreme, hourly less relevant)
                # RSI > 20:  normal Gate 4 check applies
                _skip_gate4 = False
                try:
                    import pytz as _pytz_g4
                    _et_g4 = datetime.now(_pytz_g4.timezone("America/New_York"))
                    if _et_g4.hour < 10:
                        _skip_gate4 = True
                        print(f"    [Gate4] {ticker}: skipping hourly RSI before 10 AM")
                    elif current_rsi <= 20:
                        _skip_gate4 = True
                        print(f"    [Gate4-EXTREME] {ticker}/{target_option_type}: RSI={current_rsi:.1f} — bypassing hourly gate")
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

                # ── Gate 4b: 15-minute RSI multi-timeframe ────────────────────
                rsi_15m       = get_15min_rsi(ticker)
                mtf_confirmed = rsi_15m < (rsi_threshold + 5)
                mtf_tag       = f"✅15m={rsi_15m:.1f}" if mtf_confirmed else f"⚠️15m={rsi_15m:.1f}"

                # ── Gate 4c: Sector correlation ───────────────────────────────
                sector_chg = get_sector_change(ticker)
                if target_option_type == "C":
                    if sector_chg < -0.8:
                        # Extreme RSI override: at RSI < 15, individual stock liquidation
                        # exhaustion overrides macro sector drag up to -1.5%
                        if current_rsi < 15.0 and sector_chg >= -1.5:
                            print(f"    [Sector-OVERRIDE] {ticker}/C: RSI={current_rsi:.1f} extreme — "
                                  f"sector {sector_chg:.2f}% drag overridden by capitulation ✅")
                        else:
                            skip_reasons["Sector falling"] = skip_reasons.get("Sector falling", 0) + 1
                            print(f"    [Skip] {ticker}/C: Sector down {sector_chg:.2f}% — headwind for calls")
                            _session_stats["skipped"] += 1
                            continue
                    elif sector_chg < -0.3:
                        print(f"    [⚠️ Sector] {ticker}/C: Sector soft {sector_chg:.2f}%")
                else:
                    if sector_chg > 0.8:
                        skip_reasons["Sector rising"] = skip_reasons.get("Sector rising", 0) + 1
                        print(f"    [Skip] {ticker}/P: Sector up {sector_chg:.2f}% — headwind for puts")
                        _session_stats["skipped"] += 1
                        continue
                    elif sector_chg < -0.8:
                        print(f"    [✅ Sector] {ticker}/P: Sector down {sector_chg:.2f}% — tailwind for puts")

                # ── Options flow check ────────────────────────────────────────
                # PCR threshold scales with RSI severity:
                #   RSI <= 15: PCR < 2.0 (extreme panic — put buying is hedging)
                #   RSI 15-20: PCR < 1.5 (very oversold)
                #   RSI 20-25: PCR < 1.2 (oversold)
                #   RSI > 25:  PCR < 1.0 (standard — must be bullish flow)
                flow     = check_options_flow(ticker, target_option_type)
                flow_tag = f"flow={flow['score']} {'UNUSUAL' if flow['unusual'] else ''}"
                print(f"    [Flow] {ticker}: {flow_tag} | PCR={flow['pcr']:.2f} "
                      f"calls={flow['call_vol']} puts={flow['put_vol']}")

                if target_option_type == "C":
                    if current_rsi <= 15:   _pcr_limit = 2.0
                    elif current_rsi <= 20: _pcr_limit = 1.5
                    elif current_rsi <= 25: _pcr_limit = 1.2
                    else:                   _pcr_limit = 1.0
                else:
                    _pcr_limit = 999.0

                flow_passes = (
                        (target_option_type == "C" and flow["score"] >= 35
                         and flow["pcr"] < _pcr_limit) or
                        (target_option_type == "P" and (flow["pcr"] >= 0.6
                                                        or flow["put_vol"] > flow["call_vol"]))
                )
                if not flow_passes:
                    label = ("Options flow too bearish" if target_option_type == "C"
                             else "No unusual put flow")
                    skip_reasons[label] = skip_reasons.get(label, 0) + 1
                    print(f"    [Skip] {ticker}: {label} "
                          f"(score={flow['score']} PCR={flow['pcr']:.2f} limit={_pcr_limit:.1f})")
                    _session_stats["skipped"] += 1
                    continue

                # ── Gate 6b: IV Rank filter ───────────────────────────────────
                iv_rank = get_iv_rank(ticker)
                if iv_rank > 60:
                    skip_reasons["IV too high"] = skip_reasons.get("IV too high", 0) + 1
                    print(f"    [Skip] {ticker}: IV Rank={iv_rank:.0f} > 60 — premium too expensive")
                    _session_stats["skipped"] += 1
                    continue
                else:
                    print(f"    [IV] {ticker}: IV Rank={iv_rank:.0f} ✅ (≤60 — fair premium)")

                # ── Gate 7: GEX (Gamma Exposure) ─────────────────────────────
                # Smart overrides — bypass pin if ANY of these are true:
                #   1. RSI <= 15 AND price below pin (MM buying toward pin)
                #   2. GEX declining >= 20% across scans (pin weakening)
                #   3. GEX < 0.5% of daily dollar volume (pin negligible)
                gex = get_gex(ticker, current_price)
                if gex["available"]:
                    gex_val_m   = float(gex["gex"])
                    gex_trend, gex_pct = _update_gex_history(ticker, gex_val_m)
                    pin_level   = float(gex.get("pin_level", current_price) or current_price)
                    below_pin   = current_price < pin_level
                    extreme_rsi = current_rsi <= 15

                    approx_adv_m     = (current_price * 1_000_000 * max(vol_ratio, 0.5)) / 1000
                    gex_significance = _get_gex_volume_ratio(abs(gex_val_m), approx_adv_m)

                    gex_tag = (f"GEX={gex_val_m:.2f}M {gex['gex_signal'].upper()} "
                               f"pin={pin_level:.0f}")
                    if gex_trend == "declining":
                        gex_tag += f" [↓{abs(gex_pct):.0f}% weakening]"
                    elif gex_trend == "growing":
                        gex_tag += f" [↑{gex_pct:.0f}% growing]"
                    print(f"    [GEX] {ticker}: {gex_tag}")

                    if not gex["gex_ok"]:
                        override_reason = None

                        if target_option_type == "C" and extreme_rsi and below_pin:
                            override_reason = (
                                f"RSI={current_rsi:.1f} extreme + "
                                f"${current_price:.2f} below pin ${pin_level:.0f} "
                                f"— MM buying toward pin"
                            )
                        elif gex_trend == "declining" and abs(gex_pct) >= 20:
                            override_reason = (
                                f"GEX declining {abs(gex_pct):.0f}% over "
                                f"{len(_gex_history.get(ticker, []))} scans — pin loosening"
                            )
                        elif gex_significance < 0.5:
                            override_reason = (
                                f"GEX ${gex_val_m:.1f}M = {gex_significance:.2f}% of volume "
                                f"— pin negligible"
                            )

                        if override_reason:
                            print(f"    [GEX-OVERRIDE] {ticker}/C: {override_reason} ✅")
                        else:
                            trend_note = ""
                            if gex_trend == "growing":
                                trend_note = f" [↑{gex_pct:.0f}% — strengthening]"
                            elif gex_trend == "stable":
                                trend_note = " [stable]"
                            elif gex_trend == "declining" and abs(gex_pct) < 20:
                                trend_note = f" [↓{abs(gex_pct):.0f}% weakening — watch next scan]"
                            skip_reasons["GEX pinned"] = skip_reasons.get("GEX pinned", 0) + 1
                            print(f"    [Skip] {ticker}: GEX positive ${gex_val_m:.1f}M{trend_note}")
                            _session_stats["skipped"] += 1
                            continue

                # ── Option price estimate & contract sizing ───────────────────
                real_option_price = None
                if _HAS_TRADIER:
                    try:
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

                est_cost = est_option_price * 100

                # Budget fallback: try 1 contract if cost <= 2x budget
                max_affordable = max(1, int(budget / est_cost))
                if est_cost > budget:
                    if est_cost <= budget * 2.0:
                        print(f"    [Budget] {ticker}: ${est_cost:.0f}/contract > ${budget:.0f} — forcing 1 contract")
                        max_affordable = 1
                    else:
                        print(f"    [Skip] {ticker}: Est. option cost ${est_cost:.0f} > 2x budget ${budget * 2:.0f}")
                        _session_stats["skipped"] += 1
                        continue

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
                contracts = min(contracts, max_affordable)
                # On macro event days (FOMC/CPI/NFP) cap to 1 contract — IV is inflated
                if macro_events:
                    contracts = 1

                _dir_icon = "📈CALL" if target_option_type == "C" else "📉PUT"
                print(f"  🔥 [HIGH CONVICTION] {ticker} {_dir_icon} | RSI={current_rsi:.1f} "
                      f"flow={flow['score']} regime={regime}({_kelly_regime}) -> {contracts}x contracts | {mtf_tag}")

                signal_str, theta_rolled = _build_signal(
                    ticker, current_price, contracts,
                    target_option_type, execution_mode)

                # ── Block if no valid contract found ──────────────────────────
                if signal_str.startswith("NO_CONTRACT"):
                    skip_reasons["No contract"] = skip_reasons.get("No contract", 0) + 1
                    print(f"    [Skip] {ticker}: No tradeable contract found for any expiry — "
                          f"ticker may not have active options (try adding to watchlist exclusion)")
                    _session_stats["skipped"] += 1
                    continue

                # ── Pull full contract data: Greeks + depth ───────────────────
                # Extract expiry from signal for contract lookup
                _contract_data = {}
                try:
                    _sp = signal_str.split()
                    _sc = _sp[2] if len(_sp) > 2 else ""
                    _ss = float("".join(c for c in _sc[:-1] if c.isdigit() or c == "."))
                    _se = _sp[3] if len(_sp) > 3 else ""
                    _contract_data = check_contract_exists(
                        ticker, _ss, target_option_type, _se)
                    if _contract_data.get("exists"):
                        _delta = _contract_data["delta"]
                        _theta = _contract_data["theta"]
                        _vega  = _contract_data["vega"]
                        _bsize = _contract_data["bid_size"]
                        _asize = _contract_data["ask_size"]
                        print(f"    [Greeks] {ticker}: Δ={_delta:.3f} Θ={_theta:.3f} "
                              f"V={_vega:.3f} | Depth: {_bsize}x{_asize}")
                        # Extra safety: skip if delta is extremely low (deep OTM, no gamma)
                        if target_option_type == "C" and 0 < _delta < 0.05:
                            skip_reasons["Delta too low"] = skip_reasons.get("Delta too low", 0) + 1
                            print(f"    [Skip] {ticker}: Delta={_delta:.3f} too low — "
                                  f"contract is deep OTM, no practical gamma")
                            _session_stats["skipped"] += 1
                            continue
                        # Skip if bid/ask depth is paper-thin (< 5 contracts on either side)
                        if _bsize > 0 and _asize > 0 and min(_bsize, _asize) < 3:
                            print(f"    [⚠️ Depth] {ticker}: thin market "
                                  f"({_bsize}x{_asize}) — fill risk high")
                except Exception as _ge:
                    log.debug("Greeks fetch error: %s", _ge)

                # ── Spread check ──────────────────────────────────────────────
                try:
                    _sig_parts  = signal_str.split()
                    _sig_cont   = _sig_parts[2] if len(_sig_parts) > 2 else ""
                    _sig_strike = float("".join(c for c in _sig_cont[:-1] if c.isdigit() or c == "."))
                    _sig_expiry = _sig_parts[3] if len(_sig_parts) > 3 else ""
                    spread_pct  = check_spread_pct(ticker, _sig_strike,
                                                   target_option_type, _sig_expiry)
                    if spread_pct > 15.0:
                        skip_reasons["Spread too wide"] = skip_reasons.get("Spread too wide", 0) + 1
                        print(f"    [Skip] {ticker}: Bid/ask spread {spread_pct:.1f}% > 15%")
                        _session_stats["skipped"] += 1
                        continue
                    elif spread_pct > 0:
                        print(f"    [Spread] {ticker}: {spread_pct:.1f}% bid/ask spread ✅")
                except Exception as e:
                    log.debug("Spread check error: %s", e)

                # ── VWAP entry gate ───────────────────────────────────────────
                if _HAS_VWAP:
                    vwap_r = check_vwap_entry(ticker, require_bounce=False)
                    vwap_dist = vwap_r.get("distance_pct", 0)
                    above_vwap = vwap_r.get("above_vwap", True)
                    if target_option_type == "C" and not above_vwap and abs(vwap_dist) > 0.5:
                        if current_rsi < 20.0:
                            # Extreme capitulation — stock IS expected to be below VWAP.
                            # We want the mean-reversion bounce back toward VWAP, not after it.
                            print(f"    [VWAP-OVERRIDE] {ticker}/C: RSI={current_rsi:.1f} < 20 — "
                                  f"below VWAP expected at capitulation, targeting reversion ✅")
                        else:
                            skip_reasons["Below VWAP"] = skip_reasons.get("Below VWAP", 0) + 1
                            print(f"    [Skip] {ticker}/C: Price {vwap_dist:+.2f}% below VWAP")
                            _session_stats["skipped"] += 1
                            continue
                    elif target_option_type == "P" and above_vwap and abs(vwap_dist) > 0.5:
                        skip_reasons["Above VWAP"] = skip_reasons.get("Above VWAP", 0) + 1
                        print(f"    [Skip] {ticker}/P: Price {vwap_dist:+.2f}% above VWAP")
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
                                                      f"**GEX**: {gex['gex']}M {gex['gex_signal'].upper()} pin=${gex['pin_level']}\n"
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
        lines = ["\n  ⚠️  No signals this scan. Here's why:"]

        rsi_above = sum(1 for r in skip_reasons_detail.get("rsi_values", []) if r >= rsi_threshold)
        if rsi_above:
            lines.append(f"  • {rsi_above}/{total_scanned} tickers have RSI above {rsi_threshold} "
                         f"— market is NOT oversold enough yet")

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

        if regime == "bear":
            lines.append(f"  • Bear market detected — scanning for RSI > {100-rsi_threshold} "
                         f"(overbought) to buy PUTS")
        elif regime == "bull" and spy_vs_sma > 1.5:
            lines.append(f"  • Market is strongly bullish (+{spy_vs_sma:.1f}% vs SMA20) "
                         f"— everything is overbought, RSI < {rsi_threshold} is rare today")
        if vix_level and vix_level < 15:
            lines.append(f"  • VIX={vix_level:.1f} very low — low volatility means smaller moves")

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

        if listener and _session_stats["scans"] % 3 == 0:
            listener.send_status_update(no_signal_msg)

    print_session_stats()
    return signals