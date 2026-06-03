# GilbertTrader v2.4

> Fully automated AI-powered options day trading bot. Scans markets, reads Discord signals, verifies trades through an AI risk manager, executes on Webull, monitors P&L intraday, and alerts you by email and SMS — all from one command.

**Built by Eyob Kabeto · Georgia Gwinnett College**  
**Live since May 20, 2026 · +$153 in first 2 days (+15.2%)**  
**v2.4 — June 3, 2026 — 35 improvements, 4 new adaptive learning systems**

---

## What It Does

GilbertTrader runs continuously and handles the entire trade lifecycle automatically:

```
Discord signal  ──┐
                  ├──► Signal Parser ──► ML Sieve ──► AI Risk Manager ──► Webull API ──► P&L Monitor
Market Scanner ──┘                                                                            │
                                                                         ┌────────────────────┤
                                                                         │  Auto-close at TP/SL
                                                                         │  EOD forced close 3:55 PM
                                                                         │  Trailing TP / Scale-out
                                                                         └──► Discord + Email + SMS
                                                                                      │
                                                               Friday 4:30 PM ────────┘
                                                               Reinforce Brain + TP Sieve
                                                               ► adaptive_bounds.json
                                                               ► Monday auto-applies new thresholds
```

1. **Scans** 39 tickers every 2–4 minutes using 8 gates + 7 smart overrides
2. **Dual-scans** both CALL and PUT opportunities per ticker simultaneously
3. **Verifies** contracts exist on Webull before routing — no more ghost expiry rejections
4. **Targets** strikes by live Delta (Δ 0.40–0.52) instead of static price brackets
5. **Screens** through ML win-probability gate before AI risk manager (activates at 30 fills)
6. **Executes** directly via Webull API — no browser clicking
7. **Monitors** positions with adaptive polling (5s near TP/SL, 15s otherwise)
8. **Closes** all positions by 3:55 PM ET — never holds overnight
9. **Learns** every Friday: reinforce_brain adjusts RSI/TP/SL thresholds automatically

---

## Quick Start

```powershell
cd "C:\Users\EyobK\OneDrive - Georgia Gwinnett College\Desktop\WebullTrader"
python watchdog.py
```

On startup:
1. Select Profile: **Safe** (account at $533 — rebuild first)
2. Log in to Webull in the browser window when prompted
3. Select Mode: **3 (Hybrid)** — scanner + Discord listener
4. **Start by 9:25 AM** — first scan fires at 9:35 AM (5-min chop zone skip)

---

## Architecture

```
watchdog.py  (always run this — auto-restarts on crash)
└── main.py  (orchestrator — thread-safe startup, pacing sleep)
    ├── WebullEngine      — Chromium login, API orders, auto expiry fallback
    ├── TradeEngine       — signal parsing, P&L monitor, trailing TP
    ├── MarketFeed        — Tradier WebSocket + yfinance fallback
    ├── DiscordListener   — signals in, alerts out, !commands
    ├── research_scanner  — 8-gate dual scanner + all filter logic
    ├── ml_sieve          — Logistic Regression win-probability gate
    ├── reinforce_brain   — Friday gradient parameter optimizer
    └── api_server        — Flask REST → React dashboard (port 8000)

Dashboard: http://localhost:8080  (also on Tailscale for mobile)
API:       http://localhost:8000
```

---

## Scanner — 8 Gates + 7 Smart Overrides

Every ticker is evaluated independently for CALLS and PUTS. Regime adjusts conviction, not direction.

### Base Gates

