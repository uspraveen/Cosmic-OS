from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared import estimate_text_tokens


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

                CREATE TABLE IF NOT EXISTS turn_ledger (
                    turn_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    task_id TEXT,
                    channel TEXT NOT NULL,
                    route TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    user_message_id TEXT,
                    assistant_message_id TEXT,
                    user_goal TEXT NOT NULL,
                    user_message_excerpt TEXT NOT NULL,
                    assistant_outcome TEXT NOT NULL,
                    compact_line TEXT NOT NULL,
                    facts_learned_json TEXT,
                    preferences_detected_json TEXT,
                    decisions_made_json TEXT,
                    accomplished_json TEXT,
                    tool_summary_json TEXT,
                    touched_entities_json TEXT,
                    task_refs_json TEXT,
                    artifact_refs_json TEXT,
                    failures_to_avoid_json TEXT,
                    open_loops_json TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_turn_ledger_session_completed
                    ON turn_ledger(session_id, completed_at);

                CREATE TABLE IF NOT EXISTS task_notebooks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_state TEXT,
                    notebook_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_task_notebooks_session_updated
                    ON task_notebooks(session_id, updated_at);

                CREATE TABLE IF NOT EXISTS task_input_requests (
                    input_request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    agent TEXT,
                    question TEXT NOT NULL,
                    options_json TEXT,
                    status TEXT NOT NULL,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    responded_at TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_task_input_requests_session
                    ON task_input_requests(session_id, updated_at);

                CREATE INDEX IF NOT EXISTS idx_task_input_requests_channel_status
                    ON task_input_requests(channel, status, updated_at);
                """
            )
            self._ensure_column(connection, "messages", "request_id", "TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_session_request_role
                    ON messages(session_id, request_id, role, created_at)
                """
            )
            connection.commit()

    def current_session_id(
        self,
        now: datetime | None = None,
        *,
        timezone_name: str | None = None,
        reset_hour: int = 4,
    ) -> str:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        resolved = current
        if timezone_name:
            try:
                resolved = current.astimezone(ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError:
                resolved = current.astimezone()
        else:
            resolved = current.astimezone()
        shifted = resolved - timedelta(hours=max(0, min(23, reset_hour)))
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
        max_messages: int | None = 40,
        max_chars: int | None = 48000,
        max_approx_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        history = self.get_history(session_id)
        if not history:
            return []

        if max_messages is None:
            window = history
        else:
            window = history[-max(1, max_messages) :]
        selected: list[dict[str, Any]] = []
        consumed_chars = 0
        consumed_tokens = 0
        for item in reversed(window):
            content = str(item.get("content") or "")
            if not content:
                continue
            content_len = len(content)
            content_tokens = estimate_text_tokens(content)
            if selected and max_chars is not None and consumed_chars + content_len > max_chars:
                break
            if selected and max_approx_tokens is not None and consumed_tokens + content_tokens > max_approx_tokens:
                break
            consumed_chars += content_len
            consumed_tokens += content_tokens
            selected.append(item)
        selected_history = list(reversed(selected))
        session_record = self.get_session_record(session_id)
        compacted_summary = ""
        if isinstance(session_record, dict):
            compacted_summary = str(session_record.get("compacted_summary") or "").strip()
        if compacted_summary:
            summary_content = f"[Compacted session summary]\n\n{compacted_summary}"
            summary_chars = len(summary_content)
            summary_tokens = estimate_text_tokens(summary_content)
            while selected_history:
                next_content = str(selected_history[0].get("content") or "")
                would_exceed_chars = (
                    max_chars is not None and consumed_chars + summary_chars > max_chars
                )
                would_exceed_tokens = (
                    max_approx_tokens is not None and consumed_tokens + summary_tokens > max_approx_tokens
                )
                if not would_exceed_chars and not would_exceed_tokens:
                    break
                removed = selected_history.pop(0)
                removed_content = str(removed.get("content") or "")
                consumed_chars = max(0, consumed_chars - len(removed_content))
                consumed_tokens = max(0, consumed_tokens - estimate_text_tokens(removed_content))
            consumed_chars += summary_chars
            consumed_tokens += summary_tokens
            selected_history = [
                {
                    "message_id": f"compacted_{session_id}",
                    "role": "assistant",
                    "content": summary_content,
                    "route": "system",
                    "request_id": None,
                    "awaiting_reply": False,
                    "channel": None,
                    "created_at": session_record.get("updated_at") if session_record else utcnow_iso(),
                    "metadata": {"compacted_summary": True},
                },
                *selected_history,
            ]
        return selected_history

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

    def get_session_record(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    session_id,
                    user_id,
                    created_at,
                    updated_at,
                    compaction_count,
                    compacted_summary,
                    metadata_json
                FROM sessions
                WHERE session_id = ?
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()

        if row is None:
            return None
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "compaction_count": int(row["compaction_count"] or 0),
            "compacted_summary": row["compacted_summary"],
            "metadata": metadata,
        }

    def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        record = self.get_session_record(session_id)
        if record is None:
            return {}
        metadata = record.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    def update_session_metadata(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not session_id:
            return {}
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=now)
            row = connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            metadata = json.loads(row["metadata_json"]) if row and row["metadata_json"] else {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(dict(patch or {}))
            connection.execute(
                """
                UPDATE sessions
                SET metadata_json = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (json.dumps(metadata), now, session_id),
            )
            connection.commit()
        return metadata

    def set_compaction_state(
        self,
        session_id: str,
        *,
        compacted_summary: str,
        compaction_packet: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=now)
            row = connection.execute(
                """
                SELECT compaction_count, metadata_json
                FROM sessions
                WHERE session_id = ?
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            metadata = json.loads(row["metadata_json"]) if row and row["metadata_json"] else {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["compaction_packet"] = compaction_packet
            metadata["compaction_updated_at"] = now
            connection.execute(
                """
                UPDATE sessions
                SET compacted_summary = ?,
                    compaction_count = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    compacted_summary,
                    int(row["compaction_count"] or 0) + 1 if row else 1,
                    json.dumps(metadata),
                    now,
                    session_id,
                ),
            )
            connection.commit()

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

    def list_awaiting_reply_messages(self, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
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
                  AND role = 'assistant'
                  AND awaiting_reply = 1
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()

        awaiting_messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
            awaiting_messages.append(
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
        return awaiting_messages

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

    def list_turn_ledger(
        self,
        session_id: str,
        *,
        limit: int = 20,
        before_completed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [session_id]
        where_clause = "WHERE session_id = ?"
        if before_completed_at:
            where_clause += " AND completed_at < ?"
            params.append(before_completed_at)
        params.append(max(1, limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM turn_ledger
                {where_clause}
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._deserialize_turn_ledger_row(row) for row in reversed(rows)]

    def list_all_turn_ledger(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM turn_ledger
                WHERE session_id = ?
                ORDER BY completed_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._deserialize_turn_ledger_row(row) for row in rows]

    def get_turn_ledger_entry(self, request_id: str) -> dict[str, Any] | None:
        if not request_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM turn_ledger
                WHERE request_id = ?
                LIMIT 1
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return self._deserialize_turn_ledger_row(row)

    def upsert_turn_ledger_entry(self, entry: dict[str, Any]) -> None:
        request_id = self._normalize_optional_text(entry.get("request_id"))
        session_id = self._normalize_optional_text(entry.get("session_id"))
        if not request_id or not session_id:
            raise ValueError("Turn ledger entry requires request_id and session_id")

        now = utcnow_iso()
        created_at = self._normalize_optional_text(entry.get("started_at")) or now
        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=created_at)
            connection.execute(
                """
                INSERT INTO turn_ledger (
                    turn_id,
                    request_id,
                    session_id,
                    task_id,
                    channel,
                    route,
                    started_at,
                    completed_at,
                    user_message_id,
                    assistant_message_id,
                    user_goal,
                    user_message_excerpt,
                    assistant_outcome,
                    compact_line,
                    facts_learned_json,
                    preferences_detected_json,
                    decisions_made_json,
                    accomplished_json,
                    tool_summary_json,
                    touched_entities_json,
                    task_refs_json,
                    artifact_refs_json,
                    failures_to_avoid_json,
                    open_loops_json,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    channel = excluded.channel,
                    route = excluded.route,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    user_message_id = excluded.user_message_id,
                    assistant_message_id = excluded.assistant_message_id,
                    user_goal = excluded.user_goal,
                    user_message_excerpt = excluded.user_message_excerpt,
                    assistant_outcome = excluded.assistant_outcome,
                    compact_line = excluded.compact_line,
                    facts_learned_json = excluded.facts_learned_json,
                    preferences_detected_json = excluded.preferences_detected_json,
                    decisions_made_json = excluded.decisions_made_json,
                    accomplished_json = excluded.accomplished_json,
                    tool_summary_json = excluded.tool_summary_json,
                    touched_entities_json = excluded.touched_entities_json,
                    task_refs_json = excluded.task_refs_json,
                    artifact_refs_json = excluded.artifact_refs_json,
                    failures_to_avoid_json = excluded.failures_to_avoid_json,
                    open_loops_json = excluded.open_loops_json,
                    metadata_json = excluded.metadata_json
                """,
                (
                    self._normalize_optional_text(entry.get("turn_id")) or f"turn_{uuid4().hex}",
                    request_id,
                    session_id,
                    self._normalize_optional_text(entry.get("task_id")),
                    self._normalize_optional_text(entry.get("channel")) or "",
                    self._normalize_optional_text(entry.get("route")) or "opus",
                    created_at,
                    self._normalize_optional_text(entry.get("completed_at")) or now,
                    self._normalize_optional_text(entry.get("user_message_id")),
                    self._normalize_optional_text(entry.get("assistant_message_id")),
                    str(entry.get("user_goal") or ""),
                    str(entry.get("user_message_excerpt") or ""),
                    str(entry.get("assistant_outcome") or ""),
                    str(entry.get("compact_line") or ""),
                    self._json(entry.get("facts_learned")),
                    self._json(entry.get("preferences_detected")),
                    self._json(entry.get("decisions_made")),
                    self._json(entry.get("accomplished")),
                    self._json(entry.get("tool_summary")),
                    self._json(entry.get("touched_entities")),
                    self._json(entry.get("task_refs")),
                    self._json(entry.get("artifact_refs")),
                    self._json(entry.get("failures_to_avoid")),
                    self._json(entry.get("open_loops")),
                    self._json(entry.get("metadata")),
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            connection.commit()

    def get_task_notebook(self, task_id: str) -> dict[str, Any] | None:
        if not task_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT notebook_json
                FROM task_notebooks
                WHERE task_id = ?
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        notebook = json.loads(row["notebook_json"]) if row["notebook_json"] else {}
        return notebook if isinstance(notebook, dict) else None

    def list_task_notebooks(self, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT notebook_json
                FROM task_notebooks
                WHERE session_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()
        notebooks: list[dict[str, Any]] = []
        for row in reversed(rows):
            notebook = json.loads(row["notebook_json"]) if row["notebook_json"] else {}
            if isinstance(notebook, dict):
                notebooks.append(notebook)
        return notebooks

    def upsert_task_notebook(self, task_id: str, session_id: str, notebook: dict[str, Any]) -> None:
        if not task_id or not session_id:
            raise ValueError("Task notebook requires task_id and session_id")
        now = utcnow_iso()
        created_at = self._normalize_optional_text(notebook.get("created_at")) or now
        payload = dict(notebook)
        payload.setdefault("task_id", task_id)
        payload.setdefault("status", "active")
        payload.setdefault("current_state", "")
        payload["updated_at"] = now
        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=created_at)
            connection.execute(
                """
                INSERT INTO task_notebooks (
                    task_id,
                    session_id,
                    status,
                    current_state,
                    notebook_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    status = excluded.status,
                    current_state = excluded.current_state,
                    notebook_json = excluded.notebook_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    session_id,
                    str(payload.get("status") or "active"),
                    str(payload.get("current_state") or ""),
                    json.dumps(payload),
                    created_at,
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            connection.commit()

    def upsert_task_input_request(
        self,
        *,
        input_request_id: str,
        task_id: str,
        session_id: str,
        channel: str,
        question: str,
        options: list[str] | None = None,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "pending",
        created_at: str | None = None,
    ) -> None:
        if not input_request_id or not task_id or not session_id or not channel or not question:
            raise ValueError("Task input request requires input_request_id, task_id, session_id, channel, and question")
        now = utcnow_iso()
        created = created_at or now
        with self._lock, self._connect() as connection:
            self._ensure_session(connection, session_id=session_id, created_at=created)
            connection.execute(
                """
                INSERT INTO task_input_requests (
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    agent,
                    question,
                    options_json,
                    status,
                    content,
                    created_at,
                    updated_at,
                    responded_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?)
                ON CONFLICT(input_request_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    session_id = excluded.session_id,
                    channel = excluded.channel,
                    agent = excluded.agent,
                    question = excluded.question,
                    options_json = excluded.options_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    self._normalize_optional_text(agent),
                    question,
                    self._json(options or []),
                    status,
                    created,
                    now,
                    self._json(metadata),
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            connection.commit()

    def resolve_task_input_request(
        self,
        *,
        input_request_id: str,
        content: str,
        status: str = "answered",
    ) -> dict[str, Any] | None:
        if not input_request_id:
            return None
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id, session_id, channel, question, options_json, metadata_json, agent, status
                FROM task_input_requests
                WHERE input_request_id = ?
                LIMIT 1
                """,
                (input_request_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE task_input_requests
                SET status = ?,
                    content = ?,
                    updated_at = ?,
                    responded_at = ?
                WHERE input_request_id = ?
                """,
                (
                    status,
                    content,
                    now,
                    now,
                    input_request_id,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, row["session_id"]),
            )
            connection.commit()
        return {
            "input_request_id": input_request_id,
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "channel": row["channel"],
            "question": row["question"],
            "options": json.loads(row["options_json"]) if row["options_json"] else [],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            "agent": row["agent"],
            "status": status,
            "content": content,
            "responded_at": now,
        }

    def get_task_input_request(self, input_request_id: str) -> dict[str, Any] | None:
        if not input_request_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    question,
                    options_json,
                    metadata_json,
                    agent,
                    status,
                    content,
                    created_at,
                    updated_at,
                    responded_at
                FROM task_input_requests
                WHERE input_request_id = ?
                LIMIT 1
                """,
                (input_request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "input_request_id": row["input_request_id"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "channel": row["channel"],
            "question": row["question"],
            "options": json.loads(row["options_json"]) if row["options_json"] else [],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            "agent": row["agent"],
            "status": row["status"],
            "content": row["content"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "responded_at": row["responded_at"],
        }

    def mark_task_input_request_replied(
        self,
        *,
        input_request_id: str,
        content: str,
        status: str = "answered",
    ) -> dict[str, Any] | None:
        existing = self.get_task_input_request(input_request_id)
        if existing is None:
            return None
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE task_input_requests
                SET status = ?,
                    content = ?,
                    updated_at = ?,
                    responded_at = ?
                WHERE input_request_id = ?
                """,
                (
                    status,
                    content,
                    now,
                    now,
                    input_request_id,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, existing["session_id"]),
            )
            connection.commit()
        existing["status"] = status
        existing["content"] = content
        existing["responded_at"] = now
        existing["updated_at"] = now
        return existing

    def list_pending_task_inputs(
        self,
        *,
        session_id: str | None = None,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        params.append(max(1, limit))
        where_sql = " AND ".join(clauses)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    agent,
                    question,
                    options_json,
                    created_at,
                    updated_at,
                    metadata_json
                FROM task_input_requests
                WHERE {where_sql}
                ORDER BY created_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "input_request_id": row["input_request_id"],
                    "task_id": row["task_id"],
                    "session_id": row["session_id"],
                    "channel": row["channel"],
                    "agent": row["agent"],
                    "question": row["question"],
                    "options": json.loads(row["options_json"]) if row["options_json"] else [],
                    "status": "pending",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
                }
            )
        return items

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

    def _deserialize_turn_ledger_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "turn_id": row["turn_id"],
            "request_id": row["request_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "channel": row["channel"],
            "route": row["route"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "user_message_id": row["user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "user_goal": row["user_goal"],
            "user_message_excerpt": row["user_message_excerpt"],
            "assistant_outcome": row["assistant_outcome"],
            "compact_line": row["compact_line"],
            "facts_learned": self._json_load(row["facts_learned_json"]),
            "preferences_detected": self._json_load(row["preferences_detected_json"]),
            "decisions_made": self._json_load(row["decisions_made_json"]),
            "accomplished": self._json_load(row["accomplished_json"]),
            "tool_summary": self._json_load(row["tool_summary_json"]),
            "touched_entities": self._json_load(row["touched_entities_json"]),
            "task_refs": self._json_load(row["task_refs_json"]),
            "artifact_refs": self._json_load(row["artifact_refs_json"]),
            "failures_to_avoid": self._json_load(row["failures_to_avoid_json"]),
            "open_loops": self._json_load(row["open_loops_json"]),
            "metadata": self._json_load(row["metadata_json"], default={}),
        }

    def _json(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value)

    def _json_load(self, raw: str | None, *, default: Any | None = None) -> Any:
        if not raw:
            return [] if default is None else default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [] if default is None else default

    def _normalize_optional_text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
