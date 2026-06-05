"""
news_filter.py — News & Catalyst Filter
=========================================
Checks for negative news catalysts before allowing a trade.
Prevents buying calls on stocks in freefall due to earnings miss,
FDA rejection, lawsuit, SEC investigation, or other bad news.

Uses: Yahoo Finance news (free, no key needed)
      Finnhub news (free key if FINNHUB_KEY set in .env)

Returns a score 0-100:
  80-100 = clean, no bad news → trade allowed
  40-79  = minor news → trade allowed with warning
  0-39   = bad news detected → trade blocked

Usage:
  from news_filter import check_news
  result = check_news("AAPL")
  if result["score"] >= 50:
      # proceed with trade
"""

import os
import re
import time
import logging
import requests
from typing import Optional

log = logging.getLogger("news_filter")

FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")

# Keywords that indicate bad news for a CALL position
BAD_NEWS_KEYWORDS = [
    # Earnings
    "miss", "misses", "missed", "disappoints", "disappointing",
    "below expectations", "below estimates", "lowered guidance",
    "cuts guidance", "guidance cut", "revenue shortfall",
    # Legal/Regulatory
    "sec investigation", "sec charges", "lawsuit", "class action",
    "fda rejects", "fda rejection", "clinical trial fails", "trial failure",
    "delisting", "bankruptcy", "chapter 11",
    # Company
    "layoffs", "ceo resigns", "ceo fired", "fraud", "accounting irregularities",
    "restatement", "going concern", "debt default", "credit downgrade",
    # Market
    "short seller", "short report", "hindenburg", "citron",
]

# These same keywords are PUT TAILWINDS — stock likely to fall further
PUT_TAILWIND_KEYWORDS = BAD_NEWS_KEYWORDS

GOOD_NEWS_KEYWORDS = [
    "beats", "beat", "exceeds", "upgrade", "upgraded", "buy rating",
    "price target raised", "partnership", "contract", "fda approves",
    "fda approval", "acquisition", "merger", "buyback",
]

# Cache to avoid hitting APIs repeatedly for same ticker
_cache: dict = {}
CACHE_TTL = 600  # 10 minutes


def check_news(ticker: str) -> dict:
    """
    Check recent news for a ticker.
    Returns: {score, verdict, headlines, bad_found, good_found}
    Score 0-100: < 40 bad news, 40-70 caution, > 70 clean.
    """
    now = time.time()
    if ticker in _cache and now - _cache[ticker]["ts"] < CACHE_TTL:
        return _cache[ticker]["data"]

    headlines = _fetch_headlines(ticker)
    result    = _analyze_headlines(ticker, headlines)

    _cache[ticker] = {"ts": now, "data": result}
    return result


def check_news_for_direction(ticker: str, option_type: str) -> dict:
    """
    Direction-aware news check.
    Calls:  bad news blocks (score < 40 → skip)
    Puts:   bad news confirms (score < 40 → tailwind, don't skip)
            good news blocks puts (score > 70 → headwind, skip)
    Returns: {passes, score, verdict, reason}
    """
    result = check_news(ticker)
    score  = result["score"]

    if option_type == "C":
        passes = score >= 40
        reason = f"score={score} — {'blocked: bad news' if not passes else 'clean'}"
    else:  # PUT
        passes = score <= 70   # good news is the headwind for puts
        if score < 40:
            reason = f"score={score} — bad news tailwind for put ✅"
        elif score > 70:
            reason = f"score={score} — too much good news, headwind for put"
        else:
            reason = f"score={score} — neutral, put allowed"

    return {**result, "passes": passes, "reason": reason}


