# GilbertTrader v2.1

> Fully automated AI-powered options trading bot. Scans markets, reads Discord signals, verifies trades through an AI risk manager, executes on Webull, monitors P&L, and alerts you by email and SMS — all from one command.

**Built by Eyob Kabeto**

---

## What It Does

GilbertTrader runs continuously and handles the entire trade lifecycle automatically:

```
Discord signal  ──┐
                  ├──► Signal Parser ──► AI Risk Manager ──► Webull API ──► P&L Monitor
Market Scanner ──┘                                                               │
                                                                        Auto-close at TP/SL
                                                                                 │
                                                                        Discord + Email + SMS
```

1. **Scans** 20+ tickers every 5 minutes using RSI, volume, options flow, news, and VWAP
2. **Reads** signals from your Discord channel (`BTO SPY 520C 5/16 x2`)
3. **Verifies** every trade through your local AI PC risk manager
4. **Executes** directly via the Webull API — no browser clicking
5. **Monitors** positions and auto-closes at your take-profit or stop-loss
6. **Protects** you with a circuit breaker, streak pause, and session limits
7. **Alerts** you via Discord, email, and SMS on critical events
8. **Reports** a daily summary to Discord and email at 4 PM ET

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Configuration (.env)](#configuration-env)
3. [Running the Bot](#running-the-bot)
4. [Operation Modes](#operation-modes)
5. [Risk Profiles](#risk-profiles)
6. [All Features](#all-features)
7. [Tunable Settings — Everything You Can Change](#tunable-settings)
8. [Discord Commands](#discord-commands)
9. [Signal Format](#signal-format)
10. [Scanner Logic](#scanner-logic)
11. [Dashboard](#dashboard)
12. [Alerts (Email + SMS)](#alerts-email--sms)
13. [File Reference](#file-reference)
14. [Troubleshooting](#troubleshooting)
15. [Pre-flight Checklist](#pre-flight-checklist)
16. [Paper → Live Checklist](#paper--live-checklist)

---

## Quick Start

```powershell
# 1. Extract WebullTrader zip anywhere
# 2. Fill in .env with your credentials
# 3. Run:
.\START.ps1
```

First run installs everything (~2 min). Every run after that takes ~5 seconds. Opens 3 windows: API server, bot, and dashboard.

---

## Configuration (.env)

```env
# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN=your_bot_token
# discord.com/developers → Your App → Bot → Token

DISCORD_CHANNEL_ID=1234567890123456789
# Right-click channel → Copy Channel ID (enable Developer Mode first)

# ── AI Risk Manager ───────────────────────────────────────────────────────────
AI_PC_URL=http://192.168.1.x:5000/ask
AI_PC_KEY=sk-your-key

# ── Webull ────────────────────────────────────────────────────────────────────
WEBULL_EMAIL=your@email.com
WEBULL_TRADING_PIN=123456
# 6-digit PIN from Webull app → Security Settings

# ── Tradier (real-time options data) ─────────────────────────────────────────
TRADIER_TOKEN=your_tradier_token
# Free: dash.tradier.com/settings/api → click "Get API Access!" first
TRADIER_SANDBOX=false

# ── Safety ───────────────────────────────────────────────────────────────────
LIVE_TRADING_ENABLED=false
TEST_BUDGET=300

# ── Alerts — Email (optional) ────────────────────────────────────────────────
ALERT_EMAIL_FROM=yourbot@gmail.com
ALERT_EMAIL_TO=you@gmail.com
ALERT_EMAIL_PASSWORD=your_gmail_app_password
# App password: myaccount.google.com/apppasswords

# ── Alerts — SMS via Twilio (optional) ───────────────────────────────────────
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM=+15551234567
TWILIO_TO=+15559876543
# Free trial: twilio.com
```

---

## Running the Bot

### One command (recommended)

```powershell
.\START.ps1
```

Handles everything: installs packages, checks credentials, opens 3 windows, opens browser.

### Manual (3 terminals)

```powershell
# Terminal 1
.\.venv\Scripts\Activate.ps1
python api_server.py

# Terminal 2
.\.venv\Scripts\Activate.ps1
python main.py

# Terminal 3
cd pixel-perfect
npm run dev
```

### Pre-flight check

```powershell
python full_system_test.py
# Expected: 29/31 outside market hours
# (Webull tests conflict with IDE async loop — run main.py directly to test Webull)
```

### Test a signal without placing an order

```powershell
python test_mode.py "BTO NVDA 130C 5/23 x1"
```

### Test the full paper pipeline (market hours only)

```powershell
python paper_trade_test.py
python paper_trade_test.py "BTO SPY 580C 5/30 x1"
```

---

## Operation Modes

Select when the bot starts:

| Mode | What it does |
|------|-------------|
| `1` Discord | Only executes signals from your Discord channel |
| `2` Research | Only uses the scanner to find its own setups |
| `3` Hybrid | Both simultaneously — scanner + Discord signals |

---

## Risk Profiles

Select at startup or switch live with `!profile` in Discord.

| Profile | Take Profit | Stop Loss | RSI Trigger | Max Per Trade |
|---------|-------------|-----------|-------------|---------------|
| Safe    | +15%        | -10%      | RSI < 25    | $150          |
| Normal  | +30%        | -20%      | RSI < 30    | $300          |
| Risky   | +100%       | -40%      | RSI < 35    | $500          |
| Custom  | Your choice | Your choice | Your choice | Your choice |

---

## All Features

### Signal Sources
- Discord signals — parses `BTO SPY 520C 5/16 x2` format from your channel
- Market scanner — autonomous RSI-based scanner with 6-factor verification
- Hybrid mode — both sources simultaneously with shared cooldown (no duplicate entries)

### Scanner — 6-Factor Verification
Every ticker passes all 6 gates before a signal fires:

| Gate | Check | Default |
|------|-------|---------|
| RSI | Oversold on 5-min chart | Below profile threshold |
| Volume | Above average volume | > 30% of 20-period average |
| SMA-200 | Price above 200-day moving average | Must pass |
| 1-hour RSI | Hourly momentum positive | > 40 |
| News | No bad catalysts | Score ≥ 40/100 |
| Options Flow | Unusual call activity | Score ≥ 35/100 |

Plus VIX filter: VIX > 25 tightens RSI by 3 pts. VIX > 30 caps contracts at 1.

### Position Management
- **Take-profit auto-close** — exits at your TP%
- **Stop-loss auto-close** — exits at your SL%
- **Scale-out at 20%** — sells half the position at +20% to lock in gains
- **Trailing stop** — activates after scale-out, trails 8% below peak P&L
- **Breakeven guard** — moves stop to 0% after +10% gain (can never lose on a winner)
- **Time decay exit** — closes stagnant positions after 30 minutes if P&L < +2%
- **Max 2 simultaneous positions** — portfolio heat monitor pauses new entries

### Risk & Safety
- **Circuit breaker** — shuts down and closes all positions if daily loss hits -$150
- **Streak pause** — auto-pauses after 2 consecutive losses, alerts Discord + SMS/email
- **AI PC verification** — every trade approved by your local LLM before execution
- **Budget enforcement** — rejects trades where estimated cost exceeds profile budget
- **Spread check** — rejects options with bid/ask spread > 20%
- **Cooldown** — same ticker locked out for 30 minutes after a trade
- **Webull session auto-refresh** — detects stale token after 3 failures, re-authenticates automatically

### Alerts
- **Circuit breaker fired** — email + SMS immediately
- **Big winner (+50%)** — email + SMS when a position hits +50%
- **Streak pause** — email + SMS when bot auto-pauses after losses
- **VIX spike** — email + SMS when VIX crosses 25 or 30
- **Daily summary** — Discord + email at 4 PM ET with trades, win rate, P&L, best/worst trade

### Position Sync on Startup
If the bot crashes mid-session with open positions, it reads existing positions from Webull on next startup and resumes monitoring them automatically.

### Dashboard (http://localhost:8080)
- Live P&L chart, positions, trade history
- Win rate by ticker, drawdown chart, hourly P&L heatmap, exit reason breakdown
- Streak badge and session P&L in the top ticker bar (live, updates every 10s)
- Market regime banner (🟢 BULL / 🔴 BEAR / ⚪ NEUTRAL)
- Bot control buttons: Pause / Resume / Force Scan / Emergency Close All
- Discord command terminal built into the UI
- Scanner feed via Server-Sent Events

---

## Tunable Settings

Everything you can change to get different behavior, organized by file.

### `main.py`

**`MAX_DAILY_LOSS` (line ~292)**
```python
MAX_DAILY_LOSS = -150.0   # circuit breaker fires at -$150 session loss
```
Change to `-$250`, `-$500`, etc. to allow more daily drawdown before the bot shuts down.

**`consecutive_losses` pause threshold (line ~332)**
```python
if consecutive_losses >= 2 and not listener.paused:
```
Change `2` to `3` if you want to allow more losses before auto-pausing, or `1` for maximum caution.

**`webull_refresh_fails` auto-refresh threshold (line ~408)**
```python
if webull_refresh_fails >= 3:
```
Change `3` to a higher number if you want fewer automatic browser refreshes.

**Scanner interval (line ~356)**
```python
if time.time() - last_research_time > 300:   # 300 seconds = 5 minutes
```
Change `300` to `180` for 3-minute scans or `600` for 10-minute scans. Shorter = more opportunities found but more API calls.

**Trade cooldown (line ~379)**
```python
if ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
```
Change `1800` (30 min) to `3600` for 1-hour cooldown or `900` for 15-minute cooldown per ticker.

**Max simultaneous positions (line ~344)**
```python
if len(engine._positions) >= 2:
```
Change `2` to `3` or `4` to allow more open positions. Only do this if your budget supports it.

**AI PC prompt** — `ask_ai_for_verdict()` function
```python
"3. REJECT if strike is >20% away or doesn't fit budget.\n"
```
Change `>20%` to `>15%` for stricter strike filtering or `>30%` for more flexibility.

---

### `trade_engine.py`

**Scale-out trigger (default: +20%)**
```python
scale_out_trigger_pct: float = 20.0   # sell half position at +20%
```
Change to `30.0` to let winners run longer before taking partial profits, or `10.0` to lock in gains earlier.

**Trailing stop buffer (default: 8%)**
```python
trailing_stop_buffer_pct: float = 8.0   # trail 8% below peak
```
Change to `5.0` for a tighter trail (exits sooner) or `12.0` for a looser trail (lets it breathe more).

**Breakeven guard trigger (default: +10%)**
```python
if pnl_pct >= 10.0 and not breakeven_active:
```
Change `10.0` to `5.0` to activate breakeven protection sooner, or `15.0` to wait longer.

**Time decay exit (default: 30 minutes)**
```python
if elapsed_mins > 30 and pnl_pct < 2.0:
```
Change `30` to `45` or `60` to give positions more time to work. Change `2.0` to `5.0` to only exit truly dead positions.

**Max bid/ask spread (default: 20%)**
```python
max_spread_pct: float = 5.0   # constructor default (5%), overridden to 20% internally
```
Lower this to skip illiquid options. Higher allows wider spreads.

**Big winner alert threshold (default: +50%)**
```python
if pnl_pct >= 50.0 and not getattr(pos, "_big_win_alerted", False):
```
Change `50.0` to `30.0` to get alerted sooner, or `75.0` to only alert on massive winners.

---

### `research_scanner.py`

**Watchlist — tickers the scanner monitors**
```python
MY_SMALL_STOCKS = [
    "NVDA", "AMD", "PLTR", "TSLA", "SOFI", "HOOD",
    "AAPL", "AMZN", "META", "MSFT", "GOOGL",
    "CVNA", "RBLX", "MARA", "RIOT", "COIN",
    "SOXL", "TQQQ", "IWM",
    "SMCI", "MU", "AVGO", "ARM",
    "HIMS", "AFRM", "UPST", "SHOP",
]
```
Add or remove any ticker. Also manageable live with `!add TICKER` / `!remove TICKER` in Discord.

**Volume threshold (default: 30% of average)**
```python
volume_surge = current_vol > avg_vol * 0.3
```
Change `0.3` to `1.5` for the original "1.5x average" requirement. Lower = more signals, higher = only confirmed volume surges.

**1-hour RSI minimum (default: 40)**
```python
return float(rsi.dropna().iloc[-1]) > 40
```
Change `40` to `50` to only trade when hourly momentum is stronger, or `30` to be more permissive.

**Options flow minimum score (default: 35)**
```python
if flow["score"] < 35:
```
Change to `50` to only trade when there's clear unusual activity, or `20` to use flow as a loose filter only.

**News score minimum (default: 40)**
```python
def is_safe_to_trade(ticker: str, min_score: int = 40) -> bool:
```
Change to `60` to only trade clean tickers, or `30` to allow more trades through.

**Time gates**
```python
if _hm < 9 * 60 + 35:   # skip first 5 min after open
    ...
if 11 * 60 + 45 <= _hm <= 13 * 60 + 30:   # skip lunch 11:45-1:30 ET
```
Remove the lunch gate if you want to trade through midday. Change `9 * 60 + 35` to `9 * 60 + 45` to skip the first 15 minutes.

**VIX thresholds**
```python
if vix_level > 30:   # extreme — cap contracts at 1
elif vix_level > 25:   # elevated — tighten RSI by 3pts
```
Change `30` / `25` to `35` / `28` if you want to trade through higher volatility without restriction.

**Kelly contract sizing**
```python
def kelly_contracts(rsi, flow_score, regime, budget, option_price):
```
Maximum output is capped at 4 contracts. Change the cap inside this function if you want to allow 5+.

---

### `alerts.py`

**VIX alert levels (in `main.py` ~line 455)**
```python
if vix >= 30 and last_vix_alert_lvl < 30:   # extreme alert
elif vix >= 25 and last_vix_alert_lvl < 25:  # elevated alert
elif vix < 20:                                 # reset
```
Change thresholds to match your risk tolerance.

**Daily summary time (in `main.py` ~line 475)**
```python
if (now_et.time() >= dtime(16, 0) and now_et.time() <= dtime(16, 5) ...):
```
Change `16, 0` to send at a different time. Currently fires between 4:00-4:05 PM ET.

---

### `.env` — Safety switches

**Paper vs live trading**
```env
LIVE_TRADING_ENABLED=false   # change to true for live orders
```
Also change `paper=True` to `paper=False` in `main.py` line ~175.

**Test budget**
```env
TEST_BUDGET=300   # used by test scripts, not main bot
```

---

## Discord Commands

| Command | What it does |
|---------|-------------|
| `!status` | Positions, P&L, streak, paused/active state |
| `!scan` | Force immediate market scan |
| `!pause` | Stop new entries (keeps monitoring open positions) |
| `!resume` | Resume taking signals (also resets loss streak counter) |
| `!close ALL` | Emergency close all open positions |
| `!profile safe` | Switch to Safe (TP 15% / SL 10%) |
| `!profile normal` | Switch to Normal (TP 30% / SL 20%) |
| `!profile risky` | Switch to Risky (TP 100% / SL 40%) |
| `!watchlist` | Show current scanner tickers |
| `!add NVDA` | Add NVDA to scanner watchlist |
| `!remove CHPT` | Remove CHPT from watchlist |
| `!help` | All commands |

---

## Signal Format

```
BTO SPY 520C 5/16 x2         ← buy 2 SPY 520 call, expires May 16
BTO SPY 520C 5/16             ← buy 1 contract (default)
BTO SPY 520C 5/16 @1.50      ← max limit price $1.50
STC SPY 520C 5/16 x2         ← sell to close 2 contracts
BTO SPY 520P 5/16 x1 @0.75  ← put option, max $0.75
bto aapl 200c 6/20 x3        ← case insensitive
```

If a signal uses an expiry date that isn't available on Webull, the bot sends the available expiry dates directly to your Discord channel so you can re-send with the correct date.

---

## Scanner Logic

```
Ticker selected from watchlist
        │
        ▼
[Gate 1] RSI < threshold (profile-based, ATR-adjusted, VIX-tightened)
        │ PASS
        ▼
[Gate 2] Volume > 30% of 20-period average
        │ PASS
        ▼
[Gate 3] Price above 200-day SMA
        │ PASS
        ▼
[Gate 4] 1-hour RSI > 40
        │ PASS
        ▼
[Gate 5] News score ≥ 40/100 (Yahoo Finance)
        │ PASS
        ▼
[Gate 6] Options flow score ≥ 35/100 (Tradier)
        │ PASS
        ▼
Kelly sizing → AI verification → BTO signal fired
```

---

## Dashboard

Open **http://localhost:8080** while the bot is running.

| Tab | Contents |
|-----|---------|
| Today | Portfolio value, P&L chart, open positions, quick stats |
| Positions | Live positions with P&L%, entry/current price, close button |
| History | All past trades with entry price, exit price, reason, P&L |
| Insights | Win rate by ticker, drawdown, hourly P&L heatmap, exit breakdown |
| Watchlist | Scanner tickers with live prices |
| News | Real-time market news (Yahoo Finance) |
| Activity | Live scanner feed + Discord command terminal |

The top ticker bar shows: **regime** | **SPY vs SMA-20** | **streak** | **session P&L** | **scanner status**

---

## Alerts (Email + SMS)

Add credentials to `.env` (both are optional — leave blank to skip):

| Event | Email | SMS |
|-------|-------|-----|
| Circuit breaker fires | ✅ | ✅ |
| Position hits +50% | ✅ | ✅ |
| Streak pause (2 losses) | ✅ | ✅ |
| VIX spikes above 25 or 30 | ✅ | ✅ |
| Daily summary at 4 PM ET | ✅ | ✅ |

SMS requires a free Twilio account (twilio.com). Email requires a Gmail App Password (not your regular password).

---

## File Reference

| File | Purpose | Edit when... |
|------|---------|--------------|
| `main.py` | Entry point, main loop, all safety logic | Changing thresholds, adding features |
| `api_server.py` | Flask REST + SSE API (connects bot to UI) | Adding new dashboard endpoints |
| `webull_engine.py` | Webull API client, login, orders | Webull changes their API |
| `trade_engine.py` | Order execution, P&L monitor, exits | Changing exit logic |
| `market_feed.py` | Tradier WebSocket + yfinance fallback | Switching data providers |
| `research_scanner.py` | 6-factor scanner, watchlist, Kelly sizing | Changing scanner filters |
| `discord_listener.py` | Discord bot, commands, signal parsing | Adding new commands |
| `signal_parser.py` | Parses BTO/STC signal strings | Supporting new signal formats |
| `news_filter.py` | Yahoo Finance news sentiment | Adding bad news keywords |
| `vwap_entry.py` | VWAP-based entry timing | Changing entry timing |
| `alerts.py` | Email + SMS alert system | Adding new alert types |
| `paper_trade_test.py` | Standalone paper order test | Testing before market open |
| `test_mode.py` | Dry-run any signal, no order placed | Debugging signals |
| `full_system_test.py` | 31-test pre-flight check | Adding tests for new features |
| `.env` | All credentials and switches | Never commit to git |
| `trade_log.csv` | Trade history (auto-generated) | Feeds dashboard charts |
| `brain.log` | Bot activity log (auto-generated) | Debugging |
| `webull_profile/` | Browser session cache | **Never delete** — holds your login |
| `START.ps1` | One-click startup script | Updating launch behavior |
| `pixel-perfect/` | React dashboard | Changing UI |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `paper.ticker.untradable 417` | Market closed | Run between 9:30 AM–4:00 PM ET |
| `Opening in existing browser session` | Chrome already running | Close all Chrome windows, delete `webull_profile/SingletonLock`, restart |
| `No module flask` | Flask not in venv | Run START.ps1 again — it installs missing packages |
| `ModuleNotFoundError: pandas_ta` | Not installed | `pip install pandas-ta` in venv |
| `Token not found` | Webull session expired | Log into Webull in browser, restart bot |
| `AI PC error: 500` | LLM server crashed | Check AI PC terminal, restart LLM server |
| `AI PC timeout` | LLM too slow | Timeout is 90s — if still failing, restart model on AI PC |
| UI shows mock data | `.env.local` wrong | Set `VITE_DATA_PROVIDER=http` and `VITE_API_BASE=http://localhost:8000` |
| `Expiry not found` | Wrong date in signal | Bot sends available expiries to Discord automatically |
| `Spread too wide` | Illiquid option | Use a more liquid strike/expiry |
| Webull auto-refresh fires | 3 consecutive failed orders | Expected behavior — bot re-authenticates and continues |
| Bot paused after 2 losses | Streak protection | Type `!resume` in Discord to continue |

---

## Pre-flight Checklist

Run before every session:

```powershell
python full_system_test.py
```

Expected: **29/31 PASS** outside market hours.

| Expected failures | Why |
|-------------------|-----|
| Webull browser/API tests (3) | IDE async loop conflicts with sync Playwright — not a real issue, bot runs fine |
| Paper BTO order | Market closed — only passes 9:30 AM–4:00 PM ET |

All other tests passing means the system is ready.

---

## Paper → Live Checklist

Do all of these before switching to real money:

- [ ] Run at least one full paper session (9:30 AM–4 PM ET)
- [ ] Confirm paper orders appear in Webull app → Paper Account → Orders tab
- [ ] 20+ paper trades completed
- [ ] Win rate > 40% over 20+ trades
- [ ] Average win > average loss
- [ ] Circuit breaker tested — confirm it stops and closes positions
- [ ] `!close ALL` tested — confirm it closes all positions
- [ ] Discord alerts working
- [ ] Email/SMS alerts working (optional but recommended before going live)
- [ ] `full_system_test.py` passes 31/31 at market hours
- [ ] Fund live Webull account ($500+ for options)

**To switch to live:**

```python
# main.py line ~175:
webull = WebullEngine(paper=False)   # was paper=True
```

```env
# .env:
LIVE_TRADING_ENABLED=true            # was false
```

---

## Accounts

| Account | ID | Balance | Purpose |
|---------|----|---------|---------|
| Webull Paper | 59105901 | $100,000 virtual | Testing |
| Webull Live | — | Fund before use | Real trading |

---

## Tech Stack

**Python:** `playwright`, `playwright-stealth`, `yfinance`, `pandas`, `pandas-ta`, `numpy`, `discord.py`, `Flask`, `flask-cors`, `requests`, `python-dotenv`, `websocket-client`, `webull`, `pytz`, `streamlit`, `plotly`, `rich`, `tqdm`

**Frontend:** React (TypeScript), TanStack Start, Vite, Tailwind CSS, Bun/Node

**Data:** Tradier WebSocket (real-time), yfinance (fallback, delayed)

**Alerts:** smtplib (email), Twilio (SMS, optional)

---

*GilbertTrader v2.1 · Eyob Kabeto · Georgia Gwinnett College · Last updated May 2026*
