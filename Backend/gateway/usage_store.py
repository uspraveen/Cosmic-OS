from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from shared.usage import UsageEvent


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class UsageStore:
    """Append-only SQLite usage ledger for metered provider calls."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS usage_events (
                    llm_call_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    source_component TEXT NOT NULL,
                    source_id TEXT,
                    task_id TEXT,
                    plan_id TEXT,
                    parent_task_id TEXT,
                    session_id TEXT,
                    route TEXT,
                    operation TEXT NOT NULL,
                    usage_kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_id TEXT,
                    provider_request_id TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL,
                    latency_ms INTEGER,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT,
                    metadata_json TEXT,
                    llm_call_placed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_usage_task_id
                    ON usage_events(task_id);

                CREATE INDEX IF NOT EXISTS idx_usage_plan_id
                    ON usage_events(plan_id);

                CREATE INDEX IF NOT EXISTS idx_usage_session_id
                    ON usage_events(session_id);

                CREATE INDEX IF NOT EXISTS idx_usage_source
                    ON usage_events(source_component, source_id, llm_call_placed_at);

                CREATE INDEX IF NOT EXISTS idx_usage_provider_model
                    ON usage_events(provider, model, llm_call_placed_at);
                """
            )
            connection.commit()

    def append(self, event: UsageEvent) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO usage_events (
                    llm_call_id,
                    user_id,
                    source_component,
                    source_id,
                    task_id,
                    plan_id,
                    parent_task_id,
                    session_id,
                    route,
                    operation,
                    usage_kind,
                    provider,
                    model,
                    request_id,
                    provider_request_id,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cached_tokens,
                    reasoning_tokens,
                    estimated_cost_usd,
                    latency_ms,
                    success,
                    error_code,
                    metadata_json,
                    llm_call_placed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.llm_call_id,
                    event.user_id,
                    event.source_component,
                    event.source_id,
                    event.task_id,
                    event.plan_id,
                    event.parent_task_id,
                    event.session_id,
                    event.route,
                    event.operation,
                    event.usage_kind,
                    event.provider,
                    event.model,
                    event.request_id,
                    event.provider_request_id,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.total_tokens,
                    event.cached_tokens,
                    event.reasoning_tokens,
                    event.estimated_cost_usd,
                    event.latency_ms,
                    1 if event.success else 0,
                    event.error_code,
                    _json_dumps(event.metadata_json),
                    event.llm_call_placed_at,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_events,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed_events,
                    MAX(llm_call_placed_at) AS latest_call_at
                FROM usage_events
                """
            ).fetchone()
        return {
            "db_path": str(self.db_path),
            "total_events": int(row["total_events"] if row and row["total_events"] is not None else 0),
            "failed_events": int(row["failed_events"] if row and row["failed_events"] is not None else 0),
            "latest_call_at": row["latest_call_at"] if row else None,
        }

    def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM usage_events
                ORDER BY llm_call_placed_at DESC, llm_call_id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        if payload.get("metadata_json"):
            try:
                payload["metadata_json"] = json.loads(payload["metadata_json"])
            except json.JSONDecodeError:
                pass
        payload["success"] = bool(payload.get("success"))
        return payload

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