def _fetch_headlines(ticker: str) -> list:
    """Fetch recent headlines from Yahoo Finance and optionally Finnhub."""
    headlines = []

    # Source 1: Yahoo Finance (free, no key)
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={ticker}&newsCount=10&enableFuzzyQuery=false",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        if r.ok:
            news = r.json().get("news", [])
            for item in news[:10]:
                title = item.get("title", "")
                if title:
                    headlines.append(title.lower())
    except Exception as e:
        log.debug("Yahoo news error for %s: %s", ticker, e)

    # Source 2: Tradier news (higher quality financial headlines)
    TRADIER_TOKEN = os.getenv("TRADIER_TOKEN", "")
    if TRADIER_TOKEN and len(headlines) < 8:
        try:
            r = requests.get(
                f"https://api.tradier.com/v1/markets/news",
                headers={"Authorization": f"Bearer {TRADIER_TOKEN}",
                         "Accept": "application/json"},
                params={"symbols": ticker, "count": 10},
                timeout=6,
            )
            if r.ok:
                items = r.json().get("news", {}).get("item", [])
                if isinstance(items, dict):
                    items = [items]
                for item in items[:10]:
                    title = item.get("title", "")
                    if title:
                        headlines.append(title.lower())
        except Exception as e:
            log.debug("Tradier news error for %s: %s", ticker, e)

    # Source 3: Finnhub (if key set)
    if FINNHUB_KEY and len(headlines) < 5:
        try:
            from datetime import datetime, timedelta
            to_dt   = datetime.now().strftime("%Y-%m-%d")
            from_dt = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            r = requests.get(
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={ticker}&from={from_dt}&to={to_dt}&token={FINNHUB_KEY}",
                timeout=6,
            )
            if r.ok:
                for item in r.json()[:10]:
                    title = item.get("headline", "")
                    if title:
                        headlines.append(title.lower())
        except Exception as e:
            log.debug("Finnhub news error for %s: %s", ticker, e)

    return headlines


def _analyze_headlines(ticker: str, headlines: list) -> dict:
    """
    Score headlines for call-trade safety.

    Scoring:
      Start at 50 (true neutral — no news = 50, not 70).
      Each BAD signal:  -15 (hard bad news like lawsuit, bankruptcy, miss)
      Each STRONG bad:  -25 (SEC, fraud, delisting, bankruptcy)
      Each GOOD signal: +10 (beats, upgrade, approval)
      Each STRONG good: +15 (acquisition, major contract, buyback)
      Cap: 0–100

    This way:
      - No news → 50 (neutral, trade allowed)
      - 1 bad headline → 35 (caution/blocked)
      - 1 good headline → 60 (clean)
      - 2 good headlines → 70 (clearly clean)
      - 1 bad + 1 good → 45 (caution, allowed with warning)
    """
    if not headlines:
        return {
            "score":     50,
            "verdict":   "neutral",
            "headlines": [],
            "bad_found": [],
            "good_found": [],
            "note":      "No recent news — neutral",
        }

    # Strong signals carry more weight
    STRONG_BAD  = {"sec investigation", "sec charges", "fraud", "bankruptcy",
                   "chapter 11", "delisting", "accounting irregularities",
                   "restatement", "going concern", "hindenburg", "citron",
                   "class action", "fda rejects", "clinical trial fails"}
    STRONG_GOOD = {"acquisition", "merger", "buyback", "fda approves",
                   "fda approval", "beats expectations", "raised guidance"}

    bad_found  = []
    good_found = []

    for headline in headlines:
        h = headline.lower()
        for kw in BAD_NEWS_KEYWORDS:
            if kw in h:
                weight = "strong" if kw in STRONG_BAD else "normal"
                bad_found.append((kw, weight, headline[:80]))
                break
        for kw in GOOD_NEWS_KEYWORDS:
            if kw in h:
                weight = "strong" if kw in STRONG_GOOD else "normal"
                good_found.append((kw, weight, headline[:80]))
                break

    score = 50
    for _, weight, _ in bad_found:
        score -= 25 if weight == "strong" else 15
    for _, weight, _ in good_found:
        score += 15 if weight == "strong" else 10

    score = max(0, min(100, score))

    if score >= 60:
        verdict = "clean"
    elif score >= 35:
        verdict = "caution"
    else:
        verdict = "blocked"

    bad_strs  = [f"{kw}({w}) → {h}" for kw, w, h in bad_found[:3]]
    good_strs = [f"{kw}({w}) → {h}" for kw, w, h in good_found[:3]]

    log.info("News check %s: score=%d verdict=%s bad=%d good=%d",
             ticker, score, verdict, len(bad_found), len(good_found))

    return {
        "score":     score,
        "verdict":   verdict,
        "headlines": headlines[:5],
        "bad_found": bad_strs,
        "good_found": good_strs,
        "note":      f"{len(bad_found)} bad signals, {len(good_found)} good signals",
    }


def is_safe_to_trade(ticker: str, min_score: int = 40) -> bool:
    """Simple boolean wrapper for use in scanner."""
    result = check_news(ticker)
    return result["score"] >= min_score


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    for t in ["AAPL", "GME", "MARA", "GRTX"]:
        r = check_news(t)
        print(f"\n{t}: score={r['score']} verdict={r['verdict']}")
        if r["bad_found"]:
            print(f"  BAD:  {r['bad_found'][0]}")
        if r["good_found"]:
            print(f"  GOOD: {r['good_found'][0]}")