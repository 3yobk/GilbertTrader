"""
alerts.py -- Critical event notifications for GilbertTrader
============================================================
Sends email and/or SMS (via Twilio) when important events happen:
  - Circuit breaker fires
  - Position hits +50% or more
  - Streak pause triggered (2 consecutive losses)
  - Daily summary at 4 PM ET

Setup (.env.local):
  # Email (Gmail recommended -- create App Password at myaccount.google.com/apppasswords)
  ALERT_EMAIL_FROM=yourbot@gmail.com
  ALERT_EMAIL_TO=you@gmail.com
  ALERT_EMAIL_PASSWORD=your_app_password_here

  # SMS via Twilio (free trial at twilio.com -- $0)
  TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxx
  TWILIO_AUTH_TOKEN=your_auth_token
  TWILIO_FROM=+15551234567
  TWILIO_TO=+15559876543

Leave any section blank to skip that channel.
Both email and SMS can be active at the same time.
"""

import os
import smtplib
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional

log = logging.getLogger("alerts")

# ── Load config from .env ─────────────────────────────────────────────────────
EMAIL_FROM     = os.getenv("ALERT_EMAIL_FROM", "")
EMAIL_TO       = os.getenv("ALERT_EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("ALERT_EMAIL_PASSWORD", "")
EMAIL_SMTP     = os.getenv("ALERT_EMAIL_SMTP", "smtp.gmail.com")
EMAIL_PORT     = int(os.getenv("ALERT_EMAIL_PORT", "587"))

TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = os.getenv("TWILIO_FROM", "")
TWILIO_TO      = os.getenv("TWILIO_TO", "")

_email_ok  = bool(EMAIL_FROM and EMAIL_TO and EMAIL_PASSWORD)
_twilio_ok = bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO)

if not _email_ok and not _twilio_ok:
    log.info("alerts.py: No alert channels configured. Add ALERT_EMAIL_* or TWILIO_* to .env")


# ── Core send functions ───────────────────────────────────────────────────────

def _send_email(subject: str, body: str):
    if not _email_ok:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[GilbertTrader] {subject}"
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("Alert email sent: %s", subject)
    except Exception as e:
        log.error("Email alert failed: %s", e)


def _send_sms(body: str):
    if not _twilio_ok:
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=f"[GilbertTrader] {body}", from_=TWILIO_FROM, to=TWILIO_TO)
        log.info("SMS alert sent")
    except ImportError:
        log.warning("twilio not installed -- run: pip install twilio")
    except Exception as e:
        log.error("SMS alert failed: %s", e)


def _alert(subject: str, body: str, sms_short: Optional[str] = None):
    """Send alert in background thread so it never blocks the main loop."""
    def _send():
        _send_email(subject, body)
        _send_sms(sms_short or f"{subject}: {body[:100]}")
    threading.Thread(target=_send, daemon=True).start()


# ── Named alert functions (called from main.py / trade_engine.py) ─────────────

def alert_circuit_breaker(daily_pnl: float, limit: float):
    """Called when the circuit breaker fires."""
    subject = "CIRCUIT BREAKER FIRED"
    body = (
        f"GilbertTrader has hit the daily loss limit and stopped trading.\n\n"
        f"Daily P&L:   ${daily_pnl:.2f}\n"
        f"Limit:       ${limit:.2f}\n"
        f"Time:        {datetime.now().strftime('%Y-%m-%d %H:%M ET')}\n\n"
        f"All open positions have been closed.\n"
        f"Restart main.py when ready to trade again."
    )
    _alert(subject, body, f"Circuit breaker! P&L ${daily_pnl:.2f} hit limit ${limit:.2f}")


def alert_big_winner(profit_amt: float, ticker: str = "", strike: float = 0,
                     opt_type: str = ""):
    """Called when a position closes with profit >= $50."""
    contract_str = f"{ticker} {strike}{opt_type}" if ticker else "position"
    subject = f"BIG WINNER: {contract_str} +${profit_amt:.2f}"
    body = (
        f"Position closed with a big gain!\n\n"
        f"Contract:  {contract_str}\n"
        f"P&L:       +${profit_amt:.2f}\n"
        f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M ET')}\n\n"
        f"Check the dashboard for details."
    )
    _alert(subject, body, f"BIG WIN: {contract_str} +${profit_amt:.2f}!")


def alert_streak_pause(consecutive_losses: int):
    """Called when bot auto-pauses after consecutive losses."""
    subject = f"BOT PAUSED: {consecutive_losses} consecutive losses"
    body = (
        f"GilbertTrader has paused new entries after {consecutive_losses} losses in a row.\n\n"
        f"Time:     {datetime.now().strftime('%Y-%m-%d %H:%M ET')}\n\n"
        f"To resume: type !resume in Discord or click Resume in the dashboard.\n"
        f"Review recent trades before resuming."
    )
    _alert(subject, body, f"Bot paused after {consecutive_losses} consecutive losses. Type !resume to continue.")


def alert_daily_summary(total_trades: int, wins: int, losses: int,
                        total_pnl: float, best_trade: str, worst_trade: str):
    """Called at 4 PM ET with the day's summary."""
    win_rate = round(wins / total_trades * 100) if total_trades else 0
    subject  = f"Daily Summary: ${total_pnl:+.2f} | {win_rate}% win rate"
    body = (
        f"GilbertTrader Daily Summary\n"
        f"{'='*40}\n"
        f"Date:         {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Total Trades: {total_trades}\n"
        f"Wins:         {wins}\n"
        f"Losses:       {losses}\n"
        f"Win Rate:     {win_rate}%\n"
        f"Total P&L:    ${total_pnl:+.2f}\n"
        f"Best Trade:   {best_trade}\n"
        f"Worst Trade:  {worst_trade}\n"
        f"{'='*40}\n"
    )
    _alert(subject, body, f"Day done: ${total_pnl:+.2f}, {win_rate}% win rate, {total_trades} trades")


def alert_vix_spike(vix_level: float):
    """Called when VIX crosses 25 or 30."""
    level_str = "EXTREME" if vix_level >= 30 else "ELEVATED"
    subject   = f"VIX {level_str}: {vix_level:.1f}"
    body = (
        f"VIX has spiked to {vix_level:.1f} — market volatility is {level_str.lower()}.\n\n"
        f"{'Bot has reduced position size to 1 contract max.' if vix_level >= 30 else 'RSI thresholds have been tightened by 3 points.'}\n\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )
    _alert(subject, body, f"VIX spike: {vix_level:.1f} ({level_str})")