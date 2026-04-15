"""HTTP server for incoming webhooks.

Accepts both formats on a single endpoint `POST /hook/{token}`:
- `application/json` — reference format (URLs only in `images[]` / `files[]`);
- `multipart/form-data` — additionally allows uploading files as binary parts.

The token in the URL is the only authentication factor; an unknown token
returns 404 without distinguishing "does not exist" from "revoked" to avoid
helping enumeration.

A per-IP rate limit is applied before the storage lookup — this prevents
scanning of the token space.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from aiohttp import web
from trueconf import Bot
from trueconf.enums import ParseMode
from trueconf.types.input_file import BufferedInputFile, InputFile, URLInputFile

from .bot_holder import BotHolder
from .rate_limit import SlidingWindowRateLimiter
from .storage import WebhookStorage
from .url_guard import InvalidAttachmentUrl, validate_public_url
from .utils import mask_token

logger = logging.getLogger(__name__)

_PARSE_MODE_MAP: dict[str, ParseMode] = {
    "text": ParseMode.TEXT,
    "markdown": ParseMode.MARKDOWN,
    "html": ParseMode.HTML,
}


@dataclass(frozen=True)
class HttpLimits:
    """Numeric limits for the HTTP layer."""

    max_upload_bytes: int
    max_attachments: int


@dataclass
class _Attachments:
    """Intermediate representation of the attachments to send.

    Each image is a `(file, preview)` pair. For now both point at the same
    payload — TrueConf clients render photos inline only when a preview is
    attached. See CLAUDE.md for the planned upgrade to real thumbnails.
    """

    images: list[tuple[InputFile, InputFile]]
    files: list[tuple[InputFile, str | None]]  # (file, optional filename override)

    def total(self) -> int:
        return len(self.images) + len(self.files)


def build_app(
    storage: WebhookStorage,
    holder: BotHolder,
    token_rate_limiter: SlidingWindowRateLimiter,
    ip_rate_limiter: SlidingWindowRateLimiter,
    limits: HttpLimits,
) -> web.Application:
    """Assemble the aiohttp application."""
    app = web.Application(client_max_size=limits.max_upload_bytes)
    app["storage"] = storage
    app["holder"] = holder
    app["token_rate_limiter"] = token_rate_limiter
    app["ip_rate_limiter"] = ip_rate_limiter
    app["limits"] = limits
    app.router.add_post("/hook/{token}", _handle_incoming)
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    return app


async def _healthz(_request: web.Request) -> web.Response:
    """Liveness probe."""
    return web.json_response({"ok": True})


async def _readyz(request: web.Request) -> web.Response:
    """Readiness probe: the bot is up and its TrueConf link is alive.

    We rely on the library's own state flags `connected_event` and
    `authorized_event` rather than poking the `_ws` object — the websockets
    library's connection API has changed across versions and does not always
    expose a reliable `.closed` attribute.
    """
    holder: BotHolder = request.app["holder"]
    try:
        bot = holder.bot
    except RuntimeError:
        return web.json_response({"ready": False, "reason": "bot_not_initialized"}, status=503)

    connected_event = getattr(bot, "connected_event", None)
    authorized_event = getattr(bot, "authorized_event", None)
    connected = bool(connected_event and connected_event.is_set())
    authorized = bool(authorized_event and authorized_event.is_set())

    if connected and authorized:
        return web.json_response({"ready": True})
    return web.json_response(
        {"ready": False, "connected": connected, "authorized": authorized},
        status=503,
    )


async def _handle_incoming(request: web.Request) -> web.Response:
    token: str = request.match_info["token"]
    storage: WebhookStorage = request.app["storage"]
    holder: BotHolder = request.app["holder"]
    token_limiter: SlidingWindowRateLimiter = request.app["token_rate_limiter"]
    ip_limiter: SlidingWindowRateLimiter = request.app["ip_rate_limiter"]
    limits: HttpLimits = request.app["limits"]

    # Per-IP rate limit applies BEFORE the token lookup — this shuts down scanning.
    client_ip = request.remote or "unknown"
    if not ip_limiter.allow(f"ip:{client_ip}"):
        retry_after = max(1, int(ip_limiter.retry_after(f"ip:{client_ip}")))
        return _rate_limited_response(retry_after)

    hook = storage.get_by_token(token)
    if hook is None:
        return web.json_response({"error": "not_found"}, status=404)

    if not token_limiter.allow(token):
        retry_after = max(1, int(token_limiter.retry_after(token)))
        return _rate_limited_response(retry_after)

    content_type = (request.content_type or "").lower()
    try:
        if content_type.startswith("multipart/"):
            parsed = await _parse_multipart(request, limits)
        else:
            parsed = await _parse_json(request, limits)
    except _PayloadError as exc:
        return web.json_response({"error": exc.code}, status=400)

    if parsed.text is None and parsed.attachments.total() == 0:
        return web.json_response({"error": "empty_payload"}, status=400)

    logger.info(
        "Incoming webhook hook=%s chat=%s token=%s images=%d files=%d",
        hook.name, hook.chat_id, mask_token(token),
        len(parsed.attachments.images), len(parsed.attachments.files),
    )

    delivery_errors: list[str] = []
    async with holder.acquire() as bot:
        if parsed.text:
            try:
                await bot.send_message(
                    chat_id=hook.chat_id, text=parsed.text, parse_mode=parsed.parse_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("send_message failed for %s: %s", mask_token(token), exc)
                delivery_errors.append(f"text: {type(exc).__name__}")

        for index, (image_file, image_preview) in enumerate(parsed.attachments.images):
            error = await _send_photo(
                bot, hook.chat_id, image_file, image_preview, parsed.parse_mode,
            )
            if error is not None:
                delivery_errors.append(f"images[{index}]: {error}")

        for index, (doc, fallback_name) in enumerate(parsed.attachments.files):
            error = await _send_document(bot, hook.chat_id, doc, fallback_name, parsed.parse_mode)
            if error is not None:
                delivery_errors.append(f"files[{index}]: {error}")

    if delivery_errors:
        # Do not call touch(): "last_used_at" means the last SUCCESSFUL delivery.
        return web.json_response({"ok": False, "errors": delivery_errors}, status=502)

    await storage.touch(token)
    return web.json_response({"ok": True})


def _rate_limited_response(retry_after: int) -> web.Response:
    return web.json_response(
        {"error": "rate_limited", "retry_after": retry_after},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


# --- payload parsing ---------------------------------------------------------


@dataclass(frozen=True)
class _ParsedPayload:
    text: str | None
    parse_mode: ParseMode
    attachments: _Attachments


class _PayloadError(Exception):
    """Internal validation error; `code` is returned verbatim in the JSON response."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _parse_json(request: web.Request, limits: HttpLimits) -> _ParsedPayload:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise _PayloadError("invalid_json")

    if not isinstance(payload, dict):
        raise _PayloadError("payload_must_be_object")

    text_value = payload.get("text")
    if text_value is not None and not isinstance(text_value, str):
        raise _PayloadError("text_must_be_string")

    parse_mode = _parse_mode_or_raise(payload.get("parse_mode"))

    images_raw = payload.get("images") or []
    files_raw = payload.get("files") or []
    if not isinstance(images_raw, list) or not isinstance(files_raw, list):
        raise _PayloadError("images_files_must_be_array")

    if len(images_raw) + len(files_raw) > limits.max_attachments:
        raise _PayloadError("too_many_attachments")

    images: list[tuple[InputFile, InputFile]] = []
    for item in images_raw:
        if not isinstance(item, dict):
            raise _PayloadError("image_must_be_object")
        url = item.get("url")
        if not isinstance(url, str) or not url:
            raise _PayloadError("image_url_required")
        _validate_or_raise(url)
        images.append(_make_image_pair_from_url(url))

    files: list[tuple[InputFile, str | None]] = []
    for item in files_raw:
        if not isinstance(item, dict):
            raise _PayloadError("file_must_be_object")
        url = item.get("url")
        filename = item.get("filename")
        if not isinstance(url, str) or not url:
            raise _PayloadError("file_url_required")
        if filename is not None and not isinstance(filename, str):
            raise _PayloadError("filename_must_be_string")
        _validate_or_raise(url)
        files.append(_make_document_from_url(url, filename))

    return _ParsedPayload(
        text=text_value,
        parse_mode=parse_mode,
        attachments=_Attachments(images=images, files=files),
    )


