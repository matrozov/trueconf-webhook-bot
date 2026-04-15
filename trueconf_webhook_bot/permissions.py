"""Permission checks for webhook management inside a chat."""

from __future__ import annotations

import asyncio
import logging

from trueconf import Bot
from trueconf.enums import ChatParticipantRole, ChatType

logger = logging.getLogger(__name__)

# Roles that grant permission to create and revoke webhooks in GROUP/CHANNEL chats.
_ADMIN_ROLES: frozenset[ChatParticipantRole] = frozenset(
    {ChatParticipantRole.OWNER, ChatParticipantRole.ADMIN}
)

# Chat types where admin-only enforcement does not make sense: the user is
# effectively the sole owner of such a chat and there is no moderation hierarchy.
_PERSONAL_CHAT_TYPES: frozenset[int] = frozenset(
    {int(ChatType.P2P), int(ChatType.FAVORITES)}
)

# Pagination page size when scanning chat participants. Comfortable for typical
# chats; larger chats are paged through until a match is found or a partial
# page signals the end.
_PAGE_SIZE: int = 200


async def can_manage_webhooks(bot: Bot, chat_id: str, user_id: str) -> bool:
    """Return True if the user is allowed to create/revoke webhooks in this chat.

    Rules:
    - For personal (P2P) and favorites chats, any participant is allowed — there
      is no moderation hierarchy in these chats.
    - For groups and channels, only OWNER or ADMIN roles are allowed.

    On network/API errors the function returns False (fail-closed: do not
    grant privileges when the state is unknown).
    """
    try:
        chat = await bot.get_chat_by_id(chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Failed to resolve chat type for %s: %s", chat_id, exc)
        return False

    if getattr(chat, "chat_type", None) in _PERSONAL_CHAT_TYPES:
        return True

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
