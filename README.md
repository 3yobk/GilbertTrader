# GilbertTrader — AI-Powered Options Trading System

A fully automated options trading bot built on a local "AI Brain" architecture.
It opens Webull in a real, stealthy browser, waits for your manual login, listens
to a paid Discord signal channel, validates every trade through an AI risk manager,
executes on Webull, and monitors P&L in real time — all from one command.

```
.\run_trader.ps1    (Windows)
./start.sh          (Mac / Linux)
```

---

## Table of Contents

1. [How It Works — Architecture](#1-how-it-works--architecture)
2. [Feature Reference](#2-feature-reference)
3. [First-Time Setup](#3-first-time-setup)
4. [Credentials and .env File](#4-credentials-and-env-file)
5. [Running the Bot](#5-running-the-bot)
6. [Startup Sequence](#6-startup-sequence-what-you-see)
7. [Signal Flow — From Discord to Webull](#7-signal-flow--from-discord-to-webull)
8. [Risk Management System](#8-risk-management-system)
9. [Research Scanner](#9-research-scanner)
10. [P&L Monitor and Auto-Exit Logic](#10-pl-monitor-and-auto-exit-logic)
11. [Performance Dashboard](#11-performance-dashboard)
12. [Operation Modes](#12-operation-modes)
13. [File Reference](#13-file-reference)
14. [Updating Webull Selectors](#14-updating-webull-selectors)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. How It Works — Architecture

```
Discord Signal Channel
        |
        v
DiscordListener (discord_listener.py)
  Regex filter — only trade signals pass through
        |
        v
AI Risk Manager (main.py --> ask_ai_for_verdict)
  Calls your AI PC at 192.168.0.15:5000
  Validates format, budget, strike distance
        |
     APPROVED?
       /    \
      No    Yes
      |      |
   Reject  TradeEngine (trade_engine.py)
            |
            v
     Market checks (market_feed.py)
       bid/ask spread  < 5%
       IV              < 200%
            |
            v
     WebullEngine (webull_engine.py)
       Playwright Stealth Chromium
       Navigates options chain
       Places limit order at mid-price
            |
            v
     PLMonitor thread (per position)
       Polls every 15 seconds
       Breakeven guard at +10%
       Scale-out 50% at +20%
       Trailing stop after scale-out
       Time decay exit after 30 min flat
       Full close at profit target / stop-loss
            |
            v
     trade_log.csv  -->  dashboard.py (Streamlit)
```

The "AI Brain" is a separate Flask server running on your home network
(`api_server.py` on your AI PC at `192.168.0.15:5000`). GilbertTrader
sends raw Discord text to it and gets back a cleaned, validated BTO string
or a rejection with a reason.

---

## 2. Feature Reference

### Risk Profiles

Four built-in strategies selectable at startup. Each sets profit target,
stop-loss, RSI threshold for the scanner, and maximum trade budget.

| # | Profile | Profit Target | Stop Loss | RSI Trigger | Max Budget |
|---|---------|--------------|-----------|-------------|------------|
| 1 | Safe    | +15%         | -10%      | RSI < 25    | $150       |
| 2 | Normal  | +30%         | -20%      | RSI < 30    | $300       |
| 3 | Risky   | +100%        | -40%      | RSI < 35    | $500       |
| 4 | Custom  | You set it   | You set it| You set it  | You set it |

### Startup Health Check

Before the browser opens, the bot verifies five things:

1. Discord token is loaded from `.env`
2. Discord channel ID is loaded from `.env`
3. AI PC is reachable at the configured URL
4. Market data is available (SPY live price from yfinance)
5. Playwright is installed

If any critical item fails, the bot exits with a clear error message
instead of failing silently mid-session.

Real output from a passing health check:
```
SYSTEM HEALTH CHECK:
  v Discord token loaded
  v Discord channel ID loaded
  v AI PC reachable
  v Market data available (SPY last: $737.62)
  v Playwright installed
All systems ready.
```

### AI Risk Manager

Every signal — from Discord or the scanner — is sent to your AI PC before
Webull is touched. The prompt tells the AI to act as a Senior Risk Manager:

1. Reformat the signal to `BTO [TICKER] [STRIKE][C/P] [EXPIRY]`
2. Reject if the strike is more than 20% away from the current stock price
3. Reject if the estimated cost exceeds the profile's budget limit

Real example from the live log:
```
Signal: BTO MARA 20C 05/22
AI verdict: Rejected - Strike >20% away
```

### Circuit Breaker (Daily Loss Limit)

At the start of every main loop iteration, the bot reads today's realized P&L
from `trade_log.csv`. If the total session loss reaches **-$150**, the bot sends
a Discord alert and shuts down cleanly. This protects a $1,000 account from
losing more than 15% in a single session. The limit resets automatically at
midnight each calendar day.

### Portfolio Heat Monitor (Max 2 Positions)

If 2 positions are already open, scanning and Discord processing pause until
one closes. A Discord cooling alert fires once per 2-minute window (not every
second). This forces focused capital allocation and prevents overtrading.

### Trade Cooldown (30 Minutes Per Ticker)

After any approved trade, that ticker is locked out for 30 minutes. Prevents
the bot from entering the same name twice in quick succession from two different
sources (e.g. Discord signal and scanner both firing on the same stock).

### Discord Talk-Back

The bot sends messages back to your Discord channel for:
- System online announcement when it connects
- AI approval or rejection for every signal
- Breakeven guard activation
- Scale-out execution (with remaining contract count)
- Time decay exit notification
- Circuit breaker trigger
- Portfolio heat cooling alerts
- High-conviction scanner signals with full details

### Persistent Browser Session

The Chromium session saves to `webull_profile/`. Once you log in to Webull,
the session persists across restarts — no re-login needed unless Webull
expires it.

---

## 3. First-Time Setup

### Windows (PowerShell)

```powershell
# 1. Open PowerShell in the WebullTrader folder

# 2. Allow script execution if you haven't before (one-time)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 3. Run the launcher — it handles everything else automatically
.\run_trader.ps1
```

The PowerShell script (`run_trader.ps1`) automatically:
- Creates a Python 3.13 virtual environment in `.venv`
- Installs all dependencies including `tqdm`
- Applies the Numba compatibility patch for pandas-ta on Windows
- Launches the Streamlit dashboard in the background
- Launches the main bot

### Mac / Linux

```bash
# 1. Make the launcher executable (one time only)
chmod +x start.sh

# 2. Run every time
./start.sh
```

The shell script (`start.sh`) automatically:
- Checks `.env` exists and warns you if it doesn't
- Installs all pip dependencies from `requirements.txt`
- Installs Playwright's Chromium browser
- Launches the Streamlit dashboard in the background on port 8501
- Launches the main bot in the foreground

Ctrl+C stops the bot and kills the background dashboard process.

### Manual Setup (if you prefer)

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # Mac/Linux
.\.venv\Scripts\Activate.ps1       # Windows PowerShell

# Install all dependencies
pip install -r requirements.txt

# Install Playwright's browser engine
playwright install chromium

# Set up credentials
cp .env.example .env
# Open .env in any text editor and fill in your real values

# Start the dashboard (optional — run in a separate terminal)
streamlit run dashboard.py

# Start the bot
python main.py
```

---

## 4. Credentials and .env File

Create a file called `.env` in the `WebullTrader` folder.
Copy from `.env.example` and fill in your real values.
**Never commit `.env` to git** — it contains private tokens.

```
# .env — fill in your real values

# Your Discord bot token
DISCORD_TOKEN=your-bot-token-here

# Right-click the signal channel in Discord -> Copy Channel ID
DISCORD_CHANNEL_ID=1393749056672501874

# Your AI PC address and API key
AI_PC_URL=http://192.168.0.15:5000/ask
AI_PC_KEY=sk-7d643db8

# Optional: Polygon.io key for real-time market data
# Leave blank to use yfinance fallback (30s delayed, free)
POLYGON_API_KEY=
```

### Getting a Discord Bot Token

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** and give it a name (e.g. GilbertTrader)
3. Click **Bot** in the left sidebar
4. Click **Reset Token**, then copy the token — paste it into `.env`
5. Under **Privileged Gateway Intents**, enable **Message Content Intent** (required to read messages)
6. Go to **OAuth2 -> URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Read Messages / View Channels` and `Send Messages`
7. Copy the generated URL, open it in your browser, and invite the bot to the server
8. Ask the signal channel admin to give the bot Read and Send access to that channel

### Getting a Polygon API Key (optional)

1. Sign up free at [polygon.io](https://polygon.io)
2. Free tier: 15-minute delayed data — fine for the scanner and pre-trade checks
3. Starter plan ($29/mo): real-time WebSocket data — needed for live IV greeks
4. Paste your key into `.env` as `POLYGON_API_KEY=your-key`
5. Without a key the bot uses yfinance (30-second poll, free, no sign-up)

---

## 5. Running the Bot

### Windows — every session
```powershell
.\run_trader.ps1
```

### Mac / Linux — every session
```bash
./start.sh
```

### Stopping
Press **Ctrl+C** in the terminal. The launcher script kills the background
Streamlit dashboard process automatically.

---

## 6. Startup Sequence (what you see)

```
Step 1 — Risk profile table appears
         Choose 1 / 2 / 3 / 4 and press Enter

Step 2 — Health check (5 items, all green required)
         v Discord token loaded
         v Discord channel ID loaded
         v AI PC reachable
         v Market data available (SPY last: $737.62)
         v Playwright installed
         All systems ready.

Step 3 — Strategy confirmation panel
         GILBERT TRADER SYSTEM ARMED
         Active Strategy: RISKY
         Parameters: TP 100.0% | SL 40.0% | RSI <35 | Budget $500

Step 4 — Chromium opens automatically in stealth mode
         Webull paper trading URL loads
         "ACTION REQUIRED — Please log in to Webull in the browser window"
         Log in manually using your Webull credentials
         The bot detects the dashboard element and continues automatically
         v Logged in and ready.

Step 5 — Market feed starts
         With Polygon key: real-time WebSocket connection
         Without key: yfinance fallback (30s poll)

Step 6 — Discord listener connects
         Bot announces itself in your signal channel:
         "GilbertTrader System Online. Monitoring market for reversals..."

Step 7 — Operation mode prompt
         1. Discord | 2. Research | 3. Hybrid
         Choose and press Enter. System is now ARMED.
```

---

## 7. Signal Flow — From Discord to Webull

### What counts as a valid signal

The Discord listener filters messages using regex. A message must contain
at least one of:
- `BTO`, `STC`, `BTC`, or `STO` (action verbs)
- `TICKER 110C` pattern (ticker + strike + type)
- `110C 5/15` pattern (strike + expiry)

All of these formats from paid channels work:
```
BTO NVDA 110C 5/15
BTO NVDA 110C 5/15 x3
BUY TO OPEN TSLA 200 CALL 05/15 2 contracts
STC SPY 450P 5/17 @2.50
NVDA 110C 5/15 BTO
```

### What the bot does step by step

1. **Capture** — Discord listener receives the message, regex confirms it looks like a trade signal
2. **Parse** — `trade_engine.parse_signal()` extracts ticker, strike, type, expiry, quantity using regex
3. **Cooldown check** — if this ticker was traded in the last 30 minutes, skip silently
4. **AI verdict** — raw text sent to AI PC; returns a clean BTO string or "Rejected + reason"
5. **Market conditions** — checks spread (skips if > 5%) and IV (skips if > 200%)
6. **Execute** — Webull browser navigates to the options chain, selects expiry and strike, fills the order at mid-price
7. **Monitor** — a P&L monitor thread starts immediately, checking every 15 seconds

### Real example from the live log
```
Signal received: BTO MARA 20C 05/22
  Parsed: BTO MARA 20.0C  exp 2026-05-22  qty 1
  AI verdict: Rejected - Strike >20% away
RISK REJECTION: Rejected - Strike >20% away
```

---

## 8. Risk Management System

GilbertTrader has six independent layers of protection:

### Layer 1 — AI Risk Manager
Every signal is sent to your AI PC before Webull is touched.
Rejects signals where the strike is OTM by more than 20% or where the
estimated cost exceeds the session budget.

### Layer 2 — Market Condition Checks
Before placing any order:
- **Bid/ask spread**: skips if `(ask - bid) / last > 5%` — wide spreads mean bad fills on illiquid options
- **IV filter**: skips if implied volatility > 200% — extreme IV usually means a binary event priced in

### Layer 3 — Portfolio Heat Monitor
Hard cap at 2 simultaneous open positions. When at capacity, all scanning
and signal processing pauses. A Discord cooling alert fires once per
2-minute window.

### Layer 4 — Circuit Breaker
Reads today's realized P&L from `trade_log.csv` every loop. If session
loss hits -$150, sends Discord alert and exits cleanly. Auto-resets
each calendar day.

### Layer 5 — Trade Cooldown
30-minute per-ticker lockout after any approved trade. Prevents
double-entry from both Discord and scanner hitting the same name.

### Layer 6 — Paper Mode Default
`WebullEngine` always starts with `paper=True`. Paper trades are
completely separated from your real money account in Webull.

---

## 9. Research Scanner

Active in Mode 2 (Research) and Mode 3 (Hybrid).
Scans every 5 minutes during market hours.

### Watchlist

Defined at the top of `research_scanner.py` — edit freely:
```python
MY_SMALL_STOCKS = ["AMC", "GME", "CVNA", "FUBO", "RBLX",
                   "TLRY", "CHPT", "LCID", "GRTX", "MARA"]
```

### Triple-Verification Filter

A stock must pass all three checks to generate a signal:

**Check 1 — RSI Oversold (5-minute chart)**
Downloads the current trading day in 5-minute bars.
Calculates 14-period RSI. Must be below the profile's RSI threshold.

ATR Adaptive Adjustment: if the stock's Average True Range (ATR) is
greater than 2% of price (high volatility day), the RSI threshold
drops by 5 — requiring the stock to be even more oversold before entry.
Prevents buying into volatile chop.

**Check 2 — Volume Surge**
Current 5-minute bar volume must exceed 1.5x the 20-bar rolling average.
No volume force = no entry.

**Check 3 — Multi-Timeframe Alignment**
- Daily chart (SMA-200): current price must be above the 200-day moving average. Bearish trend = skip.
- Hourly chart (RSI-14): 1-hour RSI must be above 40. If the whole day is in freefall, skip.

### Signal Generation

When a stock passes all three checks, the scanner:
1. Reads the current stock price from yfinance
2. Calculates the strike — nearest $5 increment above the current price (ATM call)
3. Calculates the expiry — nearest Friday at least 7 days out (avoids same-week theta crush)
4. Builds a real BTO string: `BTO AMC 4C 5/23`
5. Sends a full Discord alert with price, RSI, strike, expiry, and the signal string
6. Passes the signal through the same AI risk manager as Discord signals

### Market Hours Guard

The scanner returns an empty list immediately outside 9:30 AM – 4:00 PM ET,
Monday through Friday. No "No live data" spam. Clean log output:
```
[Scanner] Market closed. Opens in ~1014 min — skipping scan.
```

---

## 10. P&L Monitor and Auto-Exit Logic

Each open position gets its own background thread that polls every 15 seconds.
It reads the Positions tab in Webull first (most accurate for options P&L),
then falls back to the market feed if the UI scrape fails.

### Exit conditions (checked every 15 seconds, in order):

**Breakeven guard — activates at +10% P&L**
Stop-loss moves from the profile level to 0%. You can never lose on a
trade that reached +10%. The trailing stop will not go below 0% even
after scale-out.
Discord: `Breakeven: NVDA_110C hit 10%. SL moved to 0.0%`

**Scale-out — fires once at +20% P&L**
Sells exactly 50% of the position at the current mid-price.
Logs the partial close to `trade_log.csv` as `Scale-Out 50%`.
The remaining contracts continue running with the trailing stop.
Discord: `SCALE-OUT: Sold 2x NVDA_110C @ +20%. 2x remaining.`

**Trailing stop — activates after scale-out**
Once half the position is sold, the stop trails 8% below the peak P&L.

Examples:
- Position peaks at +40% → stop is at +32%
- Position keeps running to +60% → stop moves to +52%
- If it reverses from +60% to +52% → auto-close fires

The trailing stop never goes below 0% (breakeven protection stays active).

**Time decay exit — fires after 30 minutes if P&L below +2%**
If a position has been open for 30 minutes and has not moved more
than +2%, it closes automatically. Options lose value to theta every
minute they sit still.
Discord: `TIME EXIT: NVDA_110C stagnant for 30m. Closing.`

**Profit target — full close at the profile's TP%**
100% exit at 15% / 30% / 100% / custom.

**Stop-loss — full close at the current stop level**
The current stop level depends on the position's history:
- Before breakeven: fixed at the profile SL% (e.g. -40% on Risky)
- After breakeven guard: 0%
- After scale-out: trailing (peak P&L minus 8%)

### Status table (printed every 30 seconds)
```
Open Positions
+-----------------------+--------+---------+-----------+--------+------------+-------+
| Contract              | Action | Entry $ | Peak P&L  | Scaled | Expiry     | Mode  |
+-----------------------+--------+---------+-----------+--------+------------+-------+
| NVDA 110.0C           | BTO    | $2.45   | +18.4%    | No     | 2026-05-15 | PAPER |
+-----------------------+--------+---------+-----------+--------+------------+-------+
```

---

## 11. Performance Dashboard

A Streamlit web app that tracks trading performance.
Opens automatically when you use the launcher script.

**URL:** `http://localhost:8501`

### What it shows

- **Total Trades** — number of completed closed positions
- **Win Rate** — percentage of trades that closed in profit
- **Net Profit** — total dollar P&L across all closed trades
- **Win/Loss Pie Chart** — visual breakdown
- **Equity Curve** — cumulative P&L over time (Path to $1,000 Profit)
- **Recent Trades Table** — last 10 trades with date, ticker, outcome, and exit reason

### Auto-refresh

Refreshes automatically every 30 seconds using `st.rerun()`.
A countdown shows in the top of the page so you can see when the next
update fires. No manual browser refresh needed during live sessions.

### Trade log format

All completed trades write to `trade_log.csv`:
```
date,ticker,outcome,profit_amt,cumulative_profit,reason
2026-05-09 10:42,NVDA,Win,48.50,48.50,Profit Target
2026-05-09 11:15,MARA,Win,22.00,70.50,Scale-Out 50%
2026-05-09 11:15,MARA,Loss,-18.00,52.50,Stop Loss
```

Exit reasons that appear in the log:
`Profit Target`, `Stop Loss`, `Scale-Out 50%`, `Time Decay`, `Manual STC`, `Breakeven`

---

## 12. Operation Modes

After Webull login, you choose how the bot sources signals:

### Mode 1 — Discord Only
Passive. The bot waits for signals from your paid Discord channel.
No active scanning. Best when your paid channel is high quality and
you want to follow it exactly.

### Mode 2 — Research Only
Active. Bot ignores Discord and runs its own triple-verified scan
every 5 minutes. No Discord signals are processed. Best for testing
the scanner independently.

### Mode 3 — Hybrid (recommended)
Both sources run simultaneously. Scanner runs every 5 minutes.
Discord signals are picked up in real time. Whichever fires first goes
through the AI risk manager. The 30-minute ticker cooldown prevents
double-entry if both sources hit the same name.

---

## 13. File Reference

```
WebullTrader/
|
+-- main.py                Entry point. Risk profiles, health check, main loop,
|                          AI risk manager, circuit breaker, heat monitor.
|
+-- trade_engine.py        Signal parser, order execution, P&L monitor thread,
|                          breakeven guard, scale-out, trailing stop, time decay,
|                          CSV trade logging, session P&L calculation.
|
+-- webull_engine.py       Playwright Stealth browser automation for Webull.
|                          SELECTORS dict at top — update here if Webull UI changes.
|
+-- discord_listener.py    Connects to Discord, filters signal messages,
|                          stores latest signal, sends alerts back to channel.
|
+-- market_feed.py         Live market data. Polygon.io WebSocket (if key set)
|                          or yfinance 30s poll fallback. Option greeks via REST.
|
+-- research_scanner.py    Triple-verified scanner. RSI + ATR + Volume + SMA-200
|                          + 1-hour alignment. Generates real strike/expiry signals.
|
+-- dashboard.py           Streamlit performance dashboard. Auto-refreshes every 30s.
|
+-- run_trader.ps1         Windows launcher. Creates venv, installs deps,
|                          applies Numba patch, starts dashboard + bot.
|
+-- start.sh               Mac/Linux launcher. Checks .env, installs deps,
|                          installs Chromium, starts dashboard + bot.
|
+-- requirements.txt       All Python dependencies (plain UTF-8, pip-compatible).
|
+-- .env                   YOUR credentials. Never commit to git.
+-- .env.example           Template — copy to .env and fill in values.
+-- .gitignore             Excludes .env, webull_profile/, trade_log.csv.
|
+-- trade_log.csv          Auto-created on first trade. All closed positions.
+-- brain.log              Full timestamped log of every system event.
+-- dashboard.log          Streamlit output log.
+-- webull_profile/        Persistent Chromium session — keeps you logged in to Webull.
```

---

## 14. Updating Webull Selectors

Webull updates their web UI without notice. When the bot fails to click
a button or find a field, the fix is in the `SELECTORS` dictionary at
the top of `webull_engine.py`.

### How to find the correct new selector

1. Open `https://app.webull.com/paper-trade` in your regular Chrome browser
2. Right-click the element that is broken -> **Inspect**
3. Look for a unique `class`, `data-testid`, or `aria-label` attribute
4. Update the matching key in `SELECTORS`

### Most commonly broken selectors

| What breaks          | Key to update in SELECTORS   |
|----------------------|------------------------------|
| Login detection      | `logged_in_indicator`        |
| Symbol search box    | `search_input`               |
| Options tab button   | `options_tab`                |
| Expiry date picker   | `expiry_container`           |
| Review order button  | `review_button`              |
| Place order button   | `confirm_button`             |
| Quantity input       | `qty_input`                  |
| Positions tab        | `positions_tab`              |

---

## 15. Troubleshooting

### "Improper token has been passed" (Discord error)
Your `.env` still has the placeholder token from `.env.example`.
Replace `DISCORD_TOKEN` with your real bot token from
discord.com/developers -> your app -> Bot -> Reset Token.

### "AI PC Offline" / all signals auto-rejecting
The Flask server (`api_server.py`) on your AI PC is not running,
or the two machines are on different networks. Start the server on
your AI PC and confirm both machines are on the same Wi-Fi.

### "Strike >20% away" rejections
The AI risk manager is working correctly. The signal's strike is
deep OTM relative to the current stock price. This is the expected
behavior — deep OTM options are high risk and get filtered. If you
want to allow wider strikes, increase the 20% threshold in the
`ask_ai_for_verdict` prompt in `main.py`.

### Scanner says "Market closed" constantly
This is correct and expected. The scanner only runs during
9:30 AM – 4:00 PM ET, Monday through Friday. It skips automatically
outside those hours and logs the minutes until open.

### Bot opens Webull but can't find the options chain
Webull changed their UI. Open `webull_engine.py`, find `SELECTORS`
at the top, and update the broken entry. Use browser DevTools (F12)
on the live Webull page to find the new class name or `data-testid`.
See Section 14.

### "ModuleNotFoundError: tqdm" or pandas-ta errors (Windows)
Use `run_trader.ps1` — it applies the Numba compatibility patch
automatically. Or manually:
```powershell
pip install tqdm
pip install pandas-ta --no-deps
```

### Dashboard not loading at localhost:8501
The Streamlit process failed to start. Check `dashboard.log` in the
project folder for the error. Re-run the launcher script.

### Webull asks me to log in every time
The `webull_profile/` folder holds your session cookies. If Webull
has expired your session, just log in normally when the bot prompts
you. If the folder gets corrupted and causes errors, delete it:
```bash
rm -rf webull_profile/     # Mac/Linux
rd /s /q webull_profile    # Windows Command Prompt
```
The bot will prompt for login on next run and rebuild the profile.

### requirements.txt causes pip encoding errors
The file must be plain UTF-8 with no BOM. Open it in VS Code, click
the encoding indicator in the bottom-right status bar, select
"Save with Encoding", and choose UTF-8.

### PyNaCl / davey warnings in the log
```
WARNING — PyNaCl is not installed, voice will NOT be supported
WARNING — davey is not installed, voice will NOT be supported
```
These are harmless. GilbertTrader does not use Discord voice features.
The warnings come from discord.py and can be ignored.

---

## Safety and Legal Notes

- **Paper mode is the default.** The bot always starts with Webull Paper Trading.
  Paper trades are completely separate from your real money account.
- To enable real money trading, change `paper=True` to `paper=False` in the
  `WebullEngine` initialization in `main.py`. This adds a terminal confirmation
  prompt before every single real order.
- This project automates a browser session against Webull's web interface.
  It does not use an official Webull API. Use responsibly and ensure compliance
  with Webull's Terms of Service.
- Options trading carries substantial risk of loss. This software was built as
  a capstone engineering project and is for educational purposes.
- Never trade with money you cannot afford to lose entirely.
- Always run in paper mode and verify every feature is working correctly
  before considering a switch to real money.

---

*GilbertTrader — Built by Eyob K. | GGC Computer Science Capstone Project*
