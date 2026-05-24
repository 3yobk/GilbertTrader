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

    # Source 2: Finnhub (if key set)
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
    """Score headlines for call-trade safety."""
    if not headlines:
        # No news = neutral (can't penalize for silence)
        return {
            "score":     70,
            "verdict":   "neutral",
            "headlines": [],
            "bad_found": [],
            "good_found": [],
            "note":      "No recent news found",
        }

    bad_found  = []
    good_found = []

    for headline in headlines:
        for kw in BAD_NEWS_KEYWORDS:
            if kw in headline:
                bad_found.append(f"{kw} → {headline[:80]}")
                break
        for kw in GOOD_NEWS_KEYWORDS:
            if kw in headline:
                good_found.append(f"{kw} → {headline[:80]}")
                break

    # Score: start at 70, -15 per bad signal, +5 per good signal, min 0 max 100
    score = 70
    score -= len(bad_found)  * 15
    score += len(good_found) * 5
    score  = max(0, min(100, score))

    if score >= 70:
        verdict = "clean"
    elif score >= 40:
        verdict = "caution"
    else:
        verdict = "blocked"

    log.info("News check %s: score=%d verdict=%s bad=%d good=%d",
             ticker, score, verdict, len(bad_found), len(good_found))

    return {
        "score":     score,
        "verdict":   verdict,
        "headlines": headlines[:5],
        "bad_found": bad_found[:3],
        "good_found": good_found[:3],
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
