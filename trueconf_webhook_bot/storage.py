"""Webhook storage backed by a flat JSON file.

The whole collection lives in memory. Mutating operations are serialized with
an `asyncio.Lock`. I/O is offloaded to an executor so the event loop is not
blocked.

To optimize the `last_used_at` / `usage_count` counters, a batched flush is
used: instead of re-serializing the whole file on every successful webhook,
updates accumulate in memory and are written to disk no more often than
`_TOUCH_FLUSH_INTERVAL_SEC` seconds, and no later than `_TOUCH_FLUSH_THRESHOLD`
accumulated updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from .models import Webhook, _utcnow_iso

# Batch parameters for touch(): no more than once per 2 seconds, no more than 100 pending.
_TOUCH_FLUSH_INTERVAL_SEC: float = 2.0
_TOUCH_FLUSH_THRESHOLD: int = 100


class WebhookNameConflict(ValueError):
    """A webhook with this name already exists in this chat."""


class WebhookNotFound(KeyError):
    """The requested webhook was not found."""


class WebhookStorage:
    """Persistent webhook storage."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        self._by_id: dict[str, Webhook] = {}
        self._by_token: dict[str, Webhook] = {}
        # Number of deferred touch updates and a handle on the delayed flush task.
        self._pending_touches: int = 0
        self._last_flush_monotonic: float = 0.0
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> None:
        """Load state from disk. A missing or empty file means empty storage."""
        async with self._lock:
            self._by_id.clear()
            self._by_token.clear()
            if not self._path.exists():
                return
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, self._read_text)
            if not raw.strip():
                return
            data = json.loads(raw)
            for item in data.get("webhooks", []):
                hook = Webhook.from_dict(item)
                self._by_id[hook.id] = hook
                self._by_token[hook.token] = hook
            self._last_flush_monotonic = time.monotonic()

    def _read_text(self) -> str:
        return self._path.read_text(encoding="utf-8")

    def _serialize(self) -> str:
        payload = {"webhooks": [h.to_dict() for h in self._by_id.values()]}
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)

    def _write_sync(self, content: str) -> None:
        """Synchronous atomic write. Called from an executor."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self._path)

    async def _flush_unlocked(self) -> None:
        """Serialize and write a snapshot. Must be called while holding `_lock`."""
        content = self._serialize()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_sync, content)
        self._pending_touches = 0
        self._last_flush_monotonic = time.monotonic()

    def list_by_chat(self, chat_id: str) -> list[Webhook]:
        """All webhooks for a given chat, ordered by creation time."""
        hooks = [h for h in self._by_id.values() if h.chat_id == chat_id]
        hooks.sort(key=lambda h: h.created_at)
        return hooks

    def get_by_token(self, token: str) -> Webhook | None:
        """O(1) lookup by token."""
        return self._by_token.get(token)

    def find_by_name(self, chat_id: str, name: str) -> Webhook | None:
        """Find a webhook by name within a chat; comparison is case-insensitive."""
        needle = name.strip().casefold()
        for hook in self._by_id.values():
            if hook.chat_id == chat_id and hook.name.casefold() == needle:
                return hook
        return None

    async def create(self, chat_id: str, name: str, created_by: str) -> Webhook:
        """Create a new webhook. Raises `WebhookNameConflict` on duplicate name."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Webhook name must not be empty")

        async with self._lock:
            existing = self.find_by_name(chat_id, clean_name)
            if existing is not None:
                raise WebhookNameConflict(
                    f"A webhook named «{clean_name}» already exists in this chat"
                )
            hook = Webhook(
                id=str(uuid.uuid4()),
                chat_id=chat_id,
                name=clean_name,
                token=secrets.token_urlsafe(32),
                created_by=created_by,
            )
            self._by_id[hook.id] = hook
            self._by_token[hook.token] = hook
            await self._flush_unlocked()
            return hook

    async def revoke(self, chat_id: str, name: str) -> Webhook:
        """Revoke a webhook by name. Raises `WebhookNotFound` if it doesn't exist."""
        async with self._lock:
            hook = self.find_by_name(chat_id, name)
            if hook is None:
                raise WebhookNotFound(
                    f"No webhook named «{name.strip()}» in this chat"
                )
            self._by_id.pop(hook.id, None)
            self._by_token.pop(hook.token, None)
            await self._flush_unlocked()
            return hook

    async def touch(self, token: str) -> Webhook | None:
        """Record a successful delivery.

        The in-memory update is immediate. The on-disk write is deferred
        (batched): at most once per `_TOUCH_FLUSH_INTERVAL_SEC` seconds, or
        after `_TOUCH_FLUSH_THRESHOLD` pending updates. This matters because
        at 60+ RPS per token we must not rewrite the entire JSON on every POST.
        """
        async with self._lock:
            hook = self._by_token.get(token)
            if hook is None:
                return None
            hook.last_used_at = _utcnow_iso()
            hook.usage_count += 1
            self._pending_touches += 1

            if self._pending_touches >= _TOUCH_FLUSH_THRESHOLD:
                await self._flush_unlocked()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())
            return hook

    async def _delayed_flush(self) -> None:
        """Background task: wait for the batch interval and then flush."""
        try:
            await asyncio.sleep(_TOUCH_FLUSH_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        async with self._lock:
            if self._pending_touches > 0:
                await self._flush_unlocked()

    async def flush_pending(self) -> None:
        """Force pending counters to be written to disk (for graceful shutdown)."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        async with self._lock:
            if self._pending_touches > 0:
                await self._flush_unlocked()

    def __iter__(self) -> Iterator[Webhook]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