| Gate | Check | Call threshold | Put threshold |
|------|-------|---------------|---------------|
| 1 | RSI (5-min) | < 25–35 (profile) | > 65–75 |
| 1a | RSI upturn | ≤15: bypass · 15–20: 2-bar · >20: 3-bar+higher low | Not required |
| 1b | SPY intraday | SPY not down > 0.5% | — |
| 1c | Earnings | No earnings within 48h | No earnings within 48h |
| 2 | SMA-200 | Above SMA-200 (or RSI<20 override) | Below ideal; above needs RSI>80 |
| 3 | Volume | > 30% of 20-period avg | > 30% of 20-period avg |
| 4 | 1-hr RSI | > 40 (not in hourly freefall) · skipped before 10 AM · bypassed at RSI≤20 | < 60 · skipped before 10 AM |
| 4b | 15-min RSI | Informational only | Informational only |
| 4c | Sector | Not down > 0.8% (RSI<15: up to −1.5% allowed) | Not up > 0.8% |
| 5 | News | Score ≥ 40/100 | Score ≤ 70/100 |
| 6 | Flow + PCR | Score ≥ 35 · PCR scaled by RSI (see below) | Unusual put flow or PCR ≥ 0.6 |
| 6b | IV Rank | ≤ 60 | ≤ 60 |
| 7 | GEX | Net gamma < −$500k (3 smart overrides below) | Net gamma < −$500k |
| 8 | Spread | < 15% bid/ask | < 15% bid/ask |
| VWAP | Entry timing | Above VWAP (or RSI<20 override — below VWAP expected at capitulation) | Below VWAP |

### PCR Scaling (Flow Gate)
| RSI | PCR Limit | Reason |
|-----|-----------|--------|
| ≤ 15 | < 2.0 | Panic — institutional put buying is hedging |
| 15–20 | < 1.5 | Very oversold — some put hedging normal |
| 20–25 | < 1.2 | Oversold |
| > 25 | < 1.0 | Standard — must be bullish flow |

### GEX Smart Overrides (Gate 7)
Positive GEX normally blocks calls. Bypassed when ANY of:
1. **RSI ≤ 15 AND price below pin** — MM delta hedging pushes price toward pin (call tailwind)
2. **GEX declining ≥ 20% across 3+ scans** — pin is loosening (tracked in rolling 5-scan history)
3. **GEX < 0.5% of daily dollar volume** — pin is economically irrelevant (e.g. AMZN $1.27M GEX on $2B/day)

### Extreme RSI Overrides (all gates)
At RSI ≤ 15, the bot bypasses: upturn check, hourly RSI, sector drag up to −1.5%, VWAP position, GEX pin. These exist because at RSI 9–14 the stock IS expected to be in freefall — waiting for confirmation means missing the bounce.

---

## Data Node Coverage: 39/39

All 7 institutional data node categories now covered:

| Category | Nodes |
|----------|-------|
| Underlying | Spot price, RSI, VWAP, SMA-200, ATR, Volume, SPY regime |
| Option Chain | Strike (Delta-targeted), Expiry/DTE, Bid/Ask/Mid, Spread %, Bid/Ask depth |
| Liquidity | Daily volume, Open interest, Unusual volume ratio, PCR |
| Greeks | Delta (Δ), Gamma (GEX), Theta, Vega (Delta safety gate at Δ<0.05) |
| Volatility | IV (Tradier), HV (30-day realized), IV Rank, VIX |
| Microstructure | Net GEX, Pin level, GEX trend (5-scan), GEX/volume ratio |
| Calendar | Earnings (48h), FOMC/CPI/NFP 2026 dates (caps to 1 contract) |

---

## Delta-Targeted Strike Selection

Replaces static price brackets. Scans live Tradier chain for the strike whose Delta is closest to:
- **Normal IV (rank ≤ 60):** Δ 0.40–0.52 (near ATM, maximum gamma leverage)
- **High IV (rank > 60):** Δ 0.35–0.42 (slightly OTM, cheaper premium insulates budget)

Falls back to static price-bracket logic if chain unavailable.

---

## Contract Existence & Expiry Routing

**Webull expiry reroute:** Before building any signal, queries Webull's live options chain for available expiry dates. If RIOT only has `6/5` (Friday) on Webull, the signal is built as `6/5` from the start. No more `No option found for exact expiry` rejections.

**Tradier existence check:** Verifies the exact strike+expiry has a real market (bid AND ask > 0). Falls back: next Friday → Friday+7 → `NO_CONTRACT` skip.

**Expiry routing:**

| Day / Time | Expiry |
|-----------|--------|
| Mon–Wed before 2 PM ET | Same-day 0DTE |
| Mon–Wed after 2 PM ET | This Friday |
| Thursday | Next Friday (theta protection) |
| Friday | Same-day 0DTE |

