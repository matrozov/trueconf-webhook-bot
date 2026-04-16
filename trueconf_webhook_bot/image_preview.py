"""Image preview generation for TrueConf inline rendering.

TrueConf clients only render an image inline when a `preview` is attached
alongside the `file`. Sending the full-resolution source as its own preview
works but doubles bandwidth for every photo. This module produces a small
JPEG thumbnail from the source bytes so the file itself stays full-quality
while the preview travels cheaply.

All failures are non-fatal: the caller may fall back to using the full image
as its own preview to keep inline rendering working.
"""

from __future__ import annotations

import asyncio
import io
import logging

import httpx
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Long-side cap for the thumbnail. 320 px is enough for an in-chat preview
# card and shrinks a typical multi-megabyte phone photo to a few dozen KB.
_PREVIEW_MAX_DIMENSION = 320

# JPEG quality. 75 is the conventional "visually indistinguishable at preview
# size" default — much smaller than 85+ while staying clean to the eye.
_PREVIEW_QUALITY = 75

# Network budget for fetching the source image. Short enough to keep the
# webhook responsive, long enough for a remote CDN to answer.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Filename we attach to generated previews. Clients only use it to pick a
# decoder — the `.jpg` extension ensures the MIME guess lands on JPEG.
_PREVIEW_FILENAME = "preview.jpg"


def preview_filename() -> str:
    """Canonical filename to attach to a generated preview buffer."""
    return _PREVIEW_FILENAME


def make_thumbnail(source: bytes) -> bytes | None:
    """Encode `source` as a compact JPEG suitable for an inline preview.

    Returns the encoded bytes on success, or `None` when the input cannot
    be decoded as a recognised image or the encoder raises. All Pillow
    errors are swallowed so the caller can fall back cleanly.
    """
    try:
        with Image.open(io.BytesIO(source)) as image:
            # Honour EXIF orientation so portrait phone photos are not
            # displayed sideways after the resize.
            image = ImageOps.exif_transpose(image)
            image.thumbnail(
                (_PREVIEW_MAX_DIMENSION, _PREVIEW_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            # JPEG cannot store transparency. For RGBA/LA inputs composite
            # the image onto a white background so transparent regions do
            # not render as solid black.
            if image.mode in ("RGBA", "LA"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=_PREVIEW_QUALITY,
                optimize=True,
            )
            return buffer.getvalue()
    except Exception as exc:
        logger.info("Thumbnail generation failed: %s", exc)
        return None


async def fetch_for_preview(url: str, *, max_bytes: int) -> bytes | None:
    """Download `url` so its bytes can be fed into `make_thumbnail`.

    The body is streamed and the download is aborted as soon as it exceeds
    `max_bytes`. Redirects are NOT followed — the source URL has already
    passed SSRF validation, but a redirect target has not, so following
    redirects would open a hole in that check. Returns `None` on any
    failure (timeout, non-200, oversize, network error).
    """
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.info(
                        "Preview source %s returned HTTP %s",
                        url, response.status_code,
                    )
                    return None
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        logger.info(
                            "Preview source %s exceeded %d bytes",
                            url, max_bytes,
                        )
                        return None
                return bytes(buffer)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.info("Preview source %s fetch failed: %s", url, exc)
        return None
