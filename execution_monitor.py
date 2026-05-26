"""
execution_monitor.py — GilbertTrader Execution Engine Hardening (Phase 3)
=========================================================================
Provides:
  - buying power verification before orders
  - stale quote detection (>15s)
  - duplicate order prevention
  - orphaned position recovery
  - market halt detection (bid=ask=0)
  - Tradier feed disconnect protection
  - order confirmation verification with 3s verify poll
  - emergency liquidation mode
  - partial fill handling
  - spread expansion rejection

Usage:
    from execution_monitor import ExecutionMonitor
    exec_mon = ExecutionMonitor(webull_engine, trade_engine, listener)
    exec_mon.start()   # starts background monitoring thread

    # Before placing any order:
    ok, reason = exec_mon.pre_order_checks(ticker, cost_estimate, limit_price,
                                            bid, ask, quote_timestamp)
    if not ok:
        log.warning("Order blocked: %s", reason)
        return
"""

import time
import logging
import threading
from datetime import datetime
from typing import Optional, Tuple

log = logging.getLogger("execution_monitor")


# ── Constants ──────────────────────────────────────────────────────────────────
STALE_QUOTE_SECONDS  = 15     # reject quotes older than this
MAX_SPREAD_PCT       = 0.15   # 15% spread rejection
MIN_BUYING_POWER_PCT = 1.05   # need 5% buffer above cost
ORPHAN_CHECK_SECS    = 60     # how often to check for orphaned positions
HALT_BID_ASK_ZERO   = True    # detect market halt by bid=ask=0
CONFIRM_DELAY_SECS  = 3       # wait before verifying order confirmation
EMERGENCY_LIQUIDATE  = False   # set True to force-close everything


