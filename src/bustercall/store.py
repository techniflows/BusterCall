from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone

from bustercall.models import Room, Participant, Message, MessagePage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id     TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id  TEXT NOT NULL,
    room_id         TEXT NOT NULL REFERENCES rooms(room_id),
    display_name    TEXT NOT NULL,
    type            TEXT CHECK(type IN ('human', 'ai')) NOT NULL,
    joined_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at    TEXT,
    online          INTEGER DEFAULT 1,
    PRIMARY KEY (participant_id, room_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         TEXT NOT NULL REFERENCES rooms(room_id),
    participant_id  TEXT NOT NULL,
    content         TEXT NOT NULL,
    metadata        TEXT DEFAULT '{}',
    timestamp       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sequence        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages(room_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room_id, message_id);
"""


class MessageStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._seq_counters: dict[str, int] = {}
        self._conn = self._connect()
        self._init_schema()
        self._load_sequences()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _load_sequences(self) -> None:
        rows = self._conn.execute(
            "SELECT room_id, COALESCE(MAX(sequence), 0) as max_seq FROM messages GROUP BY room_id"
        ).fetchall()
        for row in rows:
            self._seq_counters[row["room_id"]] = row["max_seq"]

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # -- Rooms --

    def create_room(self, room_id: str, description: str = "") -> Room:
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO rooms (room_id, description, created_at) VALUES (?, ?, ?)",
                (room_id, description, now),
            )
            self._conn.commit()
        return Room(room_id=room_id, description=description, created_at=now)

    def list_rooms(self) -> list[dict]:
        rows = self._conn.execute("""
            SELECT r.room_id, r.description, r.created_at,
                   COUNT(DISTINCT CASE WHEN p.online = 1 THEN p.participant_id END) as participant_count
            FROM rooms r
            LEFT JOIN participants p ON r.room_id = p.room_id
            GROUP BY r.room_id
        """).fetchall()
        return [dict(row) for row in rows]

    def get_room(self, room_id: str) -> Room | None:
        row = self._conn.execute(
            "SELECT * FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if row is None:
            return None
        return Room(room_id=row["room_id"], description=row["description"], created_at=row["created_at"])

    # -- Participants --

    def join_room(
        self, room_id: str, participant_id: str, display_name: str, participant_type: str
    ) -> Participant:
        self.create_room(room_id)
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO participants (participant_id, room_id, display_name, type, joined_at, last_seen_at, online)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(participant_id, room_id) DO UPDATE SET
                     display_name = excluded.display_name,
                     online = 1,
                     last_seen_at = excluded.last_seen_at""",
                (participant_id, room_id, display_name, participant_type, now, now),
            )
            self._conn.commit()
        return Participant(
            participant_id=participant_id,
            room_id=room_id,
            display_name=display_name,
            type=participant_type,
            joined_at=now,
            online=True,
        )

    def leave_room(self, room_id: str, participant_id: str) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE participants SET online = 0, last_seen_at = ? WHERE participant_id = ? AND room_id = ?",
                (now, participant_id, room_id),
            )
            self._conn.commit()

    def list_participants(self, room_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM participants WHERE room_id = ? ORDER BY joined_at",
            (room_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_heartbeat(self, room_id: str, participant_id: str) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE participants SET last_seen_at = ? WHERE participant_id = ? AND room_id = ?",
                (now, participant_id, room_id),
            )
            self._conn.commit()

    # -- Messages --

    def add_message(
        self,
        room_id: str,
        participant_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> Message:
        self.create_room(room_id)
        now = self._now()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._lock:
            seq = self._seq_counters.get(room_id, 0) + 1
            self._seq_counters[room_id] = seq

            cursor = self._conn.execute(
                """INSERT INTO messages (room_id, participant_id, content, metadata, timestamp, sequence)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (room_id, participant_id, content, meta_json, now, seq),
            )
            message_id = cursor.lastrowid
            self._conn.commit()

        # Fetch display_name and type for the participant
        prow = self._conn.execute(
            "SELECT display_name, type FROM participants WHERE participant_id = ? AND room_id = ?",
            (participant_id, room_id),
        ).fetchone()
        display_name = prow["display_name"] if prow else participant_id
        ptype = prow["type"] if prow else "ai"

        return Message(
            message_id=message_id,
            room_id=room_id,
            participant_id=participant_id,
            display_name=display_name,
            participant_type=ptype,
            content=content,
            timestamp=now,
            sequence=seq,
            metadata=metadata or {},
        )

    def get_messages(
        self, room_id: str, after: int = 0, limit: int = 100
    ) -> MessagePage:
        rows = self._conn.execute(
            """SELECT m.*, p.display_name, p.type as participant_type
               FROM messages m
               LEFT JOIN participants p ON m.participant_id = p.participant_id AND m.room_id = p.room_id
               WHERE m.room_id = ? AND m.message_id > ?
               ORDER BY m.message_id ASC
               LIMIT ?""",
            (room_id, after, limit + 1),
        ).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]

        messages = [Message.from_row(dict(row)) for row in rows]
        next_cursor = messages[-1].message_id if messages else after

        return MessagePage(messages=messages, next_cursor=next_cursor, has_more=has_more)

    def get_recent_messages(self, room_id: str, limit: int = 20) -> list[Message]:
        rows = self._conn.execute(
            """SELECT m.*, p.display_name, p.type as participant_type
               FROM messages m
               LEFT JOIN participants p ON m.participant_id = p.participant_id AND m.room_id = p.room_id
               WHERE m.room_id = ?
               ORDER BY m.message_id DESC
               LIMIT ?""",
            (room_id, limit),
        ).fetchall()
        messages = [Message.from_row(dict(row)) for row in reversed(rows)]
        return messages

    def clear_messages(self, room_id: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE room_id = ?", (room_id,)
            )
            count = cursor.rowcount
            self._seq_counters[room_id] = 0
            self._conn.commit()
        return count

    def get_latest_message_id(self, room_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(message_id), 0) as max_id FROM messages WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        return row["max_id"]

    def close(self) -> None:
        self._conn.close()
