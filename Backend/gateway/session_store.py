from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionStore:
    """Small SQLite-backed store for shared daily session history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    compaction_count INTEGER NOT NULL DEFAULT 0,
                    compacted_summary TEXT,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    route TEXT,
                    awaiting_reply INTEGER NOT NULL DEFAULT 0,
                    channel TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_messages_session_channel_created
                    ON messages(session_id, channel, created_at);

                CREATE INDEX IF NOT EXISTS idx_messages_awaiting_reply
                    ON messages(session_id, channel, awaiting_reply, created_at);
                """
            )
            connection.commit()

    def current_session_id(self, now: datetime | None = None) -> str:
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        shifted = current - timedelta(hours=4)
        return f"sess_{shifted.strftime('%Y%m%d')}"

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        route: str | None = None,
        awaiting_reply: bool = False,
        channel: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if not content:
            raise ValueError("Message content cannot be empty")

        created_at = utcnow_iso()
        message_id = f"msg_{uuid4().hex}"
        metadata_json = json.dumps(metadata) if metadata is not None else None

        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=created_at)
            connection.execute(
                """
                INSERT INTO messages (
                    message_id,
                    session_id,
                    role,
                    content,
                    route,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    route,
                    1 if awaiting_reply else 0,
                    channel,
                    created_at,
                    metadata_json,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (created_at, session_id),
            )
            connection.commit()
        return message_id

    def get_history_tail(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    message_id,
                    role,
                    content,
                    route,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()

        history: list[dict[str, Any]] = []
        for row in reversed(rows):
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
            history.append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "route": row["route"],
                    "awaiting_reply": bool(row["awaiting_reply"]),
                    "channel": row["channel"],
                    "created_at": row["created_at"],
                    "metadata": metadata,
                }
            )
        return history

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    message_id,
                    role,
                    content,
                    route,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()

        history: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
            history.append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "route": row["route"],
                    "awaiting_reply": bool(row["awaiting_reply"]),
                    "channel": row["channel"],
                    "created_at": row["created_at"],
                    "metadata": metadata,
                }
            )
        return history

    def get_pruned_history(
        self,
        session_id: str,
        *,
        max_messages: int = 40,
        max_chars: int = 48000,
    ) -> list[dict[str, Any]]:
        history = self.get_history(session_id)
        if not history:
            return []

        window = history[-max(1, max_messages) :]
        selected: list[dict[str, Any]] = []
        consumed_chars = 0
        for item in reversed(window):
            content = str(item.get("content") or "")
            if not content:
                continue
            content_len = len(content)
            if selected and consumed_chars + content_len > max_chars:
                break
            consumed_chars += content_len
            selected.append(item)
        return list(reversed(selected))

    def list_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            session_rows = connection.execute(
                """
                SELECT
                    session_id,
                    created_at,
                    updated_at,
                    compacted_summary
                FROM sessions
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()

            sessions: list[dict[str, Any]] = []
            for row in session_rows:
                first_message = connection.execute(
                    """
                    SELECT content
                    FROM messages
                    WHERE session_id = ?
                      AND role = 'user'
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (row["session_id"],),
                ).fetchone()

                title_source = ""
                if first_message is not None:
                    title_source = str(first_message["content"] or "").strip()
                if not title_source:
                    title_source = str(row["compacted_summary"] or "").strip()
                if not title_source:
                    title_source = row["session_id"]

                sessions.append(
                    {
                        "id": row["session_id"],
                        "title": self._title_for_session(title_source),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
        return sessions

    def get_last_awaiting_reply(self, session_id: str, channel: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    message_id,
                    role,
                    content,
                    route,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                FROM messages
                WHERE session_id = ?
                  AND channel = ?
                  AND role = 'assistant'
                  AND awaiting_reply = 1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, channel),
            ).fetchone()

        if row is None:
            return None

        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        return {
            "message_id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            "route": row["route"],
            "awaiting_reply": bool(row["awaiting_reply"]),
            "channel": row["channel"],
            "created_at": row["created_at"],
            "metadata": metadata,
        }

    def clear_awaiting_reply(self, message_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE messages SET awaiting_reply = 0 WHERE message_id = ?",
                (message_id,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_session(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO sessions (
                session_id,
                user_id,
                created_at,
                updated_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, "local-user", created_at, created_at, None),
        )

    def _title_for_session(self, content: str, limit: int = 80) -> str:
        normalized = " ".join(str(content or "").strip().split())
        if not normalized:
            return "Untitled Session"
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3].rstrip()}..."
