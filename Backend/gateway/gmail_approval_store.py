"""Durable Gmail outbound approval queue for user-owned Gmail drafts."""

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


class GmailApprovalStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS gmail_approvals (
                    approval_id TEXT PRIMARY KEY,
                    unique_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    account_id TEXT NOT NULL,
                    account_email TEXT,
                    account_label TEXT,
                    draft_id TEXT NOT NULL,
                    message_id TEXT,
                    thread_id TEXT,
                    subject TEXT,
                    to_json TEXT,
                    cc_json TEXT,
                    bcc_json TEXT,
                    body_text TEXT,
                    body_preview TEXT,
                    notes TEXT,
                    request_id TEXT,
                    session_id TEXT,
                    task_id TEXT,
                    source_task_id TEXT,
                    reviewer_note TEXT,
                    send_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    sent_at TEXT,
                    payload_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_gmail_approvals_status_updated
                    ON gmail_approvals(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_gmail_approvals_account_draft
                    ON gmail_approvals(account_id, draft_id);
                """
            )
            connection.commit()

    def upsert_pending(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = utcnow_iso()
        normalized = self._normalize_pending(item, now=now)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT approval_id FROM gmail_approvals WHERE unique_key = ? LIMIT 1",
                (normalized["unique_key"],),
            ).fetchone()
            created = existing is None
            connection.execute(
                """
                INSERT INTO gmail_approvals (
                    approval_id, unique_key, status, account_id, account_email, account_label,
                    draft_id, message_id, thread_id, subject, to_json, cc_json, bcc_json,
                    body_text, body_preview, notes, request_id, session_id, task_id,
                    source_task_id, reviewer_note, send_result_json, created_at, updated_at,
                    reviewed_at, sent_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    account_email = excluded.account_email,
                    account_label = excluded.account_label,
                    message_id = excluded.message_id,
                    thread_id = excluded.thread_id,
                    subject = excluded.subject,
                    to_json = excluded.to_json,
                    cc_json = excluded.cc_json,
                    bcc_json = excluded.bcc_json,
                    body_text = excluded.body_text,
                    body_preview = excluded.body_preview,
                    notes = excluded.notes,
                    request_id = excluded.request_id,
                    session_id = excluded.session_id,
                    task_id = excluded.task_id,
                    source_task_id = excluded.source_task_id,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                self._insert_values(normalized),
            )
            connection.commit()
            row = self._get_by_unique_key(connection, normalized["unique_key"])
        return self._row_to_dict(row), created

    def list(self, *, include_terminal: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        where = "" if include_terminal else "WHERE status IN ('pending', 'sending')"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM gmail_approvals
                {where}
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 WHEN 'sending' THEN 1 ELSE 2 END,
                    updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 100), 500)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        normalized_id = self._text(approval_id)
        if not normalized_id:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM gmail_approvals WHERE approval_id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_sending(self, approval_id: str) -> dict[str, Any] | None:
        return self._update_status(approval_id, "sending")

    def mark_sent(self, approval_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE gmail_approvals
                SET status = 'sent', updated_at = ?, reviewed_at = COALESCE(reviewed_at, ?),
                    sent_at = ?, send_result_json = ?
                WHERE approval_id = ?
                """,
                (now, now, now, json.dumps(result or {}, ensure_ascii=False), self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM gmail_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_send_failed(self, approval_id: str, message: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE gmail_approvals
                SET status = 'pending', updated_at = ?, reviewer_note = ?
                WHERE approval_id = ?
                """,
                (now, self._text(message) or "Send failed.", self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM gmail_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_rejected(self, approval_id: str, note: str | None = None) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE gmail_approvals
                SET status = 'rejected', updated_at = ?, reviewed_at = ?, reviewer_note = ?
                WHERE approval_id = ?
                """,
                (now, now, self._text(note), self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM gmail_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _update_status(self, approval_id: str, status: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE gmail_approvals SET status = ?, updated_at = ? WHERE approval_id = ?",
                (status, now, self._text(approval_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM gmail_approvals WHERE approval_id = ? LIMIT 1",
                (self._text(approval_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _normalize_pending(self, item: dict[str, Any], *, now: str) -> dict[str, Any]:
        account_id = self._text(item.get("account_id"))
        draft_id = self._text(item.get("draft_id"))
        if not account_id or not draft_id:
            raise ValueError("Gmail approval requires account_id and draft_id.")
        unique_key = f"{account_id}|{draft_id}"
        digest = hashlib.sha256(unique_key.encode("utf-8")).hexdigest()[:16]
        body_text = self._text(item.get("body_text") or item.get("body"))
        body_preview = self._text(item.get("body_preview")) or " ".join(body_text.split())[:500]
        return {
            "approval_id": self._text(item.get("approval_id")) or f"gma_{digest}",
            "unique_key": unique_key,
            "status": self._text(item.get("status")) or "pending",
            "account_id": account_id,
            "account_email": self._text(item.get("account_email")),
            "account_label": self._text(item.get("account_label")),
            "draft_id": draft_id,
            "message_id": self._text(item.get("message_id")),
            "thread_id": self._text(item.get("thread_id")),
            "subject": self._text(item.get("subject")) or "(No subject)",
            "to": self._string_list(item.get("to")),
            "cc": self._string_list(item.get("cc")),
            "bcc": self._string_list(item.get("bcc")),
            "body_text": body_text,
            "body_preview": body_preview,
            "notes": self._text(item.get("notes")),
            "request_id": self._text(item.get("request_id")),
            "session_id": self._text(item.get("session_id")),
            "task_id": self._text(item.get("task_id")),
            "source_task_id": self._text(item.get("source_task_id")),
            "reviewer_note": self._text(item.get("reviewer_note")),
            "send_result": item.get("send_result") if isinstance(item.get("send_result"), dict) else {},
            "created_at": self._text(item.get("created_at")) or now,
            "updated_at": now,
            "reviewed_at": self._text(item.get("reviewed_at")),
            "sent_at": self._text(item.get("sent_at")),
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else dict(item),
        }

    def _insert_values(self, item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["approval_id"],
            item["unique_key"],
            item["status"],
            item["account_id"],
            item.get("account_email"),
            item.get("account_label"),
            item["draft_id"],
            item.get("message_id"),
            item.get("thread_id"),
            item.get("subject"),
            json.dumps(item.get("to") or [], ensure_ascii=False),
            json.dumps(item.get("cc") or [], ensure_ascii=False),
            json.dumps(item.get("bcc") or [], ensure_ascii=False),
            item.get("body_text"),
            item.get("body_preview"),
            item.get("notes"),
            item.get("request_id"),
            item.get("session_id"),
            item.get("task_id"),
            item.get("source_task_id"),
            item.get("reviewer_note"),
            json.dumps(item.get("send_result") or {}, ensure_ascii=False),
            item.get("created_at"),
            item.get("updated_at"),
            item.get("reviewed_at"),
            item.get("sent_at"),
            json.dumps(item.get("payload") or {}, ensure_ascii=False),
        )

    def _get_by_unique_key(self, connection: sqlite3.Connection, unique_key: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM gmail_approvals WHERE unique_key = ? LIMIT 1",
            (unique_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Gmail approval insert did not return a row.")
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
        for src, dst in (("to_json", "to"), ("cc_json", "cc"), ("bcc_json", "bcc")):
            data[dst] = self._json_list(data.pop(src, "[]"))
        data["send_result"] = self._json_obj(data.pop("send_result_json", "{}"))
        data["payload"] = self._json_obj(data.pop("payload_json", "{}"))
        return data

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            values = []
        out: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _json_list(raw: Any) -> list[Any]:
        try:
            value = json.loads(str(raw or "[]"))
        except Exception:
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _json_obj(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
