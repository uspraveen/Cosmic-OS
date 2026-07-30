from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventAutomationStore:
    """Durable registry for user standing instructions bound to external events."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS event_automations (
                    automation_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    raw_instruction TEXT NOT NULL,
                    condition_json TEXT NOT NULL DEFAULT '{}',
                    action_json TEXT NOT NULL DEFAULT '{}',
                    approval_policy_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT,
                    created_request_id TEXT,
                    created_session_id TEXT,
                    created_channel TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_matched_at TEXT,
                    match_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_event_automations_event_status
                    ON event_automations(event_type, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS event_automation_matches (
                    match_id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_ref TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    orchestrator_request_id TEXT,
                    orchestrator_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(automation_id, event_ref)
                );

                CREATE INDEX IF NOT EXISTS idx_event_automation_matches_event
                    ON event_automation_matches(event_type, event_ref, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_event_automation_matches_automation
                    ON event_automation_matches(automation_id, created_at DESC);
                """
            )
            connection.commit()

    def create_or_update_automation(self, automation: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        normalized = self._normalize_automation(automation, now=now)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT automation_id, created_at FROM event_automations WHERE automation_id = ?",
                (normalized["automation_id"],),
            ).fetchone()
            created_at = existing["created_at"] if existing else normalized["created_at"]
            connection.execute(
                """
                INSERT INTO event_automations (
                    automation_id,
                    event_type,
                    label,
                    raw_instruction,
                    condition_json,
                    action_json,
                    approval_policy_json,
                    status,
                    created_by,
                    created_request_id,
                    created_session_id,
                    created_channel,
                    created_at,
                    updated_at,
                    last_matched_at,
                    match_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(automation_id) DO UPDATE SET
                    event_type = excluded.event_type,
                    label = excluded.label,
                    raw_instruction = excluded.raw_instruction,
                    condition_json = excluded.condition_json,
                    action_json = excluded.action_json,
                    approval_policy_json = excluded.approval_policy_json,
                    status = excluded.status,
                    created_by = COALESCE(event_automations.created_by, excluded.created_by),
                    created_request_id = COALESCE(event_automations.created_request_id, excluded.created_request_id),
                    created_session_id = COALESCE(event_automations.created_session_id, excluded.created_session_id),
                    created_channel = COALESCE(event_automations.created_channel, excluded.created_channel),
                    updated_at = excluded.updated_at
                """,
                (
                    normalized["automation_id"],
                    normalized["event_type"],
                    normalized["label"],
                    normalized["raw_instruction"],
                    json.dumps(normalized["condition"], ensure_ascii=False, sort_keys=True),
                    json.dumps(normalized["action"], ensure_ascii=False, sort_keys=True),
                    json.dumps(normalized["approval_policy"], ensure_ascii=False, sort_keys=True),
                    normalized["status"],
                    normalized.get("created_by"),
                    normalized.get("created_request_id"),
                    normalized.get("created_session_id"),
                    normalized.get("created_channel"),
                    created_at,
                    now,
                    normalized.get("last_matched_at"),
                    int(normalized.get("match_count") or 0),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM event_automations WHERE automation_id = ?",
                (normalized["automation_id"],),
            ).fetchone()
        return self._automation_row_to_dict(row)

    def list_automations(
        self,
        *,
        event_type: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        normalized_event_type = self._text(event_type)
        normalized_status = self._text(status)
        if normalized_event_type:
            where.append("event_type = ?")
            params.append(normalized_event_type)
        if normalized_status and normalized_status != "all":
            where.append("status = ?")
            params.append(normalized_status)
        sql = "SELECT * FROM event_automations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 50))))
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._automation_row_to_dict(row) for row in rows]

    def get_automation(self, automation_id: str) -> dict[str, Any] | None:
        normalized_id = self._text(automation_id)
        if not normalized_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_automations WHERE automation_id = ?",
                (normalized_id,),
            ).fetchone()
        return self._automation_row_to_dict(row) if row else None

    def set_automation_status(self, automation_id: str, status: str) -> dict[str, Any] | None:
        normalized_id = self._text(automation_id)
        normalized_status = self._text(status) or "inactive"
        if not normalized_id:
            return None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE event_automations
                SET status = ?, updated_at = ?
                WHERE automation_id = ?
                """,
                (normalized_status, utcnow_iso(), normalized_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM event_automations WHERE automation_id = ?",
                (normalized_id,),
            ).fetchone()
        return self._automation_row_to_dict(row) if row else None

    def record_match(self, match: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        normalized = self._normalize_match(match, now=now)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_automation_matches (
                    match_id,
                    automation_id,
                    event_type,
                    event_ref,
                    decision,
                    confidence,
                    evidence_json,
                    orchestrator_request_id,
                    orchestrator_task_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["match_id"],
                    normalized["automation_id"],
                    normalized["event_type"],
                    normalized["event_ref"],
                    normalized["decision"],
                    normalized["confidence"],
                    json.dumps(normalized["evidence"], ensure_ascii=False, sort_keys=True),
                    normalized.get("orchestrator_request_id"),
                    normalized.get("orchestrator_task_id"),
                    now,
                    now,
                ),
            )
            created = cursor.rowcount > 0
            if created:
                connection.execute(
                    """
                    UPDATE event_automations
                    SET last_matched_at = ?, match_count = match_count + 1, updated_at = ?
                    WHERE automation_id = ?
                    """,
                    (now, now, normalized["automation_id"]),
                )
            connection.commit()
            row = connection.execute(
                """
                SELECT *
                FROM event_automation_matches
                WHERE automation_id = ? AND event_ref = ?
                """,
                (normalized["automation_id"], normalized["event_ref"]),
            ).fetchone()
        payload = self._match_row_to_dict(row)
        payload["created"] = created
        return payload

    def update_match_dispatch(
        self,
        *,
        match_id: str,
        orchestrator_request_id: str | None = None,
        orchestrator_task_id: str | None = None,
        decision: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = self._text(match_id)
        if not normalized_id:
            return None
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [utcnow_iso()]
        if orchestrator_request_id is not None:
            updates.append("orchestrator_request_id = ?")
            params.append(self._text(orchestrator_request_id))
        if orchestrator_task_id is not None:
            updates.append("orchestrator_task_id = ?")
            params.append(self._text(orchestrator_task_id))
        if decision is not None:
            updates.append("decision = ?")
            params.append(self._text(decision) or "matched")
        params.append(normalized_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE event_automation_matches SET {', '.join(updates)} WHERE match_id = ?",
                params,
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM event_automation_matches WHERE match_id = ?",
                (normalized_id,),
            ).fetchone()
        return self._match_row_to_dict(row) if row else None

    def list_matches(
        self,
        *,
        automation_id: str | None = None,
        event_ref: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        normalized_automation_id = self._text(automation_id)
        normalized_event_ref = self._text(event_ref)
        if normalized_automation_id:
            where.append("automation_id = ?")
            params.append(normalized_automation_id)
        if normalized_event_ref:
            where.append("event_ref = ?")
            params.append(normalized_event_ref)
        sql = "SELECT * FROM event_automation_matches"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 50))))
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._match_row_to_dict(row) for row in rows]

    def _normalize_automation(self, automation: dict[str, Any], *, now: str) -> dict[str, Any]:
        automation_id = self._text(automation.get("automation_id")) or f"aut_{uuid4().hex[:12]}"
        event_type = self._text(automation.get("event_type")) or "gmail.inbound"
        raw_instruction = self._text(automation.get("raw_instruction")) or self._text(
            automation.get("instruction")
        )
        if not raw_instruction:
            raise ValueError("raw_instruction is required")
        label = self._text(automation.get("label")) or raw_instruction.splitlines()[0][:96]
        condition = automation.get("condition") if isinstance(automation.get("condition"), dict) else {}
        action = automation.get("action") if isinstance(automation.get("action"), dict) else {}
        approval_policy = (
            automation.get("approval_policy")
            if isinstance(automation.get("approval_policy"), dict)
            else {}
        )
        if not action:
            action = {"type": "orchestrator_task", "goal": raw_instruction}
        return {
            "automation_id": automation_id,
            "event_type": event_type,
            "label": label,
            "raw_instruction": raw_instruction,
            "condition": condition,
            "action": action,
            "approval_policy": approval_policy,
            "status": self._text(automation.get("status")) or "active",
            "created_by": self._text(automation.get("created_by")),
            "created_request_id": self._text(automation.get("created_request_id")),
            "created_session_id": self._text(automation.get("created_session_id")),
            "created_channel": self._text(automation.get("created_channel")),
            "created_at": self._text(automation.get("created_at")) or now,
            "updated_at": now,
            "last_matched_at": self._text(automation.get("last_matched_at")),
            "match_count": self._int(automation.get("match_count"), default=0),
        }

    def _normalize_match(self, match: dict[str, Any], *, now: str) -> dict[str, Any]:
        automation_id = self._text(match.get("automation_id"))
        event_type = self._text(match.get("event_type"))
        event_ref = self._text(match.get("event_ref"))
        if not automation_id or not event_type or not event_ref:
            raise ValueError("automation_id, event_type, and event_ref are required")
        confidence = match.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        return {
            "match_id": self._text(match.get("match_id")) or f"mat_{uuid4().hex[:12]}",
            "automation_id": automation_id,
            "event_type": event_type,
            "event_ref": event_ref,
            "decision": self._text(match.get("decision")) or "matched",
            "confidence": max(0.0, min(1.0, confidence_value)),
            "evidence": match.get("evidence") if isinstance(match.get("evidence"), dict) else {},
            "orchestrator_request_id": self._text(match.get("orchestrator_request_id")),
            "orchestrator_task_id": self._text(match.get("orchestrator_task_id")),
            "created_at": self._text(match.get("created_at")) or now,
            "updated_at": now,
        }

    def _automation_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "automation_id": row["automation_id"],
            "event_type": row["event_type"],
            "label": row["label"],
            "raw_instruction": row["raw_instruction"],
            "condition": self._loads(row["condition_json"]),
            "action": self._loads(row["action_json"]),
            "approval_policy": self._loads(row["approval_policy_json"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_request_id": row["created_request_id"],
            "created_session_id": row["created_session_id"],
            "created_channel": row["created_channel"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_matched_at": row["last_matched_at"],
            "match_count": row["match_count"],
        }

    def _match_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "match_id": row["match_id"],
            "automation_id": row["automation_id"],
            "event_type": row["event_type"],
            "event_ref": row["event_ref"],
            "decision": row["decision"],
            "confidence": row["confidence"],
            "evidence": self._loads(row["evidence_json"]),
            "orchestrator_request_id": row["orchestrator_request_id"],
            "orchestrator_task_id": row["orchestrator_task_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
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
    def _loads(value: Any) -> dict[str, Any]:
        try:
            decoded = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

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
