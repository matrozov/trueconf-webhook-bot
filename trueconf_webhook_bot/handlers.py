"""Webhook management commands: `/webhook_create`, `/webhook_list`, `/webhook_revoke`.

The secret URL is delivered exclusively in a direct message to the initiator,
so it never appears in the group chat history. The group chat only receives
an impersonal acknowledgement.

Creation order: reserve the storage record first (so concurrent `/webhook_create`
calls with the same name don't race), then deliver the URL in a DM, then
confirm. If DM delivery fails, the reserved record is revoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from trueconf import Bot, Router
from trueconf.exceptions import ApiErrorException
from trueconf.filters.command import CommandObject
from trueconf.types.message import Message

from .bot_holder import BotHolder
from .config import Config
from .filters import BotCommand, strip_all_mentions
from .permissions import can_manage_webhooks
from .storage import WebhookNameConflict, WebhookNotFound, WebhookStorage
from .utils import build_webhook_url, mask_token, sanitize_chat_input

logger = logging.getLogger(__name__)


async def _chat_label(bot: Bot, chat_id: str) -> str:
    """Return a human-readable label for a chat, falling back to a short id."""
    try:
        chat = await bot.get_chat_by_id(chat_id)
        title = getattr(chat, "title", None)
        if title:
            return f"«{title}»"
    except Exception:
        pass
    return f"chat `{chat_id[:12]}…`"


async def _reply(
    bot: Bot,
    chat_id: str,
    text: str,
    *,
    fallback_user_id: str | None = None,
) -> None:
    """Send a chat reply, with a DM fallback when the origin chat is read-only.

    TrueConf returns `[303] Not enough rights` when the bot is a member of a
    chat (receives messages) but is not allowed to post there — typical for a
    channel where the bot is just a subscriber. When that happens, we try the
    user's personal chat so the caller still sees the answer, prefixing the
    message with the source chat label so references like "this chat" in the
    body still make sense. If the DM also fails, we give up and log; there is
    no other channel left.
    """
    try:
        await bot.send_message(chat_id, text)
        return
    except ApiErrorException as exc:
        logger.info("Reply suppressed in chat %s: %s", chat_id, exc)

    if fallback_user_id is None:
        return
    try:
        personal = await bot.create_personal_chat(fallback_user_id)
        if personal.chat_id == chat_id:
            # Fallback would point at the same chat that just rejected us.
            return
        label = await _chat_label(bot, chat_id)
        fallback_text = f"[Reply to your command in {label}]\n{text}"
        await bot.send_message(personal.chat_id, fallback_text)
    except ApiErrorException as exc:
        logger.info("DM fallback to user %s suppressed: %s", fallback_user_id, exc)
    except Exception as exc:
        logger.warning("DM fallback to user %s failed: %s", fallback_user_id, exc)


def build_router(
    storage: WebhookStorage,
    holder: BotHolder,
    config: Config,
) -> Router:
    """Assemble the router with the three management commands."""

    router = Router(name="webhook-bot-commands")

    @router.message(BotCommand("webhook_create", holder=holder, bot_username=config.trueconf_bot_username))
    async def cmd_create(message: Message, command: CommandObject) -> None:
        name = sanitize_chat_input(strip_all_mentions(command.args or ""))
        user_id = message.from_user.id
        logger.info("cmd_create: user=%s chat=%s name=%r", user_id, message.chat_id, name)

        async with holder.acquire() as bot:
            if not name:
                await _reply(
                    bot, message.chat_id, "Usage: `/webhook_create <name>`",
                    fallback_user_id=user_id,
                )
                return

            if config.webhook_admin_only and not await can_manage_webhooks(bot, message.chat_id, user_id):
                await _reply(
                    bot, message.chat_id, "Only chat administrators can create webhooks.",
                    fallback_user_id=user_id,
                )
                return

            # Reserve the name up-front. This guards against two concurrent
            # /webhook_create commands with the same name racing each other.
            logger.info("cmd_create: passed admin check, reserving in storage")
            try:
                reserved = await storage.create(
                    chat_id=message.chat_id, name=name, created_by=user_id,
                )
            except WebhookNameConflict as exc:
                await _reply(bot, message.chat_id, str(exc), fallback_user_id=user_id)
                return
            except ValueError as exc:
                await _reply(bot, message.chat_id, str(exc), fallback_user_id=user_id)
                return

            logger.info("cmd_create: reserved hook id=%s, opening DM", reserved.id)
            url = build_webhook_url(config.webhook_public_url, reserved.token)
            masked = mask_token(reserved.token)

            try:
                personal = await bot.create_personal_chat(user_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Could not open a DM with user %s: %s", user_id, exc)
                with contextlib.suppress(WebhookNotFound):
                    await storage.revoke(message.chat_id, reserved.name)
                await _reply(
                    bot, message.chat_id,
                    "Could not open a direct message with you. "
                    "Please allow DMs from the bot and try again.",
                )
                return

            source_is_dm = (message.chat_id == personal.chat_id)
            logger.info(
                "cmd_create: personal.chat_id=%s source_is_dm=%s",
                personal.chat_id, source_is_dm,
            )
            source_label = await _chat_label(bot, reserved.chat_id)
            logger.info("cmd_create: source_label=%s", source_label)
            dm_body = (
                f"Webhook «{reserved.name}» created for {source_label}.\n\n"
                f"URL (save it now — it won't be shown again):\n{url}"
            )

            # In a direct chat with the bot, source and DM are the same chat —
            # a single message is enough and probing is pointless.
            if source_is_dm:
                logger.info("cmd_create: P2P path, sending URL")
                try:
                    await bot.send_message(personal.chat_id, dm_body)
                    logger.info("cmd_create: P2P URL delivered")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to deliver URL in P2P chat: %s", exc)
                    with contextlib.suppress(WebhookNotFound):
                        await storage.revoke(message.chat_id, reserved.name)
                return

            # Probe the source chat: if the bot cannot post there, the webhook
            # would be useless (incoming POSTs could not deliver anything).
            # Roll back before exposing the URL to the user.
            probe_text = (
                f"✅ Webhook «{reserved.name}» created ({masked}). "
                "Check your direct messages for the URL."
            )
            try:
                await bot.send_message(message.chat_id, probe_text)
            except asyncio.CancelledError:
                raise
            except ApiErrorException as exc:
                logger.info(
                    "Source chat %s rejected our probe (%s) — rolling back webhook",
                    message.chat_id, exc,
                )
                with contextlib.suppress(WebhookNotFound):
                    await storage.revoke(message.chat_id, reserved.name)
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        personal.chat_id,
                        f"⚠ Could not create webhook for {source_label}: "
                        "the bot does not have permission to post messages there. "
                        "Please grant it posting rights or use a different chat.",
                    )
                return

            # Source works — hand off the URL in DM.
            try:
                await bot.send_message(personal.chat_id, dm_body)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Failed to deliver the URL via DM to user %s: %s", user_id, exc)
                with contextlib.suppress(WebhookNotFound):
                    await storage.revoke(message.chat_id, reserved.name)
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        message.chat_id,
                        f"⚠ Webhook «{reserved.name}» was revoked: "
                        "could not deliver the URL via direct message. "
                        "Please enable DMs from the bot and try again.",
                    )
                return

    @router.message(BotCommand("webhook_list", holder=holder, bot_username=config.trueconf_bot_username))
    async def cmd_list(message: Message, command: CommandObject) -> None:
        hooks = storage.list_by_chat(message.chat_id)
        async with holder.acquire() as bot:
            if not hooks:
                await _reply(bot, message.chat_id, "No webhooks in this chat.", fallback_user_id=message.from_user.id)
                return
            lines = [f"Webhooks in this chat ({len(hooks)}):"]
            for hook in hooks:
                last = hook.last_used_at or "never"
                lines.append(
                    f"• «{hook.name}» — `{mask_token(hook.token)}`, "
                    f"created {hook.created_at}, used {hook.usage_count} times, last {last}"
                )
            await _reply(bot, message.chat_id, "\n".join(lines), fallback_user_id=message.from_user.id)

    @router.message(BotCommand("webhook_revoke", holder=holder, bot_username=config.trueconf_bot_username))
    async def cmd_revoke(message: Message, command: CommandObject) -> None:
        name = sanitize_chat_input(strip_all_mentions(command.args or ""))
        async with holder.acquire() as bot:
            if not name:
                await _reply(bot, message.chat_id, "Usage: `/webhook_revoke <name>`", fallback_user_id=message.from_user.id)
                return

            user_id = message.from_user.id
            if config.webhook_admin_only and not await can_manage_webhooks(bot, message.chat_id, user_id):
                await _reply(
                    bot, message.chat_id, "Only chat administrators can revoke webhooks.",
                    fallback_user_id=user_id,
                )
                return

            try:
                hook = await storage.revoke(message.chat_id, name)
            except WebhookNotFound as exc:
                await _reply(bot, message.chat_id, str(exc), fallback_user_id=message.from_user.id)
                return

            await _reply(bot, message.chat_id, f"🗑 Webhook «{hook.name}» revoked.", fallback_user_id=message.from_user.id)

    return router