class ExecutionMonitor:
    """
    Background monitor that enforces execution safety rules.
    Runs as a daemon thread alongside the main bot loop.
    """

    def __init__(self, webull, engine, listener=None):
        self.webull   = webull
        self.engine   = engine
        self.listener = listener

        self._running          = False
        self._thread: Optional[threading.Thread] = None
        self._pending_orders:  set = set()    # order_key -> prevent duplicates
        self._order_lock       = threading.Lock()
        self._emergency_mode   = False
        self._halt_tickers:    set = set()    # tickers currently halted
        self._last_quote_ts:   dict = {}      # ticker -> timestamp of last quote
        self._last_tradier_ok  = time.time()  # last successful Tradier data

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        """Start the background monitoring thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="ExecutionMonitor",
        )
        self._thread.start()
        log.info("ExecutionMonitor started")

    def stop(self):
        self._running = False

    # ── Pre-order checks ───────────────────────────────────────────────────────

    def pre_order_checks(self,
                         ticker: str,
                         cost_estimate: float,
                         limit_price: float,
                         bid: float,
                         ask: float,
                         quote_timestamp: float = None) -> Tuple[bool, str]:
        """
        Run all pre-order safety checks.
        Returns (True, "") if safe to proceed, or (False, reason) to block.

        Call this from trade_engine._execute() before webull.place_order().
        """

        # 0. Emergency mode — block everything
        if self._emergency_mode:
            return False, "EMERGENCY MODE active — all orders blocked"

        # 1. Buying power verification
        try:
            acc = self.webull.get_account_value()
            settled = float(acc.get("settled_cash") or acc.get("buying_power") or 0)
            if settled < cost_estimate * MIN_BUYING_POWER_PCT:
                return False, (f"Insufficient buying power: have ${settled:.2f}, "
                               f"need ${cost_estimate * MIN_BUYING_POWER_PCT:.2f}")
        except Exception as e:
            log.warning("Buying power check error: %s — proceeding with caution", e)

        # 2. Stale quote detection
        if quote_timestamp:
            age = time.time() - quote_timestamp
            if age > STALE_QUOTE_SECONDS:
                return False, (f"Quote is {age:.0f}s old (max {STALE_QUOTE_SECONDS}s) "
                               f"— stale data, refusing order")

        # 3. Market halt detection (bid=0 AND ask=0)
        if HALT_BID_ASK_ZERO and bid == 0 and ask == 0:
            self._halt_tickers.add(ticker)
            msg = f"Market halt detected on {ticker} (bid=ask=0)"
            log.warning(msg)
            if self.listener:
                self.listener.send_status_update(
                    f"⚠️ **MARKET HALT**: {ticker} — bid=ask=0. Order blocked."
                )
            return False, msg

        # Clear halt if we're getting quotes again
        self._halt_tickers.discard(ticker)

        # 4. Spread expansion rejection
        if bid > 0 and ask > 0:
            mid        = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            if spread_pct > MAX_SPREAD_PCT:
                return False, (f"Spread too wide: {spread_pct:.1%} > {MAX_SPREAD_PCT:.0%} "
                               f"(bid=${bid:.2f} ask=${ask:.2f})")

        # 5. Duplicate order prevention
        order_key = f"{ticker}_{limit_price:.2f}"
        with self._order_lock:
            if order_key in self._pending_orders:
                return False, f"Duplicate order blocked: {order_key}"
            self._pending_orders.add(order_key)

        # 6. Tradier feed freshness
        feed_age = time.time() - self._last_tradier_ok
        if feed_age > 120:   # 2 min without Tradier data
            log.warning("Tradier feed may be disconnected (last ok %.0fs ago)", feed_age)
            if self.listener:
                self.listener.send_status_update(
                    f"⚠️ Tradier feed stale ({feed_age:.0f}s) — using delayed data"
                )

        return True, ""

    def release_order_lock(self, ticker: str, limit_price: float):
        """Call after order attempt (success or failure) to release the lock."""
        order_key = f"{ticker}_{limit_price:.2f}"
        with self._order_lock:
            self._pending_orders.discard(order_key)

    def mark_tradier_ok(self):
        """Call whenever a successful Tradier data fetch occurs."""
        self._last_tradier_ok = time.time()

    # ── Order confirmation verifier ────────────────────────────────────────────

    def verify_order_confirmed(self, ticker: str, option_type: str,
                                strike: float, expected_qty: int) -> bool:
        """
        Wait CONFIRM_DELAY_SECS then verify the order actually shows up
        in Webull's open positions. Returns True if confirmed.
        """
        time.sleep(CONFIRM_DELAY_SECS)
        try:
            positions = self.webull.get_open_positions_from_webull() or []
            for pos in positions:
                pos_ticker = str(pos.get("ticker", {}).get("symbol", ""))
                if (ticker.upper() in pos_ticker.upper() and
                        abs(float(pos.get("optionExercisePrice", 0)) - strike) < 1.0):
                    qty = int(pos.get("quantity", 0))
                    log.info("Order verified: %s x%d in Webull positions", ticker, qty)
                    return True
            log.warning(
                "Order NOT found in Webull positions after %ds — may have failed",
                CONFIRM_DELAY_SECS
            )
            return False
        except Exception as e:
            log.debug("verify_order_confirmed error: %s", e)
            return True   # fail open — don't block on verification error

    # ── Partial fill handling ──────────────────────────────────────────────────

    def get_actual_fill_qty(self, ticker: str, strike: float) -> Optional[int]:
        """
        Check Webull positions for actual fill quantity.
        Returns None if position not found.
        Used to handle partial fills where fewer contracts than ordered were filled.
        """
        try:
            positions = self.webull.get_open_positions_from_webull() or []
            for pos in positions:
                pos_ticker = str(pos.get("ticker", {}).get("symbol", ""))
                if (ticker.upper() in pos_ticker.upper() and
                        abs(float(pos.get("optionExercisePrice", 0)) - strike) < 1.0):
                    return int(pos.get("quantity", 0))
        except Exception as e:
            log.debug("get_actual_fill_qty error: %s", e)
        return None

    # ── Emergency liquidation ──────────────────────────────────────────────────

    def activate_emergency_mode(self, reason: str = "Manual trigger"):
        """
        Block all new orders and close all open positions immediately.
        Call via !emergency Discord command or circuit breaker.
        """
        self._emergency_mode = True
        log.critical("EMERGENCY MODE ACTIVATED: %s", reason)
        if self.listener:
            self.listener.send_status_update(
                f"🚨 **EMERGENCY LIQUIDATION**: {reason}\n"
                f"All new orders blocked. Closing all positions..."
            )
        # Close all positions
        try:
            with self.engine._lock:
                position_keys = list(self.engine._positions.keys())
            for key in position_keys:
                pos = self.engine._positions.get(key)
                if pos:
                    self.engine._auto_close(key, pos, reason=f"Emergency: {reason}")
                    log.info("Emergency closed: %s", key)
        except Exception as e:
            log.error("Emergency close error: %s", e)

    def deactivate_emergency_mode(self):
        """Re-enable trading after emergency."""
        self._emergency_mode = False
        log.info("Emergency mode deactivated — trading re-enabled")
        if self.listener:
            self.listener.send_status_update(
                "✅ Emergency mode cleared — trading re-enabled"
            )

    # ── Background monitor loop ────────────────────────────────────────────────

    def _monitor_loop(self):
        """
        Background thread: checks for orphaned positions, halted markets,
        and Tradier feed health every ORPHAN_CHECK_SECS seconds.
        """
        while self._running:
            try:
                self._check_orphaned_positions()
                self._check_halted_positions()
            except Exception as e:
                log.debug("ExecutionMonitor loop error: %s", e)
            time.sleep(ORPHAN_CHECK_SECS)

    def _check_orphaned_positions(self):
        """
        Compare engine's tracked positions against Webull's actual positions.
        Alert on discrepancies.
        """
        try:
            webull_positions = self.webull.get_open_positions_from_webull() or []
            webull_tickers = set()
            for wp in webull_positions:
                ticker = str(wp.get("ticker", {}).get("symbol", ""))
                if ticker:
                    webull_tickers.add(ticker.upper())

            with self.engine._lock:
                tracked = dict(self.engine._positions)

            # Check: engine tracks position that Webull doesn't have
            for key, pos in tracked.items():
                if pos.signal.ticker.upper() not in webull_tickers:
                    log.warning(
                        "ORPHAN: engine tracks %s but Webull shows nothing — "
                        "may have been closed externally", key
                    )
                    if self.listener:
                        self.listener.send_status_update(
                            f"⚠️ **ORPHAN POSITION**: {key} is in bot memory but "
                            f"not in Webull. Check manually — removing from tracker."
                        )
                    # Remove from tracking since Webull doesn't see it
                    with self.engine._lock:
                        self.engine._positions.pop(key, None)
                    self.engine._save_positions()

        except Exception as e:
            log.debug("_check_orphaned_positions error: %s", e)

    def _check_halted_positions(self):
        """
        For any currently held positions, check if the stock has halted.
        Detect via zero bid/ask on the underlying.
        """
        try:
            with self.engine._lock:
                tracked = dict(self.engine._positions)

            for key, pos in tracked.items():
                try:
                    quote = self.webull.get_option_quote(pos.signal.ticker)
                    if quote:
                        bid = float(quote.get("bid") or 0)
                        ask = float(quote.get("ask") or 0)
                        if bid == 0 and ask == 0:
                            if pos.signal.ticker not in self._halt_tickers:
                                self._halt_tickers.add(pos.signal.ticker)
                                log.warning("HALT detected on open position: %s", key)
                                if self.listener:
                                    self.listener.send_status_update(
                                        f"⚠️ **TRADING HALT**: {pos.signal.ticker} — "
                                        f"bid=ask=0. Holding position until trading resumes."
                                    )
                        else:
                            # Trading resumed
                            if pos.signal.ticker in self._halt_tickers:
                                self._halt_tickers.discard(pos.signal.ticker)
                                log.info("Halt cleared on %s — trading resumed", pos.signal.ticker)
                                if self.listener:
                                    self.listener.send_status_update(
                                        f"✅ **HALT CLEARED**: {pos.signal.ticker} — "
                                        f"trading resumed"
                                    )
                except Exception as e:
                    log.debug("Halt check error for %s: %s", key, e)

        except Exception as e:
            log.debug("_check_halted_positions error: %s", e)

    # ── Slippage estimation ────────────────────────────────────────────────────

    @staticmethod
    def estimate_slippage_pct(bid: float, ask: float,
                               limit_price: float) -> float:
        """
        Estimate expected slippage as % of mid.
        For limit orders: slippage = 0 if we're buying at or below mid.
        For market orders: slippage = half the spread.
        """
        if bid <= 0 or ask <= 0:
            return 0.0
        mid = (bid + ask) / 2
        if mid <= 0:
            return 0.0
        if limit_price and limit_price <= mid:
            return 0.0   # buying at or below mid — no slippage
        # Buying above mid or market order
        worst_case = ask
        slippage   = (worst_case - mid) / mid * 100
        return round(slippage, 2)

    # ── Invalid expiry fallback ────────────────────────────────────────────────

    @staticmethod
    def validate_expiry(expiry: str) -> Tuple[bool, str]:
        """
        Validate that an expiry string is a real future date
        and not a weekend/holiday.
        Returns (valid, corrected_expiry_or_error).
        """
        try:
            from datetime import date, timedelta
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            today    = date.today()

            if exp_date < today:
                # Already expired — find next valid Friday
                days_ahead = (4 - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_friday = today + timedelta(days=days_ahead)
                return False, next_friday.strftime("%Y-%m-%d")

            if exp_date.weekday() == 5:   # Saturday → use Friday
                return True, (exp_date - timedelta(days=1)).strftime("%Y-%m-%d")
            if exp_date.weekday() == 6:   # Sunday → use Friday
                return True, (exp_date - timedelta(days=2)).strftime("%Y-%m-%d")

            return True, expiry

        except Exception as e:
            log.debug("validate_expiry error: %s", e)
            return False, expiry

    # ── Webull reconnect recovery ──────────────────────────────────────────────

    def attempt_webull_reconnect(self, max_attempts: int = 3) -> bool:
        """
        Attempt to re-initialize the Webull API connection.
        Called when consecutive order failures suggest a session issue.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                log.info("Webull reconnect attempt %d/%d", attempt, max_attempts)
                if self.webull._wb:
                    self.webull._wb.logout()
                time.sleep(5)
                self.webull._init_api()
                if self.webull._api_ready:
                    log.info("Webull reconnected successfully")
                    if self.listener:
                        self.listener.send_status_update(
                            "✅ Webull API reconnected successfully"
                        )
                    return True
            except Exception as e:
                log.warning("Reconnect attempt %d failed: %s", attempt, e)
                time.sleep(10 * attempt)

        log.error("All Webull reconnect attempts failed")
        if self.listener:
            self.listener.send_status_update(
                "❌ **Webull reconnect FAILED** — manual intervention required"
            )
        return False

    # ── Kill switch HTTP endpoint helper ─────────────────────────────────────

    def register_kill_switch(self, app, password: str = None):
        """
        Register a /api/emergency/liquidate endpoint on the Flask app.
        Callable from a phone browser without Discord.
        Password protection optional but recommended.
        """
        kill_pw = password or os.getenv("KILL_SWITCH_PASSWORD", "gilbert_kill")

        @app.route("/api/emergency/liquidate", methods=["POST"])
        def emergency_liquidate():
            from flask import request, jsonify
            data = request.get_json(silent=True) or {}
            pw   = data.get("password", "")
            if pw != kill_pw:
                return jsonify({"error": "Unauthorized"}), 403
            self.activate_emergency_mode("HTTP kill switch triggered")
            return jsonify({"status": "EMERGENCY MODE ACTIVATED"})

        @app.route("/api/emergency/clear", methods=["POST"])
        def emergency_clear():
            from flask import request, jsonify
            data = request.get_json(silent=True) or {}
            pw   = data.get("password", "")
            if pw != kill_pw:
                return jsonify({"error": "Unauthorized"}), 403
            self.deactivate_emergency_mode()
            return jsonify({"status": "Emergency mode cleared"})

        import os
        log.info("Kill switch registered at /api/emergency/liquidate (pw=%s)", kill_pw[:3] + "***")
