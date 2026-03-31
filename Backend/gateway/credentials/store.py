"""SQLite store for Gateway-owned Google credentials.

Schema follows the architecture spec §22.2:
- accounts: connected provider accounts
- credentials: encrypted tokens per account
- resource_bindings: learned resource-to-account mappings
- credential_audit: append-only audit trail
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.sqlite_client import connect_sync

from .encryption import decrypt_token, encrypt_token_str


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _new_account_id() -> str:
    return f"acc_{uuid4().hex[:12]}"


def _new_credential_ref() -> str:
    return f"cred_{uuid4().hex[:12]}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL,
    provider_account_id TEXT NOT NULL DEFAULT '',
    email TEXT,
    display_name TEXT,
    account_label TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    connected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_ref TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    granted_scopes TEXT NOT NULL DEFAULT '[]',
    encrypted_refresh_token TEXT NOT NULL,
    encrypted_access_token TEXT,
    access_token_expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS resource_bindings (
    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT,
    account_id TEXT NOT NULL,
    last_accessed_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id),
    UNIQUE (resource_type, external_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_bindings_name
ON resource_bindings(resource_type, display_name);

CREATE TABLE IF NOT EXISTS credential_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT 'system',
    agent_id TEXT NOT NULL DEFAULT 'system',
    credential_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    scopes_used TEXT NOT NULL DEFAULT '[]',
    action TEXT NOT NULL,
    result TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_task ON credential_audit(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_credential ON credential_audit(credential_ref);
"""


