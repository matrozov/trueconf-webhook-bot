"""Long-running supervisor for Bot: token refresh + watchdog.

Strategy: hot-swap the token — write a new JWT into the private `_Bot__token`
field and force-close the WebSocket. The library's internal retry loop will
reconnect and re-authorize with the new token.

A single `Bot` instance lives for the whole process — only the token changes.
This prevents dual sessions, duplicate message handling and complex dispatcher
rewiring.

Watchdog: `bot.run()` is wrapped in a loop with exponential backoff. Failure
(expired token, network glitch) triggers a forced refresh and restart.

In ready-JWT mode (`config.uses_credentials == False`) refresh is disabled;
the supervisor only restarts `run()` on crashes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from trueconf import Bot
from trueconf.utils.token import get_auth_token

from .bot_holder import BotHolder
from .config import Config
from .utils import parse_jwt_exp

logger = logging.getLogger(__name__)

# JWT is stored as a name-mangled private attribute; if it ever disappears, we
# want to fail fast at startup rather than silently break a month later.
_TOKEN_ATTR: str = "_Bot__token"

# How many seconds before the JWT expiry to run the refresh.
_REFRESH_MARGIN_SECONDS: int = 5 * 24 * 3600

# How long to wait for in-flight sends to drain before force reconnect.
_DRAIN_BEFORE_RECONNECT_SEC: float = 2.0

# Refresh retry parameters on repeated failures.
_REFRESH_RETRY_MIN: float = 30.0
_REFRESH_RETRY_MAX: float = 600.0
_REFRESH_MAX_ATTEMPTS: int = 5

# Watchdog backoff on `bot.run()` crashes.
_RUN_BACKOFF_MIN: float = 1.0
_RUN_BACKOFF_MAX: float = 60.0

# Conservative fallback when we can't parse `exp` from the current token.
_REFRESH_FALLBACK_SECONDS: int = 25 * 24 * 3600


class BotSupervisor:
    """Runs the bot in a loop and refreshes the token ahead of expiry."""

    def __init__(self, config: Config, holder: BotHolder, bot: Bot):
        self._config = config
        self._holder = holder
        self._bot = bot
        self._stopping = asyncio.Event()
        self._refresh_task: asyncio.Task[None] | None = None
        # Lock so that _refresh_loop and the watchdog don't call get_auth_token in parallel.
        self._refresh_lock = asyncio.Lock()

        holder.set(bot)

        if config.uses_credentials and not hasattr(bot, _TOKEN_ATTR):
            raise RuntimeError(
                f"Bot is missing the private attribute {_TOKEN_ATTR}; the hot-swap "
                "refresh strategy is incompatible with this python-trueconf-bot version."
            )

    async def run(self) -> None:
        """Main loop. Blocks until `shutdown()` is called."""
        if self._config.uses_credentials:
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="token-refresh")
        try:
            await self._run_with_watchdog()
        finally:
            if self._refresh_task is not None:
                self._refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._refresh_task

    async def shutdown(self) -> None:
        """Ask the supervisor and the bot to stop. Idempotent."""
        self._stopping.set()
        with contextlib.suppress(Exception):
            await self._bot.shutdown()

    async def _run_with_watchdog(self) -> None:
        backoff = _RUN_BACKOFF_MIN
        while not self._stopping.is_set():
            try:
                await self._bot.run(handle_signals=False)
                if self._stopping.is_set():
                    return
                logger.warning("bot.run() returned without a stop request, restarting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("bot.run() crashed: %s", type(exc).__name__)
                if self._config.uses_credentials:
                    await self._safe_refresh_once()

            if self._stopping.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RUN_BACKOFF_MAX)

    async def _refresh_loop(self) -> None:
        """Background task: wait until the refresh window, then hot-swap the token."""
        while not self._stopping.is_set():
            sleep_for = self._seconds_until_refresh()
            logger.info("Next token refresh in %.0f seconds", sleep_for)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                pass
            if self._stopping.is_set():
                return
            await self._safe_refresh_once()

    def _seconds_until_refresh(self) -> float:
        """Determine the wait until the next refresh, based on JWT `exp`."""
        current_token = getattr(self._bot, _TOKEN_ATTR, None)
        exp = parse_jwt_exp(current_token) if current_token else None
        if exp is None:
            return float(_REFRESH_FALLBACK_SECONDS)
        now = int(time.time())
        remaining = exp - now - _REFRESH_MARGIN_SECONDS
        return float(max(remaining, _REFRESH_RETRY_MIN))

    async def _safe_refresh_once(self) -> None:
        """Attempt to refresh the token with bounded retries. The `_refresh_lock`
        ensures only one attempt runs at a time."""
        async with self._refresh_lock:
            delay = _REFRESH_RETRY_MIN
            for attempt in range(1, _REFRESH_MAX_ATTEMPTS + 1):
                try:
                    await self._refresh_once()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Token refresh attempt %d failed: %s",
                        attempt, type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _REFRESH_RETRY_MAX)
            logger.error(
                "Token refresh failed after %d attempts, continuing with the old token",
                _REFRESH_MAX_ATTEMPTS,
            )

    async def _refresh_once(self) -> None:
        """One refresh step: new JWT -> swap in -> drain in-flight -> close WS."""
        loop = asyncio.get_running_loop()
        new_token = await loop.run_in_executor(
            None,
            lambda: get_auth_token(
                server=self._config.trueconf_server,
                username=self._config.trueconf_username,
                password=self._config.trueconf_password,
                verify=self._config.trueconf_verify_ssl,
                port=self._config.trueconf_web_port,
            ),
        )
        if not new_token:
            raise RuntimeError("Server did not return an access_token")

        # Swap the field first, then drain in-flight operations (they will
        # finish on the old WS), only then close the socket. This minimizes
        # the window during which an active send observes a dropped connection.
        setattr(self._bot, _TOKEN_ATTR, new_token)
        logger.info("Token refreshed")

        drained = await self._holder.wait_idle(_DRAIN_BEFORE_RECONNECT_SEC)
        if not drained:
            logger.warning(
                "Did not drain %d in-flight operations within %.1fs — continuing",
                self._holder.in_flight, _DRAIN_BEFORE_RECONNECT_SEC,
            )

        ws = getattr(self._bot, "_ws", None)
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
