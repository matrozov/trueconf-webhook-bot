"""Domain models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    """ISO-8601 string in UTC, with a Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Webhook:
    """A generated incoming webhook record.

    Fields:
        id: UUID of the record, used for internal references.
        chat_id: TrueConf chat identifier where messages are delivered.
        name: human-readable name, unique within a chat.
        token: public secret embedded in the URL and the only authentication factor.
        created_at: ISO-8601 UTC string, creation moment.
        created_by: TrueConf user id of whoever created the hook.
        last_used_at: ISO-8601 UTC of the last successful incoming request, or None.
        usage_count: total number of successful deliveries.
    """

    id: str
    chat_id: str
    name: str
    token: str
    created_at: str = field(default_factory=_utcnow_iso)
    created_by: str = ""
    last_used_at: str | None = None
    usage_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Webhook":
        """Create a record from a JSON dict. Missing optional fields fall back to defaults."""
        return cls(
            id=data["id"],
            chat_id=data["chat_id"],
            name=data["name"],
            token=data["token"],
            created_at=data.get("created_at") or _utcnow_iso(),
            created_by=data.get("created_by", ""),
            last_used_at=data.get("last_used_at"),
            usage_count=int(data.get("usage_count", 0)),
        )
