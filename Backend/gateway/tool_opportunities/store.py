from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


class ToolOpportunityStore:
    """Canonical gateway store for persistent custom-tool opportunities."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS tool_opportunities (
                    opportunity_id TEXT PRIMARY KEY,
                    seed_key TEXT UNIQUE,
                    title TEXT NOT NULL,
                    tool_type TEXT NOT NULL DEFAULT 'site',
                    goal TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    proposed_features_json TEXT NOT NULL DEFAULT '[]',
                    helpful_materials_json TEXT NOT NULL DEFAULT '[]',
                    required_inputs_json TEXT NOT NULL DEFAULT '[]',
                    data_sources_json TEXT NOT NULL DEFAULT '[]',
                    trigger_source TEXT NOT NULL DEFAULT 'orchestrator',
                    source_context_refs_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    confidence REAL,
                    expected_value TEXT,
                    suggested_at TEXT,
                    last_presented_at TEXT,
                    presentation_count INTEGER NOT NULL DEFAULT 0,
                    user_feedback TEXT,
                    declined_reason TEXT,
                    defer_until TEXT,
                    alpha_project_id TEXT,
                    build_task_id TEXT,
                    deployment_url TEXT,
                    repo_url TEXT,
                    health_status TEXT,
                    last_checked_at TEXT,
                    created_by TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tool_opportunities_status
                    ON tool_opportunities(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tool_opportunities_alpha_project
                    ON tool_opportunities(alpha_project_id);
                """
            )
            connection.commit()

    def list_items(self, *, statuses: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            normalized = [str(item).strip() for item in statuses if str(item).strip()]
            if normalized:
                where = "WHERE status IN ({0})".format(",".join("?" for _ in normalized))
                params.extend(normalized)
        params.append(max(1, min(int(limit), 1000)))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM tool_opportunities {where} ORDER BY updated_at DESC, opportunity_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, opportunity_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tool_opportunities WHERE opportunity_id = ?",
                (str(opportunity_id or "").strip(),),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def get_by_seed_key(self, seed_key: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tool_opportunities WHERE seed_key = ?",
                (str(seed_key or "").strip(),),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def create(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO tool_opportunities (
                    opportunity_id, seed_key, title, tool_type, goal, reasoning,
                    proposed_features_json, helpful_materials_json, required_inputs_json,
                    data_sources_json, trigger_source, source_context_refs_json, status,
                    confidence, expected_value, suggested_at, last_presented_at,
                    presentation_count, user_feedback, declined_reason, defer_until,
                    alpha_project_id, build_task_id, deployment_url, repo_url,
                    health_status, last_checked_at, created_by, metadata_json,
                    created_at, updated_at
                ) VALUES (
                    :opportunity_id, :seed_key, :title, :tool_type, :goal, :reasoning,
                    :proposed_features_json, :helpful_materials_json, :required_inputs_json,
                    :data_sources_json, :trigger_source, :source_context_refs_json, :status,
                    :confidence, :expected_value, :suggested_at, :last_presented_at,
                    :presentation_count, :user_feedback, :declined_reason, :defer_until,
                    :alpha_project_id, :build_task_id, :deployment_url, :repo_url,
                    :health_status, :last_checked_at, :created_by, :metadata_json,
                    :created_at, :updated_at
                )
                """,
                self._db_values(item),
            )
            connection.commit()
        return self.get_item(str(item["opportunity_id"])) or item

    def update(self, opportunity_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "title", "tool_type", "goal", "reasoning", "proposed_features_json",
            "helpful_materials_json", "required_inputs_json", "data_sources_json",
            "trigger_source", "source_context_refs_json", "status", "confidence",
            "expected_value", "suggested_at", "last_presented_at", "presentation_count",
            "user_feedback", "declined_reason", "defer_until", "alpha_project_id",
            "build_task_id", "deployment_url", "repo_url", "health_status",
            "last_checked_at", "metadata_json", "updated_at",
        }
        clean = {key: value for key, value in changes.items() if key in allowed}
        if not clean:
            return self.get_item(opportunity_id)
        assignments = ", ".join(f"{key} = ?" for key in clean)
        values = [clean[key] for key in clean]
        values.append(str(opportunity_id or "").strip())
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE tool_opportunities SET {assignments} WHERE opportunity_id = ?",
                tuple(values),
            )
            connection.commit()
        return self.get_item(opportunity_id)

    def summary(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tool_opportunities GROUP BY status"
            ).fetchall()
        by_status = {str(row["status"]): int(row["count"]) for row in rows}
        return {"db_path": str(self.db_path), "total": sum(by_status.values()), "by_status": by_status}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _db_values(item: dict[str, Any]) -> dict[str, Any]:
        values = dict(item)
        for key in (
            "proposed_features", "helpful_materials", "required_inputs", "data_sources",
            "source_context_refs", "metadata",
        ):
            values[f"{key}_json"] = _json_dumps(values.pop(key, [] if key != "metadata" else {}))
        return values

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in (
            "proposed_features", "helpful_materials", "required_inputs", "data_sources",
            "source_context_refs",
        ):
            item[key] = _json_load(item.pop(f"{key}_json", None), [])
        item["metadata"] = _json_load(item.pop("metadata_json", None), {})
        return item
