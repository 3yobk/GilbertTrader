# GilbertTrader 🤖

> A fully automated AI-powered options trading system. Scans markets, reads Discord signals, verifies trades through an AI risk manager, executes on Webull, and monitors P&L — all from one command.

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![React](https://img.shields.io/badge/React-18-61dafb?style=flat-square&logo=react)
![Webull](https://img.shields.io/badge/Broker-Webull-00c087?style=flat-square)
![Discord](https://img.shields.io/badge/Signals-Discord-5865f2?style=flat-square&logo=discord)
![License](https://img.shields.io/badge/License-Personal-lightgrey?style=flat-square)

---

## What It Does

GilbertTrader runs 24/7 and handles the entire trade lifecycle:

```
Discord signal  ──┐
                  ├──▶  Signal Parser  ──▶  AI Risk Manager  ──▶  Webull API  ──▶  P&L Monitor
Market Scanner ──┘                                                                      │
                                                                                        ▼
                                                                               Auto-close at TP/SL
```

1. **Scans** 20+ tickers every 5 minutes using RSI, volume, options flow, news, and VWAP
2. **Reads** signals from your Discord channel (`BTO SPY 520C 5/16 x2`)
3. **Verifies** every trade through your local AI PC risk manager
4. **Executes** directly via the Webull API — no browser clicking
5. **Monitors** positions in real time and auto-closes at your take-profit or stop-loss
6. **Reports** everything to Discord and the live React dashboard

---

## Table of Contents

1. [Architecture](#architecture)
2. [Features](#features)
3. [System Requirements](#system-requirements)
4. [Installation](#installation)
5. [Configuration (.env)](#configuration-env)
6. [Running the System](#running-the-system)
7. [Discord Commands](#discord-commands)
8. [Signal Format](#signal-format)
9. [Risk Profiles](#risk-profiles)
10. [Scanner Logic](#scanner-logic)
11. [Dashboard](#dashboard)
12. [File Reference](#file-reference)
13. [Troubleshooting](#troubleshooting)

---

## Architecture

```
WebullTrader/
├── main.py                ← Entry point — orchestrates everything
├── api_server.py          ← Flask REST + SSE API (connects Python to React UI)
├── webull_engine.py       ← Webull API client (login, orders, positions)
├── trade_engine.py        ← Order execution, P&L monitoring, auto-close logic
├── market_feed.py         ← Real-time data (Tradier WebSocket + yfinance fallback)
├── research_scanner.py    ← 6-factor market scanner
├── discord_listener.py    ← Discord bot (signals in, alerts out, commands)
├── signal_parser.py       ← Parses BTO/STC signal strings
├── news_filter.py         ← Blocks trades with bad news catalysts
├── vwap_entry.py          ← VWAP-based entry timing
├── dashboard.py           ← Streamlit analytics (legacy)
├── full_system_test.py    ← Pre-flight system test
└── pixel-perfect/         ← React dashboard (main UI)
    ├── src/
    │   ├── components/GilbertDashboard.tsx
    │   ├── lib/provider.ts      ← Mock ↔ HTTP data switching
    │   ├── hooks/useData.ts     ← React hooks for all API data
    │   └── routes/api/          ← Server-side API routes
    └── .env.local               ← VITE_DATA_PROVIDER + VITE_API_BASE
```

**Data flow at runtime:**

```
Tradier WebSocket ──▶ market_feed.py ──▶ research_scanner.py ──▶ trade_engine.py
Discord channel   ──▶ discord_listener.py ──▶ signal_parser.py ──▶ trade_engine.py
                                                                        │
                                                              webull_engine.py
                                                                        │
                                                              Webull Paper/Live API
                                                                        │
                                                              api_server.py ──▶ React UI
```

---

## Features

### Signal Sources
- **Discord signals** — reads your channel and parses `BTO SPY 520C 5/16 x2` format
- **Market scanner** — autonomous RSI-based scanner with 6-factor verification
- **Hybrid mode** — both sources simultaneously

### Scanner — 6-Factor Verification
Every signal passes all 6 gates before executing:

| Gate | What it checks | Why it matters |
|------|---------------|----------------|
| RSI < threshold | Stock is oversold on 5-min chart | Entry timing |
| ATR adjustment | Volatility-adjusted RSI threshold | Adapts to market conditions |
| Volume surge | Current volume > 1.5× 20-period average | Confirms real buying pressure |
| SMA-200 trend | Price above 200-day moving average | Only trade with the trend |
| 1-hour RSI | Hourly RSI > 40 | Avoids intraday crashes |
| Options flow | Unusual call volume vs open interest | Smart money confirmation |

Plus two additional checks:
- **News filter** — blocks trades when bad catalysts detected (earnings miss, SEC investigation, bankruptcy, etc.)
- **VWAP timing** — shows whether price is at institutional fair value

### Position Management
- **Take-profit auto-close** — exits when P&L hits your TP%
- **Stop-loss auto-close** — exits when P&L drops below your SL%
- **Scale-out at 50%** — sells half the position at 50% of TP to lock in gains
- **Breakeven guard** — moves stop to 0% after +10% gain (can never lose on a winner)
- **Trailing stop** — activates after scale-out, protects remaining position
- **Time decay exit** — closes stagnant positions after 30 minutes (avoids theta decay)
- **Circuit breaker** — shuts down trading if daily loss exceeds $150

### Risk Management
- 4 risk profiles (Safe / Normal / Risky / Custom)
- Maximum 2 open positions simultaneously (portfolio heat monitor)
- Kelly-based position sizing (scales contracts 1-5 based on signal conviction)
- AI PC budget verification before every trade
- Market regime filter — only calls in bull/neutral, no calls in bear market

### Execution
- Direct Webull API integration — no browser clicking
- Cookie token extraction (authenticated from browser session, no MFA prompts)
- Smart limit pricing — mid-price + buffer for BTO, bid - buffer for STC
- Paper trading mode (`paper=True`) for safe testing
- Spread check before entry — rejects options with wide bid/ask spreads

### Dashboard
- Live React UI (pixel-perfect Webull-style design)
- Real-time scanner feed via Server-Sent Events
- P&L curve, drawdown chart, win rate by ticker, hourly P&L heatmap
- Bot control buttons — Pause/Resume/Force Scan/Emergency Close All
- Discord command terminal built into the UI
- Market regime banner (🟢 BULL / 🔴 BEAR / 🟡 NEUTRAL)
- Live positions with P&L polling every 10 seconds

---

## System Requirements

| Component | Requirement |
|-----------|-------------|
| Python | 3.11+ |
| Node.js | 18+ (or Bun) |
| OS | Windows 10/11 (Mac/Linux with minor path changes) |
| RAM | 4GB minimum, 8GB recommended |
| Webull | Free account (paper or live, options level enabled) |
| Discord | Bot token + channel ID |
| AI PC | Local LLM server running at a known IP:port |
| Tradier | Free brokerage account (for real-time options data) |

---

## Installation

### 1. Clone and set up Python environment

```powershell
cd WebullTrader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install webull
playwright install chromium
```

### 2. Set up the React UI

```powershell
cd pixel-perfect
bun install        # or: npm install
```

Create `pixel-perfect/.env.local`:
```
VITE_DATA_PROVIDER=http
VITE_API_BASE=http://localhost:8000
```

### 3. Configure credentials

Copy `.env.example` to `.env` and fill in your values (see [Configuration](#configuration-env) below).

### 4. Run the pre-flight test

```powershell
cd WebullTrader
.\.venv\Scripts\Activate.ps1
python full_system_test.py
```

All 31 tests should pass except "Paper BTO order" which requires market hours.

---

## Configuration (.env)

```env
# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN=your_bot_token_here
# Get from: discord.com/developers → Your App → Bot → Token

DISCORD_CHANNEL_ID=1234567890123456789
# Right-click your signal channel → Copy Channel ID (enable Developer Mode first)

# ── AI PC Risk Manager ────────────────────────────────────────────────────────
AI_PC_URL=http://192.168.1.x:5000/ask
# Your local LLM server address

AI_PC_KEY=sk-your-key-here
# API key for your local LLM server

# ── Webull ────────────────────────────────────────────────────────────────────
WEBULL_EMAIL=your@email.com
# Your Webull login email

WEBULL_TRADING_PIN=123456
# Your 6-digit Webull trading PIN (different from login password)

# ── Tradier (real-time options data) ─────────────────────────────────────────
TRADIER_TOKEN=your_tradier_token_here
# Free at: dash.tradier.com/settings/api
# After creating account, click "Get API Access!" on your dashboard

TRADIER_SANDBOX=false
# Set to true for paper/sandbox testing

# ── Optional ──────────────────────────────────────────────────────────────────
FINNHUB_KEY=your_finnhub_key
# Optional — news filter backup source
# Free at: finnhub.io/register (60 calls/min)
```

### Getting each credential

**Discord Bot Token:**
1. Go to [discord.com/developers](https://discord.com/developers/applications)
2. New Application → Bot → Reset Token → Copy

**Discord Channel ID:**
1. Discord Settings → Advanced → Enable Developer Mode
2. Right-click your signal channel → Copy Channel ID

**Tradier Token:**
1. Open a free account at [brokerage.tradier.com](https://brokerage.tradier.com)
2. Go to [dash.tradier.com/settings/api](https://dash.tradier.com/settings/api)
3. Click **"Get API Access!"** on your dashboard (required — separate from account creation)
4. Generate token and copy it

**Webull Trading PIN:**
- This is the 6-digit number Webull asks before placing orders
- Not your login password — set it in Webull app under Security Settings

---

## Running the System

### Option A — One command (recommended)

```powershell
cd WebullTrader
.\start_all.ps1
```

Opens 3 terminals: API server, bot, and UI dashboard.

### Option B — Manual (3 terminals)

**Terminal 1 — API Server:**
```powershell
.\.venv\Scripts\Activate.ps1
python api_server.py
```

**Terminal 2 — Main Bot:**
```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

**Terminal 3 — UI Dashboard:**
```powershell
cd pixel-perfect
bun run dev
```

Then open **http://localhost:3000**

### Startup sequence

1. Select risk profile (1-4)
2. Browser opens — log in to Webull (saved after first time)
3. Select operation mode:
    - `1` — Discord only (executes signals from your channel)
    - `2` — Research only (scanner finds its own setups)
    - `3` — Hybrid (both simultaneously)
4. Bot runs — watch the terminal and Discord for activity

---

## Discord Commands

Type these in your Discord signal channel and the bot responds:

| Command | What it does |
|---------|-------------|
| `!status` | Show open positions, session P&L, regime |
| `!scan` | Force an immediate market scan |
| `!pause` | Stop new entries (keeps monitoring open positions) |
| `!resume` | Resume taking new signals |
| `!close ALL` | Emergency close all open positions |
| `!profile safe` | Switch to Safe profile (TP 15% / SL 10%) |
| `!profile normal` | Switch to Normal profile (TP 30% / SL 20%) |
| `!profile risky` | Switch to Risky profile (TP 100% / SL 40%) |
| `!watchlist` | Show current scanner tickers |
| `!add NVDA` | Add NVDA to the scanner watchlist |
| `!remove CHPT` | Remove CHPT from the watchlist |
| `!help` | Show all commands |

Commands also work from the UI dashboard's Discord terminal panel.

---

## Signal Format

The bot parses these signal formats from Discord:

```
BTO SPY 520C 5/16 x2         ← Buy 2 SPY 520 Call, expires May 16
BTO SPY 520C 5/16             ← Buy 1 contract (default quantity)
BTO SPY 520C 5/16 @1.50      ← Buy with max limit price $1.50
BTO SPY 520C 5/16 x2 @1.50  ← Buy 2 contracts, max $1.50
STC SPY 520C 5/16 x2         ← Sell to close 2 contracts
BTO SPY 520P 5/16 x1 @0.75  ← Put option
bto aapl 200c 6/20 x3        ← Case insensitive
```

**Fields:**
- `BTO` — Buy to Open | `STC` — Sell to Close
- Ticker — 1-5 uppercase letters
- Strike — number (integer or decimal)
- `C` or `P` — Call or Put
- Expiry — M/D format (e.g. `5/16`, `12/20`)
- `x2` — quantity (optional, defaults to 1)
- `@1.50` — max limit price (optional, defaults to mid-price)

---

## Risk Profiles

| Profile | Take Profit | Stop Loss | RSI Trigger | Max Trade |
|---------|-------------|-----------|-------------|-----------|
| Safe | +15% | -10% | RSI < 25 | $150 |
| Normal | +30% | -20% | RSI < 30 | $300 |
| Risky | +100% | -40% | RSI < 35 | $500 |
| Custom | Your choice | Your choice | Your choice | Your choice |

Switch profiles live from Discord: `!profile risky`

---

## Scanner Logic

The scanner runs every 5 minutes across 20+ tickers. A signal fires only when **all 6 gates pass:**

```
Ticker selected
      │
      ▼
[Gate 1] RSI < threshold (5-min bars, ATR-adjusted)
      │ PASS
      ▼
[Gate 2] Volume > 1.5× 20-period average
      │ PASS
      ▼
[Gate 3] Price above 200-day SMA
      │ PASS
      ▼
[Gate 4] 1-hour RSI > 40
      │ PASS
      ▼
[Gate 5] News score ≥ 40/100 (no bad catalysts)
      │ PASS
      ▼
[Gate 6] Options flow score ≥ 35/100 (Tradier)
      │ PASS
      ▼
Kelly sizing → BTO signal fired to Discord + trade_engine
```

**Default watchlist:**
Small caps: AMC, GME, CVNA, FUBO, RBLX, TLRY, CHPT, LCID, GRTX, MARA
Large caps: AAPL, NVDA, TSLA, AMZN, META
ETFs: QQQ, XLF, XBI, SOXS

Add/remove via `!add TICKER` / `!remove TICKER` in Discord.

---

## Dashboard

Open **http://localhost:3000** while the bot is running.

| Tab | What's there |
|-----|-------------|
| Today | Portfolio value, P&L chart, open positions, quick stats |
| Positions | Live positions with P&L%, entry/current price, status, close button |
| History | All past trades with exit reason and P&L |
| Insights | Win rate by ticker, drawdown chart, hourly P&L heatmap, exit breakdown |
| Watchlist | Current scanner tickers with live prices |
| News | Real-time market news (Yahoo Finance, no key needed) |
| Activity | Live scanner feed + Discord command terminal |

**Header controls:**
- ▶ / ⏸ — Pause or resume the bot
- ⟳ Scan — Force an immediate scan
- ⚡ Close All — Emergency close (requires confirmation click)
- Regime banner — Shows 🟢 BULL / 🔴 BEAR / 🟡 NEUTRAL based on SPY SMA

---

## File Reference

| File | Purpose | Edit when... |
|------|---------|--------------|
| `main.py` | Bot entry point, main loop | Changing operation modes or adding new features |
| `api_server.py` | REST API connecting bot to UI | Adding new UI data endpoints |
| `webull_engine.py` | Webull API client | Webull changes their API or auth flow |
| `trade_engine.py` | Order execution and P&L monitor | Changing exit logic (TP/SL/trailing stop) |
| `market_feed.py` | Data feed (Tradier/yfinance) | Switching data providers |
| `research_scanner.py` | Market scanner | Changing scanner filters or watchlist |
| `discord_listener.py` | Discord bot | Adding new commands |
| `signal_parser.py` | Signal text parser | Supporting new signal formats |
| `news_filter.py` | News catalyst filter | Adding/removing bad news keywords |
| `vwap_entry.py` | VWAP entry timing | Changing entry timing logic |
| `full_system_test.py` | System health check | Adding tests for new features |
| `.env` | All credentials | Never commit to git |
| `trade_log.csv` | Trade history | Auto-generated, used by dashboard |
| `brain.log` | Bot activity log | Auto-generated, tailed by dashboard |
| `webull_profile/` | Browser session cache | Never delete — saves your Webull login |

---

## Troubleshooting

### "Market closed — paper.ticker.untradable"
Normal behavior. Paper options orders only accepted during market hours (9:30 AM–4:00 PM ET). Run the bot at market open.

### "TRADIER_TOKEN not set"
1. Open [brokerage.tradier.com](https://brokerage.tradier.com) — create free account
2. Go to your dashboard → click **"Get API Access!"** (required separate step)
3. Go to [dash.tradier.com/settings/api](https://dash.tradier.com/settings/api) → generate token
4. Add `TRADIER_TOKEN=your_token` to `.env`

### "webull not installed"
```powershell
.\.venv\Scripts\Activate.ps1
pip install webull
```

### "pandas_ta not installed"
```powershell
pip install pandas-ta
```

### Browser opens but login keeps looping
Delete the `webull_profile/` folder and log in fresh:
```powershell
Remove-Item -Recurse -Force webull_profile
python main.py
```

### AI PC timeout
Your AI PC is taking too long. Check it's running and reachable at the URL in `.env`. The timeout is 45 seconds — if your PC is slow, reduce the question length in `main.py`'s `ask_ai_for_verdict()`.

### UI shows no data / "Mock Data"
Make sure `pixel-perfect/.env.local` exists with:
```
VITE_DATA_PROVIDER=http
VITE_API_BASE=http://localhost:8000
```
And that `api_server.py` is running (`python api_server.py`).

### Discord commands not responding
1. Confirm your `DISCORD_TOKEN` is for a **Bot** (not user token)
2. Enable **Message Content Intent** in Discord Developer Portal → Your App → Bot → Privileged Gateway Intents
3. Invite the bot to your server with `bot` and `applications.commands` scopes

---

## Pre-flight Checklist

Run before every trading session:

```powershell
python full_system_test.py
```

Expected: **30/31 PASS** (paper order test only passes during market hours).

| Test | What confirms |
|------|--------------|
| ✅ Credentials | All keys loaded from .env |
| ✅ Signal parser | All signal formats parse correctly |
| ✅ yfinance | Live price data working |
| ✅ Tradier | Real-time quotes working |
| ✅ Market regime | SPY trend detection working |
| ✅ News filter | Yahoo Finance news working |
| ✅ VWAP | Intraday VWAP calculating correctly |
| ✅ Scanner | Runs without errors, regime detected |
| ✅ Kelly sizing | Position sizing math correct |
| ✅ Options flow | Tradier chain data working |
| ✅ Webull login | Token extracted from browser |
| ✅ Webull API | Account ID found ($100k paper) |
| ✅ Discord | Bot online as GilbertTrader#0929 |
| ✅ AI PC | Risk manager reachable and responding |
| ✅ Dashboard | trade_log.csv readable |
| ✅ Full pipeline | Signal→parse→news→regime→VWAP flow works |

---

## Paper → Live Checklist

Before switching to real money:

- [ ] 30+ paper trades completed with positive expectancy
- [ ] Win rate > 40% over 30+ trades
- [ ] Average win > average loss
- [ ] `full_system_test.py` passes 31/31
- [ ] Tradier token confirmed working (real-time data streaming)
- [ ] Circuit breaker tested (confirm it stops after daily loss limit)
- [ ] Discord commands tested (`!close ALL` works correctly)

To switch to live trading, change **one line** in `main.py`:

```python
# Line 178 — change this:
webull = WebullEngine(paper=True)

# To this:
webull = WebullEngine(paper=False)
```

---

*Built by Eyob Kabeto — GilbertTrader v2.0*