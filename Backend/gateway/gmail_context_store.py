from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GmailContextStore:
    """Durable compact ledger for Gmail items surfaced to the user.

    This stores references and short metadata only. Full Gmail bodies stay in Gmail
    and are fetched by the Gmail Agent when a later user turn needs exact context.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS surfaced_gmail_items (
                    surfaced_id TEXT PRIMARY KEY,
                    unique_key TEXT NOT NULL UNIQUE,
                    account_id TEXT,
                    account_email TEXT,
                    message_id TEXT,
                    thread_id TEXT,
                    subject TEXT,
                    sender TEXT,
                    category TEXT,
                    priority REAL,
                    suggested_action TEXT,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    item_count INTEGER NOT NULL DEFAULT 1,
                    source_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_surfaced_gmail_status_updated
                    ON surfaced_gmail_items(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_surfaced_gmail_thread
                    ON surfaced_gmail_items(account_id, thread_id, updated_at DESC);
                """
            )
            connection.commit()

    def upsert_surfaced_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        normalized = self._normalize_item(item, now=now)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO surfaced_gmail_items (
                    surfaced_id,
                    unique_key,
                    account_id,
                    account_email,
                    message_id,
                    thread_id,
                    subject,
                    sender,
                    category,
                    priority,
                    suggested_action,
                    reason,
                    status,
                    item_count,
                    source_task_id,
                    created_at,
                    updated_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    account_id = excluded.account_id,
                    account_email = excluded.account_email,
                    message_id = excluded.message_id,
                    thread_id = excluded.thread_id,
                    subject = excluded.subject,
                    sender = excluded.sender,
                    category = excluded.category,
                    priority = excluded.priority,
                    suggested_action = excluded.suggested_action,
                    reason = excluded.reason,
                    -- Never re-open an item that already finished a surface decision.
                    status = CASE
                        WHEN surfaced_gmail_items.status IN (
                            'notified', 'delivered', 'suppressed', 'ignored', 'self'
                        )
                        THEN surfaced_gmail_items.status
                        ELSE excluded.status
                    END,
                    item_count = excluded.item_count,
                    source_task_id = excluded.source_task_id,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    normalized["surfaced_id"],
                    normalized["unique_key"],
                    normalized.get("account_id"),
                    normalized.get("account_email"),
                    normalized.get("message_id"),
                    normalized.get("thread_id"),
                    normalized.get("subject"),
                    normalized.get("sender"),
                    normalized.get("category"),
                    normalized.get("priority"),
                    normalized.get("suggested_action"),
                    normalized.get("reason"),
                    normalized.get("status") or "active",
                    int(normalized.get("item_count") or 1),
                    normalized.get("source_task_id"),
                    normalized.get("created_at") or now,
                    normalized.get("updated_at") or now,
                    json.dumps(normalized.get("payload") or {}, ensure_ascii=False),
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT *
                FROM surfaced_gmail_items
                WHERE unique_key = ?
                LIMIT 1
                """,
                (normalized["unique_key"],),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else normalized

    def list_recent(
        self,
        *,
        limit: int = 5,
        lookback_hours: int = 72,
        status: str = "active",
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
        ).isoformat().replace("+00:00", "Z")
        status_filter = [
            str(item).strip()
            for item in (statuses if statuses is not None else [status])
            if str(item).strip()
        ] or ["active"]
        placeholders = ", ".join("?" for _ in status_filter)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM surfaced_gmail_items
                WHERE status IN ({placeholders}) AND updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*status_filter, cutoff, max(1, limit)),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM surfaced_gmail_items
                WHERE message_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_status(self, surfaced_id: str, status: str) -> bool:
        normalized_id = str(surfaced_id or "").strip()
        normalized_status = str(status or "").strip() or "active"
        if not normalized_id:
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE surfaced_gmail_items
                SET status = ?, updated_at = ?
                WHERE surfaced_id = ?
                """,
                (normalized_status, utcnow_iso(), normalized_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def _normalize_item(self, item: dict[str, Any], *, now: str) -> dict[str, Any]:
        account_id = self._text(item.get("account_id"))
        account_email = self._text(item.get("account_email"))
        message_id = self._text(item.get("message_id"))
        thread_id = self._text(item.get("thread_id"))
        subject = self._text(item.get("subject"))
        sender = self._text(item.get("sender")) or self._text(item.get("from"))
        unique_key = "|".join(
            [
                account_id or account_email or "gmail",
                message_id or "",
                thread_id or "",
                subject or "",
                sender or "",
            ]
        )
        digest = hashlib.sha256(unique_key.encode("utf-8")).hexdigest()[:16]
        priority = item.get("priority")
        try:
            priority_value = float(priority) if priority is not None else None
        except (TypeError, ValueError):
            priority_value = None
        return {
            "surfaced_id": self._text(item.get("surfaced_id")) or f"gmail_{digest}",
            "unique_key": unique_key,
            "account_id": account_id,
            "account_email": account_email,
            "message_id": message_id,
            "thread_id": thread_id,
            "subject": subject,
            "sender": sender,
            "category": self._text(item.get("category")),
            "priority": priority_value,
            "suggested_action": self._text(item.get("suggested_action")),
            "reason": self._text(item.get("reason")),
            "status": self._text(item.get("status")) or "active",
            "item_count": self._int(item.get("item_count"), default=1),
            "source_task_id": self._text(item.get("source_task_id")),
            "created_at": self._text(item.get("created_at")) or now,
            "updated_at": now,
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else item,
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = {}
        raw_payload = row["payload_json"]
        if raw_payload:
            try:
                decoded = json.loads(raw_payload)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = {}
        return {
            "surfaced_id": row["surfaced_id"],
            "account_id": row["account_id"],
            "account_email": row["account_email"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "sender": row["sender"],
            "category": row["category"],
            "priority": row["priority"],
            "suggested_action": row["suggested_action"],
            "reason": row["reason"],
            "status": row["status"],
            "item_count": row["item_count"],
            "source_task_id": row["source_task_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "payload": payload,
        }

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
