from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Room:
    room_id: str
    description: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Participant:
    participant_id: str
    room_id: str
    display_name: str
    type: Literal["human", "ai"]
    joined_at: str = field(default_factory=_now)
    last_seen_at: str | None = None
    online: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Message:
    message_id: int
    room_id: str
    participant_id: str
    display_name: str
    participant_type: str
    content: str
    timestamp: str
    sequence: int
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_row(cls, row: dict) -> Message:
        meta = row.get("metadata", "{}")
        if isinstance(meta, str):
            meta = json.loads(meta) if meta else {}
        return cls(
            message_id=row["message_id"],
            room_id=row["room_id"],
            participant_id=row["participant_id"],
            display_name=row.get("display_name", row["participant_id"]),
            participant_type=row.get("participant_type", "ai"),
            content=row["content"],
            timestamp=row["timestamp"],
            sequence=row["sequence"],
            metadata=meta,
        )


@dataclass
class MessagePage:
    messages: list[Message]
    next_cursor: int
    has_more: bool

    def to_dict(self) -> dict:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }
