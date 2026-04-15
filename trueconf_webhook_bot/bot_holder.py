"""Reference container for the current `Bot` instance plus in-flight accounting.

HTTP handlers and bot commands access the bot via `holder.acquire()` — an async
context manager that:

- yields the active `Bot` instance (or raises if it isn't initialized yet);
- increments the in-flight counter. Before hot-swapping the token, the
  supervisor waits for the counter to drain back to zero so that live
  `bot.send_*` calls are not interrupted when the WebSocket is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from trueconf import Bot


class BotHolder:
    """Single-slot container for `Bot` with in-flight accounting."""

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._in_flight: int = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def set(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def bot(self) -> Bot:
        """Return the current instance. Raises `RuntimeError` if it hasn't been set yet."""
        if self._bot is None:
            raise RuntimeError("Bot is not initialized yet")
        return self._bot

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Bot]:
        """Lease the bot for a single operation (`bot.send_*`, `create_personal_chat`, ...).

        While a lease is active, the supervisor will not close the WebSocket
        under a hot-swap: it waits for `wait_idle()` (with a timeout) before
        forcing a reconnect.
        """
        if self._bot is None:
            raise RuntimeError("Bot is not initialized yet")
        self._in_flight += 1
        self._idle.clear()
        try:
            yield self._bot
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def wait_idle(self, timeout: float) -> bool:
        """Wait for all leases to finish, or until the timeout expires.

        Returns True if the wait completed; False if the timeout fired (in
        which case the supervisor proceeds with the refresh anyway, tolerating
        a possible 502 on the remaining in-flight requests).
        """
        if self._in_flight == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
