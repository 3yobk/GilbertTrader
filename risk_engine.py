"""
risk_engine.py — GilbertTrader Advanced Risk Engine (Phase 4)
=============================================================
Provides:
  - Three operating modes: safe / normal / aggressive
  - Automatic mode switching based on VIX, streak, realized vol
  - Sector exposure limits
  - Directional (calls/puts) balance tracking
  - Portfolio-level stop (not just session P&L)
  - Dynamic position sizing based on volatility
  - Daily VAR estimation
  - Correlation-aware sizing

Usage:
    from risk_engine import RiskEngine
    risk = RiskEngine(analytics_engine)
    risk.update(vix=18.5, streak_losses=1, session_pnl=-50,
                account_value=1152.42)

    mode = risk.current_mode        # "safe" | "normal" | "aggressive"
    ok, reason = risk.can_enter_trade(ticker, direction, cost, sector)
    adj_qty = risk.adjust_quantity(base_qty, ticker, iv_rank)
"""

import logging
import threading
from datetime import datetime
from typing import Optional, Tuple

log = logging.getLogger("risk_engine")

# ── Sector map ────────────────────────────────────────────────────────────────
SECTOR_MAP = {
    # Tech
    "AAPL": "tech", "NVDA": "tech", "AMD": "tech", "MSFT": "tech",
    "GOOGL": "tech", "META": "tech", "AMZN": "tech", "TSLA": "tech",
    "CRM": "tech", "ORCL": "tech", "INTC": "tech", "QCOM": "tech",
    "PLTR": "tech", "SNOW": "tech", "SHOP": "tech", "COIN": "tech",
    # Finance
    "JPM": "finance", "GS": "finance", "BAC": "finance", "WFC": "finance",
    "C": "finance", "MS": "finance", "HOOD": "finance", "SOFI": "finance",
    # ETF (broad)
    "SPY": "etf", "QQQ": "etf", "IWM": "etf", "DIA": "etf",
    "TQQQ": "etf", "SQQQ": "etf", "SPXL": "etf", "TLT": "etf",
    # Consumer/Meme
    "GME": "meme", "AMC": "meme", "BBBY": "meme",
    "TSLA": "consumer", "F": "consumer", "GM": "consumer",
    # Energy
    "XOM": "energy", "CVX": "energy", "OXY": "energy",
    # Healthcare
    "MRNA": "healthcare", "PFE": "healthcare", "JNJ": "healthcare",
    # Crypto-proxy
    "MARA": "crypto", "RIOT": "crypto", "CLSK": "crypto",
}


class RiskMode:
    SAFE       = "safe"
    NORMAL     = "normal"
    AGGRESSIVE = "aggressive"


