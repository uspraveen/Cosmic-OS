from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DeliveryQueueStore:
    """SQLite-backed durable queue for user-visible channel deliveries."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS delivery_queue (
                    delivery_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_queue_pending_dedupe
                    ON delivery_queue(dedupe_key)
                    WHERE status = 'pending';

                CREATE INDEX IF NOT EXISTS idx_delivery_queue_due
                    ON delivery_queue(status, available_at, updated_at);
                """
            )
            connection.commit()

    def enqueue(
        self,
        *,
        dedupe_key: str,
        channel: str,
        event_type: str,
        payload: dict[str, Any],
        available_at: str | None = None,
        last_error: str | None = None,
        attempts: int = 0,
    ) -> str:
        now = utcnow_iso()
        ready_at = available_at or now
        payload_json = json.dumps(payload, sort_keys=True)

        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT delivery_id
                FROM delivery_queue
                WHERE dedupe_key = ?
                  AND status = 'pending'
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                delivery_id = str(existing["delivery_id"])
                connection.execute(
                    """
                    UPDATE delivery_queue
                    SET payload_json = ?,
                        updated_at = ?,
                        available_at = ?,
                        last_error = COALESCE(?, last_error),
                        attempts = CASE
                            WHEN attempts < ? THEN ?
                            ELSE attempts
                        END
                    WHERE delivery_id = ?
                    """,
                    (
                        payload_json,
                        now,
                        ready_at,
                        last_error,
                        attempts,
                        attempts,
                        delivery_id,
                    ),
                )
                connection.commit()
                return delivery_id

            delivery_id = "dlv_{0}".format(uuid4().hex)
            connection.execute(
                """
                INSERT INTO delivery_queue (
                    delivery_id,
                    dedupe_key,
                    channel,
                    event_type,
                    payload_json,
                    status,
                    created_at,
                    updated_at,
                    available_at,
                    attempts,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    dedupe_key,
                    channel,
                    event_type,
                    payload_json,
                    now,
                    now,
                    ready_at,
                    max(0, attempts),
                    last_error,
                ),
            )
            connection.commit()
            return delivery_id

    def fetch_due(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    delivery_id,
                    dedupe_key,
                    channel,
                    event_type,
                    payload_json,
                    created_at,
                    updated_at,
                    available_at,
                    attempts,
                    last_error
                FROM delivery_queue
                WHERE status = 'pending'
                  AND available_at <= ?
                ORDER BY available_at ASC, created_at ASC
                LIMIT ?
                """,
                (utcnow_iso(), max(1, limit)),
            ).fetchall()

        deliveries: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            deliveries.append(
                {
                    "delivery_id": row["delivery_id"],
                    "dedupe_key": row["dedupe_key"],
                    "channel": row["channel"],
                    "event_type": row["event_type"],
                    "payload": payload,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "available_at": row["available_at"],
                    "attempts": int(row["attempts"]),
                    "last_error": row["last_error"],
                }
            )
        return deliveries

    def reschedule(
        self,
        delivery_id: str,
        *,
        next_attempts: int,
        available_at: str,
        last_error: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE delivery_queue
                SET attempts = ?,
                    available_at = ?,
                    updated_at = ?,
                    last_error = ?
                WHERE delivery_id = ?
                  AND status = 'pending'
                """,
                (
                    max(0, next_attempts),
                    available_at,
                    utcnow_iso(),
                    last_error,
                    delivery_id,
                ),
            )
            connection.commit()

    def mark_delivered(self, delivery_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM delivery_queue WHERE delivery_id = ?",
                (delivery_id,),
            )
            connection.commit()

    def mark_dead_letter(
        self,
        delivery_id: str,
        *,
        attempts: int,
        last_error: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE delivery_queue
                SET status = 'deadletter',
                    attempts = ?,
                    updated_at = ?,
                    last_error = ?
                WHERE delivery_id = ?
                """,
                (
                    max(0, attempts),
                    utcnow_iso(),
                    last_error,
                    delivery_id,
                ),
            )
            connection.commit()

    def list_pending_inputs(self, channel: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM delivery_queue
                WHERE status = 'pending'
                  AND channel = ?
                  AND event_type = 'task.input_required'
                ORDER BY created_at ASC
                """,
                (channel,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def summary(self, *, since: str | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM delivery_queue WHERE status = 'pending'"
                ).fetchone()[0]
            )
            deadletter_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM delivery_queue WHERE status = 'deadletter'"
                ).fetchone()[0]
            )
            oldest_pending = connection.execute(
                """
                SELECT created_at
                FROM delivery_queue
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            latest_deadletter = connection.execute(
                """
                SELECT updated_at
                FROM delivery_queue
                WHERE status = 'deadletter'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            new_deadletter_count = None
            if since:
                new_deadletter_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM delivery_queue
                        WHERE status = 'deadletter'
                          AND updated_at > ?
                        """,
                        (since,),
                    ).fetchone()[0]
                )
        return {
            "pending_count": pending_count,
            "deadletter_count": deadletter_count,
            "oldest_pending_at": oldest_pending["created_at"] if oldest_pending is not None else None,
            "latest_deadletter_at": latest_deadletter["updated_at"] if latest_deadletter is not None else None,
            "new_deadletter_count_since": new_deadletter_count,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
