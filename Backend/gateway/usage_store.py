from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from shared.usage import UsageEvent
from shared import utcnow


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class UsageStore:
    """Append-only SQLite usage ledger for metered provider calls."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS usage_events (
                    llm_call_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    source_component TEXT NOT NULL,
                    source_id TEXT,
                    task_id TEXT,
                    plan_id TEXT,
                    parent_task_id TEXT,
                    session_id TEXT,
                    route TEXT,
                    operation TEXT NOT NULL,
                    usage_kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_id TEXT,
                    provider_request_id TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL,
                    latency_ms INTEGER,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT,
                    metadata_json TEXT,
                    llm_call_placed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_usage_task_id
                    ON usage_events(task_id);

                CREATE INDEX IF NOT EXISTS idx_usage_plan_id
                    ON usage_events(plan_id);

                CREATE INDEX IF NOT EXISTS idx_usage_session_id
                    ON usage_events(session_id);

                CREATE INDEX IF NOT EXISTS idx_usage_source
                    ON usage_events(source_component, source_id, llm_call_placed_at);

                CREATE INDEX IF NOT EXISTS idx_usage_provider_model
                    ON usage_events(provider, model, llm_call_placed_at);
                """
            )
            connection.commit()

    def append(self, event: UsageEvent) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO usage_events (
                    llm_call_id,
                    user_id,
                    source_component,
                    source_id,
                    task_id,
                    plan_id,
                    parent_task_id,
                    session_id,
                    route,
                    operation,
                    usage_kind,
                    provider,
                    model,
                    request_id,
                    provider_request_id,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cached_tokens,
                    reasoning_tokens,
                    estimated_cost_usd,
                    latency_ms,
                    success,
                    error_code,
                    metadata_json,
                    llm_call_placed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.llm_call_id,
                    event.user_id,
                    event.source_component,
                    event.source_id,
                    event.task_id,
                    event.plan_id,
                    event.parent_task_id,
                    event.session_id,
                    event.route,
                    event.operation,
                    event.usage_kind,
                    event.provider,
                    event.model,
                    event.request_id,
                    event.provider_request_id,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.total_tokens,
                    event.cached_tokens,
                    event.reasoning_tokens,
                    event.estimated_cost_usd,
                    event.latency_ms,
                    1 if event.success else 0,
                    event.error_code,
                    _json_dumps(event.metadata_json),
                    event.llm_call_placed_at,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_events,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed_events,
                    MAX(llm_call_placed_at) AS latest_call_at
                FROM usage_events
                """
            ).fetchone()
        return {
            "db_path": str(self.db_path),
            "total_events": int(row["total_events"] if row and row["total_events"] is not None else 0),
            "failed_events": int(row["failed_events"] if row and row["failed_events"] is not None else 0),
            "latest_call_at": row["latest_call_at"] if row else None,
        }

    def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM usage_events
                ORDER BY llm_call_placed_at DESC, llm_call_id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def dashboard_summary(
        self,
        *,
        period_days: int = 30,
        provider_limit: int = 5,
        feature_limit: int = 6,
    ) -> dict[str, Any]:
        normalized_period = max(1, int(period_days))
        cutoff = utcnow() - timedelta(days=normalized_period)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

        with self._lock, self._connect() as connection:
            totals_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(COALESCE(estimated_cost_usd, 0)), 0) AS total_cost_usd,
                    MAX(llm_call_placed_at) AS latest_call_at
                FROM usage_events
                WHERE llm_call_placed_at >= ?
                """,
                (cutoff_iso,),
            ).fetchone()
            provider_rows = connection.execute(
                """
                SELECT
                    provider,
                    model,
                    COUNT(*) AS call_count,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(COALESCE(estimated_cost_usd, 0)), 0) AS total_cost_usd
                FROM usage_events
                WHERE llm_call_placed_at >= ?
                GROUP BY provider, model
                ORDER BY total_cost_usd DESC, total_tokens DESC, call_count DESC
                LIMIT ?
                """,
                (cutoff_iso, max(1, min(int(provider_limit), 10))),
            ).fetchall()
            feature_rows = connection.execute(
                """
                SELECT
                    source_component,
                    operation,
                    COUNT(*) AS call_count
                FROM usage_events
                WHERE llm_call_placed_at >= ?
                GROUP BY source_component, operation
                ORDER BY call_count DESC, source_component ASC, operation ASC
                """,
                (cutoff_iso,),
            ).fetchall()

        total_calls = int(totals_row["total_calls"] if totals_row and totals_row["total_calls"] is not None else 0)
        total_tokens = int(totals_row["total_tokens"] if totals_row and totals_row["total_tokens"] is not None else 0)
        total_cost_usd = float(totals_row["total_cost_usd"] if totals_row and totals_row["total_cost_usd"] is not None else 0.0)
        latest_call_at = totals_row["latest_call_at"] if totals_row else None

        providers: list[dict[str, Any]] = []
        for row in provider_rows:
            call_count = int(row["call_count"] or 0)
            provider_cost = float(row["total_cost_usd"] or 0.0)
            provider_tokens = int(row["total_tokens"] or 0)
            percent = round((provider_cost / total_cost_usd) * 100.0, 1) if total_cost_usd > 0 else (
                round((provider_tokens / total_tokens) * 100.0, 1) if total_tokens > 0 else 0.0
            )
            providers.append(
                {
                    "name": _display_provider_name(str(row["provider"] or "").strip()),
                    "role": str(row["model"] or "").strip() or "metered-model",
                    "tokens": provider_tokens,
                    "cost_usd": provider_cost,
                    "count": call_count,
                    "percent": percent,
                }
            )

        feature_buckets: dict[str, int] = defaultdict(int)
        for row in feature_rows:
            label = _feature_label(
                source_component=str(row["source_component"] or "").strip(),
                operation=str(row["operation"] or "").strip(),
            )
            feature_buckets[label] += int(row["call_count"] or 0)

        usage_by_feature: list[dict[str, Any]] = []
        for label, count in sorted(feature_buckets.items(), key=lambda item: (-item[1], item[0]))[: max(1, min(int(feature_limit), 12))]:
            usage_by_feature.append(
                {
                    "label": label,
                    "count": count,
                    "percent": round((count / total_calls) * 100.0, 1) if total_calls > 0 else 0.0,
                }
            )

        return {
            "period_days": normalized_period,
            "period_label": f"Rolling {normalized_period}d",
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
            "latest_call_at": latest_call_at,
            "providers": providers,
            "usage_by_feature": usage_by_feature,
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        if payload.get("metadata_json"):
            try:
                payload["metadata_json"] = json.loads(payload["metadata_json"])
            except json.JSONDecodeError:
                pass
        payload["success"] = bool(payload.get("success"))
        return payload

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _display_provider_name(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return "Unknown"
    return {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "xai": "xAI",
        "groq": "Groq",
        "perplexity": "Perplexity",
    }.get(normalized, normalized.replace("_", " ").title())


def _feature_label(*, source_component: str, operation: str) -> str:
    op = operation.lower()
    src = source_component.lower()
    if "docs." in op or "docs_" in op or "docs-parser" in op:
        return "Documents"
    if "memory" in op:
        return "Memory"
    if "wishlist" in op:
        return "Capability wishlist"
    if "research" in op or "perplexity" in op or "firecrawl" in op or "web_search" in op or "web_fetch" in op:
        return "Research"
    if "scheduler" in op or "reminder" in op or src == "scheduler":
        return "Scheduling"
    if "router" in op or src == "model_router":
        return "Routing"
    if "orchestrator.process" in op or src == "orchestrator":
        return "Orchestration"
    if src == "gateway":
        return "Gateway"
    return source_component.replace("_", " ").title() if source_component else "Other"
