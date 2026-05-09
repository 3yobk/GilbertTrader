"""
discord_listener.py — Fixed Signal Bridge
"""

import re
import time
import threading
import logging
import asyncio
from typing import Callable, Optional

log = logging.getLogger("discord_listener")

SIGNAL_PATTERNS = [
    r"\b(BTO|STC|BTC|STO)\b", # Action
    r"\b[A-Z]{2,5}\s+\d+[CP]\b", # Ticker + Strike
    r"\b\d+[CP]\s+\d+/\d+\b", # Strike + Expiry
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
        self._latest_raw_message = None

    def get_latest_signal(self):
        """Returns the message and wipes the buffer to prevent double-processing."""
        msg = self._latest_raw_message
        self._latest_raw_message = None
        return msg

    def start_in_thread(self):
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="DiscordListener",
        )
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

        # We pass 'self' into the client so the client can update this class directly
        self._client = _SignalClient(
            channel_id = self.channel_id,
            parent     = self, # Pass the DiscordListener instance
            intents    = intents,
        )

        try:
            self._loop.run_until_complete(self._client.start(self.token))
        except Exception as exc:
            log.error("Discord error: %s", exc)
        finally:
            self._loop.close()

class _SignalClient:
    def __new__(cls, channel_id, parent, intents):
        import discord
        class _Inner(discord.Client):
            def __init__(self_inner):
                super().__init__(intents=intents)
                self_inner.target_channel_id = channel_id
                self_inner.parent = parent # Store reference to the Listener class

            async def on_ready(self_inner):
                log.info("Discord connected as %s", self_inner.user)
                channel = self_inner.get_channel(self_inner.target_channel_id)
                if channel:
                    log.info("Listening to #%s", channel.name)
                else:
                    log.warning("Channel %d not found!", self_inner.target_channel_id)

            async def on_message(self_inner, message):
                if message.author == self_inner.user:
                    return
                if message.channel.id != self_inner.target_channel_id:
                    return

                raw = message.content.strip()
                if not raw:
                    return

                # --- THE CRITICAL FIX ---
                if _SIGNAL_RE.search(raw):
                    log.info("Signal captured: %s", raw[:50])
                    # Update the variable that main.py is actually checking!
                    self_inner.parent._latest_raw_message = raw
                    # Also trigger the callback if main.py used one
                    if self_inner.parent.on_signal:
                        self_inner.parent.on_signal(raw)

        return _Inner()