---

## Adaptive Learning Systems (NEW in v2.4)

### Dynamic Slippage Cache
Tracks per-ticker execution friction from real fills using EWMA (α=0.2). Injected into Kelly sizing so the bot accounts for real fill costs, not just estimated mid-price. Injected into AI prompt context.

### ML Win-Probability Gate (`ml_sieve.py`)
Logistic Regression trained on `trade_log.csv`.  
Features: `entry_rsi`, `vol_ratio`, `flow_score`, `put_call_ratio`, `gex_significance`, `spy_vs_sma20`  
**Inactive until 30 fills.** Blocks if P(win) < 45%. Retrains after each confirmed fill. Model persisted to disk between sessions.

### Friday Reinforcement Optimizer (`reinforce_brain.py`)
Runs every Friday at 4:30 PM ET. Reads full trade history and computes:
- RSI threshold → shifted toward mean winning entry RSI (clamped 20–35)
- SL% → tightened to 15% if average losses exceed 25%
- TP% → raised to 35% if win rate > 55% with small losses

Writes `adaptive_bounds.json`. Monday morning startup reads the file and applies thresholds automatically.

### AI Prompt: Binary Output Jail
Replaced conversational prompt with strict binary utility format. Forces exactly:
- **Approved:** `BTO [TICKER] [STRIKE][C/P] [EXPIRY] x[QTY]`
- **Rejected:** `Rejected: [5-word reason]`

No markdown, no backticks, no explanations. Live slippage factor and ML win-probability injected into context so the AI can downsize contracts on low-confidence setups.

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

### Other Exits
- **EOD forced close** — all positions at 3:55 PM ET, never holds overnight
- **Breakeven guard** — SL moves to 0% after +10% gain
- **Time decay** — closes stagnant positions after 30 min if P&L < +2% (after 10:30 AM only)
- **Adaptive SL** — options ≤$1.00 get −35% SL, ≤$2.00 get −25% SL

---

## Risk Controls

| Control | Setting |
|---------|---------|
| Max daily loss | −15% of account |
| Max trades/day | 4 confirmed entries |
| Max concurrent positions | 2 |
| Streak pause | Auto-pause after 2 consecutive losses |
| Session re-entry block | Stopped-out ticker blocked all day |
| Circuit breaker | Closes all and stops on daily loss limit |
| Macro event cap | FOMC/CPI/NFP → max 1 contract |

**Streak scaling:** 3 wins → +10% budget · 2 losses → −20% budget

---

## Current Profile (account at $533 — use Safe until $800)

```
Profile: SAFE
TP:     +15% trailing
SL:     −10% smart confirmation
RSI:    < 25
Budget: $150 per trade
```

Switch to Normal at $800, Risky at $1,500.

---

## Automation Schedule

| Time (ET) | Event |
|-----------|-------|
| 9:00 AM | Pre-market gap-up/gap-down scan → Discord |
| 9:15 AM | Alpha Sieve — top 5 conviction tickers for the day |
| 9:35 AM | Live scanning begins (chop zone 9:30–9:35 skipped) |
| 10:30 AM | Scan interval widens to 4 min |
| 11:45–1:30 PM | Lunch chop — scanner pauses |
| 3:00 PM | Power hour — scan interval returns to 2 min |
| 3:55 PM | EOD forced close |
| 4:15 PM | Daily summary → Discord |
| Fri 4:30 PM | reinforce_brain + TP sieve → adaptive_bounds.json |

**Start the bot by 9:25 AM.** First scan at 9:35 catches the best opening dip RSI readings.

---

## Watchlist (39 tickers)

