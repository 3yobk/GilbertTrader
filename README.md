# GilbertTrader v2.3

> Fully automated AI-powered options day trading bot. Scans markets, reads Discord signals, verifies trades through an AI risk manager, executes on Webull, monitors P&L intraday, and alerts you by email and SMS — all from one command.

**Built by Eyob Kabeto · Georgia Gwinnett College**  
**Live since May 20, 2026 · +$153 in first 2 days (+15.2%)**

---

## What It Does

GilbertTrader runs continuously and handles the entire trade lifecycle automatically:

```
Discord signal  ──┐
                  ├──► Signal Parser ──► AI Risk Manager ──► Webull API ──► P&L Monitor
Market Scanner ──┘                                                               │
                                                               ┌─────────────────┤
                                                               │  Auto-close at TP/SL
                                                               │  EOD forced close 3:55 PM
                                                               │  Trailing TP / Scale-out
                                                               └──► Discord + Email + SMS
```

1. **Scans** 25+ tickers every 2–4 minutes using 8 gates: RSI, volume, SMA-200, hourly RSI, sector, news, options flow, GEX, IV rank, and VWAP
2. **Dual-scans** both CALL and PUT opportunities per ticker simultaneously — regime adjusts conviction, not direction
3. **Routes** to 0DTE options before 2 PM for maximum gamma on intraday moves
4. **Verifies** every trade through your local AI PC risk manager with real stock price validation
5. **Executes** directly via the Webull API — no browser clicking
6. **Monitors** positions with adaptive polling (5s near TP/SL, 15s otherwise)
7. **Closes** all positions by 3:55 PM ET — never holds overnight
8. **Protects** with circuit breaker, streak pause, session re-entry block, and max 4 trades/day
9. **Reports** a daily summary to Discord and email at 4:15 PM ET

---

## Quick Start

```powershell
cd "C:\Users\EyobK\OneDrive - Georgia Gwinnett College\Desktop\WebullTrader"
python watchdog.py
```

On startup:
1. Select Profile: **Custom** → TP 30%, SL 20%, RSI 30, Budget $500
2. Log in to Webull in the browser window when prompted
3. Select Mode: **3 (Hybrid)** — scanner + Discord listener
4. Pre-market gap scan fires at 9:00 AM → Alpha Sieve at 9:15 AM → live scanning at 9:30 AM

---

## Architecture

```
watchdog.py  (always run this — auto-restarts on crash)
└── main.py  (orchestrator)
    ├── WebullEngine      — Chromium login, API orders, account cache
    ├── TradeEngine       — signal parsing, P&L monitor, trailing TP
    ├── MarketFeed        — Tradier WebSocket + yfinance fallback
    ├── DiscordListener   — signals in, alerts out, !commands
    ├── research_scanner  — 8-gate dual scanner + all filter logic
    └── api_server        — Flask REST → React dashboard (port 8000)

Dashboard: http://localhost:8080  (also on Tailscale for mobile)
API:       http://localhost:8000
```

---

## Scanner — 8 Gates

Every ticker is evaluated independently for CALLS and PUTS in the same scan cycle. Regime (bull/bear/neutral) adjusts conviction scoring, not direction.

| Gate | Check | Call threshold | Put threshold |
|------|-------|---------------|---------------|
| 1 | RSI (5-min) | < 30 oversold | > 70 overbought |
| 1a | RSI upturn confirmation | Rising + higher low | Not required |
| 1b | SPY intraday change | SPY not down > 0.5% | — |
| 1c | Earnings calendar | No earnings within 48h | No earnings within 48h |
| 2 | SMA-200 trend | Above SMA-200 (or RSI < 20 override) | Below SMA-200 = ideal; above needs RSI > 80 |
| 3 | Volume surge | > 30% of 20-period avg | > 30% of 20-period avg |
| 4 | 1-hour RSI | > 40 (not in hourly freefall) | < 60 (actually weakening hourly); skipped before 10 AM |
| 4b | 15-min RSI | Informational only | Informational only |
| 4c | Sector correlation | Sector not down > 0.8% | Sector not up > 0.8% (falling sector = tailwind) |
| 5 | News filter | Score ≥ 40/100 | Score ≤ 70/100 (bad news = put tailwind) |
| 6 | Options flow + PCR | Score ≥ 35, PCR < 1.0 | Unusual put flow or PCR ≥ 0.6 |
| 6b | IV Rank | ≤ 60 (fair premium) | ≤ 60 (fair premium) |
| 7 | GEX (Gamma Exposure) | Net gamma < −$500k | Net gamma < −$500k |
| 8 | Bid/ask spread | < 15% | < 15% |
| VWAP | Entry timing | Price above VWAP (bounce) | Price below VWAP (rejection) |

