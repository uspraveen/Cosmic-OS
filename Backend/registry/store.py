from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
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
