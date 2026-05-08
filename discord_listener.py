"""
discord_listener.py  —  Paid Signal Channel Lurker

Connects to Discord as a bot (recommended) or user token,
listens to ONE specific channel, and fires on_signal(raw_text)
for every message that looks like a trade signal.

Discord setup (recommended — bot token, not user token):
  1. Go to https://discord.com/developers/applications
  2. Create an app → Bot → copy the token
  3. Under OAuth2 → URL Generator: scopes=bot, permissions=Read Messages
  4. Paste the invite URL in your browser to add the bot to the server
  5. The signal channel owner must give the bot Read Messages permission
"""

import re
import time
import threading
import logging
import asyncio
from typing import Callable, Optional

log = logging.getLogger("discord_listener")

# Patterns that suggest a message is a trade signal, not chat noise
SIGNAL_PATTERNS = [
    r"\b(BTO|STC|BTC|STO)\b",           # options action verbs
    r"\b[A-Z]{2,5}\s+\d+[CP]\b",        # e.g. "NVDA 110C"
    r"\b\d+[CP]\s+\d+/\d+\b",           # e.g. "110C 5/15"
    r"\b(CALL|PUT)\b",
    r"\b(SELL|BUY)\s+TO\s+(OPEN|CLOSE)\b",
    r"\bx\d+\b",                          # quantity e.g. "x3"
]
_SIGNAL_RE = re.compile("|".join(SIGNAL_PATTERNS), re.IGNORECASE)


class DiscordListener:
    def __init__(self, channel_id: int, token: str, on_signal: Callable[[str], None]):
        self.channel_id = channel_id
        self.token      = token
        self.on_signal  = on_signal
        self._client    = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # --- ADD THIS LINE ---
        self._latest_raw_message = None #

    # --- ADD THIS FUNCTION ---
    def get_latest_signal(self): #
        msg = self._latest_raw_message
        self._latest_raw_message = None # Clear it so we don't double-process
        return msg

    def start_in_thread(self):
        """Start the Discord event loop in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="DiscordListener",
        )
        self._thread.start()
        time.sleep(2)   # give the event loop a moment to connect
        log.info("Discord listener thread started (channel=%d)", self.channel_id)

    def stop(self):
        if self._client and self._loop:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def _run_loop(self):
        """Entry point for the daemon thread — owns its own event loop."""
        try:
            import discord
        except ImportError:
            log.error("discord.py not installed.  Run: pip install discord.py")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = _SignalClient(
            channel_id = self.channel_id,
            on_signal  = self.on_signal,
            intents    = intents,
        )

        try:
            self._loop.run_until_complete(self._client.start(self.token))
        except Exception as exc:
            log.error("Discord error: %s", exc)
        finally:
            self._loop.close()


class _SignalClient:
    """Internal discord.Client subclass."""

    def __new__(cls, channel_id, on_signal, intents):
        # Import here so the module loads even without discord installed
        import discord
        # Dynamically create the class that inherits from discord.Client
        class _Inner(discord.Client):
            def __init__(self_inner):
                super().__init__(intents=intents)
                self_inner.target_channel_id = channel_id
                self_inner._on_signal        = on_signal

            async def on_ready(self_inner):
                log.info("Discord connected as %s", self_inner.user)
                channel = self_inner.get_channel(self_inner.target_channel_id)
                if channel:
                    log.info("Listening to #%s", channel.name)
                else:
                    log.warning("Channel %d not found — check ID and bot permissions",
                                self_inner.target_channel_id)

            async def on_message(self_inner, message):
                if message.author == self_inner.user:
                    return
                if message.channel.id != self_inner.target_channel_id:
                    return

                raw = message.content.strip()
                if not raw:
                    return

                if _SIGNAL_RE.search(raw):
                    log.info("Signal captured from %s: %s", message.author.name, raw[:100])
                    # This triggers the 'on_signal' function we passed in main.py,
                    # which will save the message into self._latest_raw_message
                    self_inner._on_signal(raw)
                else:
                    log.debug("Ignored non-signal message: %s", raw[:60])

        instance = _Inner()
        return instance
