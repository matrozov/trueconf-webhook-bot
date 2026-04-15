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

from trueconf import Router
from trueconf.filters import Command
from trueconf.filters.command import CommandObject
from trueconf.types.message import Message

from .bot_holder import BotHolder
from .config import Config
from .permissions import is_admin
from .storage import WebhookNameConflict, WebhookNotFound, WebhookStorage
from .utils import build_webhook_url, mask_token

logger = logging.getLogger(__name__)


def build_router(
    storage: WebhookStorage,
    holder: BotHolder,
    config: Config,
) -> Router:
    """Assemble the router with the three management commands."""

    router = Router(name="webhook-bot-commands")

    @router.message(Command("webhook_create"))
    async def cmd_create(message: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        if not name:
            async with holder.acquire() as bot:
                await bot.send_message(message.chat_id, "Usage: `/webhook_create <name>`")
            return

        user_id = message.from_user.id

        async with holder.acquire() as bot:
            if config.webhook_admin_only and not await is_admin(bot, message.chat_id, user_id):
                await bot.send_message(
                    message.chat_id, "Only chat administrators can create webhooks."
                )
                return

            # Reserve the name up-front. This guards against two concurrent
            # /webhook_create commands with the same name racing each other.
            try:
                reserved = await storage.create(
                    chat_id=message.chat_id, name=name, created_by=user_id,
                )
            except WebhookNameConflict as exc:
                await bot.send_message(message.chat_id, str(exc))
                return
            except ValueError as exc:
                await bot.send_message(message.chat_id, str(exc))
                return

            url = build_webhook_url(config.webhook_public_url, reserved.token)
            masked = mask_token(reserved.token)

            try:
                personal = await bot.create_personal_chat(user_id)
                await bot.send_message(
                    personal.chat_id,
                    f"Webhook «{reserved.name}» created for chat `{reserved.chat_id}`.\n\n"
                    f"URL (save it now — it won't be shown again):\n{url}",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to deliver the URL via DM to user %s: %s", user_id, exc
                )
                # Roll back the reserved record since the URL never reached the caller.
                with contextlib.suppress(WebhookNotFound):
                    await storage.revoke(message.chat_id, reserved.name)
                await bot.send_message(
                    message.chat_id,
                    "Could not deliver the URL via direct message. "
                    "Please make sure the bot can DM you and try again.",
                )
                return

            await bot.send_message(
                message.chat_id,
                f"✅ Webhook «{reserved.name}» created ({masked}). "
                "The URL has been sent to you in a direct message.",
            )

    @router.message(Command("webhook_list"))
    async def cmd_list(message: Message, command: CommandObject) -> None:
        hooks = storage.list_by_chat(message.chat_id)
        async with holder.acquire() as bot:
            if not hooks:
                await bot.send_message(message.chat_id, "No webhooks in this chat.")
                return
            lines = [f"Webhooks in this chat ({len(hooks)}):"]
            for hook in hooks:
                last = hook.last_used_at or "never"
                lines.append(
                    f"• «{hook.name}» — `{mask_token(hook.token)}`, "
                    f"created {hook.created_at}, used {hook.usage_count} times, last {last}"
                )
            await bot.send_message(message.chat_id, "\n".join(lines))

    @router.message(Command("webhook_revoke"))
    async def cmd_revoke(message: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        async with holder.acquire() as bot:
            if not name:
                await bot.send_message(message.chat_id, "Usage: `/webhook_revoke <name>`")
                return

            user_id = message.from_user.id
            if config.webhook_admin_only and not await is_admin(bot, message.chat_id, user_id):
                await bot.send_message(
                    message.chat_id, "Only chat administrators can revoke webhooks."
                )
                return

            try:
                hook = await storage.revoke(message.chat_id, name)
            except WebhookNotFound as exc:
                await bot.send_message(message.chat_id, str(exc))
                return

            await bot.send_message(message.chat_id, f"🗑 Webhook «{hook.name}» revoked.")

    return router
