"""Durable Notion write approval queue.

Mirrors GmailApprovalStore: a Notion write proposed by the orchestrator parks
here as ``pending`` and only executes when the user presses Approve on the
card. The proposal (parent, title, content) is stored verbatim, so Approve
re-plays exactly what the user previewed — the gateway never lets the model's
phantom of the draft drift from what lands in Notion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class NotionApprovalStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS notion_approvals (
                    approval_id TEXT PRIMARY KEY,
                    unique_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    operation TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    workspace TEXT,
                    parent_page_id TEXT,
                    parent_database_id TEXT,
                    parent_title TEXT,
                    page_id TEXT,
                    page_title TEXT,
                    content_text TEXT,
                    content_preview TEXT,
                    notes TEXT,
                    request_id TEXT,
                    session_id TEXT,
                    task_id TEXT,
                    channel TEXT,
                    purpose TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    payload_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_notion_approvals_status_updated
                    ON notion_approvals(status, updated_at DESC);
                """
            )
            connection.commit()

    def upsert_pending(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert or re-open a proposal keyed on account + operation + target.

        Re-proposing the same write to the same target re-opens the same card
        instead of multiplying cards (the lesson from the vault approval
        multiplication RCA), but a card that already executed never flips back.
        """
        now = utcnow_iso()
        normalized = self._normalize_pending(item, now=now)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT status FROM notion_approvals WHERE unique_key = ? LIMIT 1",
                (normalized["unique_key"],),
            ).fetchone()
            created = existing is None
            connection.execute(
                """
                INSERT INTO notion_approvals (
                    approval_id, unique_key, status, operation, account_id, workspace,
                    parent_page_id, parent_database_id, parent_title, page_id, page_title,
                    content_text, content_preview, notes, request_id, session_id, task_id,
                    channel, purpose, result_json, created_at, updated_at, completed_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    status = CASE
                        WHEN notion_approvals.status IN ('completed', 'executing')
                            THEN notion_approvals.status
                        ELSE 'pending'
                    END,
                    workspace = excluded.workspace,
                    parent_page_id = excluded.parent_page_id,
                    parent_database_id = excluded.parent_database_id,
                    parent_title = excluded.parent_title,
                    page_title = excluded.page_title,
                    content_text = excluded.content_text,
                    content_preview = excluded.content_preview,
                    notes = excluded.notes,
                    request_id = excluded.request_id,
                    session_id = excluded.session_id,
                    task_id = excluded.task_id,
                    channel = excluded.channel,
                    purpose = excluded.purpose,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                self._insert_values(normalized),
            )
            connection.commit()
            row = self._get_by_unique_key(connection, normalized["unique_key"])
        return self._row_to_dict(row), created

    def list(self, *, include_terminal: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        where = "" if include_terminal else "WHERE status IN ('pending', 'executing')"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM notion_approvals
                {where}
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 WHEN 'executing' THEN 1 ELSE 2 END,
                    updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 100), 500)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notion_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_executing(self, approval_id: str) -> dict[str, Any] | None:
        return self._update_status(approval_id, "executing")

    def mark_completed(self, approval_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE notion_approvals
                SET status = 'completed', updated_at = ?, completed_at = ?, result_json = ?
                WHERE approval_id = ?
                """,
                (now, now, json.dumps(result or {}, ensure_ascii=False), self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM notion_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_failed(self, approval_id: str, message: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE notion_approvals
                SET status = 'pending', updated_at = ?, notes = ?
                WHERE approval_id = ?
                """,
                (now, self._text(message) or "Notion write failed.", self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM notion_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_rejected(self, approval_id: str, note: str | None = None) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE notion_approvals
                SET status = 'rejected', updated_at = ?, notes = ?
                WHERE approval_id = ?
                """,
                (now, self._text(note), self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM notion_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _update_status(self, approval_id: str, status: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE notion_approvals SET status = ?, updated_at = ? WHERE approval_id = ?",
                (status, now, self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM notion_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _normalize_pending(self, item: dict[str, Any], *, now: str) -> dict[str, Any]:
        operation = self._text(item.get("operation"))
        account_id = self._text(item.get("account_id"))
        if operation not in {"create_page", "update_page"}:
            raise ValueError("Notion approval requires operation create_page or update_page.")
        if not account_id:
            raise ValueError("Notion approval requires account_id.")
        if operation == "create_page" and not self._text(item.get("parent_page_id")) and not self._text(item.get("parent_database_id")):
            raise ValueError("Creating a Notion page requires parent_page_id or parent_database_id.")
        if operation == "update_page" and not self._text(item.get("page_id")):
            raise ValueError("Updating a Notion page requires page_id.")
        # The proposal fingerprint: same account + operation + target. Content
        # changes re-open the same card with the new preview rather than
        # stacking a second one for the same destination.
        unique_key = "|".join(
            [
                account_id,
                operation,
                self._text(item.get("parent_page_id")),
                self._text(item.get("parent_database_id")),
                self._text(item.get("page_id")),
            ]
        )
        digest = hashlib.sha256(unique_key.encode("utf-8")).hexdigest()[:16]
        content_text = str(item.get("content_text") or item.get("content") or "")
        content_preview = self._text(item.get("content_preview")) or " ".join(content_text.split())[:500]
        return {
            "approval_id": self._text(item.get("approval_id")) or f"nta_{digest}",
            "unique_key": unique_key,
            "status": self._text(item.get("status")) or "pending",
            "operation": operation,
            "account_id": account_id,
            "workspace": self._text(item.get("workspace")),
            "parent_page_id": self._text(item.get("parent_page_id")),
            "parent_database_id": self._text(item.get("parent_database_id")),
            "parent_title": self._text(item.get("parent_title")),
            "page_id": self._text(item.get("page_id")),
            "page_title": self._text(item.get("page_title")) or self._text(item.get("title")),
            "content_text": content_text,
            "content_preview": content_preview,
            "notes": self._text(item.get("notes")),
            "request_id": self._text(item.get("request_id")),
            "session_id": self._text(item.get("session_id")),
            "task_id": self._text(item.get("task_id")),
            "channel": self._text(item.get("channel")),
            "purpose": self._text(item.get("purpose")),
            "result": item.get("result") if isinstance(item.get("result"), dict) else {},
            "created_at": self._text(item.get("created_at")) or now,
            "updated_at": now,
            "completed_at": self._text(item.get("completed_at")),
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else dict(item),
        }

    def _insert_values(self, item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["approval_id"],
            item["unique_key"],
            item["status"],
            item["operation"],
            item["account_id"],
            item.get("workspace"),
            item.get("parent_page_id"),
            item.get("parent_database_id"),
            item.get("parent_title"),
            item.get("page_id"),
            item.get("page_title"),
            item.get("content_text"),
            item.get("content_preview"),
            item.get("notes"),
            item.get("request_id"),
            item.get("session_id"),
            item.get("task_id"),
            item.get("channel"),
            item.get("purpose"),
            json.dumps(item.get("result") or {}, ensure_ascii=False),
            item.get("created_at"),
            item.get("updated_at"),
            item.get("completed_at"),
            json.dumps(item.get("payload") or {}, ensure_ascii=False),
        )

    def _get_by_unique_key(self, connection: sqlite3.Connection, unique_key: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM notion_approvals WHERE unique_key = ? LIMIT 1",
            (unique_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Notion approval insert did not return a row.")
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["result"] = self._json_obj(data.pop("result_json", "{}"))
        data["payload"] = self._json_obj(data.pop("payload_json", "{}"))
        return data

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _json_obj(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
