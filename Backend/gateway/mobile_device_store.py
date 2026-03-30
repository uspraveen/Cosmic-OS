import sqlite3
import threading
from pathlib import Path
from typing import Any

from .session_store import utcnow_iso


class MobileDeviceStore:
    """SQLite-backed registry for mobile devices connected to the Gateway."""

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
                    revoked_at TEXT,
                    revoke_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_mobile_devices_last_seen
                    ON mobile_devices(last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_mobile_devices_revoked
                    ON mobile_devices(revoked_at, last_seen_at DESC);
                """
            )
            connection.commit()

    def authorize_device(self, device_id: str, *, channel: str | None = None) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")

        now = utcnow_iso()
        normalized_channel = str(channel or "").strip() or f"mobile:{normalized_device_id}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mobile_devices (
                    device_id,
                    first_seen_at,
                    last_seen_at,
                    last_connected_at,
                    last_channel,
                    last_authorized_at,
                    revoked_at,
                    revoke_reason
                )
                VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_channel = excluded.last_channel,
                    last_authorized_at = excluded.last_authorized_at,
                    revoked_at = NULL,
                    revoke_reason = NULL
                """,
                (
                    normalized_device_id,
                    now,
                    now,
                    normalized_channel,
                    now,
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
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?, last_disconnected_at = ?
                WHERE device_id = ?
                """,
                (
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
            row = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

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
        return {
            "device_id": row["device_id"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "last_connected_at": row["last_connected_at"],
            "last_disconnected_at": row["last_disconnected_at"],
            "last_session_id": row["last_session_id"],
            "last_channel": row["last_channel"],
            "last_authorized_at": row["last_authorized_at"],
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
            "revoked": bool(row["revoked_at"]),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection
