# AI Brain Trading System

Automated options trading bot that:
- Opens Webull in a real browser and waits for your manual login
- Listens to your paid Discord signal channel
- Has live market data for pre-trade checks and P&L monitoring
- Auto-closes positions at your profit target or stop-loss

---

## Setup (one time)

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Install the Chromium browser
playwright install chromium

# 3. Set your environment variables (or create a .env file)
export DISCORD_TOKEN="your-discord-bot-or-user-token"
export DISCORD_CHANNEL_ID="123456789012345678"
export POLYGON_API_KEY="your-polygon-key"   # optional but recommended
```

### Getting a Discord token

**Recommended (Bot token — no TOS risk):**
1. Go to https://discord.com/developers/applications
2. New Application → Bot → Reset Token → copy it
3. OAuth2 → URL Generator → scopes: `bot` → permissions: `Read Messages`
4. Paste the URL in your browser to invite the bot to the signal server
5. Ask the server admin to give the bot access to the signal channel

**Alternative (User token — against Discord TOS, risk of account ban):**
- Extract from browser DevTools → Network tab → any Discord API request → `Authorization` header

### Getting a Polygon API key (free)
1. Sign up at https://polygon.io
2. Free tier gives 15-minute delayed data
3. Starter plan ($29/mo) gives real-time WebSocket data

---

## Running

```bash
# Paper trading (safe — default)
python main.py

# Paper trading with custom profit/stop targets
python main.py --profit-target 20 --stop-loss 15

# REAL MONEY (will ask you to confirm twice)
python main.py --real
```

---

## What happens when you run it

```
Step 1/4  Chromium opens → Webull loads → you log in manually
Step 2/4  Market data feed connects (Polygon WS or yfinance fallback)
Step 3/4  Trade engine arms
Step 4/4  Discord listener connects to your channel
✅  System ARMED. Waiting for signals...
```

When a signal like `BTO NVDA 110C 5/15 x2` arrives:
1. The regex parser extracts: ticker=NVDA, strike=110, type=C, expiry=2025-05-15, qty=2, action=BTO
2. Market checks run: spread width, IV sanity check
3. Webull navigates to the options chain and places the order
4. A background thread monitors P&L every 15 seconds
5. When +15% is hit → auto STC (sell to close)
6. When -20% is hit → auto STC (stop-loss)

---

## File structure

```
main.py              ← run this
webull_engine.py     ← Playwright browser automation for Webull
discord_listener.py  ← lurks in your paid signal channel
market_feed.py       ← live data via Polygon WebSocket / yfinance fallback
trade_engine.py      ← parses signals, executes trades, monitors P&L
requirements.txt
webull_profile/      ← persistent Chromium profile (auto-created, keep logged in)
brain.log            ← full log file
```

---

## Updating Webull selectors

Webull updates their UI regularly. When something breaks, open `webull_engine.py`
and update the `SELECTORS` dict at the top. Use browser DevTools (F12) to find
the new element class names or data-testid attributes.

---

## Safety notes

- **Paper mode is the default.** Always test thoroughly in paper mode first.
- Real money mode requires you to type `CONFIRM REAL MONEY` at startup AND confirm each individual order.
- The bot will not place orders outside market hours (9:30am–4:00pm ET, Mon–Fri).
- Positions with a bid/ask spread over 5% of price are skipped automatically.
- Options with IV > 200% are skipped automatically.
