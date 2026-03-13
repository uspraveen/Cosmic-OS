from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SchedulerStore:
    """SQLite-backed scheduler profile, cron manager, and heartbeat state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self, *, default_timezone: str) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS scheduler_profile (
                    profile_id TEXT PRIMARY KEY,
                    user_timezone TEXT NOT NULL,
                    timezone_source TEXT NOT NULL,
                    timezone_reported_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cron_jobs (
                    cron_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    description TEXT,
                    cron_expr TEXT,
                    timezone TEXT NOT NULL,
                    next_fire_at TEXT,
                    last_fired_at TEXT,
                    last_result_status TEXT,
                    last_result_summary TEXT,
                    paused_at TEXT,
                    pause_reason TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cron_jobs_next_fire
                    ON cron_jobs(paused_at, next_fire_at, updated_at);

                CREATE TABLE IF NOT EXISTS cron_history (
                    run_id TEXT PRIMARY KEY,
                    cron_id TEXT NOT NULL,
                    scheduled_for TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    summary TEXT,
                    FOREIGN KEY(cron_id) REFERENCES cron_jobs(cron_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_cron_history_cron_started
                    ON cron_history(cron_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_config (
                    config_id TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    paused_at TEXT,
                    pause_reason TEXT,
                    last_result_status TEXT,
                    last_result_summary TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            now = utcnow_iso()
            connection.execute(
                """
                INSERT INTO scheduler_profile (
                    profile_id,
                    user_timezone,
                    timezone_source,
                    timezone_reported_at,
                    updated_at
                )
                VALUES ('default', ?, 'fallback', NULL, ?)
                ON CONFLICT(profile_id) DO NOTHING
                """,
                (default_timezone, now),
            )
            connection.execute(
                """
                INSERT INTO heartbeat_config (
                    config_id,
                    timezone,
                    enabled,
                    paused_at,
                    pause_reason,
                    last_result_status,
                    last_result_summary,
                    updated_at
                )
                VALUES ('default', ?, 1, NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(config_id) DO NOTHING
                """,
                (default_timezone, now),
            )
            connection.commit()

    def get_profile(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    user_timezone,
                    timezone_source,
                    timezone_reported_at,
                    updated_at
                FROM scheduler_profile
                WHERE profile_id = 'default'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("scheduler profile has not been initialized")
        return dict(row)

    def update_user_timezone(self, timezone_name: str, *, source: str) -> dict[str, Any]:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE scheduler_profile
                SET user_timezone = ?,
                    timezone_source = ?,
                    timezone_reported_at = ?,
                    updated_at = ?
                WHERE profile_id = 'default'
                """,
                (timezone_name, source, now, now),
            )
            connection.execute(
                """
                UPDATE heartbeat_config
                SET timezone = ?,
                    updated_at = ?
                WHERE config_id = 'default'
                """,
                (timezone_name, now),
            )
            connection.commit()
        return self.get_profile()

    def upsert_cron(
        self,
        *,
        cron_id: str,
        name: str,
        kind: str,
        description: str | None,
        cron_expr: str | None,
        timezone_name: str,
        next_fire_at: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cron_jobs (
                    cron_id,
                    name,
                    kind,
                    description,
                    cron_expr,
                    timezone,
                    next_fire_at,
                    last_fired_at,
                    last_result_status,
                    last_result_summary,
                    paused_at,
                    pause_reason,
                    metadata_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                ON CONFLICT(cron_id) DO UPDATE SET
                    name = excluded.name,
                    kind = excluded.kind,
                    description = excluded.description,
                    cron_expr = excluded.cron_expr,
                    timezone = excluded.timezone,
                    next_fire_at = excluded.next_fire_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cron_id,
                    name,
                    kind,
                    description,
                    cron_expr,
                    timezone_name,
                    next_fire_at,
                    metadata_json,
                    now,
                    now,
                ),
            )
            connection.commit()
        record = self.get_cron(cron_id)
        if record is None:
            raise RuntimeError(f"failed to upsert cron {cron_id}")
        return record

    def get_cron(self, cron_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    cron_id,
                    name,
                    kind,
                    description,
                    cron_expr,
                    timezone,
                    next_fire_at,
                    last_fired_at,
                    last_result_status,
                    last_result_summary,
                    paused_at,
                    pause_reason,
                    metadata_json,
                    created_at,
                    updated_at
                FROM cron_jobs
                WHERE cron_id = ?
                LIMIT 1
                """,
                (cron_id,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        metadata_json = record.pop("metadata_json", None)
        record["metadata"] = json.loads(metadata_json) if metadata_json else {}
        record["paused"] = bool(record.get("paused_at"))
        return record

    def list_crons(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    cron_id,
                    name,
                    kind,
                    description,
                    cron_expr,
                    timezone,
                    next_fire_at,
                    last_fired_at,
                    last_result_status,
                    last_result_summary,
                    paused_at,
                    pause_reason,
                    metadata_json,
                    created_at,
                    updated_at
                FROM cron_jobs
                ORDER BY kind ASC, name ASC, cron_id ASC
                """
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            metadata_json = record.pop("metadata_json", None)
            record["metadata"] = json.loads(metadata_json) if metadata_json else {}
            record["paused"] = bool(record.get("paused_at"))
            entries.append(record)
        return entries

    def fetch_due_crons(self, *, now_iso: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    cron_id,
                    name,
                    kind,
                    description,
                    cron_expr,
                    timezone,
                    next_fire_at,
                    last_fired_at,
                    last_result_status,
                    last_result_summary,
                    paused_at,
                    pause_reason,
                    metadata_json,
                    created_at,
                    updated_at
                FROM cron_jobs
                WHERE paused_at IS NULL
                  AND next_fire_at IS NOT NULL
                  AND next_fire_at <= ?
                ORDER BY next_fire_at ASC, updated_at ASC
                LIMIT ?
                """,
                (now_iso, max(1, limit)),
            ).fetchall()
        due: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            metadata_json = record.pop("metadata_json", None)
            record["metadata"] = json.loads(metadata_json) if metadata_json else {}
            record["paused"] = False
            due.append(record)
        return due

    def pause_cron(self, cron_id: str, *, reason: str | None = None) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE cron_jobs
                SET paused_at = ?,
                    pause_reason = ?,
                    updated_at = ?
                WHERE cron_id = ?
                """,
                (now, (reason or "").strip() or None, now, cron_id),
            )
            connection.commit()
        return self.get_cron(cron_id)

    def resume_cron(self, cron_id: str, *, next_fire_at: str | None = None) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            if next_fire_at is None:
                connection.execute(
                    """
                    UPDATE cron_jobs
                    SET paused_at = NULL,
                        pause_reason = NULL,
                        updated_at = ?
                    WHERE cron_id = ?
                    """,
                    (now, cron_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE cron_jobs
                    SET paused_at = NULL,
                        pause_reason = NULL,
                        next_fire_at = ?,
                        updated_at = ?
                    WHERE cron_id = ?
                    """,
                    (next_fire_at, now, cron_id),
                )
            connection.commit()
        return self.get_cron(cron_id)

    def record_cron_result(
        self,
        *,
        cron_id: str,
        scheduled_for: str | None,
        status: str,
        summary: str | None,
        next_fire_at: str | None,
    ) -> dict[str, Any] | None:
        now = utcnow_iso()
        run_id = f"cronrun_{uuid4().hex}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cron_history (
                    run_id,
                    cron_id,
                    scheduled_for,
                    started_at,
                    completed_at,
                    status,
                    summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cron_id,
                    scheduled_for,
                    now,
                    now,
                    status,
                    summary,
                ),
            )
            connection.execute(
                """
                UPDATE cron_jobs
                SET last_fired_at = ?,
                    last_result_status = ?,
                    last_result_summary = ?,
                    next_fire_at = ?,
                    updated_at = ?
                WHERE cron_id = ?
                """,
                (now, status, summary, next_fire_at, now, cron_id),
            )
            connection.commit()
        return self.get_cron(cron_id)

    def list_cron_history(self, cron_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    run_id,
                    cron_id,
                    scheduled_for,
                    started_at,
                    completed_at,
                    status,
                    summary
                FROM cron_history
                WHERE cron_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (cron_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_heartbeat(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    timezone,
                    enabled,
                    paused_at,
                    pause_reason,
                    last_result_status,
                    last_result_summary,
                    updated_at
                FROM heartbeat_config
                WHERE config_id = 'default'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("heartbeat config has not been initialized")
        record = dict(row)
        record["enabled"] = bool(record.get("enabled"))
        record["paused"] = bool(record.get("paused_at"))
        return record

    def pause_heartbeat(self, *, reason: str | None = None) -> dict[str, Any]:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_config
                SET paused_at = ?,
                    pause_reason = ?,
                    updated_at = ?
                WHERE config_id = 'default'
                """,
                (now, (reason or "").strip() or None, now),
            )
            connection.commit()
        return self.get_heartbeat()

    def resume_heartbeat(self) -> dict[str, Any]:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_config
                SET paused_at = NULL,
                    pause_reason = NULL,
                    updated_at = ?
                WHERE config_id = 'default'
                """,
                (now,),
            )
            connection.commit()
        return self.get_heartbeat()

    def summary(self) -> dict[str, Any]:
        profile = self.get_profile()
        heartbeat = self.get_heartbeat()
        crons = self.list_crons()
        return {
            "user_timezone": profile["user_timezone"],
            "timezone_source": profile["timezone_source"],
            "cron_count": len(crons),
            "paused_cron_count": sum(1 for cron in crons if cron.get("paused")),
            "heartbeat_paused": bool(heartbeat.get("paused")),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
