"""
trade_engine.py — Signal Parser + Trade Executor + P&L Monitor
Includes: Breakeven Guard, Time Decay Exit, CSV Data Logging,
          get_session_pnl() (circuit breaker fix),
          Trailing Stop (upgrade), Scale-Out at 50% (upgrade)
"""

import re
import time
import logging
import threading
import csv
import os
import pandas as pd
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

log     = logging.getLogger("trade_engine")
console = Console()

# ── Signal pattern — covers most common paid channel formats ──────────────────
_SIGNAL_RE = re.compile(
    r"(?P<action>BTO|STC|BTC|STO|BUY\s+TO\s+OPEN|SELL\s+TO\s+CLOSE|BUY|SELL)"
    r".*?"
    r"(?P<ticker>[A-Z]{1,5})"
    r"\s*"
    r"(?P<strike>\d+(?:\.\d+)?)"
    r"(?P<option_type>[CP])\b"
    r".*?"
    r"(?P<expiry_month>\d{1,2})/(?P<expiry_day>\d{1,2})"
    r"(?:/(?P<expiry_year>\d{2,4}))?"
    r"(?:.*?x(?P<qty>\d+))?",
    re.IGNORECASE | re.DOTALL,
    )

ACTION_MAP = {
    "BTO": "BTO", "BUY TO OPEN": "BTO", "BUY": "BTO",
    "STC": "STC", "SELL TO CLOSE": "STC", "BTC": "STC", "STO": "STO", "SELL": "STC",
}


@dataclass
class TradeSignal:
    ticker:      str
    strike:      float
    option_type: str        # "C" or "P"
    expiry:      str        # "YYYY-MM-DD"
    action:      str        # "BTO" or "STC"
    quantity:    int = 1
    raw_text:    str = ""


@dataclass
class OpenPosition:
    signal:         TradeSignal
    entry_price:    float
    entry_time:     str = ""
    paper:          bool = True
    highest_pnl:    float = 0.0
    scaled_out:     bool = False   # True once we've sold the first 50%
    monitor_thread: Optional[threading.Thread] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.entry_time:
            self.entry_time = datetime.utcnow().isoformat()