class RiskEngine:
    """
    Portfolio-level risk manager.
    Wraps all position sizing and entry decisions.
    """

    def __init__(self, analytics=None):
        self.analytics      = analytics
        self._lock          = threading.Lock()
        self.current_mode   = RiskMode.NORMAL

        # Live tracking
        self._deployed: dict = {}   # position_key -> {"ticker","direction","cost","sector"}
        self._session_pnl   = 0.0
        self._account_value = 1152.42
        self._vix           = 0.0
        self._streak_losses = 0
        self._streak_wins   = 0
        self._realized_vol  = 0.0   # intraday realized vol of SPY

        # Mode parameters (overridden per mode)
        self._mode_params = {
            RiskMode.SAFE: {
                "max_positions":    1,
                "budget_mult":      0.3,       # 30% of normal budget
                "rsi_threshold":    20,         # only extreme signals
                "max_sector_pct":   0.8,        # 80% sector limit
                "max_direction_pct":1.0,        # no direction limit in safe
                "max_daily_loss_pct":0.08,      # 8% account
            },
            RiskMode.NORMAL: {
                "max_positions":    2,
                "budget_mult":      1.0,
                "rsi_threshold":    30,
                "max_sector_pct":   0.6,        # 60% sector limit
                "max_direction_pct":0.8,        # 80% max one direction
                "max_daily_loss_pct":0.15,      # 15% account
            },
            RiskMode.AGGRESSIVE: {
                "max_positions":    3,
                "budget_mult":      1.5,
                "rsi_threshold":    32,         # slightly looser
                "max_sector_pct":   0.5,
                "max_direction_pct":0.75,
                "max_daily_loss_pct":0.20,
            },
        }

    # ── Update state ──────────────────────────────────────────────────────────

    def update(self, vix: float = 0, streak_losses: int = 0,
               streak_wins: int = 0, session_pnl: float = 0,
               account_value: float = 0, realized_vol: float = 0):
        """Update live risk metrics and re-evaluate mode."""
        with self._lock:
            if vix:
                self._vix = vix
            self._streak_losses = streak_losses
            self._streak_wins   = streak_wins
            if session_pnl:
                self._session_pnl = session_pnl
            if account_value:
                self._account_value = account_value
            if realized_vol:
                self._realized_vol = realized_vol

        self._evaluate_mode()

    def _evaluate_mode(self):
        """Automatically select risk mode based on current conditions."""
        with self._lock:
            vix     = self._vix
            losses  = self._streak_losses
            wins    = self._streak_wins
            pnl_pct = (self._session_pnl / self._account_value * 100
                       if self._account_value > 0 else 0)
            rvol    = self._realized_vol

        old_mode = self.current_mode

        # SAFE: any of these conditions
        if (vix > 25 or
                losses >= 3 or
                pnl_pct <= -8.0 or
                rvol > 0.025):
            new_mode = RiskMode.SAFE

        # AGGRESSIVE: requires ALL of these
        elif (vix < 15 and
              wins >= 5 and
              pnl_pct >= 5.0 and
              losses == 0):
            new_mode = RiskMode.AGGRESSIVE

        else:
            new_mode = RiskMode.NORMAL

        if new_mode != old_mode:
            self.current_mode = new_mode
            log.info(
                "Risk mode changed: %s → %s (VIX=%.1f losses=%d wins=%d pnl=%.1f%%)",
                old_mode, new_mode, vix, losses, wins, pnl_pct
            )

    @property
    def params(self) -> dict:
        return self._mode_params[self.current_mode]

    # ── Position entry gate ───────────────────────────────────────────────────

    def can_enter_trade(self, ticker: str, direction: str,
                        cost: float, sector: str = None) -> Tuple[bool, str]:
        """
        Portfolio-level entry gate. Call before executing any trade.
        Returns (True, "") or (False, reason_string).
        """
        p = self.params
        with self._lock:
            deployed        = dict(self._deployed)
            account_value   = self._account_value
            session_pnl     = self._session_pnl

        # 1. Max concurrent positions
        if len(deployed) >= p["max_positions"]:
            return False, (f"Max positions ({p['max_positions']}) reached "
                           f"in {self.current_mode} mode")

        # 2. Portfolio-level daily loss stop
        pnl_pct = (session_pnl / account_value * 100) if account_value > 0 else 0
        if pnl_pct <= -(p["max_daily_loss_pct"] * 100):
            return False, (f"Portfolio-level stop: session P&L={pnl_pct:.1f}% "
                           f"exceeds -{p['max_daily_loss_pct'] * 100:.0f}% limit")

        # 3. Sector exposure limit
        if sector:
            sector_deployed = sum(
                v["cost"] for v in deployed.values()
                if v.get("sector") == sector
            )
            total_deployed = sum(v["cost"] for v in deployed.values())
            if total_deployed > 0:
                sector_pct = (sector_deployed + cost) / (total_deployed + cost)
                if sector_pct > p["max_sector_pct"]:
                    return False, (f"Sector limit: {sector} would be "
                                   f"{sector_pct:.0%} of portfolio "
                                   f"(max {p['max_sector_pct']:.0%})")

        # 4. Directional balance — only applies when there are EXISTING positions
        #    First trade always allowed regardless of direction
        if deployed:
            calls_deployed = sum(
                v["cost"] for v in deployed.values()
                if v.get("direction", "").upper() == "C"
            )
            puts_deployed = sum(
                v["cost"] for v in deployed.values()
                if v.get("direction", "").upper() == "P"
            )
            total_after = calls_deployed + puts_deployed + cost
            if total_after > 0:
                if direction.upper() == "C":
                    calls_pct = (calls_deployed + cost) / total_after
                    if calls_pct > p["max_direction_pct"]:
                        return False, (f"Direction limit: calls would be "
                                       f"{calls_pct:.0%} of portfolio "
                                       f"(max {p['max_direction_pct']:.0%})")
                elif direction.upper() == "P":
                    puts_pct = (puts_deployed + cost) / total_after
                    if puts_pct > p["max_direction_pct"]:
                        return False, (f"Direction limit: puts would be "
                                       f"{puts_pct:.0%} of portfolio "
                                       f"(max {p['max_direction_pct']:.0%})")

        # 5. Emergency mode (set by execution_monitor)
        # (checked separately by execution_monitor.pre_order_checks)

        return True, ""

    # ── Position sizing ───────────────────────────────────────────────────────

    def adjust_quantity(self, base_qty: int, ticker: str,
                        iv_rank: float = 50.0,
                        budget: float = 500.0) -> int:
        """
        Adjust contract quantity based on:
        - Current risk mode (budget multiplier)
        - IV Rank (scale down when IV is expensive)
        - Analytics conviction modifier (if analytics available)
        """
        p    = self.params
        mult = p["budget_mult"]

        # IV adjustment: high IV = expensive options = smaller size
        if iv_rank > 50:
            iv_scale = 1.0 - ((iv_rank - 50) / 100)   # 0.5–1.0
            mult *= max(0.5, iv_scale)

        # Analytics conviction modifier
        if self.analytics:
            try:
                vix = self._vix
                with self._lock:
                    regime = getattr(self, "_regime", "neutral")
                conv_mod = self.analytics.get_conviction_modifier(
                    ticker, regime, vix, 28.0
                )
                mult *= conv_mod
            except Exception:
                pass

        adj_qty = max(1, round(base_qty * mult))
        log.debug(
            "adjust_quantity: base=%d mode=%s mult=%.2f iv_rank=%.0f → %d",
            base_qty, self.current_mode, mult, iv_rank, adj_qty
        )
        return adj_qty

    def get_budget(self, base_budget: float) -> float:
        """Return mode-adjusted budget."""
        return base_budget * self.params["budget_mult"]

    def get_rsi_threshold(self) -> int:
        """Return mode-appropriate RSI threshold."""
        return self.params["rsi_threshold"]

    # ── Position tracking ─────────────────────────────────────────────────────

    def register_open(self, position_key: str, ticker: str,
                      direction: str, cost: float):
        """Call when a position is confirmed open."""
        sector = SECTOR_MAP.get(ticker.upper(), "other")
        with self._lock:
            self._deployed[position_key] = {
                "ticker":    ticker,
                "direction": direction,
                "cost":      cost,
                "sector":    sector,
                "opened_at": datetime.now().isoformat(),
            }
        log.info("Risk registered open: %s %s $%.0f sector=%s mode=%s",
                 ticker, direction, cost, sector, self.current_mode)

    def register_close(self, position_key: str, pnl: float):
        """Call when a position closes. Updates session P&L."""
        with self._lock:
            self._deployed.pop(position_key, None)
            self._session_pnl += pnl
        self._evaluate_mode()   # re-check mode after each close
        log.info("Risk closed: %s P&L=$%.2f session_total=$%.2f",
                 position_key, pnl, self._session_pnl)

    # ── VAR estimation ────────────────────────────────────────────────────────

    def estimate_daily_var(self, confidence: float = 0.95) -> float:
        """
        Simple parametric VAR estimate for current deployed positions.
        Uses average option daily move of 30% (rough estimate without
        actual option Greeks).
        Returns estimated $ at risk at given confidence level.
        """
        with self._lock:
            total_cost = sum(v["cost"] for v in self._deployed.values())

        # Parametric: assume 30% daily vol on option portfolio
        # At 95% confidence: Z=1.645
        import math
        z_score    = 1.645 if confidence == 0.95 else 2.326   # 99%
        daily_vol  = 0.30   # 30% assumed daily vol for options
        var_dollar = total_cost * daily_vol * z_score / math.sqrt(252)
        return round(var_dollar, 2)

    # ── Status summary ────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current risk engine status for Discord/dashboard."""
        with self._lock:
            deployed = dict(self._deployed)
            vix      = self._vix
            losses   = self._streak_losses
            wins     = self._streak_wins
            pnl      = self._session_pnl
            acct     = self._account_value

        total_deployed   = sum(v["cost"] for v in deployed.values())
        pnl_pct          = (pnl / acct * 100) if acct > 0 else 0
        sector_breakdown = {}
        for v in deployed.values():
            s = v.get("sector", "other")
            sector_breakdown[s] = sector_breakdown.get(s, 0) + v["cost"]

        calls_d = sum(v["cost"] for v in deployed.values() if v.get("direction","").upper()=="C")
        puts_d  = sum(v["cost"] for v in deployed.values() if v.get("direction","").upper()=="P")

        return {
            "mode":             self.current_mode,
            "open_positions":   len(deployed),
            "total_deployed":   round(total_deployed, 2),
            "session_pnl":      round(pnl, 2),
            "session_pnl_pct":  round(pnl_pct, 2),
            "vix":              vix,
            "streak_losses":    losses,
            "streak_wins":      wins,
            "calls_deployed":   round(calls_d, 2),
            "puts_deployed":    round(puts_d, 2),
            "sector_breakdown": sector_breakdown,
            "var_95":           self.estimate_daily_var(0.95),
            "budget_mult":      self.params["budget_mult"],
            "max_positions":    self.params["max_positions"],
        }

    def format_status_message(self) -> str:
        """Format risk status for Discord."""
        s = self.get_status()
        mode_emoji = {"safe": "🛡️", "normal": "⚖️", "aggressive": "🚀"}.get(
            s["mode"], "⚖️"
        )
        lines = [
            f"{mode_emoji} **Risk Mode: {s['mode'].upper()}**",
            f"Open: {s['open_positions']}/{self.params['max_positions']} | "
            f"Deployed: ${s['total_deployed']:.0f}",
            f"Session P&L: ${s['session_pnl']:+.2f} ({s['session_pnl_pct']:+.1f}%)",
            f"VIX: {s['vix']:.1f} | Losses: {s['streak_losses']} | Wins: {s['streak_wins']}",
            f"Calls: ${s['calls_deployed']:.0f} | Puts: ${s['puts_deployed']:.0f}",
            f"VAR (95%): ${s['var_95']:.0f}",
        ]
        if s["sector_breakdown"]:
            lines.append("Sectors: " + " | ".join(
                f"{k}: ${v:.0f}" for k, v in s["sector_breakdown"].items()
            ))
        return "\n".join(lines)
