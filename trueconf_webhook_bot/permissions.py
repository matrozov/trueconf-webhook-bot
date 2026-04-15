"""Permission checks for webhook management inside a chat."""

from __future__ import annotations

import asyncio
import logging

from trueconf import Bot
from trueconf.enums import ChatParticipantRole

logger = logging.getLogger(__name__)

# Roles that grant permission to create and revoke webhooks when admin-only mode
# is on. In TrueConf the "admin" roles are the chat owner and explicit admins.
_ADMIN_ROLES: frozenset[ChatParticipantRole] = frozenset(
    {ChatParticipantRole.OWNER, ChatParticipantRole.ADMIN}
)

# Pagination page size when scanning chat participants. Comfortable for typical
# chats; larger chats are paged through until a match is found or a partial
# page signals the end.
_PAGE_SIZE: int = 200


async def is_admin(bot: Bot, chat_id: str, user_id: str) -> bool:
    """Return True if the user has an OWNER or ADMIN role in the given chat.

    Pagination is walked until the first match or an empty page. TrueConf may
    return identifiers with or without a domain (`user` vs `user@server`), so
    comparison is done against the local part only.

    Args:
        bot: `Bot` instance (typically from `BotHolder`).
        chat_id: chat identifier.
        user_id: id of the user who invoked the command.

    Returns: True if the role is admin-equivalent, otherwise False.

    Side effects: on network/API errors the function logs a warning and returns
    False (fail-closed — do not grant privileges when the state is unknown).
    """
    target = _local_part(user_id)
    page = 1
    try:
        while True:
            response = await bot.get_chat_participants(
                chat_id=chat_id, page_size=_PAGE_SIZE, page_number=page
            )
            participants = getattr(response, "participants", []) or []
            if not participants:
                return False
            for participant in participants:
                if _local_part(participant.user_id) == target:
                    return participant.role in _ADMIN_ROLES
            if len(participants) < _PAGE_SIZE:
                return False
            page += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Failed to verify role of user %s in chat %s: %s", user_id, chat_id, exc)
        return False


def _local_part(user_id: str) -> str:
    """Strip the domain from a TrueConf id: `user@server` -> `user`."""
    return user_id.split("@", 1)[0].casefold()
