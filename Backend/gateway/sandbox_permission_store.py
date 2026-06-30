"""Durable sandbox capability permission queue for orchestrator code execution."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SandboxPermissionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS sandbox_permissions (
                    permission_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    description TEXT,
                    network INTEGER NOT NULL DEFAULT 0,
                    host_read_paths_json TEXT,
                    host_write_paths_json TEXT,
                    allowed_hosts_json TEXT,
                    code TEXT NOT NULL,
                    packages_json TEXT,
                    timeout_sec REAL,
                    request_id TEXT,
                    session_id TEXT,
                    task_id TEXT,
                    channel TEXT,
                    reviewer_note TEXT,
                    result_json TEXT,
                    auto_approved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    executed_at TEXT,
                    payload_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sandbox_permissions_status_updated
                    ON sandbox_permissions(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sandbox_permissions_session
                    ON sandbox_permissions(session_id, updated_at DESC);
                """
            )
            # Migration for databases created before auto_approved existed.
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(sandbox_permissions)"
                ).fetchall()
            }
            if "auto_approved" not in columns:
                connection.execute(
                    "ALTER TABLE sandbox_permissions ADD COLUMN auto_approved INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()

    def create_pending(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        permission_id = str(item.get("permission_id") or "").strip() or f"sbx_perm_{uuid4().hex[:12]}"
        normalized = {
            "permission_id": permission_id,
            "status": "pending",
            "description": str(item.get("description") or "").strip(),
            "network": 1 if item.get("network") else 0,
            "host_read_paths_json": json.dumps(item.get("host_read_paths") or [], ensure_ascii=False),
            "host_write_paths_json": json.dumps(item.get("host_write_paths") or [], ensure_ascii=False),
            "allowed_hosts_json": json.dumps(item.get("allowed_hosts") or [], ensure_ascii=False),
            "code": str(item.get("code") or ""),
            "packages_json": json.dumps(item.get("packages") or [], ensure_ascii=False),
            "timeout_sec": float(item["timeout_sec"]) if item.get("timeout_sec") not in (None, "") else None,
            "request_id": str(item.get("request_id") or "").strip() or None,
            "session_id": str(item.get("session_id") or "").strip() or None,
            "task_id": str(item.get("task_id") or "").strip() or None,
            "channel": str(item.get("channel") or "").strip() or None,
            "reviewer_note": None,
            "result_json": None,
            "created_at": now,
            "updated_at": now,
            "reviewed_at": None,
            "executed_at": None,
            "payload_json": json.dumps(item.get("payload") or {}, ensure_ascii=False),
        }
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_permissions (
                    permission_id, status, description, network,
                    host_read_paths_json, host_write_paths_json, allowed_hosts_json,
                    code, packages_json, timeout_sec,
                    request_id, session_id, task_id, channel,
                    reviewer_note, result_json,
                    created_at, updated_at, reviewed_at, executed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["permission_id"],
                    normalized["status"],
                    normalized["description"],
                    normalized["network"],
                    normalized["host_read_paths_json"],
                    normalized["host_write_paths_json"],
                    normalized["allowed_hosts_json"],
                    normalized["code"],
                    normalized["packages_json"],
                    normalized["timeout_sec"],
                    normalized["request_id"],
                    normalized["session_id"],
                    normalized["task_id"],
                    normalized["channel"],
                    normalized["reviewer_note"],
                    normalized["result_json"],
                    normalized["created_at"],
                    normalized["updated_at"],
                    normalized["reviewed_at"],
                    normalized["executed_at"],
                    normalized["payload_json"],
                ),
            )
            connection.commit()
        return self._row_to_dict(normalized)

    def get(self, permission_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_permissions WHERE permission_id = ? LIMIT 1",
                (permission_id,),
            ).fetchone()
        return self._row_to_dict(dict(row)) if row else None

    def list_for_scope(
        self,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return permissions tied to a turn, preferring the narrowest scope available.

        Lookup order: request_id, then task_id, then session_id. This lets the
        gateway re-attach a sandbox permission block to a response even when the
        orchestrator receipt plumbing does not carry it through to completion.
        """
        clauses: list[tuple[str, str]] = []
        rid = str(request_id or "").strip()
        tid = str(task_id or "").strip()
        sid = str(session_id or "").strip()
        if rid:
            clauses.append(("request_id = ?", rid))
        if tid:
            clauses.append(("task_id = ?", tid))
        if sid:
            clauses.append(("session_id = ?", sid))
        if not clauses:
            return []
        with self._lock, self._connection() as connection:
            for where, value in clauses:
                rows = connection.execute(
                    f"""
                    SELECT * FROM sandbox_permissions
                    WHERE {where}
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (value, int(limit)),
                ).fetchall()
                if rows:
                    return [self._row_to_dict(dict(row)) for row in rows]
        return []

    def list_completed_for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return successfully executed permissions for a session.

        Used to derive a session-scoped capability grant so that follow-up
        sandbox runs whose capabilities are a subset of what the user already
        approved can run without prompting again.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return []
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sandbox_permissions
                WHERE session_id = ? AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (sid, int(limit)),
            ).fetchall()
        return [self._row_to_dict(dict(row)) for row in rows]

    def mark_approved(self, permission_id: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_permissions
                SET status = 'approved', updated_at = ?, reviewed_at = ?
                WHERE permission_id = ? AND status IN ('pending', 'failed')
                """,
                (now, now, permission_id),
            )
            connection.commit()
            if cursor.rowcount <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM sandbox_permissions WHERE permission_id = ? LIMIT 1",
                (permission_id,),
            ).fetchone()
        return self._row_to_dict(dict(row)) if row else None

    def mark_auto_approved(self, permission_id: str) -> None:
        """Flag a permission as auto-approved so it never renders an approval card."""
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE sandbox_permissions SET auto_approved = 1 WHERE permission_id = ?",
                (permission_id,),
            )
            connection.commit()

    def mark_running(self, permission_id: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_permissions
                SET status = 'running', updated_at = ?, reviewed_at = COALESCE(reviewed_at, ?)
                WHERE permission_id = ? AND status IN ('pending', 'failed')
                """,
                (now, now, permission_id),
            )
            connection.commit()
            if cursor.rowcount <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM sandbox_permissions WHERE permission_id = ? LIMIT 1",
                (permission_id,),
            ).fetchone()
        return self._row_to_dict(dict(row)) if row else None

    def mark_rejected(self, permission_id: str, *, note: str | None = None) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_permissions
                SET status = 'rejected', reviewer_note = ?, updated_at = ?, reviewed_at = ?
                WHERE permission_id = ? AND status IN ('pending', 'failed')
                """,
                (note, now, now, permission_id),
            )
            connection.commit()
            if cursor.rowcount <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM sandbox_permissions WHERE permission_id = ? LIMIT 1",
                (permission_id,),
            ).fetchone()
        return self._row_to_dict(dict(row)) if row else None

    def mark_executed(self, permission_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        now = utcnow_iso()
        status = "completed" if not result.get("error") and str(result.get("status") or "") == "completed" else "failed"
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_permissions
                SET status = ?, result_json = ?, updated_at = ?, executed_at = ?
                WHERE permission_id = ? AND status IN ('running', 'approved')
                """,
                (status, json.dumps(result, ensure_ascii=False), now, now, permission_id),
            )
            connection.commit()
            if cursor.rowcount <= 0:
                return None
            connection.commit()
            row = connection.execute(
                "SELECT * FROM sandbox_permissions WHERE permission_id = ? LIMIT 1",
                (permission_id,),
            ).fetchone()
        return self._row_to_dict(dict(row)) if row else None

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        def loads_list(key: str) -> list[Any]:
            raw = row.get(key)
            if isinstance(raw, list):
                return raw
            if not raw:
                return []
            try:
                parsed = json.loads(str(raw))
            except (TypeError, ValueError):
                return []
            return parsed if isinstance(parsed, list) else []

        result = None
        if row.get("result_json"):
            try:
                result = json.loads(str(row["result_json"]))
            except (TypeError, ValueError):
                result = None

        return {
            "permission_id": row.get("permission_id"),
            "status": row.get("status"),
            "description": row.get("description"),
            "network": bool(row.get("network")),
            "host_read_paths": loads_list("host_read_paths_json"),
            "host_write_paths": loads_list("host_write_paths_json"),
            "allowed_hosts": loads_list("allowed_hosts_json"),
            "code": row.get("code"),
            "packages": loads_list("packages_json"),
            "timeout_sec": row.get("timeout_sec"),
            "request_id": row.get("request_id"),
            "session_id": row.get("session_id"),
            "task_id": row.get("task_id"),
            "channel": row.get("channel"),
            "reviewer_note": row.get("reviewer_note"),
            "result": result,
            "auto_approved": bool(row.get("auto_approved")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "reviewed_at": row.get("reviewed_at"),
            "executed_at": row.get("executed_at"),
        }

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()
