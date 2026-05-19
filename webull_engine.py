"""
webull_engine.py  —  Webull Engine (Hybrid: Browser Login + API Orders)
========================================================================
- Chromium browser: login, session persistence, position monitoring
- webull Python API: place_order_option() for all order execution
  (bypasses all browser click issues permanently)

SETUP:
  pip install webull playwright playwright-stealth
  Add to .env:
    WEBULL_EMAIL=your@email.com
    WEBULL_PASSWORD=yourpassword
    WEBULL_TRADING_PIN=123456
"""

import os
import re
import time
import logging
from typing import Optional
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright_stealth import Stealth
from rich.console import Console
from rich.prompt import Confirm

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log     = logging.getLogger("webull")
console = Console()

WEBULL_EMAIL = os.getenv("WEBULL_EMAIL", "")
WEBULL_PASS  = os.getenv("WEBULL_PASSWORD", "")
WEBULL_PIN   = os.getenv("WEBULL_TRADING_PIN", "")

# ── DOM selectors (browser still used for login + position monitoring) ────────
SELECTORS = {
    "logged_in_indicator": (
        '[data-testid="portfolio-value"], .account-info, .nav-user-info, '
        ':text("Net Account Value"), :text("Paper Trading"), :text("Watchlists")'
    ),
    "positions_tab":      ':text("Positions"), [data-testid="positions-tab"]',
    "position_row":       '.position-item, [data-testid="position-row"], [class*="positionRow"]',
    "position_pnl_pct":   '.position-pnl-pct, .unrealized-pnl-pct, [class*="pnlPct"]',
    "position_mkt_value": '.position-market-value, [class*="marketValue"]',
    "current_bid":        '.bid-price, [class*="bidPrice"]',
    "current_ask":        '.ask-price, [class*="askPrice"]',
    "current_last":       '.last-price, [class*="lastPrice"]',
}

WEBULL_URL       = "https://app.webull.com/trade"
WEBULL_PAPER_URL = "https://app.webull.com/paper"
USER_DATA_DIR    = os.path.join(os.getcwd(), "webull_profile")


