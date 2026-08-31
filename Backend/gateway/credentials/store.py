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


def _new_repo_row_id() -> str:
    return f"ghr_{uuid4().hex[:12]}"


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

CREATE TABLE IF NOT EXISTS github_repositories (
    repo_row_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    installation_id TEXT NOT NULL DEFAULT '',
    github_repo_id TEXT NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    clone_url TEXT NOT NULL DEFAULT '',
    ssh_url TEXT NOT NULL DEFAULT '',
    html_url TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT '',
    permissions_json TEXT NOT NULL DEFAULT '{}',
    local_path TEXT,
    branch TEXT,
    last_commit_sha TEXT,
    last_commit_message TEXT,
    last_commit_author TEXT,
    last_commit_at TEXT,
    last_ahead INTEGER NOT NULL DEFAULT 0,
    last_behind INTEGER NOT NULL DEFAULT 0,
    last_dirty INTEGER NOT NULL DEFAULT 0,
    last_task_id TEXT,
    last_session_id TEXT,
    alpha_project_id TEXT,
    last_progress_source TEXT,
    last_progress_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    sync_error TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_github_repositories_unique
ON github_repositories(account_id, github_repo_id);

CREATE INDEX IF NOT EXISTS idx_github_repositories_status
ON github_repositories(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_github_repositories_full_name
ON github_repositories(full_name);
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
        conn.execute("DELETE FROM github_repositories WHERE account_id = ?", [account_id])
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

    # ── GitHub Repositories ───────────────────────────────────────────
    #
    # The gateway owns the authoritative list of repositories a GitHub App
    # installation granted COSMIC access to. Rows carry both the authorization
    # facts (clone URL, permissions, status) and the last local progress Alpha
    # reported (checkout path, branch, last commit) so the orchestrator and
    # Alpha can resolve "the user's repo X" to a concrete VM path and state.

    def upsert_github_repositories(
        self,
        *,
        account_id: str,
        installation_id: str,
        repos: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Insert/update repository rows from a GitHub API payload.

        Each item is a GitHub repository object (``id``, ``full_name``,
        ``clone_url``, ``permissions``, ...). Existing rows keep their local
        progress columns; only authorization fields are refreshed, and a repo
        that had lost access and reappears is marked active again.
        """
        now_iso = _utcnow_iso()
        conn = self._get_conn()
        added = 0
        updated = 0
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            repo_id = str(repo.get("id") or "").strip()
            full_name = str(repo.get("full_name") or "").strip()
            if not repo_id or not full_name:
                continue
            owner, _, name = full_name.partition("/")
            name = name or full_name
            row = conn.execute(
                "SELECT repo_row_id FROM github_repositories WHERE account_id = ? AND github_repo_id = ?",
                [account_id, repo_id],
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO github_repositories (
                        repo_row_id, account_id, installation_id, github_repo_id,
                        node_id, owner, name, full_name, private, clone_url,
                        ssh_url, html_url, default_branch, permissions_json,
                        status, synced_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                    [
                        _new_repo_row_id(),
                        account_id,
                        str(installation_id or ""),
                        repo_id,
                        str(repo.get("node_id") or ""),
                        owner,
                        name,
                        full_name,
                        1 if repo.get("private") else 0,
                        str(repo.get("clone_url") or ""),
                        str(repo.get("ssh_url") or ""),
                        str(repo.get("html_url") or ""),
                        str(repo.get("default_branch") or ""),
                        json.dumps(repo.get("permissions") or {}),
                        now_iso,
                        now_iso,
                        now_iso,
                    ],
                )
                added += 1
                continue
            conn.execute(
                """UPDATE github_repositories
                   SET installation_id = ?,
                       node_id = ?,
                       owner = ?,
                       name = ?,
                       full_name = ?,
                       private = ?,
                       clone_url = ?,
                       ssh_url = ?,
                       html_url = ?,
                       default_branch = ?,
                       permissions_json = ?,
                       status = 'active',
                       sync_error = NULL,
                       synced_at = ?,
                       updated_at = ?
                   WHERE account_id = ? AND github_repo_id = ?""",
                [
                    str(installation_id or ""),
                    str(repo.get("node_id") or ""),
                    owner,
                    name,
                    full_name,
                    1 if repo.get("private") else 0,
                    str(repo.get("clone_url") or ""),
                    str(repo.get("ssh_url") or ""),
                    str(repo.get("html_url") or ""),
                    str(repo.get("default_branch") or ""),
                    json.dumps(repo.get("permissions") or {}),
                    now_iso,
                    now_iso,
                    account_id,
                    repo_id,
                ],
            )
            updated += 1
        conn.commit()
        return {"added": added, "updated": updated, "total": added + updated}

    def mark_github_repositories_missing(
        self,
        account_id: str,
        keep_github_repo_ids: set[str] | list[str],
    ) -> list[str]:
        """Mark a sync pass: repos of this account not in the keep set lose access.

        GitHub delivers removals both via webhooks and implicitly, when an
        installation's repository list comes back without a repo it used to
        contain. Only previously-usable rows are downgraded; anything already
        revoked/access_removed keeps its terminal state.
        """
        keep = {str(item) for item in (keep_github_repo_ids or []) if str(item).strip()}
        now_iso = _utcnow_iso()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT repo_row_id, github_repo_id FROM github_repositories WHERE account_id = ? AND status = 'active'",
            [account_id],
        ).fetchall()
        demoted: list[str] = []
        for row in rows:
            if str(row["github_repo_id"]) in keep:
                continue
            conn.execute(
                """UPDATE github_repositories
                   SET status = 'access_removed', updated_at = ?
                   WHERE repo_row_id = ?""",
                [now_iso, row["repo_row_id"]],
            )
            demoted.append(str(row["repo_row_id"]))
        conn.commit()
        return demoted

    def list_github_repositories(
        self,
        *,
        account_id: str | None = None,
        statuses: list[str] | tuple[str, ...] = ("active",),
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        normalized_statuses = [str(item).strip() for item in (statuses or ()) if str(item or "").strip()]
        if normalized_statuses and "all" not in normalized_statuses:
            placeholders = ", ".join("?" for _ in normalized_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(normalized_statuses)
        sql = "SELECT * FROM github_repositories"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        rows = conn.execute(sql, params).fetchall()
        result = [self._row_to_github_repository(row) for row in rows]
        needle = str(query or "").strip().casefold()
        if needle:
            def matches(item: dict[str, Any]) -> bool:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "full_name",
                        "owner",
                        "name",
                        "html_url",
                        "clone_url",
                        "local_path",
                        "alpha_project_id",
                    )
                ).casefold()
                return needle in haystack
            result = [item for item in result if matches(item)]
        cap = max(1, min(int(limit or 50), 200))
        return result[:cap]

    def get_github_repository(self, repo_row_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM github_repositories WHERE repo_row_id = ?",
            [str(repo_row_id or "").strip()],
        ).fetchone()
        return self._row_to_github_repository(row) if row is not None else None

    def find_github_repository(self, ref: str) -> dict[str, Any] | None:
        """Resolve a free-form repo reference to the newest matching row.

        Accepts a repo_row_id, ``owner/name``, an https clone/html URL, or an
        ssh URL. Case-insensitive on the name forms because GitHub treats them
        as such. Ties break on updated_at DESC, so a reconnected account wins.
        """
        normalized = str(ref or "").strip()
        if not normalized:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM github_repositories WHERE repo_row_id = ?",
            [normalized],
        ).fetchone()
        if row is not None:
            return self._row_to_github_repository(row)
        folded = normalized.casefold().rstrip("/")
        for candidate in self._github_ref_variants(folded):
            row = conn.execute(
                """SELECT * FROM github_repositories
                   WHERE LOWER(full_name) = ? OR LOWER(clone_url) = ?
                      OR LOWER(html_url) = ? OR LOWER(ssh_url) = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                [candidate, candidate, candidate, candidate],
            ).fetchone()
            if row is not None:
                return self._row_to_github_repository(row)
        return None

    @staticmethod
    def _github_ref_variants(ref: str) -> list[str]:
        """Name/URL forms that should match the same repository row."""
        variants = [ref]
        if ref.startswith("https://github.com/") and ref.endswith(".git"):
            variants.append(ref[: -len(".git")])
        if ref.startswith("https://github.com/"):
            variants.append(ref[len("https://github.com/"):].removesuffix(".git"))
        if ref.startswith("git@github.com:"):
            variants.append(ref[len("git@github.com:"):].removesuffix(".git"))
        if ref.startswith("ssh://git@github.com/"):
            variants.append(ref[len("ssh://git@github.com/"):].removesuffix(".git"))
        return [item for item in dict.fromkeys(variants) if item]

    def update_github_repository_progress(
        self,
        repo_row_id: str,
        *,
        local_path: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        commit_message: str | None = None,
        commit_author: str | None = None,
        commit_at: str | None = None,
        ahead: int | None = None,
        behind: int | None = None,
        dirty: bool | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        alpha_project_id: str | None = None,
        source: str | None = None,
        sync_error: str | None = None,
    ) -> dict[str, Any] | None:
        """Record Alpha's last known local state for a repository.

        Progress fields are COALESCEd so a partial report (for example a
        clone that failed before any commit was read) never erases a known
        good last-commit record.
        """
        normalized_row_id = str(repo_row_id or "").strip()
        if not normalized_row_id:
            return None
        now_iso = _utcnow_iso()
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT * FROM github_repositories WHERE repo_row_id = ?",
            [normalized_row_id],
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            """UPDATE github_repositories
               SET local_path = COALESCE(?, local_path),
                   branch = COALESCE(?, branch),
                   last_commit_sha = COALESCE(?, last_commit_sha),
                   last_commit_message = COALESCE(?, last_commit_message),
                   last_commit_author = COALESCE(?, last_commit_author),
                   last_commit_at = COALESCE(?, last_commit_at),
                   last_ahead = COALESCE(?, last_ahead),
                   last_behind = COALESCE(?, last_behind),
                   last_dirty = COALESCE(?, last_dirty),
                   last_task_id = COALESCE(?, last_task_id),
                   last_session_id = COALESCE(?, last_session_id),
                   alpha_project_id = COALESCE(?, alpha_project_id),
                   last_progress_source = ?,
                   last_progress_at = ?,
                   sync_error = ?,
                   updated_at = ?
               WHERE repo_row_id = ?""",
            [
                local_path,
                branch,
                commit_sha,
                commit_message,
                commit_author,
                commit_at,
                ahead,
                behind,
                None if dirty is None else (1 if dirty else 0),
                task_id,
                session_id,
                alpha_project_id,
                str(source or "").strip() or None,
                now_iso,
                str(sync_error or "").strip()[:500] or None,
                now_iso,
                normalized_row_id,
            ],
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM github_repositories WHERE repo_row_id = ?",
            [normalized_row_id],
        ).fetchone()
        return self._row_to_github_repository(updated) if updated is not None else None

    def mark_github_repositories_status(
        self,
        *,
        account_id: str,
        github_repo_ids: list[str] | set[str],
        status: str,
        sync_error: str | None = None,
    ) -> list[str]:
        """Transition repository rows to an explicit status (access_removed, revoked, active)."""
        normalized = [str(item) for item in (github_repo_ids or []) if str(item).strip()]
        if not normalized:
            return []
        now_iso = _utcnow_iso()
        conn = self._get_conn()
        updated: list[str] = []
        for repo_id in normalized:
            row = conn.execute(
                "SELECT repo_row_id FROM github_repositories WHERE account_id = ? AND github_repo_id = ?",
                [account_id, repo_id],
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "UPDATE github_repositories SET status = ?, sync_error = ?, updated_at = ? WHERE repo_row_id = ?",
                [status, (sync_error or "")[:500] or None, now_iso, row["repo_row_id"]],
            )
            updated.append(str(row["repo_row_id"]))
        conn.commit()
        return updated

    def set_github_repositories_status_for_installation(
        self,
        installation_id: str,
        *,
        status: str,
        sync_error: str | None = None,
    ) -> list[str]:
        """Transition every repository row of one installation to a status.

        Used by webhooks for installation-wide revocation/suspension, where
        the event carries the installation but not a repo list.
        """
        normalized = str(installation_id or "").strip()
        if not normalized:
            return []
        now_iso = _utcnow_iso()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT repo_row_id FROM github_repositories WHERE installation_id = ? AND status = 'active'",
            [installation_id],
        ).fetchall()
        updated: list[str] = []
        for row in rows:
            conn.execute(
                "UPDATE github_repositories SET status = ?, sync_error = ?, updated_at = ? WHERE repo_row_id = ?",
                [status, (sync_error or "")[:500] or None, now_iso, row["repo_row_id"]],
            )
            updated.append(str(row["repo_row_id"]))
        conn.commit()
        return updated

    def find_account_id_by_installation(self, installation_id: str) -> str | None:
        normalized = str(installation_id or "").strip()
        if not normalized:
            return None
        conn = self._get_conn()
        row = conn.execute(
            """SELECT account_id FROM accounts
               WHERE json_extract(metadata_json, '$.github_installation_id') = ?
               ORDER BY connected_at DESC LIMIT 1""",
            [normalized],
        ).fetchone()
        if row is not None:
            return str(row["account_id"])
        # Fallback for stores where the JSON1 functions are unavailable:
        # scan github accounts and match in Python.
        for account_row in conn.execute(
            "SELECT account_id, metadata_json FROM accounts WHERE provider = 'github'"
        ).fetchall():
            try:
                metadata = json.loads(account_row["metadata_json"] or "{}")
            except Exception:
                continue
            if str(metadata.get("github_installation_id") or "").strip() == normalized:
                return str(account_row["account_id"])
        return None

    def _row_to_github_repository(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            permissions = json.loads(row["permissions_json"] or "{}")
        except Exception:
            permissions = {}
        if not isinstance(permissions, dict):
            permissions = {}
        return {
            "repo_row_id": row["repo_row_id"],
            "account_id": row["account_id"],
            "installation_id": row["installation_id"],
            "github_repo_id": row["github_repo_id"],
            "node_id": row["node_id"],
            "owner": row["owner"],
            "name": row["name"],
            "full_name": row["full_name"],
            "private": bool(row["private"]),
            "clone_url": row["clone_url"],
            "ssh_url": row["ssh_url"],
            "html_url": row["html_url"],
            "default_branch": row["default_branch"],
            "permissions": permissions,
            "can_push": bool(permissions.get("push")),
            "local_path": row["local_path"],
            "branch": row["branch"],
            "last_commit": {
                "sha": row["last_commit_sha"],
                "message": row["last_commit_message"],
                "author": row["last_commit_author"],
                "committed_at": row["last_commit_at"],
            }
            if row["last_commit_sha"]
            else None,
            "last_ahead": int(row["last_ahead"] or 0),
            "last_behind": int(row["last_behind"] or 0),
            "last_dirty": bool(row["last_dirty"]),
            "last_task_id": row["last_task_id"],
            "last_session_id": row["last_session_id"],
            "alpha_project_id": row["alpha_project_id"],
            "last_progress_source": row["last_progress_source"],
            "last_progress_at": row["last_progress_at"],
            "status": row["status"],
            "sync_error": row["sync_error"],
            "synced_at": row["synced_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

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
