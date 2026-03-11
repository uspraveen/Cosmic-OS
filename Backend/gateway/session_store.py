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
                    request_id TEXT,
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

                CREATE INDEX IF NOT EXISTS idx_messages_session_request_role
                    ON messages(session_id, request_id, role, created_at);

                CREATE TABLE IF NOT EXISTS channel_links (
                    channel TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    greeting_state TEXT NOT NULL DEFAULT 'new',
                    metadata_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_channel_links_platform
                    ON channel_links(platform, last_seen_at);

                CREATE TABLE IF NOT EXISTS memory_episode_links (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new',
                    memory_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_episode_links_session
                    ON memory_episode_links(session_id, updated_at);

                CREATE TABLE IF NOT EXISTS task_summary_links (
                    task_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new',
                    memory_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_summary_links_session
                    ON task_summary_links(session_id, updated_at);
                """
            )
            self._ensure_column(connection, "messages", "request_id", "TEXT")
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
        request_id = None
        if isinstance(metadata, dict):
            value = metadata.get("request_id")
            if value is not None:
                request_id = str(value).strip() or None

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
                    request_id,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    route,
                    request_id,
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
                    request_id,
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
                    "request_id": row["request_id"],
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
                    request_id,
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
                    "request_id": row["request_id"],
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
                    request_id,
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
            "request_id": row["request_id"],
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

    def find_message_by_request_id(
        self,
        session_id: str,
        *,
        request_id: str,
        role: str,
    ) -> dict[str, Any] | None:
        if not session_id or not request_id or role not in {"user", "assistant"}:
            return None

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    message_id,
                    role,
                    content,
                    route,
                    request_id,
                    awaiting_reply,
                    channel,
                    created_at,
                    metadata_json
                FROM messages
                WHERE session_id = ?
                  AND role = ?
                  AND request_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, role, request_id),
            ).fetchone()

        if row is None:
            return None

        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        return {
            "message_id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            "route": row["route"],
            "request_id": row["request_id"],
            "awaiting_reply": bool(row["awaiting_reply"]),
            "channel": row["channel"],
            "created_at": row["created_at"],
            "metadata": metadata,
        }

    def claim_memory_episode_ingest(
        self,
        *,
        request_id: str,
        session_id: str,
        stale_after_seconds: int = 900,
    ) -> bool:
        if not request_id or not session_id:
            return False

        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, updated_at
                FROM memory_episode_links
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

            if row is None:
                connection.execute(
                    """
                    INSERT INTO memory_episode_links (
                        request_id,
                        session_id,
                        state,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 'ingesting', ?, ?)
                    """,
                    (request_id, session_id, now, now),
                )
                connection.commit()
                return True

            state = str(row["state"] or "").strip().lower()
            if state == "ingested":
                return False

            if state == "ingesting":
                updated_at = self._parse_utc(row["updated_at"])
                if updated_at is not None:
                    age_seconds = (self._parse_utc(now) - updated_at).total_seconds()
                    if age_seconds < max(1, stale_after_seconds):
                        return False

            connection.execute(
                """
                UPDATE memory_episode_links
                SET session_id = ?,
                    state = 'ingesting',
                    updated_at = ?,
                    last_error = NULL
                WHERE request_id = ?
                """,
                (session_id, now, request_id),
            )
            connection.commit()
            return True

    def mark_memory_episode_ingested(self, request_id: str, *, memory_id: str | None = None) -> None:
        if not request_id:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_episode_links
                SET state = 'ingested',
                    memory_id = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (memory_id, utcnow_iso(), request_id),
            )
            connection.commit()

    def release_memory_episode_ingest_claim(self, request_id: str, *, error_text: str | None = None) -> None:
        if not request_id:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_episode_links
                SET state = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (error_text, utcnow_iso(), request_id),
            )
            connection.commit()

    def claim_task_summary_write(
        self,
        *,
        task_id: str,
        request_id: str,
        session_id: str,
        stale_after_seconds: int = 900,
    ) -> bool:
        if not task_id or not request_id or not session_id:
            return False

        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, updated_at
                FROM task_summary_links
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()

            if row is None:
                connection.execute(
                    """
                    INSERT INTO task_summary_links (
                        task_id,
                        request_id,
                        session_id,
                        state,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'writing', ?, ?)
                    """,
                    (task_id, request_id, session_id, now, now),
                )
                connection.commit()
                return True

            state = str(row["state"] or "").strip().lower()
            if state == "written":
                return False

            if state == "writing":
                updated_at = self._parse_utc(row["updated_at"])
                if updated_at is not None:
                    age_seconds = (self._parse_utc(now) - updated_at).total_seconds()
                    if age_seconds < max(1, stale_after_seconds):
                        return False

            connection.execute(
                """
                UPDATE task_summary_links
                SET request_id = ?,
                    session_id = ?,
                    state = 'writing',
                    updated_at = ?,
                    last_error = NULL
                WHERE task_id = ?
                """,
                (request_id, session_id, now, task_id),
            )
            connection.commit()
            return True

    def mark_task_summary_written(self, task_id: str, *, memory_id: str | None = None) -> None:
        if not task_id:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE task_summary_links
                SET state = 'written',
                    memory_id = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (memory_id, utcnow_iso(), task_id),
            )
            connection.commit()

    def release_task_summary_write_claim(self, task_id: str, *, error_text: str | None = None) -> None:
        if not task_id:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE task_summary_links
                SET state = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (error_text, utcnow_iso(), task_id),
            )
            connection.commit()

    def list_rollover_candidates(self, *, current_session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    session_id,
                    created_at,
                    updated_at,
                    compacted_summary,
                    metadata_json
                FROM sessions
                WHERE session_id != ?
                ORDER BY created_at ASC
                """,
                (current_session_id,),
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            if not isinstance(metadata, dict):
                metadata = {}
            summary_status = str(metadata.get("summary_status") or "").strip()
            if metadata.get("rollover_finalized_at") and summary_status not in {
                "summary_failed",
                "memory_write_failed",
            }:
                continue
            candidates.append(
                {
                    "session_id": row["session_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "compacted_summary": row["compacted_summary"],
                    "metadata": metadata,
                }
            )
        return candidates

    def mark_session_rollover_finalized(
        self,
        session_id: str,
        *,
        transcript_path: str | None = None,
        summary_memory_id: str | None = None,
        summary_status: str | None = None,
        compacted_summary: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["rollover_finalized_at"] = utcnow_iso()
            if transcript_path:
                metadata["transcript_path"] = transcript_path
            if summary_memory_id:
                metadata["summary_memory_id"] = summary_memory_id
            if summary_status:
                metadata["summary_status"] = summary_status
            connection.execute(
                """
                UPDATE sessions
                SET metadata_json = ?,
                    compacted_summary = COALESCE(?, compacted_summary),
                    updated_at = ?
                WHERE session_id = ?
                """,
                (json.dumps(metadata), compacted_summary, utcnow_iso(), session_id),
            )
            connection.commit()

    def claim_channel_greeting(
        self,
        *,
        channel: str,
        platform: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not channel or not platform:
            return False

        now = utcnow_iso()
        metadata_json = json.dumps(metadata) if metadata is not None else None

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO channel_links (
                    channel,
                    platform,
                    first_seen_at,
                    last_seen_at,
                    greeting_state,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, 'new', ?)
                ON CONFLICT(channel) DO UPDATE SET
                    platform = excluded.platform,
                    last_seen_at = excluded.last_seen_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    channel,
                    platform,
                    now,
                    now,
                    metadata_json,
                ),
            )
            updated = connection.execute(
                """
                UPDATE channel_links
                SET greeting_state = 'sending',
                    last_seen_at = ?,
                    metadata_json = ?
                WHERE channel = ?
                  AND greeting_state = 'new'
                """,
                (
                    now,
                    metadata_json,
                    channel,
                ),
            )
            connection.commit()
            return updated.rowcount > 0

    def mark_channel_greeting_sent(self, channel: str) -> None:
        if not channel:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE channel_links
                SET greeting_state = 'sent',
                    last_seen_at = ?
                WHERE channel = ?
                """,
                (utcnow_iso(), channel),
            )
            connection.commit()

    def release_channel_greeting_claim(self, channel: str) -> None:
        if not channel:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE channel_links
                SET greeting_state = 'new',
                    last_seen_at = ?
                WHERE channel = ?
                  AND greeting_state = 'sending'
                """,
                (utcnow_iso(), channel),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        columns = {
            str(row["name"] or "").strip()
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

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

    def _parse_utc(self, raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