class WebullEngine:
    def __init__(self, paper: bool = True):
        self.paper      = paper
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]           = None
        self._wb        = None   # webull API client
        self._api_ready = False

        # Pending order context (set by navigate_to_option)
        self._pending_ticker      = None
        self._pending_expiry      = None
        self._pending_strike      = None
        self._pending_option_type = None
        self._pending_option_id   = None
        self.last_order_confirmed = False   # set True only when API returns 200

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def open_browser(self):
        """Launch Chromium for login/monitoring, and init the API client."""
        console.print("  Starting Chromium (stealth mode)...")
        self.playwright = Stealth().use_sync(sync_playwright()).start()
        self.context = self.playwright.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
            viewport=None,
        )
        self.page = (self.context.pages[0]
                     if self.context.pages else self.context.new_page())

        url = WEBULL_PAPER_URL if self.paper else WEBULL_URL
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait_for_login()
        console.print("  [green]✓[/green] Logged in and ready.")

        # Init API client in background after browser login
        self._init_api()

    def _wait_for_login(self):
        console.print("\n  [bold yellow]ACTION REQUIRED[/bold yellow]  "
                      "Please log in to Webull in the browser window.")
        console.print("  Waiting for login", end="")
        while True:
            try:
                self.page.wait_for_selector(
                    SELECTORS["logged_in_indicator"], timeout=5_000)
                console.print("  ✓")
                return
            except Exception:
                console.print(".", end="")
                time.sleep(3)

    def _get_token_from_cookies(self) -> bool:
        """
        Read the access token directly from browser cookies.
        Webull stores it as 'web_lt' cookie — starts with 'dc_'.
        """
        try:
            cookies = self.context.cookies()
            token = None
            did   = None
            for c in cookies:
                if c['name'] == 'web_lt' and c['value'].startswith('dc_'):
                    token = c['value']
                if c['name'] == 'web_did':
                    did = c['value']

            if not token:
                # Fallback: parse document.cookie string
                cookie_str = self.page.evaluate("() => document.cookie")
                for part in cookie_str.split(';'):
                    part = part.strip()
                    if part.startswith('web_lt='):
                        token = part.split('=', 1)[1]
                        break

            if token:
                log.info("Token found in cookies (web_lt): %s...", token[:25])
                self._wb._access_token  = token
                self._wb._refresh_token = ""
                self._wb._token_expire  = ""
                self._wb._uuid          = ""
                if did:
                    self._wb._did = did
                    log.info("DID: %s", did)
                self._wb.get_account_id()
                if WEBULL_PIN:
                    self._wb.get_trade_token(WEBULL_PIN)
                self._api_ready = True
                log.info("Webull API ready via cookie token")
                self._write_account_cache()
                return True

            log.warning("web_lt cookie not found")
            return False
        except Exception as e:
            log.warning("Cookie token error: %s", e)
            return False

    def _extract_tokens_from_browser(self) -> bool:
        """
        Extract Webull auth token from sessionStorage, cookies, and
        IndexedDB — wherever Webull actually stores it.
        """
        if not self.page:
            return False
        try:
            import json

            # Check sessionStorage (where Webull actually keeps the token)
            result = self.page.evaluate("""
                () => {
                    // Check sessionStorage
                    const ss = {};
                    for (let i=0; i<sessionStorage.length; i++){
                        const k = sessionStorage.key(i);
                        ss[k] = sessionStorage.getItem(k);
                    }

                    // Check all cookies via document.cookie
                    const cookies = {};
                    document.cookie.split(';').forEach(c => {
                        const [k,v] = c.trim().split('=');
                        if(k) cookies[k.trim()] = v;
                    });

                    // Deep search localStorage for any token-like value
                    const ls = {};
                    for (let i=0; i<localStorage.length; i++){
                        const k = localStorage.key(i);
                        ls[k] = localStorage.getItem(k);
                    }

                    // Try to find token by searching all storage values
                    let token = null, did = null;

                    // Search sessionStorage
                    for (const [k,v] of Object.entries(ss)){
                        if(!v) continue;
                        if(k.toLowerCase().includes('token') && v.startsWith('dc_')){
                            token = v; break;
                        }
                        try {
                            const obj = JSON.parse(v);
                            if(obj && obj.accessToken) { token=obj.accessToken; break; }
                            if(obj && obj.access_token) { token=obj.access_token; break; }
                        } catch(e){}
                    }

                    // Search localStorage deeply (parse JSON values)
                    if(!token){
                        for (const [k,v] of Object.entries(ls)){
                            if(!v || v==='null') continue;
                            try {
                                const obj = JSON.parse(v);
                                // Recursively search object for accessToken
                                const find = (o) => {
                                    if(!o || typeof o !== 'object') return null;
                                    if(o.accessToken && o.accessToken.startsWith('dc_')) return o.accessToken;
                                    if(o.access_token && String(o.access_token).startsWith('dc_')) return o.access_token;
                                    for(const val of Object.values(o)){
                                        const r = find(val);
                                        if(r) return r;
                                    }
                                    return null;
                                };
                                const found = find(obj);
                                if(found){ token=found; break; }
                            } catch(e){
                                // Raw string token?
                                if(v.startsWith('dc_')) { token=v; break; }
                            }
                        }
                    }

                    // Get DID from cookie or storage
                    did = cookies['did'] || ss['did'] || ls['did'] || '';

                    return {
                        token: token,
                        did: did,
                        ss_keys: Object.keys(ss),
                        cookie_str: document.cookie.substring(0,500)
                    };
                }
            """)

            log.info("sessionStorage keys: %s", result.get("ss_keys", []))
            log.info("Cookies preview: %s", result.get("cookie_str", "")[:200])

            token = result.get("token")
            did   = result.get("did", "")

            if token:
                log.info("Token found: %s...", token[:25])
                self._wb._access_token  = token
                self._wb._refresh_token = ""
                self._wb._token_expire  = ""
                self._wb._uuid          = ""
                if did:
                    self._wb._did = did
                self._wb.get_account_id()
                if WEBULL_PIN:
                    self._wb.get_trade_token(WEBULL_PIN)
                self._api_ready = True
                log.info("Webull API ready via browser token")
                return True

            # Token starts with 'dc_' — try intercepting via fetch override
            log.info("Token not in storage — installing fetch interceptor to capture it...")
            self.page.evaluate("""
                () => {
                    window._capturedToken = null;
                    const orig = window.fetch;
                    window.fetch = function(...args) {
                        const req = args[0];
                        const opts = args[1] || {};
                        const headers = opts.headers || {};
                        const token = headers['access_token'] || headers['Access_Token'] || '';
                        if(token && token.startsWith('dc_')){
                            window._capturedToken = token;
                        }
                        return orig.apply(this, args);
                    };
                }
            """)
            # Trigger API calls by navigating to quotes page
            self.page.goto("https://app.webull.com/paper", wait_until="networkidle")
            time.sleep(2)

            token = self.page.evaluate("() => window._capturedToken")
            if token:
                log.info("Token captured via fetch interceptor: %s...", token[:25])
                self._wb._access_token = token
                self._wb.get_account_id()
                if WEBULL_PIN:
                    self._wb.get_trade_token(WEBULL_PIN)
                self._api_ready = True
                return True

            log.warning("Could not capture token from browser")
            return False

        except Exception as e:
            log.warning("Token extraction error: %s", e)
            return False

    def _init_api(self):
        """
        Initialize webull Python API client using saved tokens (no MFA needed).

        Priority:
        1. webull_credentials.json (tokens grabbed from browser Network tab once)
        2. WEBULL_EMAIL + WEBULL_PASSWORD + MFA prompt (full login fallback)

        To get tokens (one time):
          - Open Chrome DevTools on Webull tab
          - Application tab -> Local Storage -> app.webull.com
          - Find accessToken, refreshToken, tokenExpireTime, uuid
          - OR Network tab -> filter 'login' -> check Response JSON
          - Save as webull_credentials.json in WebullTrader folder
        """
        try:
            if self.paper:
                from webull import paper_webull
                self._wb = paper_webull()
            else:
                from webull import webull as wb_cls
                self._wb = wb_cls()
        except ImportError:
            log.warning("webull not installed — run: pip install webull")
            return

        # ── Method 0: Read token from browser cookies (web_lt) ──────────────
        log.info("Attempting to extract tokens from browser session...")
        if self._get_token_from_cookies():
            return   # Done instantly — token is in the cookie
        if self._extract_tokens_from_browser():
            return   # Fallback storage search

        # ── Method 1: Use saved credential tokens (no MFA) ───────────────────
        cred_file = os.path.join(os.getcwd(), "webull_credentials.json")
        if os.path.exists(cred_file):
            try:
                import json
                with open(cred_file) as f:
                    creds = json.load(f)

                self._wb._refresh_token = creds["refreshToken"]
                self._wb._access_token  = creds["accessToken"]
                self._wb._token_expire  = creds["tokenExpireTime"]
                self._wb._uuid          = creds.get("uuid", "")

                # Refresh the token
                n = self._wb.refresh_login()
                if n and "accessToken" in n:
                    creds["accessToken"]     = n["accessToken"]
                    creds["refreshToken"]    = n["refreshToken"]
                    creds["tokenExpireTime"] = n["tokenExpireTime"]
                    with open(cred_file, "w") as f:
                        json.dump(creds, f, indent=2)
                    log.info("Webull API: tokens refreshed from credentials file")

                self._wb.get_account_id()
                if WEBULL_PIN:
                    self._wb.get_trade_token(WEBULL_PIN)
                self._api_ready = True
                log.info("Webull API ready via saved tokens (no MFA needed)")
                return
            except Exception as e:
                log.warning("Token login failed: %s — trying email/password", e)

        # ── Method 2: Skip — browser extraction already failed ───────────────
        log.warning(
            "Could not get API token from browser. "
            "Bot will continue with browser-only monitoring. "
            "Orders cannot be placed until token is captured."
        )

    def _print_token_instructions(self):
        console.print("""
  [yellow]HOW TO GET YOUR WEBULL TOKENS (one time, 2 minutes):[/yellow]

  1. Open Webull in Chrome (you're already logged in)
  2. Press F12 -> Application tab -> Local Storage -> https://app.webull.com
  3. Find and copy these values:
       access_token      -> accessToken
       refresh_token     -> refreshToken
       token_expire      -> tokenExpireTime
       uuid              -> uuid
  4. Create WebullTrader/webull_credentials.json:
       {
         "accessToken":     "dc_us1.xxxxx",
         "refreshToken":    "xxxxx",
         "tokenExpireTime": "2026-05-12T00:00:00.000+0000",
         "uuid":            "xxxxx"
       }
  5. Restart the bot — no MFA ever again!
""")

    def close(self):
        if self.context:    self.context.close()
        if self.playwright: self.playwright.stop()

    # ── Options lookup ─────────────────────────────────────────────────────────

    def navigate_to_option(self, ticker: str, expiry_str: str,
                           strike: float, option_type: str) -> bool:
        """
        Look up the Webull optionId via API for later order placement.
        Falls back to browser navigation if API not available.
        """
        self._pending_ticker      = ticker
        self._pending_expiry      = expiry_str
        self._pending_strike      = strike
        self._pending_option_type = option_type
        self._pending_option_id   = None

        if self._api_ready:
            self._pending_option_id = self._get_option_id_api(
                ticker, expiry_str, strike, option_type)
            if self._pending_option_id:
                log.info("Option found via API: %s %s%s %s -> id=%s",
                         ticker, strike, option_type, expiry_str,
                         self._pending_option_id)
                return True

        # Fallback: browser navigation (for monitoring only, not ordering)
        log.info("API lookup failed — using browser navigation for monitoring")
        return self._navigate_browser(ticker, expiry_str, strike, option_type)

    def _get_option_id_api(self, ticker, expiry, strike, option_type) -> Optional[str]:
        """
        Look up optionId via webull API — EXACT expiry match only.
        SAFETY: Never substitutes a different expiry. If the exact expiry
        is unavailable, returns None and logs available expiries.
        """
        try:
            chain = self._wb.get_options(stock=ticker)
            side  = "call" if option_type.upper() == "C" else "put"
            log.info("get_options returned %d groups for %s", len(chain) if chain else 0, ticker)

            best_id    = None
            best_delta = 999
            available_expiries = set()

            for group in (chain or []):
                opt = group.get(side)
                if not opt or not isinstance(opt, dict):
                    continue
                exp       = opt.get("expireDate", "")
                s         = float(opt.get("strikePrice", 0))
                ticker_id = str(opt.get("tickerId", ""))
                delta     = abs(s - strike)
                if exp:
                    available_expiries.add(exp)

                # ONLY accept exact expiry
                if exp != expiry:
                    continue

                if delta < 0.01:
                    log.info("Exact match: %s %s%s %s tickerId=%s",
                             ticker, s, side[0].upper(), exp, ticker_id)
                    return ticker_id

                if delta < best_delta:
                    best_delta = delta
                    best_id    = ticker_id

            if best_id:
                log.info("Best strike match for exact expiry %s (delta=%.2f): %s",
                         expiry, best_delta, best_id)
                return best_id

            # No exact expiry found — log available and refuse
            avail_sorted = sorted(available_expiries)
            log.warning(
                "No option found for exact expiry: %s %s%s %s. "
                "Available expiries: %s",
                ticker, strike, option_type, expiry,
                ", ".join(avail_sorted) if avail_sorted else "none"
            )
            self._last_available_expiries = avail_sorted
            return None

        except Exception as e:
            log.warning("get_option_id_api error: %s", e)
        return None

    def _navigate_browser(self, ticker, expiry_str, strike, option_type) -> bool:
        """Browser navigation fallback — keeps the chain visible for monitoring."""
        log.info("Browser nav: %s %s%s %s", ticker, strike, option_type, expiry_str)
        try:
            self._search_ticker(ticker)
            self._open_options_tab()
            self._select_expiry(expiry_str)
            self._click_strike(strike, option_type)
            return True
        except Exception as exc:
            log.error("Browser navigate failed: %s", exc)
            return False

    def _search_ticker(self, ticker: str):
        search = self.page.locator(
            'input[placeholder*="Symbol"], input[placeholder*="Search"]').first
        search.click()
        time.sleep(0.4)
        search.fill(ticker)
        time.sleep(1.5)
        for sel in ['[role="option"]', 'li', '[class*="item"]']:
            try:
                items = self.page.locator(sel).filter(has_text=ticker)
                if items.count() > 0:
                    items.first.click()
                    break
            except Exception:
                continue
        time.sleep(1.5)

    def _open_options_tab(self):
        for fn in [
            lambda: self.page.click(':text-is("Options")', timeout=5_000),
            lambda: self.page.click(':text("Options")',    timeout=5_000),
            lambda: self.page.locator('button, a, [role="tab"]')
                    .filter(has_text="Options").first.click(timeout=5_000),
        ]:
            try:
                fn(); time.sleep(1.2); return
            except Exception:
                continue

    def _select_expiry(self, expiry_str: str):
        dt  = datetime.strptime(expiry_str, "%Y-%m-%d")
        mon = dt.strftime("%b")
        day = dt.strftime("%d").lstrip("0") or dt.strftime("%d")
        labels = [f"{mon} {day}", f"{dt.strftime('%m')}/{dt.strftime('%d')}",
                  f"{dt.month}/{day}", expiry_str]
        for label in labels:
            try:
                self.page.click(f':text("{label}")', timeout=3_000)
                time.sleep(0.8); return
            except Exception:
                continue
        log.warning("Could not select expiry %s — using default", expiry_str)

    def _click_strike(self, strike: float, option_type: str):
        strike_int = int(strike)
        try:
            all_cells = self.page.locator("td").all()
            for cell in all_cells:
                try:
                    txt = cell.inner_text(timeout=500).strip()
                    val = float(txt)
                    if 0.01 < val < 20:
                        cell.click(); time.sleep(0.8); return
                except Exception:
                    continue
        except Exception as e:
            log.debug("Strike click error: %s", e)

    # ── Order placement ────────────────────────────────────────────────────────

    def place_order(self, action: str, quantity: int,
                    limit_price: Optional[float] = None,
                    paper: Optional[bool] = None) -> Optional[float]:
        """
        Place an options order.
        Uses webull API if available (reliable), browser as last resort.
        action: 'BTO' | 'STC'
        """
        use_paper = paper if paper is not None else self.paper
        is_buy    = action.upper() in ("BTO", "BUY")

        if not use_paper:
            console.print(f"\n[bold red]REAL ORDER[/bold red]  "
                          f"{action} {quantity}x {self._pending_ticker} "
                          f"{self._pending_strike}{self._pending_option_type} "
                          f"@ {limit_price}")
            if not Confirm.ask("Place this order?", default=False):
                log.warning("Real order aborted.")
                return None

        # ── Primary: webull API ───────────────────────────────────────────────
        if self._api_ready and self._pending_option_id:
            return self._place_via_api(action, quantity, limit_price, is_buy)

        # ── Fallback: log error, don't attempt broken browser click ──────────
        log.error(
            "Cannot place order: API not ready or optionId not found. "
            "Set WEBULL_EMAIL + WEBULL_PASSWORD in .env and restart."
        )
        return None

    def _place_via_api(self, action, quantity, limit_price, is_buy) -> Optional[float]:
        """
        Place option order via the correct paper trading endpoint.
        Sets self.last_order_confirmed = True ONLY when API returns 200.
        All failures leave last_order_confirmed = False so trade_engine
        knows not to start position monitoring.
        """
        import requests as _req
        import uuid as _uuid

        self.last_order_confirmed = False   # reset before every attempt

        api_action = "BUY" if is_buy else "SELL"
        order_type = "LMT" if limit_price else "MKT"
        lmt        = round(limit_price, 2) if limit_price else None

        log.info("API order: %s x%d optionId=%s @ %s (paper=%s)",
                 action, quantity, self._pending_option_id, lmt, self.paper)

        if self.paper:
            paper_base = "https://act.webullbroker.com/webull-paper-center/api"
            url = (f"{paper_base}/paper/1/acc/{self._wb._account_id}"
                   f"/orderop/place/{self._pending_option_id}")
            payload = {
                "orderType":                 order_type,
                "serialId":                  str(_uuid.uuid4()),
                "timeInForce":               "DAY",
                "quantity":                  quantity,
                "action":                    api_action,
                "tickerId":                  int(self._pending_option_id),
                "tickerType":                "OPTION",
                "outsideRegularTradingHour": False,
            }
            if lmt:
                payload["lmtPrice"] = lmt
            try:
                headers = self._wb.build_req_headers(
                    include_trade_token=True, include_time=True)
                r = _req.post(url, json=payload, headers=headers, timeout=10)
                log.info("Paper order response: %s %s", r.status_code, r.text[:300])
                if r.status_code == 200:
                    self.last_order_confirmed = True
                    data = r.json()
                    return float(data.get("avgFilledPrice") or lmt or 0) or lmt
                elif r.status_code == 417:
                    resp = r.json() if r.text else {}
                    log.error(
                        "Paper order rejected (417): %s — market closed or ticker untradable",
                        resp.get("msg", r.text[:100])
                    )
                else:
                    log.error("Paper order failed %s: %s", r.status_code, r.text[:200])
            except Exception as e:
                log.error("Paper order exception: %s", e)

        else:
            # Live trading — check LIVE_TRADING_ENABLED safety gate
            live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
            if not live_enabled:
                log.error(
                    "LIVE ORDER BLOCKED: LIVE_TRADING_ENABLED=false in .env. "
                    "Set LIVE_TRADING_ENABLED=true to allow real orders."
                )
                return None
            try:
                result = self._wb.place_order_option(
                    optionId  = self._pending_option_id,
                    lmtPrice  = str(lmt) if lmt else None,
                    action    = api_action,
                    orderType = order_type,
                    enforce   = "DAY",
                    quant     = quantity,
                )
                log.info("Live order placed: %s", result)
                if isinstance(result, dict):
                    self.last_order_confirmed = True
                    fill = result.get("avgFilledPrice") or result.get("filledValue")
                    return float(fill) if fill else lmt
            except Exception as e:
                log.error("Live order exception: %s", e)

        return None

    # ── Account cache ─────────────────────────────────────────────────────────

    def _write_account_cache(self):
        """Write real account balance + positions to account_cache.json every 30s."""
        try:
            import json as _json
            acc = self.get_account_value()
            if acc and acc.get("net_value"):
                acc["positions"] = self.get_open_positions_from_webull()
                path = os.path.join(os.path.dirname(__file__), "account_cache.json")
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(acc, f, ensure_ascii=True)
                log.info("Account cache updated: net=$%.2f buying_power=$%.2f "
                         "positions=%d paper=%s",
                         acc["net_value"], acc["buying_power"],
                         len(acc["positions"]), acc["paper"])
        except Exception as e:
            log.info("Account cache write failed: %s", e)

    def get_account_value(self) -> dict:
        """Fetch real account balance for paper or live."""
        if not self._api_ready or not self._wb:
            return {}
        try:
            import requests as _req
            headers    = self._wb.build_req_headers(
                include_trade_token=False, include_time=True)
            account_id = self._wb._account_id

            if self.paper:
                paper_base = "https://act.webullbroker.com/webull-paper-center/api"
                url = f"{paper_base}/paper/1/acc/{account_id}"
                r   = _req.get(url, headers=headers, timeout=10)
                if r.ok:
                    d       = r.json()
                    net     = float(d.get("netLiquidation") or
                                    d.get("netLiquidationValue") or
                                    d.get("netAsset") or
                                    d.get("totalMarketValue") or 0)
                    members = {m["key"]: m["value"]
                               for m in d.get("accountMembers", [])}
                    cash    = float(members.get("usableCash") or
                                    members.get("cashBalance") or
                                    d.get("cashBalance") or
                                    d.get("usableCash") or
                                    d.get("dayBuyingPower") or 0)
                    log.info("Paper account: net=$%.2f buying_power=$%.2f", net, cash)
                    return {"net_value": net, "buying_power": cash,
                            "day_pnl": float(d.get("unrealizedProfitLoss") or 0),
                            "paper": True}
            else:
                acc = self._wb.get_account()
                if acc:
                    members = {m["key"]: m["value"]
                               for m in acc.get("accountMembers", [])}
                    log.info("Live account keys: %s", list(members.keys()))
                    net  = float(members.get("netLiquidation") or
                                 members.get("totalNetLiquidationValue") or
                                 members.get("totalAsset") or
                                 members.get("netAsset") or 0)
                    cash = float(members.get("dayBuyingPower") or
                                 members.get("usableCash") or
                                 members.get("cashBalance") or 0)
                    if net == 0 and cash > 0:
                        net = cash
                    log.info("Live account: net=$%.2f buying_power=$%.2f", net, cash)
                    return {"net_value": net, "buying_power": cash,
                            "day_pnl": float(members.get("unrealizedPnL", 0) or 0),
                            "paper": False}
        except Exception as e:
            log.info("get_account_value error: %s", e)
        return {}

    def get_open_positions_from_webull(self) -> list:
        """Fetch open positions from Webull — paper and live."""
        if not self._api_ready or not self._wb:
            return []
        try:
            return self._wb.get_positions() or []
        except Exception as e:
            log.debug("get_positions error: %s", e)
            return []

    # ── Bracket orders ────────────────────────────────────────────────────────

    def place_bracket_orders(self, option_id: str, quantity: int,
                             entry_price: float, tp_pct: float,
                             sl_pct: float) -> dict:
        """
        Place TP (limit sell) + SL (stop-limit sell) on Webull after BTO confirms.
        These live on Webull's servers and execute even if the bot crashes.
        Returns dict with tp_price, sl_price, tp_order_id, sl_order_id.
        """
        import requests as _req
        import uuid as _uuid

        results = {"tp_price": None, "sl_price": None,
                   "tp_order_id": None, "sl_order_id": None}

        if not self._api_ready or not option_id or not entry_price:
            return results

        tp_price = round(entry_price * (1 + tp_pct / 100), 2)
        sl_price = round(entry_price * (1 - sl_pct / 100), 2)
        sl_stop  = round(sl_price * 1.02, 2)   # stop triggers 2% above limit
        results["tp_price"] = tp_price
        results["sl_price"] = sl_price

        headers = self._wb.build_req_headers(
            include_trade_token=True, include_time=True)

        if self.paper:
            paper_base = "https://act.webullbroker.com/webull-paper-center/api"
            url = (f"{paper_base}/paper/1/acc/{self._wb._account_id}"
                   f"/orderop/place/{option_id}")
        else:
            live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
            if not live_enabled:
                log.warning("Bracket orders blocked: LIVE_TRADING_ENABLED=false")
                return results
            url = None   # live uses library method below

        def _place_paper(order_type, lmt, aux=None, label=""):
            payload = {
                "orderType":                 order_type,
                "serialId":                  str(_uuid.uuid4()),
                "timeInForce":               "GTC",
                "quantity":                  quantity,
                "action":                    "SELL",
                "tickerId":                  int(option_id),
                "tickerType":                "OPTION",
                "outsideRegularTradingHour": False,
                "lmtPrice":                  lmt,
            }
            if aux:
                payload["auxPrice"] = aux
            try:
                r = _req.post(url, json=payload, headers=headers, timeout=10)
                log.info("Bracket %s response: %s %s", label, r.status_code, r.text[:150])
                if r.status_code == 200:
                    return r.json().get("orderId")
            except Exception as e:
                log.error("Bracket %s error: %s", label, e)
            return None

        if self.paper:
            results["tp_order_id"] = _place_paper("LMT", tp_price, label="TP")
            results["sl_order_id"] = _place_paper("STP", sl_price, aux=sl_stop, label="SL")
        else:
            # Live bracket via webull library
            try:
                tp_r = self._wb.place_order_option(
                    optionId=option_id, lmtPrice=str(tp_price),
                    action="SELL", orderType="LMT", enforce="GTC", quant=quantity)
                results["tp_order_id"] = tp_r.get("orderId") if isinstance(tp_r, dict) else None
                sl_r = self._wb.place_order_option(
                    optionId=option_id, lmtPrice=str(sl_price),
                    action="SELL", orderType="STP", enforce="GTC", quant=quantity)
                results["sl_order_id"] = sl_r.get("orderId") if isinstance(sl_r, dict) else None
            except Exception as e:
                log.error("Live bracket orders error: %s", e)

        log.info("Bracket orders: TP=$%.2f (id=%s) SL=$%.2f (id=%s)",
                 tp_price, results["tp_order_id"],
                 sl_price, results["sl_order_id"])
        return results

    # ── Position monitoring (browser) ─────────────────────────────────────────

    def get_position_pnl(self, ticker: str, strike: float,
                         option_type: str) -> Optional[dict]:
        """Check P&L via API first, browser as fallback."""
        # Try API
        if self._api_ready:
            try:
                positions = self._wb.get_positions()
                for pos in positions:
                    sym = pos.get("ticker", {}).get("symbol", "")
                    if sym == ticker:
                        unr  = pos.get("unrealizedProfitLoss")
                        cost = pos.get("cost")
                        val  = pos.get("marketValue")
                        pnl  = (float(unr)/float(cost)*100
                                if unr and cost and float(cost) != 0 else None)
                        return {"pnl_pct": round(pnl, 2) if pnl else None,
                                "market_value": float(val) if val else None}
            except Exception as e:
                log.debug("API position error: %s", e)

        # Browser fallback
        if not self.page:
            return None
        try:
            self.page.click(SELECTORS["positions_tab"], timeout=5_000)
            time.sleep(1.2)
            rows  = self.page.locator(SELECTORS["position_row"])
            count = rows.count()
            for i in range(count):
                row  = rows.nth(i)
                text = row.inner_text()
                if ticker in text and (str(int(strike)) in text or option_type in text):
                    pnl_text = self._safe_inner_text(
                        row.locator(SELECTORS["position_pnl_pct"]))
                    val_text = self._safe_inner_text(
                        row.locator(SELECTORS["position_mkt_value"]))
                    return {"pnl_pct":      self._parse_pct(pnl_text),
                            "market_value": self._parse_float(val_text)}
        except Exception as exc:
            log.debug("Browser position error: %s", exc)
        return None

    def get_option_quote(self, ticker: str) -> Optional[dict]:
        """
        Get option quote via API.
        Webull returns bid/ask inside nested structure:
          q["data"][0]["bidList"][0]["price"]
          q["data"][0]["askList"][0]["price"]
        """
        if self._api_ready and self._pending_option_id:
            try:
                q = self._wb.get_option_quote(
                    stock=ticker, optionId=self._pending_option_id)
                # Parse nested structure
                data = q.get("data", []) if isinstance(q, dict) else []
                if data and isinstance(data, list):
                    opt      = data[0]
                    bid_list = opt.get("bidList", [])
                    ask_list = opt.get("askList", [])
                    bid  = float(bid_list[0]["price"]) if bid_list else 0.0
                    ask  = float(ask_list[0]["price"]) if ask_list else 0.0
                    last = float(opt.get("close", 0) or 0)
                else:
                    # Fallback: try flat keys
                    bid  = float(q.get("bidPrice", 0) or 0)
                    ask  = float(q.get("askPrice", 0) or 0)
                    last = float(q.get("close", 0) or 0)

                if bid == 0 and ask == 0:
                    log.warning("get_option_quote: bid and ask both 0 for %s", ticker)
                    return None

                return {"bid": bid, "ask": ask, "last": last}
            except Exception as e:
                log.debug("API quote error: %s", e)

        if not self.page:
            return None
        try:
            return {
                "bid":  self._parse_float(self._safe_inner_text(
                    self.page.locator(SELECTORS["current_bid"]).first)),
                "ask":  self._parse_float(self._safe_inner_text(
                    self.page.locator(SELECTORS["current_ask"]).first)),
                "last": self._parse_float(self._safe_inner_text(
                    self.page.locator(SELECTORS["current_last"]).first)),
            }
        except Exception as e:
            log.debug("Browser quote error: %s", e)
        return None

    def get_quote(self, ticker: str) -> Optional[dict]:
        """Get stock quote via API."""
        if self._api_ready:
            try:
                q = self._wb.get_quote(stock=ticker)
                return {
                    "ticker":    ticker,
                    "bid":       float(q.get("bidList", [{}])[0].get("price", 0)),
                    "ask":       float(q.get("askList", [{}])[0].get("price", 0)),
                    "last":      float(q.get("close", 0)),
                    "volume":    float(q.get("volume", 0)),
                    "timestamp": time.time(),
                }
            except Exception as e:
                log.debug("API get_quote error: %s", e)
        return None

    def is_market_open(self) -> bool:
        from datetime import time as dtime
        import pytz
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        return (dtime(9, 30) <= now.time() <= dtime(16, 0)
                and now.weekday() < 5)

    def minutes_to_open(self) -> int:
        from datetime import timedelta
        import pytz
        ET  = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        ot  = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now > ot:
            ot += timedelta(days=1)
        return max(0, int((ot - now).total_seconds() / 60))

    # ── Utilities ──────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_inner_text(locator) -> str:
        try:   return locator.inner_text(timeout=2_000)
        except: return ""

    @staticmethod
    def _parse_float(text: str) -> Optional[float]:
        try:   return float(re.sub(r"[^\d.\-]", "", text))
        except: return None

    @staticmethod
    def _parse_pct(text: str) -> Optional[float]:
        try:   return float(re.sub(r"[^\d.\-]", "", text))
        except: return None