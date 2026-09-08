"""SQLite persistence for the Cosmic password vault.

All secret fields are stored Fernet-encrypted (see gateway.credentials.encryption)
using the shared CREDENTIAL_ENCRYPTION_KEY. Secrets are only ever decrypted for
three callers: the user's explicit reveal endpoint, the TOTP code generator, and
the internal /internal/vault/resolve endpoint used by the orchestrator at task
dispatch time. Lookups return a credential_ref, never the password.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from ..credentials.encryption import decrypt_token, encrypt_token_str

ORCHESTRATOR_AGENT_ID = "cosmic/orchestrator:1.0.0"

POLICY_ALWAYS_ASK = "always_ask"
POLICY_ALWAYS_ALLOW = "always_allow"
POLICY_WINDOW = "window"
POLICY_MODES = (POLICY_ALWAYS_ASK, POLICY_ALWAYS_ALLOW, POLICY_WINDOW)

PENDING_STATUS_PENDING = "pending"
PENDING_STATUS_APPROVED = "approved"
PENDING_STATUS_REJECTED = "rejected"
PENDING_STATUS_CONSUMED = "consumed"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def derive_site_domain(site_url: str) -> str:
    """Reduce a site URL (or bare domain) to a matchable hostname."""
    raw = str(site_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "•" * max(8, min(len(value), 24))


class VaultStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS vault_entries (
                    entry_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    site_url TEXT NOT NULL DEFAULT '',
                    site_domain TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    password_encrypted TEXT NOT NULL DEFAULT '',
                    totp_seed_encrypted TEXT NOT NULL DEFAULT '',
                    notes_encrypted TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'user',
                    created_by_task_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vault_entries_domain
                    ON vault_entries(site_domain);

                CREATE TABLE IF NOT EXISTS vault_policies (
                    entry_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'always_ask',
                    window_expires_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (entry_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS vault_pending (
                    request_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    entry_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    task_id TEXT,
                    session_id TEXT,
                    channel TEXT,
                    purpose TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_vault_pending_entry_status
                    ON vault_pending(entry_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS vault_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    entry_id TEXT,
                    actor TEXT NOT NULL DEFAULT 'user',
                    action TEXT NOT NULL,
                    task_id TEXT,
                    result TEXT NOT NULL DEFAULT 'ok',
                    detail TEXT
                );
                """
            )
            connection.commit()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    # ── entries ──────────────────────────────────────────────────────────────

    def add_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        entry_id = str(item.get("entry_id") or "").strip() or f"vault_{uuid4().hex[:16]}"
        row = {
            "entry_id": entry_id,
            "title": str(item.get("title") or "").strip() or "Untitled entry",
            "site_url": str(item.get("site_url") or "").strip(),
            "site_domain": derive_site_domain(item.get("site_url")),
            "username": str(item.get("username") or "").strip(),
            # Agent-initiated saves arrive with secrets already encrypted (the
            # pending approval row must not hold plaintext); user saves encrypt here.
            "password_encrypted": str(item.get("password_encrypted") or "").strip()
            or encrypt_token_str(str(item.get("password") or "")),
            "totp_seed_encrypted": str(item.get("totp_seed_encrypted") or "").strip()
            or encrypt_token_str(str(item.get("totp_seed") or "")),
            "notes_encrypted": str(item.get("notes_encrypted") or "").strip()
            or encrypt_token_str(str(item.get("notes") or "")),
            "tags_json": json.dumps(
                [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()],
                ensure_ascii=False,
            ),
            "source": str(item.get("source") or "user").strip() or "user",
            "created_by_task_id": str(item.get("created_by_task_id") or "").strip() or None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vault_entries (
                    entry_id, title, site_url, site_domain, username,
                    password_encrypted, totp_seed_encrypted, notes_encrypted,
                    tags_json, source, created_by_task_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["entry_id"], row["title"], row["site_url"], row["site_domain"],
                    row["username"], row["password_encrypted"], row["totp_seed_encrypted"],
                    row["notes_encrypted"], row["tags_json"], row["source"],
                    row["created_by_task_id"], row["created_at"], row["updated_at"],
                ),
            )
            connection.commit()
        self.append_audit(entry_id, str(row["source"]), "create", row.get("created_by_task_id"))
        return self.get_entry(entry_id) or {}

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM vault_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def list_entries(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM vault_entries ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def find_entries(self, query: str) -> list[dict[str, Any]]:
        """Match by entry_id, then exact domain, then subdomain, then title."""
        needle = str(query or "").strip()
        if not needle:
            return []
        lowered = needle.lower()
        domain = derive_site_domain(needle)
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            by_id = connection.execute(
                "SELECT * FROM vault_entries WHERE entry_id = ?", (needle,)
            ).fetchall()
            if by_id:
                return [self._row_to_entry(row) for row in by_id]
            if domain:
                exact = connection.execute(
                    "SELECT * FROM vault_entries WHERE site_domain = ? ORDER BY updated_at DESC",
                    (domain,),
                ).fetchall()
                if exact:
                    return [self._row_to_entry(row) for row in exact]
                # Query host is a subdomain of a stored domain (api.github.com → github.com),
                # or a stored domain is a subdomain of the query host.
                suffix = connection.execute(
                    """
                    SELECT * FROM vault_entries
                    WHERE site_domain LIKE ? OR ? LIKE '%.' || site_domain
                    ORDER BY updated_at DESC
                    """,
                    (f"%.{domain}", domain),
                ).fetchall()
                if suffix:
                    return [self._row_to_entry(row) for row in suffix]
            by_title = connection.execute(
                "SELECT * FROM vault_entries WHERE lower(title) LIKE ? ORDER BY updated_at DESC",
                (f"%{lowered}%",),
            ).fetchall()
        return [self._row_to_entry(row) for row in by_title]

    def update_entry(self, entry_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get_entry(entry_id)
        if not existing:
            return None
        assignments: list[str] = []
        params: list[Any] = []
        for field, column in (
            ("title", "title"),
            ("site_url", "site_url"),
            ("username", "username"),
        ):
            if field in patch and patch[field] is not None:
                assignments.append(f"{column} = ?")
                params.append(str(patch[field]).strip())
        if "site_url" in patch and patch["site_url"] is not None:
            assignments.append("site_domain = ?")
            params.append(derive_site_domain(patch["site_url"]))
        for field, column in (
            ("password", "password_encrypted"),
            ("totp_seed", "totp_seed_encrypted"),
            ("notes", "notes_encrypted"),
        ):
            if field in patch and patch[field] is not None:
                assignments.append(f"{column} = ?")
                params.append(encrypt_token_str(str(patch[field])))
        if "tags" in patch and patch["tags"] is not None:
            assignments.append("tags_json = ?")
            params.append(
                json.dumps(
                    [str(tag).strip() for tag in (patch["tags"] or []) if str(tag).strip()],
                    ensure_ascii=False,
                )
            )
        if not assignments:
            return existing
        assignments.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(entry_id)
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE vault_entries SET {', '.join(assignments)} WHERE entry_id = ?",
                params,
            )
            connection.commit()
        return self.get_entry(entry_id)

    def delete_entry(self, entry_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM vault_entries WHERE entry_id = ?", (entry_id,)
            )
            connection.execute("DELETE FROM vault_policies WHERE entry_id = ?", (entry_id,))
            connection.commit()
        return cursor.rowcount > 0

    # ── policies ─────────────────────────────────────────────────────────────

    def get_policy(self, entry_id: str, agent_id: str = ORCHESTRATOR_AGENT_ID) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM vault_policies WHERE entry_id = ? AND agent_id = ?",
                (entry_id, agent_id),
            ).fetchone()
        if not row:
            return {
                "entry_id": entry_id,
                "agent_id": agent_id,
                "mode": POLICY_ALWAYS_ASK,
                "window_expires_at": None,
                "updated_at": None,
            }
        return dict(row)

    def set_policy(
        self,
        entry_id: str,
        mode: str,
        window_expires_at: str | None = None,
        agent_id: str = ORCHESTRATOR_AGENT_ID,
    ) -> dict[str, Any]:
        if mode not in POLICY_MODES:
            raise ValueError(f"Invalid vault policy mode: {mode}")
        now = utcnow_iso()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vault_policies (entry_id, agent_id, mode, window_expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (entry_id, agent_id)
                DO UPDATE SET mode = excluded.mode,
                              window_expires_at = excluded.window_expires_at,
                              updated_at = excluded.updated_at
                """,
                (entry_id, agent_id, mode, window_expires_at, now),
            )
            connection.commit()
        return self.get_policy(entry_id, agent_id)

    def policy_allows_use(self, entry_id: str, agent_id: str = ORCHESTRATOR_AGENT_ID) -> bool:
        policy = self.get_policy(entry_id, agent_id)
        mode = str(policy.get("mode") or POLICY_ALWAYS_ASK)
        if mode == POLICY_ALWAYS_ALLOW:
            return True
        if mode == POLICY_WINDOW:
            expires_at = str(policy.get("window_expires_at") or "")
            if not expires_at:
                return False
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                return False
            return datetime.now(timezone.utc) < expiry
        return False

    # ── pending approval requests ────────────────────────────────────────────

    def create_pending(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()
        request_id = str(item.get("request_id") or "").strip() or f"vault_req_{uuid4().hex[:12]}"
        row = {
            "request_id": request_id,
            "action": str(item.get("action") or "").strip() or "use_entry",
            "entry_id": str(item.get("entry_id") or "").strip() or None,
            "payload_json": json.dumps(item.get("payload") or {}, ensure_ascii=False),
            "status": PENDING_STATUS_PENDING,
            "task_id": str(item.get("task_id") or "").strip() or None,
            "session_id": str(item.get("session_id") or "").strip() or None,
            "channel": str(item.get("channel") or "").strip() or None,
            "purpose": str(item.get("purpose") or "").strip() or None,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        }
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vault_pending (
                    request_id, action, entry_id, payload_json, status,
                    task_id, session_id, channel, purpose, created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["request_id"], row["action"], row["entry_id"], row["payload_json"],
                    row["status"], row["task_id"], row["session_id"], row["channel"],
                    row["purpose"], row["created_at"], row["updated_at"], row["resolved_at"],
                ),
            )
            connection.commit()
        return self.get_pending(request_id) or {}

    def get_pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM vault_pending WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._row_to_pending(row) if row else None

    def list_pending(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            if status:
                rows = connection.execute(
                    "SELECT * FROM vault_pending WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM vault_pending ORDER BY updated_at DESC LIMIT 200"
                ).fetchall()
        return [self._row_to_pending(row) for row in rows]

    def mark_pending(self, request_id: str, status: str) -> dict[str, Any] | None:
        resolved_at = utcnow_iso() if status in (PENDING_STATUS_APPROVED, PENDING_STATUS_REJECTED, PENDING_STATUS_CONSUMED) else None
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE vault_pending
                SET status = ?, updated_at = ?,
                    resolved_at = COALESCE(resolved_at, ?)
                WHERE request_id = ?
                """,
                (status, utcnow_iso(), resolved_at, request_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_pending(request_id)

    def take_approved_use(self, entry_id: str, task_id: str | None) -> dict[str, Any] | None:
        """Consume one approved use_entry request for this entry+task, if any."""
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM vault_pending
                WHERE action = 'use_entry' AND entry_id = ?
                  AND status = 'approved' AND (task_id IS NULL OR task_id = ?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (entry_id, task_id or ""),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE vault_pending SET status = 'consumed', updated_at = ? WHERE request_id = ?",
                (utcnow_iso(), row["request_id"]),
            )
            connection.commit()
        return self._row_to_pending(row)

    # ── audit ────────────────────────────────────────────────────────────────

    def append_audit(
        self,
        entry_id: str | None,
        actor: str,
        action: str,
        task_id: str | None = None,
        result: str = "ok",
        detail: str | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO vault_audit (timestamp, entry_id, actor, action, task_id, result, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utcnow_iso(), entry_id, actor, action, task_id, result, detail),
            )
            connection.commit()

    def list_audit(self, limit: int = 100, entry_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            connection.row_factory = sqlite3.Row
            if entry_id:
                rows = connection.execute(
                    "SELECT * FROM vault_audit WHERE entry_id = ? ORDER BY audit_id DESC LIMIT ?",
                    (entry_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM vault_audit ORDER BY audit_id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row_to_entry(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["tags"] = json.loads(data.get("tags_json") or "[]")
        except (TypeError, ValueError):
            data["tags"] = []
        data.pop("tags_json", None)
        data["has_password"] = bool(data.get("password_encrypted"))
        data["has_totp"] = bool(data.get("totp_seed_encrypted"))
        data["has_notes"] = bool(data.get("notes_encrypted"))
        return data

    @staticmethod
    def _row_to_pending(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["payload"] = json.loads(data.get("payload_json") or "{}")
        except (TypeError, ValueError):
            data["payload"] = {}
        data.pop("payload_json", None)
        return data


def decrypt_entry_secrets(entry: dict[str, Any]) -> dict[str, Any]:
    """Decrypt the secret columns of an entry row for an authorized caller."""
    decrypted = dict(entry)
    decrypted["password"] = decrypt_token(entry.get("password_encrypted") or "")
    decrypted["totp_seed"] = decrypt_token(entry.get("totp_seed_encrypted") or "")
    decrypted["notes"] = decrypt_token(entry.get("notes_encrypted") or "")
    return decrypted
