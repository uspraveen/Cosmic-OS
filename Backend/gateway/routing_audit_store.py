from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .session_store import utcnow_iso


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


class RoutingAuditStore:
    """SQLite-backed inspection store for final Gateway routing decisions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS routing_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    source TEXT,
                    source_id TEXT,
                    query_text TEXT NOT NULL,
                    route_override TEXT,
                    sticky_hit INTEGER NOT NULL DEFAULT 0,
                    decision_source TEXT NOT NULL,
                    classifier_route TEXT,
                    final_route TEXT NOT NULL,
                    dispatch_target TEXT NOT NULL,
                    confidence REAL,
                    signals_json TEXT,
                    conversation_context_json TEXT,
                    classifier_payload_json TEXT,
                    classifier_metrics_json TEXT,
                    classifier_model TEXT,
                    classifier_latency_ms REAL,
                    decision_latency_ms REAL,
                    error_text TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_routing_audit_created
                    ON routing_audit(created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_routing_audit_request
                    ON routing_audit(request_id);

                CREATE INDEX IF NOT EXISTS idx_routing_audit_session
                    ON routing_audit(session_id, created_at DESC);
                """
            )
            connection.commit()

    def append(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        source: str | None,
        source_id: str | None,
        query_text: str,
        route_override: str | None,
        sticky_hit: bool,
        decision_source: str,
        classifier_route: str | None,
        final_route: str,
        dispatch_target: str,
        confidence: float | None,
        signals: list[Any] | None,
        conversation_context: list[dict[str, Any]] | None,
        classifier_payload: dict[str, Any] | None,
        classifier_metrics: dict[str, Any] | None,
        classifier_model: str | None,
        classifier_latency_ms: float | None,
        decision_latency_ms: float | None,
        error_text: str | None,
    ) -> None:
        created_at = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_audit (
                    request_id,
                    session_id,
                    channel,
                    source,
                    source_id,
                    query_text,
                    route_override,
                    sticky_hit,
                    decision_source,
                    classifier_route,
                    final_route,
                    dispatch_target,
                    confidence,
                    signals_json,
                    conversation_context_json,
                    classifier_payload_json,
                    classifier_metrics_json,
                    classifier_model,
                    classifier_latency_ms,
                    decision_latency_ms,
                    error_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    session_id,
                    channel,
                    source,
                    source_id,
                    query_text,
                    route_override,
                    1 if sticky_hit else 0,
                    decision_source,
                    classifier_route,
                    final_route,
                    dispatch_target,
                    confidence,
                    _json_dumps(signals),
                    _json_dumps(conversation_context),
                    _json_dumps(classifier_payload),
                    _json_dumps(classifier_metrics),
                    classifier_model,
                    classifier_latency_ms,
                    decision_latency_ms,
                    error_text,
                    created_at,
                ),
            )
            connection.commit()

    def list_entries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    request_id,
                    session_id,
                    channel,
                    source,
                    source_id,
                    query_text,
                    route_override,
                    sticky_hit,
                    decision_source,
                    classifier_route,
                    final_route,
                    dispatch_target,
                    confidence,
                    signals_json,
                    conversation_context_json,
                    classifier_payload_json,
                    classifier_metrics_json,
                    classifier_model,
                    classifier_latency_ms,
                    decision_latency_ms,
                    error_text,
                    created_at
                FROM routing_audit
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            entries.append(
                {
                    "request_id": row["request_id"],
                    "session_id": row["session_id"],
                    "channel": row["channel"],
                    "source": row["source"],
                    "source_id": row["source_id"],
                    "query_text": row["query_text"],
                    "route_override": row["route_override"],
                    "sticky_hit": bool(row["sticky_hit"]),
                    "decision_source": row["decision_source"],
                    "classifier_route": row["classifier_route"],
                    "final_route": row["final_route"],
                    "dispatch_target": row["dispatch_target"],
                    "confidence": row["confidence"],
                    "signals": json.loads(row["signals_json"]) if row["signals_json"] else [],
                    "conversation_context": (
                        json.loads(row["conversation_context_json"])
                        if row["conversation_context_json"]
                        else []
                    ),
                    "classifier_payload": (
                        json.loads(row["classifier_payload_json"])
                        if row["classifier_payload_json"]
                        else None
                    ),
                    "classifier_metrics": (
                        json.loads(row["classifier_metrics_json"])
                        if row["classifier_metrics_json"]
                        else None
                    ),
                    "classifier_model": row["classifier_model"],
                    "classifier_latency_ms": row["classifier_latency_ms"],
                    "decision_latency_ms": row["decision_latency_ms"],
                    "error_text": row["error_text"],
                    "created_at": row["created_at"],
                }
            )
        return entries

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
