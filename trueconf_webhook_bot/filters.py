"""Custom filters on top of python-trueconf-bot."""

from __future__ import annotations

import dataclasses
import logging
import re
import time

from trueconf.enums import ChatType
from trueconf.enums.message_type import MessageType
from trueconf.filters import Command
from trueconf.types.message import Message

from .bot_holder import BotHolder

logger = logging.getLogger(__name__)

# Short-TTL cache for chat types. Chat type does not change in practice,
# so caching avoids re-querying `get_chat_by_id` for every matching command
# and for every incoming message in the same chat.
_CHAT_TYPE_TTL_SEC: float = 300.0
_chat_type_cache: dict[str, tuple[float, int | None]] = {}

# Chat types where any participant can issue a command without a preceding
# mention — there is no risk of another participant accidentally starting a
# line with `/something` and triggering the bot.
_MENTION_OPTIONAL_CHAT_TYPES: frozenset[int] = frozenset(
    {int(ChatType.P2P), int(ChatType.FAVORITES)}
)

# Match a single leading mention at the very start of the text:
# - TrueConf HTML anchor inserted by clients when typing @username, e.g.
#   `<a href="trueconf:user@server">user</a>`;
# - a plain `@user` token as a fallback.
# Surrounding whitespace is consumed so the remaining text can be checked
# for the command prefix.
_LEADING_MENTION_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:<a\s+[^>]*>[^<]*</a>|@\S+)\s*",
    re.IGNORECASE,
)

# Matches any mention anywhere in the text: a TrueConf HTML anchor or a plain
# @user token. Used to strip mentions out of command arguments so they never
# leak into webhook names.
_ANY_MENTION_RE: re.Pattern[str] = re.compile(
    r"<a\s+[^>]*>[^<]*</a>|@\S+",
    re.IGNORECASE,
)

# HTML anchor mention: extract the trueconf:USER@SERVER target from href.
_HTML_MENTION_RE: re.Pattern[str] = re.compile(
    r'<a\s+[^>]*href\s*=\s*"trueconf:([^"&]+)[^"]*"[^>]*>[^<]*</a>',
    re.IGNORECASE,
)

# Plain @token mention; captures the username that follows `@`.
_PLAIN_MENTION_RE: re.Pattern[str] = re.compile(r"@(\S+)")


def strip_leading_mention(text: str) -> str:
    """Remove a leading bot mention (if any) from the raw message text."""
    return _LEADING_MENTION_RE.sub("", text, count=1)


def strip_all_mentions(text: str) -> str:
    """Remove every mention (HTML anchor or @token) from the text."""
    return _ANY_MENTION_RE.sub("", text)


def _normalize_username(value: str) -> str:
    """Reduce a TrueConf identifier to its local (pre-`@`) part, case-folded."""
    return value.split("@", 1)[0].casefold()


def contains_bot_mention(text: str, bot_username: str) -> bool:
    """Return True if `text` mentions the specific bot by username.

    Recognizes two forms:
    - HTML anchor: `<a href="trueconf:zaebot@server&do=profile">zaebot</a>`
    - Plain `@token`: `@zaebot` (with or without domain).

    Mentions of other users do not count — this avoids triggering on a
    random participant being @-tagged in an otherwise commandless message.
    """
    if not bot_username:
        return False
    target = _normalize_username(bot_username)
    for m in _HTML_MENTION_RE.finditer(text):
        if _normalize_username(m.group(1)) == target:
            return True
    for m in _PLAIN_MENTION_RE.finditer(text):
        if _normalize_username(m.group(1)) == target:
            return True
    return False


class BotCommand(Command):
    """Drop-in replacement for `Command` with two extra behaviours:

    - Tolerates a leading @mention of the bot (some TrueConf clients rewrite
      `@bot /cmd` so the text starts with the mention, not with the prefix).
    - In group chats and channels requires an explicit mention of the bot,
      so that an unrelated participant typing a line starting with `/...` is
      not mistakenly treated as a command. In P2P and Favorites chats the
      mention is optional (no one else could issue a command there).

    The chat-type check needs a live `Bot` to call `get_chat_by_id`; pass a
    `BotHolder` to enable it. When the holder is `None`, the mention-required
    behaviour is disabled — this keeps the filter testable in isolation.
    """

    def __init__(
        self,
        *commands,
        holder: BotHolder | None = None,
        bot_username: str | None = None,
        **kwargs,
    ):
        super().__init__(*commands, **kwargs)
        self._holder = holder
        self._bot_username = bot_username

    async def __call__(self, event):
        if not isinstance(event, Message):
            return False
        if event.type != MessageType.PLAIN_MESSAGE:
            return False
        raw_text = event.content.text or ""

        # Self-mention can appear anywhere in the text (`@bot /cmd`,
        # `/cmd @bot`, `hey @bot please /cmd`). Check the whole message
        # for a mention of the specific bot, not any arbitrary user.
        has_self_mention = contains_bot_mention(raw_text, self._bot_username or "")

        # For command parsing we still want to drop a leading mention so
        # `@bot /cmd` is treated the same as `/cmd`.
        text = strip_leading_mention(raw_text)
        if not text.startswith(self.prefix):
            return False

        # Parse and validate the command NAME first. This short-circuits
        # messages that happen to start with `/` but are not aimed at us —
        # without calling any chat-metadata API.
        try:
            command_obj = self.extract_command(text)
        except ValueError:
            return False
        try:
            self.validate_command(command_obj)
        except ValueError:
            return False

        # The command name matches ours. If the caller did not explicitly
        # mention the bot, check whether the chat type requires a mention
        # (group/channel) or allows an unmentioned command (P2P/favorites).
        if not has_self_mention and self._holder is not None:
            if not await self._mention_optional(event.chat_id):
                return False

        if self.magic:
            result = self.magic.resolve(command_obj)
            if not result:
                return False
            if isinstance(result, dict):
                return {"command": dataclasses.replace(command_obj, magic_result=result)}
            return {"command": command_obj}
        return {"command": command_obj}

    async def _mention_optional(self, chat_id: str) -> bool:
        """Return True if the chat is private enough to allow unmentioned commands."""
        chat_type = await _get_chat_type(self._holder, chat_id)
        return chat_type in _MENTION_OPTIONAL_CHAT_TYPES


async def _get_chat_type(holder: BotHolder, chat_id: str) -> int | None:
    """Return the chat type, using a small per-chat TTL cache.

    Chat type is effectively immutable, so a 5-minute cache is generous but
    safe. On lookup failure we cache `None` briefly so repeated failures
    don't hammer the API — the caller treats `None` as "unknown" and the
    fail-closed policy requires an explicit mention in that case.
    """
    now = time.monotonic()
    cached = _chat_type_cache.get(chat_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        chat = await holder.bot.get_chat_by_id(chat_id)
        chat_type = getattr(chat, "chat_type", None)
    except Exception as exc:
        logger.info("Could not resolve chat type for %s: %s", chat_id, exc)
        chat_type = None
    _chat_type_cache[chat_id] = (now + _CHAT_TYPE_TTL_SEC, chat_type)
    return chat_type
