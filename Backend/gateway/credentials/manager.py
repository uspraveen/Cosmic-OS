"""Credential Manager — core logic for Google OAuth token lifecycle.

Responsibilities:
- Initiate OAuth connect flow (generate PKCE, return authorize URL)
- Handle OAuth callback (exchange code, store tokens)
- List / resolve credentials for orchestrator dispatch
- Refresh access tokens
- Disconnect / revoke credentials
- Multi-account resolution
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlencode

from .github_client import GitHubApiClient
from .providers import GoogleAdapter, ProviderAdapter, get_provider_adapter
from .store import CredentialStore

logger = logging.getLogger(__name__)

_GENERIC_ACCOUNT_LABELS = {"", "google account", "google"}
_GOOGLE_SCOPE_IMPLICATIONS = {
    "https://www.googleapis.com/auth/drive": {
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    },
    "https://www.googleapis.com/auth/calendar": {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    },
}


# OAuth error codes that mean the grant itself is dead. Anything else is an
# accident of the moment and must not cost the user a reconnect.
_GOOGLE_FATAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_scope",
}

# A refresh that fails transiently is retried in place before giving up.
_REFRESH_MAX_ATTEMPTS = 3
_REFRESH_RETRY_BACKOFF_SEC = (0.5, 1.5)

# How long a needs_auth account waits between self-heal attempts.
_RECOVERY_COOLDOWN_SEC = 900.0

# Webhook-free registry freshness for GitHub repository grants. A GitHub App
# has exactly one webhook URL, so per-user VMs can never each receive push
# events. Instead, every gateway re-enumerates its own installation when a
# registry read finds the stored grant older than this.
GITHUB_REGISTRY_FRESHNESS_SEC = 600.0

# Floor between re-enumerations of the same account, so a persistently failing
# sync (dead credential, GitHub outage) cannot turn every read into a retry.
GITHUB_REGISTRY_MIN_REFRESH_INTERVAL_SEC = 60.0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _oauth_error_payload(exc: BaseException | None) -> tuple[str, str]:
    """Pull the OAuth (error, error_description) pair out of a failed refresh."""
    response = getattr(exc, "response", None)
    if response is None:
        return "", ""
    try:
        payload = response.json()
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return (
        str(payload.get("error") or "").strip().lower(),
        str(payload.get("error_description") or "").strip(),
    )


def classify_refresh_failure(exc: BaseException | None) -> str:
    """Classify a token-refresh failure as 'fatal' or 'transient'.

    Only Google explicitly rejecting the grant means the user has to
    reconnect. Timeouts, connection resets, 5xx, rate limits and local
    resource exhaustion (e.g. EMFILE, which once condemned a perfectly
    healthy account for a week) say nothing at all about the refresh token.
    """
    if exc is None:
        return "transient"
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) not in {400, 401}:
        return "transient"
    error_code, _ = _oauth_error_payload(exc)
    if error_code:
        return "fatal" if error_code in _GOOGLE_FATAL_REFRESH_ERRORS else "transient"
    # 400/401 with no parseable OAuth error body: Google's token endpoint uses
    # these for genuine grant failures, so keep the historical behaviour.
    return "fatal"


def describe_refresh_failure(exc: BaseException | None) -> str:
    """Human-readable failure reason, preferring Google's own OAuth error.

    The raw httpx message is just a status line and URL. When an account is
    condemned, this string is the only surviving evidence of why, so it needs
    to name the actual cause.
    """
    error_code, description = _oauth_error_payload(exc)
    if error_code:
        detail = f"{error_code}: {description}" if description else error_code
        return f"Token refresh failed: {detail}"
    return f"Token refresh failed: {exc}"


def _is_generic_account_label(value: str | None) -> bool:
    return str(value or "").strip().lower() in _GENERIC_ACCOUNT_LABELS


def _account_display_label(account: dict[str, Any]) -> str:
    stored_label = str(account.get("account_label") or "").strip()
    if stored_label and not _is_generic_account_label(stored_label):
        return stored_label
    return (
        str(account.get("email") or "").strip()
        or str(account.get("display_name") or "").strip()
        or stored_label
        or "Google account"
    )


def _normalized_hint_value(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _hint_matches_value(normalized_hint: str, normalized_value: str) -> bool:
    if not normalized_hint or not normalized_value:
        return False
    if normalized_hint == normalized_value:
        return True
    if "@" in normalized_value or "@" in normalized_hint:
        return normalized_value in normalized_hint or normalized_hint in normalized_value
    if len(normalized_hint) >= 4 and normalized_hint in normalized_value:
        return True
    if len(normalized_value) >= 4 and normalized_value in normalized_hint:
        return True
    return False


def google_scopes_satisfy(granted_scopes: list[str] | set[str], required_scopes: list[str] | set[str]) -> bool:
    granted = {
        str(scope or "").strip()
        for scope in granted_scopes
        if str(scope or "").strip()
    }
    if not granted:
        return False
    effective_granted = set(granted)
    for scope in granted:
        effective_granted.update(_GOOGLE_SCOPE_IMPLICATIONS.get(scope, set()))
    required = {
        str(scope or "").strip()
        for scope in required_scopes
        if str(scope or "").strip()
    }
    return required.issubset(effective_granted)


def provider_scopes_satisfy(
    provider: str,
    granted_scopes: list[str] | set[str],
    required_scopes: list[str] | set[str],
) -> bool:
    """Does this credential cover what the caller needs?

    Scope coverage is a Google concept. A GitHub App authorises per repository
    at install time, and its user-to-server tokens legitimately come back with
    an empty scope list - so Google's rule (which treats "no scopes" as "no
    access") rejects every healthy GitHub credential, and the caller sees a
    connected account that can do nothing.
    """
    if str(provider or "").strip() == "github":
        return True
    return google_scopes_satisfy(granted_scopes, required_scopes)


# Google Calendar scopes for Phase 1
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

GOOGLE_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
]

GOOGLE_DOCS_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Base profile scopes
GOOGLE_BASE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Full default scope set for workspace + profile. Desktop normally sends the
# selected tool scopes explicitly, but this keeps non-desktop callers from
# silently creating a Calendar-only account.
GOOGLE_DEFAULT_SCOPES = (
    GOOGLE_BASE_SCOPES
    + GOOGLE_CALENDAR_SCOPES
    + GOOGLE_GMAIL_SCOPES
    + GOOGLE_DOCS_SCOPES
    + GOOGLE_SHEETS_SCOPES
)

# GitHub App user-to-server scopes. Repository access is NOT granted here - it
# comes from which repositories the user selects when installing the App, which
# is the whole reason for preferring an App over an OAuth App. `read:user` only
# identifies the account so it can be labelled in Settings.
GITHUB_DEFAULT_SCOPES = ("read:user",)


class OAuthFlowState:
    """Transient PKCE + state for an in-progress OAuth flow."""

    def __init__(
        self,
        provider: str,
        scopes: list[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.scopes = scopes
        self.metadata = dict(metadata or {})
        self.state = token_urlsafe(32)
        self.code_verifier = token_urlsafe(64)
        self.code_challenge = (
            urlsafe_b64encode(hashlib.sha256(self.code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )


class CredentialManager:
    """Gateway-owned credential manager for OAuth providers."""

    def __init__(
        self,
        store: CredentialStore,
        google_client_id: str = "",
        google_client_secret: str = "",
        google_redirect_uri: str = "",
        github_client_id: str = "",
        github_client_secret: str = "",
        github_redirect_uri: str = "",
        github_app_slug: str = "",
        github_api_client: Any = None,
    ) -> None:
        self._store = store
        self._google_client_id = google_client_id
        self._google_client_secret = google_client_secret
        self._google_redirect_uri = google_redirect_uri
        self._github_client_id = github_client_id
        self._github_client_secret = github_client_secret
        self._github_redirect_uri = github_redirect_uri
        self._github_app_slug = (github_app_slug or "").strip()
        self._github_api = github_api_client
        # In-flight OAuth flows: state -> OAuthFlowState
        self._pending_flows: dict[str, OAuthFlowState] = {}
        # Webhook-free registry freshness bookkeeping.
        self._inflight_github_syncs: set[str] = set()
        self._github_freshen_attempted_at: dict[str, float] = {}

    def _github_api_client(self):
        """Lazily construct the GitHub enumeration client.

        Injectable for tests; the lazy default keeps constructing a
        CredentialManager free of network clients when GitHub is unused.
        """
        if self._github_api is None:
            from .github_client import GitHubApiClient

            self._github_api = GitHubApiClient()
        return self._github_api

    def _oauth_client(self, provider: str) -> tuple[str, str, str]:
        """(client_id, client_secret, redirect_uri) for one provider.

        Explicit per provider on purpose. This used to hand Google's client id,
        secret and redirect URI to whatever adapter was selected, so the first
        non-Google provider added would have POSTed Google's client secret to a
        third party's token endpoint. An unknown provider now fails loudly.
        """
        if provider == "google":
            return (
                self._google_client_id,
                self._google_client_secret,
                self._google_redirect_uri,
            )
        if provider == "github":
            return (
                self._github_client_id,
                self._github_client_secret,
                self._github_redirect_uri,
            )
        raise ValueError(f"No OAuth client credentials configured for provider: {provider}")

    @property
    def google_configured(self) -> bool:
        return bool(self._google_client_id and self._google_client_secret)

    @property
    def github_configured(self) -> bool:
        return bool(self._github_client_id and self._github_client_secret)

    def provider_configured(self, provider: str) -> bool:
        try:
            client_id, client_secret, _redirect = self._oauth_client(provider)
        except ValueError:
            return False
        return bool(client_id and client_secret)

    # ── OAuth Connect Flow ────────────────────────────────────────────

    def start_oauth_flow(
        self,
        provider: str = "google",
        scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Start an OAuth flow. Returns {authorize_url, state}."""
        if provider == "google":
            if not self.google_configured:
                raise ValueError("Google OAuth client credentials are not configured.")
            effective_scopes = scopes or GOOGLE_DEFAULT_SCOPES
        elif provider == "github":
            if not self.github_configured:
                raise ValueError("GitHub OAuth client credentials are not configured.")
            effective_scopes = scopes or GITHUB_DEFAULT_SCOPES
        else:
            effective_scopes = scopes or []

        client_id, _client_secret, redirect_uri = self._oauth_client(provider)

        flow = OAuthFlowState(provider, effective_scopes, metadata=metadata)
        self._pending_flows[flow.state] = flow

        adapter = get_provider_adapter(provider)
        params = adapter.get_authorize_params(
            scopes=effective_scopes,
            state=flow.state,
            code_challenge=flow.code_challenge,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )

        authorize_url = f"{adapter.authorize_url}?{urlencode(params)}"
        return {"authorize_url": authorize_url, "state": flow.state}

    async def handle_oauth_callback(
        self,
        code: str,
        state: str,
    ) -> dict[str, Any]:
        """Handle OAuth callback. Exchange code, fetch profile, store tokens.
        Returns the account record."""
        flow = self._pending_flows.pop(state, None)
        if flow is None:
            raise ValueError("Invalid or expired OAuth state.")

        adapter = get_provider_adapter(flow.provider)

        client_id, client_secret, redirect_uri = self._oauth_client(flow.provider)

        # Exchange code for tokens
        token_resp = await adapter.exchange_code(
            code=code,
            code_verifier=flow.code_verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
        )

        # Fetch user profile. The adapter maps its own field names; see
        # ProviderAdapter.normalize_profile.
        profile = await adapter.get_user_info(token_resp.access_token)
        identity = adapter.normalize_profile(profile)
        provider_account_id = identity["provider_account_id"]
        email = identity["email"]
        display_name = identity["display_name"]
        avatar_url = identity["avatar_url"]
        hosted_domain = identity["hosted_domain"]
        if not provider_account_id:
            # A blank id collides with every other blank id, so two accounts
            # would silently overwrite each other in the store.
            raise ValueError(
                f"{flow.provider} did not return an account id for this user."
            )
        now_ts = time.time()
        requested_label = str(flow.metadata.get("account_label") or "").strip()
        if _is_generic_account_label(requested_label):
            requested_label = ""
        selected_tools = [
            str(item).strip()
            for item in (flow.metadata.get("selected_tools") or [])
            if str(item).strip()
        ]
        platform_key = str(flow.metadata.get("platform_key") or "workspace").strip() or "workspace"
        requested_primary = bool(flow.metadata.get("is_primary"))
        metadata_patch = {
            "avatar_url": avatar_url,
            "hosted_domain": hosted_domain,
            "last_connected_at": now_ts,
            "last_auth_error": "",
            "selected_tools": selected_tools,
            "required_scopes": token_resp.scopes or flow.scopes,
            "platform_key": platform_key,
        }

        # Check if account already exists
        existing = self._store.get_account_by_provider_account(
            flow.provider, provider_account_id
        )
        expires_at_ts = now_ts + max(60, token_resp.expires_in)

        if existing:
            account_id = existing["account_id"]
            existing_label = str(existing.get("account_label") or "").strip()
            self._store.update_account(
                account_id,
                email=email,
                display_name=display_name,
                status="active",
                account_label=(
                    requested_label
                    or ((email or display_name) if _is_generic_account_label(existing_label) else None)
                ),
                metadata_patch=metadata_patch,
            )
        else:
            account = self._store.create_account(
                provider=flow.provider,
                provider_account_id=provider_account_id,
                email=email,
                display_name=display_name,
                account_label=requested_label or email or display_name,
                is_primary=len(self._store.list_accounts(flow.provider)) == 0,
                metadata=metadata_patch,
            )
            account_id = account["account_id"]

        if requested_primary:
            self._store.set_primary(account_id)

        # Store credential
        credential_ref = self._store.store_credential(
            account_id=account_id,
            granted_scopes=token_resp.scopes or flow.scopes,
            access_token=token_resp.access_token,
            refresh_token=token_resp.refresh_token or "",
            expires_at_ts=expires_at_ts,
        )

        self._store.log_audit(
            action="connect",
            provider=flow.provider,
            result="success",
            credential_ref=credential_ref,
        )

        account = self._store.get_account(account_id)
        account["credential_ref"] = credential_ref
        account["granted_scopes"] = token_resp.scopes or flow.scopes
        return account

    # ── Account Management ────────────────────────────────────────────

    def list_accounts(self, provider: str = "google") -> list[dict[str, Any]]:
        accounts = self._store.list_accounts(provider)
        result = []
        for acct in accounts:
            cred = self._store.get_active_credential(acct["account_id"])
            metadata = acct.get("_metadata") if isinstance(acct.get("_metadata"), dict) else {}
            entry = dict(acct)
            entry["has_refresh_token"] = bool(cred and cred["refresh_token"])
            entry["granted_scopes"] = cred["granted_scopes"] if cred else []
            entry["token_expires_at"] = (
                cred["access_token_expires_at"] if cred else None
            )
            entry["avatar_url"] = str(metadata.get("avatar_url") or "").strip()
            entry["hosted_domain"] = str(metadata.get("hosted_domain") or "").strip()
            entry["last_connected_at"] = metadata.get("last_connected_at")
            entry["last_disconnected_at"] = metadata.get("last_disconnected_at")
            entry["last_auth_error"] = str(metadata.get("last_auth_error") or "").strip()
            entry["selected_tools"] = [
                str(item).strip()
                for item in (metadata.get("selected_tools") or [])
                if str(item).strip()
            ]
            entry["required_scopes"] = [
                str(item).strip()
                for item in (metadata.get("required_scopes") or [])
                if str(item).strip()
            ]
            entry["platform_key"] = str(metadata.get("platform_key") or "workspace").strip() or "workspace"
            entry["account_display_label"] = _account_display_label(entry)
            entry.pop("_metadata", None)
            result.append(entry)
        return result

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        acct = self._store.get_account(account_id)
        if acct is None:
            return None
        cred = self._store.get_active_credential(account_id)
        metadata = acct.get("_metadata") if isinstance(acct.get("_metadata"), dict) else {}
        entry = dict(acct)
        entry["has_refresh_token"] = bool(cred and cred["refresh_token"])
        entry["granted_scopes"] = cred["granted_scopes"] if cred else []
        entry["avatar_url"] = str(metadata.get("avatar_url") or "").strip()
        entry["hosted_domain"] = str(metadata.get("hosted_domain") or "").strip()
        entry["last_connected_at"] = metadata.get("last_connected_at")
        entry["last_disconnected_at"] = metadata.get("last_disconnected_at")
        entry["last_auth_error"] = str(metadata.get("last_auth_error") or "").strip()
        entry["selected_tools"] = [
            str(item).strip()
            for item in (metadata.get("selected_tools") or [])
            if str(item).strip()
        ]
        entry["required_scopes"] = [
            str(item).strip()
            for item in (metadata.get("required_scopes") or [])
            if str(item).strip()
        ]
        entry["platform_key"] = str(metadata.get("platform_key") or "workspace").strip() or "workspace"
        entry["account_display_label"] = _account_display_label(entry)
        entry.pop("_metadata", None)
        return entry

    def update_account_metadata(
        self, account_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Merge extra provider-specific facts onto an account.

        Used for things the OAuth exchange itself does not carry - GitHub's
        installation id arrives as a callback query parameter, not in the token
        response.
        """
        if not account_id or not isinstance(patch, dict) or not patch:
            return self._store.get_account(account_id) if account_id else None
        self._store.update_account(account_id, metadata_patch=patch)
        return self._store.get_account(account_id)

    # ── GitHub Repository Enumeration ─────────────────────────────────

    async def sync_github_repositories(
        self,
        account_id: str | None = None,
        *,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """Enumerate an installation's repositories into the store.

        Resolves a fresh user-to-server token, finds the account's GitHub App
        installation (metadata first, then a live installations lookup), lists
        the installation's repositories, reconciles them into the
        ``github_repositories`` table, and demotes rows that dropped out of
        the grant. Never raises into the caller: failures come back as
        ``{"synced": False, "error": ...}`` so a background sync can log and
        move on without an exception handler at every call site.
        """
        accounts = (
            [acct for acct in self._store.list_accounts("github") if acct.get("status") == "active"]
            if account_id is None
            else [acct for acct in [self._store.get_account(account_id)] if acct is not None]
        )
        if not accounts:
            return {
                "synced": False,
                "reason": "no_active_github_account",
                "repo_count": 0,
                "added": 0,
                "updated": 0,
                "removed": [],
            }

        if len(accounts) == 1:
            try:
                return await self._sync_github_repositories_for_account(
                    accounts[0], max_pages=max_pages
                )
            except Exception as exc:
                logger.warning(
                    "credentials.github_repo_sync_failed account_id=%s error=%s",
                    accounts[0].get("account_id"),
                    str(exc)[:200],
                )
                return {
                    "synced": False,
                    "account_id": accounts[0].get("account_id"),
                    "error": str(exc)[:300],
                    "repo_count": 0,
                    "added": 0,
                    "updated": 0,
                    "removed": [],
                }

        totals: dict[str, Any] = {"repo_count": 0, "added": 0, "updated": 0, "removed": []}
        failed: list[dict[str, Any]] = []
        for acct in accounts:
            try:
                single = await self._sync_github_repositories_for_account(acct, max_pages=max_pages)
            except Exception as exc:
                logger.warning(
                    "credentials.github_repo_sync_failed account_id=%s error=%s",
                    acct.get("account_id"),
                    str(exc)[:200],
                )
                failed.append(
                    {"account_id": acct.get("account_id"), "error": str(exc)[:300]}
                )
                continue
            totals["repo_count"] += int(single.get("repo_count") or 0)
            totals["added"] += int(single.get("added") or 0)
            totals["updated"] += int(single.get("updated") or 0)
            totals["removed"].extend(single.get("removed") or [])
        return {
            "synced": not failed,
            "failed_accounts": failed,
            "account_id": None,
            "installation_id": "",
            "repo_count": totals["repo_count"],
            "added": totals["added"],
            "updated": totals["updated"],
            "removed": totals["removed"],
        }

    def list_github_repositories(
        self,
        *,
        account_id: str | None = None,
        statuses: list[str] | tuple[str, ...] = ("active",),
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._store.list_github_repositories(
            account_id=account_id,
            statuses=statuses,
            query=query,
            limit=limit,
        )

    def find_github_repository(self, ref: str) -> dict[str, Any] | None:
        return self._store.find_github_repository(ref)

    def record_github_repository_progress(
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
        """Record Alpha's last known local state for one repository.

        Rejects unknown repos so a stale Alpha cannot fabricate rows; the
        authorization table stays gateway-owned.
        """
        return self._store.update_github_repository_progress(
            repo_row_id,
            local_path=local_path,
            branch=branch,
            commit_sha=commit_sha,
            commit_message=commit_message,
            commit_author=commit_author,
            commit_at=commit_at,
            ahead=ahead,
            behind=behind,
            dirty=dirty,
            task_id=task_id,
            session_id=session_id,
            alpha_project_id=alpha_project_id,
            source=source,
            sync_error=sync_error,
        )

    def find_account_id_by_installation(self, installation_id: str) -> str | None:
        return self._store.find_account_id_by_installation(installation_id)

    def mark_github_repositories_status(
        self,
        *,
        account_id: str,
        github_repo_ids: list[str] | set[str],
        status: str,
        sync_error: str | None = None,
    ) -> list[str]:
        return self._store.mark_github_repositories_status(
            account_id=account_id,
            github_repo_ids=github_repo_ids,
            status=status,
            sync_error=sync_error,
        )

    def revoke_github_repositories_for_installation(
        self,
        installation_id: str,
        *,
        sync_error: str | None = None,
    ) -> list[str]:
        return self._store.set_github_repositories_status_for_installation(
            installation_id,
            status="revoked",
            sync_error=sync_error,
        )

    def mark_github_repositories_for_installation(
        self,
        installation_id: str,
        *,
        status: str,
        sync_error: str | None = None,
    ) -> list[str]:
        return self._store.set_github_repositories_status_for_installation(
            installation_id,
            status=status,
            sync_error=sync_error,
        )

    async def _sync_github_repositories_for_account(
        self,
        account: dict[str, Any],
        *,
        max_pages: int,
    ) -> dict[str, Any]:
        """Sync one GitHub account's installation repository list.

        Raises on credential/API failure so callers can distinguish "nothing
        to sync" from "enumeration failed"; webhook and connect callers log.
        """
        account_id = str(account.get("account_id") or "")
        if not account_id or account.get("provider") != "github":
            raise ValueError(f"No GitHub account found: {account_id}")
        cred = self._store.get_active_credential(account_id)
        if cred is None or not cred.get("access_token"):
            raise PermissionError(f"No active GitHub credential for account {account_id}.")
        # Enumeration can take several paged round trips; a token resolved at
        # entry could expire mid-listing. Resolve through the shared refresh
        # path so the token is fresh by construction.
        resolved = await self.resolve_credential(
            provider="github",
            required_scopes=[],
            account_id=account_id,
            allow_primary_fallback=True,
        )
        if not resolved or not resolved.get("access_token"):
            # GitHub App tokens may be non-expiring (no refresh token); the
            # resolve path demands a refresh token, so fall back to the raw
            # stored access token when it is present and unexpired.
            cred = self._store.get_active_credential(account_id)
            expires_at = cred.get("access_token_expires_at") if cred else None
            if cred and cred.get("access_token") and (
                not expires_at or float(expires_at) > time.time() + 90
            ):
                resolved = {"access_token": cred["access_token"]}
        if not resolved or not resolved.get("access_token"):
            raise PermissionError(f"Unable to resolve a usable GitHub token for {account_id}.")
        token = str(resolved["access_token"])

        metadata = account.get("_metadata") if isinstance(account.get("_metadata"), dict) else {}
        installation_id = str(metadata.get("github_installation_id") or "").strip()
        try:
            if not installation_id:
                installation_id = await self._discover_github_installation_id(
                    token, account_id
                )
                if installation_id:
                    self.update_account_metadata(
                        account_id, {"github_installation_id": installation_id}
                    )
            await self._capture_github_installation_metadata(account_id, token)
            repositories = []
            if installation_id:
                repositories = await self._github_api_client().list_installation_repositories(
                    token,
                    installation_id,
                    max_pages=max_pages,
                )
        except PermissionError as exc:
            # GitHub rejected the credential at the API level: the grant is
            # dead right now, not merely stale. Condemn the account so health
            # reads show reauth_required instead of retrying a dead token all
            # day; the auth-health probe remains the throttled self-heal path.
            self.mark_account_auth_error(
                account_id, str(exc).strip() or "GitHub rejected the credential."
            )
            self._store.log_audit(
                action="github_repo_sync",
                provider="github",
                result="credential_rejected",
            )
            raise
        except KeyError:
            # The installation id stored at connect time no longer exists
            # (uninstalled from the GitHub side). Nothing to reconcile.
            self._store.log_audit(
                action="github_repo_sync",
                provider="github",
                result="installation_missing",
            )
            return {
                "account_id": account_id,
                "installation_id": "",
                "repo_count": 0,
                "added": 0,
                "updated": 0,
                "removed": [],
                "synced": False,
                "reason": "installation_missing",
            }

        summary = self._store.upsert_github_repositories(
            account_id=account_id,
            installation_id=installation_id,
            repos=repositories,
        )
        removed = self._store.mark_github_repositories_missing(
            account_id,
            [str(item.get("id")) for item in repositories if isinstance(item, dict)],
        )
        self._store.update_account(
            account_id,
            metadata_patch={
                "github_repos_synced_at": time.time(),
                "github_repos_count": len(repositories),
            },
        )
        self._store.log_audit(
            action="github_repo_sync",
            provider="github",
            result="success",
            credential_ref=(cred.get("credential_ref") if cred else "") or "",
        )
        logger.info(
            "credentials.github_repos_synced account_id=%s installation=%s repos=%d added=%d updated=%d removed=%d",
            account_id,
            installation_id,
            len(repositories),
            summary["added"],
            summary["updated"],
            len(removed),
        )
        return {
            "synced": True,
            "account_id": account_id,
            "installation_id": installation_id,
            "repo_count": len(repositories),
            "added": summary["added"],
            "updated": summary["updated"],
            "removed": removed,
        }

    async def _discover_github_installation_id(
        self,
        access_token: str,
        account_id: str,
    ) -> str:
        """Find this user's installation of our GitHub App.

        ``/user/installations`` returns installations across every App the
        user has authorized, so a bare count of one is only safe to trust when
        it is genuinely the only one. Any ambiguity is left unresolved: the
        connect flow already stamps the installation id from the callback, so
        this discovery path only fires for accounts connected before that
        existed or flows that skipped the install page.
        """
        try:
            installations = await self._github_api_client().list_user_installations(
                access_token
            )
        except Exception as exc:
            logger.warning(
                "credentials.github_installations_lookup_failed account_id=%s error=%s",
                account_id,
                str(exc)[:200],
            )
            return ""
        installation = self._match_owned_installation(installations)
        if installation is not None:
            return str(installation.get("id") or "").strip()
        logger.warning(
            "credentials.github_installation_ambiguous account_id=%s installations=%d",
            account_id,
            len(installations),
        )
        return ""

    def _match_owned_installation(
        self, installations: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Pick our App's installation out of ``/user/installations`` output.

        That endpoint returns installations across every App the user has
        authorized, so a bare count of one is only safe to trust when it is
        genuinely the only one. Anything ambiguous yields None rather than a
        guess.
        """
        owned = [
            item
            for item in installations
            if self._github_app_slug
            and str(item.get("app_slug") or "").strip().lower() == self._github_app_slug.lower()
        ]
        if not owned and len(installations) == 1:
            owned = installations
        return owned[0] if len(owned) == 1 else None

    async def _capture_github_installation_metadata(
        self, account_id: str, access_token: str
    ) -> None:
        """Best-effort refresh of the connection facts the settings panel shows.

        Records the GitHub login this account connected as and the
        installation's permission grant. A lookup failure logs and returns:
        the repository enumeration around it must not depend on this
        succeeding.
        """
        try:
            installations = await self._github_api_client().list_user_installations(
                access_token
            )
        except Exception as exc:
            logger.warning(
                "credentials.github_installation_metadata_lookup_failed account_id=%s error=%s",
                account_id,
                str(exc)[:200],
            )
            return
        installation = self._match_owned_installation(installations)
        if installation is None:
            return
        patch: dict[str, Any] = {}
        account_info = installation.get("account")
        login = (
            str(account_info.get("login") or "").strip()
            if isinstance(account_info, dict)
            else ""
        )
        if login:
            patch["github_login"] = login
        user_id = (
            str(account_info.get("id") or "").strip()
            if isinstance(account_info, dict)
            else ""
        )
        if user_id:
            patch["github_user_id"] = user_id
        permissions = installation.get("permissions")
        if isinstance(permissions, dict) and permissions:
            patch["github_permissions"] = permissions
        if patch:
            self.update_account_metadata(account_id, patch)

    def _github_registry_age(self, account_id: str) -> float | None:
        """Seconds since the registry was last reconciled, or None if never."""
        metadata = self._store.get_account(account_id)
        metadata = metadata.get("_metadata") if isinstance(metadata, dict) else None
        if not isinstance(metadata, dict):
            return None
        try:
            synced_at = float(metadata.get("github_repos_synced_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if synced_at <= 0:
            return None
        return max(0.0, time.time() - synced_at)

    async def _freshen_github_registry(self, account_id: str, *, blocking: bool) -> None:
        """Re-enumerate one account's installation if the registry went stale.

        Single-flight per account, with a minimum interval between attempts so
        a persistently failing sync cannot turn every read into a retry. The
        blocking variant awaits the refresh (resolve-miss retries); the
        background variant returns immediately. sync_github_repositories
        already contains its own failures, so callers never see exceptions
        from the refresh itself.
        """
        age = self._github_registry_age(account_id)
        if age is not None and age < GITHUB_REGISTRY_FRESHNESS_SEC:
            return
        now = time.time()
        if now - self._github_freshen_attempted_at.get(account_id, 0.0) < (
            GITHUB_REGISTRY_MIN_REFRESH_INTERVAL_SEC
        ):
            return
        if account_id in self._inflight_github_syncs:
            if not blocking:
                return
            # Someone else is refreshing this account; wait for the registry
            # to settle rather than doing the work twice.
            while account_id in self._inflight_github_syncs:
                await asyncio.sleep(0.1)
            return
        self._github_freshen_attempted_at[account_id] = now
        self._inflight_github_syncs.add(account_id)
        try:
            if blocking:
                await self.sync_github_repositories(account_id)
            else:
                asyncio.get_running_loop().create_task(
                    self._background_freshen(account_id)
                )
        finally:
            if blocking:
                self._inflight_github_syncs.discard(account_id)

    async def _background_freshen(self, account_id: str) -> None:
        try:
            await self.sync_github_repositories(account_id)
        except Exception:
            logger.exception(
                "credentials.github_registry_refresh_failed account_id=%s",
                account_id,
            )
        finally:
            self._inflight_github_syncs.discard(account_id)

    async def ensure_github_registry_fresh(
        self, account_id: str | None = None, *, blocking: bool = False
    ) -> None:
        """Keep the repository registry truthful by pulling, not by push.

        Reads call this: anything past the freshness TTL triggers one bounded
        re-enumeration of the user's own installation. That is what lets a
        per-user VM stay correct without a webhook — a GitHub App has exactly
        one webhook URL and could never deliver to every user's gateway.
        """
        accounts = self._store.list_accounts("github")
        candidates = [
            str(account.get("account_id") or "")
            for account in accounts
            if account.get("status") == "active"
            and (not account_id or account.get("account_id") == account_id)
        ]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                await self._freshen_github_registry(candidate, blocking=blocking)
            except Exception:
                logger.exception(
                    "credentials.github_registry_refresh_failed account_id=%s",
                    candidate,
                )

    async def probe_github_account_health(self, account_id: str) -> dict[str, Any]:
        """Actively verify one GitHub account's credential against the API.

        GitHub health otherwise only surfaces lazily — on the next refresh,
        sync, or API call. This is the GitHub counterpart of the Google auth
        health probe: resolve (which refreshes a near-expiry token), then one
        ``GET /user``. A revoked or expired credential comes back as
        ``reauth_required`` before a user-visible task hits it.
        """
        acct = self._store.get_account(account_id)
        if acct is None or acct.get("provider") != "github":
            return {
                "account_id": account_id,
                "login": "",
                "status": "unknown",
                "needs_reconnect": False,
                "error": "No such GitHub account.",
            }
        metadata = acct.get("_metadata") if isinstance(acct.get("_metadata"), dict) else {}
        result: dict[str, Any] = {
            "account_id": account_id,
            "login": str(metadata.get("github_login") or "").strip(),
            "status": "unknown",
            "needs_reconnect": False,
            "error": "",
        }
        if acct.get("status") == "needs_auth" and acct.get("has_refresh_token"):
            # Same self-heal the Google probe grants: one throttled refresh
            # settles whether needs_auth was a real revocation or a transient
            # failure that condemned the account unfairly.
            try:
                if await self.attempt_account_recovery(account_id):
                    acct = self._store.get_account(account_id) or acct
            except Exception:
                logger.exception(
                    "github_auth_health.recovery_failed account_id=%s", account_id
                )
        if acct.get("status") != "active":
            result.update(
                {
                    "status": "reauth_required",
                    "needs_reconnect": True,
                    "error": "GitHub account is not active. Reconnect it in Cosmic settings.",
                }
            )
            return result
        try:
            resolved = await self.resolve_credential(
                provider="github",
                required_scopes=[],
                account_id=account_id,
                allow_primary_fallback=True,
            )
            token = str((resolved or {}).get("access_token") or "")
            if not token:
                # GitHub App tokens may be non-expiring (no refresh token); the
                # resolve path demands one, so fall back to the stored token
                # when it is present and unexpired.
                cred = self._store.get_active_credential(account_id)
                expires_at = cred.get("access_token_expires_at") if cred else None
                if cred and cred.get("access_token") and (
                    not expires_at or float(expires_at) > time.time() + 90
                ):
                    token = str(cred["access_token"])
            if not token:
                result.update(
                    {
                        "status": "reauth_required",
                        "needs_reconnect": True,
                        "error": "No usable GitHub credential. Reconnect in Cosmic settings.",
                    }
                )
                return result
        except Exception as exc:
            result.update(
                {
                    "status": "reauth_required",
                    "needs_reconnect": True,
                    "error": str(exc)[:300] or "Unable to resolve the GitHub credential.",
                }
            )
            return result
        try:
            user = await self._github_api_client().get_authenticated_user(token)
        except PermissionError as exc:
            message = str(exc).strip() or "GitHub rejected the credential."
            self.mark_account_auth_error(account_id, message)
            result.update(
                {
                    "status": "reauth_required",
                    "needs_reconnect": True,
                    "error": message,
                }
            )
            return result
        except Exception as exc:
            result.update(
                {
                    "status": "provider_error",
                    "needs_reconnect": False,
                    "error": str(exc)[:300] or "GitHub API call failed.",
                }
            )
            return result
        login = str(user.get("login") or "").strip()
        if login:
            patch_user: dict[str, Any] = {"github_login": login}
            user_id = str(user.get("id") or "").strip()
            if user_id:
                patch_user["github_user_id"] = user_id
            self.update_account_metadata(account_id, patch_user)
        result.update({"login": login or result["login"], "status": "healthy"})
        return result

    def github_git_identity(self, account_id: str) -> dict[str, str] | None:
        """The commit identity Alpha must use for this account's checkouts.

        Commits have to land as the connected user, never as a bot name the
        registry invented. GitHub attributes commits by email, so the
        account's noreply address keeps the work linked to the user's profile
        without exposing a real inbox; the ID-prefixed form is what modern
        GitHub issues, with the bare-login legacy form as fallback when only
        the login is known.
        """
        acct = self._store.get_account(account_id)
        if acct is None or acct.get("provider") != "github":
            return None
        metadata = acct.get("_metadata") if isinstance(acct.get("_metadata"), dict) else {}
        login = str(metadata.get("github_login") or "").strip()
        if not login:
            return None
        name = str(acct.get("display_name") or "").strip() or login
        user_id = str(metadata.get("github_user_id") or "").strip()
        email = (
            f"{user_id}+{login}@users.noreply.github.com"
            if user_id
            else f"{login}@users.noreply.github.com"
        )
        return {"name": name, "email": email}

    async def disconnect_account(self, account_id: str) -> dict[str, Any]:
        """Revoke tokens and mark account as revoked."""
        acct = self._store.get_account(account_id)
        if acct is None:
            raise ValueError(f"Account not found: {account_id}")

        cred = self._store.get_active_credential(account_id)
        if cred and cred["refresh_token"]:
            try:
                adapter = get_provider_adapter(acct["provider"])
                revoke_id, revoke_secret, _redirect = self._oauth_client(acct["provider"])
                await adapter.revoke_token(
                    cred["refresh_token"],
                    revoke_id,
                    revoke_secret,
                )
            except Exception as exc:
                logger.warning("Failed to revoke token on provider side: %s", exc)

        self._store.revoke_account_credentials(account_id)
        self._store.update_account(
            account_id,
            status="revoked",
            metadata_patch={"last_disconnected_at": time.time()},
        )

        self._store.log_audit(
            action="revoke",
            provider=acct["provider"],
            result="success",
            credential_ref=cred["credential_ref"] if cred else "",
        )

        return self.get_account(account_id)

    # ── Credential Resolution (for orchestrator) ──────────────────────

    async def resolve_credential(
        self,
        *,
        provider: str,
        required_scopes: list[str],
        account_id: str | None = None,
        account_hint: str | None = None,
        resource_hint: str | None = None,
        session_id: str | None = None,
        allow_primary_fallback: bool = False,
        operation_mode: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a short-lived access token for the given provider + scopes.

        Follows §22.4 account resolution policy:
          1. explicit account_id
          2. resource binding (via resource_hint)
          3. session-context account (future)
          4. exactly one active account
          5. primary account as last resort (reads only)
          6. None → orchestrator must escalate to user

        Returns dict with: credential_ref, access_token, provider, scopes, expires_at
        Or None if no matching account/credential found or ambiguous.
        """
        del session_id
        normalized_operation_mode = str(operation_mode or "").strip().lower()

        # 1. If account_id given, use it directly
        if account_id:
            acct = self._store.get_account(account_id)
            if acct is None or acct["status"] != "active":
                return None
        # 2. If account_hint given, resolve by label/email/display name
        elif account_hint:
            account_id = self._resolve_account_hint(
                provider=provider,
                account_hint=account_hint,
                allow_primary_fallback=allow_primary_fallback,
            )
        # 3. If resource_hint, try resource bindings
        elif resource_hint:
            bindings = self._store.lookup_resource_binding(
                resource_type=f"{provider}_resource",
                external_id=resource_hint,
            )
            if bindings:
                account_id = bindings[0]["account_id"]
            else:
                account_id = None
        else:
            account_id = None

        # 4. If no account resolved yet, follow §22.4 policy
        if account_id is None:
            active_accounts = [
                a
                for a in self._store.list_accounts(provider)
                if a["status"] == "active"
                and self._store.get_active_credential(a["account_id"])
            ]
            if not active_accounts:
                return None
            if len(active_accounts) == 1:
                account_id = active_accounts[0]["account_id"]
            else:
                if allow_primary_fallback:
                    primary_accounts = [
                        account
                        for account in active_accounts
                        if bool(account.get("is_primary"))
                    ]
                    if len(primary_accounts) == 1:
                        account_id = primary_accounts[0]["account_id"]
                    else:
                        return None
                else:
                    return None

        # 5. Get credential and ensure access token is fresh
        cred = self._store.get_active_credential(account_id)
        if cred is None or not cred["refresh_token"]:
            self._store.log_audit(
                action="resolve",
                provider=provider,
                result="failed",
            )
            return None

        # 6. Check scope coverage
        if not provider_scopes_satisfy(
            provider, cred["granted_scopes"], required_scopes
        ):
            # Scope mismatch — needs re-consent
            self._store.log_audit(
                action="resolve",
                provider=provider,
                result="scope_insufficient",
                credential_ref=cred["credential_ref"],
                scopes_used=list(required_scopes),
            )
            return None

        # 7. Refresh if expired or expiring within 90 seconds
        now_ts = time.time()
        expires_at = cred["access_token_expires_at"]
        if not cred["access_token"] or (expires_at and expires_at < now_ts + 90):
            cred = await self._refresh_access_token(cred)

        # 8. Compute ISO expiry
        expires_at_iso = None
        if cred.get("access_token_expires_at"):
            try:
                expires_at_iso = datetime.fromtimestamp(
                    cred["access_token_expires_at"], tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        self._store.log_audit(
            action="resolve",
            provider=provider,
            result="success",
            credential_ref=cred["credential_ref"],
            scopes_used=list(required_scopes),
        )

        account = self._store.get_account(account_id) or {}

        return {
            "credential_ref": cred["credential_ref"],
            "access_token": cred["access_token"],
            "provider": provider,
            "scopes": cred["granted_scopes"],
            "expires_at": expires_at_iso,
            "account_id": account_id,
            "account_email": str(account.get("email") or "").strip() or None,
            "account_display_name": str(account.get("display_name") or "").strip() or None,
            "account_label": str(account.get("account_label") or "").strip() or None,
            "account_is_primary": bool(account.get("is_primary")),
            "operation_mode": normalized_operation_mode or None,
        }

    def update_account_preferences(
        self,
        account_id: str,
        *,
        account_label: str | None = None,
        is_primary: bool | None = None,
        selected_tools: list[str] | None = None,
        required_scopes: list[str] | None = None,
        platform_key: str | None = None,
    ) -> dict[str, Any]:
        acct = self._store.get_account(account_id)
        if acct is None:
            raise ValueError(f"Account not found: {account_id}")
        metadata_patch: dict[str, Any] = {}
        if selected_tools is not None:
            metadata_patch["selected_tools"] = [
                str(item).strip()
                for item in selected_tools
                if str(item).strip()
            ]
        if required_scopes is not None:
            metadata_patch["required_scopes"] = [
                str(item).strip()
                for item in required_scopes
                if str(item).strip()
            ]
        if platform_key is not None:
            metadata_patch["platform_key"] = str(platform_key).strip() or "workspace"
        updated = self._store.update_account(
            account_id,
            account_label=account_label,
            metadata_patch=metadata_patch or None,
        )
        if is_primary:
            self._store.set_primary(account_id)
            updated = self._store.get_account(account_id) or updated
        result = self.get_account(account_id)
        if result is None:
            raise ValueError(f"Account not found after update: {account_id}")
        return result

    def purge_account(self, account_id: str) -> None:
        acct = self._store.get_account(account_id)
        if acct is None:
            raise ValueError(f"Account not found: {account_id}")
        cred = self._store.get_active_credential(account_id)
        self._store.delete_account(account_id)
        self._store.log_audit(
            action="purge",
            provider=acct["provider"],
            result="success",
            credential_ref=cred["credential_ref"] if cred else "",
        )

    def mark_account_auth_error(self, account_id: str, error: str, *, status: str = "needs_auth") -> None:
        acct = self._store.get_account(account_id)
        if acct is None:
            return
        self._store.update_account(
            account_id,
            status=status,
            metadata_patch={
                "last_auth_error": str(error or "Google credential requires reconnect.")[:500],
            },
        )
        cred = self._store.get_active_credential(account_id)
        self._store.log_audit(
            action="auth_health_probe",
            provider=acct["provider"],
            result="failed",
            credential_ref=cred["credential_ref"] if cred else "",
        )

    async def attempt_account_recovery(
        self,
        account_id: str,
        *,
        cooldown_sec: float = _RECOVERY_COOLDOWN_SEC,
    ) -> bool:
        """Give a needs_auth account one throttled chance to revive itself.

        needs_auth is otherwise terminal: resolve_credential only considers
        active accounts, so nothing ever revisits one and the single exit is a
        manual reconnect. That is correct when the grant is genuinely revoked
        and wrong whenever the account was condemned by something unrelated to
        the grant. One refresh settles which it is - a revoked grant fails
        again and stays put, a healthy one is restored by the success path.

        Returns True only when the account came back as usable.
        """
        acct = self._store.get_account(account_id)
        if acct is None or acct.get("status") != "needs_auth":
            return False
        cred = self._store.get_active_credential(account_id)
        if cred is None or not cred.get("refresh_token"):
            return False

        metadata = acct.get("_metadata") or {}
        last_attempt = _parse_iso_ts(metadata.get("last_recovery_attempt_at"))
        if last_attempt is not None and time.time() - last_attempt < cooldown_sec:
            return False

        # Stamp before attempting so concurrent probes cannot stampede Google.
        self._store.update_account(
            account_id,
            metadata_patch={"last_recovery_attempt_at": _utcnow_iso()},
        )
        try:
            await self._refresh_access_token(cred)
        except Exception:
            # _record_refresh_failure already captured the reason and decided
            # whether the account stays condemned.
            return False
        restored = self._store.get_account(account_id)
        recovered = bool(restored and restored.get("status") == "active")
        if recovered:
            logger.info("credentials.account_recovered account_id=%s", account_id)
        return recovered

    async def refresh_credential(self, credential_ref: str) -> dict[str, Any] | None:
        """Refresh an access token by credential_ref. Used by orchestrator.refresh_credential."""
        cred = self._store.get_credential_by_ref(credential_ref)
        if cred is None:
            return None

        acct = self._store.get_account(cred["account_id"])
        if acct is None:
            return None

        refreshed = await self._refresh_access_token(cred)

        expires_at_iso = None
        if refreshed.get("access_token_expires_at"):
            try:
                expires_at_iso = datetime.fromtimestamp(
                    refreshed["access_token_expires_at"], tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        self._store.log_audit(
            action="refresh",
            provider=acct["provider"],
            result="success",
            credential_ref=credential_ref,
        )

        return {
            "credential_ref": refreshed["credential_ref"],
            "access_token": refreshed["access_token"],
            "provider": acct["provider"],
            "scopes": refreshed["granted_scopes"],
            "expires_at": expires_at_iso,
        }

    # ── Internal ──────────────────────────────────────────────────────

    async def _refresh_access_token(self, cred: dict[str, Any]) -> dict[str, Any]:
        """Refresh the access token using the stored refresh token."""
        acct = self._store.get_account(cred["account_id"])
        adapter = get_provider_adapter(acct["provider"])
        client_id, client_secret, _redirect = self._oauth_client(acct["provider"])

        token_resp = None
        last_exc: BaseException | None = None
        for attempt in range(_REFRESH_MAX_ATTEMPTS):
            try:
                token_resp = await adapter.refresh_token(
                    cred["refresh_token"],
                    client_id,
                    client_secret,
                )
                break
            except Exception as exc:
                last_exc = exc
                if classify_refresh_failure(exc) == "fatal":
                    break
                if attempt + 1 < _REFRESH_MAX_ATTEMPTS:
                    await asyncio.sleep(_REFRESH_RETRY_BACKOFF_SEC[attempt])

        if token_resp is None:
            self._record_refresh_failure(cred, acct, last_exc)
            raise last_exc if last_exc is not None else RuntimeError(
                f"Token refresh returned no response for {cred['credential_ref']}"
            )

        now_ts = time.time()
        expires_at_ts = now_ts + max(60, token_resp.expires_in)

        # Update stored refresh token if provider returned a new one
        if (
            token_resp.refresh_token
            and token_resp.refresh_token != cred["refresh_token"]
        ):
            self._store.store_credential(
                account_id=cred["account_id"],
                granted_scopes=token_resp.scopes or cred["granted_scopes"],
                access_token=token_resp.access_token,
                refresh_token=token_resp.refresh_token,
                expires_at_ts=expires_at_ts,
            )
            # Re-fetch the new credential
            new_cred = self._store.get_active_credential(cred["account_id"])
            if new_cred:
                # Same clearing the plain path does below: a rotated refresh
                # token is still a successful refresh, so the account must not
                # be left carrying a stale needs_auth/error state.
                self._clear_account_auth_error(cred["account_id"])
                return new_cred

        # Otherwise just update the access token
        self._store.update_access_token(
            cred["credential_ref"],
            token_resp.access_token,
            expires_at_ts=expires_at_ts,
        )

        # Clear auth error metadata on success
        self._clear_account_auth_error(cred["account_id"])

        cred["access_token"] = token_resp.access_token
        cred["access_token_expires_at"] = expires_at_ts
        return cred

    def _clear_account_auth_error(self, account_id: str) -> None:
        self._store.update_account(
            account_id,
            status="active",
            metadata_patch={
                "last_auth_error": "",
                "last_refresh_error": "",
                "last_refresh_failure_kind": "",
            },
        )

    def _record_refresh_failure(
        self,
        cred: dict[str, Any],
        acct: dict[str, Any],
        exc: BaseException | None,
    ) -> None:
        failure_kind = classify_refresh_failure(exc)
        message = describe_refresh_failure(exc)[:500]
        logger.error(
            "credentials.refresh_failed credential_ref=%s kind=%s error=%s",
            cred["credential_ref"],
            failure_kind,
            exc,
        )
        self._store.log_audit(
            action="refresh",
            provider=acct["provider"],
            result="failed" if failure_kind == "fatal" else "failed_transient",
            credential_ref=cred["credential_ref"],
        )
        metadata_patch: dict[str, Any] = {
            "last_refresh_error": message,
            "last_refresh_failure_kind": failure_kind,
            "last_refresh_failure_at": _utcnow_iso(),
        }
        if failure_kind == "fatal":
            metadata_patch["last_auth_error"] = message
            self._store.update_account(
                cred["account_id"],
                status="needs_auth",
                metadata_patch=metadata_patch,
            )
            return
        # Transient: leave the account active. Marking it needs_auth here would
        # be a one-way door - resolve_credential only considers active accounts,
        # so nothing would ever retry it and the user would be forced to
        # reconnect by hand over a blip that had nothing to do with the grant.
        self._store.update_account(cred["account_id"], metadata_patch=metadata_patch)

    def _resolve_account_hint(
        self,
        *,
        provider: str,
        account_hint: str,
        allow_primary_fallback: bool,
    ) -> str | None:
        normalized_hint = _normalized_hint_value(account_hint)
        if not normalized_hint:
            return None

        matches = self.account_hint_candidates(
            provider=provider,
            account_hint=account_hint,
            active_only=True,
        )
        if len(matches) == 1:
            return matches[0]["account_id"]
        if len(matches) > 1 and allow_primary_fallback:
            primary_matches = [
                account for account in matches if bool(account.get("is_primary"))
            ]
            if len(primary_matches) == 1:
                return primary_matches[0]["account_id"]
        return None

    def account_hint_candidates(
        self,
        *,
        provider: str,
        account_hint: str,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        normalized_hint = _normalized_hint_value(account_hint)
        if not normalized_hint:
            return []

        accounts = self._store.list_accounts(provider)
        if active_only:
            accounts = [
                account
                for account in accounts
                if account["status"] == "active"
                and self._store.get_active_credential(account["account_id"])
            ]
        if not accounts:
            return []

        def fields(account: dict[str, Any]) -> list[str]:
            metadata = account.get("_metadata") if isinstance(account.get("_metadata"), dict) else {}
            platform_key = str(metadata.get("platform_key") or "").strip()
            values = [
                _account_display_label(account),
                str(account.get("display_name") or "").strip(),
                str(account.get("email") or "").strip(),
                str(account.get("provider_account_id") or "").strip(),
                platform_key,
            ]
            stored_label = str(account.get("account_label") or "").strip()
            if stored_label and not _is_generic_account_label(stored_label):
                values.append(stored_label)
            if bool(account.get("is_primary")):
                values.extend(["primary", "main"])
            normalized_values: list[str] = []
            for value in values:
                normalized = _normalized_hint_value(value)
                if normalized and normalized not in normalized_values:
                    normalized_values.append(normalized)
            return normalized_values

        exact_matches = [
            account
            for account in accounts
            if normalized_hint in fields(account)
        ]
        if len(exact_matches) == 1:
            return exact_matches

        matches = [
            account
            for account in accounts
            if any(_hint_matches_value(normalized_hint, value) for value in fields(account))
        ]
        return exact_matches or matches
