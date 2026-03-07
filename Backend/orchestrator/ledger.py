from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from shared import TaskEnvelope


def utcnow_iso() -> str:
    from shared import utcnow

    return utcnow().isoformat().replace("+00:00", "Z")


class TaskLedger:
    """Minimal durable task ledger for the thin Opus orchestrator."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    task_list_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    session_id TEXT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    channel TEXT,
                    idempotency_key TEXT NOT NULL,
                    request_id TEXT,
                    query TEXT,
                    envelope_json TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
                    ON tasks(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_tasks_session_channel
                    ON tasks(session_id, channel, status);
                """
            )
            connection.commit()

    def create_task(self, task: TaskEnvelope) -> None:
        payload = task.model_dump(mode="json")
        query = str(task.input.get("query") or "").strip() or None
        request_id = str(task.input.get("request_id") or "").strip() or None
        created_at = payload["created_at"]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    task_id,
                    task_list_id,
                    parent_task_id,
                    session_id,
                    sender,
                    recipient,
                    intent,
                    status,
                    priority,
                    source,
                    source_id,
                    channel,
                    idempotency_key,
                    request_id,
                    query,
                    envelope_json,
                    result_json,
                    error_code,
                    error_message,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL)
                """,
                (
                    task.task_id,
                    task.task_list_id,
                    task.parent_task_id,
                    task.session_id,
                    task.sender,
                    task.recipient,
                    task.intent,
                    "running",
                    task.priority,
                    task.source,
                    task.source_id,
                    task.channel,
                    task.idempotency_key,
                    request_id,
                    query,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            connection.commit()

    def mark_completed(self, task_id: str, *, result: dict[str, Any]) -> None:
        completed_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    result_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    completed_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    completed_at,
                    completed_at,
                    task_id,
                ),
            )
            connection.commit()

    def mark_failed(self, task_id: str, *, code: str, message: str) -> None:
        completed_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE task_id = ?
                """,
                (
                    code,
                    message,
                    completed_at,
                    completed_at,
                    task_id,
                ),
            )
            connection.commit()

    def list_active_tasks(
        self,
        *,
        session_id: str | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'running'"]
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)

        where_sql = " AND ".join(clauses)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT task_id, session_id, channel, query, status, created_at, updated_at
                FROM tasks
                WHERE {where_sql}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()

        return [
            {
                "task_id": row["task_id"],
                "session_id": row["session_id"],
                "channel": row["channel"],
                "query": row["query"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON;")
        connection.execute("PRAGMA busy_timeout = 5000;")
        return connection
