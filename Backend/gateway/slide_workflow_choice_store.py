"""Durable Slide Agent workflow-choice queue for inline choice cards.

When the Slide Agent refuses a `slide.create` delegation because no workflow
was chosen (NEEDS_WORKFLOW_CHOICE), the orchestrator attaches a
`slide_workflow_choice` receipt carrying the original request. The Gateway
persists it here so the desktop/mobile client can render an inline choice
card and the user's pick can resume the exact request later, even across a
gateway restart.
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


class SlideWorkflowChoiceStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS slide_workflow_choices (
                    choice_id TEXT PRIMARY KEY,
                    unique_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    workflow TEXT,
                    description TEXT NOT NULL,
                    requested_slides INTEGER,
                    validate_flag INTEGER NOT NULL DEFAULT 0,
                    force_catalog INTEGER NOT NULL DEFAULT 0,
                    artifacts_json TEXT,
                    artifact_count INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    task_id TEXT,
                    channel TEXT,
                    request_id TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    selected_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_slide_choices_status_updated
                    ON slide_workflow_choices(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_slide_choices_session
                    ON slide_workflow_choices(session_id, updated_at DESC);
                """
            )
            connection.commit()

    def upsert_pending(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = utcnow_iso()
        normalized = self._normalize_pending(item, now=now)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT choice_id FROM slide_workflow_choices WHERE unique_key = ? LIMIT 1",
                (normalized["unique_key"],),
            ).fetchone()
            created = existing is None
            connection.execute(
                """
                INSERT INTO slide_workflow_choices (
                    choice_id, unique_key, status, workflow, description, requested_slides,
                    validate_flag, force_catalog, artifacts_json, artifact_count,
                    session_id, task_id, channel, request_id, payload_json,
                    created_at, updated_at, selected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    status = CASE
                        WHEN slide_workflow_choices.status NOT IN ('pending') THEN slide_workflow_choices.status
                        ELSE 'pending'
                    END,
                    description = excluded.description,
                    requested_slides = excluded.requested_slides,
                    validate_flag = excluded.validate_flag,
                    force_catalog = excluded.force_catalog,
                    artifacts_json = excluded.artifacts_json,
                    artifact_count = excluded.artifact_count,
                    session_id = excluded.session_id,
                    task_id = excluded.task_id,
                    channel = excluded.channel,
                    request_id = excluded.request_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                self._insert_values(normalized),
            )
            connection.commit()
            row = self._get_by_unique_key(connection, normalized["unique_key"])
        return self._row_to_dict(row), created

    def list(self, *, include_terminal: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        where = "" if include_terminal else "WHERE status = 'pending'"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM slide_workflow_choices
                {where}
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 100), 500)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, choice_id: str) -> dict[str, Any] | None:
        normalized_id = self._text(choice_id)
        if not normalized_id:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM slide_workflow_choices WHERE choice_id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_selected(self, choice_id: str, workflow: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE slide_workflow_choices
                SET status = 'selected', workflow = ?, updated_at = ?, selected_at = ?
                WHERE choice_id = ?
                """,
                (self._text(workflow), now, now, self._text(choice_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM slide_workflow_choices WHERE choice_id = ? LIMIT 1",
                (self._text(choice_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_pending(self, choice_id: str) -> dict[str, Any] | None:
        """Revert to pending after a failed continuation so the user can retry."""
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE slide_workflow_choices
                SET status = 'pending', updated_at = ?
                WHERE choice_id = ?
                """,
                (now, self._text(choice_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM slide_workflow_choices WHERE choice_id = ? LIMIT 1",
                (self._text(choice_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def mark_cancelled(self, choice_id: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE slide_workflow_choices
                SET status = 'cancelled', updated_at = ?
                WHERE choice_id = ?
                """,
                (now, self._text(choice_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM slide_workflow_choices WHERE choice_id = ? LIMIT 1",
                (self._text(choice_id),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def _normalize_pending(self, item: dict[str, Any], *, now: str) -> dict[str, Any]:
        choice_id = self._text(item.get("choice_id"))
        description = self._text(item.get("description"))
        if not description:
            raise ValueError("Slide workflow choice requires a description.")
        # Dedupe on the request content so a retried delegation that lands on
        # the same NEEDS_WORKFLOW_CHOICE failure reuses one card instead of
        # stacking identical choices on every retry.
        unique_key = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]
        if not choice_id:
            choice_id = f"slide_wf_{unique_key}"
        try:
            requested_slides = int(item.get("requested_slides") or 0)
        except (TypeError, ValueError):
            requested_slides = 0
        artifacts = [
            artifact
            for artifact in (item.get("artifacts") if isinstance(item.get("artifacts"), list) else [])
            if isinstance(artifact, dict)
        ]
        return {
            "choice_id": choice_id,
            "unique_key": unique_key,
            "status": self._text(item.get("status")) or "pending",
            "workflow": self._text(item.get("workflow")) or None,
            "description": description,
            "requested_slides": requested_slides if requested_slides > 0 else None,
            "validate": bool(item.get("validate")),
            "force_catalog": bool(item.get("force_catalog")),
            "artifacts": artifacts,
            "artifact_count": len(artifacts),
            "session_id": self._text(item.get("session_id")),
            "task_id": self._text(item.get("task_id")),
            "channel": self._text(item.get("channel")),
            "request_id": self._text(item.get("request_id")),
            "created_at": self._text(item.get("created_at")) or now,
            "updated_at": now,
            "selected_at": self._text(item.get("selected_at")),
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else dict(item),
        }

    def _insert_values(self, item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["choice_id"],
            item["unique_key"],
            item["status"],
            item.get("workflow"),
            item["description"],
            item.get("requested_slides"),
            1 if item.get("validate") else 0,
            1 if item.get("force_catalog") else 0,
            json.dumps(item.get("artifacts") or [], ensure_ascii=False),
            item.get("artifact_count") or 0,
            item.get("session_id"),
            item.get("task_id"),
            item.get("channel"),
            item.get("request_id"),
            json.dumps(item.get("payload") or {}, ensure_ascii=False),
            item.get("created_at"),
            item.get("updated_at"),
            item.get("selected_at"),
        )

    def _get_by_unique_key(self, connection: sqlite3.Connection, unique_key: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM slide_workflow_choices WHERE unique_key = ? LIMIT 1",
            (unique_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Slide workflow choice insert did not return a row.")
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
        data["artifacts"] = self._json_list(data.pop("artifacts_json", "[]"))
        data["payload"] = self._json_obj(data.pop("payload_json", "{}"))
        data["validate"] = bool(data.pop("validate_flag", 0))
        data["force_catalog"] = bool(data.pop("force_catalog", 0))
        return data

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

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
