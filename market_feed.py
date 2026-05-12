"""
market_feed.py  —  Live Market Data Feed (Tradier Edition)
===========================================================

PRIMARY:   Tradier WebSocket — FREE with brokerage account, TRUE real-time
FALLBACK:  yfinance poll — used automatically when TRADIER_TOKEN is not set

TO ENABLE TRADIER (one-time setup):
  1. Free account at https://brokerage.tradier.com
  2. Token at https://dash.tradier.com/settings/api
  3. Add to your .env:
       TRADIER_TOKEN=your_token_here
       TRADIER_SANDBOX=false

Drop-in replacement — same public API as the original Polygon version:
  market.start()
  market.stop()
  market.get_quote(ticker)               -> {bid, ask, last, volume, timestamp}
  market.get_option_greeks(ticker, ...)  -> {iv, delta, theta, gamma}
  market.is_market_open()               -> bool
  market.subscribe(ticker, cb)           -> callback on every live tick
  market.subscribe_option(occ_sym, cb)   -> callback on every option tick
"""

import threading
import logging
import time
import json
import os
import requests as _requests
from datetime import datetime, time as dtime
from typing import Optional, Dict, Callable, List
import pytz

log = logging.getLogger("market_feed")

MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
ET           = pytz.timezone("America/New_York")

_TRADIER_REST_LIVE    = "https://api.tradier.com/v1"
_TRADIER_REST_SANDBOX = "https://sandbox.tradier.com/v1"
_TRADIER_STREAM_URL   = "wss://stream.tradier.com/v1/markets/events"


