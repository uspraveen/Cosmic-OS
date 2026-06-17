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
    ) -> None:
        self._store = store
        self._google_client_id = google_client_id
        self._google_client_secret = google_client_secret
        self._google_redirect_uri = google_redirect_uri
        # In-flight OAuth flows: state -> OAuthFlowState
        self._pending_flows: dict[str, OAuthFlowState] = {}

    @property
    def google_configured(self) -> bool:
        return bool(self._google_client_id and self._google_client_secret)

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
        else:
            effective_scopes = scopes or []

        flow = OAuthFlowState(provider, effective_scopes, metadata=metadata)
        self._pending_flows[flow.state] = flow

        adapter = get_provider_adapter(provider)
        params = adapter.get_authorize_params(
            scopes=effective_scopes,
            state=flow.state,
            code_challenge=flow.code_challenge,
            redirect_uri=self._google_redirect_uri,
            client_id=self._google_client_id,
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

        # Exchange code for tokens
        token_resp = await adapter.exchange_code(
            code=code,
            code_verifier=flow.code_verifier,
            redirect_uri=self._google_redirect_uri,
            client_id=self._google_client_id,
            client_secret=self._google_client_secret,
        )

        # Fetch user profile
        profile = await adapter.get_user_info(token_resp.access_token)
        provider_account_id = str(profile.get("id") or "")
        email = str(profile.get("email") or "")
        display_name = str(profile.get("name") or "")
        avatar_url = str(profile.get("picture") or "")
        hosted_domain = str(profile.get("hd") or "")
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

    async def disconnect_account(self, account_id: str) -> dict[str, Any]:
        """Revoke tokens and mark account as revoked."""
        acct = self._store.get_account(account_id)
        if acct is None:
            raise ValueError(f"Account not found: {account_id}")

        cred = self._store.get_active_credential(account_id)
        if cred and cred["refresh_token"]:
            try:
                adapter = get_provider_adapter(acct["provider"])
                await adapter.revoke_token(
                    cred["refresh_token"],
                    self._google_client_id,
                    self._google_client_secret,
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
        if not google_scopes_satisfy(cred["granted_scopes"], required_scopes):
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

        try:
            token_resp = await adapter.refresh_token(
                cred["refresh_token"],
                self._google_client_id,
                self._google_client_secret,
            )
        except Exception as exc:
            logger.error("Token refresh failed for %s: %s", cred["credential_ref"], exc)
            self._store.log_audit(
                action="refresh",
                provider=acct["provider"],
                result="failed",
                credential_ref=cred["credential_ref"],
            )
            self._store.update_account(
                cred["account_id"],
                status="needs_auth",
                metadata_patch={
                    "last_auth_error": f"Token refresh failed: {exc}",
                },
            )
            raise

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
                return new_cred

        # Otherwise just update the access token
        self._store.update_access_token(
            cred["credential_ref"],
            token_resp.access_token,
            expires_at_ts=expires_at_ts,
        )

        # Clear auth error metadata on success
        self._store.update_account(
            cred["account_id"],
            status="active",
            metadata_patch={"last_auth_error": ""},
        )

        cred["access_token"] = token_resp.access_token
        cred["access_token_expires_at"] = expires_at_ts
        return cred

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