class TradeEngine:
    def __init__(
            self,
            webull,
            market,
            paper:               bool  = True,
            profit_target_pct:   float = 15.0,
            stop_loss_pct:       float = 20.0,
            max_spread_pct:      float = 5.0,
            require_market_open: bool  = True,
            listener=None,
            # ── UPGRADE: trailing stop & scale-out config ──────────────────
            trailing_stop_buffer_pct: float = 8.0,   # trail 8% below peak
            scale_out_trigger_pct:    float = 20.0,  # sell half at +20%
    ):
        self.webull                   = webull
        self.market                   = market
        self.paper                    = paper
        self.profit_target_pct        = profit_target_pct
        self.stop_loss_pct            = stop_loss_pct
        self.max_spread_pct           = max_spread_pct
        self.require_market_open      = require_market_open
        self.listener                 = listener
        self.trailing_stop_buffer_pct = trailing_stop_buffer_pct
        self.scale_out_trigger_pct    = scale_out_trigger_pct

        self._positions: dict[str, OpenPosition] = {}
        self.max_trade_budget = getattr(self, "max_trade_budget", 300.0)
        self.max_spread_pct   = getattr(self, "max_spread_pct", 0.20)
        self._lock = threading.Lock()

    # ── FIX: get_session_pnl — reads realized P&L from trade_log.csv ─────────
    def get_session_pnl(self) -> float:
        """
        Returns the total realized P&L for today's session by reading trade_log.csv.
        Filters to today's date so it resets each morning automatically.
        Returns 0.0 if no trades have been logged yet.
        """
        try:
            if not os.path.isfile("trade_log.csv"):
                return 0.0
            df = pd.read_csv("trade_log.csv")
            if df.empty:
                return 0.0
            today = datetime.now().strftime("%Y-%m-%d")
            # date column is "YYYY-MM-DD HH:MM" — filter by day prefix
            todays = df[df['date'].str.startswith(today)]
            return float(todays['profit_amt'].sum()) if not todays.empty else 0.0
        except Exception as e:
            log.debug("get_session_pnl error: %s", e)
            return 0.0

    # ── Data Logging for Dashboard ─────────────────────────────────────────────
    def log_trade(self, pos: OpenPosition, fill_price: float, reason: str,
                  qty_override: int = None):
        """Saves trade data to CSV for the dashboard."""
        file_exists = os.path.isfile("trade_log.csv")
        qty = qty_override if qty_override is not None else pos.signal.quantity

        # Dollar profit: option price * 100 multiplier * contracts
        profit_amt = (fill_price - pos.entry_price) * 100 * qty
        outcome    = "Win" if profit_amt > 0 else "Loss"

        cumulative = 0.0
        if file_exists:
            try:
                existing_df = pd.read_csv("trade_log.csv")
                cumulative  = float(existing_df['cumulative_profit'].iloc[-1]) + profit_amt
            except Exception:
                cumulative = profit_amt
        else:
            cumulative = profit_amt

        row = {
            'date':              datetime.now().strftime("%Y-%m-%d %H:%M"),
            'ticker':            pos.signal.ticker,
            'strike':            pos.signal.strike,
            'option_type':       pos.signal.option_type,
            'expiry':            pos.signal.expiry,
            'quantity':          qty,
            'entry_price':       round(pos.entry_price, 4),
            'exit_price':        round(fill_price, 4),
            'outcome':           outcome,
            'profit_amt':        round(profit_amt, 2),
            'cumulative_profit': round(cumulative, 2),
            'reason':            reason,
            'mode':              'live' if not self.paper else 'paper',
        }

        # Write header only if new file or columns changed
        need_header = not file_exists
        if file_exists:
            try:
                with open("trade_log.csv", "r") as f:
                    existing_cols = f.readline().strip().split(",")
                if existing_cols != list(row.keys()):
                    need_header = False  # append only — don't break existing rows
            except Exception:
                pass

        with open("trade_log.csv", 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if need_header:
                writer.writeheader()
            writer.writerow(row)

        console.print(f"[bold magenta]📊 Trade Logged: {outcome} "
                      f"{pos.signal.ticker} {pos.signal.strike}{pos.signal.option_type} "
                      f"entry=${pos.entry_price:.2f} exit=${fill_price:.2f} "
                      f"P&L=${profit_amt:.2f}[/bold magenta]")

        # Callback so main.py can update streak accurately
        if hasattr(self, 'on_trade_closed') and callable(self.on_trade_closed):
            self.on_trade_closed(outcome, profit_amt)

    def sync_webull_orders_to_log(self):
        """
        On startup, pull today's filled orders from Webull and write any
        that aren't already in trade_log.csv. Prevents missing trades if
        bot crashed or was restarted mid-session.
        """
        try:
            import pandas as pd
            from datetime import date
            today = date.today().strftime("%Y-%m-%d")

            # Read existing log to avoid duplicates
            existing_keys = set()
            if os.path.isfile("trade_log.csv"):
                try:
                    df = pd.read_csv("trade_log.csv")
                    for _, r in df.iterrows():
                        existing_keys.add(
                            f"{r.get('ticker','')}_{r.get('exit_price','')}_{r.get('date','')[:10]}"
                        )
                except Exception:
                    pass

            # Fetch today's orders from Webull
            orders = []
            try:
                orders = self.webull._wb.get_history_orders(status="Filled", count=20) or []
            except Exception as e:
                log.debug("Webull order history fetch error: %s", e)
                return

            mode = 'live' if not self.paper else 'paper'
            synced = 0
            for order in orders:
                try:
                    filled_time = order.get("filledTime", order.get("createTime", ""))
                    if today not in str(filled_time):
                        continue
                    action = order.get("action", "")
                    if action != "SELL":
                        continue  # only log sells (closed trades)

                    ticker   = order.get("ticker", {}).get("symbol", "?")
                    avg_price = float(order.get("avgFilledPrice", 0) or 0)
                    qty       = int(order.get("filledQuantity", 1) or 1)
                    reason    = "Manual STC" if order.get("orderType") == "MKT" else "Auto STC"

                    key = f"{ticker}_{avg_price}_{today}"
                    if key in existing_keys:
                        continue

                    # We don't have entry price from order history alone
                    # Log with what we have — profit_amt will be 0 (unknown entry)
                    row = {
                        'date':              filled_time[:16] if filled_time else today,
                        'ticker':            ticker,
                        'strike':            order.get("optionExercisePrice", "?"),
                        'option_type':       "C" if "Call" in str(order.get("optionType","")) else "P",
                        'expiry':            order.get("optionExpireDate", "?"),
                        'quantity':          qty,
                        'entry_price':       0.0,
                        'exit_price':        round(avg_price, 4),
                        'outcome':           "Unknown",
                        'profit_amt':        0.0,
                        'cumulative_profit': 0.0,
                        'reason':            reason,
                        'mode':              mode,
                    }
                    file_exists = os.path.isfile("trade_log.csv")
                    with open("trade_log.csv", 'a', newline='') as f:
                        writer = csv.DictWriter(f, fieldnames=row.keys())
                        if not file_exists:
                            writer.writeheader()
                        writer.writerow(row)
                    existing_keys.add(key)
                    synced += 1
                except Exception as e:
                    log.debug("Order sync row error: %s", e)

            if synced:
                log.info("Synced %d Webull orders to trade_log.csv", synced)
        except Exception as e:
            log.debug("sync_webull_orders_to_log error: %s", e)

    def on_raw_signal(self, raw_text: str):
        console.print(f"\n[bold cyan]📡 Signal received:[/bold cyan]  {raw_text[:120]}")
        signal = self.parse_signal(raw_text)
        if signal is None:
            console.print("  [yellow]⚠ Could not parse trade signal — skipped[/yellow]")
            return
        self._log_signal(signal)
        if self.require_market_open and not self.market.is_market_open():
            mins = self.market.minutes_to_open()
            console.print(f"  [yellow]⏰  Market closed — opens in ~{mins} min[/yellow]")
            return
        if not self._check_market_conditions(signal):
            return
        self._execute(signal)

    def parse_signal(self, raw_text: str) -> Optional[TradeSignal]:
        text = raw_text.upper().strip()
        text = re.sub(r"\s+", " ", text)
        m = _SIGNAL_RE.search(text)
        if not m:
            log.debug("No regex match in: %s", raw_text[:80])
            return None
        try:
            raw_action = re.sub(r"\s+", " ", m.group("action")).strip()
            action     = ACTION_MAP.get(raw_action, "BTO")
            ticker     = m.group("ticker").upper()
            strike     = float(m.group("strike"))
            opt_type   = m.group("option_type").upper()
            month      = int(m.group("expiry_month"))
            day        = int(m.group("expiry_day"))
            year_raw   = m.group("expiry_year")
            year       = int(year_raw) if year_raw else datetime.now().year
            if year < 100:
                year += 2000
            expiry = f"{year}-{month:02d}-{day:02d}"
            qty    = int(m.group("qty") or 1)
            return TradeSignal(
                ticker=ticker, strike=strike, option_type=opt_type,
                expiry=expiry, action=action, quantity=qty, raw_text=raw_text,
            )
        except Exception as exc:
            log.debug("Parse error: %s — %s", exc, raw_text[:60])
            return None

    def _check_market_conditions(self, signal: TradeSignal) -> bool:
        quote = self.market.get_quote(signal.ticker)
        if quote is None:
            console.print(f"  [dim]No market data for {signal.ticker} — proceeding anyway[/dim]")
            return True
        last = quote.get("last")
        bid  = quote.get("bid")
        ask  = quote.get("ask")
        if bid and ask and last and last > 0:
            spread_pct = ((ask - bid) / last) * 100
            if spread_pct > self.max_spread_pct:
                console.print(f"  [red]✗ Spread too wide: {spread_pct:.1f}% — skipping[/red]")
                return False
        greeks = self.market.get_option_greeks(
            signal.ticker, signal.expiry, signal.strike, signal.option_type
        )
        if greeks:
            iv = greeks.get("iv", 0) or 0
            if iv > 2.0:
                console.print(f"  [red]✗ IV extremely high ({iv:.0%}) — skipping[/red]")
                return False
            console.print(
                f"  [dim]IV={iv:.0%}  delta={greeks.get('delta', '?')}  "
                f"theta={greeks.get('theta', '?')}[/dim]"
            )
        last_str = f"${last:.2f}" if last else "unknown"
        console.print(f"  [dim]{signal.ticker} last={last_str}  bid={bid}  ask={ask}[/dim]")
        return True

    def _execute(self, signal: TradeSignal):
        position_key = f"{signal.ticker}_{signal.strike}{signal.option_type}_{signal.expiry}"
        if signal.action == "STC":
            self._close_position(signal, position_key)
            return
        if position_key in self._positions:
            console.print(f"  [yellow]Position {position_key} already open — skipping BTO[/yellow]")
            return
        console.print(
            f"  [bold green]→ Executing {signal.action} {signal.quantity}x "
            f"{signal.ticker} {signal.strike}{signal.option_type} exp {signal.expiry} "
            f"({'PAPER' if self.paper else 'REAL'})[/bold green]"
        )
        ok = self.webull.navigate_to_option(
            signal.ticker, signal.expiry, signal.strike, signal.option_type
        )
        if not ok:
            console.print("  [red]✗ Navigation to option failed[/red]")
            return
        quote       = self.webull.get_option_quote(signal.ticker)
        limit_price = None
        if quote:
            bid = float(quote.get("bid") or 0)
            ask = float(quote.get("ask") or 0)
            if bid > 0 and ask > 0:
                mid          = (bid + ask) / 2
                spread_pct   = (ask - bid) / mid if mid > 0 else 1.0
                max_spread   = getattr(self, "max_spread_pct", 0.20)

                if spread_pct > max_spread:
                    console.print(
                        f"  [red]✗ Spread too wide: {spread_pct:.1%} > {max_spread:.0%} — skipping[/red]"
                    )
                    return

                limit_price  = round(mid, 2)
                est_cost     = limit_price * 100 * signal.quantity
                max_trade    = getattr(self, "max_trade_budget", 300)

                if est_cost > max_trade:
                    console.print(
                        f"  [red]✗ Est. cost ${est_cost:.0f} > budget ${max_trade:.0f} — skipping[/red]"
                    )
                    return

                console.print(
                    f"  [dim]Quote: bid=${bid:.2f} ask=${ask:.2f} "
                    f"mid=${limit_price:.2f} spread={spread_pct:.1%} "
                    f"est.cost=${est_cost:.0f}[/dim]"
                )
            else:
                console.print("  [red]✗ No valid bid/ask — refusing order[/red]")
                return
        # FIX: track whether order was actually accepted by the API
        api_fill = self.webull.place_order(
            action=signal.action, quantity=signal.quantity,
            limit_price=limit_price, paper=self.paper,
        )

        # api_fill is None when:
        #   - paper endpoint returned 417 (market closed / untradable)
        #   - live endpoint returned 500
        #   - any other API failure
        # In ALL these cases we must NOT start monitoring.
        # We only use limit_price as the estimated entry if order was confirmed.

        order_confirmed = self.webull.last_order_confirmed  # set by place_order
        if not order_confirmed:
            console.print(
                f"  [red]✗ Order NOT placed — API rejected or market closed. "
                f"No position created.[/red]"
            )
            if self.listener:
                self.listener.send_status_update(
                    f"❌ Order failed: {signal.ticker} {signal.strike}"
                    f"{signal.option_type} — not tradable or market closed"
                )
            return

        # Order confirmed — use returned fill or limit as estimated entry
        fill_price = api_fill if api_fill else limit_price
        if fill_price is None:
            console.print("  [yellow]Order confirmed but no fill price — using $0 entry[/yellow]")
            fill_price = 0.0

        pos = OpenPosition(signal=signal, entry_price=fill_price, paper=self.paper)
        with self._lock:
            self._positions[position_key] = pos
        console.print(f"  [green]✓ Order confirmed. Entry est. ${fill_price:.2f}[/green]")

        # ── OTOCO Bracket: TP + SL placed on Webull hardware instantly ────────
        bracket   = {}
        option_id = getattr(self.webull, "_pending_option_id", None)
        if fill_price and fill_price > 0 and option_id:
            bracket = self.webull.place_otoco_bracket(
                option_id   = option_id,
                quantity    = signal.quantity,
                entry_price = fill_price,
                tp_pct      = self.profit_target_pct,
                sl_pct      = self.stop_loss_pct,
            )

        tp_str = (f"TP=${bracket['tp_price']:.2f}" if bracket.get("tp_price")
                  else f"TP=+{self.profit_target_pct:.0f}%")
        sl_str = (f"SL=${bracket['sl_price']:.2f}" if bracket.get("sl_price")
                  else f"SL=-{self.stop_loss_pct:.0f}%")
        bracket_note = (" | Bracket orders on Webull ✓" if bracket.get("tp_order_id")
                        else " | Python monitor active")

        if self.listener:
            self.listener.send_status_update(
                f"✅ **BTO confirmed**: {signal.ticker} {signal.strike}"
                f"{signal.option_type} {signal.expiry} "
                f"@ ${fill_price:.2f} x{signal.quantity}\n"
                f"**{tp_str}** | **{sl_str}**{bracket_note}"
            )
        self._start_monitor(position_key, pos)

    def _close_position(self, signal: TradeSignal, position_key: str):
        console.print(f"  [bold yellow]→ Closing {position_key}[/bold yellow]")
        ok = self.webull.navigate_to_option(
            signal.ticker, signal.expiry, signal.strike, signal.option_type
        )
        if ok:
            fill = self.webull.place_order(
                action="STC", quantity=signal.quantity, paper=self.paper
            )
            with self._lock:
                pos = self._positions.get(position_key)
                if pos and fill:
                    self.log_trade(pos, fill, reason="Manual STC")
                self._positions.pop(position_key, None)

    _POSITIONS_FILE = "open_positions.json"

    def _save_positions(self):
        """Persist open positions to disk so they survive restarts."""
        import json as _json
        try:
            data = {}
            with self._lock:
                for key, pos in self._positions.items():
                    data[key] = {
                        "ticker":      pos.signal.ticker,
                        "strike":      pos.signal.strike,
                        "option_type": pos.signal.option_type,
                        "expiry":      pos.signal.expiry,
                        "quantity":    pos.signal.quantity,
                        "entry_price": pos.entry_price,
                        "paper":       pos.paper,
                        "scaled_out":  pos.scaled_out,
                        "highest_pnl": pos.highest_pnl,
                    }
            with open(self._POSITIONS_FILE, "w") as f:
                _json.dump(data, f)
        except Exception as e:
            log.debug("_save_positions error: %s", e)

    def _load_positions(self):
        """
        On startup, reload any positions saved to disk.
        Resumes P&L monitoring with the original entry price.
        """
        import json as _json
        if not os.path.exists(self._POSITIONS_FILE):
            return
        try:
            with open(self._POSITIONS_FILE) as f:
                data = _json.load(f)
            if not data:
                return
            log.info("Resuming %d position(s) from open_positions.json", len(data))
            for key, d in data.items():
                try:
                    sig = TradeSignal(
                        ticker=d["ticker"], strike=d["strike"],
                        option_type=d["option_type"], expiry=d["expiry"],
                        quantity=d["quantity"], action="BTO", raw_text=key,
                    )
                    pos = OpenPosition(
                        signal=sig, entry_price=d["entry_price"], paper=d["paper"]
                    )
                    pos.scaled_out  = d.get("scaled_out", False)
                    pos.highest_pnl = d.get("highest_pnl", 0.0)
                    with self._lock:
                        self._positions[key] = pos
                    console.print(
                        f"  [cyan]↩ Resumed monitoring: {key} "
                        f"entry=${d['entry_price']:.2f}[/cyan]"
                    )
                    self._start_monitor(key, pos)
                except Exception as e:
                    log.warning("Could not resume position %s: %s", key, e)
        except Exception as e:
            log.warning("_load_positions error: %s", e)
        finally:
            # Clear the file after loading so stale positions don't persist forever
            try:
                os.remove(self._POSITIONS_FILE)
            except Exception:
                pass

    def _start_monitor(self, position_key: str, pos: OpenPosition):
        self._save_positions()   # persist to disk immediately
        t = threading.Thread(
            target=self._monitor_loop, args=(position_key, pos),
            daemon=True, name=f"PLMonitor-{position_key}",
        )
        pos.monitor_thread = t
        t.start()

    def _monitor_loop(self, position_key: str, pos: OpenPosition):
        log.info("P&L monitor started: %s @ entry $%.2f", position_key, pos.entry_price)
        start_ts         = time.time()
        breakeven_active = False
        current_sl       = self.stop_loss_pct

        while True:
            with self._lock:
                if position_key not in self._positions:
                    return

            time.sleep(15)
            if not self.market.is_market_open():
                continue

            data = self.webull.get_position_pnl(
                pos.signal.ticker, pos.signal.strike, pos.signal.option_type
            )
            if data and data.get("pnl_pct") is not None:
                pnl_pct = data["pnl_pct"]
            else:
                # Get option price directly, not stock price
                option_quote = self.webull.get_option_quote(pos.signal.ticker)
                if option_quote:
                    bid = option_quote.get("bid", 0)
                    ask = option_quote.get("ask", 0)
                    current_option = (bid + ask) / 2 if bid and ask else option_quote.get("last", 0)
                else:
                    current_option = None
                if current_option and pos.entry_price:
                    pnl_pct = ((current_option - pos.entry_price) / pos.entry_price) * 100
                else:
                    continue

            # Track all-time high P&L for trailing stop
            if pnl_pct > pos.highest_pnl:
                pos.highest_pnl = pnl_pct

            # ── UPGRADE: Trailing stop — trails below peak ────────────────────
            # Once we've hit the scale-out trigger, the stop trails the peak
            # rather than sitting at a fixed level. This locks in gains as the
            # position keeps running.
            if pos.scaled_out and pos.highest_pnl > self.scale_out_trigger_pct:
                trailing_stop = pos.highest_pnl - self.trailing_stop_buffer_pct
                # Never let the trailing stop go below breakeven once we've
                # activated the breakeven guard
                if breakeven_active:
                    trailing_stop = max(trailing_stop, 0.0)
                current_sl = trailing_stop
                log.debug(
                    "%s trailing stop: peak=%.1f%% → stop=%.1f%%",
                    position_key, pos.highest_pnl, current_sl,
                )

            # BREAKEVEN guard (existing logic, unchanged)
            if pnl_pct >= 10.0 and not breakeven_active:
                if not pos.scaled_out:          # only snap to 0 if no trailing yet
                    current_sl = 0.0
                breakeven_active = True
                if self.listener:
                    self.listener.send_status_update(
                        f"🛡️ **BREAKEVEN**: {position_key} hit 10%. SL moved to 0.0%."
                    )

            # ── TIME DECAY EXIT — momentum-aware with extension trailing stop ─
            # After 30 min with P&L < +2%, check momentum before closing.
            # If still moving up, extend 5 min — but add a hard trailing floor
            # so a sudden crash during the extension triggers an immediate close.
            elapsed_mins = (time.time() - start_ts) / 60
            if elapsed_mins > 30 and pnl_pct < 2.0:
                last_pnl     = getattr(pos, "_last_decay_check_pnl", None)
                last_check_t = getattr(pos, "_last_decay_check_t", 0)
                ext_peak     = getattr(pos, "_extension_peak_pnl", None)
                now_t        = time.time()

                # If in extension window, enforce hard trailing floor continuously
                if ext_peak is not None:
                    # Update peak if we've gone higher during extension
                    if pnl_pct > ext_peak:
                        pos._extension_peak_pnl = pnl_pct
                        ext_peak = pnl_pct
                    # Hard trailing floor: 5% pullback from extension peak = close now
                    trailing_floor = ext_peak - 5.0
                    if pnl_pct < trailing_floor:
                        if self.listener:
                            self.listener.send_status_update(
                                f"🚨 **EXTENSION STOP**: {position_key} dropped "
                                f"{ext_peak - pnl_pct:.1f}% from extension peak "
                                f"({ext_peak:+.1f}% → {pnl_pct:+.1f}%). Closing now."
                            )
                        self._auto_close(position_key, pos, reason="Extension Stop")
                        return

                if last_pnl is None:
                    # First time hitting 30min — record P&L, set extension peak, wait 5 min
                    pos._last_decay_check_pnl = pnl_pct
                    pos._last_decay_check_t   = now_t
                    pos._extension_peak_pnl   = pnl_pct
                    if self.listener:
                        self.listener.send_status_update(
                            f"⏱️ {position_key} at 30min P&L={pnl_pct:+.1f}% — "
                            f"watching 5 more minutes for momentum."
                        )
                elif now_t - last_check_t >= 300:  # 5 more minutes passed
                    momentum = pnl_pct - last_pnl
                    if momentum > 1.0:
                        # Still moving up — extend another 5 min, update baseline
                        pos._last_decay_check_pnl = pnl_pct
                        pos._last_decay_check_t   = now_t
                        if self.listener:
                            self.listener.send_status_update(
                                f"⏱️ {position_key} momentum +{momentum:.1f}% — "
                                f"extending 5 more minutes."
                            )
                    else:
                        # Flat or falling — close now
                        if self.listener:
                            self.listener.send_status_update(
                                f"⏱️ **TIME EXIT**: {position_key} stagnant "
                                f"(momentum {momentum:+.1f}%). Closing."
                            )
                        self._auto_close(position_key, pos, reason="Time Decay")
                        return

            color = "green" if pnl_pct >= 0 else "red"
            console.print(
                f"  [{color}]📊 {position_key}  P&L: {pnl_pct:+.1f}%  "
                f"peak: {pos.highest_pnl:+.1f}%  stop: {current_sl:+.1f}%[/{color}]"
            )

            # ── UPGRADE: Scale-out — sell 50% at scale_out_trigger_pct ──────
            # Fires once per position. Reduces qty by half in Webull, logs the
            # partial fill, then lets the remainder run with the trailing stop.
            if not pos.scaled_out and pnl_pct >= self.scale_out_trigger_pct:
                half_qty = max(1, pos.signal.quantity // 2)
                console.print(
                    f"  [bold cyan]💰 SCALE-OUT: Selling {half_qty}x at "
                    f"{pnl_pct:+.1f}% — locking in gains[/bold cyan]"
                )
                ok = self.webull.navigate_to_option(
                    pos.signal.ticker, pos.signal.expiry,
                    pos.signal.strike, pos.signal.option_type,
                )
                if ok:
                    quote = self.webull.get_option_quote(pos.signal.ticker)
                    lp    = None
                    if quote and quote.get("bid") and quote.get("ask"):
                        lp = round((quote["bid"] + quote["ask"]) / 2, 2)
                    fill = self.webull.place_order(
                        action="STC", quantity=half_qty,
                        limit_price=lp, paper=pos.paper,
                    )
                    if fill:
                        self.log_trade(pos, fill, reason="Scale-Out 50%", qty_override=half_qty)
                    pos.scaled_out      = True
                    remaining_qty       = pos.signal.quantity - half_qty
                    pos.signal.quantity = remaining_qty

                    # ── GHOST CONTRACT FIX ────────────────────────────────────
                    # If remaining qty is 0 or less after scale-out (e.g. only
                    # had 1 contract), purge the position entirely right now.
                    # Don't leave a ghost tracking a flat position in Webull.
                    if remaining_qty <= 0:
                        log.info("Scale-out reduced qty to 0 — purging %s", position_key)
                        if self.listener:
                            self.listener.send_status_update(
                                f"💰 **SCALE-OUT COMPLETE**: {position_key} fully closed "
                                f"@ {pnl_pct:+.1f}%. No contracts remaining."
                            )
                        with self._lock:
                            self._positions.pop(position_key, None)
                        # Sync with Webull to confirm flat
                        try:
                            self.webull._write_account_cache()
                        except Exception:
                            pass
                        return
                    # Sync remaining qty against Webull before continuing
                    try:
                        wb_positions = self.webull.get_open_positions_from_webull() or []
                        wb_qty = next(
                            (int(p.get("quantity", 0)) for p in wb_positions
                             if pos.signal.ticker in str(p.get("ticker", ""))),
                            None
                        )
                        if wb_qty is not None and wb_qty != remaining_qty:
                            log.warning("Qty mismatch after scale-out: local=%d webull=%d — syncing",
                                        remaining_qty, wb_qty)
                            pos.signal.quantity = wb_qty
                            if wb_qty <= 0:
                                with self._lock:
                                    self._positions.pop(position_key, None)
                                return
                    except Exception as e:
                        log.debug("Webull qty sync error: %s", e)

                    if self.listener:
                        self.listener.send_status_update(
                            f"💰 **SCALE-OUT**: Sold {half_qty}x {position_key} "
                            f"@ {pnl_pct:+.1f}%. {pos.signal.quantity}x remaining."
                        )

            # Full profit target hit
            if pnl_pct >= self.profit_target_pct:
                console.print(
                    f"  [bold green]🎯 Profit target hit ({pnl_pct:+.1f}%) — auto-closing[/bold green]"
                )
                self._auto_close(position_key, pos, reason="Profit Target")
                return

            # ── Adaptive stop loss for cheap options ─────────────────────────
            # Cheap options ($0.50-$1.50) have wide bid/ask spreads that can
            # trigger a -20% stop on normal noise. Use a wider SL for cheap
            # options but also check if the trend is still going down before closing.
            if pos.entry_price <= 1.00:
                # Very cheap option — use -35% SL (spread noise is $0.20+)
                effective_sl = max(current_sl, 35.0)
            elif pos.entry_price <= 2.00:
                # Cheap option — use -25% SL
                effective_sl = max(current_sl, 25.0)
            else:
                effective_sl = current_sl

            # Stop-loss hit (uses effective_sl)
            if pnl_pct <= -effective_sl:
                console.print(
                    f"  [bold red]🛑 Stop-loss hit ({pnl_pct:+.1f}% vs -{effective_sl:.0f}%) "
                    f"— auto-closing[/bold red]"
                )
                self._auto_close(position_key, pos, reason="Stop Loss")
                return

    def _auto_close(self, position_key: str, pos: OpenPosition, reason: str):
        close_signal = TradeSignal(
            ticker=pos.signal.ticker, strike=pos.signal.strike,
            option_type=pos.signal.option_type, expiry=pos.signal.expiry,
            action="STC", quantity=pos.signal.quantity,
        )
        ok = self.webull.navigate_to_option(
            close_signal.ticker, close_signal.expiry,
            close_signal.strike, close_signal.option_type,
        )
        if ok:
            fill = self.webull.place_order(
                action="STC", quantity=close_signal.quantity, paper=pos.paper
            )
            if fill:
                self.log_trade(pos, fill, reason=reason)
            console.print(f"  [cyan]Auto-close [{reason}] fill: ${fill or '?'}[/cyan]")
        with self._lock:
            self._positions.pop(position_key, None)
        self._save_positions()   # update file after close

    def print_status(self):
        with self._lock:
            positions = dict(self._positions)
        if not positions:
            return
        table = Table(title="Open Positions", border_style="bright_blue", show_header=True)
        table.add_column("Contract",  style="cyan",   no_wrap=True)
        table.add_column("Action",    style="yellow")
        table.add_column("Entry $",   justify="right")
        table.add_column("Peak P&L",  justify="right")
        table.add_column("Scaled",    style="dim")
        table.add_column("Expiry",    style="dim")
        table.add_column("Mode",      style="dim")
        for key, pos in positions.items():
            table.add_row(
                f"{pos.signal.ticker} {pos.signal.strike}{pos.signal.option_type}",
                pos.signal.action,
                f"${pos.entry_price:.2f}",
                f"{pos.highest_pnl:+.1f}%",
                "Yes" if pos.scaled_out else "No",
                pos.signal.expiry,
                "PAPER" if pos.paper else "REAL",
            )
        console.print(table)

    def _log_signal(self, signal: TradeSignal):
        console.print(
            f"  [dim]Parsed:[/dim]  [bold]{signal.action}[/bold] "
            f"[cyan]{signal.ticker}[/cyan] {signal.strike}"
            f"[yellow]{signal.option_type}[/yellow]  "
            f"exp [dim]{signal.expiry}[/dim]  qty [bold]{signal.quantity}[/bold]"
        )