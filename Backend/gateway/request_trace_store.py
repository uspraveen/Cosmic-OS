import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .session_store import utcnow_iso

_MAX_TRACE_EVENTS = 64
_MAX_QUERY_EXCERPT_CHARS = 400
_MAX_EVENT_DETAIL_CHARS = 800


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def _json_load(value: str | None, *, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _truncate_text(value: Any, *, max_chars: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class RequestTraceStore:
    """SQLite-backed request trace store for Gateway observability."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS request_traces (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    route TEXT NOT NULL,
                    source TEXT,
                    source_id TEXT,
                    task_id TEXT,
                    user_query_excerpt TEXT,
                    status TEXT NOT NULL,
                    final_event_type TEXT,
                    final_message TEXT,
                    specialist_receipts_json TEXT,
                    delivery_json TEXT,
                    events_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_request_traces_session_updated
                    ON request_traces(session_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_request_traces_status_updated
                    ON request_traces(status, updated_at DESC);
                """
            )
            connection.commit()

    def record_event(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        route: str,
        event_type: str,
        stage: str,
        status: str,
        title: str,
        detail: str | None = None,
        task_id: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        user_query_excerpt: str | None = None,
        final_message: str | None = None,
        specialist_receipts: list[dict[str, Any]] | None = None,
        delivery: dict[str, Any] | None = None,
        completed: bool = False,
        completed_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_request_id = str(request_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_channel = str(channel or "").strip()
        normalized_route = str(route or "").strip() or "opus"
        normalized_event_type = str(event_type or "").strip()
        if not normalized_request_id or not normalized_session_id or not normalized_channel or not normalized_event_type:
            raise ValueError("request_id, session_id, channel, and event_type are required")

        now = utcnow_iso()
        resolved_completed_at = completed_at or (now if completed else None)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT events_json, created_at, task_id, source, source_id, user_query_excerpt,
                       specialist_receipts_json, delivery_json, status
                FROM request_traces
                WHERE request_id = ?
                LIMIT 1
                """,
                (normalized_request_id,),
            ).fetchone()

            events = _json_load(row["events_json"], default=[]) if row else []
            if not isinstance(events, list):
                events = []

            event_payload = {
                "at": now,
                "event_type": normalized_event_type,
                "stage": str(stage or "").strip() or normalized_event_type,
                "status": str(status or "").strip() or "active",
                "title": str(title or "").strip() or normalized_event_type,
            }
            truncated_detail = _truncate_text(detail, max_chars=_MAX_EVENT_DETAIL_CHARS)
            if truncated_detail:
                event_payload["detail"] = truncated_detail
            if metadata:
                event_payload["metadata"] = metadata
            if task_id:
                event_payload["task_id"] = str(task_id).strip()

            if not events or events[-1] != event_payload:
                events.append(event_payload)
            if len(events) > _MAX_TRACE_EVENTS:
                events = events[-_MAX_TRACE_EVENTS:]

            resolved_task_id = str(task_id or "").strip() or (str(row["task_id"]).strip() if row and row["task_id"] else None)
            resolved_source = str(source or "").strip() or (str(row["source"]).strip() if row and row["source"] else None)
            resolved_source_id = str(source_id or "").strip() or (str(row["source_id"]).strip() if row and row["source_id"] else None)
            resolved_excerpt = _truncate_text(
                user_query_excerpt or (row["user_query_excerpt"] if row else None),
                max_chars=_MAX_QUERY_EXCERPT_CHARS,
            )
            resolved_specialist_receipts = (
                specialist_receipts
                if specialist_receipts is not None
                else _json_load(row["specialist_receipts_json"], default=[]) if row else []
            )
            resolved_delivery = (
                delivery
                if delivery is not None
                else _json_load(row["delivery_json"], default={}) if row else {}
            )
            resolved_status = str(status or "").strip() or (str(row["status"]).strip() if row and row["status"] else "active")
            resolved_final_message = _truncate_text(final_message, max_chars=_MAX_EVENT_DETAIL_CHARS)
            created_at = row["created_at"] if row and row["created_at"] else now

            connection.execute(
                """
                INSERT INTO request_traces (
                    request_id,
                    session_id,
                    channel,
                    route,
                    source,
                    source_id,
                    task_id,
                    user_query_excerpt,
                    status,
                    final_event_type,
                    final_message,
                    specialist_receipts_json,
                    delivery_json,
                    events_json,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    channel = excluded.channel,
                    route = excluded.route,
                    source = excluded.source,
                    source_id = excluded.source_id,
                    task_id = excluded.task_id,
                    user_query_excerpt = excluded.user_query_excerpt,
                    status = excluded.status,
                    final_event_type = excluded.final_event_type,
                    final_message = COALESCE(excluded.final_message, request_traces.final_message),
                    specialist_receipts_json = excluded.specialist_receipts_json,
                    delivery_json = excluded.delivery_json,
                    events_json = excluded.events_json,
                    updated_at = excluded.updated_at,
                    completed_at = COALESCE(excluded.completed_at, request_traces.completed_at)
                """,
                (
                    normalized_request_id,
                    normalized_session_id,
                    normalized_channel,
                    normalized_route,
                    resolved_source,
                    resolved_source_id,
                    resolved_task_id,
                    resolved_excerpt,
                    resolved_status,
                    normalized_event_type if resolved_completed_at else None,
                    resolved_final_message,
                    _json_dumps(resolved_specialist_receipts or []),
                    _json_dumps(resolved_delivery or {}),
                    _json_dumps(events),
                    created_at,
                    now,
                    resolved_completed_at,
                ),
            )
            connection.commit()

    def list_session_traces(self, session_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, session_id, channel, route, source, source_id, task_id,
                       user_query_excerpt, status, final_event_type, final_message,
                       specialist_receipts_json, delivery_json, events_json,
                       created_at, updated_at, completed_at
                FROM request_traces
                WHERE session_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (normalized_session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_request_trace(self, request_id: str) -> dict[str, Any] | None:
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, session_id, channel, route, source, source_id, task_id,
                       user_query_excerpt, status, final_event_type, final_message,
                       specialist_receipts_json, delivery_json, events_json,
                       created_at, updated_at, completed_at
                FROM request_traces
                WHERE request_id = ?
                LIMIT 1
                """,
                (normalized_request_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "session_id": row["session_id"],
            "channel": row["channel"],
            "route": row["route"],
            "source": row["source"],
            "source_id": row["source_id"],
            "task_id": row["task_id"],
            "user_query_excerpt": row["user_query_excerpt"],
            "status": row["status"],
            "final_event_type": row["final_event_type"],
            "final_message": row["final_message"],
            "specialist_receipts": _json_load(row["specialist_receipts_json"], default=[]),
            "delivery": _json_load(row["delivery_json"], default={}),
            "events": _json_load(row["events_json"], default=[]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
