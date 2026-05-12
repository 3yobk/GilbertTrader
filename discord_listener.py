"""
discord_listener.py — Upgraded with Full Command System
=========================================================
UPGRADES:
  1. Discord COMMANDS — type commands in Discord to control the bot live
     !status       → current positions + P&L
     !scan         → force an immediate scan
     !pause        → pause trading (no new entries)
     !resume       → resume trading
     !close ALL    → emergency close all positions
     !profile risky → switch risk profile live
     !watchlist    → show current scan tickers
     !add TICKER   → add ticker to watchlist
     !remove TICKER→ remove ticker from watchlist
     !help         → list all commands
  2. Message queue so no alerts get dropped under load
  3. Duplicate signal filter (same signal within 5 min = ignored)
  4. Signal confidence shown in Discord message
"""

import re
import time
import threading
import logging
import asyncio
import queue
from typing import Callable, Optional
from datetime import datetime

log = logging.getLogger("discord_listener")

SIGNAL_PATTERNS = [
    r"\b(BTO|STC|BTC|STO)\b",
    r"\b[A-Z]{2,5}\s+\d+[CP]\b",
    r"\b\d+[CP]\s+\d+/\d+\b",
]
_SIGNAL_RE = re.compile("|".join(SIGNAL_PATTERNS), re.IGNORECASE)
_CMD_RE    = re.compile(r"^!(status|scan|pause|resume|close|profile|watchlist|add|remove|help)\b",
                        re.IGNORECASE)


class DiscordListener:
    def __init__(self, channel_id: int, token: str,
                 on_signal: Callable[[str], None] = None,
                 on_command: Callable[[str, str], None] = None):
        self.channel_id  = channel_id
        self.token       = token
        self.on_signal   = on_signal
        self.on_command  = on_command   # callback(command, args)
        self._client     = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest_raw_message = None

        # Upgrade 2: message queue so no alerts dropped
        self._send_queue: queue.Queue = queue.Queue()

        # Upgrade 3: duplicate signal filter
        self._recent_signals: dict = {}   # signal_text -> timestamp

        # Upgrade 1: pause/resume state
        self.paused = False

    def get_latest_signal(self):
        msg = self._latest_raw_message
        self._latest_raw_message = None
        return msg

    def send_status_update(self, message: str):
        """Thread-safe queued send — never drops messages."""
        self._send_queue.put(message)
        if self._client and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._client.drain_queue(),
                self._loop
            )

    def start_in_thread(self):
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="DiscordListener")
        self._thread.start()
        time.sleep(2)
        log.info("Discord listener thread started (channel=%d)", self.channel_id)

    def stop(self):
        if self._client and self._loop:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def _run_loop(self):
        try:
            import discord
        except ImportError:
            log.error("discord.py not installed.")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = _SignalClient(
            channel_id=self.channel_id, parent=self, intents=intents)
        try:
            self._loop.run_until_complete(self._client.start(self.token))
        except Exception as exc:
            log.error("Discord error: %s", exc)
        finally:
            self._loop.close()

    def _is_duplicate(self, text: str) -> bool:
        """Upgrade 3: block same signal repeated within 5 minutes."""
        key = re.sub(r"\s+", " ", text.upper().strip())[:60]
        now = time.time()
        if key in self._recent_signals:
            if now - self._recent_signals[key] < 300:
                return True
        self._recent_signals[key] = now
        # Cleanup old entries
        self._recent_signals = {k: v for k, v in self._recent_signals.items()
                                if now - v < 600}
        return False


class _SignalClient:
    def __new__(cls, channel_id, parent, intents):
        import discord

        class _Inner(discord.Client):
            def __init__(self_inner):
                super().__init__(intents=intents)
                self_inner.target_channel_id = channel_id
                self_inner.parent = parent

            async def drain_queue(self_inner):
                """Send all queued messages."""
                channel = self_inner.get_channel(self_inner.target_channel_id)
                if not channel:
                    return
                while not self_inner.parent._send_queue.empty():
                    try:
                        msg = self_inner.parent._send_queue.get_nowait()
                        # Split long messages to stay under Discord 2000 char limit
                        for chunk in [msg[i:i+1990] for i in range(0, len(msg), 1990)]:
                            await channel.send(chunk)
                    except Exception as e:
                        log.error("Discord send error: %s", e)

            async def send_alert(self_inner, message: str):
                channel = self_inner.get_channel(self_inner.target_channel_id)
                if channel:
                    try:
                        await channel.send(message)
                    except Exception as e:
                        log.error("Discord send_alert error: %s", e)

            async def on_ready(self_inner):
                log.info("Discord connected as %s", self_inner.user)
                await self_inner.send_alert(
                    "🤖 **GilbertTrader Online** | Type `!help` for commands")

            async def on_message(self_inner, message):
                if message.author == self_inner.user:
                    return
                if message.channel.id != self_inner.target_channel_id:
                    return

                raw = message.content.strip()
                if not raw:
                    return

                # ── Upgrade 1: Command handler ─────────────────────────────
                if _CMD_RE.match(raw):
                    await self_inner.handle_command(raw)
                    return

                # ── Signal detection ───────────────────────────────────────
                if _SIGNAL_RE.search(raw):
                    # Upgrade 3: duplicate check
                    if self_inner.parent._is_duplicate(raw):
                        log.info("Duplicate signal ignored: %s", raw[:40])
                        await self_inner.send_alert(
                            f"⚠️ Duplicate signal ignored: `{raw[:60]}`")
                        return

                    # Paused check
                    if self_inner.parent.paused:
                        await self_inner.send_alert(
                            "⏸️ Bot is PAUSED — signal ignored. Type `!resume` to enable.")
                        return

                    log.info("Signal captured: %s", raw[:50])
                    self_inner.parent._latest_raw_message = raw
                    if self_inner.parent.on_signal:
                        self_inner.parent.on_signal(raw)

            # ── Upgrade 1: Command implementations ────────────────────────
            async def handle_command(self_inner, raw: str):
                parts   = raw.strip().split()
                cmd     = parts[0].lower().lstrip("!")
                args    = " ".join(parts[1:]) if len(parts) > 1 else ""
                channel = self_inner.get_channel(self_inner.target_channel_id)

                if cmd == "help":
                    await channel.send(
                        "**GilbertTrader Commands**\n"
                        "`!status`         — show open positions + P&L\n"
                        "`!scan`           — force immediate market scan\n"
                        "`!pause`          — stop new entries\n"
                        "`!resume`         — allow new entries\n"
                        "`!close ALL`      — emergency close all positions\n"
                        "`!profile safe|normal|risky` — switch risk level\n"
                        "`!watchlist`      — show current scan tickers\n"
                        "`!add TICKER`     — add to watchlist\n"
                        "`!remove TICKER`  — remove from watchlist\n"
                        "`!help`           — this menu"
                    )

                elif cmd == "pause":
                    self_inner.parent.paused = True
                    await channel.send("⏸️ **Trading PAUSED** — no new entries will be made.")

                elif cmd == "resume":
                    self_inner.parent.paused = False
                    await channel.send("▶️ **Trading RESUMED** — bot is active.")

                elif cmd == "status":
                    # Delegate to on_command callback in main.py
                    if self_inner.parent.on_command:
                        self_inner.parent.on_command("status", args)
                    else:
                        await channel.send("Status: bot running. No position data available.")

                else:
                    # Delegate all other commands to main.py
                    if self_inner.parent.on_command:
                        self_inner.parent.on_command(cmd, args)
                    else:
                        await channel.send(f"Command `{cmd}` received.")

        return _Inner()