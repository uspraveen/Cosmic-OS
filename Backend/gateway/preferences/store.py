from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..session_store import utcnow_iso

_VISUAL_RESPONSE_ENHANCEMENT_KEY = "visual_response_enhancement"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


class GatewayPreferenceStore:
    """SQLite-backed VM-global app preference store."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS app_preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_source TEXT,
                    updated_device_id TEXT
                );
                """
            )
            self._seed_defaults(connection)
            connection.commit()

    def get_visual_response_enhancement(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            row = connection.execute(
                """
                SELECT key, value_json, revision, updated_at, updated_source, updated_device_id
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_VISUAL_RESPONSE_ENHANCEMENT_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "visual_response_enhancement preference is missing after initialization"
                )
            return self._row_to_visual_response_enhancement(row)

    def set_visual_response_enhancement(
        self,
        enabled: bool,
        *,
        source: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_source = str(source or "").strip() or None
        normalized_device_id = str(device_id or "").strip() or None
        now = utcnow_iso()
        value_json = _json_dumps({"enabled": bool(enabled)})
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            existing = connection.execute(
                """
                SELECT revision
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_VISUAL_RESPONSE_ENHANCEMENT_KEY,),
            ).fetchone()
            previous_revision = int(existing["revision"]) if existing else 0
            next_revision = previous_revision + 1
            connection.execute(
                """
                INSERT INTO app_preferences (
                    key,
                    value_json,
                    revision,
                    updated_at,
                    updated_source,
                    updated_device_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at,
                    updated_source = excluded.updated_source,
                    updated_device_id = excluded.updated_device_id
                """,
                (
                    _VISUAL_RESPONSE_ENHANCEMENT_KEY,
                    value_json,
                    next_revision,
                    now,
                    normalized_source,
                    normalized_device_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT key, value_json, revision, updated_at, updated_source, updated_device_id
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_VISUAL_RESPONSE_ENHANCEMENT_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "visual_response_enhancement preference could not be reloaded after update"
                )
            return self._row_to_visual_response_enhancement(row)

    def _seed_defaults(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT 1 FROM app_preferences WHERE key = ? LIMIT 1",
            (_VISUAL_RESPONSE_ENHANCEMENT_KEY,),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO app_preferences (
                key,
                value_json,
                revision,
                updated_at,
                updated_source,
                updated_device_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _VISUAL_RESPONSE_ENHANCEMENT_KEY,
                _json_dumps({"enabled": True}),
                1,
                utcnow_iso(),
                "system_default",
                None,
            ),
        )

    def _row_to_visual_response_enhancement(
        self, row: sqlite3.Row
    ) -> dict[str, Any]:
        try:
            value = json.loads(row["value_json"]) if row["value_json"] else {}
        except json.JSONDecodeError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        return {
            "enabled": bool(value.get("enabled", True)),
            "revision": int(row["revision"]) if row["revision"] is not None else 1,
            "updated_at": str(row["updated_at"] or "").strip() or utcnow_iso(),
            "updated_source": (
                str(row["updated_source"]).strip() if row["updated_source"] else None
            ),
            "updated_device_id": (
                str(row["updated_device_id"]).strip()
                if row["updated_device_id"]
                else None
            ),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

