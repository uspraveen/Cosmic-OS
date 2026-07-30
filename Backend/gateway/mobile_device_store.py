import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .session_store import utcnow_iso


class MobileDeviceStore:
    """SQLite-backed registry for mobile devices connected to the Gateway."""

    _METADATA_COLUMNS: dict[str, str] = {
        "device_name": "TEXT",
        "device_name_source": "TEXT",
        "model_name": "TEXT",
        "brand": "TEXT",
        "manufacturer": "TEXT",
        "platform": "TEXT",
        "os_name": "TEXT",
        "os_version": "TEXT",
        "device_type": "TEXT",
        "is_physical_device": "INTEGER",
        "app_version": "TEXT",
        "app_build": "TEXT",
    }
    _PUSH_COLUMNS: dict[str, str] = {
        "push_token": "TEXT",
        "push_token_updated_at": "TEXT",
        "fcm_token": "TEXT",
        "fcm_token_updated_at": "TEXT",
        "notifications_enabled": "INTEGER DEFAULT 1",
        "notification_preferences_json": "TEXT",
        "presence_state": "TEXT",
        "visible_screen": "TEXT",
        "last_presence_at": "TEXT",
    }
    _PRESENCE_STATES = {"foreground", "background", "inactive", "offline"}
    _VISIBLE_SCREENS = {
        "chat",
        "tasks",
        "meeting",
        "spaces",
        "manage",
        "agents",
        "agent-email",
    }

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS mobile_devices (
                    device_id TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_connected_at TEXT,
                    last_disconnected_at TEXT,
                    last_session_id TEXT,
                    last_channel TEXT,
                    last_authorized_at TEXT,
                    device_name TEXT,
                    device_name_source TEXT,
                    model_name TEXT,
                    brand TEXT,
                    manufacturer TEXT,
                    platform TEXT,
                    os_name TEXT,
                    os_version TEXT,
                    device_type TEXT,
                    is_physical_device INTEGER,
                    app_version TEXT,
                    app_build TEXT,
                    revoked_at TEXT,
                    revoke_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_mobile_devices_last_seen
                    ON mobile_devices(last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_mobile_devices_revoked
                    ON mobile_devices(revoked_at, last_seen_at DESC);
                """
            )
            self._ensure_metadata_columns(connection)
            self._ensure_push_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mobile_devices_push
                    ON mobile_devices(push_token)
                    WHERE push_token IS NOT NULL;
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mobile_devices_fcm
                    ON mobile_devices(fcm_token)
                    WHERE fcm_token IS NOT NULL;
                """
            )
            connection.commit()

    def authorize_device(
        self,
        device_id: str,
        *,
        channel: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        normalized_channel = str(channel or "").strip() or f"mobile:{normalized_device_id}"
        normalized_metadata = self._normalize_metadata(metadata)
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    last_connected_at,
                    last_channel,
                    last_authorized_at,
                    device_name,
                    device_name_source,
                    model_name,
                    brand,
                    manufacturer,
                    platform,
                    os_name,
                    os_version,
                    device_type,
                    is_physical_device,
                    app_version,
                    app_build,
                    revoked_at,
                    revoke_reason
                )
                VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL
                )
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_channel = excluded.last_channel,
                    last_authorized_at = excluded.last_authorized_at,
                    device_name = COALESCE(excluded.device_name, mobile_devices.device_name),
                    device_name_source = COALESCE(excluded.device_name_source, mobile_devices.device_name_source),
                    model_name = COALESCE(excluded.model_name, mobile_devices.model_name),
                    brand = COALESCE(excluded.brand, mobile_devices.brand),
                    manufacturer = COALESCE(excluded.manufacturer, mobile_devices.manufacturer),
                    platform = COALESCE(excluded.platform, mobile_devices.platform),
                    os_name = COALESCE(excluded.os_name, mobile_devices.os_name),
                    os_version = COALESCE(excluded.os_version, mobile_devices.os_version),
                    device_type = COALESCE(excluded.device_type, mobile_devices.device_type),
                    is_physical_device = COALESCE(excluded.is_physical_device, mobile_devices.is_physical_device),
                    app_version = COALESCE(excluded.app_version, mobile_devices.app_version),
                    app_build = COALESCE(excluded.app_build, mobile_devices.app_build),
                    revoked_at = NULL,
                    revoke_reason = NULL
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    normalized_channel,
                    now,
                    normalized_metadata["device_name"],
                    normalized_metadata["device_name_source"],
                    normalized_metadata["model_name"],
                    normalized_metadata["brand"],
                    normalized_metadata["manufacturer"],
                    normalized_metadata["platform"],
                    normalized_metadata["os_name"],
                    normalized_metadata["os_version"],
                    normalized_metadata["device_type"],
                    normalized_metadata["is_physical_device"],
                    normalized_metadata["app_version"],
                    normalized_metadata["app_build"],
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def record_connected(self, device_id: str, *, channel: str | None = None) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        normalized_channel = str(channel or "").strip() or f"mobile:{normalized_device_id}"
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    last_connected_at,
                    last_channel,
                    revoked_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_connected_at = excluded.last_connected_at,
                    last_channel = excluded.last_channel
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    now,
                    normalized_channel,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def record_session(
        self,
        device_id: str,
        *,
        session_id: str | None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        normalized_channel = str(channel or "").strip() or f"mobile:{normalized_device_id}"
        normalized_session_id = str(session_id or "").strip() or None
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    last_connected_at,
                    last_session_id,
                    last_channel,
                    revoked_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_connected_at = COALESCE(excluded.last_connected_at, mobile_devices.last_connected_at),
                    last_session_id = COALESCE(excluded.last_session_id, mobile_devices.last_session_id),
                    last_channel = excluded.last_channel
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    now,
                    normalized_session_id,
                    normalized_channel,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def record_disconnected(self, device_id: str) -> dict[str, Any] | None:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            self._ensure_push_columns(connection)
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?,
                    last_disconnected_at = ?,
                    presence_state = 'offline',
                    last_presence_at = ?
                WHERE device_id = ?
                """,
                (
                    now,
                    now,
                    now,
                    normalized_device_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_devices(self, *, limit: int = 100) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            rows = connection.execute(
                """
                SELECT *
                FROM mobile_devices
                ORDER BY last_seen_at DESC, first_seen_at DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            return None
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update_push_token(
        self,
        device_id: str,
        *,
        push_token: str | None,
        fcm_token: str | None = None,
        notifications_enabled: bool | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        normalized_token = str(push_token or "").strip()
        normalized_fcm_token = str(fcm_token or "").strip() or None
        if not normalized_device_id:
            raise ValueError("device_id is required")
        if not normalized_token and not normalized_fcm_token:
            raise ValueError("push_token or fcm_token is required")
        if normalized_token and not normalized_token.startswith("ExponentPushToken["):
            raise ValueError("push_token must be an Expo push token")
        if normalized_fcm_token and len(normalized_fcm_token) < 20:
            raise ValueError("fcm_token is invalid")

        now = utcnow_iso()
        preferences_json = (
            self._serialize_preferences(preferences)
            if preferences is not None
            else None
        )
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            self._ensure_push_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    push_token,
                    push_token_updated_at,
                    fcm_token,
                    fcm_token_updated_at,
                    notifications_enabled,
                    notification_preferences_json,
                    revoked_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    push_token = COALESCE(excluded.push_token, mobile_devices.push_token),
                    push_token_updated_at = CASE
                        WHEN excluded.push_token IS NOT NULL THEN excluded.push_token_updated_at
                        ELSE mobile_devices.push_token_updated_at
                    END,
                    fcm_token = COALESCE(excluded.fcm_token, mobile_devices.fcm_token),
                    fcm_token_updated_at = CASE
                        WHEN excluded.fcm_token IS NOT NULL THEN excluded.fcm_token_updated_at
                        ELSE mobile_devices.fcm_token_updated_at
                    END,
                    notifications_enabled = COALESCE(excluded.notifications_enabled, mobile_devices.notifications_enabled),
                    notification_preferences_json = COALESCE(excluded.notification_preferences_json, mobile_devices.notification_preferences_json),
                    revoked_at = NULL,
                    revoke_reason = NULL
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    normalized_token or None,
                    now if normalized_token else None,
                    normalized_fcm_token,
                    now if normalized_fcm_token else None,
                    None if notifications_enabled is None else 1 if notifications_enabled else 0,
                    preferences_json,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def clear_push_token(self, device_id: str) -> dict[str, Any] | None:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_push_columns(connection)
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?,
                    push_token = NULL,
                    push_token_updated_at = NULL,
                    fcm_token = NULL,
                    fcm_token_updated_at = NULL
                WHERE device_id = ?
                """,
                (now, normalized_device_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def clear_fcm_token(self, device_id: str) -> dict[str, Any] | None:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")
        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_push_columns(connection)
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?,
                    fcm_token = NULL,
                    fcm_token_updated_at = NULL
                WHERE device_id = ?
                """,
                (now, normalized_device_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update_presence(
        self,
        device_id: str,
        *,
        state: str,
        visible_screen: str | None = None,
    ) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        normalized_state = str(state or "").strip().lower()
        normalized_screen = str(visible_screen or "").strip().lower() or None
        if not normalized_device_id:
            raise ValueError("device_id is required")
        if normalized_state not in self._PRESENCE_STATES:
            raise ValueError("invalid presence state")
        if normalized_screen and normalized_screen not in self._VISIBLE_SCREENS:
            raise ValueError("invalid visible_screen")

        now = utcnow_iso()
        with self._lock, self._connect() as connection:
            self._ensure_push_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    presence_state,
                    visible_screen,
                    last_presence_at,
                    revoked_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    presence_state = excluded.presence_state,
                    visible_screen = excluded.visible_screen,
                    last_presence_at = excluded.last_presence_at
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    normalized_state,
                    normalized_screen,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def list_push_targets(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        normalized_session_id = str(session_id or "").strip() or None
        with self._lock, self._connect() as connection:
            self._ensure_push_columns(connection)
            if normalized_session_id:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM mobile_devices
                    WHERE (push_token IS NOT NULL OR fcm_token IS NOT NULL)
                      AND COALESCE(notifications_enabled, 1) = 1
                      AND revoked_at IS NULL
                      AND last_session_id = ?
                    ORDER BY last_seen_at DESC
                    """,
                    (normalized_session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM mobile_devices
                    WHERE (push_token IS NOT NULL OR fcm_token IS NOT NULL)
                      AND COALESCE(notifications_enabled, 1) = 1
                      AND revoked_at IS NULL
                    ORDER BY last_seen_at DESC
                    """
                ).fetchall()
        return self._dedupe_push_targets([self._row_to_record(row) for row in rows])

    def is_revoked(self, device_id: str) -> bool:
        row = self.get_device(device_id)
        return bool(row and row.get("revoked_at"))

    def revoke_device(self, device_id: str, *, reason: str | None = None) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        normalized_reason = str(reason or "").strip() or "Revoked from desktop"
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    revoked_at,
                    revoke_reason,
                    last_channel
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    revoked_at = excluded.revoked_at,
                    revoke_reason = excluded.revoke_reason,
                    last_channel = COALESCE(mobile_devices.last_channel, excluded.last_channel)
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    now,
                    normalized_reason,
                    f"mobile:{normalized_device_id}",
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row)

    def revoke_all_devices(self, *, reason: str | None = None) -> dict[str, Any]:
        now = utcnow_iso()
        normalized_reason = str(reason or "").strip() or "Revoked from desktop"
        with self._lock, self._connect() as connection:
            self._ensure_metadata_columns(connection)
            device_rows = connection.execute(
                "SELECT device_id FROM mobile_devices ORDER BY last_seen_at DESC, first_seen_at DESC"
            ).fetchall()
            device_ids = [str(row["device_id"]) for row in device_rows if row["device_id"]]
            if device_ids:
                connection.execute(
                    """
                    UPDATE mobile_devices
                    SET last_seen_at = ?, revoked_at = ?, revoke_reason = ?
                    """,
                    (
                        now,
                        now,
                        normalized_reason,
                    ),
                )
            connection.commit()
        return {
            "revoked_count": len(device_ids),
            "device_ids": device_ids,
            "revoked_at": now,
            "reason": normalized_reason,
        }

    def _row_to_record(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        available = set(row.keys())

        def _row_value(key: str) -> Any:
            if key not in available:
                return None
            return row[key]

        return {
            "device_id": _row_value("device_id"),
            "device_name": _row_value("device_name"),
            "device_name_source": _row_value("device_name_source"),
            "model_name": _row_value("model_name"),
            "brand": _row_value("brand"),
            "manufacturer": _row_value("manufacturer"),
            "platform": _row_value("platform"),
            "os_name": _row_value("os_name"),
            "os_version": _row_value("os_version"),
            "device_type": _row_value("device_type"),
            "is_physical_device": bool(_row_value("is_physical_device")) if _row_value("is_physical_device") is not None else None,
            "app_version": _row_value("app_version"),
            "app_build": _row_value("app_build"),
            "first_seen_at": _row_value("first_seen_at"),
            "last_seen_at": _row_value("last_seen_at"),
            "last_connected_at": _row_value("last_connected_at"),
            "last_disconnected_at": _row_value("last_disconnected_at"),
            "last_session_id": _row_value("last_session_id"),
            "last_channel": _row_value("last_channel"),
            "last_authorized_at": _row_value("last_authorized_at"),
            "revoked_at": _row_value("revoked_at"),
            "revoke_reason": _row_value("revoke_reason"),
            "revoked": bool(_row_value("revoked_at")),
            "push_token": _row_value("push_token"),
            "push_token_updated_at": _row_value("push_token_updated_at"),
            "fcm_token": _row_value("fcm_token"),
            "fcm_token_updated_at": _row_value("fcm_token_updated_at"),
            "notifications_enabled": (
                bool(_row_value("notifications_enabled"))
                if _row_value("notifications_enabled") is not None
                else True
            ),
            "notification_preferences_json": _row_value("notification_preferences_json"),
            "presence_state": _row_value("presence_state"),
            "visible_screen": _row_value("visible_screen"),
            "last_presence_at": _row_value("last_presence_at"),
        }

    def _dedupe_push_targets(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen_fcm_tokens: set[str] = set()
        seen_push_tokens: set[str] = set()
        for device in devices:
            fcm_token = str(device.get("fcm_token") or "").strip()
            push_token = str(device.get("push_token") or "").strip()
            if (fcm_token and fcm_token in seen_fcm_tokens) or (push_token and push_token in seen_push_tokens):
                continue
            if fcm_token:
                seen_fcm_tokens.add(fcm_token)
            if push_token:
                seen_push_tokens.add(push_token)
            deduped.append(device)
        return deduped

    def _ensure_metadata_columns(self, connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"]): str(row["type"] or "")
            for row in connection.execute("PRAGMA table_info(mobile_devices)").fetchall()
        }
        for column_name, column_type in self._METADATA_COLUMNS.items():
            if column_name in existing:
                continue
            connection.execute(f"ALTER TABLE mobile_devices ADD COLUMN {column_name} {column_type}")

    def _ensure_push_columns(self, connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"]): str(row["type"] or "")
            for row in connection.execute("PRAGMA table_info(mobile_devices)").fetchall()
        }
        for column_name, column_type in self._PUSH_COLUMNS.items():
            if column_name in existing:
                continue
            connection.execute(f"ALTER TABLE mobile_devices ADD COLUMN {column_name} {column_type}")

    def _serialize_preferences(self, preferences: dict[str, Any] | None) -> str | None:
        if not preferences:
            return None
        allowed: dict[str, Any] = {}
        for key in ("chat", "tasks", "approvals", "agent_email"):
            if key in preferences:
                allowed[key] = bool(preferences[key])
        return json.dumps(allowed, sort_keys=True) if allowed else None

    def _normalize_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        payload = metadata or {}

        def _clean(value: Any, *, max_length: int = 200) -> str | None:
            normalized = str(value or "").strip()
            if not normalized:
                return None
            return normalized[:max_length]

        is_physical_device_raw = payload.get("is_physical_device")
        is_physical_device: int | None
        if isinstance(is_physical_device_raw, bool):
            is_physical_device = 1 if is_physical_device_raw else 0
        elif is_physical_device_raw is None or str(is_physical_device_raw).strip() == "":
            is_physical_device = None
        else:
            normalized_bool = str(is_physical_device_raw).strip().lower()
            if normalized_bool in {"1", "true", "yes"}:
                is_physical_device = 1
            elif normalized_bool in {"0", "false", "no"}:
                is_physical_device = 0
            else:
                is_physical_device = None

        return {
            "device_name": _clean(payload.get("device_name")),
            "device_name_source": _clean(payload.get("device_name_source"), max_length=64),
            "model_name": _clean(payload.get("model_name")),
            "brand": _clean(payload.get("brand")),
            "manufacturer": _clean(payload.get("manufacturer")),
            "platform": _clean(payload.get("platform"), max_length=64),
            "os_name": _clean(payload.get("os_name"), max_length=64),
            "os_version": _clean(payload.get("os_version"), max_length=64),
            "device_type": _clean(payload.get("device_type"), max_length=64),
            "is_physical_device": is_physical_device,
            "app_version": _clean(payload.get("app_version"), max_length=64),
            "app_build": _clean(payload.get("app_build"), max_length=64),
        }

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