```python
# Mega-cap tech
"NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
# High-beta momentum
"AMD", "PLTR", "ARM", "AVGO", "MU", "SMCI",
# Fintech / growth
"SOFI", "HOOD", "COIN", "AFRM", "UPST", "HIMS",
# Retail / consumer
"SHOP", "CVNA", "RBLX",
# Leveraged ETFs + broad market
"SOXL", "TQQQ", "IWM", "SPY", "QQQ",
# Crypto proxy
"MARA", "RIOT",
# Cybersecurity / cloud
"CRWD", "PANW", "SNOW", "NET",
# Consumer platforms
"UBER", "DASH",
# Payments / streaming
"XYZ", "PYPL", "ZM", "SPOT",
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
| `!profile safe\|normal\|risky` | Switch risk profile |

---

## File Reference

| File | Purpose |
|------|---------|
| `watchdog.py` | **Run this** — auto-restarts main.py on crash |
| `main.py` | Orchestrator — thread-safe startup, ML gate, adaptive bounds |
| `research_scanner.py` | 8-gate dual scanner, Delta targeting, GEX overrides, contract check |
| `webull_engine.py` | Chromium login, orders, auto expiry fallback |
| `trade_engine.py` | P&L monitor, trailing TP, smart confirmation stop |
| `market_feed.py` | Tradier WebSocket + yfinance fallback |
| `api_server.py` | Flask REST API → React dashboard |
| `discord_listener.py` | Discord bot, command routing |
| `news_filter.py` | Direction-aware news gate |
| `signal_parser.py` | Parse Discord signals into structured orders |
| `vwap_entry.py` | Intraday VWAP calculation |
| `alerts.py` | Email + SMS alerts |
| `ml_sieve.py` | **NEW** — Logistic Regression win-probability gate |
| `reinforce_brain.py` | **NEW** — Friday gradient parameter optimizer |
| `adaptive_bounds.json` | Auto-generated — applied on startup |
| `ml_sieve_model.pkl` | Auto-generated — persisted ML model |
| `test_gilberttrader.py` | Test suite — run after any code change |

---

## Configuration (.env)

```env
LIVE_TRADING_ENABLED=true
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=1393749056672501874
DISCORD_SIGNAL_CHANNEL_ID=1506715066895171614
AI_PC_URL=http://100.91.139.29:5000/ask
AI_PC_KEY=sk-8656a75a
TRADIER_TOKEN=...
WEBULL_EMAIL=eyobdesie1@gmail.com
WEBULL_TRADING_PIN=522386
```

---

## Account Growth Targets

| Account | Budget/trade | Expected win (+30%) | Weekly (3 wins) |
|---------|-------------|--------------------|--------------| 
| $533 (now) | $150 (Safe) | ~$45 | ~$135 |
| $800 | $300 (Normal) | ~$90 | ~$270 |
| $1,500 | $500 (Normal) | ~$150 | ~$450 |
| $5,000 | $1,500 | ~$450 | ~$1,350 |

**Don't change from Safe until $800.**

---

## Known Issues

| Issue | Status |
|-------|--------|
| ML sieve | Inactive until 30 fills — needs live trading history |
| Monday GEX | First Monday of month → positive GEX everywhere (gamma repositioning) — wait for Tuesday |
| Start time | Must start by 9:25 AM — best RSI readings are in the 9:35–9:55 window |
| T+1 settlement | Cash account — bot warns on unsettled cash |

---

## v2.4 Changes (June 3, 2026)

**35 improvements across all files.** Key changes:

- Watchlist 27 → 39 tickers
- Gate 1a: 3-tier RSI upturn (extreme bypass, fast confirm, normal confirm)
- Gate 4/VWAP/Sector: bypassed at extreme RSI (≤15–20) instead of blocking
- Flow gate PCR: 4-tier scaling based on RSI severity
- GEX: 3 smart overrides (below pin, declining trend, negligible vs volume)
- Contract existence check + Webull expiry reroute (fixes ZM/RIOT ghost expiries)
- Delta-targeted strike selection from live chain
- Full Greeks output (Δ, Θ, V) per signal + bid/ask depth
- Macro event calendar (FOMC/CPI/NFP → 1 contract max)
- ML win-probability gate (`ml_sieve.py`)
- Friday reinforcement optimizer (`reinforce_brain.py`)
- Thread-safe startup (race condition fixed)
- AI prompt: binary output jail format
- Dynamic slippage EWMA cache

---

*GilbertTrader v2.4 · Eyob Kabeto · Georgia Gwinnett College*  
*Live since May 20, 2026 · v2.4 June 3, 2026*