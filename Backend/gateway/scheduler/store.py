from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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

    def initialize(
        self,
        *,
        default_timezone: str,
        default_heartbeat_interval_sec: int = 1800,
    ) -> None:
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

                CREATE TABLE IF NOT EXISTS scheduler_manual_overrides (
                    override_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_label TEXT,
                    action TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT,
                    source TEXT,
                    previous_state_json TEXT NOT NULL DEFAULT '{}',
                    resulting_state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_scheduler_manual_overrides_created
                    ON scheduler_manual_overrides(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_scheduler_manual_overrides_target
                    ON scheduler_manual_overrides(target_type, target_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_config (
                    config_id TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    interval_sec INTEGER NOT NULL DEFAULT 1800,
                    prompt TEXT,
                    delivery_channel TEXT DEFAULT 'desktop',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    paused_at TEXT,
                    pause_reason TEXT,
                    last_fired_at TEXT,
                    next_fire_at TEXT,
                    last_suppressed_at TEXT,
                    last_delivered_at TEXT,
                    last_delivered_summary TEXT,
                    last_result_status TEXT,
                    last_result_summary TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS heartbeat_calendar_events (
                    event_key TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    calendar_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_changed_at TEXT,
                    last_included_at TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    summary TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_calendar_seen
                    ON heartbeat_calendar_events(last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_watchpoints (
                    watchpoint_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    created_by TEXT NOT NULL DEFAULT 'orchestrator',
                    check_kind TEXT NOT NULL DEFAULT 'manual',
                    check_config_json TEXT NOT NULL DEFAULT '{}',
                    baseline_state_json TEXT NOT NULL DEFAULT '{}',
                    notify_policy TEXT NOT NULL DEFAULT 'on_new',
                    status TEXT NOT NULL DEFAULT 'active',
                    status_reason TEXT,
                    status_changed_at TEXT,
                    status_changed_by TEXT,
                    last_checked_at TEXT,
                    last_check_status TEXT,
                    last_check_detail TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_delivered_at TEXT,
                    delivery_count INTEGER NOT NULL DEFAULT 0,
                    created_request_id TEXT,
                    created_session_id TEXT,
                    created_channel TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_watchpoints_status
                    ON heartbeat_watchpoints(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_watchpoint_events (
                    event_id TEXT PRIMARY KEY,
                    watchpoint_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    status TEXT,
                    reason TEXT,
                    actor TEXT,
                    source TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(watchpoint_id)
                        REFERENCES heartbeat_watchpoints(watchpoint_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_watchpoint_events_wp
                    ON heartbeat_watchpoint_events(watchpoint_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_beat_notes (
                    note_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'note',
                    outcome TEXT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    stale_reason TEXT,
                    stale_at TEXT,
                    stale_by TEXT,
                    author TEXT NOT NULL DEFAULT 'orchestrator',
                    request_id TEXT,
                    session_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_beat_notes_created
                    ON heartbeat_beat_notes(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_heartbeat_beat_notes_status
                    ON heartbeat_beat_notes(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS heartbeat_consumptions (
                    consumption_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    message_id TEXT,
                    request_id TEXT,
                    source_id TEXT,
                    channel TEXT,
                    platform TEXT,
                    device_id TEXT,
                    consumed_via TEXT,
                    consumed_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_consumptions_session
                    ON heartbeat_consumptions(session_id, consumed_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_heartbeat_consumptions_message
                    ON heartbeat_consumptions(message_id)
                    WHERE message_id IS NOT NULL AND message_id != '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_heartbeat_consumptions_request
                    ON heartbeat_consumptions(request_id)
                    WHERE request_id IS NOT NULL AND request_id != '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_heartbeat_consumptions_source
                    ON heartbeat_consumptions(source_id)
                    WHERE source_id IS NOT NULL AND source_id != '';
                """
            )
            self._ensure_heartbeat_columns(connection)
            now = utcnow_iso()
            heartbeat_interval_sec = max(60, int(default_heartbeat_interval_sec or 1800))
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
                    interval_sec,
                    prompt,
                    delivery_channel,
                    enabled,
                    paused_at,
                    pause_reason,
                    last_fired_at,
                    next_fire_at,
                    last_suppressed_at,
                    last_delivered_at,
                    last_delivered_summary,
                    last_result_status,
                    last_result_summary,
                    updated_at
                )
                VALUES ('default', ?, ?, NULL, 'desktop', 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(config_id) DO NOTHING
                """,
                (default_timezone, heartbeat_interval_sec, now),
            )
            connection.execute(
                """
                UPDATE heartbeat_config
                SET interval_sec = CASE
                        WHEN interval_sec IS NULL OR interval_sec < 60 THEN ?
                        ELSE interval_sec
                    END,
                    delivery_channel = CASE
                        WHEN delivery_channel IS NULL OR TRIM(delivery_channel) = '' THEN 'desktop'
                        ELSE delivery_channel
                    END
                WHERE config_id = 'default'
                """,
                (heartbeat_interval_sec,),
            )
            connection.execute(
                """
                UPDATE heartbeat_config
                SET last_delivered_at = COALESCE(last_delivered_at, last_fired_at),
                    last_delivered_summary = COALESCE(last_delivered_summary, last_result_summary)
                WHERE config_id = 'default'
                  AND last_result_status = 'delivered'
                  AND (last_delivered_summary IS NULL OR TRIM(last_delivered_summary) = '')
                """
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

    def delete_cron(self, cron_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM cron_jobs
                WHERE cron_id = ?
                """,
                (cron_id,),
            )
            connection.commit()
        return bool(cursor.rowcount)

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

    def record_manual_override(
        self,
        *,
        target_type: str,
        target_id: str,
        action: str,
        reason: str | None = None,
        actor: str | None = None,
        source: str | None = None,
        previous_state: dict[str, Any] | None = None,
        resulting_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        previous = previous_state or {}
        resulting = resulting_state or {}
        target_label = (
            str(resulting.get("label") or resulting.get("name") or "").strip()
            or str(previous.get("label") or previous.get("name") or "").strip()
            or None
        )
        row = {
            "override_id": f"sched_override_{uuid4().hex}",
            "target_type": str(target_type or "").strip() or "cron",
            "target_id": str(target_id or "").strip(),
            "target_label": target_label,
            "action": str(action or "").strip(),
            "reason": (reason or "").strip() or None,
            "actor": (actor or "").strip() or None,
            "source": (source or "").strip() or None,
            "previous_state": previous,
            "resulting_state": resulting,
            "created_at": now,
        }
        if not row["target_id"] or not row["action"]:
            raise ValueError("target_id and action are required")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_manual_overrides (
                    override_id,
                    target_type,
                    target_id,
                    target_label,
                    action,
                    reason,
                    actor,
                    source,
                    previous_state_json,
                    resulting_state_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["override_id"],
                    row["target_type"],
                    row["target_id"],
                    row["target_label"],
                    row["action"],
                    row["reason"],
                    row["actor"],
                    row["source"],
                    json.dumps(previous, sort_keys=True, default=str),
                    json.dumps(resulting, sort_keys=True, default=str),
                    now,
                ),
            )
            connection.commit()
        return row

    def list_manual_overrides(
        self,
        *,
        target_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized_target_type = str(target_type or "").strip()
        sql = """
            SELECT
                override_id,
                target_type,
                target_id,
                target_label,
                action,
                reason,
                actor,
                source,
                previous_state_json,
                resulting_state_json,
                created_at
            FROM scheduler_manual_overrides
        """
        params: list[Any] = []
        if normalized_target_type:
            sql += " WHERE target_type = ?"
            params.append(normalized_target_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 20))))
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            previous_json = entry.pop("previous_state_json", None)
            resulting_json = entry.pop("resulting_state_json", None)
            try:
                entry["previous_state"] = json.loads(previous_json) if previous_json else {}
            except (TypeError, json.JSONDecodeError):
                entry["previous_state"] = {}
            try:
                entry["resulting_state"] = json.loads(resulting_json) if resulting_json else {}
            except (TypeError, json.JSONDecodeError):
                entry["resulting_state"] = {}
            entries.append(entry)
        return entries

    def get_heartbeat(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    timezone,
                    interval_sec,
                    prompt,
                    delivery_channel,
                    enabled,
                    paused_at,
                    pause_reason,
                    last_fired_at,
                    next_fire_at,
                    last_suppressed_at,
                    last_delivered_at,
                    last_delivered_summary,
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
        try:
            interval_sec = int(record.get("interval_sec") or 1800)
        except (TypeError, ValueError):
            interval_sec = 1800
        record["interval_sec"] = max(60, interval_sec)
        record["delivery_channel"] = (
            str(record.get("delivery_channel") or "").strip() or "desktop"
        )
        return record

    def schedule_heartbeat(self, *, next_fire_at: str | None) -> dict[str, Any]:
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_config
                SET next_fire_at = ?,
                    updated_at = ?
                WHERE config_id = 'default'
                """,
                (next_fire_at, now),
            )
            connection.commit()
        return self.get_heartbeat()

    def record_heartbeat_result(
        self,
        *,
        status: str,
        summary: str | None,
        next_fire_at: str | None,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        normalized_status = str(status or "").strip() or "completed"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_config
                SET last_fired_at = ?,
                    next_fire_at = ?,
                    last_suppressed_at = CASE
                        WHEN ? = 'suppressed' THEN ?
                        ELSE last_suppressed_at
                    END,
                    last_delivered_at = CASE
                        WHEN ? = 'delivered' THEN ?
                        ELSE last_delivered_at
                    END,
                    last_delivered_summary = CASE
                        WHEN ? = 'delivered' THEN ?
                        ELSE last_delivered_summary
                    END,
                    last_result_status = ?,
                    last_result_summary = ?,
                    updated_at = ?
                WHERE config_id = 'default'
                """,
                (
                    now,
                    next_fire_at,
                    normalized_status,
                    now,
                    normalized_status,
                    now,
                    normalized_status,
                    summary,
                    normalized_status,
                    summary,
                    now,
                ),
            )
            connection.commit()
        note_content = (summary or "").strip() or f"Heartbeat {normalized_status}."
        self.append_heartbeat_beat_note(
            content=note_content,
            kind="beat",
            outcome=normalized_status,
            author="gateway",
        )
        return self.get_heartbeat()

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
            "heartbeat_enabled": bool(heartbeat.get("enabled")),
            "heartbeat_next_fire_at": heartbeat.get("next_fire_at"),
            "heartbeat_last_result_status": heartbeat.get("last_result_status"),
        }

    def annotate_heartbeat_calendar_events(
        self,
        events: list[dict[str, Any]],
        *,
        included_at: str | None = None,
    ) -> dict[str, Any]:
        now = included_at or utcnow_iso()
        annotated: list[dict[str, Any]] = []
        new_count = 0
        changed_count = 0
        seen_count = 0
        with self._lock, self._connect() as connection:
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_key = self._heartbeat_calendar_event_key(event)
                if not event_key:
                    continue
                signature = self._heartbeat_calendar_event_signature(event)
                row = connection.execute(
                    """
                    SELECT signature, first_seen_at, last_seen_at, last_changed_at
                    FROM heartbeat_calendar_events
                    WHERE event_key = ?
                    """,
                    (event_key,),
                ).fetchone()
                is_new = row is None
                is_changed = bool(row is not None and row["signature"] != signature)
                if is_new:
                    new_count += 1
                    first_seen_at = now
                    last_changed_at = None
                    previous_seen_at = None
                else:
                    seen_count += 1
                    first_seen_at = str(row["first_seen_at"] or now)
                    previous_seen_at = str(row["last_seen_at"] or "") or None
                    last_changed_at = str(row["last_changed_at"] or "") or None
                    if is_changed:
                        changed_count += 1
                        last_changed_at = now
                payload = dict(event)
                payload.update(
                    {
                        "heartbeat_event_key": event_key,
                        "heartbeat_seen_before": not is_new,
                        "heartbeat_new": is_new,
                        "heartbeat_changed": is_changed,
                        "heartbeat_first_seen_at": first_seen_at,
                        "heartbeat_previous_seen_at": previous_seen_at,
                        "heartbeat_last_changed_at": last_changed_at,
                    }
                )
                annotated.append(payload)
                connection.execute(
                    """
                    INSERT INTO heartbeat_calendar_events (
                        event_key,
                        account_id,
                        calendar_id,
                        event_id,
                        signature,
                        first_seen_at,
                        last_seen_at,
                        last_changed_at,
                        last_included_at,
                        start_at,
                        end_at,
                        summary,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        signature = excluded.signature,
                        last_seen_at = excluded.last_seen_at,
                        last_changed_at = COALESCE(excluded.last_changed_at, heartbeat_calendar_events.last_changed_at),
                        last_included_at = excluded.last_included_at,
                        start_at = excluded.start_at,
                        end_at = excluded.end_at,
                        summary = excluded.summary,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        event_key,
                        str(event.get("account_id") or ""),
                        str(event.get("calendar_id") or ""),
                        str(event.get("event_id") or event.get("id") or ""),
                        signature,
                        first_seen_at,
                        now,
                        last_changed_at,
                        now,
                        str(event.get("start") or ""),
                        str(event.get("end") or ""),
                        str(event.get("summary") or ""),
                        json.dumps(event, sort_keys=True),
                    ),
                )
            cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat().replace(
                "+00:00",
                "Z",
            )
            connection.execute(
                """
                DELETE FROM heartbeat_calendar_events
                WHERE last_seen_at < ?
                """,
                (cutoff,),
            )
            connection.commit()
        return {
            "events": annotated,
            "event_count": len(annotated),
            "new_event_count": new_count,
            "changed_event_count": changed_count,
            "seen_event_count": seen_count,
            "included_at": now,
        }

    def record_heartbeat_consumption(
        self,
        *,
        session_id: str | None,
        message_id: str | None,
        request_id: str | None,
        source_id: str | None,
        channel: str | None,
        platform: str | None,
        device_id: str | None,
        consumed_via: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = {
            "session_id": self._clean_optional_text(session_id),
            "message_id": self._clean_optional_text(message_id),
            "request_id": self._clean_optional_text(request_id),
            "source_id": self._clean_optional_text(source_id),
            "channel": self._clean_optional_text(channel),
            "platform": self._clean_optional_text(platform),
            "device_id": self._clean_optional_text(device_id),
            "consumed_via": self._clean_optional_text(consumed_via),
        }
        if not (
            normalized["message_id"]
            or normalized["request_id"]
            or normalized["source_id"]
        ):
            raise ValueError(
                "heartbeat consumption requires a message_id, request_id, or source_id"
            )
        now = utcnow_iso()
        metadata_json = json.dumps(
            metadata if isinstance(metadata, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            existing = self._find_heartbeat_consumption_row(
                connection,
                message_id=normalized["message_id"],
                request_id=normalized["request_id"],
                source_id=normalized["source_id"],
            )
            if existing is not None:
                connection.execute(
                    """
                    UPDATE heartbeat_consumptions
                    SET session_id = COALESCE(?, session_id),
                        message_id = COALESCE(?, message_id),
                        request_id = COALESCE(?, request_id),
                        source_id = COALESCE(?, source_id),
                        channel = COALESCE(?, channel),
                        platform = COALESCE(?, platform),
                        device_id = COALESCE(?, device_id),
                        consumed_via = COALESCE(?, consumed_via),
                        consumed_at = ?,
                        metadata_json = ?
                    WHERE consumption_id = ?
                    """,
                    (
                        normalized["session_id"],
                        normalized["message_id"],
                        normalized["request_id"],
                        normalized["source_id"],
                        normalized["channel"],
                        normalized["platform"],
                        normalized["device_id"],
                        normalized["consumed_via"],
                        now,
                        metadata_json,
                        existing["consumption_id"],
                    ),
                )
                consumption_id = str(existing["consumption_id"])
            else:
                consumption_id = f"hbcon_{uuid4().hex[:16]}"
                connection.execute(
                    """
                    INSERT INTO heartbeat_consumptions (
                        consumption_id,
                        session_id,
                        message_id,
                        request_id,
                        source_id,
                        channel,
                        platform,
                        device_id,
                        consumed_via,
                        consumed_at,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        consumption_id,
                        normalized["session_id"],
                        normalized["message_id"],
                        normalized["request_id"],
                        normalized["source_id"],
                        normalized["channel"],
                        normalized["platform"],
                        normalized["device_id"],
                        normalized["consumed_via"],
                        now,
                        metadata_json,
                    ),
                )
            connection.commit()
            row = connection.execute(
                """
                SELECT *
                FROM heartbeat_consumptions
                WHERE consumption_id = ?
                """,
                (consumption_id,),
            ).fetchone()
        return self._heartbeat_consumption_record(row)

    def list_heartbeat_consumptions(
        self,
        *,
        session_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        normalized_session_id = self._clean_optional_text(session_id)
        bounded_limit = max(1, min(int(limit or 500), 1000))
        with self._lock, self._connect() as connection:
            if normalized_session_id:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM heartbeat_consumptions
                    WHERE session_id = ?
                    ORDER BY consumed_at DESC
                    LIMIT ?
                    """,
                    (normalized_session_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM heartbeat_consumptions
                    ORDER BY consumed_at DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
        return [self._heartbeat_consumption_record(row) for row in rows if row is not None]

    # ── Heartbeat watchpoints: durable standing commitments ─────────────
    #
    # Replaces the free-text "Active watchpoints" section of the old
    # heartbeat_notes.md. Rows are never hard-deleted; staleness/deactivation
    # is a soft status transition with a reason, so "what happened to this
    # watch?" stays queryable forever.

    HEARTBEAT_WATCHPOINT_STATUSES = frozenset(
        {"active", "stale", "inactive", "superseded", "completed"}
    )
    HEARTBEAT_CHECK_STATUSES = frozenset({"ok", "inconclusive", "failed"})
    HEARTBEAT_BEAT_NOTE_KINDS = frozenset(
        {"note", "plan", "watchpoint", "beat", "legacy_import"}
    )

    def upsert_heartbeat_watchpoint(
        self,
        *,
        watchpoint_id: str | None = None,
        name: str,
        description: str | None = None,
        created_by: str = "orchestrator",
        check_kind: str = "manual",
        check_config: dict[str, Any] | None = None,
        baseline_state: dict[str, Any] | None = None,
        notify_policy: str = "on_new",
        request_id: str | None = None,
        session_id: str | None = None,
        channel: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a durable heartbeat watchpoint.

        Watchpoints are never hard-deleted; deactivation goes through
        :meth:`set_heartbeat_watchpoint_status` so the history of "what happened
        to this watch?" stays queryable.
        """
        normalized_name = str(name or "").strip()
        normalized_notify = str(notify_policy or "on_new").strip() or "on_new"
        if normalized_notify not in {"on_new", "on_every_check", "manual"}:
            raise ValueError("notify_policy must be one of: on_new, on_every_check, manual")
        check_config_json = json.dumps(
            check_config if isinstance(check_config, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        )
        baseline_state_json = json.dumps(
            baseline_state if isinstance(baseline_state, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        )
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            existing = None
            if watchpoint_id:
                existing = connection.execute(
                    "SELECT * FROM heartbeat_watchpoints WHERE watchpoint_id = ?",
                    (watchpoint_id,),
                    ).fetchone()
            if existing is not None and not normalized_name:
                normalized_name = str(existing["name"] or "").strip()
            if not normalized_name:
                raise ValueError("watchpoint name is required")
            if existing is not None:
                connection.execute(
                    """
                    UPDATE heartbeat_watchpoints
                    SET name = ?,
                        description = COALESCE(?, description),
                        check_kind = ?,
                        check_config_json = ?,
                        baseline_state_json = COALESCE(?, baseline_state_json),
                        notify_policy = ?,
                        updated_at = ?
                    WHERE watchpoint_id = ?
                    """,
                    (
                        normalized_name,
                        description,
                        str(check_kind or "manual").strip() or "manual",
                        check_config_json,
                        baseline_state_json,
                        normalized_notify,
                        now,
                        watchpoint_id,
                    ),
                )
            else:
                watchpoint_id = watchpoint_id or f"hbwp_{uuid4().hex[:12]}"
                connection.execute(
                    """
                    INSERT INTO heartbeat_watchpoints (
                        watchpoint_id,
                        name,
                        description,
                        created_by,
                        check_kind,
                        check_config_json,
                        baseline_state_json,
                        notify_policy,
                        created_request_id,
                        created_session_id,
                        created_channel,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        watchpoint_id,
                        normalized_name,
                        description,
                        created_by or "orchestrator",
                        check_kind or "manual",
                        check_config_json,
                        baseline_state_json,
                        normalized_notify,
                        request_id,
                        session_id,
                        channel,
                        now,
                        now,
                    ),
                )
                self._insert_heartbeat_watchpoint_event(
                    connection,
                    watchpoint_id=watchpoint_id,
                    event="created",
                    status="active",
                    reason="Watchpoint registered.",
                    actor=actor or created_by or "orchestrator",
                    source="orchestrator",
                    details={
                        "name": normalized_name,
                        "check_kind": str(check_kind or "manual").strip() or "manual",
                        "notify_policy": normalized_notify,
                    },
                )
            if existing is not None:
                self._insert_heartbeat_watchpoint_event(
                    connection,
                    watchpoint_id=watchpoint_id,
                    event="updated",
                    actor=actor or "orchestrator",
                    details={
                        "name": normalized_name,
                        "check_kind": str(check_kind or "manual").strip() or "manual",
                        "notify_policy": normalized_notify,
                        "baseline_state_replaced": isinstance(baseline_state, dict),
                    },
                )
            connection.commit()
            row = self._get_heartbeat_watchpoint_row(connection, watchpoint_id)
        return self._heartbeat_watchpoint_record(row)

    def get_heartbeat_watchpoint(self, watchpoint_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM heartbeat_watchpoints WHERE watchpoint_id = ?",
                (watchpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return self._heartbeat_watchpoint_record(row)

    def list_heartbeat_watchpoints(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List active watchpoints; pass include_inactive=True for forensics.

        Inactive watchpoints are kept forever (soft state) and sorted after
        active ones, so a later "where did my visitor notification go?" question
        is answerable from the registry alone.
        """
        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._lock, self._connect() as connection:
            if include_inactive:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM heartbeat_watchpoints
                    ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END ASC,
                             updated_at DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM heartbeat_watchpoints
                    WHERE status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
        return [self._heartbeat_watchpoint_record(row) for row in rows if row is not None]

    def set_heartbeat_watchpoint_status(
        self,
        watchpoint_id: str,
        *,
        status: str,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        """Soft-transition a watchpoint; rows are never hard-deleted here."""
        normalized_status = str(status or "").strip()
        if normalized_status not in self.HEARTBEAT_WATCHPOINT_STATUSES:
            raise ValueError(
                "watchpoint status must be one of: "
                + ", ".join(sorted(self.HEARTBEAT_WATCHPOINT_STATUSES))
            )
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM heartbeat_watchpoints WHERE watchpoint_id = ?",
                (watchpoint_id,),
            ).fetchone()
            if row is None:
                return None
            previous_status = str(row["status"] or "active")
            if previous_status == normalized_status and not (reason or "").strip():
                return self._heartbeat_watchpoint_record(row)
            connection.execute(
                """
                UPDATE heartbeat_watchpoints
                SET status = ?,
                    status_reason = ?,
                    status_changed_at = ?,
                    status_changed_by = ?,
                    updated_at = ?
                WHERE watchpoint_id = ?
                """,
                (
                    normalized_status,
                    (reason or "").strip() or None,
                    now,
                    actor or "user",
                    now,
                    watchpoint_id,
                ),
            )
            self._insert_heartbeat_watchpoint_event(
                connection,
                watchpoint_id=watchpoint_id,
                event="status_changed",
                status=normalized_status,
                reason=reason,
                actor=actor or "user",
                details={"previous_status": previous_status},
            )
            connection.commit()
        return self.get_heartbeat_watchpoint(watchpoint_id)

    def record_heartbeat_watchpoint_check(
        self,
        watchpoint_id: str,
        *,
        check_status: str,
        detail: str | None = None,
        baseline_state: dict[str, Any] | None = None,
        delivered: bool = False,
    ) -> dict[str, Any] | None:
        """Record a gateway-side check outcome for a watchpoint."""
        normalized_status = str(check_status or "").strip() or "ok"
        if normalized_status not in self.HEARTBEAT_CHECK_STATUSES:
            raise ValueError("check_status must be one of: ok, inconclusive, failed")
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM heartbeat_watchpoints WHERE watchpoint_id = ?",
                (watchpoint_id,),
            ).fetchone()
            if existing is None:
                return None
            baseline_json = (
                json.dumps(baseline_state, sort_keys=True, separators=(",", ":"))
                if isinstance(baseline_state, dict)
                else None
            )
            connection.execute(
                """
                UPDATE heartbeat_watchpoints
                SET last_checked_at = ?,
                    last_check_status = ?,
                    last_check_detail = ?,
                    consecutive_failures = CASE
                        WHEN ? = 'ok' THEN 0
                        ELSE consecutive_failures + 1
                    END,
                    last_delivered_at = CASE WHEN ? THEN ? ELSE last_delivered_at END,
                    delivery_count = delivery_count + CASE WHEN ? THEN 1 ELSE 0 END,
                    baseline_state_json = COALESCE(?, baseline_state_json),
                    updated_at = ?
                WHERE watchpoint_id = ?
                """,
                (
                    now,
                    normalized_status,
                    self._clean_optional_text(detail),
                    normalized_status,
                    bool(delivered),
                    now if delivered else None,
                    bool(delivered),
                    baseline_json,
                    now,
                    watchpoint_id,
                ),
            )
            if normalized_status != "ok" or delivered:
                self._insert_heartbeat_watchpoint_event(
                    connection,
                    watchpoint_id=watchpoint_id,
                    event="check",
                    status=normalized_status,
                    reason=self._clean_optional_text(detail),
                    actor="orchestrator",
                    details={"delivered": bool(delivered)},
                )
            connection.commit()
        return self.get_heartbeat_watchpoint(watchpoint_id)

    def list_heartbeat_watchpoint_history(
        self,
        watchpoint_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM heartbeat_watchpoint_events
                WHERE watchpoint_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (watchpoint_id, max(1, min(200, int(limit or 20)))),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            details_json = entry.pop("details_json", None)
            try:
                entry["details"] = json.loads(details_json) if details_json else {}
            except (TypeError, ValueError):
                entry["details"] = {}
            entries.append(entry)
        return entries


    # ── Heartbeat beat notes (the notes markdown, as rows) ──────────────
    #
    # Replaces the free-text suppression log of heartbeat_notes.md. Appends
    # never rewrite existing rows, so notes can no longer be silently
    # amputated the way the 32K markdown file was.

    def append_heartbeat_beat_note(
        self,
        *,
        content: str,
        kind: str = "note",
        outcome: str | None = None,
        author: str = "orchestrator",
        request_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one note row. Appends never rewrite existing rows."""
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("heartbeat beat note content is required")
        normalized_kind = str(kind or "").strip() or "note"
        if normalized_kind not in self.HEARTBEAT_BEAT_NOTE_KINDS:
            raise ValueError(
                "heartbeat beat note kind must be one of: "
                + ", ".join(sorted(self.HEARTBEAT_BEAT_NOTE_KINDS))
            )
        now = utcnow_iso()
        note_id = f"hbnote_{uuid4().hex[:16]}"
        metadata_json = json.dumps(
            metadata if isinstance(metadata, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO heartbeat_beat_notes (
                    note_id, kind, outcome, content, status, author,
                    request_id, session_id, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    normalized_kind,
                    outcome,
                    normalized_content,
                    author or "orchestrator",
                    request_id,
                    session_id,
                    metadata_json,
                    now,
                    now,
                ),
            )
            if normalized_kind == "beat":
                self._prune_heartbeat_beat_notes(connection)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM heartbeat_beat_notes WHERE note_id = ?",
                (note_id,),
            ).fetchone()
        return self._heartbeat_beat_note_record(row)

    def list_heartbeat_beat_notes(
        self,
        *,
        limit: int = 50,
        include_stale: bool = False,
        kind: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        """List beat notes oldest-first (render order for the beat prompt)."""
        bounded_limit = max(1, min(int(limit or 50), 500))
        clauses: list[str] = []
        params: list[Any] = []
        if not include_stale:
            clauses.append("status = 'active'")
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(bounded_limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM heartbeat_beat_notes
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        records = [self._heartbeat_beat_note_record(row) for row in rows]
        records.reverse()
        return records

    def mark_heartbeat_beat_note_stale(
        self,
        *,
        note_id: str | None = None,
        match: str | None = None,
        reason: str | None = None,
        actor: str | None = None,
    ) -> int:
        """Soft-invalidate notes by id or content substring (history survives)."""
        normalized_match = (match or "").strip()
        normalized_note_id = (note_id or "").strip()
        if not normalized_note_id and not normalized_match:
            raise ValueError("requires note_id or match text")
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            if normalized_note_id:
                cursor = connection.execute(
                    """
                    UPDATE heartbeat_beat_notes
                    SET status = 'stale',
                        stale_reason = ?,
                        stale_at = ?,
                        stale_by = ?,
                        updated_at = ?
                    WHERE note_id = ? AND status = 'active'
                    """,
                    (
                        reason or "Removed via heartbeat_notes tool",
                        now,
                        actor or "orchestrator",
                        now,
                        normalized_note_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE heartbeat_beat_notes
                    SET status = 'stale',
                        stale_reason = ?,
                        stale_at = ?,
                        stale_by = ?,
                        updated_at = ?
                    WHERE status = 'active' AND instr(content, ?) > 0
                    """,
                    (
                        reason or "Removed via heartbeat_notes tool",
                        now,
                        actor or "orchestrator",
                        now,
                        normalized_match,
                    ),
                )
            connection.commit()
            return int(cursor.rowcount)

    def replace_heartbeat_beat_notes(
        self,
        *,
        content: str,
        author: str = "orchestrator",
        request_id: str | None = None,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Soft-replace: mark every active note stale, then insert new content."""
        normalized_content = str(content or "").strip()
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE heartbeat_beat_notes
                SET status = 'stale',
                    stale_reason = ?,
                    stale_at = ?,
                    stale_by = ?,
                    updated_at = ?
                WHERE status = 'active'
                """,
                (
                    reason or "Replaced by newer heartbeat notes",
                    now,
                    author or "orchestrator",
                    now,
                ),
            )
            if normalized_content:
                connection.execute(
                    """
                    INSERT INTO heartbeat_beat_notes (
                        note_id, kind, content, status, author,
                        request_id, session_id, created_at, updated_at
                    ) VALUES (?, 'note', ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (
                        f"hbnote_{uuid4().hex[:16]}",
                        normalized_content,
                        author or "orchestrator",
                        request_id,
                        session_id,
                        now,
                        now,
                    ),
                )
            connection.commit()
        return 1

    def render_heartbeat_notes(
        self,
        *,
        limit_items: int = 60,
        include_stale: bool = False,
    ) -> str:
        """Render beat notes oldest-first as plain lines for a beat prompt.

        The gateway runtime applies the head+tail excerpt on this text; the
        store only renders rows and never trims content.
        """
        items = self.list_heartbeat_beat_notes(
            limit=max(20, min(int(limit_items or 60), 200)),
            include_stale=include_stale,
        )
        lines: list[str] = []
        for item in items:
            created_at = item.get("created_at") or "?"
            kind = str(item.get("kind") or "note")
            outcome = item.get("outcome") or ""
            label = f"{kind}/{outcome}" if outcome else kind
            content = " ".join(str(item.get("content") or "").split())
            stamp = self._format_note_stamp(created_at)
            lines.append(f"- [{stamp} | {label}] {content}")
        return "\n".join(lines).strip()

    # ── internal helpers ────────────────────────────────────────────────

    def _get_heartbeat_watchpoint_row(
        self,
        connection: sqlite3.Connection,
        watchpoint_id: str,
    ):
        return connection.execute(
            "SELECT * FROM heartbeat_watchpoints WHERE watchpoint_id = ? LIMIT 1",
            (watchpoint_id,),
        ).fetchone()

    def _insert_heartbeat_watchpoint_event(
        self,
        connection: sqlite3.Connection,
        *,
        watchpoint_id: str,
        event: str,
        status: str | None = None,
        reason: str | None = None,
        actor: str | None = None,
        source: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO heartbeat_watchpoint_events (
                event_id, watchpoint_id, event, status, reason, actor, source,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"hbwe_{uuid4().hex[:16]}",
                watchpoint_id,
                event,
                status,
                reason,
                actor,
                source or "orchestrator",
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                utcnow_iso(),
            ),
        )

    @staticmethod
    def _prune_heartbeat_beat_notes(connection: sqlite3.Connection) -> None:
        """Deterministic retention — SQL, not prose parsing.

        - Suppress beats: keep all for 48h, then 1 newest per UTC day for 14 days.
        - Delivered/failed/completed beats: 30 days.
        - Stale model notes: 14 days.
        Watchpoint rows are not pruned here.
        """
        now = datetime.now(timezone.utc)
        cutoff_30d = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        cutoff_14d = (now - timedelta(days=14)).isoformat().replace("+00:00", "Z")
        cutoff_48h = (now - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            DELETE FROM heartbeat_beat_notes
            WHERE kind = 'beat'
              AND IFNULL(outcome, '') != 'suppressed'
              AND created_at < ?
            """,
            (cutoff_30d,),
        )
        connection.execute(
            """
            DELETE FROM heartbeat_beat_notes
            WHERE kind = 'beat'
              AND outcome = 'suppressed'
              AND created_at < ?
            """,
            (cutoff_14d,),
        )
        connection.execute(
            """
            DELETE FROM heartbeat_beat_notes
            WHERE kind = 'beat'
              AND outcome = 'suppressed'
              AND created_at < ?
              AND note_id NOT IN (
                  SELECT note_id FROM (
                      SELECT note_id,
                             ROW_NUMBER() OVER (
                                 PARTITION BY substr(created_at, 1, 10)
                                 ORDER BY created_at DESC
                             ) AS rn
                      FROM heartbeat_beat_notes
                      WHERE kind = 'beat' AND outcome = 'suppressed'
                  )
                  WHERE rn = 1
              )
            """,
            (cutoff_48h,),
        )
        connection.execute(
            """
            DELETE FROM heartbeat_beat_notes
            WHERE status = 'stale'
              AND stale_at IS NOT NULL
              AND stale_at < ?
            """,
            (cutoff_14d,),
        )

    @staticmethod
    def _heartbeat_watchpoint_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        record = dict(row)
        for column, key in (
            ("check_config_json", "check_config"),
            ("baseline_state_json", "baseline_state"),
        ):
            raw = record.pop(column, None)
            try:
                parsed = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                parsed = {}
            record[key] = parsed if isinstance(parsed, dict) else {}
        return record

    @staticmethod
    def _heartbeat_beat_note_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        record = dict(row)
        try:
            metadata = json.loads(record.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        record.pop("metadata_json", None)
        return record

    @staticmethod
    def _format_note_stamp(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "?"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%m/%d %H:%M UTC")

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _clean_optional_text(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _find_heartbeat_consumption_row(
        self,
        connection: sqlite3.Connection,
        *,
        message_id: str | None,
        request_id: str | None,
        source_id: str | None,
    ) -> sqlite3.Row | None:
        for column, value in (
            ("message_id", message_id),
            ("request_id", request_id),
            ("source_id", source_id),
        ):
            if not value:
                continue
            row = connection.execute(
                f"""
                SELECT *
                FROM heartbeat_consumptions
                WHERE {column} = ?
                LIMIT 1
                """,
                (value,),
            ).fetchone()
            if row is not None:
                return row
        return None

    @staticmethod
    def _heartbeat_consumption_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        record = dict(row)
        try:
            metadata = json.loads(record.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        record["metadata"] = metadata if isinstance(metadata, dict) else {}
        record.pop("metadata_json", None)
        return record

    @staticmethod
    def _heartbeat_calendar_event_key(event: dict[str, Any]) -> str:
        account_id = str(event.get("account_id") or "").strip()
        calendar_id = str(event.get("calendar_id") or "").strip()
        event_id = str(event.get("event_id") or event.get("id") or "").strip()
        if not account_id or not calendar_id or not event_id:
            return ""
        return f"{account_id}:{calendar_id}:{event_id}"

    @staticmethod
    def _heartbeat_calendar_event_signature(event: dict[str, Any]) -> str:
        payload = {
            "summary": str(event.get("summary") or "").strip(),
            "start": str(event.get("start") or "").strip(),
            "end": str(event.get("end") or "").strip(),
            "location": str(event.get("location") or "").strip(),
            "status": str(event.get("status") or "").strip(),
            "meetingLink": str(event.get("meetingLink") or event.get("meeting_link") or "").strip(),
            "calendar_id": str(event.get("calendar_id") or "").strip(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _ensure_heartbeat_columns(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA table_info(heartbeat_config)").fetchall()
        existing = {str(row["name"]) for row in rows}
        columns = {
            "interval_sec": "interval_sec INTEGER NOT NULL DEFAULT 1800",
            "prompt": "prompt TEXT",
            "delivery_channel": "delivery_channel TEXT DEFAULT 'desktop'",
            "last_fired_at": "last_fired_at TEXT",
            "next_fire_at": "next_fire_at TEXT",
            "last_suppressed_at": "last_suppressed_at TEXT",
            "last_delivered_at": "last_delivered_at TEXT",
            "last_delivered_summary": "last_delivered_summary TEXT",
        }
        for name, ddl in columns.items():
            if name in existing:
                continue
            connection.execute(f"ALTER TABLE heartbeat_config ADD COLUMN {ddl}")