**IV Rank** uses real Tradier implied volatility as primary source, 30-day realized vol as fallback.

**Auto regime switching:**
- Bull (SPY > SMA20) → Call conviction +15 pts / Put conviction −15 pts
- Bear (SPY < SMA20) → Put conviction +15 pts / Call conviction −15 pts

---

## Trade Engine — Exit Logic

### Trailing TP
Activates at +30%. Dynamic pullback based on peak:
- Peak +30–50% → trail **12%** below peak
- Peak +50–100% → trail **18%** below peak
- Peak +100%+ → trail **25%** below peak

### Scale-out
Sells 50% at +20% to lock partial gains. Remaining 50% continues with trailing TP.

### Smart Confirmation Stop
When stop (−20%) triggers, bot checks before closing:
1. 1-min RSI turning up → hold
2. Price recovering above trigger → hold
3. RSI < 20 (extreme oversold) → hold up to 10 min
4. Swing low broken → close immediately
5. 10 min elapsed with no recovery → close

### EOD Forced Close
All open positions closed at **3:55 PM ET** with a Discord alert. Never holds same-week expiry overnight.

### Other Exits
- **Breakeven guard** — SL moves to 0% after +10% gain
- **Time decay** — closes stagnant positions after 30 min if P&L < +2% (only after 10:30 AM ET)
- **Adaptive SL** — options ≤$1.00 get −35% SL, ≤$2.00 get −25% SL

---

## Expiry Routing (0DTE Day Trading)

| Day / Time | Expiry |
|-----------|--------|
| Mon–Wed before 2 PM ET | Same-day 0DTE |
| Mon–Wed after 2 PM ET | This Friday |
| Thursday | Next Friday (theta protection) |
| Friday | Same-day 0DTE |

---

## Risk Controls

| Control | Setting |
|---------|---------|
| Max daily loss | −15% of account (~$173 at $1,152) |
| Max trades/day | 4 confirmed entries |
| Max concurrent positions | 2 (or deployed capital ≥ 2× budget) |
| Streak pause | Auto-pause after 2 consecutive losses |
| Session re-entry block | Stopped-out ticker blocked all day |
| Circuit breaker | Closes all and stops on daily loss limit |

**Streak scaling:** 3 wins → +10% budget | 2 losses → −20% budget

---

## Current Profile (DO NOT CHANGE until $2,000)

```
TP:     +30% (trailing, not hard close)
SL:     −20% (smart confirmation, not instant)
RSI:    < 30
Budget: $500 per trade (max 50% of settled cash)
```

---

## Discord Commands

| Command | Effect |
|---------|--------|
| `!status` | Open positions + session P&L |
| `!scan` | Force immediate scan |
| `!pause` / `!resume` | Pause/resume new entries |
| `!close ALL` | Emergency close all positions |
| `!sl off\|on\|<pct>` | Stop loss control |
| `!tp off\|on\|<pct>` | Take profit control |
| `!scaleout off\|on` | Scale-out control |
| `!limiters off\|on` | Disable/restore ALL exit rules |
| `!settings` | Show current TP/SL/scale-out |
| `!add TICKER` / `!remove TICKER` | Edit watchlist |
| `!help` | List all commands |

All commands also work from the **Controls tab** in the React dashboard.

---

## Automation

| Time (ET) | Event |
|-----------|-------|
| 9:00 AM | Pre-market gap-up/gap-down scan → Discord |
| 9:15 AM | Alpha Sieve — top 5 conviction tickers for the day |
| 9:30 AM | Live scanning begins (2-min intervals at open) |
| 10:30 AM | Scan interval widens to 4 min |
| 11:45–1:30 PM | Lunch chop — scanner pauses |
| 3:00 PM | Power hour — scan interval returns to 2 min |
| 3:55 PM | EOD forced close — all positions closed |
| 4:15 PM | Daily summary → Discord + email |

---

## File Reference

