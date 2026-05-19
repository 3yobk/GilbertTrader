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
        outcome = "Win" if profit_amt > 0 else "Loss"

        cumulative = 0.0
        if file_exists:
            try:
                existing_df = pd.read_csv("trade_log.csv")
                cumulative = float(existing_df['cumulative_profit'].iloc[-1]) + profit_amt
            except Exception:
                cumulative = profit_amt
        else:
            cumulative = profit_amt

        row = {
            'date':               datetime.now().strftime("%Y-%m-%d %H:%M"),
            'ticker':             pos.signal.ticker,
            'outcome':            outcome,
            'profit_amt':         round(profit_amt, 2),
            'cumulative_profit':  round(cumulative, 2),
            'reason':             reason,
            'mode':               'live' if not self.paper else 'paper',
        }

        with open("trade_log.csv", 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

        console.print(f"[bold magenta]📊 Trade Logged: {outcome} (${profit_amt:.2f})[/bold magenta]")

        # Callback so main.py can update streak accurately
        if hasattr(self, 'on_trade_closed') and callable(self.on_trade_closed):
            self.on_trade_closed(outcome, profit_amt)

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

        # ── Bracket orders: TP + SL placed on Webull (survive bot restarts) ──
        bracket = {}
        option_id = getattr(self.webull, "_pending_option_id", None)
        if fill_price and fill_price > 0 and option_id:
            bracket = self.webull.place_bracket_orders(
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

    def _start_monitor(self, position_key: str, pos: OpenPosition):
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
                quote   = self.market.get_quote(pos.signal.ticker)
                current = quote.get("last") if quote else None
                if current and pos.entry_price:
                    pnl_pct = ((current - pos.entry_price) / pos.entry_price) * 100
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

            # TIME DECAY exit (existing logic, unchanged)
            elapsed_mins = (time.time() - start_ts) / 60
            if elapsed_mins > 30 and pnl_pct < 2.0:
                if self.listener:
                    self.listener.send_status_update(
                        f"⏱️ **TIME EXIT**: {position_key} stagnant for 30m. Closing."
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
                    pos.scaled_out = True
                    pos.signal.quantity -= half_qty   # remaining contracts
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

            # Stop-loss hit (uses current_sl which may be trailing or breakeven)
            if pnl_pct <= -current_sl:
                console.print(
                    f"  [bold red]🛑 Stop-loss hit ({pnl_pct:+.1f}%) — auto-closing[/bold red]"
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