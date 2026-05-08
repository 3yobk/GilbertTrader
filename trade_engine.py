"""
trade_engine.py  —  Signal Parser + Trade Executor + P&L Monitor

Flow:
  raw Discord text
    → parse_signal()       regex + structure extraction (no AI cost for simple signals)
    → confirm_with_market() check IV, bid/ask spread, market hours
    → execute_trade()       drives WebullEngine
    → PLMonitor thread      watches P&L every 15s, auto-closes on target/stop
"""

import re
import time
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

log     = logging.getLogger("trade_engine")
console = Console()

# ── Signal pattern — covers most common paid channel formats ──────────────────
# Examples:  "BTO NVDA 110C 5/15 x3"
#            "BUY TO OPEN TSLA 200 CALL 05/15 2 contracts"
#            "STC SPY 450P 5/17 @2.50"
#            "NVDA 110C 5/15 BTO"

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
    signal:       TradeSignal
    entry_price:  float
    entry_time:   str = ""
    paper:        bool = True
    monitor_thread: Optional[threading.Thread] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.entry_time:
            self.entry_time = datetime.utcnow().isoformat()


class TradeEngine:
    def __init__(
        self,
        webull,
        market,
        paper:             bool  = True,
        profit_target_pct: float = 15.0,
        stop_loss_pct:     float = 20.0,
        max_spread_pct:    float = 5.0,    # skip if bid/ask spread > 5% of price
        require_market_open: bool = True,
    ):
        self.webull              = webull
        self.market              = market
        self.paper               = paper
        self.profit_target_pct   = profit_target_pct
        self.stop_loss_pct       = stop_loss_pct
        self.max_spread_pct      = max_spread_pct
        self.require_market_open = require_market_open

        self._positions: dict[str, OpenPosition] = {}  # key: "NVDA_110C_2025-05-15"
        self._lock = threading.Lock()

    # ── Entry point ───────────────────────────────────────────────────────────

    def on_raw_signal(self, raw_text: str):
        """Called by DiscordListener for every incoming message."""
        console.print(f"\n[bold cyan]📡 Signal received:[/bold cyan]  {raw_text[:120]}")

        signal = self.parse_signal(raw_text)
        if signal is None:
            console.print("  [yellow]⚠ Could not parse trade signal — skipped[/yellow]")
            return

        self._log_signal(signal)

        # Market checks
        if self.require_market_open and not self.market.is_market_open():
            mins = self.market.minutes_to_open()
            console.print(f"  [yellow]⏰  Market closed — opens in ~{mins} min[/yellow]")
            return

        # Market data sanity check
        if not self._check_market_conditions(signal):
            return

        # Execute
        self._execute(signal)

    # ── Signal parsing ────────────────────────────────────────────────────────

    def parse_signal(self, raw_text: str) -> Optional[TradeSignal]:
        """
        Pure-regex parser — handles 90% of paid channel formats.
        No API call needed, works offline, zero latency.
        """
        # Normalize
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

    # ── Market condition checks ───────────────────────────────────────────────

    def _check_market_conditions(self, signal: TradeSignal) -> bool:
        """Check bid/ask spread and basic sanity. Warn but don't block on failure."""
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

        # Check IV if available
        greeks = self.market.get_option_greeks(
            signal.ticker, signal.expiry, signal.strike, signal.option_type)
        if greeks:
            iv = greeks.get("iv", 0) or 0
            if iv > 2.0:   # IV > 200% — extremely dangerous
                console.print(f"  [red]✗ IV extremely high ({iv:.0%}) — skipping[/red]")
                return False
            console.print(f"  [dim]IV={iv:.0%}  delta={greeks.get('delta', '?')}  "
                          f"theta={greeks.get('theta', '?')}[/dim]")

        last_str = f"${last:.2f}" if last else "unknown"
        console.print(f"  [dim]{signal.ticker} last={last_str}  "
                      f"bid={bid}  ask={ask}[/dim]")
        return True

    # ── Trade execution ───────────────────────────────────────────────────────

    def _execute(self, signal: TradeSignal):
        position_key = f"{signal.ticker}_{signal.strike}{signal.option_type}_{signal.expiry}"

        # STC — close an open position
        if signal.action == "STC":
            self._close_position(signal, position_key)
            return

        # BTO — open a new position
        if position_key in self._positions:
            console.print(f"  [yellow]Position {position_key} already open — skipping BTO[/yellow]")
            return

        console.print(f"  [bold green]→ Executing {signal.action} "
                      f"{signal.quantity}x {signal.ticker} "
                      f"{signal.strike}{signal.option_type} exp {signal.expiry} "
                      f"({'PAPER' if self.paper else 'REAL'})[/bold green]")

        ok = self.webull.navigate_to_option(
            signal.ticker, signal.expiry, signal.strike, signal.option_type)
        if not ok:
            console.print("  [red]✗ Navigation to option failed[/red]")
            return

        # Use mid-price as limit
        quote       = self.webull.get_option_quote(signal.ticker)
        limit_price = None
        if quote and quote.get("bid") and quote.get("ask"):
            limit_price = round((quote["bid"] + quote["ask"]) / 2, 2)

        fill_price = self.webull.place_order(
            action=signal.action, quantity=signal.quantity,
            limit_price=limit_price, paper=self.paper,
        )

        if fill_price is None:
            fill_price = limit_price  # best estimate if scrape failed

        if fill_price is not None:
            pos = OpenPosition(signal=signal, entry_price=fill_price, paper=self.paper)
            with self._lock:
                self._positions[position_key] = pos

            console.print(f"  [green]✓ Filled @ ${fill_price:.2f}[/green]")
            self._start_monitor(position_key, pos)
        else:
            console.print("  [yellow]Order submitted but fill price unknown — monitor manually[/yellow]")

    def _close_position(self, signal: TradeSignal, position_key: str):
        console.print(f"  [bold yellow]→ Closing {position_key}[/bold yellow]")
        ok = self.webull.navigate_to_option(
            signal.ticker, signal.expiry, signal.strike, signal.option_type)
        if ok:
            self.webull.place_order(action="STC", quantity=signal.quantity, paper=self.paper)
        with self._lock:
            pos = self._positions.pop(position_key, None)
            if pos and pos.monitor_thread:
                # Signal monitor to stop (it checks _positions)
                pass

    # ── P&L Monitor ───────────────────────────────────────────────────────────

    def _start_monitor(self, position_key: str, pos: OpenPosition):
        t = threading.Thread(
            target=self._monitor_loop,
            args=(position_key, pos),
            daemon=True,
            name=f"PLMonitor-{position_key}",
        )
        pos.monitor_thread = t
        t.start()

    def _monitor_loop(self, position_key: str, pos: OpenPosition):
        log.info("P&L monitor started: %s @ entry $%.2f", position_key, pos.entry_price)
        while True:
            # Check if position was manually closed
            with self._lock:
                if position_key not in self._positions:
                    log.info("Monitor %s: position removed — stopping", position_key)
                    return

            time.sleep(15)

            if not self.market.is_market_open():
                continue

            # Try Webull UI first (most accurate for options)
            data = self.webull.get_position_pnl(
                pos.signal.ticker, pos.signal.strike, pos.signal.option_type)

            if data and data.get("pnl_pct") is not None:
                pnl_pct = data["pnl_pct"]
            else:
                # Fallback: compute from live market quote
                quote   = self.market.get_quote(pos.signal.ticker)
                current = quote.get("last") if quote else None
                if current and pos.entry_price:
                    pnl_pct = ((current - pos.entry_price) / pos.entry_price) * 100
                else:
                    continue

            color = "green" if pnl_pct >= 0 else "red"
            console.print(f"  [{color}]📊 {position_key}  P&L: {pnl_pct:+.1f}%[/{color}]")

            if pnl_pct >= self.profit_target_pct:
                console.print(f"  [bold green]🎯 Profit target hit ({pnl_pct:+.1f}%) — auto-closing[/bold green]")
                self._auto_close(position_key, pos, reason="profit_target")
                return

            if pnl_pct <= -self.stop_loss_pct:
                console.print(f"  [bold red]🛑 Stop-loss hit ({pnl_pct:+.1f}%) — auto-closing[/bold red]")
                self._auto_close(position_key, pos, reason="stop_loss")
                return

    def _auto_close(self, position_key: str, pos: OpenPosition, reason: str):
        close_signal = TradeSignal(
            ticker      = pos.signal.ticker,
            strike      = pos.signal.strike,
            option_type = pos.signal.option_type,
            expiry      = pos.signal.expiry,
            action      = "STC",
            quantity    = pos.signal.quantity,
        )
        ok = self.webull.navigate_to_option(
            close_signal.ticker, close_signal.expiry,
            close_signal.strike, close_signal.option_type,
        )
        if ok:
            fill = self.webull.place_order(action="STC",
                                            quantity=close_signal.quantity,
                                            paper=pos.paper)
            console.print(f"  [cyan]Auto-close [{reason}] fill: ${fill or '?'}[/cyan]")
        with self._lock:
            self._positions.pop(position_key, None)

    # ── Status display ────────────────────────────────────────────────────────

    def print_status(self):
        with self._lock:
            positions = dict(self._positions)

        if not positions:
            return

        table = Table(title="Open Positions", border_style="bright_blue", show_header=True)
        table.add_column("Contract",   style="cyan",   no_wrap=True)
        table.add_column("Action",     style="yellow")
        table.add_column("Entry $",    justify="right")
        table.add_column("Expiry",     style="dim")
        table.add_column("Mode",       style="dim")

        for key, pos in positions.items():
            table.add_row(
                f"{pos.signal.ticker} {pos.signal.strike}{pos.signal.option_type}",
                pos.signal.action,
                f"${pos.entry_price:.2f}",
                pos.signal.expiry,
                "PAPER" if pos.paper else "REAL",
            )
        console.print(table)

    def _log_signal(self, signal: TradeSignal):
        console.print(
            f"  [dim]Parsed:[/dim]  "
            f"[bold]{signal.action}[/bold] "
            f"[cyan]{signal.ticker}[/cyan] "
            f"{signal.strike}[yellow]{signal.option_type}[/yellow]  "
            f"exp [dim]{signal.expiry}[/dim]  "
            f"qty [bold]{signal.quantity}[/bold]"
        )
