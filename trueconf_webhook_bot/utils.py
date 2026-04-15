"""Small helpers: token masking, JWT parsing, URL building."""

from __future__ import annotations

import base64
import html
import json
import re

# TrueConf chat clients rewrite @mentions into HTML anchor tags like
# `<a href="trueconf:user@server">user</a>` before sending the message text
# to the server. When such text ends up as a webhook name it later re-renders
# as a live link in echoed messages. We strip tags but keep the visible text.
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")


def mask_token(token: str, prefix: int = 4, suffix: int = 4) -> str:
    """Return a masked form of a token, safe to show in logs or in /webhook_list.

    Produces `abcd…wxyz`. If the token is too short, returns a placeholder so
    we never accidentally reveal too much of the original value.

    Args:
        token: original token string.
        prefix: how many leading characters to keep.
        suffix: how many trailing characters to keep.
    """
    if not token:
        return "(empty)"
    if len(token) <= prefix + suffix:
        return "…" * 8
    return f"{token[:prefix]}…{token[-suffix:]}"


def sanitize_chat_input(raw: str) -> str:
    """Strip HTML tags (and decode entities) from a user-supplied chat string.

    TrueConf chat clients inline @mentions as HTML anchors inside the message
    body. When such text is persisted and later echoed back as plain text we
    want the visible name only, not the raw tag markup.
    """
    without_tags = _HTML_TAG_RE.sub("", raw)
    decoded = html.unescape(without_tags)
    return " ".join(decoded.split())


def build_webhook_url(public_base_url: str, token: str) -> str:
    """Compose the public webhook URL from the base URL and a token."""
    return f"{public_base_url.rstrip('/')}/hook/{token}"


def parse_jwt_exp(token: str) -> int | None:
    """Extract the JWT expiration unix timestamp from the payload.

    Returns an integer number of seconds since the epoch, or None if the field
    is missing or the string is not a JWT. The signature is not verified
    (the server already checked it when issuing the token) — this is purely
    informational and used by the supervisor to schedule refresh.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_encoded = parts[1]
        padded = payload_encoded + "=" * (-len(payload_encoded) % 4)
        payload_raw = base64.urlsafe_b64decode(padded)
        payload = json.loads(payload_raw)
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