| File | Purpose |
|------|---------|
| `watchdog.py` | **Run this** — auto-restarts main.py on crash |
| `main.py` | Orchestrator — startup, main loop, Discord commands |
| `api_server.py` | Flask REST API → React dashboard |
| `trade_engine.py` | P&L monitor, trailing TP, smart confirmation stop |
| `webull_engine.py` | Chromium login, order placement, OTOCO brackets |
| `research_scanner.py` | 8-gate dual scanner, IV rank, VWAP gate |
| `market_feed.py` | Tradier WebSocket + yfinance fallback |
| `discord_listener.py` | Discord bot, command routing, signal detection |
| `news_filter.py` | Direction-aware news gate (bad news = put tailwind) |
| `signal_parser.py` | Parse Discord option signals into structured orders |
| `vwap_entry.py` | Intraday VWAP calculation and bounce detection |
| `alerts.py` | Email + SMS alerts (big winners, circuit breaker, etc.) |
| `test_gilberttrader.py` | **81-test suite** — run after any code change |
| `paper_trade_test.py` | Market-hours paper order verification |
| `test_mode.py` | Dry-run a specific signal (no order sent) |

**Delete these** (obsolete):
- `dashboard.py` — old Streamlit dashboard, replaced by React
- `full_system_test.py` — replaced by `test_gilberttrader.py`
- `*.bak`, `*.bak2`, `*.bak3` files — editing backups

---

## Configuration (.env)

```env
LIVE_TRADING_ENABLED=true
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=1393749056672501874          # #gilbert-chat
DISCORD_SIGNAL_CHANNEL_ID=1506715066895171614    # #option-trades (subscribers)
AI_PC_URL=http://100.91.139.29:5000/ask
AI_PC_KEY=sk-8656a75a
TRADIER_TOKEN=...
WEBULL_EMAIL=eyobdesie1@gmail.com
WEBULL_TRADING_PIN=522386
```

Optional alert channels:
```env
ALERT_EMAIL_FROM=yourbot@gmail.com
ALERT_EMAIL_TO=you@gmail.com
ALERT_EMAIL_PASSWORD=your_app_password
TWILIO_ACCOUNT_SID=ACxxxxxxx
TWILIO_AUTH_TOKEN=your_token
TWILIO_FROM=+15551234567
TWILIO_TO=+15559876543
```

---

## Account Growth Targets

| Account | Budget/trade | Expected win (+30%) | Weekly (3 wins) |
|---------|-------------|--------------------|--------------| 
| $1,152 (now) | $500 | ~$150 | ~$450 |
| $2,000 | $750 | ~$225 | ~$675 |
| $5,000 | $1,500 | ~$450 | ~$1,350 |
| $10,000 | $3,000 | ~$900 | ~$2,700 |

**Don't change settings until $2,000.** Strategy works — +15.2% in 2 days.

---

## Position Recovery After Restart

If the bot crashes with an open position, resume monitoring manually:

```powershell
'{"TICKER_STRIKE_EXPIRY": {"ticker": "NVDA", "strike": 220.0, "option_type": "C", "expiry": "2026-05-30", "quantity": 1, "entry_price": 2.42, "paper": false, "scaled_out": false, "highest_pnl": 0.0}}' | Out-File .\open_positions.json -Encoding utf8
```

Then restart `python watchdog.py` — `_load_positions()` resumes monitoring automatically.

---

## Testing

Run the full test suite after any code change:

```powershell
python test_gilberttrader.py
python test_gilberttrader.py -v   # verbose — shows pass details
```

The suite covers 81 tests across 26 sections [A]–[Z]:

- **Logic simulations** — boundary cases for every gate, exit rule, and timing decision (no network needed)
- **Source code audits** — verifies upgrades are present, dead code is gone, signatures match
- **Integration tests** — signal parser, trade engine, alerts, position persistence

All 81 tests pass on a clean install. Tests skip gracefully when optional dependencies (pytz, rich) aren't installed.

---

## Known Issues

| Issue | Status |
|-------|--------|
| OTOCO brackets | Webull paper endpoint returns 404 — Python monitor covers exits |
| After-hours day P&L | Shows $0 after 4 PM — correct, Webull doesn't serve real-time P&L after close |
| OpEx Fridays | High GEX, few/no signals — normal, wait for Monday |
| T+1 settlement | Cash account funds settle next day — bot warns on unsettled cash |

---

*GilbertTrader v2.3 · Eyob Kabeto · Georgia Gwinnett College*  
*Live since May 20, 2026 · Upgraded May 24, 2026*