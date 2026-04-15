"""Entrypoint: wire all components together and run the event loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aiohttp import web
from trueconf import Bot, Dispatcher
from trueconf.utils.token import get_auth_token

from .bot_holder import BotHolder
from .config import Config, ConfigError, load_config
from .handlers import build_router
from .http_server import HttpLimits, build_app
from .rate_limit import SlidingWindowRateLimiter
from .storage import WebhookStorage
from .supervisor import BotSupervisor

logger = logging.getLogger("trueconf_webhook_bot")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_bot(config: Config, dispatcher: Dispatcher) -> Bot:
    """Build a `Bot` instance according to the selected authentication mode.

    We do not call `Bot.from_credentials()` because it hard-codes `protocol`
    and `port` when calling the OAuth endpoint (uses `https://server:443`
    regardless of the `https`/`web_port` arguments we pass). Instead we fetch
    the token ourselves with the correct protocol/port and then construct
    `Bot` directly.
    """
    if config.trueconf_token is not None:
        token = config.trueconf_token
    else:
        token = get_auth_token(
            server=config.trueconf_server,
            username=config.trueconf_username or "",
            password=config.trueconf_password or "",
            verify=config.trueconf_verify_ssl,
            protocol="https" if config.trueconf_https else "http",
            port=config.trueconf_web_port,
        )
        if not token:
            raise RuntimeError("TrueConf OAuth endpoint did not return an access_token")

    return Bot(
        server=config.trueconf_server,
        token=token,
        web_port=config.trueconf_web_port,
        https=config.trueconf_https,
        verify_ssl=config.trueconf_verify_ssl,
        dispatcher=dispatcher,
    )


async def _run(config: Config) -> None:
    storage = WebhookStorage(config.webhook_storage_path)
    await storage.load()
    logger.info("Loaded %d webhook(s) from %s", len(storage), storage.path)

    holder = BotHolder()

    token_rate_limiter = SlidingWindowRateLimiter(
        limit=config.webhook_rate_limit_per_minute, window_seconds=60.0,
    )
    ip_rate_limiter = SlidingWindowRateLimiter(
        limit=config.webhook_rate_limit_per_ip_per_minute, window_seconds=60.0,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(storage, holder, config))

    bot = _build_bot(config, dispatcher)
    supervisor = BotSupervisor(config, holder, bot)

    limits = HttpLimits(
        max_upload_bytes=config.webhook_max_upload_mb * 1024 * 1024,
        max_attachments=config.webhook_max_attachments,
    )
    app = build_app(storage, holder, token_rate_limiter, ip_rate_limiter, limits)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.webhook_http_host, port=config.webhook_http_port)
    await site.start()
    logger.info(
        "HTTP server listening on http://%s:%d (max upload %d MB, rate %d/min per token, %d/min per IP)",
        config.webhook_http_host, config.webhook_http_port,
        config.webhook_max_upload_mb,
        config.webhook_rate_limit_per_minute,
        config.webhook_rate_limit_per_ip_per_minute,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    supervisor_task = asyncio.create_task(supervisor.run(), name="supervisor")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, _pending = await asyncio.wait(
            {supervisor_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if supervisor_task in done:
            with contextlib.suppress(asyncio.CancelledError):
                supervisor_task.result()
    finally:
        logger.info("Shutting down...")

        # 1. Stop accepting new HTTP requests (`site.stop` closes the listener).
        with contextlib.suppress(Exception):
            await site.stop()

        # 2. Let in-flight requests drain before the bot goes away.
        with contextlib.suppress(Exception):
            await runner.shutdown()

        # 3. Now stop the bot and the supervisor.
        await supervisor.shutdown()
        if not supervisor_task.done():
            supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await supervisor_task
        if not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task

        # 4. Flush any deferred touch counters and release the HTTP runner.
        with contextlib.suppress(Exception):
            await storage.flush_pending()
        with contextlib.suppress(Exception):
            await runner.cleanup()
        logger.info("Stopped")


def cli() -> None:
    """CLI wrapper."""
    _configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(2)
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
