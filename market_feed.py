"""
market_feed.py  —  Live Market Data Feed

Primary:  Polygon.io WebSocket (free tier = 15-min delayed; paid = real-time)
Fallback: yfinance scrape (delayed, no WebSocket, but zero cost)

Provides:
  market.get_quote(ticker)          → {bid, ask, last, volume, timestamp}
  market.get_option_greeks(...)     → {iv, delta, theta, gamma} via Polygon REST
  market.is_market_open()          → bool
  market.subscribe(ticker, cb)      → register a callback for live ticks
"""

import threading
import logging
import time
import json
from datetime import datetime, time as dtime
import pytz
from typing import Optional, Dict, Callable, Any

log = logging.getLogger("market_feed")

MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
ET           = pytz.timezone("America/New_York")


class MarketFeed:
    def __init__(self, api_key: str = ""):
        self.api_key   = api_key
        self.connected = False
        self._quotes: Dict[str, dict]   = {}   # ticker → latest quote
        self._callbacks: Dict[str, list] = {}  # ticker → [callback, ...]
        self._ws       = None
        self._thread: Optional[threading.Thread] = None
        self._stop     = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self.api_key:
            self._thread = threading.Thread(
                target=self._polygon_connect, daemon=True, name="MarketFeed")
            self._thread.start()
        else:
            log.warning("No Polygon API key — using yfinance fallback (delayed)")
            self._thread = threading.Thread(
                target=self._yfinance_poll_loop, daemon=True, name="MarketFeed-yf")
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ws:
            try: self._ws.close()
            except: pass

    # ── Public API ────────────────────────────────────────────────────────────

    def get_quote(self, ticker: str) -> Optional[dict]:
        """Return the latest cached quote, or fetch immediately if not cached."""
        ticker = ticker.upper()
        if ticker in self._quotes:
            return self._quotes[ticker]
        # On-demand fetch via REST
        return self._fetch_rest(ticker)

    def get_option_greeks(self, ticker: str, expiry: str,
                           strike: float, option_type: str) -> Optional[dict]:
        """Fetch option greeks from Polygon REST API."""
        if not self.api_key:
            return None
        try:
            import requests
            # Polygon option ticker format: O:NVDA250515C00110000
            dt  = datetime.strptime(expiry, "%Y-%m-%d")
            exp = dt.strftime("%y%m%d")
            cp  = "C" if option_type.upper() == "C" else "P"
            k   = int(strike * 1000)
            sym = f"O:{ticker}{exp}{cp}{k:08d}"
            url = f"https://api.polygon.io/v3/snapshot/options/{ticker}/{sym}"
            r   = requests.get(url, params={"apiKey": self.api_key}, timeout=5)
            if r.ok:
                d = r.json().get("results", {})
                g = d.get("greeks", {})
                return {
                    "iv":    d.get("implied_volatility"),
                    "delta": g.get("delta"),
                    "theta": g.get("theta"),
                    "gamma": g.get("gamma"),
                    "bid":   d.get("day", {}).get("close"),
                }
        except Exception as exc:
            log.debug("get_option_greeks error: %s", exc)
        return None

    def subscribe(self, ticker: str, callback: Callable[[dict], None]):
        """Register a callback to be fired on every new tick for this ticker."""
        ticker = ticker.upper()
        self._callbacks.setdefault(ticker, []).append(callback)
        log.info("Subscribed to live ticks: %s", ticker)
        if self.api_key and self._ws:
            self._polygon_subscribe(ticker)

    def is_market_open(self) -> bool:
        now = datetime.now(ET).time()
        return MARKET_OPEN <= now <= MARKET_CLOSE and datetime.now(ET).weekday() < 5

    def minutes_to_open(self) -> int:
        """Return minutes until next market open (0 if already open)."""
        if self.is_market_open():
            return 0
        now = datetime.now(ET)
        # Simple: calculate to today's open (doesn't handle weekends perfectly)
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now > open_today:
            # Already past open today — skip to tomorrow
            from datetime import timedelta
            open_today += timedelta(days=1)
        delta = open_today - now
        return max(0, int(delta.total_seconds() / 60))

    # ── Polygon WebSocket ─────────────────────────────────────────────────────

    def _polygon_connect(self):
        try:
            import websocket   # pip install websocket-client
        except ImportError:
            log.warning("websocket-client not installed. Falling back to yfinance.")
            self._yfinance_poll_loop()
            return

        url = "wss://socket.polygon.io/stocks"

        def on_open(ws):
            log.info("Polygon WebSocket connected")
            ws.send(json.dumps({"action": "auth", "params": self.api_key}))

        def on_message(ws, message):
            try:
                events = json.loads(message)
                for ev in events:
                    ev_type = ev.get("ev")
                    if ev_type == "status":
                        if ev.get("status") == "auth_success":
                            log.info("Polygon auth success")
                            self.connected = True
                    elif ev_type in ("Q", "T"):   # Quote or Trade tick
                        ticker = ev.get("sym", "")
                        quote  = {
                            "ticker":    ticker,
                            "bid":       ev.get("bp", ev.get("p")),
                            "ask":       ev.get("ap", ev.get("p")),
                            "last":      ev.get("p", ev.get("bp")),
                            "volume":    ev.get("s", 0),
                            "timestamp": ev.get("t", 0),
                        }
                        self._quotes[ticker] = quote
                        for cb in self._callbacks.get(ticker, []):
                            try: cb(quote)
                            except Exception as e: log.debug("Tick callback error: %s", e)
            except Exception as exc:
                log.debug("Polygon message error: %s", exc)

        def on_error(ws, error):
            log.error("Polygon WS error: %s", error)
            self.connected = False

        def on_close(ws, *args):
            log.warning("Polygon WS closed — reconnecting in 5s...")
            self.connected = False

        import websocket as ws_lib
        while not self._stop.is_set():
            try:
                self._ws = ws_lib.WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.run_forever(ping_interval=30)
            except Exception as exc:
                log.error("Polygon connect error: %s", exc)
            if not self._stop.is_set():
                time.sleep(5)

    def _polygon_subscribe(self, ticker: str):
        """Send subscribe message for a ticker after auth."""
        if self._ws:
            try:
                self._ws.send(json.dumps({
                    "action": "subscribe",
                    "params": f"Q.{ticker},T.{ticker}"
                }))
                log.info("Polygon subscribed: %s", ticker)
            except Exception as exc:
                log.debug("Subscribe error: %s", exc)

    # ── yfinance fallback ─────────────────────────────────────────────────────

    def _yfinance_poll_loop(self):
        """Poll yfinance every 30 seconds for all subscribed tickers."""
        try:
            import yfinance as yf
        except ImportError:
            log.error("yfinance not installed. Run: pip install yfinance")
            return

        self.connected = True
        log.info("yfinance fallback feed started (30s poll)")
        while not self._stop.is_set():
            tickers = list(self._callbacks.keys()) + list(self._quotes.keys())
            for ticker in set(tickers):
                try:
                    data = yf.Ticker(ticker).fast_info
                    quote = {
                        "ticker": ticker,
                        "bid":    getattr(data, "bid",  None) or getattr(data, "last_price", None),
                        "ask":    getattr(data, "ask",  None) or getattr(data, "last_price", None),
                        "last":   getattr(data, "last_price", None),
                        "volume": getattr(data, "three_month_average_volume", None),
                        "timestamp": time.time(),
                    }
                    self._quotes[ticker] = quote
                    for cb in self._callbacks.get(ticker, []):
                        try: cb(quote)
                        except Exception as e: log.debug("Tick callback error: %s", e)
                except Exception as exc:
                    log.debug("yfinance fetch error for %s: %s", ticker, exc)
            time.sleep(30)

    def _fetch_rest(self, ticker: str) -> Optional[dict]:
        """One-off REST fetch for an uncached ticker."""
        if self.api_key:
            try:
                import requests
                r = requests.get(
                    f"https://api.polygon.io/v2/last/trade/{ticker}",
                    params={"apiKey": self.api_key},
                    timeout=5,
                )
                if r.ok:
                    res = r.json().get("results", {})
                    q   = {"ticker": ticker, "last": res.get("p"),
                           "bid": None, "ask": None,
                           "volume": res.get("s"), "timestamp": res.get("t")}
                    self._quotes[ticker] = q
                    return q
            except Exception as exc:
                log.debug("REST fetch error: %s", exc)

        # yfinance fallback for single fetch
        try:
            import yfinance as yf
            data = yf.Ticker(ticker).fast_info
            q = {
                "ticker": ticker,
                "bid":    getattr(data, "bid", None),
                "ask":    getattr(data, "ask", None),
                "last":   getattr(data, "last_price", None),
                "volume": None,
                "timestamp": time.time(),
            }
            self._quotes[ticker] = q
            return q
        except Exception:
            return None