async def _parse_multipart(request: web.Request, limits: HttpLimits) -> _ParsedPayload:
    text_value: str | None = None
    parse_mode_raw: str | None = None
    images: list[tuple[InputFile, InputFile]] = []
    files: list[tuple[InputFile, str | None]] = []

    reader = await request.multipart()
    async for part in reader:
        name = part.name or ""

        if name == "text":
            text_value = (await part.text()) or None

        elif name == "parse_mode":
            parse_mode_raw = (await part.text()).strip()

        elif name == "image_url":
            url = (await part.text()).strip()
            if not url:
                raise _PayloadError("image_url_required")
            _validate_or_raise(url)
            images.append(_make_image_pair_from_url(url))

        elif name == "file_url":
            url = (await part.text()).strip()
            if not url:
                raise _PayloadError("file_url_required")
            _validate_or_raise(url)
            files.append(_make_document_from_url(url, None))

        elif name == "image":
            data = await part.read(decode=False)
            if not data:
                raise _PayloadError("image_empty")
            images.append(_make_image_pair_from_bytes(data, part.filename or "image.jpg"))

        elif name == "file":
            data = await part.read(decode=False)
            if not data:
                raise _PayloadError("file_empty")
            filename = part.filename or "file.bin"
            files.append((BufferedInputFile(file=data, filename=filename), filename))

        else:
            # Unknown parts are skipped silently so clients can attach extra
            # metadata in the future without breaking.
            await part.read(decode=False)

        if len(images) + len(files) > limits.max_attachments:
            raise _PayloadError("too_many_attachments")

    parse_mode = _parse_mode_or_raise(parse_mode_raw)

    return _ParsedPayload(
        text=text_value,
        parse_mode=parse_mode,
        attachments=_Attachments(images=images, files=files),
    )


