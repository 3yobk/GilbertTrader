"""
webull_engine.py  —  Webull Browser Automation

Opens a persistent Chromium session, navigates to Webull,
waits for manual login, then exposes methods to place and monitor options orders.

All selectors are in the SELECTORS dict at the top — update them here
when Webull changes their UI (they do this often).
"""

import os
import re
import time
import logging
from typing import Optional
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright_stealth import Stealth
from rich.console import Console
from rich.prompt import Confirm

log     = logging.getLogger("webull")
console = Console()

# ── DOM selectors (update here when Webull changes their UI) ──────────────────
SELECTORS = {
    # Login detection — when this element appears, you're logged in
    "logged_in_indicator": '[data-testid="portfolio-value"], .account-info, .nav-user-info, :text("Net Account Value"), :text("$")',

    # Search / ticker
    "search_input":        'input[placeholder*="Symbol"], input[placeholder*="Search"]',
    "search_result_item":  '.symbol-search-item, [data-testid="search-result-item"]',

    # Options chain
    "options_tab":         'text=Options, [data-testid="options-tab"]',
    "expiry_container":    '.expiry-select, .expiration-date-container',
    "expiry_button":       '.expiry-item, .expiration-date-item',
    "chain_call_cell":     '.call-side .option-row, .options-call-row',
    "chain_put_cell":      '.put-side .option-row, .options-put-row',
    "strike_price_cell":   '.strike-price',

    # Order ticket (appears when you click a strike)
    "order_ticket":        '.order-ticket, .trade-panel',
    "order_side_buy":      'button:has-text("Buy"), [data-testid="buy-button"]',
    "order_side_sell":     'button:has-text("Sell"), [data-testid="sell-button"]',
    "qty_input":           'input[aria-label*="Quantity"], input[placeholder*="Qty"]',
    "limit_price_input":   'input[aria-label*="Limit"], input[aria-label*="Price"]',
    "order_type_select":   'select[aria-label*="Order Type"], .order-type-selector',
    "review_button":       'button:has-text("Review Order"), button:has-text("Preview")',
    "confirm_button":      'button:has-text("Place Order"), button:has-text("Confirm")',

    # Paper trading toggle / banner
    "paper_mode_badge":    'text=Paper, .paper-trading-badge',

    # Portfolio / P&L
    "positions_tab":       'text=Positions, [data-testid="positions-tab"]',
    "position_row":        '.position-item, [data-testid="position-row"]',
    "position_ticker":     '.position-symbol, .ticker-symbol',
    "position_pnl_pct":    '.position-pnl-pct, .unrealized-pnl-pct',
    "position_mkt_value":  '.position-market-value',
    "current_bid":         '.bid-price',
    "current_ask":         '.ask-price',
    "current_last":        '.last-price',
}

WEBULL_URL       = "https://app.webull.com/trade"
WEBULL_PAPER_URL = "https://app.webull.com/paper-trade"
USER_DATA_DIR    = os.path.join(os.getcwd(), "webull_profile")


