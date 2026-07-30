from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..session_store import utcnow_iso

_VISUAL_RESPONSE_ENHANCEMENT_KEY = "visual_response_enhancement"
_ALPHA_EXECUTION_PROVIDER_KEY = "alpha_execution_provider"
_COSMIC_ORCHESTRATOR_MODEL_KEY = "cosmic_orchestrator_model"
_COSMIC_HEARTBEAT_KEY = "cosmic_heartbeat"
_FIREWORKS_KIMI_MODEL = "accounts/fireworks/models/kimi-k2p6"
_FIREWORKS_GLM_MODEL = "accounts/fireworks/models/glm-5p2"


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

    def get_cosmic_heartbeat(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            row = connection.execute(
                """
                SELECT key, value_json, revision, updated_at, updated_source, updated_device_id
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_COSMIC_HEARTBEAT_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "cosmic_heartbeat preference is missing after initialization"
                )
            return self._row_to_cosmic_heartbeat(row)

    def set_cosmic_heartbeat(
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
                (_COSMIC_HEARTBEAT_KEY,),
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
                    _COSMIC_HEARTBEAT_KEY,
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
                (_COSMIC_HEARTBEAT_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "cosmic_heartbeat preference could not be reloaded after update"
                )
            return self._row_to_cosmic_heartbeat(row)

    def get_alpha_execution_provider(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            row = connection.execute(
                """
                SELECT key, value_json, revision, updated_at, updated_source, updated_device_id
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_ALPHA_EXECUTION_PROVIDER_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "alpha_execution_provider preference is missing after initialization"
                )
            return self._row_to_alpha_execution_provider(row)

    def set_alpha_execution_provider(
        self,
        preferred_harness: str,
        *,
        source: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_alpha_execution_provider(preferred_harness)
        normalized_source = str(source or "").strip() or None
        normalized_device_id = str(device_id or "").strip() or None
        now = utcnow_iso()
        value_json = _json_dumps({"preferred_harness": normalized})
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            existing = connection.execute(
                """
                SELECT revision
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_ALPHA_EXECUTION_PROVIDER_KEY,),
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
                    _ALPHA_EXECUTION_PROVIDER_KEY,
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
                (_ALPHA_EXECUTION_PROVIDER_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "alpha_execution_provider preference could not be reloaded after update"
                )
            return self._row_to_alpha_execution_provider(row)

    def get_cosmic_orchestrator_model(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            row = connection.execute(
                """
                SELECT key, value_json, revision, updated_at, updated_source, updated_device_id
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_COSMIC_ORCHESTRATOR_MODEL_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "cosmic_orchestrator_model preference is missing after initialization"
                )
            return self._row_to_cosmic_orchestrator_model(row)

    def set_cosmic_orchestrator_model(
        self,
        provider: str,
        *,
        model: str | None = None,
        source: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_provider = self._normalize_cosmic_orchestrator_provider(provider)
        normalized_model = self._normalize_cosmic_orchestrator_model(
            normalized_provider,
            model,
        )
        normalized_source = str(source or "").strip() or None
        normalized_device_id = str(device_id or "").strip() or None
        now = utcnow_iso()
        value_json = _json_dumps(
            {
                "provider": normalized_provider,
                "model": normalized_model,
            }
        )
        with self._lock, self._connect() as connection:
            self._seed_defaults(connection)
            existing = connection.execute(
                """
                SELECT revision
                FROM app_preferences
                WHERE key = ?
                LIMIT 1
                """,
                (_COSMIC_ORCHESTRATOR_MODEL_KEY,),
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
                    _COSMIC_ORCHESTRATOR_MODEL_KEY,
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
                (_COSMIC_ORCHESTRATOR_MODEL_KEY,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "cosmic_orchestrator_model preference could not be reloaded after update"
                )
            return self._row_to_cosmic_orchestrator_model(row)

    def _seed_defaults(self, connection: sqlite3.Connection) -> None:
        defaults = {
            _VISUAL_RESPONSE_ENHANCEMENT_KEY: {"enabled": True},
            _COSMIC_HEARTBEAT_KEY: {"enabled": True},
            _ALPHA_EXECUTION_PROVIDER_KEY: {"preferred_harness": "codex"},
            _COSMIC_ORCHESTRATOR_MODEL_KEY: {
                "provider": "fireworks_glm",
                "model": _FIREWORKS_GLM_MODEL,
            },
        }
        for key, value in defaults.items():
            existing = connection.execute(
                "SELECT 1 FROM app_preferences WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if existing is not None:
                continue
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
                    key,
                    _json_dumps(value),
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

    def _row_to_cosmic_heartbeat(self, row: sqlite3.Row) -> dict[str, Any]:
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

    def _row_to_alpha_execution_provider(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(row["value_json"]) if row["value_json"] else {}
        except json.JSONDecodeError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        return {
            "preferred_harness": self._normalize_alpha_execution_provider(
                str(value.get("preferred_harness") or "codex")
            ),
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

    def _row_to_cosmic_orchestrator_model(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(row["value_json"]) if row["value_json"] else {}
        except json.JSONDecodeError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        provider = self._normalize_cosmic_orchestrator_provider(
            str(value.get("provider") or "anthropic")
        )
        return {
            "provider": provider,
            "model": self._normalize_cosmic_orchestrator_model(
                provider,
                str(value.get("model") or ""),
            ),
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

    def _normalize_alpha_execution_provider(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"codex", "cursor"} else "codex"

    def _normalize_cosmic_orchestrator_provider(self, value: str) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"fireworks", "fireworks_kimi", "kimi", "kimi_k2_6", "smarter"}:
            return "fireworks_kimi"
        if normalized in {"fireworks_glm", "glm", "glm_5p2", "glm_5_2", "glm52"}:
            return "fireworks_glm"
        return "anthropic"

    def _normalize_cosmic_orchestrator_model(
        self,
        provider: str,
        model: str | None,
    ) -> str:
        normalized_provider = self._normalize_cosmic_orchestrator_provider(provider)
        normalized_model = str(model or "").strip()
        if normalized_provider == "fireworks_kimi":
            return normalized_model or _FIREWORKS_KIMI_MODEL
        if normalized_provider == "fireworks_glm":
            return normalized_model or _FIREWORKS_GLM_MODEL
        return normalized_model

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