class CredentialStore:
    """SQLite-backed credential store for Gateway-owned OAuth tokens."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect_sync(self._db_path)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Account CRUD ──────────────────────────────────────────────────

    def create_account(
        self,
        *,
        provider: str,
        provider_account_id: str = "",
        email: str = "",
        display_name: str = "",
        account_label: str = "",
        is_primary: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account_id = _new_account_id()
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO accounts
               (account_id, user_id, provider, provider_account_id, email,
                display_name, account_label, is_primary, status, metadata_json,
                connected_at, updated_at)
               VALUES (?, 'default', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            [
                account_id,
                provider,
                provider_account_id,
                email,
                display_name,
                account_label or display_name or email or f"{provider} account",
                1 if is_primary else 0,
                json.dumps(metadata or {}),
                now,
                now,
            ],
        )
        conn.commit()
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", [account_id]
        ).fetchone()
        if row is None:
            return None
        return self._row_to_account(row)

    def get_account_by_provider_account(
        self, provider: str, provider_account_id: str
    ) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM accounts WHERE provider = ? AND provider_account_id = ?",
            [provider, provider_account_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_account(row)

    def list_accounts(self, provider: str | None = None) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if provider:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE provider = ? ORDER BY is_primary DESC, connected_at",
                [provider],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY provider, is_primary DESC, connected_at"
            ).fetchall()
        return [self._row_to_account(r) for r in rows]

    def update_account(
        self,
        account_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        account_label: str | None = None,
        is_primary: bool | None = None,
        status: str | None = None,
        provider_account_id: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = self._get_conn()
        existing = self.get_account(account_id)
        if existing is None:
            raise ValueError(f"Account not found: {account_id}")

        updates: list[str] = []
        params: list[Any] = []

        if email is not None:
            updates.append("email = ?")
            params.append(email)
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if account_label is not None:
            updates.append("account_label = ?")
            params.append(account_label)
        if is_primary is not None:
            updates.append("is_primary = ?")
            params.append(1 if is_primary else 0)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if provider_account_id is not None:
            updates.append("provider_account_id = ?")
            params.append(provider_account_id)
        if metadata_patch:
            merged = dict(existing.get("_metadata") or {})
            merged.update(metadata_patch)
            updates.append("metadata_json = ?")
            params.append(json.dumps(merged))

        if not updates:
            return existing

        updates.append("updated_at = ?")
        params.append(_utcnow_iso())
        params.append(account_id)

        conn.execute(
            f"UPDATE accounts SET {', '.join(updates)} WHERE account_id = ?",
            params,
        )
        conn.commit()
        return self.get_account(account_id)

    def delete_account(self, account_id: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM credentials WHERE account_id = ?", [account_id])
        conn.execute("DELETE FROM resource_bindings WHERE account_id = ?", [account_id])
        conn.execute("DELETE FROM accounts WHERE account_id = ?", [account_id])
        conn.commit()

    def set_primary(self, account_id: str) -> None:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account not found: {account_id}")
        conn = self._get_conn()
        conn.execute(
            "UPDATE accounts SET is_primary = 0 WHERE provider = ?",
            [account["provider"]],
        )
        conn.execute(
            "UPDATE accounts SET is_primary = 1 WHERE account_id = ?",
            [account_id],
        )
        conn.commit()

    # ── Credential CRUD ───────────────────────────────────────────────

    def store_credential(
        self,
        *,
        account_id: str,
        granted_scopes: list[str],
        access_token: str,
        refresh_token: str,
        expires_at_ts: float | None = None,
    ) -> str:
        credential_ref = _new_credential_ref()
        now = _utcnow_iso()
        conn = self._get_conn()

        # Revoke existing active credentials for this account
        conn.execute(
            """UPDATE credentials
               SET revoked_at = ?, updated_at = ?
               WHERE account_id = ? AND revoked_at IS NULL""",
            [now, now, account_id],
        )

        conn.execute(
            """INSERT INTO credentials
               (credential_ref, account_id, granted_scopes,
                encrypted_refresh_token, encrypted_access_token,
                access_token_expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                credential_ref,
                account_id,
                json.dumps(granted_scopes),
                encrypt_token_str(refresh_token),
                encrypt_token_str(access_token) if access_token else None,
                str(expires_at_ts) if expires_at_ts else None,
                now,
                now,
            ],
        )
        conn.commit()
        return credential_ref

    def get_active_credential(self, account_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM credentials
               WHERE account_id = ? AND revoked_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            [account_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    def get_credential_by_ref(self, credential_ref: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM credentials WHERE credential_ref = ?",
            [credential_ref],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    def update_access_token(
        self,
        credential_ref: str,
        access_token: str,
        expires_at_ts: float | None = None,
    ) -> None:
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """UPDATE credentials
               SET encrypted_access_token = ?, access_token_expires_at = ?, updated_at = ?
               WHERE credential_ref = ?""",
            [
                encrypt_token_str(access_token),
                str(expires_at_ts) if expires_at_ts else None,
                now,
                credential_ref,
            ],
        )
        conn.commit()

    def revoke_account_credentials(self, account_id: str) -> None:
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """UPDATE credentials
               SET revoked_at = ?, updated_at = ?
               WHERE account_id = ? AND revoked_at IS NULL""",
            [now, now, account_id],
        )
        conn.commit()

    # ── Audit ─────────────────────────────────────────────────────────

    def log_audit(
        self,
        *,
        action: str,
        provider: str,
        result: str,
        credential_ref: str = "",
        task_id: str = "system",
        agent_id: str = "system",
        scopes_used: list[str] | None = None,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO credential_audit
               (timestamp, task_id, agent_id, credential_ref, provider,
                scopes_used, action, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                _utcnow_iso(),
                task_id,
                agent_id,
                credential_ref,
                provider,
                json.dumps(scopes_used or []),
                action,
                result,
            ],
        )
        conn.commit()

    # ── Resource Bindings ─────────────────────────────────────────────

    def upsert_resource_binding(
        self,
        *,
        resource_type: str,
        external_id: str,
        account_id: str,
        display_name: str = "",
    ) -> None:
        now = _utcnow_iso()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO resource_bindings
               (resource_type, external_id, display_name, account_id, last_accessed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(resource_type, external_id, account_id)
               DO UPDATE SET display_name = ?, last_accessed_at = ?""",
            [
                resource_type,
                external_id,
                display_name,
                account_id,
                now,
                display_name,
                now,
            ],
        )
        conn.commit()

    def lookup_resource_binding(
        self,
        resource_type: str,
        external_id: str | None = None,
        display_name: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if external_id:
            rows = conn.execute(
                """SELECT rb.*, a.email, a.display_name as account_display
                   FROM resource_bindings rb
                   JOIN accounts a ON a.account_id = rb.account_id
                   WHERE rb.resource_type = ? AND rb.external_id = ?
                   ORDER BY rb.last_accessed_at DESC""",
                [resource_type, external_id],
            ).fetchall()
        elif display_name:
            rows = conn.execute(
                """SELECT rb.*, a.email, a.display_name as account_display
                   FROM resource_bindings rb
                   JOIN accounts a ON a.account_id = rb.account_id
                   WHERE rb.resource_type = ? AND rb.display_name LIKE ?
                   ORDER BY rb.last_accessed_at DESC""",
                [resource_type, f"%{display_name}%"],
            ).fetchall()
        else:
            return []
        return [dict(r) for r in rows]

    # ── Helpers ───────────────────────────────────────────────────────

    def _row_to_account(self, row: sqlite3.Row) -> dict[str, Any]:
        metadata = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            pass
        return {
            "account_id": row["account_id"],
            "user_id": row["user_id"],
            "provider": row["provider"],
            "provider_account_id": row["provider_account_id"],
            "email": row["email"] or "",
            "display_name": row["display_name"] or "",
            "account_label": row["account_label"] or "",
            "is_primary": bool(row["is_primary"]),
            "status": row["status"],
            "connected_at": row["connected_at"],
            "updated_at": row["updated_at"],
            "_metadata": metadata,
        }

    def _row_to_credential(self, row: sqlite3.Row) -> dict[str, Any]:
        granted_scopes = []
        try:
            granted_scopes = json.loads(row["granted_scopes"] or "[]")
        except Exception:
            pass

        refresh_token = ""
        try:
            refresh_token = decrypt_token(row["encrypted_refresh_token"])
        except Exception:
            pass

        access_token = ""
        if row["encrypted_access_token"]:
            try:
                access_token = decrypt_token(row["encrypted_access_token"])
            except Exception:
                pass

        expires_at = None
        if row["access_token_expires_at"]:
            try:
                expires_at = float(row["access_token_expires_at"])
            except Exception:
                pass

        return {
            "credential_ref": row["credential_ref"],
            "account_id": row["account_id"],
            "granted_scopes": granted_scopes,
            "refresh_token": refresh_token,
            "access_token": access_token,
            "access_token_expires_at": expires_at,
            "revoked_at": row["revoked_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