class MarketFeed:

    def __init__(self, api_key: str = ""):
        self._token   = os.getenv("TRADIER_TOKEN", api_key or "").strip()
        self._sandbox = os.getenv("TRADIER_SANDBOX", "false").lower() == "true"
        self._rest    = _TRADIER_REST_SANDBOX if self._sandbox else _TRADIER_REST_LIVE

        self.connected   = False
        self._quotes:    Dict[str, dict] = {}
        self._callbacks: Dict[str, List] = {}
        self._ws         = None
        self._session_id = None
        self._thread:    Optional[threading.Thread] = None
        self._stop       = threading.Event()
        self._subscribed: List[str] = []

        if not self._token:
            log.warning(
                "TRADIER_TOKEN not set — running on yfinance fallback (delayed data). "
                "Add TRADIER_TOKEN to .env for live WebSocket feed. "
                "Free token: https://dash.tradier.com/settings/api"
            )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if self._token:
            self._thread = threading.Thread(
                target=self._tradier_connect_loop,
                daemon=True, name="MarketFeed-Tradier")
        else:
            self._thread = threading.Thread(
                target=self._yfinance_poll_loop,
                daemon=True, name="MarketFeed-yf")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ws:
            try: self._ws.close()
            except: pass

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_quote(self, ticker: str) -> Optional[dict]:
        ticker = ticker.upper()
        if ticker in self._quotes:
            return self._quotes[ticker]
        return self._tradier_rest_quote(ticker) if self._token else self._yf_quote(ticker)

    def get_option_greeks(self, ticker: str, expiry: str,
                          strike: float, option_type: str) -> Optional[dict]:
        if not self._token:
            return None
        try:
            r = _requests.get(
                f"{self._rest}/markets/options/chains",
                headers=self._headers(),
                params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
                timeout=5,
            )
            r.raise_for_status()
            chain = r.json().get("options", {}).get("option", [])
            if isinstance(chain, dict):
                chain = [chain]
            for opt in chain:
                if (abs(float(opt.get("strike", 0)) - strike) < 0.01 and
                        opt.get("option_type", "").upper()[0] == option_type.upper()[0]):
                    g = opt.get("greeks") or {}
                    return {
                        "iv":    opt.get("implied_volatility"),
                        "delta": g.get("delta"),
                        "theta": g.get("theta"),
                        "gamma": g.get("gamma"),
                        "bid":   opt.get("bid"),
                        "ask":   opt.get("ask"),
                        "last":  opt.get("last"),
                    }
        except Exception as exc:
            log.debug("get_option_greeks error: %s", exc)
        return None

    def subscribe(self, ticker: str, callback: Callable[[dict], None]):
        ticker = ticker.upper()
        self._callbacks.setdefault(ticker, []).append(callback)
        log.info("Subscribed equity: %s", ticker)
        self._ws_subscribe([ticker])

    def subscribe_option(self, occ_symbol: str, callback: Callable[[dict], None]):
        sym = occ_symbol.upper()
        self._callbacks.setdefault(sym, []).append(callback)
        log.info("Subscribed option: %s", sym)
        self._ws_subscribe([sym])

    def is_market_open(self) -> bool:
        now = datetime.now(ET).time()
        return MARKET_OPEN <= now <= MARKET_CLOSE and datetime.now(ET).weekday() < 5

    def minutes_to_open(self) -> int:
        if self.is_market_open():
            return 0
        from datetime import timedelta
        now = datetime.now(ET)
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now > open_today:
            open_today += timedelta(days=1)
        return max(0, int((open_today - now).total_seconds() / 60))

    # ── Tradier REST ───────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _tradier_rest_quote(self, ticker: str) -> Optional[dict]:
        try:
            r = _requests.get(
                f"{self._rest}/markets/quotes",
                headers=self._headers(),
                params={"symbols": ticker, "greeks": "false"},
                timeout=5,
            )
            r.raise_for_status()
            raw = r.json().get("quotes", {}).get("quote", {})
            if isinstance(raw, list):
                raw = raw[0]
            q = {
                "ticker":    ticker,
                "bid":       raw.get("bid"),
                "ask":       raw.get("ask"),
                "last":      raw.get("last"),
                "volume":    raw.get("volume"),
                "timestamp": time.time(),
            }
            self._quotes[ticker] = q
            return q
        except Exception as exc:
            log.debug("Tradier REST quote error: %s", exc)
            return self._yf_quote(ticker)

    def validate_token(self) -> bool:
        """
        Hits the Tradier profile endpoint to confirm the token works.
        Logs a clear actionable message on failure.
        Called once at startup before attempting WebSocket.
        """
        if not self._token:
            return False
        try:
            r = _requests.get(
                f"{self._rest}/user/profile",
                headers=self._headers(),
                timeout=8,
            )
            if r.status_code == 200:
                name = r.json().get("profile", {}).get("name", "unknown")
                log.info("Tradier token valid — account: %s", name)
                return True
            elif r.status_code == 401:
                log.error(
                    "TRADIER TOKEN INVALID (401 Unauthorized). "
                    "Go to dash.tradier.com/settings/api and regenerate your token, "
                    "then update TRADIER_TOKEN in your .env."
                )
            elif r.status_code == 403:
                log.error(
                    "TRADIER TOKEN FORBIDDEN (403). "
                    "Your account may not have API access enabled. "
                    "Go to your Tradier dashboard and click 'Get API Access!' to enable it."
                )
            else:
                log.error("Tradier token check failed: HTTP %s", r.status_code)
        except Exception as exc:
            log.error("Tradier token validation error: %s", exc)
        return False

    def _create_stream_session(self) -> Optional[str]:
        """Create a Tradier streaming session. Returns None cleanly on failure."""
        try:
            r = _requests.post(
                "https://stream.tradier.com/v1/markets/events/session",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            sid = r.json()["stream"]["sessionid"]
            log.info("Tradier stream session: %s...", sid[:12])
            return sid
        except _requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 500:
                log.error(
                    "TRADIER STREAM NOT ENABLED (500). "
                    "Go to web.tradier.com, scroll to bottom right, "
                    "click [Get API Access!] button, then restart the bot. "
                    "Switching to yfinance fallback — no further retries."
                )
                self._stream_disabled = True   # signal to stop retrying
            elif status == 401:
                log.error("Tradier stream 401 — token rejected. Check TRADIER_TOKEN in .env.")
                self._stream_disabled = True
            else:
                log.error("Tradier session HTTP %s: %s", status, exc)
        except Exception as exc:
            log.error("Tradier session error: %s", exc)
        return None

    # ── Tradier WebSocket ──────────────────────────────────────────────────────

    def _tradier_connect_loop(self):
        try:
            import websocket as ws_lib
        except ImportError:
            log.error("websocket-client not installed — run: pip install websocket-client")
            self._yfinance_poll_loop()
            return


        # Validate token first — gives a clear error if API access is not enabled
        if not self.validate_token():
            log.warning(
                "Tradier token validation failed — falling back to yfinance. "
                "Fix the issue above and restart to enable live feed."
            )
            self._yfinance_poll_loop()
            return

        # Try to get a session. On hard failures (500/401) bail immediately to yfinance.
        self._stream_disabled = False
        fail_count = 0
        MAX_FAILS  = 3

        while not self._stop.is_set():
            self._session_id = self._create_stream_session()

            if not self._session_id:
                # Hard stop — streaming API not enabled or token rejected
                if self._stream_disabled:
                    log.warning(
                        "Tradier streaming unavailable — running on yfinance fallback. "
                        "Enable API streaming on web.tradier.com and restart."
                    )
                    self._yfinance_poll_loop()
                    return
                fail_count += 1
                if fail_count >= MAX_FAILS:
                    log.warning(
                        "Tradier session failed %d times — switching to yfinance fallback.",
                        MAX_FAILS
                    )
                    self._yfinance_poll_loop()
                    return
                wait = 10 * fail_count
                log.warning("Tradier session failed (%d/%d) — retrying in %ds",
                            fail_count, MAX_FAILS, wait)
                self._stop.wait(wait)
                continue

            fail_count = 0   # reset on success

            def on_open(ws):
                log.info("Tradier WebSocket connected")
                self.connected = True
                syms = list(self._callbacks.keys())
                if syms:
                    self._ws_send_subscribe(ws, syms)

            def on_message(ws, raw):
                try:
                    self._dispatch_tick(json.loads(raw))
                except Exception:
                    pass

            def on_error(ws, error):
                log.error("Tradier WS error: %s", error)
                self.connected = False

            def on_close(ws, code, msg):
                log.warning("Tradier WS closed — reconnecting...")
                self.connected = False

            try:
                self._ws = ws_lib.WebSocketApp(
                    _TRADIER_STREAM_URL,
                    header=[f"Authorization: Bearer {self._token}"],
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:
                log.error("Tradier WS connect error: %s", exc)
                self.connected = False

            if not self._stop.is_set():
                self._stop.wait(5)

    def _ws_send_subscribe(self, ws, symbols: List[str]):
        if not self._session_id:
            return
        try:
            ws.send(json.dumps({
                "symbols":   symbols,
                "sessionid": self._session_id,
                "linebreak": True,
                "filter":    ["quote", "trade", "summary"],
            }))
            log.info("Tradier subscribed: %s", symbols)
        except Exception as exc:
            log.debug("Subscribe error: %s", exc)

    def _ws_subscribe(self, symbols: List[str]):
        new = [s for s in symbols if s not in self._subscribed]
        if not new:
            return
        self._subscribed.extend(new)
        if self._ws and self.connected:
            self._ws_send_subscribe(self._ws, new)

    def _dispatch_tick(self, tick: dict):
        t_type = tick.get("type")
        symbol = tick.get("symbol", "").upper()

        if t_type == "quote":
            bid = float(tick.get("bid") or 0)
            ask = float(tick.get("ask") or 0)
            quote = {
                "ticker":    symbol,
                "bid":       bid,
                "ask":       ask,
                "last":      round((bid + ask) / 2, 4) if bid and ask else bid or ask,
                "spread":    round(ask - bid, 4),
                "volume":    tick.get("bidsize"),
                "timestamp": tick.get("timestamp", time.time() * 1000) / 1000,
                "raw":       tick,
            }
            self._quotes[symbol] = quote
            for cb in self._callbacks.get(symbol, []):
                try:    cb(quote)
                except Exception as e: log.debug("Callback error: %s", e)

        elif t_type == "trade":
            price = float(tick.get("price") or 0)
            trade = {
                "ticker":    symbol,
                "bid":       price,
                "ask":       price,
                "last":      price,
                "volume":    tick.get("size"),
                "cumvol":    tick.get("cvol"),
                "timestamp": tick.get("timestamp", time.time() * 1000) / 1000,
                "raw":       tick,
            }
            self._quotes.setdefault(symbol, trade)
            for cb in self._callbacks.get(symbol, []):
                try:    cb(trade)
                except Exception as e: log.debug("Callback error: %s", e)

    # ── yfinance fallback ──────────────────────────────────────────────────────

    def _yfinance_poll_loop(self):
        try:
            import yfinance as yf
        except ImportError:
            log.error("yfinance not installed — run: pip install yfinance")
            return

        self.connected = True
        log.info("yfinance fallback feed active (30s poll, delayed data)")
        while not self._stop.is_set():
            equity = [t for t in set(list(self._callbacks) + list(self._quotes))
                      if len(t) <= 5 and t.isalpha()]
            for ticker in equity:
                q = self._yf_quote(ticker)
                if q:
                    for cb in self._callbacks.get(ticker, []):
                        try:    cb(q)
                        except Exception as e: log.debug("Callback error: %s", e)
            self._stop.wait(30)

    def _yf_quote(self, ticker: str) -> Optional[dict]:
        try:
            import yfinance as yf
            data = yf.Ticker(ticker).fast_info
            q = {
                "ticker":    ticker,
                "bid":       getattr(data, "bid",  None) or getattr(data, "last_price", None),
                "ask":       getattr(data, "ask",  None) or getattr(data, "last_price", None),
                "last":      getattr(data, "last_price", None),
                "volume":    getattr(data, "three_month_average_volume", None),
                "timestamp": time.time(),
            }
            self._quotes[ticker] = q
            return q
        except Exception as exc:
            log.debug("yfinance error for %s: %s", ticker, exc)
            return None