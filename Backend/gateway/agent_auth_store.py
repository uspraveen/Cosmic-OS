from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.sqlite_client import connect_sync

from .credentials.encryption import decrypt_token, encrypt_token_str


PROVIDER_CODEX = "codex"
PROVIDER_CURSOR = "cursor"
PROVIDER_OPENCODE = "opencode"
_CODEX_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
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
    # Cursor Grok 4.6 — High effort, not Fast (effort/fast are part of the CLI model id).
    # 4.5 spellings map to 4.6 so preferences saved before Cursor's upgrade keep working.
    "grok": "cursor-grok-4.6-high",
    "grok 4.5": "cursor-grok-4.6-high",
    "grok 4.6": "cursor-grok-4.6-high",
    "grok4.5": "cursor-grok-4.6-high",
    "grok4.6": "cursor-grok-4.6-high",
    "grok-4.5": "cursor-grok-4.6-high",
    "grok-4.6": "cursor-grok-4.6-high",
    "grok-4.5-high": "cursor-grok-4.6-high",
    "grok-4.6-high": "cursor-grok-4.6-high",
    "cursor grok": "cursor-grok-4.6-high",
    "cursor-grok": "cursor-grok-4.6-high",
    "cursor grok 4.5": "cursor-grok-4.6-high",
    "cursor grok 4.6": "cursor-grok-4.6-high",
    "cursor-grok-4.5": "cursor-grok-4.6-high",
    "cursor-grok-4.6": "cursor-grok-4.6-high",
    "cursor grok 4.5 high": "cursor-grok-4.6-high",
    "cursor grok 4.6 high": "cursor-grok-4.6-high",
    "cursor-grok-4.5-high": "cursor-grok-4.6-high",
    "cursor-grok-4.6-high": "cursor-grok-4.6-high",
    "cursor-grok-4.5-high-fast": "cursor-grok-4.6-high-fast",
    "cursor-grok-4.5-medium": "cursor-grok-4.6-medium",
}
# OpenCode Zen model ids rotate weekly (models.dev + opencode.ai/zen/v1/models
# feed), so this list is a curated seed for the default picker plus friendly
# aliases — NOT an allowlist. Anything non-empty and sane is accepted so a
# fresh Zen drop works before COSMIC ships code changes.
_OPENCODE_MODEL_ALIASES = {
    "mimo": "mimo-v2.5-free",
    "mimo v2.5 pro": "mimo-v2.5-free",
    "mimo-v2.5-pro": "mimo-v2.5-free",
    "mimo v2.5": "mimo-v2.5-free",
    "mimov2.5": "mimo-v2.5-free",
    "bigpickle": "big-pickle",
}


