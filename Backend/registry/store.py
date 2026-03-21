from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared import utcnow


def utcnow_iso() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


class RegistryStore:
    """Persistent capability registry for future agent dispatch."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'registered',
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    heartbeat_ttl INTEGER NOT NULL DEFAULT 30,
                    max_task_duration_sec INTEGER NOT NULL DEFAULT 300,
                    card_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_intents (
                    agent_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    timeout_sec INTEGER,
                    PRIMARY KEY (agent_id, intent),
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agents_status
                    ON agents(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_agent_intents_intent
                    ON agent_intents(intent, agent_id);

                CREATE TABLE IF NOT EXISTS agent_usage_daily (
                    agent_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    first_used_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, intent, usage_date),
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_usage_daily_recent
                    ON agent_usage_daily(usage_date DESC, last_used_at DESC);

                CREATE TABLE IF NOT EXISTS featured_specialists (
                    rank INTEGER PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    agent_summary TEXT,
                    common_intents_json TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT,
                    refreshed_at TEXT NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_featured_specialists_agent
                    ON featured_specialists(agent_id);
                """
            )
            connection.commit()

    def upsert_agent_card(self, card: dict[str, Any], *, status: str = "registered") -> None:
        agent_id = str(card.get("agent_id") or "").strip()
        if not agent_id:
            raise ValueError("agent_card.agent_id is required")

        display_name = str(card.get("display_name") or agent_id).strip() or agent_id
        sla = card.get("sla") if isinstance(card.get("sla"), dict) else {}
        max_concurrency = max(1, int(sla.get("max_concurrency") or 1))
        heartbeat_ttl = max(1, int(sla.get("heartbeat_ttl_sec") or 30))
        max_task_duration = max(1, int(sla.get("max_task_duration_sec") or 300))
        intents = card.get("intents") if isinstance(card.get("intents"), list) else []
        now = utcnow_iso()

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    agent_id,
                    display_name,
                    status,
                    max_concurrency,
                    heartbeat_ttl,
                    max_task_duration_sec,
                    card_json,
                    registered_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    status = excluded.status,
                    max_concurrency = excluded.max_concurrency,
                    heartbeat_ttl = excluded.heartbeat_ttl,
                    max_task_duration_sec = excluded.max_task_duration_sec,
                    card_json = excluded.card_json,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id,
                    display_name,
                    status,
                    max_concurrency,
                    heartbeat_ttl,
                    max_task_duration,
                    json.dumps(card, ensure_ascii=False, default=_json_default),
                    now,
                    now,
                ),
            )
            connection.execute("DELETE FROM agent_intents WHERE agent_id = ?", (agent_id,))
            for item in intents:
                if not isinstance(item, dict):
                    continue
                intent_name = str(item.get("name") or "").strip()
                if not intent_name:
                    continue
                timeout_sec = item.get("timeout_sec")
                timeout_value = int(timeout_sec) if isinstance(timeout_sec, (int, float, str)) and str(timeout_sec).strip() else None
                connection.execute(
                    """
                    INSERT INTO agent_intents (agent_id, intent, timeout_sec)
                    VALUES (?, ?, ?)
                    """,
                    (agent_id, intent_name, timeout_value),
                )
            connection.commit()

    def mark_deprecated(self, agent_id: str) -> None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agents
                SET status = 'deprecated',
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (now, agent_id),
            )
            connection.commit()

    def get_card(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT card_json
                FROM agents
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["card_json"])
        return data if isinstance(data, dict) else None

    def list_agents(self, *, status: str | None = "registered") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_id, display_name, status, max_concurrency, heartbeat_ttl, max_task_duration_sec, registered_at, updated_at
                FROM agents
                {where_sql}
                ORDER BY agent_id ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_agent_cards(self, *, status: str | None = "registered") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT agent_id, card_json
                FROM agents
                {where_sql}
                ORDER BY agent_id ASC
                """,
                params,
            ).fetchall()
        cards: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["card_json"])
            if isinstance(payload, dict):
                cards.append(payload)
        return cards

    def list_agents_for_intent(self, intent: str, *, status: str | None = "registered") -> list[dict[str, Any]]:
        params: list[Any] = [intent]
        status_sql = ""
        if status:
            status_sql = "AND a.status = ?"
            params.append(status)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.agent_id, a.display_name, a.status, a.max_concurrency, a.heartbeat_ttl, i.timeout_sec
                FROM agent_intents i
                INNER JOIN agents a ON a.agent_id = i.agent_id
                WHERE i.intent = ?
                {status_sql}
                ORDER BY a.agent_id ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_intents(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT intent, timeout_sec
                FROM agent_intents
                WHERE agent_id = ?
                ORDER BY intent ASC
                """,
                (agent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_agent_usage(
        self,
        agent_id: str,
        intent: str,
        *,
        used_at: datetime | None = None,
    ) -> None:
        normalized_agent_id = str(agent_id or "").strip()
        normalized_intent = str(intent or "").strip()
        if not normalized_agent_id or not normalized_intent:
            return
        used_dt = _coerce_datetime(used_at)
        used_iso = _datetime_to_iso(used_dt)
        usage_date = used_dt.date().isoformat()

        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM agents WHERE agent_id = ?",
                (normalized_agent_id,),
            ).fetchone()
            if exists is None:
                return
            connection.execute(
                """
                INSERT INTO agent_usage_daily (
                    agent_id,
                    intent,
                    usage_date,
                    usage_count,
                    first_used_at,
                    last_used_at,
                    updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(agent_id, intent, usage_date) DO UPDATE SET
                    usage_count = agent_usage_daily.usage_count + 1,
                    last_used_at = excluded.last_used_at,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_agent_id,
                    normalized_intent,
                    usage_date,
                    used_iso,
                    used_iso,
                    used_iso,
                ),
            )
            connection.commit()

    def refresh_featured_specialists(
        self,
        *,
        limit: int = 5,
        lookback_days: int = 14,
        refreshed_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        normalized_limit = max(0, int(limit))
        normalized_lookback = max(1, int(lookback_days))
        refresh_dt = _coerce_datetime(refreshed_at)
        refresh_iso = _datetime_to_iso(refresh_dt)

        if normalized_limit == 0:
            with self._lock, self._connect() as connection:
                connection.execute("DELETE FROM featured_specialists")
                connection.commit()
            return []

        cards = {
            str(card.get("agent_id") or "").strip(): card
            for card in self.list_agent_cards(status="registered")
            if isinstance(card, dict) and str(card.get("agent_id") or "").strip()
        }
        if not cards:
            with self._lock, self._connect() as connection:
                connection.execute("DELETE FROM featured_specialists")
                connection.commit()
            return []

        cutoff_date = (refresh_dt.date() - timedelta(days=normalized_lookback - 1)).isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT agent_id, intent, usage_date, usage_count, last_used_at
                FROM agent_usage_daily
                WHERE usage_date >= ?
                """,
                (cutoff_date,),
            ).fetchall()

        usage_by_agent: dict[str, dict[str, Any]] = {}
        for row in rows:
            agent_id = str(row["agent_id"] or "").strip()
            if not agent_id or agent_id not in cards:
                continue
            stats = usage_by_agent.setdefault(
                agent_id,
                {
                    "usage_count": 0,
                    "weighted_usage": 0.0,
                    "last_used_at": None,
                    "intents": defaultdict(int),
                },
            )
            usage_count = max(0, int(row["usage_count"] or 0))
            usage_date = str(row["usage_date"] or "").strip()
            days_ago = _days_ago(usage_date, refresh_dt.date())
            stats["usage_count"] += usage_count
            stats["weighted_usage"] += usage_count * _usage_day_weight(days_ago)
            last_used_at = _parse_iso_datetime(row["last_used_at"])
            if last_used_at is not None:
                current_last = stats["last_used_at"]
                if current_last is None or last_used_at > current_last:
                    stats["last_used_at"] = last_used_at
            intent_name = str(row["intent"] or "").strip()
            if intent_name:
                stats["intents"][intent_name] += usage_count

        ranked: list[dict[str, Any]] = []
        for agent_id, stats in usage_by_agent.items():
            usage_count = int(stats["usage_count"])
            if usage_count <= 0:
                continue
            last_used_at = stats["last_used_at"]
            score = float(stats["weighted_usage"]) + _recency_bonus(last_used_at, refresh_dt)
            top_intents = [
                intent_name
                for intent_name, _count in sorted(
                    stats["intents"].items(),
                    key=lambda item: (-item[1], item[0]),
                )[:2]
            ]
            card = cards[agent_id]
            ranked.append(
                {
                    "agent_id": agent_id,
                    "display_name": str(card.get("display_name") or agent_id).strip() or agent_id,
                    "agent_summary": _build_agent_summary(card, top_intents),
                    "common_intents": top_intents,
                    "score": round(score, 3),
                    "usage_count": usage_count,
                    "last_used_at": _datetime_to_iso(last_used_at) if last_used_at else None,
                }
            )

        ranked.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                int(item.get("usage_count") or 0),
                str(item.get("last_used_at") or ""),
                str(item.get("display_name") or ""),
            ),
            reverse=True,
        )
        featured = ranked[:normalized_limit]

        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM featured_specialists")
            for index, item in enumerate(featured, start=1):
                connection.execute(
                    """
                    INSERT INTO featured_specialists (
                        rank,
                        agent_id,
                        display_name,
                        agent_summary,
                        common_intents_json,
                        score,
                        usage_count,
                        last_used_at,
                        refreshed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        index,
                        item["agent_id"],
                        item["display_name"],
                        item.get("agent_summary"),
                        json.dumps(item.get("common_intents") or [], ensure_ascii=False),
                        float(item.get("score") or 0.0),
                        int(item.get("usage_count") or 0),
                        item.get("last_used_at"),
                        refresh_iso,
                    ),
                )
            connection.commit()

        return self.list_featured_specialists(limit=normalized_limit)

    def list_featured_specialists(self, *, limit: int = 5) -> list[dict[str, Any]]:
        normalized_limit = max(0, int(limit))
        if normalized_limit == 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rank, agent_id, display_name, agent_summary, common_intents_json, score, usage_count, last_used_at, refreshed_at
                FROM featured_specialists
                ORDER BY rank ASC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                common_intents = json.loads(row["common_intents_json"])
            except Exception:
                common_intents = []
            if not isinstance(common_intents, list):
                common_intents = []
            results.append(
                {
                    "rank": int(row["rank"]),
                    "agent_id": str(row["agent_id"] or "").strip(),
                    "display_name": str(row["display_name"] or "").strip(),
                    "agent_summary": str(row["agent_summary"] or "").strip(),
                    "common_intents": [str(item).strip() for item in common_intents if str(item).strip()],
                    "score": float(row["score"] or 0.0),
                    "usage_count": int(row["usage_count"] or 0),
                    "last_used_at": str(row["last_used_at"] or "").strip() or None,
                    "refreshed_at": str(row["refreshed_at"] or "").strip(),
                }
            )
        return results

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON;")
        connection.execute("PRAGMA busy_timeout = 5000;")
        return connection


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _datetime_to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_ago(usage_date: str, today: date) -> int:
    try:
        parsed = date.fromisoformat(usage_date)
    except ValueError:
        return 9999
    delta = today - parsed
    return max(0, delta.days)


def _usage_day_weight(days_ago: int) -> float:
    if days_ago <= 1:
        return 4.0
    if days_ago <= 3:
        return 3.0
    if days_ago <= 7:
        return 2.0
    return 1.0


def _recency_bonus(last_used_at: datetime | None, refreshed_at: datetime) -> float:
    if last_used_at is None:
        return 0.0
    age_hours = max(0.0, (refreshed_at - last_used_at).total_seconds() / 3600.0)
    if age_hours <= 24:
        return 8.0
    if age_hours <= 72:
        return 5.0
    if age_hours <= 168:
        return 3.0
    return 1.0


def _build_agent_summary(card: dict[str, Any], top_intents: list[str]) -> str:
    description = str(card.get("description") or "").strip()
    if description:
        return description[:220]
    intents = card.get("intents") if isinstance(card.get("intents"), list) else []
    for item in intents:
        if not isinstance(item, dict):
            continue
        intent_name = str(item.get("name") or "").strip()
        if top_intents and intent_name not in top_intents:
            continue
        intent_description = str(item.get("description") or "").strip()
        if intent_description:
            return intent_description[:220]
    if top_intents:
        return f"Common intents: {', '.join(top_intents[:2])}"
    return ""
