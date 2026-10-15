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
     !sl off/on/<pct> → disable/enable/set stop loss
     !tp off/on/<pct> → disable/enable/set take profit
     !scaleout off/on → disable/enable scale-out
     !limiters off/on → disable/restore ALL exit rules
     !settings     → show current TP/SL/scale-out values
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

# All recognized commands — add new ones here
_CMD_RE = re.compile(
    r"^!(status|scan|pause|resume|close|profile|watchlist|add|remove|help"
    r"|sl|tp|scaleout|limiters|settings)\b",
    re.IGNORECASE
)


class DiscordListener:
    def __init__(self, channel_id: int, token: str,
                 on_signal: Callable[[str], None] = None,
                 on_command: Callable[[str, str], None] = None,
                 signal_channel_id: int = None):
        self.channel_id        = channel_id
        self.signal_channel_id = signal_channel_id
        self.token      = token
        self.on_signal  = on_signal
        self.on_command = on_command
        self._client    = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest_raw_message = None
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

    def send_signal(self, message: str):
        """
        Send an AI-approved trade signal to the #option-trades channel.
        Falls back to the main channel if signal_channel_id is not set.
        """
        if self.signal_channel_id and self._client and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._client.send_to_channel(self.signal_channel_id, message),
                self._loop
            )
        else:
            self.send_status_update(message)

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
        key = re.sub(r"\s+", " ", text.upper().strip())[:60]
        now = time.time()
        if key in self._recent_signals:
            if now - self._recent_signals[key] < 300:
                return True
        self._recent_signals[key] = now
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
                        for chunk in [msg[i:i+1990] for i in range(0, len(msg), 1990)]:
                            await channel.send(chunk)
                    except Exception as e:
                        log.error("Discord send error: %s", e)

            async def send_to_channel(self_inner, channel_id: int, message: str):
                try:
                    ch = self_inner.get_channel(channel_id)
                    if not ch:
                        ch = await self_inner.fetch_channel(channel_id)
                    if ch:
                        for chunk in [message[i:i+1990] for i in range(0, len(message), 1990)]:
                            await ch.send(chunk)
                except Exception as e:
                    log.error("Discord send_to_channel error: %s", e)

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
                    if self_inner.parent.paused:
                        await self_inner.send_alert(
                            "⏸️ Bot is PAUSED — signal ignored. Type `!resume` to enable.")
                        return
                    log.info("Signal captured: %s", raw[:50])
                    self_inner.parent._latest_raw_message = raw
                    if self_inner.parent.on_signal:
                        self_inner.parent.on_signal(raw)

            async def handle_command(self_inner, raw: str):
                parts   = raw.strip().split()
                cmd     = parts[0].lower().lstrip("!")
                args    = " ".join(parts[1:]) if len(parts) > 1 else ""
                channel = self_inner.get_channel(self_inner.target_channel_id)

                if cmd == "help":
                    await channel.send(
                        "**GilbertTrader Commands**\n"
                        "`!status`                   — open positions + P&L\n"
                        "`!scan`                     — force immediate scan\n"
                        "`!pause` / `!resume`        — pause/resume new entries\n"
                        "`!close ALL`                — emergency close all\n"
                        "`!profile safe|normal|risky` — switch risk profile\n"
                        "`!watchlist`                — show scan tickers\n"
                        "`!add TICKER` / `!remove TICKER` — edit watchlist\n"
                        "────────────────────────────\n"
                        "`!powerhour off|on`         — stop loss control\n"
                        "`!sl off|on|<pct>`          — stop loss control\n"
                        "`!tp off|on|<pct>`          — take profit control\n"
                        "`!scaleout off|on`          — scale-out control\n"
                        "`!limiters off|on`          — disable/restore ALL exits\n"
                        "`!settings`                 — show current values"
                    )

                elif cmd == "pause":
                    self_inner.parent.paused = True
                    await channel.send("⏸️ **Trading PAUSED** — no new entries.")

                elif cmd == "resume":
                    self_inner.parent.paused = False
                    await channel.send("▶️ **Trading RESUMED** — bot is active.")

                elif cmd in ("status", "sl", "tp", "scaleout", "limiters",
                             "settings", "scan", "close", "profile",
                             "watchlist", "add", "remove"):
                    # Delegate to main.py on_command in thread pool
                    # (avoids blocking Discord heartbeat on slow commands)
                    if self_inner.parent.on_command:
                        import concurrent.futures
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            self_inner.parent.on_command,
                            cmd, args
                        )
                    else:
                        await channel.send(f"Command `{cmd}` received.")

                else:
                    await channel.send(f"Unknown command `!{cmd}`. Type `!help`.")

        return _Inner()