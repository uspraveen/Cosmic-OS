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