def _parse_mode_or_raise(raw: Any) -> ParseMode:
    if raw is None:
        return ParseMode.TEXT
    if not isinstance(raw, str):
        raise _PayloadError("invalid_parse_mode")
    value = _PARSE_MODE_MAP.get(raw.strip().lower())
    if value is None:
        raise _PayloadError("invalid_parse_mode")
    return value


def _validate_or_raise(url: str) -> None:
    try:
        validate_public_url(url)
    except InvalidAttachmentUrl as exc:
        logger.info("Rejected unsafe URL: %s", exc)
        raise _PayloadError("unsafe_url") from exc


def _make_image_pair_from_url(url: str) -> tuple[InputFile, InputFile]:
    """Build `(file, preview)` for an image URL — the preview mirrors the file
    so TrueConf clients render the image inline instead of as a generic file."""
    fn = _filename_from_url(url, "image", ".jpg")
    return URLInputFile(url=url, filename=fn), URLInputFile(url=url, filename=fn)


def _make_image_pair_from_bytes(data: bytes, filename: str) -> tuple[InputFile, InputFile]:
    """Same as `_make_image_pair_from_url` but for an in-memory multipart part."""
    return (
        BufferedInputFile(file=data, filename=filename),
        BufferedInputFile(file=data, filename=filename),
    )


def _make_document_from_url(url: str, filename: str | None) -> tuple[InputFile, str]:
    """Build an `InputFile` plus the effective filename for a document URL."""
    effective_name = filename or _filename_from_url(url, "file", ".bin")
    return URLInputFile(url=url, filename=effective_name), effective_name


def _filename_from_url(url: str, default_stem: str, default_ext: str) -> str:
    """Best-effort filename from a URL's path component.

    python-trueconf-bot's `URLInputFile` constructor eagerly calls
    `mimetypes.guess_type(filename)` and then `guess_extension(mime_type)`
    in the base class; both crash on `None`. We therefore guarantee a
    filename with a recognisable extension so `guess_type()` returns a type
    and the second branch is never taken.
    """
    try:
        parsed = urlparse(url)
        # Percent-decode so names like `%D0%A4%D0%B0%D0%B9%D0%BB.pdf` come
        # through as `Файл.pdf` instead of the raw encoded form.
        candidate = unquote(PurePosixPath(parsed.path).name)
    except Exception:
        candidate = ""
    if candidate and "." in candidate:
        return candidate
    return f"{default_stem}{default_ext}"


# --- delivery ---------------------------------------------------------------


async def _send_photo(
    bot: Bot,
    chat_id: str,
    file: InputFile,
    preview: InputFile,
    parse_mode: ParseMode,
) -> str | None:
    try:
        await bot.send_photo(
            chat_id=chat_id, file=file, preview=preview, parse_mode=parse_mode,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("send_photo failed: %s", exc)
        return type(exc).__name__
    return None


async def _send_document(
    bot: Bot,
    chat_id: str,
    file: InputFile,
    _fallback_name: str | None,
    parse_mode: ParseMode,
) -> str | None:
    try:
        await bot.send_document(chat_id=chat_id, file=file, parse_mode=parse_mode)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("send_document failed: %s", exc)
        return type(exc).__name__
    return None
