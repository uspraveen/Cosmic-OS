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

                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    original_query TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planning',
                    total_steps INTEGER NOT NULL DEFAULT 0,
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    plan_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'user',
                    source_id TEXT,
                    channel TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_plans_status
                    ON plans(status);

                CREATE INDEX IF NOT EXISTS idx_plans_session
                    ON plans(session_id);

                CREATE TABLE IF NOT EXISTS plan_steps (
                    step_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    step_number INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    intent TEXT,
                    agent_id TEXT,
                    depends_on TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    task_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    input_json TEXT,
                    output_json TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_steps_plan
                    ON plan_steps(plan_id, step_number);

                CREATE INDEX IF NOT EXISTS idx_steps_status
                    ON plan_steps(plan_id, status);

                CREATE TABLE IF NOT EXISTS task_input_requests (
                    input_request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    session_id TEXT,
                    channel TEXT,
                    agent TEXT,
                    question TEXT NOT NULL,
                    options_json TEXT,
                    status TEXT NOT NULL,
                    reply_content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    replied_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_task_input_requests_task_status
                    ON task_input_requests(task_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS task_input_waits (
                    input_request_id TEXT PRIMARY KEY,
                    waiting_task_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    recipient TEXT NOT NULL,
                    resume_intent TEXT NOT NULL,
                    resume_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resumed_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_task_input_waits_status_updated
                    ON task_input_waits(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_task_input_waits_waiting_task
                    ON task_input_waits(waiting_task_id, status);

                CREATE INDEX IF NOT EXISTS idx_task_input_waits_resumed_task
                    ON task_input_waits(resumed_task_id);

                CREATE TABLE IF NOT EXISTS reverse_task_waits (
                    reverse_task_id TEXT PRIMARY KEY,
                    waiting_task_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    reverse_intent TEXT NOT NULL,
                    target_intent TEXT,
                    target_agent_id TEXT,
                    reverse_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delegated_task_id TEXT,
                    resumed_task_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resumed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_reverse_task_waits_status_updated
                    ON reverse_task_waits(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_reverse_task_waits_waiting_task
                    ON reverse_task_waits(waiting_task_id, status);

                CREATE INDEX IF NOT EXISTS idx_reverse_task_waits_delegated_task
                    ON reverse_task_waits(delegated_task_id);

                CREATE INDEX IF NOT EXISTS idx_reverse_task_waits_resumed_task
                    ON reverse_task_waits(resumed_task_id);
                """
            )
            connection.commit()

    def create_plan(
        self,
        *,
        plan_id: str,
        session_id: str | None,
        original_query: str,
        plan_json: dict[str, Any],
        source: str = "user",
        source_id: str | None = None,
        channel: str | None = None,
        status: str = "planning",
        total_steps: int = 0,
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO plans (
                    plan_id,
                    session_id,
                    original_query,
                    status,
                    total_steps,
                    completed_steps,
                    current_step,
                    plan_json,
                    source,
                    source_id,
                    channel,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    plan_id,
                    session_id,
                    original_query,
                    status,
                    total_steps,
                    json.dumps(plan_json, ensure_ascii=False),
                    source,
                    source_id,
                    channel,
                    now,
                    now,
                ),
            )
            connection.commit()

    def create_plan_step(
        self,
        *,
        step_id: str,
        plan_id: str,
        step_number: int,
        description: str,
        intent: str | None,
        agent_id: str | None,
        depends_on: list[str] | None,
        input_json: dict[str, Any] | None,
        status: str = "pending",
        max_attempts: int = 3,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO plan_steps (
                    step_id,
                    plan_id,
                    step_number,
                    description,
                    intent,
                    agent_id,
                    depends_on,
                    status,
                    task_id,
                    attempt,
                    max_attempts,
                    input_json,
                    output_json,
                    started_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL, NULL, NULL)
                """,
                (
                    step_id,
                    plan_id,
                    step_number,
                    description,
                    intent,
                    agent_id,
                    json.dumps(depends_on or [], ensure_ascii=False),
                    status,
                    max(1, int(max_attempts)),
                    json.dumps(input_json or {}, ensure_ascii=False),
                ),
            )
            connection.commit()

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM plans
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan_json"] = json.loads(result["plan_json"]) if result.get("plan_json") else None
        return result

    def list_plan_steps(self, plan_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM plan_steps
                WHERE plan_id = ?
                ORDER BY step_number ASC
                """,
                (plan_id,),
            ).fetchall()

        steps: list[dict[str, Any]] = []
        for row in rows:
            step = dict(row)
            step["depends_on"] = json.loads(step["depends_on"]) if step.get("depends_on") else []
            step["input_json"] = json.loads(step["input_json"]) if step.get("input_json") else {}
            step["output_json"] = json.loads(step["output_json"]) if step.get("output_json") else None
            steps.append(step)
        return steps

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

    def mark_deferred(self, task_id: str, *, result: dict[str, Any]) -> None:
        updated_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'deferred',
                    result_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    completed_at = NULL
                WHERE task_id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    updated_at,
                    task_id,
                ),
            )
            connection.commit()

    def mark_suspended(self, task_id: str, *, payload: dict[str, Any] | None = None) -> None:
        updated_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'suspended',
                    result_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    completed_at = NULL
                WHERE task_id = ?
                """,
                (
                    json.dumps(payload or {}, ensure_ascii=False),
                    updated_at,
                    task_id,
                ),
            )
            connection.commit()

    def mark_resumed(self, task_id: str, *, payload: dict[str, Any] | None = None) -> None:
        updated_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    result_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    completed_at = NULL
                WHERE task_id = ?
                """,
                (
                    json.dumps(payload or {}, ensure_ascii=False),
                    updated_at,
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

    def mark_cancelled(self, task_id: str, *, message: str) -> None:
        completed_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    error_code = 'CANCELLED',
                    error_message = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE task_id = ?
                """,
                (
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
        clauses = ["status IN ('running', 'suspended')"]
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

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result.get("envelope_json"):
            result["envelope_json"] = json.loads(result["envelope_json"])
        if result.get("result_json"):
            result["result_json"] = json.loads(result["result_json"])
        return result

    def create_task_input_request(
        self,
        *,
        input_request_id: str,
        task_id: str,
        session_id: str | None,
        channel: str | None,
        agent: str | None,
        question: str,
        options: list[str] | None,
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO task_input_requests (
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    agent,
                    question,
                    options_json,
                    status,
                    reply_content,
                    created_at,
                    updated_at,
                    replied_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                """,
                (
                    input_request_id,
                    task_id,
                    session_id,
                    channel,
                    agent,
                    question,
                    json.dumps(options or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def mark_task_input_replied(self, input_request_id: str, *, content: str) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE task_input_requests
                SET status = 'answered',
                    reply_content = ?,
                    updated_at = ?,
                    replied_at = ?
                WHERE input_request_id = ?
                """,
                (
                    content,
                    now,
                    now,
                    input_request_id,
                ),
            )
            connection.commit()

    def create_task_input_wait(
        self,
        *,
        input_request_id: str,
        waiting_task_id: str,
        parent_task_id: str | None,
        recipient: str,
        resume_intent: str,
        resume_payload: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO task_input_waits (
                    input_request_id,
                    waiting_task_id,
                    parent_task_id,
                    recipient,
                    resume_intent,
                    resume_payload_json,
                    status,
                    resumed_task_id,
                    created_at,
                    updated_at,
                    resumed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                """,
                (
                    input_request_id,
                    waiting_task_id,
                    parent_task_id,
                    recipient,
                    resume_intent,
                    json.dumps(resume_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_task_input_wait(self, input_request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM task_input_waits
                WHERE input_request_id = ?
                """,
                (input_request_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["resume_payload_json"] = (
            json.loads(result["resume_payload_json"]) if result.get("resume_payload_json") else {}
        )
        return result

    def get_task_input_wait_by_resumed_task(self, resumed_task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM task_input_waits
                WHERE resumed_task_id = ?
                """,
                (resumed_task_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["resume_payload_json"] = (
            json.loads(result["resume_payload_json"]) if result.get("resume_payload_json") else {}
        )
        return result

    def mark_task_input_wait_resumed(self, input_request_id: str, *, resumed_task_id: str) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE task_input_waits
                SET status = 'resumed',
                    resumed_task_id = ?,
                    updated_at = ?,
                    resumed_at = ?
                WHERE input_request_id = ?
                """,
                (
                    resumed_task_id,
                    now,
                    now,
                    input_request_id,
                ),
            )
            connection.commit()

    def create_reverse_task_wait(
        self,
        *,
        reverse_task_id: str,
        waiting_task_id: str,
        parent_task_id: str | None,
        sender: str,
        recipient: str,
        reverse_intent: str,
        target_intent: str | None,
        target_agent_id: str | None,
        reverse_payload: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reverse_task_waits (
                    reverse_task_id,
                    waiting_task_id,
                    parent_task_id,
                    sender,
                    recipient,
                    reverse_intent,
                    target_intent,
                    target_agent_id,
                    reverse_payload_json,
                    status,
                    delegated_task_id,
                    resumed_task_id,
                    error_code,
                    error_message,
                    created_at,
                    updated_at,
                    resumed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?, ?, NULL)
                """,
                (
                    reverse_task_id,
                    waiting_task_id,
                    parent_task_id,
                    sender,
                    recipient,
                    reverse_intent,
                    target_intent,
                    target_agent_id,
                    json.dumps(reverse_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_reverse_task_wait(self, reverse_task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reverse_task_waits
                WHERE reverse_task_id = ?
                """,
                (reverse_task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_reverse_task_wait(dict(row))

    def list_pending_reverse_task_waits(self, waiting_task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM reverse_task_waits
                WHERE waiting_task_id = ?
                  AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (waiting_task_id,),
            ).fetchall()
        return [self._decode_reverse_task_wait(dict(row)) for row in rows]

    def list_open_reverse_task_waits(self, waiting_task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM reverse_task_waits
                WHERE waiting_task_id = ?
                  AND status IN ('pending', 'dispatched')
                ORDER BY created_at ASC
                """,
                (waiting_task_id,),
            ).fetchall()
        return [self._decode_reverse_task_wait(dict(row)) for row in rows]

    def get_reverse_task_wait_by_delegated_task(self, delegated_task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reverse_task_waits
                WHERE delegated_task_id = ?
                """,
                (delegated_task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_reverse_task_wait(dict(row))

    def get_reverse_task_wait_by_resumed_task(self, resumed_task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reverse_task_waits
                WHERE resumed_task_id = ?
                """,
                (resumed_task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_reverse_task_wait(dict(row))

    def mark_reverse_task_wait_dispatched(self, reverse_task_id: str, *, delegated_task_id: str) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE reverse_task_waits
                SET status = 'dispatched',
                    delegated_task_id = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE reverse_task_id = ?
                """,
                (
                    delegated_task_id,
                    now,
                    reverse_task_id,
                ),
            )
            connection.commit()

    def mark_reverse_task_wait_resumed(self, reverse_task_id: str, *, resumed_task_id: str) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE reverse_task_waits
                SET status = 'resumed',
                    resumed_task_id = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    resumed_at = ?
                WHERE reverse_task_id = ?
                """,
                (
                    resumed_task_id,
                    now,
                    now,
                    reverse_task_id,
                ),
            )
            connection.commit()

    def mark_reverse_task_wait_failed(
        self,
        reverse_task_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE reverse_task_waits
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE reverse_task_id = ?
                """,
                (
                    code,
                    message,
                    now,
                    reverse_task_id,
                ),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON;")
        connection.execute("PRAGMA busy_timeout = 5000;")
        return connection

    def _decode_reverse_task_wait(self, record: dict[str, Any]) -> dict[str, Any]:
        record["reverse_payload_json"] = (
            json.loads(record["reverse_payload_json"]) if record.get("reverse_payload_json") else {}
        )
        return record
