from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..session_store import utcnow_iso

_MAX_PREVIEW_CHARS = 400
_MAX_JSON_CHARS = 8_000


def _truncate_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
    return _truncate_text(rendered, max_chars=_MAX_JSON_CHARS)


class MemoryWriteAuditStore:
    """SQLite-backed audit ledger for durable long-term memory writes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS memory_write_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    write_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    writer_id TEXT,
                    request_id TEXT,
                    session_id TEXT,
                    task_id TEXT,
                    channel TEXT,
                    source_kind TEXT,
                    source_id TEXT,
                    memory_id TEXT,
                    title TEXT,
                    original_kind TEXT,
                    normalized_kind TEXT,
                    canonical_key TEXT,
                    content_hash TEXT,
                    content_preview TEXT,
                    tags_json TEXT,
                    metadata_json TEXT,
                    provenance_json TEXT,
                    response_json TEXT,
                    deduplicated INTEGER NOT NULL DEFAULT 0,
                    rate_limited INTEGER NOT NULL DEFAULT 0,
                    guard_applied INTEGER NOT NULL DEFAULT 0,
                    indexed INTEGER,
                    error_text TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_created
                    ON memory_write_audit(created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_request
                    ON memory_write_audit(request_id);

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_session
                    ON memory_write_audit(session_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_task
                    ON memory_write_audit(task_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_writer
                    ON memory_write_audit(writer_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_write_audit_hash
                    ON memory_write_audit(writer_id, content_hash, created_at DESC);
                """
            )
            connection.commit()

    def append(
        self,
        *,
        operation: str,
        write_source: str,
        status: str,
        writer_id: str | None,
        request_id: str | None,
        session_id: str | None,
        task_id: str | None,
        channel: str | None,
        source_kind: str | None,
        source_id: str | None,
        memory_id: str | None,
        title: str | None,
        original_kind: str | None,
        normalized_kind: str | None,
        canonical_key: str | None,
        content_hash: str | None,
        content_preview: str | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
        provenance: dict[str, Any] | None,
        response: dict[str, Any] | None,
        deduplicated: bool,
        rate_limited: bool,
        guard_applied: bool,
        indexed: bool | None,
        error_text: str | None,
    ) -> None:
        created_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_write_audit (
                    operation,
                    write_source,
                    status,
                    writer_id,
                    request_id,
                    session_id,
                    task_id,
                    channel,
                    source_kind,
                    source_id,
                    memory_id,
                    title,
                    original_kind,
                    normalized_kind,
                    canonical_key,
                    content_hash,
                    content_preview,
                    tags_json,
                    metadata_json,
                    provenance_json,
                    response_json,
                    deduplicated,
                    rate_limited,
                    guard_applied,
                    indexed,
                    error_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation,
                    write_source,
                    status,
                    writer_id,
                    request_id,
                    session_id,
                    task_id,
                    channel,
                    source_kind,
                    source_id,
                    memory_id,
                    title,
                    original_kind,
                    normalized_kind,
                    canonical_key,
                    content_hash,
                    _truncate_text(content_preview, max_chars=_MAX_PREVIEW_CHARS),
                    _json_dumps(tags or []),
                    _json_dumps(metadata),
                    _json_dumps(provenance),
                    _json_dumps(response),
                    1 if deduplicated else 0,
                    1 if rate_limited else 0,
                    1 if guard_applied else 0,
                    None if indexed is None else (1 if indexed else 0),
                    _truncate_text(error_text, max_chars=_MAX_JSON_CHARS),
                    created_at,
                ),
            )
            connection.commit()

    def count_recent_guarded_entries(
        self,
        *,
        writer_id: str,
        since_created_at: str,
    ) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM memory_write_audit
                WHERE writer_id = ?
                  AND guard_applied = 1
                  AND status = 'saved'
                  AND rate_limited = 0
                  AND created_at >= ?
                """,
                (writer_id, since_created_at),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def find_recent_duplicate(
        self,
        *,
        writer_id: str,
        content_hash: str,
        since_created_at: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    operation,
                    write_source,
                    status,
                    writer_id,
                    request_id,
                    session_id,
                    task_id,
                    channel,
                    source_kind,
                    source_id,
                    memory_id,
                    title,
                    original_kind,
                    normalized_kind,
                    canonical_key,
                    content_hash,
                    content_preview,
                    tags_json,
                    metadata_json,
                    provenance_json,
                    response_json,
                    deduplicated,
                    rate_limited,
                    guard_applied,
                    indexed,
                    error_text,
                    created_at
                FROM memory_write_audit
                WHERE writer_id = ?
                  AND content_hash = ?
                  AND created_at >= ?
                  AND (status = 'saved' OR status = 'deduplicated')
                  AND memory_id IS NOT NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (writer_id, content_hash, since_created_at),
            ).fetchone()
        if row is None:
            return None
        return self._deserialize_row(row)

    def list_entries(
        self,
        *,
        limit: int = 50,
        request_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        writer_id: str | None = None,
        operation: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if writer_id:
            clauses.append("writer_id = ?")
            params.append(writer_id)
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    operation,
                    write_source,
                    status,
                    writer_id,
                    request_id,
                    session_id,
                    task_id,
                    channel,
                    source_kind,
                    source_id,
                    memory_id,
                    title,
                    original_kind,
                    normalized_kind,
                    canonical_key,
                    content_hash,
                    content_preview,
                    tags_json,
                    metadata_json,
                    provenance_json,
                    response_json,
                    deduplicated,
                    rate_limited,
                    guard_applied,
                    indexed,
                    error_text,
                    created_at
                FROM memory_write_audit
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, max(1, limit)),
            ).fetchall()

        return [self._deserialize_row(row) for row in rows]

    def _deserialize_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation": row["operation"],
            "write_source": row["write_source"],
            "status": row["status"],
            "writer_id": row["writer_id"],
            "request_id": row["request_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "channel": row["channel"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "memory_id": row["memory_id"],
            "title": row["title"],
            "original_kind": row["original_kind"],
            "normalized_kind": row["normalized_kind"],
            "canonical_key": row["canonical_key"],
            "content_hash": row["content_hash"],
            "content_preview": row["content_preview"],
            "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            "provenance": json.loads(row["provenance_json"]) if row["provenance_json"] else None,
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "deduplicated": bool(row["deduplicated"]),
            "rate_limited": bool(row["rate_limited"]),
            "guard_applied": bool(row["guard_applied"]),
            "indexed": None if row["indexed"] is None else bool(row["indexed"]),
            "error_text": row["error_text"],
            "created_at": row["created_at"],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