class WebullEngine:
    def __init__(self, paper: bool = True):
        self.paper      = paper
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]           = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open_browser(self):
        """
        Launch Chromium with stealth, navigate to Webull, then block
        until you manually complete login.  Uses a persistent profile so
        you stay logged in across restarts.
        """
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
            viewport=None,   # let the window use its natural size
        )
        self.page = (self.context.pages[0]
                     if self.context.pages else self.context.new_page())

        url = WEBULL_PAPER_URL if self.paper else WEBULL_URL
        self.page.goto(url, wait_until="domcontentloaded")

        self._wait_for_login()
        console.print("  [green]✓[/green] Logged in and ready.")

    def _wait_for_login(self):
        """Poll until the post-login dashboard element appears."""
        console.print("\n  [bold yellow]ACTION REQUIRED[/bold yellow]  "
                      "Please log in to Webull in the browser window.")
        console.print("  Waiting for login", end="")
        while True:
            try:
                # This checks if you are logged in
                self.page.wait_for_selector(
                    SELECTORS["logged_in_indicator"],
                    timeout=5_000,
                )
                console.print("  ✓")
                return
            except Exception:
                # REMOVED 'flush=True' to fix the Mac crash
                console.print(".", end="")
                time.sleep(3)


    def close(self):
        if self.context:  self.context.close()
        if self.playwright: self.playwright.stop()

    # ── Options chain navigation ──────────────────────────────────────────────

    def navigate_to_option(self, ticker: str, expiry_str: str,
                            strike: float, option_type: str) -> bool:
        """
        Search for ticker, open options chain, select the right
        expiry + strike + call/put cell.  Returns True on success.
        """
        log.info("Navigating to %s %s%s exp=%s", ticker, strike, option_type, expiry_str)
        try:
            self._search_ticker(ticker)
            self._open_options_tab()
            self._select_expiry(expiry_str)
            self._click_strike(strike, option_type)
            return True
        except Exception as exc:
            log.error("navigate_to_option failed: %s", exc)
            return False

    def _search_ticker(self, ticker: str):
        self.page.click(SELECTORS["search_input"])
        self.page.fill(SELECTORS["search_input"], ticker)
        time.sleep(0.8)
        # Click the first matching result
        first = self.page.locator(SELECTORS["search_result_item"]).first
        first.click()
        time.sleep(1.2)

    def _open_options_tab(self):
        self.page.click(SELECTORS["options_tab"])
        time.sleep(1)

    def _select_expiry(self, expiry_str: str):
        """
        expiry_str is YYYY-MM-DD.
        Webull shows dates like 'May 15' or '05/15' — try both formats.
        """
        dt      = datetime.strptime(expiry_str, "%Y-%m-%d")
        labels  = [dt.strftime("%b %-d"), dt.strftime("%m/%d"), dt.strftime("%-m/%-d")]
        container = self.page.locator(SELECTORS["expiry_container"])
        for label in labels:
            try:
                container.locator(f"text={label}").first.click(timeout=3_000)
                time.sleep(0.8)
                return
            except Exception:
                continue
        raise ValueError(f"Expiry {expiry_str} not found in chain (tried {labels})")

    def _click_strike(self, strike: float, option_type: str):
        """Click the bid/ask cell for the matching strike."""
        strike_int = int(strike)
        selector   = (SELECTORS["chain_call_cell"] if option_type == "C"
                       else SELECTORS["chain_put_cell"])
        rows = self.page.locator(selector)
        count = rows.count()
        for i in range(count):
            row  = rows.nth(i)
            text = row.inner_text()
            if str(strike_int) in text or str(strike) in text:
                row.click()
                time.sleep(0.8)
                return
        raise ValueError(f"Strike {strike}{option_type} not found in chain")

    # ── Order placement ───────────────────────────────────────────────────────

    def place_order(self, action: str, quantity: int,
                    limit_price: Optional[float] = None,
                    paper: Optional[bool] = None) -> Optional[float]:
        """
        Fill the order ticket and submit.
        action: "BTO" (buy to open) | "STC" (sell to close)
        Returns the confirmed fill price, or None if it can't be scraped.
        """
        use_paper = paper if paper is not None else self.paper
        is_buy    = action.upper() in ("BTO", "BUY")

        log.info("Placing order: %s x%d @ %s (paper=%s)",
                 action, quantity, limit_price or "market", use_paper)

        # Wait for order ticket to open
        self.page.wait_for_selector(SELECTORS["order_ticket"], timeout=10_000)

        # Buy / Sell side
        side_selector = SELECTORS["order_side_buy"] if is_buy else SELECTORS["order_side_sell"]
        self.page.click(side_selector)
        time.sleep(0.4)

        # Quantity
        qty_el = self.page.locator(SELECTORS["qty_input"])
        qty_el.triple_click()
        qty_el.type(str(quantity))

        # Limit price (clear existing value, type new one)
        if limit_price is not None:
            price_el = self.page.locator(SELECTORS["limit_price_input"])
            price_el.triple_click()
            price_el.type(f"{limit_price:.2f}")
        time.sleep(0.4)

        # Review
        self.page.click(SELECTORS["review_button"])
        time.sleep(1.2)

        # Confirm — real money requires terminal confirmation
        if not use_paper:
            console.print(f"\n[bold red]⚠️  REAL ORDER REVIEW[/bold red]  "
                          f"{action} {quantity}x | Limit: {limit_price}")
            if not Confirm.ask("Place this order?", default=False):
                self.page.keyboard.press("Escape")
                log.warning("Real order aborted by operator.")
                return None

        self.page.click(SELECTORS["confirm_button"])
        time.sleep(2)

        return self._scrape_fill_price()

    def _scrape_fill_price(self) -> Optional[float]:
        """Try to grab the confirmed fill price from the post-order banner."""
        for selector in [SELECTORS["current_last"], SELECTORS["current_bid"]]:
            try:
                el   = self.page.locator(selector).first
                text = el.inner_text(timeout=4_000)
                return float(re.sub(r"[^\d.]", "", text))
            except Exception:
                pass
        return None

    # ── Live position data ────────────────────────────────────────────────────

    def get_position_pnl(self, ticker: str, strike: float,
                          option_type: str) -> Optional[dict]:
        """
        Navigate to Positions tab and scrape P&L for the given contract.
        Returns dict with keys: pnl_pct, market_value, current_price
        Returns None if position not found.
        """
        try:
            self.page.click(SELECTORS["positions_tab"])
            time.sleep(1.2)

            rows  = self.page.locator(SELECTORS["position_row"])
            count = rows.count()
            label = f"{ticker} {strike}{option_type}"

            for i in range(count):
                row  = rows.nth(i)
                text = row.inner_text()
                if ticker in text and (str(int(strike)) in text or option_type in text):
                    pnl_text = self._safe_inner_text(
                        row.locator(SELECTORS["position_pnl_pct"]))
                    val_text = self._safe_inner_text(
                        row.locator(SELECTORS["position_mkt_value"]))
                    pnl_pct  = self._parse_pct(pnl_text)
                    mkt_val  = self._parse_float(val_text)
                    log.debug("Position %s: pnl=%.1f%% val=%.2f", label, pnl_pct or 0, mkt_val or 0)
                    return {"pnl_pct": pnl_pct, "market_value": mkt_val}
        except Exception as exc:
            log.debug("get_position_pnl error: %s", exc)
        return None

    def get_option_quote(self, ticker: str) -> Optional[dict]:
        """Return current bid/ask/last from the open options chain page."""
        try:
            bid  = self._safe_inner_text(self.page.locator(SELECTORS["current_bid"]).first)
            ask  = self._safe_inner_text(self.page.locator(SELECTORS["current_ask"]).first)
            last = self._safe_inner_text(self.page.locator(SELECTORS["current_last"]).first)
            return {
                "bid":  self._parse_float(bid),
                "ask":  self._parse_float(ask),
                "last": self._parse_float(last),
            }
        except Exception as exc:
            log.debug("get_option_quote error: %s", exc)
            return None

    # ── Utilities ─────────────────────────────────────────────────────────────

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
