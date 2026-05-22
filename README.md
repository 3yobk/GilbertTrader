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

1. **Scans** 20+ tickers every 5 minutes using RSI, volume, options flow, GEX, news, and VWAP
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

| Profile | Take Profit | Stop Loss | RSI Trigger | Max Per Trade | Max Loss/Trade |
|---------|-------------|-----------|-------------|---------------|----------------|
| Safe    | +15%        | -10%      | RSI < 25    | $150          | ~$15           |
| Normal  | +30%        | -20%      | RSI < 30    | $300          | ~$60           |
| Risky   | +100%       | -40%      | RSI < 35    | $500          | ~$200          |
| Custom  | Your choice | Your choice | Your choice | Your choice | — |

**TP/SL percentages apply to the option price, not the stock price.** A 30% TP on a $2.00 option = sell at $2.60.

**Recommended profile by account size:**

| Account | Profile | Why |
|---------|---------|-----|
| < $500 | Safe | Limits max loss to ~$15/trade, preserves capital |
| $500–$1,000 | Normal | Balances opportunity with protection |
| $1,000–$2,500 | Normal ($500 budget) | Scale budget up, keep Normal TP/SL |
| > $2,500 | Risky or Custom | Larger account can absorb wider stops |

The profiles control **per-trade** risk. The **circuit breaker** (15% of account) is the account-level daily stop regardless of profile.

---

## All Features

### Signal Sources
- Discord signals — parses `BTO SPY 520C 5/16 x2` format from your channel
- Market scanner — autonomous RSI-based scanner with 7-factor verification
- Hybrid mode — both sources simultaneously with shared cooldown (no duplicate entries)

### Scanner — 6-Factor Verification
Every ticker passes all 7 gates before a signal fires:

