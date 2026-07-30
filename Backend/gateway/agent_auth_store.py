from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.sqlite_client import connect_sync

from .credentials.encryption import decrypt_token, encrypt_token_str


PROVIDER_CODEX = "codex"
PROVIDER_CURSOR = "cursor"
_CODEX_MODEL_ALIASES = {
    "gpt-5.1-codex": "gpt-5.4",
    "gpt-5.1-codex-mini": "gpt-5.4",
    "gpt-5.3-codex-mini": "gpt-5.4",
}
_CODEX_MODELS = {"auto", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"}
_CURSOR_MODEL_ALIASES = {
    "composer": "composer-2.5",
    "composer normal": "composer-2.5",
    "normal composer": "composer-2.5",
    "composer 2": "composer-2.5",
    "composer2": "composer-2.5",
    "composer-2": "composer-2.5",
    "composer-2-normal": "composer-2.5",
    "composer 2 normal": "composer-2.5",
    "normal composer 2": "composer-2.5",
    "composer 2.5": "composer-2.5",
    "composer2.5": "composer-2.5",
    "composer-2.5": "composer-2.5",
    "composer-2.5-normal": "composer-2.5",
    "composer 2.5 normal": "composer-2.5",
    "normal composer 2.5": "composer-2.5",
    # Cursor Grok 4.5 — High effort, not Fast (effort/fast are part of the CLI model id).
    "grok": "cursor-grok-4.5-high",
    "grok 4.5": "cursor-grok-4.5-high",
    "grok4.5": "cursor-grok-4.5-high",
    "grok-4.5": "cursor-grok-4.5-high",
    "grok-4.5-high": "cursor-grok-4.5-high",
    "cursor grok": "cursor-grok-4.5-high",
    "cursor-grok": "cursor-grok-4.5-high",
    "cursor grok 4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5-high": "cursor-grok-4.5-high",
    "cursor grok 4.5 high": "cursor-grok-4.5-high",
}


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_provider_auth (
    provider TEXT PRIMARY KEY,
    auth_mode TEXT NOT NULL DEFAULT 'chatgpt',
    preferred_model TEXT NOT NULL DEFAULT 'auto',
    reasoning_effort TEXT NOT NULL DEFAULT 'auto',
    approval_mode TEXT NOT NULL DEFAULT 'suggest',
    vm_sync_enabled INTEGER NOT NULL DEFAULT 1,
    encrypted_api_key TEXT NOT NULL DEFAULT '',
    has_api_key INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'not_configured',
    login_required_reason TEXT NOT NULL DEFAULT '',
    last_cli_status_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""


class AgentAuthStore:
    """Gateway-owned auth settings for agent-side CLI providers."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self._get_conn()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_codex(self, *, include_secret: bool = False) -> dict[str, Any]:
        return self._get_provider(
            PROVIDER_CODEX,
            include_secret=include_secret,
            default_auth_mode="chatgpt",
            default_preferred_model="auto",
        )

    def get_cursor(self, *, include_secret: bool = False) -> dict[str, Any]:
        return self._get_provider(
            PROVIDER_CURSOR,
            include_secret=include_secret,
            default_auth_mode="oauth",
            default_preferred_model="cursor-grok-4.5-high",
        )

    def save_codex(
        self,
        *,
        auth_mode: str | None = None,
        preferred_model: str | None = None,
        reasoning_effort: str | None = None,
        approval_mode: str | None = None,
        vm_sync_enabled: bool | None = None,
        api_key: str | None = None,
        status: str | None = None,
        login_required_reason: str | None = None,
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_codex(include_secret=True)
        next_auth_mode = _normalize_choice(
            auth_mode,
            allowed={"chatgpt", "api_key"},
            fallback=str(current.get("auth_mode") or "chatgpt"),
        )
        next_model = _normalize_codex_model(
            preferred_model,
            fallback=str(current.get("preferred_model") or "auto"),
        )
        next_reasoning = _normalize_choice(
            reasoning_effort,
            allowed={"auto", "low", "medium", "high", "xhigh"},
            fallback=str(current.get("reasoning_effort") or "auto"),
        )
        next_approval = _normalize_choice(
            approval_mode,
            allowed={"suggest", "auto_edit", "full_auto"},
            fallback=str(current.get("approval_mode") or "suggest"),
        )
        next_sync_enabled = (
            bool(vm_sync_enabled)
            if vm_sync_enabled is not None
            else bool(current.get("vm_sync_enabled", True))
        )
        next_api_key = (
            str(api_key).strip()
            if api_key is not None
            else str(current.get("api_key") or "")
        )
        cli_status = (
            last_cli_status
            if isinstance(last_cli_status, dict)
            else current.get("last_cli_status")
            if isinstance(current.get("last_cli_status"), dict)
            else {}
        )

        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO agent_provider_auth
               (provider, auth_mode, preferred_model, reasoning_effort, approval_mode, vm_sync_enabled,
                encrypted_api_key, has_api_key, status, login_required_reason,
                last_cli_status_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 auth_mode = excluded.auth_mode,
                 preferred_model = excluded.preferred_model,
                 reasoning_effort = excluded.reasoning_effort,
                 approval_mode = excluded.approval_mode,
                 vm_sync_enabled = excluded.vm_sync_enabled,
                 encrypted_api_key = excluded.encrypted_api_key,
                 has_api_key = excluded.has_api_key,
                 status = excluded.status,
                 login_required_reason = excluded.login_required_reason,
                 last_cli_status_json = excluded.last_cli_status_json,
                 updated_at = excluded.updated_at""",
            [
                PROVIDER_CODEX,
                next_auth_mode,
                next_model,
                next_reasoning,
                next_approval,
                1 if next_sync_enabled else 0,
                encrypt_token_str(next_api_key) if next_api_key else "",
                1 if bool(next_api_key) else 0,
                (status or str(current.get("status") or "not_configured")).strip()
                or "not_configured",
                (
                    login_required_reason
                    if login_required_reason is not None
                    else str(current.get("login_required_reason") or "")
                ),
                json.dumps(cli_status),
                now,
            ],
        )
        conn.commit()
        return self.get_codex(include_secret=False)

    def save_cursor(
        self,
        *,
        auth_mode: str | None = None,
        preferred_model: str | None = None,
        approval_mode: str | None = None,
        vm_sync_enabled: bool | None = None,
        status: str | None = None,
        login_required_reason: str | None = None,
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_cursor(include_secret=False)
        next_auth_mode = _normalize_choice(
            auth_mode,
            allowed={"oauth"},
            fallback=str(current.get("auth_mode") or "oauth"),
        )
        next_model = _normalize_cursor_model(
            preferred_model,
            fallback=str(current.get("preferred_model") or "auto"),
        )
        next_approval = _normalize_choice(
            approval_mode,
            allowed={"suggest", "auto_edit", "full_auto"},
            fallback=str(current.get("approval_mode") or "suggest"),
        )
        next_sync_enabled = (
            bool(vm_sync_enabled)
            if vm_sync_enabled is not None
            else bool(current.get("vm_sync_enabled", True))
        )
        cli_status = (
            last_cli_status
            if isinstance(last_cli_status, dict)
            else current.get("last_cli_status")
            if isinstance(current.get("last_cli_status"), dict)
            else {}
        )

        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO agent_provider_auth
               (provider, auth_mode, preferred_model, approval_mode, vm_sync_enabled,
                encrypted_api_key, has_api_key, status, login_required_reason,
                last_cli_status_json, updated_at)
               VALUES (?, ?, ?, ?, ?, '', 0, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 auth_mode = excluded.auth_mode,
                 preferred_model = excluded.preferred_model,
                 approval_mode = excluded.approval_mode,
                 vm_sync_enabled = excluded.vm_sync_enabled,
                 encrypted_api_key = '',
                 has_api_key = 0,
                 status = excluded.status,
                 login_required_reason = excluded.login_required_reason,
                 last_cli_status_json = excluded.last_cli_status_json,
                 updated_at = excluded.updated_at""",
            [
                PROVIDER_CURSOR,
                next_auth_mode,
                next_model,
                next_approval,
                1 if next_sync_enabled else 0,
                (status or str(current.get("status") or "not_configured")).strip()
                or "not_configured",
                (
                    login_required_reason
                    if login_required_reason is not None
                    else str(current.get("login_required_reason") or "")
                ),
                json.dumps(cli_status),
                now,
            ],
        )
        conn.commit()
        return self.get_cursor(include_secret=False)

    def clear_codex_api_key(
        self,
        *,
        status: str = "logged_out",
        login_required_reason: str = "user_logged_out",
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_codex(include_secret=True)
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO agent_provider_auth
               (provider, auth_mode, preferred_model, reasoning_effort, approval_mode, vm_sync_enabled,
                encrypted_api_key, has_api_key, status, login_required_reason,
                last_cli_status_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, '', 0, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 encrypted_api_key = '',
                 has_api_key = 0,
                 status = excluded.status,
                 login_required_reason = excluded.login_required_reason,
                 last_cli_status_json = excluded.last_cli_status_json,
                 updated_at = excluded.updated_at""",
            [
                PROVIDER_CODEX,
                str(current.get("auth_mode") or "chatgpt"),
                str(current.get("preferred_model") or "auto"),
                str(current.get("reasoning_effort") or "auto"),
                str(current.get("approval_mode") or "suggest"),
                1 if bool(current.get("vm_sync_enabled", True)) else 0,
                status,
                login_required_reason,
                json.dumps(last_cli_status or {}),
                now,
            ],
        )
        conn.commit()
        return self.get_codex(include_secret=False)

    def clear_cursor_auth(
        self,
        *,
        status: str = "logged_out",
        login_required_reason: str = "user_logged_out",
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_cursor(include_secret=False)
        return self.save_cursor(
            auth_mode="oauth",
            preferred_model=str(current.get("preferred_model") or "auto"),
            approval_mode=str(current.get("approval_mode") or "suggest"),
            vm_sync_enabled=bool(current.get("vm_sync_enabled", True)),
            status=status,
            login_required_reason=login_required_reason,
            last_cli_status=last_cli_status or {},
        )

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect_sync(self._db_path)
            self._conn.executescript(_SCHEMA)
            self._ensure_columns()
            self._conn.commit()
        return self._conn

    def _ensure_columns(self) -> None:
        if self._conn is None:
            return
        columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in self._conn.execute("PRAGMA table_info(agent_provider_auth)").fetchall()
        }
        if "reasoning_effort" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_provider_auth "
                "ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'auto'"
            )

    def _get_provider(
        self,
        provider: str,
        *,
        include_secret: bool,
        default_auth_mode: str,
        default_preferred_model: str,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM agent_provider_auth WHERE provider = ?",
            [provider],
        ).fetchone()
        if row is None:
            payload = {
                "provider": provider,
                "auth_mode": default_auth_mode,
                "preferred_model": default_preferred_model,
                "reasoning_effort": "auto",
                "approval_mode": "suggest",
                "vm_sync_enabled": True,
                "has_api_key": False,
                "status": "not_configured",
                "login_required_reason": "",
                "last_cli_status": {},
                "updated_at": "",
            }
            if include_secret:
                payload["api_key"] = ""
            return payload

        last_cli_status: dict[str, Any] = {}
        try:
            parsed = json.loads(row["last_cli_status_json"] or "{}")
            if isinstance(parsed, dict):
                last_cli_status = parsed
        except Exception:
            last_cli_status = {}

        payload = {
            "provider": row["provider"],
            "auth_mode": row["auth_mode"],
            "preferred_model": row["preferred_model"],
            "reasoning_effort": row["reasoning_effort"] if "reasoning_effort" in row.keys() else "auto",
            "approval_mode": row["approval_mode"],
            "vm_sync_enabled": bool(row["vm_sync_enabled"]),
            "has_api_key": bool(row["has_api_key"]),
            "status": row["status"],
            "login_required_reason": row["login_required_reason"],
            "last_cli_status": last_cli_status,
            "updated_at": row["updated_at"],
        }
        if provider == PROVIDER_CURSOR:
            payload["preferred_model"] = _normalize_cursor_model(
                str(payload.get("preferred_model") or ""),
                fallback=default_preferred_model,
            )
        if include_secret:
            api_key = ""
            if row["encrypted_api_key"]:
                try:
                    api_key = decrypt_token(row["encrypted_api_key"])
                except Exception:
                    api_key = ""
            payload["api_key"] = api_key
        return payload


def _normalize_choice(value: str | None, *, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip()
    if normalized in allowed:
        return normalized
    return fallback if fallback in allowed else sorted(allowed)[0]


def _normalize_codex_model(value: str | None, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        normalized = str(fallback or "").strip() or "auto"
    normalized = normalized.lower()
    normalized = _CODEX_MODEL_ALIASES.get(normalized, normalized)
    return normalized if normalized in _CODEX_MODELS else "auto"


def _normalize_cursor_model(value: str | None, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        normalized = fallback.strip() if fallback else "auto"
    if not normalized:
        return "auto"
    alias_key = " ".join(normalized.lower().split())
    return _CURSOR_MODEL_ALIASES.get(alias_key, normalized)[:80]