class OpenCodeProviderId:
    """Validation/canonicalization for OpenCode provider ids (models.dev ids:
    lowercase letters, digits, dash, dot, underscore)."""

    PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")

    @classmethod
    def normalize(cls, value: str) -> str:
        pid = str(value or "").strip().lower()
        if not cls.PATTERN.match(pid):
            raise ValueError(f"Unsupported OpenCode provider id: {value!r}")
        return pid


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
            default_preferred_model=DEFAULT_CODEX_MODEL,
        )

    def get_cursor(self, *, include_secret: bool = False) -> dict[str, Any]:
        return self._get_provider(
            PROVIDER_CURSOR,
            include_secret=include_secret,
            default_auth_mode="oauth",
            default_preferred_model="cursor-grok-4.6-high",
        )

    def _opencode_keys(self, *, include_secret: bool) -> dict[str, Any]:
        """Per-provider key map for the OpenCode harness.

        Keys are stored as one encrypted JSON blob ({provider_id: api_key}).
        Secrets are returned only with include_secret=True (internal-token
        transport); every other caller sees just the connected ids.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT provider_keys_json FROM agent_provider_auth WHERE provider = ?",
            [PROVIDER_OPENCODE],
        ).fetchone()
        blob = row["provider_keys_json"] if row is not None else ""
        keys: dict[str, str] = {}
        if blob:
            try:
                parsed = json.loads(decrypt_token(blob))
                if isinstance(parsed, dict):
                    keys = {
                        str(pid).strip().lower(): str(key)
                        for pid, key in parsed.items()
                        if str(pid or "").strip() and str(key or "").strip()
                    }
            except Exception:
                keys = {}
        return keys if include_secret else {pid: "" for pid in keys}

    def connect_opencode_provider(
        self,
        *,
        provider_id: str,
        api_key: str,
    ) -> dict[str, Any]:
        pid = OpenCodeProviderId.normalize(provider_id)
        key = str(api_key or "").strip()
        current = self._opencode_keys(include_secret=True)
        if not key:
            current.pop(pid, None)
        else:
            current[pid] = key
        self._write_opencode_keys(current)
        return self.get_opencode(include_secret=False)

    def disconnect_opencode_provider(self, *, provider_id: str) -> dict[str, Any]:
        pid = OpenCodeProviderId.normalize(provider_id)
        current = self._opencode_keys(include_secret=True)
        current.pop(pid, None)
        self._write_opencode_keys(current)
        return self.get_opencode(include_secret=False)

    def _write_opencode_keys(self, keys: dict[str, str]) -> None:
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO agent_provider_auth
               (provider, auth_mode, preferred_model, reasoning_effort, approval_mode, vm_sync_enabled,
                encrypted_api_key, has_api_key, status, login_required_reason,
                last_cli_status_json, provider_keys_json, updated_at)
               VALUES (?, 'api_key', 'mimo-v2.5-free', 'auto', 'suggest', 1, '', ?, 'stored', '', '{}', ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 provider_keys_json = excluded.provider_keys_json,
                 has_api_key = excluded.has_api_key,
                 updated_at = excluded.updated_at""",
            [
                PROVIDER_OPENCODE,
                1 if keys else 0,
                encrypt_token_str(json.dumps(keys)),
                now,
            ],
        )
        conn.commit()

    def get_opencode(self, *, include_secret: bool = False) -> dict[str, Any]:
        payload = self._get_provider(
            PROVIDER_OPENCODE,
            include_secret=include_secret,
            default_auth_mode="api_key",
            default_preferred_model="mimo-v2.5-free",
        )
        keys = self._opencode_keys(include_secret=True)
        connected = sorted(keys)
        # With per-provider keys, "has credentials at all" is what the rest of
        # COSMIC keys off of; free Zen models work even with zero entries.
        payload["has_api_key"] = bool(connected)
        payload["connected_providers"] = connected
        if include_secret:
            payload["provider_keys"] = dict(keys)
        else:
            payload.pop("api_key", None)
        return payload

    def save_opencode(
        self,
        *,
        auth_mode: str | None = None,
        preferred_model: str | None = None,
        variant: str | None = None,
        vm_sync_enabled: bool | None = None,
        api_key: str | None = None,
        status: str | None = None,
        login_required_reason: str | None = None,
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_opencode(include_secret=False)
        next_model = _normalize_opencode_model(
            preferred_model,
            fallback=str(current.get("preferred_model") or "mimo-v2.5-free"),
        )
        next_variant = _normalize_choice(
            variant,
            allowed={"auto", "minimal", "low", "medium", "high", "xhigh"},
            fallback=str(current.get("reasoning_effort") or "auto"),
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
               VALUES (?, 'api_key', ?, ?, 'suggest', ?, '', 0, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 preferred_model = excluded.preferred_model,
                 reasoning_effort = excluded.reasoning_effort,
                 vm_sync_enabled = excluded.vm_sync_enabled,
                 login_required_reason = excluded.login_required_reason,
                 last_cli_status_json = excluded.last_cli_status_json,
                 updated_at = excluded.updated_at""",
            [
                PROVIDER_OPENCODE,
                next_model,
                next_variant,
                1 if bool(vm_sync_enabled if vm_sync_enabled is not None else current.get("vm_sync_enabled", True)) else 0,
                (status or str(current.get("status") or "stored")).strip()
                or "stored",
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
        # Single-key convenience paths still land in the map under Zen's id.
        if api_key is not None:
            self.connect_opencode_provider(
                provider_id="opencode",
                api_key=str(api_key).strip(),
            )
        return self.get_opencode(include_secret=False)

    def clear_opencode_api_key(
        self,
        *,
        status: str = "logged_out",
        login_required_reason: str = "user_logged_out",
        last_cli_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.disconnect_opencode_provider(provider_id="opencode")

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
        if "provider_keys_json" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_provider_auth "
                "ADD COLUMN provider_keys_json TEXT NOT NULL DEFAULT ''"
            )
            # One-time migration: the original OpenCode design stored a single
            # optional Zen key in encrypted_api_key. Fold it into the
            # multi-provider map so per-provider connect/disconnect has one
            # source of truth from here on.
            row = self._conn.execute(
                "SELECT provider, encrypted_api_key FROM agent_provider_auth "
                "WHERE provider = ? AND encrypted_api_key != ''",
                [PROVIDER_OPENCODE],
            ).fetchone()
            if row is not None:
                legacy = ""
                try:
                    legacy = decrypt_token(row["encrypted_api_key"])
                except Exception:
                    legacy = ""
                keys: dict[str, str] = {}
                if legacy.strip():
                    keys["opencode"] = legacy.strip()
                self._conn.execute(
                    "UPDATE agent_provider_auth SET provider_keys_json = ?, encrypted_api_key = '', has_api_key = ? WHERE provider = ?",
                    [encrypt_token_str(json.dumps(keys)), 1 if keys else 0, PROVIDER_OPENCODE],
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
        if provider == PROVIDER_CODEX:
            payload["preferred_model"] = _normalize_codex_model(
                str(payload.get("preferred_model") or ""),
                fallback=default_preferred_model,
            )
        if provider == PROVIDER_OPENCODE:
            payload["preferred_model"] = _normalize_opencode_model(
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
    normalized = str(value or "").strip().lower()
    if normalized in _CODEX_MODELS:
        return normalized
    fallback = str(fallback or "").strip().lower()
    if fallback in _CODEX_MODELS:
        return fallback
    return DEFAULT_CODEX_MODEL


def _normalize_cursor_model(value: str | None, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        normalized = fallback.strip() if fallback else "auto"
    if not normalized:
        return "auto"
    alias_key = " ".join(normalized.lower().split())
    return _CURSOR_MODEL_ALIASES.get(alias_key, normalized)[:80]


def _normalize_opencode_model(value: str | None, *, fallback: str) -> str:
    """Permissive: unknown-but-sane ids pass through so brand-new Zen models
    can be selected before COSMIC learns about them. Only emptiness falls
    back; the alias table just canonicalizes friendly names (e.g. users
    typing 'mimo v2.5 pro' get today's MiMo id)."""
    normalized = str(value or "").strip()
    if not normalized:
        normalized = (fallback or "").strip() or "mimo-v2.5-free"
    lowered = normalized.lower()
    if lowered == "auto":
        return "auto"
    bare = lowered.split("/")[-1]
    aliased = _OPENCODE_MODEL_ALIASES.get(bare) or _OPENCODE_MODEL_ALIASES.get(
        " ".join(lowered.split())
    )
    if aliased:
        return aliased
    sanitized = "".join(ch for ch in normalized if ch.isalnum() or ch in "/.-_")[:120]
    return sanitized or (fallback or "mimo-v2.5-free")