| Gate | Check | Default |
|------|-------|---------|
| RSI | Oversold on 5-min chart | Below profile threshold |
| Volume | Above average volume | > 30% of 20-period average |
| SMA-200 | Price above 200-day moving average | Soft warning (doesn't block) |
| 1-hour RSI | Hourly momentum positive | > 30 |
| News | No bad catalysts | Score ≥ 40/100 |
| Options Flow | Unusual call activity + PCR < 1.0 | Score ≥ 35/100 |
| **GEX** | **Negative gamma exposure — stock moves freely** | **Net GEX < -$500k** |

Plus VIX filter: VIX > 25 tightens RSI by 3 pts. VIX > 30 caps contracts at 1.

**GEX (Gate 7):** Gamma Exposure measures whether market makers are positioned to pin the stock (positive GEX) or amplify its moves (negative GEX). For 0DTE scalping you want negative GEX — the stock moves aggressively and options react dramatically. Positive GEX means the stock is likely to stay flat near a key strike level, killing theta on short-dated options. Calculated live from Tradier options chain open interest and gamma. No additional API key needed.

### Position Management
- **Take-profit auto-close** — exits at your TP%
- **Stop-loss auto-close** — exits at your SL%
- **OTOCO bracket orders** — the millisecond a BTO fills, a Take-Profit limit sell and a Stop-Loss stop-limit sell are placed directly on Webull's servers as GTC orders. 2% slippage ceiling on the stop-limit fill price. These execute even if the bot crashes — risk is managed on Webull hardware, not in Python memory
- **Scale-out at 20%** — sells half the position at +20% to lock in gains. If only 1 contract, closes fully and purges the position from tracking immediately (no ghost contracts)
- **Trailing stop** — activates after scale-out, trails 8% below peak P&L
- **Breakeven guard** — moves stop to 0% after +10% gain (can never lose on a winner)
- **Momentum extension engine** — instead of closing blindly at 30 minutes, checks if P&L improved by +1% or more over the last interval. If momentum is positive, extends the timer 5 more minutes and keeps watching. Keeps extending as long as the option is moving up
- **Extension trailing floor** — during any extension window, checks live P&L every 15 seconds. If P&L drops 5% below the extension peak, closes immediately without waiting for the timer — protects against flash crashes during the extension
- **Time decay exit** — closes stagnant positions after 30 minutes if momentum is flat or falling and P&L < +2%
- **Ghost contract fix** — after any scale-out or close, syncs remaining quantity against Webull's live positions API to confirm the bot's internal count matches reality. Purges any position where Webull shows 0 contracts remaining
- **Max 2 simultaneous positions** — portfolio heat monitor pauses new entries

### Risk & Safety
- **Circuit breaker** — percentage-based, not flat dollar. Shuts down and closes all positions if daily loss exceeds a set % of your account balance (default 15%). Scales automatically as your account grows — no manual adjustment needed
- **Streak pause** — auto-pauses after 2 consecutive losses, alerts Discord + SMS/email. Streak tracked accurately via trade close callback
- **AI PC verification** — every trade approved by your local LLM before execution (3 retries with 10s/20s gaps on failure). Prompt explicitly clarifies strike vs stock price comparison to prevent false rejections
- **Budget enforcement** — rejects trades where estimated OTM option cost exceeds profile budget. Uses price-aware OTM estimates (not ATM) so large-cap options aren't incorrectly blocked
- **Spread check** — rejects options with bid/ask spread > 20%
- **Cooldown** — same ticker locked out for 30 minutes after a trade
- **Webull session auto-refresh** — detects stale token after 3 failures, re-authenticates automatically
- **Expiry-day protection** — (pending) force-close at 3:45 PM on expiration day to avoid auto-exercise risk on in-the-money contracts

### Bi-Directional Market Regime
- **Bull market** — standard operation: RSI < threshold on 5-min chart, high call flow, buys CALLS
- **Bear market** — inverted: targets RSI > 70 (overbought rallies), checks for unusual put volume or high PCR, buys PUTS with strikes slightly below current price
- **Neutral** — calls only, standard RSI threshold

### Alerts
- **Circuit breaker fired** — email + SMS immediately
- **Big winner (+50%)** — email + SMS when a position hits +50%
- **Streak pause** — email + SMS when bot auto-pauses after losses
- **VIX spike** — email + SMS when VIX crosses 25 or 30
- **Daily summary** — Discord + email at 4 PM ET with trades, win rate, P&L, best/worst trade. Filtered by current mode (paper or live)

### Alpha Sieve (Pre-Market Watchlist Filter)
Every morning at 9:15 AM ET, before the market opens, the bot automatically evaluates 50+ candidate tickers and overwrites the scanner watchlist with the top 5 highest-momentum targets for that day. The scoring formula cross-multiplies a 3-period Rate-of-Change on 1-hour bars against a volume-surge ratio (today vs 5-day average). Only tickers with positive upward momentum qualify. Result posted to Discord. Static watchlist still used as fallback on weekends or if sieve fails.

### Graceful Shutdown (Ctrl+C)
Pressing Ctrl+C triggers a clean 7-step shutdown sequence instead of a hard kill: pauses new entries → logs open positions to `brain.log` → flushes `trade_log.csv` → stops Tradier WebSocket → stops Discord listener → closes Chromium browser (saves session). SIGTERM (process kill) uses the same handler.
If the bot crashes mid-session with open positions, it reads existing positions from Webull on next startup and resumes monitoring them automatically. Manually placed phone trades also appear in the dashboard positions tab.

### Dashboard (http://localhost:8080)
- Live P&L chart, positions, trade history — all filtered by current paper/live mode
- Win rate by ticker, drawdown chart, hourly P&L heatmap, exit reason breakdown
- Streak badge and session P&L in the top ticker bar (updates every 30s)
- Market regime banner (🟢 BULL / 🔴 BEAR / ⚪ NEUTRAL) with **📋 PAPER** / **💰 LIVE** badge
- Real account balance from Webull (paper shows $100k virtual, live shows real balance)
- Bot control buttons: Pause / Resume / Force Scan / Emergency Close All
- Discord command terminal built into the UI
- Scanner feed via Server-Sent Events

---

## Tunable Settings

Everything you can change to get different behavior, organized by file.

### `main.py`

**Circuit breaker — percentage of account (default: 15%)**
```python
MAX_DAILY_LOSS_PCT = 15.0   # % of real account balance
```
The dollar threshold is calculated automatically at startup from your real Webull balance. Change to `10.0` for tighter protection or `20.0` for more room. Scales automatically when you add or withdraw funds — no manual adjustment needed.

**Extension momentum threshold (default: +1%)**
```python
if momentum > 1.0:   # in time decay extension logic in trade_engine.py
```
Change to `0.5` to extend on smaller moves (more extensions but more theta risk) or `2.0` to only extend on strong momentum.

**Extension trailing floor (default: 5% from peak)**
```python
trailing_floor = ext_peak - 5.0   # in trade_engine.py
```
Change to `3.0` for tighter crash protection during extensions, or `8.0` for more room on volatile options.

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

**Alpha Sieve timing window**
```python
if dtime(9, 15) <= now_et.time() <= dtime(9, 20):
```
Change `9, 15` to `8, 45` to run earlier (pre-pre-market) or `9, 25` to run closer to open. The 5-minute window prevents double-running.

**Alpha Sieve top N**
```python
run_alpha_sieve_pre_filter(top_n=5, listener=listener)
```
Change `top_n=5` to select more or fewer tickers each morning.

**AI PC prompt** — `ask_ai_for_verdict()` function
```python
"3. REJECT if strike is >20% away or doesn't fit budget.\n"
```
Change `>20%` to `>15%` for stricter strike filtering or `>30%` for more flexibility.

---

### `trade_engine.py`

**OTOCO bracket — slippage ceiling on stop-limit (default: 2%)**

In `webull_engine.place_otoco_bracket()`:
```python
sl_limit = round(sl_trigger * 0.98, 2)   # 2% slippage ceiling below trigger
```
Change `0.98` to `0.95` for a wider slippage allowance in fast markets, or `0.99` for tighter execution.

**Scale-out trigger (default: +20%)**
```python
scale_out_trigger_pct: float = 20.0   # sell half position at +20%
```
Change to `30.0` to let winners run longer, or `10.0` to lock in gains earlier. If you only trade 1 contract, scale-out closes the full position and purges it immediately — no ghost tracking.

**Extension momentum threshold (default: +1%)**
```python
if momentum > 1.0:
```
Minimum P&L improvement over 5 minutes to grant a time extension. Keep at `1.0` to avoid extending on bid/ask spread noise.

**Extension trailing floor (default: 5%)**
```python
trailing_floor = ext_peak - 5.0
```
If P&L drops 5% below the extension peak, closes immediately. Checked every 15 seconds — not just at the 5-minute checkpoint.

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

**Alpha Sieve — number of top tickers to select (default: 5)**
```python
run_alpha_sieve_pre_filter(top_n=5, listener=listener)
```
Change `top_n=5` to `top_n=10` to keep more candidates, or `top_n=3` for maximum focus. Called in `main.py` at 9:15 AM ET.

**Alpha Sieve — candidate pool**
```python
_ALPHA_CANDIDATE_POOL = ["NVDA", "AMD", "TSLA", ...]
```
Add any ticker to the pool for the sieve to consider. The sieve will score it and include it only if it ranks in the top N by momentum score that day.

**Alpha Sieve — momentum scoring**
The score is `3-period_ROC × volume_surge_ratio`. To weight volume more heavily, change the formula inside `run_alpha_sieve_pre_filter`:
```python
score = roc_3 * vol_surge        # current: equal weight
score = roc_3 * (vol_surge ** 2) # vol-weighted: squares the surge multiplier
```

**Watchlist — static fallback (used when sieve doesn't run)**
```python
MY_SMALL_STOCKS = [
    "NVDA", "AMD", "PLTR", "TSLA", "SOFI", "HOOD",
    ...
]
```
This list is used outside market hours and as a fallback if the sieve errors. Also manageable live with `!add TICKER` / `!remove TICKER` in Discord.

**Volume threshold (default: 30% of average)**
```python
volume_surge = current_vol > avg_vol * 0.3
```
Change `0.3` to `1.5` for the original "1.5x average" requirement. Lower = more signals, higher = only confirmed volume surges.

**1-hour RSI minimum (default: 30)**
```python
return float(rsi.dropna().iloc[-1]) > 30
```
Change `30` to `40` to only trade when hourly momentum is stronger. Lowered from 40 to 30 to avoid blocking oversold stocks with weak hourly readings.

**Options flow minimum score (default: 35)**
```python
if flow["score"] < 35:
```
Change to `50` to only trade when there's clear unusual activity, or `20` to use flow as a loose filter only.

**GEX threshold (default: ±$500k)**
```python
if net_gex < -500_000:    # negative — good for scalping
    ...
elif net_gex > 500_000:   # positive — stock pinned, skip
    ...
```
Change `500_000` to `1_000_000` to only skip on very strong pinning, or `250_000` to be more aggressive about filtering pinned stocks. Setting to `999_000_000` effectively disables the GEX gate entirely while keeping the logging.

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

**Paper vs live trading — one line, no code change needed**
```env
LIVE_TRADING_ENABLED=false   # paper mode (safe)
LIVE_TRADING_ENABLED=true    # live mode (real money)
```
The bot reads this on startup and automatically sets paper or live mode. The dashboard badge updates to show 📋 PAPER or 💰 LIVE. See [Switching Between Paper and Live](#switching-between-paper-and-live) for full details.

**Test budget**
```env
TEST_BUDGET=300   # used by test scripts only, not the main bot
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
[Gate 3] Price above 200-day SMA (soft — logs warning, does not block)
        │ NOTE
        ▼
[Gate 4] 1-hour RSI > 30
        │ PASS
        ▼
[Gate 5] News score ≥ 40/100 (Yahoo Finance)
        │ PASS
        ▼
[Gate 6] Options flow score ≥ 35/100 + PCR < 1.0 (Tradier)
        │ PASS
        ▼
[Gate 7] GEX (Gamma Exposure) negative or neutral
         Net dealer gamma < -$500k means stock moves freely
         Positive GEX = stock pinned = skip
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

The top ticker bar shows: **📋 PAPER / 💰 LIVE** | **regime** | **SPY vs SMA-20** | **streak** | **session P&L** | **scanner status**

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
| `research_scanner.py` | 7-factor scanner, Alpha Sieve, GEX, watchlist, Kelly sizing | Changing scanner filters |
| `discord_listener.py` | Discord bot, commands, signal parsing | Adding new commands |
| `signal_parser.py` | Parses BTO/STC signal strings | Supporting new signal formats |
| `news_filter.py` | Yahoo Finance news sentiment | Adding bad news keywords |
| `vwap_entry.py` | VWAP-based entry timing | Changing entry timing |
| `alerts.py` | Email + SMS alert system | Adding new alert types |
| `paper_trade_test.py` | Standalone paper order test | Testing before market open |
| `test_mode.py` | Dry-run any signal, no order placed | Debugging signals |
| `full_system_test.py` | 31-test pre-flight check | Adding tests for new features |
| `.env` | All credentials and switches | Never commit to git |
| `trade_log.csv` | Trade history (auto-generated) | Has `mode` column — paper and live trades are completely separate. Full columns: date, ticker, strike, option_type, expiry, quantity, entry_price, exit_price, outcome, profit_amt, cumulative_profit, reason, mode |
| `account_cache.json` | Real-time Webull balance + positions (auto-generated) | Written every 30s by bot. Read by API server for dashboard. Delete to reset |
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
| Alpha Sieve picks bad tickers | Volume data unavailable pre-market | Run after 9:00 AM ET when yfinance has volume data. Sieve logs each score to terminal |
| OTOCO bracket orders not in Webull | Paper account may not support GTC | Check Webull app → Paper Account → Orders. If missing, the Python monitor covers exits |
| Ctrl+C leaves positions open | Graceful shutdown logged them | Check `brain.log` for `SHUTDOWN with N open positions` — re-run bot to resume monitoring |
| Ghost contract in dashboard | Scale-out qty mismatch | Fixed in v2.2 — bot now syncs qty against Webull after every scale-out and purges positions with 0 remaining |
| GEX always shows neutral | Tradier greeks not returning | GEX requires `greeks=true` on chain fetch — check Tradier token is valid. Falls back to neutral (allows trade through) if unavailable |
| Too many signals blocked by GEX | Threshold too tight | Change `500_000` to `1_000_000` in `get_gex()` in `research_scanner.py` |
| Day P&L shows $0 in dashboard | After-hours API limitation | Webull doesn't serve real-time P&L after 4 PM. Update `account_cache.json` manually or wait for next trading session |
| Trade history empty | trade_log.csv missing columns | Delete `trade_log.csv` and restart — the new format writes all columns including strike, entry/exit price, expiry |
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
| Paper BTO order | Market closed — only passes 9:30 AM–4:00 PM ET |
| Webull browser/API tests | IDE async loop conflicts with sync Playwright — works fine when running `main.py` directly |

All other tests passing means the system is ready.

---

## Switching Between Paper and Live

The bot automatically detects which mode to use based on a single line in `.env`. No code changes needed.

### Switch to live trading

```env
# .env
LIVE_TRADING_ENABLED=true
```

Restart the bot. At startup you'll see:
```
⚠️  LIVE TRADING ENABLED — real money at risk!
```

The dashboard top bar will show **💰 LIVE** instead of **📋 PAPER**.

### Switch back to paper

```env
# .env
LIVE_TRADING_ENABLED=false
```

Restart the bot. At startup you'll see:
```
Paper mode active (LIVE_TRADING_ENABLED=false)
```

The dashboard top bar will show **📋 PAPER**.

### What changes between modes

| | Paper | Live |
|-|-------|------|
| Orders sent to | Webull paper account | Webull live account |
| Account balance shown | Paper account ($100k virtual) | Real account balance |
| Positions shown | Paper positions | Real positions (including phone trades) |
| Dashboard badge | 📋 PAPER | 💰 LIVE |
| Risk | None | Real money |

### ⚠️ Important before going live

- Paper account ID is `59105901` ($100k virtual)
- Live account needs $500+ to buy options
- The bot will show your real account balance and real open positions automatically
- Manually placed trades from your phone also appear in the dashboard under live mode

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

**One line to go live:**

```env
LIVE_TRADING_ENABLED=true
```

**One line to go back to paper:**

```env
LIVE_TRADING_ENABLED=false
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

*GilbertTrader v2.2 · Eyob Kabeto · Last updated May 2026 · First live session: $153.42 using $1k