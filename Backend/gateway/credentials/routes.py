"""Gateway credential + Google calendar API routes.

Exposed endpoints:
- POST   /auth/connect/google          → start OAuth flow, returns authorize_url
- GET    /auth/callback/google         → handle OAuth callback
- GET    /internal/credentials/accounts → list connected accounts
- DELETE /internal/credentials/accounts/{account_id} → disconnect account
- POST   /internal/credentials/resolve → resolve access token for orchestrator
- POST   /internal/credentials/refresh → refresh access token by credential_ref
- GET    /internal/google/calendar/agenda → agenda snapshot for desktop UI
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .manager import google_scopes_satisfy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credentials"])


# ── Request/Response models ──────────────────────────────────────────────────


class ConnectGoogleRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)
    account_label: str | None = None
    selected_tools: list[str] = Field(default_factory=list)
    is_primary: bool | None = None
    platform_key: str | None = None


class ConnectGitHubRequest(BaseModel):
    account_label: str | None = None
    is_primary: bool | None = None


class ResolveRequest(BaseModel):
    provider: str = "google"
    required_scopes: list[str] = Field(default_factory=list)
    account_id: str | None = None
    account_hint: str | None = None
    resource_hint: str | None = None
    session_id: str | None = None
    allow_primary_fallback: bool = False
    operation_mode: str | None = None


class UpdateAccountRequest(BaseModel):
    account_label: str | None = None
    is_primary: bool | None = None
    selected_tools: list[str] | None = None
    required_scopes: list[str] | None = None
    platform_key: str | None = None


class RefreshRequest(BaseModel):
    credential_ref: str


class GoogleAuthHealthRequest(BaseModel):
    agent_id: str | None = None
    tool: str
    required_scopes: list[str] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_manager(request: Request):
    runtime = request.app.state.gateway_runtime
    return runtime.credential_manager


def _check_local_token(request: Request) -> None:
    """Require GATEWAY_LOCAL_API_TOKEN for internal credential endpoints."""
    runtime = request.app.state.gateway_runtime
    expected = runtime.config.local_api_token
    if not expected:
        return  # no token configured — open (dev mode)
    provided = _extract_local_request_token(request)
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid local token")


def _extract_local_request_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for header_name in ("X-Local-Token", "X-API-Token"):
        token = request.headers.get(header_name, "").strip()
        if token:
            return token
    return ""


def _check_internal_token(request: Request) -> None:
    """Require GATEWAY_INTERNAL_TOKEN for inter-service credential endpoints."""
    runtime = request.app.state.gateway_runtime
    expected = runtime.config.internal_token
    if not expected:
        return  # no token configured — open (dev mode)
    provided = request.headers.get("X-Internal-Token", "")
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid internal token")


def _check_local_or_internal_token(request: Request) -> None:
    """Accept either trusted caller of the auth-health probes.

    The desktop settings panel authenticates with the local API token, while
    agent heartbeats on the VM carry the internal token. Both are
    gateway-configured secrets, so either one may read credential health.
    """
    try:
        _check_local_token(request)
        return
    except HTTPException:
        pass
    _check_internal_token(request)


def _gmail_was_disabled(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if not before or not after:
        return False
    before_tools = {
        str(item).strip()
        for item in (before.get("selected_tools") or [])
        if str(item).strip()
    }
    after_tools = {
        str(item).strip()
        for item in (after.get("selected_tools") or [])
        if str(item).strip()
    }
    return "gmail" in before_tools and "gmail" not in after_tools


def _gmail_was_enabled(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if not before or not after:
        return False
    before_tools = {
        str(item).strip()
        for item in (before.get("selected_tools") or [])
        if str(item).strip()
    }
    after_tools = {
        str(item).strip()
        for item in (after.get("selected_tools") or [])
        if str(item).strip()
    }
    return "gmail" not in before_tools and "gmail" in after_tools


def _extract_google_meeting_link(item: dict[str, Any]) -> str:
    direct = str(item.get("hangoutLink") or "").strip()
    if direct:
        return direct
    conference = item.get("conferenceData")
    if isinstance(conference, dict):
        entry_points = conference.get("entryPoints")
        if isinstance(entry_points, list):
            for entry in entry_points:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("entryPointType") or "").strip().lower() != "video":
                    continue
                uri = str(entry.get("uri") or "").strip()
                if uri:
                    return uri
    return ""


# ── Desktop-facing OAuth routes ──────────────────────────────────────────────


@router.post("/auth/connect/google")
async def start_google_connect(body: ConnectGoogleRequest, request: Request):
    """Start Google OAuth flow. Returns authorize_url for the desktop to open."""
    _check_local_token(request)
    mgr = _get_manager(request)
    if not mgr.google_configured:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth client credentials are not configured on the Gateway.",
        )
    scopes = body.scopes if body.scopes else None
    result = mgr.start_oauth_flow(
        provider="google",
        scopes=scopes,
        metadata={
            "account_label": body.account_label,
            "selected_tools": body.selected_tools,
            "is_primary": body.is_primary,
            "platform_key": body.platform_key,
        },
    )
    return result


@router.get("/auth/callback/google")
async def google_oauth_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    """Handle Google OAuth callback. Exchanges code for tokens."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")
    mgr = _get_manager(request)
    try:
        account = await mgr.handle_oauth_callback(code=code, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("OAuth callback failed")
        raise HTTPException(status_code=500, detail=str(exc))
    account_id = str(account.get("account_id") or "")
    account_snapshot = mgr.get_account(account_id) if account_id else account
    if "gmail" in set((account_snapshot or {}).get("selected_tools") or []):
        runtime = getattr(request.app.state, "gateway_runtime", None)
        if runtime is not None:
            asyncio.create_task(runtime.sync_gmail_watch_for_account(account_id))
    title = "Google Connected"
    subtitle = str(account.get("email") or account.get("account_label") or "Your Google account").strip() or "Your Google account"
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{title}</title>
    <style>
      body {{ background: #050607; color: #f5f7fb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; }}
      .card {{ width: min(92vw, 520px); padding: 32px 28px; border-radius: 24px; background: linear-gradient(180deg, rgba(22,26,34,.96), rgba(10,12,16,.98)); border: 1px solid rgba(255,255,255,.08); box-shadow: 0 24px 80px rgba(0,0,0,.45); }}
      h1 {{ margin: 0 0 8px; font-size: 28px; }}
      p {{ margin: 0; color: rgba(235,240,248,.74); line-height: 1.6; }}
      .badge {{ display: inline-block; margin-bottom: 16px; padding: 6px 10px; border-radius: 999px; background: rgba(84, 173, 88, .16); color: #9ce1a0; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }}
    </style>
  </head>
  <body>
    <div class="card">
      <div class="badge">Connected</div>
      <h1>{title}</h1>
      <p>{subtitle} is now available in COSMIC. You can return to the app.</p>
    </div>
  </body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@router.post("/auth/connect/github")
async def start_github_connect(body: ConnectGitHubRequest, request: Request):
    """Start GitHub App user authorization. Returns a URL for the desktop to open.

    First-time connects go to the App's install page rather than straight to
    the authorize endpoint. That page is where the user chooses which
    repositories Cosmic may touch, and with "Request user authorization during
    installation" enabled it returns an authorization code in the same pass -
    so one trip gets both the installation and the token. Going straight to
    authorize would produce a token scoped to no repositories at all.

    Once at least one account is connected we use the plain authorize URL,
    because a reconnect should not drag the user back through the repo picker.
    """
    _check_local_token(request)
    mgr = _get_manager(request)
    if not mgr.github_configured:
        raise HTTPException(
            status_code=503,
            detail="GitHub OAuth client credentials are not configured on the Gateway.",
        )
    result = mgr.start_oauth_flow(
        provider="github",
        metadata={
            "account_label": body.account_label,
            "is_primary": body.is_primary,
        },
    )
    runtime = getattr(request.app.state, "gateway_runtime", None)
    app_slug = ""
    if runtime is not None:
        app_slug = str(getattr(runtime.config, "github_app_slug", "") or "").strip()
    already_connected = bool(mgr.list_accounts("github"))
    if app_slug and not already_connected:
        result["authorize_url"] = (
            f"https://github.com/apps/{app_slug}/installations/new"
            f"?state={result['state']}"
        )
        result["flow"] = "install"
    else:
        result["flow"] = "authorize"
    return result


@router.get("/auth/callback/github")
async def github_oauth_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
    installation_id: str = Query(""),
    setup_action: str = Query(""),
):
    """Handle the GitHub callback relayed by the desktop's loopback listener."""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code or not state:
        # GitHub sends setup_action=install with no code when the user changes
        # an existing installation's repositories. Nothing to exchange, and it
        # is not a failure worth showing as one.
        if setup_action and installation_id:
            return HTMLResponse(
                content=_github_result_page(
                    "Repositories Updated",
                    "Cosmic's repository access has been updated.",
                ),
                status_code=200,
            )
        raise HTTPException(status_code=400, detail="Missing code or state.")
    mgr = _get_manager(request)
    try:
        account = await mgr.handle_oauth_callback(code=code, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("GitHub OAuth callback failed")
        raise HTTPException(status_code=500, detail=str(exc))
    if installation_id:
        account_id = str(account.get("account_id") or "")
        if account_id:
            try:
                mgr.update_account_metadata(
                    account_id, {"github_installation_id": installation_id}
                )
            except Exception:
                # The token is already stored; a missing installation id is
                # cosmetic and must not fail a successful connect.
                logger.exception(
                    "Failed to record GitHub installation id for %s", account_id
                )
            _schedule_github_repo_sync(request, account_id)
    subtitle = (
        str(account.get("display_name") or account.get("account_label") or "").strip()
        or "Your GitHub account"
    )
    return HTMLResponse(
        content=_github_result_page(
            "GitHub Connected",
            f"{subtitle} is now available in COSMIC. You can return to the app.",
        ),
        status_code=200,
    )


def _github_result_page(title: str, subtitle: str) -> str:
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{title}</title>
    <style>
      body {{ background: #050607; color: #f5f7fb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; }}
      .card {{ width: min(92vw, 520px); padding: 32px 28px; border-radius: 24px; background: linear-gradient(180deg, rgba(22,26,34,.96), rgba(10,12,16,.98)); border: 1px solid rgba(255,255,255,.08); box-shadow: 0 24px 80px rgba(0,0,0,.45); }}
      h1 {{ margin: 0 0 8px; font-size: 28px; }}
      p {{ margin: 0; color: rgba(235,240,248,.74); line-height: 1.6; }}
      .badge {{ display: inline-block; margin-bottom: 16px; padding: 6px 10px; border-radius: 999px; background: rgba(84, 173, 88, .16); color: #9ce1a0; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }}
    </style>
  </head>
  <body>
    <div class="card">
      <div class="badge">Connected</div>
      <h1>{title}</h1>
      <p>{subtitle}</p>
    </div>
  </body>
</html>"""


# ── Account management routes ────────────────────────────────────────────────


@router.get("/internal/credentials/accounts")
async def list_accounts(request: Request, provider: str = Query("google")):
    """List connected accounts for a provider. Used by desktop settings."""
    _check_local_token(request)
    mgr = _get_manager(request)
    return {"accounts": mgr.list_accounts(provider)}


@router.get("/internal/credentials/accounts/{account_id}")
async def get_account(request: Request, account_id: str):
    """Get a single connected account."""
    _check_local_token(request)
    mgr = _get_manager(request)
    acct = mgr.get_account(account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"account": acct}


@router.delete("/internal/credentials/accounts/{account_id}")
async def disconnect_account(request: Request, account_id: str):
    """Disconnect and revoke a Google account."""
    _check_local_token(request)
    mgr = _get_manager(request)
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if runtime is not None:
        try:
            await runtime.stop_gmail_watch_for_account(account_id)
        except Exception:
            logger.exception("gmail watch stop failed before account disconnect")
    try:
        result = await mgr.disconnect_account(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "revoked", "account": result}


@router.patch("/internal/credentials/accounts/{account_id}")
async def update_account(request: Request, account_id: str, body: UpdateAccountRequest):
    """Update display preferences for a connected account."""
    _check_local_token(request)
    mgr = _get_manager(request)
    before = mgr.get_account(account_id)
    try:
        result = mgr.update_account_preferences(
            account_id,
            account_label=body.account_label,
            is_primary=body.is_primary,
            selected_tools=body.selected_tools,
            required_scopes=body.required_scopes,
            platform_key=body.platform_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if _gmail_was_disabled(before, result):
        runtime = getattr(request.app.state, "gateway_runtime", None)
        if runtime is not None:
            try:
                await runtime.stop_gmail_watch_for_account(account_id)
            except Exception:
                logger.exception("gmail watch stop failed after Gmail tool disable")
    elif _gmail_was_enabled(before, result):
        runtime = getattr(request.app.state, "gateway_runtime", None)
        if runtime is not None:
            asyncio.create_task(runtime.sync_gmail_watch_for_account(account_id))
    return {"account": result}


@router.delete("/internal/credentials/accounts/{account_id}/purge")
async def purge_account(request: Request, account_id: str):
    """Remove an account record after disconnect."""
    _check_local_token(request)
    mgr = _get_manager(request)
    try:
        mgr.purge_account(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted", "account_id": account_id}


# ── Orchestrator-facing credential resolution ────────────────────────────────


@router.post("/internal/credentials/resolve")
async def resolve_credential(body: ResolveRequest, request: Request):
    """Resolve a short-lived access token for orchestrator dispatch.

    Returns 404 when no account is found OR when multiple accounts exist
    and the caller did not specify which one to use. In the multi-account
    case the orchestrator should escalate to the user via user.input_required.
    """
    _check_internal_token(request)
    mgr = _get_manager(request)
    result = await mgr.resolve_credential(
        provider=body.provider,
        required_scopes=body.required_scopes,
        account_id=body.account_id,
        account_hint=body.account_hint,
        resource_hint=body.resource_hint,
        session_id=body.session_id,
        allow_primary_fallback=body.allow_primary_fallback,
        operation_mode=body.operation_mode,
    )
    if result is None:
        hinted = bool(body.account_id or body.account_hint or body.resource_hint)
        if hinted:
            if body.account_hint:
                candidates = mgr.account_hint_candidates(
                    provider=body.provider,
                    account_hint=body.account_hint,
                    active_only=True,
                )
                if len(candidates) > 1:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "ambiguous_account",
                            "message": (
                                "The account hint matched multiple Google accounts. "
                                "Specify the exact account email."
                            ),
                            "accounts": [
                                {
                                    "account_id": a["account_id"],
                                    "account_label": a.get("account_label", ""),
                                    "account_display_label": (
                                        a.get("account_display_label")
                                        or a.get("email")
                                        or a.get("display_name")
                                        or a.get("account_label", "")
                                    ),
                                    "display_name": a.get("display_name", ""),
                                    "email": a.get("email", ""),
                                    "is_primary": a.get("is_primary", False),
                                }
                                for a in candidates
                            ],
                        },
                    )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "credential_unavailable",
                    "message": (
                        f"No usable {body.provider} credential matched the requested "
                        "account. The account may need reconnecting with the required scopes."
                    ),
                },
            )
        # Check if this is a multi-account ambiguity vs truly no account
        accounts = mgr.list_accounts(body.provider)
        if len(accounts) > 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "ambiguous_account",
                    "message": "Multiple Google accounts connected. Specify which account to use.",
                    "accounts": [
                        {
                            "account_id": a["account_id"],
                            "account_label": a.get("account_label", ""),
                            "account_display_label": a.get("account_display_label", ""),
                            "display_name": a.get("display_name", ""),
                            "email": a.get("email", ""),
                            "is_primary": a.get("is_primary", False),
                        }
                        for a in accounts
                    ],
                },
            )
        raise HTTPException(
            status_code=404,
            detail="No matching account or credential found. User may need to connect/re-consent.",
        )
    return result


@router.post("/internal/credentials/google/auth-health-version")
async def google_auth_health_version(body: GoogleAuthHealthRequest, request: Request):
    """Cheap change marker for the auth-health probe cache.

    Pure local DB read - no token refresh, no Google API calls. Agents poll
    this on every heartbeat to detect that an account's status changed (e.g.
    a user just reconnected) and bypass their longer-lived auth-health cache
    immediately instead of waiting out the full cache TTL.
    """
    _check_internal_token(request)
    mgr = _get_manager(request)
    tool = _normalize_google_tool(body.tool)
    required_scopes = _normalize_scope_list(body.required_scopes)
    if not tool:
        raise HTTPException(status_code=400, detail="Unknown Google tool for auth health probe.")
    accounts = [
        account
        for account in mgr.list_accounts("google")
        if _account_participates_in_tool(account, tool=tool, required_scopes=required_scopes)
    ]
    version = "|".join(
        f"{a.get('account_id')}:{a.get('status')}:{a.get('updated_at')}"
        for a in sorted(accounts, key=lambda a: str(a.get("account_id") or ""))
    )
    return {"version": version}


@router.post("/internal/credentials/google/auth-health")
async def google_auth_health(body: GoogleAuthHealthRequest, request: Request):
    """Probe Google auth health for a specialist without mutating user data.

    This is intentionally stronger than a process ping: it resolves/refreshes
    the Gateway-owned credential for each relevant connected account and then
    performs one tiny scoped Google API call. It lets agent heartbeats report
    reauth_required before a user-visible task fails.
    """
    _check_internal_token(request)
    mgr = _get_manager(request)
    tool = _normalize_google_tool(body.tool)
    required_scopes = _normalize_scope_list(body.required_scopes)
    if not tool:
        raise HTTPException(status_code=400, detail="Unknown Google tool for auth health probe.")
    if not required_scopes:
        raise HTTPException(status_code=400, detail="required_scopes must not be empty.")

    accounts = [
        account
        for account in mgr.list_accounts("google")
        if _account_participates_in_tool(account, tool=tool, required_scopes=required_scopes)
    ]
    if not accounts:
        return {
            "status": "healthy",
            "healthy": True,
            "available": True,
            "provider": "google",
            "tool": tool,
            "agent_id": body.agent_id,
            "reason": "no_connected_accounts_for_tool",
            "account_count": 0,
            "accounts": [],
        }

    account_results: list[dict[str, Any]] = []
    for account in accounts:
        account_id = str(account.get("account_id") or "").strip()
        account_result = {
            "account_id": account_id,
            "email": str(account.get("email") or "").strip(),
            "display_name": str(account.get("display_name") or "").strip(),
            "account_label": str(account.get("account_display_label") or account.get("account_label") or "").strip(),
            "is_primary": bool(account.get("is_primary")),
            "status": "unknown",
            "needs_reconnect": False,
            "error": "",
        }
        if (
            account_id
            and account.get("status") == "needs_auth"
            and account.get("has_refresh_token")
        ):
            # Nothing else ever retries a needs_auth account. This probe is the
            # natural place to give it one throttled attempt, so an account
            # condemned by a transient failure heals itself instead of waiting
            # on the user to notice a reconnect prompt.
            try:
                if await mgr.attempt_account_recovery(account_id):
                    account = mgr.get_account(account_id) or account
            except Exception:
                logger.exception(
                    "google_auth_health.recovery_failed account_id=%s", account_id
                )
        if not account_id or account.get("status") != "active" or not account.get("has_refresh_token"):
            account_result.update(
                {
                    "status": "reauth_required",
                    "needs_reconnect": True,
                    "error": "Google account is not active or has no refresh token.",
                }
            )
            account_results.append(account_result)
            continue
        try:
            auth = await mgr.resolve_credential(
                provider="google",
                required_scopes=required_scopes,
                account_id=account_id,
                operation_mode="read",
            )
            if not auth:
                account_result.update(
                    {
                        "status": "reauth_required",
                        "needs_reconnect": True,
                        "error": "Unable to resolve Google credential with required scopes.",
                    }
                )
                account_results.append(account_result)
                continue
            await _probe_google_tool_auth(tool, str(auth.get("access_token") or ""))
            account_result.update(
                {
                    "status": "healthy",
                    "needs_reconnect": False,
                    "credential_ref": auth.get("credential_ref"),
                    "expires_at": auth.get("expires_at"),
                }
            )
        except PermissionError as exc:
            message = str(exc).strip() or "Google credential requires reconnect."
            mgr.mark_account_auth_error(account_id, message)
            account_result.update(
                {
                    "status": "reauth_required",
                    "needs_reconnect": True,
                    "error": message,
                }
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            message = _google_auth_probe_error(exc)
            if status_code in {401, 403}:
                mgr.mark_account_auth_error(
                    account_id,
                    message,
                    status="needs_auth" if status_code == 401 else "active",
                )
                account_result.update(
                    {
                        "status": "reauth_required",
                        "needs_reconnect": True,
                        "error": message,
                    }
                )
            else:
                account_result.update(
                    {
                        "status": "provider_error",
                        "needs_reconnect": False,
                        "error": message,
                    }
                )
        except Exception as exc:
            account_result.update(
                {
                    "status": "provider_error",
                    "needs_reconnect": False,
                    "error": str(exc).strip()[:500] or "Google auth health probe failed.",
                }
            )
        account_results.append(account_result)

    healthy_count = sum(1 for item in account_results if item.get("status") == "healthy")
    reauth_count = sum(1 for item in account_results if item.get("status") == "reauth_required")
    provider_error_count = sum(1 for item in account_results if item.get("status") == "provider_error")
    if healthy_count == len(account_results):
        status_value = "healthy"
        available = True
    elif healthy_count > 0:
        status_value = "degraded"
        available = True
    elif reauth_count > 0 and provider_error_count == 0:
        status_value = "reauth_required"
        available = False
    else:
        status_value = "provider_error"
        available = False

    response = {
        "status": status_value,
        "healthy": status_value == "healthy",
        "available": available,
        "provider": "google",
        "tool": tool,
        "agent_id": body.agent_id,
        "account_count": len(account_results),
        "healthy_account_count": healthy_count,
        "reauth_required_count": reauth_count,
        "provider_error_count": provider_error_count,
        "accounts": account_results,
    }
    if reauth_count > 0:
        runtime = getattr(request.app.state, "gateway_runtime", None)
        if runtime is not None and hasattr(runtime, "publish_google_reauth_required"):
            try:
                await runtime.publish_google_reauth_required(
                    tool=tool,
                    agent_id=body.agent_id,
                    accounts=account_results,
                    status=status_value,
                )
            except Exception:
                logger.exception(
                    "google_auth_health.reauth_notification_failed tool=%s agent_id=%s",
                    tool,
                    body.agent_id,
                )
    return response


@router.get("/internal/credentials/google/snapshot")
async def google_integrations_snapshot(request: Request):
    """Gateway-backed integrations snapshot matching the desktop UI contract."""
    _check_local_token(request)
    mgr = _get_manager(request)
    accounts = mgr.list_accounts("google")
    connected_count = sum(
        1
        for account in accounts
        if account.get("status") == "active" and account.get("has_refresh_token")
    )
    normalized_accounts: list[dict[str, Any]] = []
    for account in accounts:
        required_scopes = [
            str(item).strip()
            for item in (account.get("required_scopes") or [])
            if str(item).strip()
        ]
        granted_scopes = [
            str(item).strip()
            for item in (account.get("granted_scopes") or [])
            if str(item).strip()
        ]
        selected_tools = [
            str(item).strip()
            for item in (account.get("selected_tools") or [])
            if str(item).strip()
        ]
        normalized_accounts.append(
            {
                "account_id": account["account_id"],
                "provider": "google",
                "platform_key": str(account.get("platform_key") or "workspace").strip() or "workspace",
                "email": str(account.get("email") or "").strip(),
                "display_name": str(account.get("display_name") or "").strip(),
                "account_label": str(account.get("account_label") or account.get("display_name") or account.get("email") or "Google account").strip() or "Google account",
                "status": "connected" if account.get("status") == "active" else str(account.get("status") or "needs_auth"),
                "is_primary": bool(account.get("is_primary")),
                "granted_scopes": granted_scopes,
                "required_scopes": required_scopes,
                "selected_tools": selected_tools,
                "metadata": {
                    "avatar_url": account.get("avatar_url", ""),
                    "hosted_domain": account.get("hosted_domain", ""),
                    "last_connected_at": account.get("last_connected_at"),
                    "last_disconnected_at": account.get("last_disconnected_at"),
                    "has_refresh_token": bool(account.get("has_refresh_token")),
                    "access_token_expires_at": account.get("token_expires_at"),
                    "last_auth_error": account.get("last_auth_error", ""),
                    "scope_match": google_scopes_satisfy(granted_scopes, required_scopes) if required_scopes else True,
                },
                "tools": [
                    {
                        "tool_id": tool_id,
                        "tool_name": tool_id.replace("_", " ").title(),
                        "platform_key": str(account.get("platform_key") or "workspace").strip() or "workspace",
                        "scopes": [],
                        "config": {},
                    }
                    for tool_id in selected_tools
                ],
            }
        )

    return {
        "providers": [
            {
                "provider": "google",
                "display_name": "Google",
                "metadata": {
                    "supports_multi_account": True,
                    "supports_tool_scopes": True,
                    "owner": "gateway",
                },
                "accounts": normalized_accounts,
                "account_count": len(normalized_accounts),
                "connected_count": connected_count,
            }
        ]
    }


@router.post("/internal/credentials/refresh")
async def refresh_credential(body: RefreshRequest, request: Request):
    """Refresh an access token by credential_ref. Used by orchestrator.refresh_credential."""
    _check_internal_token(request)
    mgr = _get_manager(request)
    result = await mgr.refresh_credential(body.credential_ref)
    if result is None:
        raise HTTPException(status_code=404, detail="Credential not found.")
    return result


# ── Desktop calendar agenda endpoint ─────────────────────────────────────────


_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_USERINFO_API = "https://www.googleapis.com/oauth2/v2/userinfo"


def _to_google_rfc3339_z(value: datetime) -> str:
    """Google Calendar accepts UTC RFC3339 timestamps with a single trailing Z."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _calendar_window_bounds() -> tuple[str, str]:
    """Current month start → 2 months ahead (matches desktop behavior)."""
    now = datetime.now(tz=timezone.utc)
    window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_after_next = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return _to_google_rfc3339_z(window_start), _to_google_rfc3339_z(month_after_next)


async def _google_get_json(
    access_token: str, url: str, params: dict | None = None
) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params or {},
        )
        if resp.status_code == 401:
            raise PermissionError("Google access token expired.")
        resp.raise_for_status()
        return resp.json()


def _normalize_scope_list(scopes: list[str]) -> list[str]:
    result: list[str] = []
    for scope in scopes or []:
        normalized = str(scope or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalize_google_tool(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "mail": "gmail",
        "google-mail": "gmail",
        "gcal": "calendar",
        "google-calendar": "calendar",
        "doc": "docs",
        "google-docs": "docs",
        "document": "docs",
        "documents": "docs",
        "sheet": "sheets",
        "google-sheets": "sheets",
        "spreadsheet": "sheets",
        "spreadsheets": "sheets",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"gmail", "calendar", "docs", "sheets"} else ""


def _account_participates_in_tool(
    account: dict[str, Any],
    *,
    tool: str,
    required_scopes: list[str],
) -> bool:
    if account.get("status") not in {"active", "needs_auth"}:
        return False
    granted_scopes = {
        str(item).strip()
        for item in (account.get("granted_scopes") or [])
        if str(item).strip()
    }
    selected_tools = {
        str(item).strip().lower().replace("_", "-")
        for item in (account.get("selected_tools") or [])
        if str(item).strip()
    }
    selected_tools = {_normalize_google_tool(item) or item for item in selected_tools}
    if selected_tools:
        return tool in selected_tools
    return bool(set(required_scopes).intersection(granted_scopes))


async def _probe_google_tool_auth(tool: str, access_token: str) -> None:
    if not access_token:
        raise PermissionError("Google access token is missing.")
    params: dict[str, Any]
    if tool == "gmail":
        await _google_get_json(access_token, f"{_GMAIL_API}/users/me/profile")
        return
    if tool == "calendar":
        await _google_get_json(
            access_token,
            f"{_CALENDAR_API}/users/me/calendarList",
            {"maxResults": 1, "showDeleted": "false"},
        )
        return
    if tool == "docs":
        params = {
            "q": "mimeType='application/vnd.google-apps.document' and trashed=false",
            "pageSize": 1,
            "fields": "files(id)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        await _google_get_json(access_token, f"{_DRIVE_API}/files", params)
        return
    if tool == "sheets":
        params = {
            "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            "pageSize": 1,
            "fields": "files(id)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        await _google_get_json(access_token, f"{_DRIVE_API}/files", params)
        return
    raise ValueError(f"Unknown Google tool: {tool}")


def _google_auth_probe_error(exc: httpx.HTTPStatusError) -> str:
    status_code = exc.response.status_code
    try:
        payload = exc.response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = str(error_payload.get("message") or "").strip()
            status = str(error_payload.get("status") or "").strip()
            if message and status:
                return f"Google API error {status_code} ({status}): {message}"[:500]
            if message:
                return f"Google API error {status_code}: {message}"[:500]
    return f"Google API error {status_code}"[:500]


async def _fetch_calendar_list(access_token: str) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        params: dict[str, Any] = {
            "showDeleted": "false",
            "minAccessRole": "reader",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = await _google_get_json(
            access_token, f"{_CALENDAR_API}/users/me/calendarList", params
        )
        items.extend(payload.get("items") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    visible = []
    for item in items:
        access_role = str(item.get("accessRole") or "").strip()
        if access_role == "freeBusyReader":
            continue
        if item.get("hidden"):
            continue
        if item.get("selected") is False and not item.get("primary"):
            continue
        cal_id = str(item.get("id") or "").strip()
        if not cal_id:
            continue
        visible.append(
            {
                "id": cal_id,
                "name": str(
                    item.get("summaryOverride") or item.get("summary") or "Calendar"
                ).strip()
                or "Calendar",
                "color": str(
                    item.get("backgroundColor") or item.get("foregroundColor") or ""
                ).strip(),
                "primary": bool(item.get("primary")),
                "access_role": access_role or "reader",
            }
        )
    return visible


async def _fetch_events(
    access_token: str,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 24,
) -> list[dict]:
    if not time_min or not time_max:
        time_min, time_max = _calendar_window_bounds()
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "showDeleted": "false",
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": max(1, int(max_results)),
    }
    import urllib.parse

    encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
    payload = await _google_get_json(
        access_token,
        f"{_CALENDAR_API}/calendars/{encoded_cal_id}/events",
        params,
    )

    events = []
    for item in payload.get("items") or []:
        if str(item.get("status") or "").strip().lower() == "cancelled":
            continue
        start_payload = item.get("start") or {}
        end_payload = item.get("end") or {}
        is_all_day = bool(start_payload.get("date")) and not start_payload.get(
            "dateTime"
        )
        start_val = str(
            start_payload.get("dateTime") or start_payload.get("date") or ""
        ).strip()
        end_val = str(
            end_payload.get("dateTime") or end_payload.get("date") or ""
        ).strip()
        if not start_val:
            continue
        events.append(
            {
                "id": f"{calendar_id}:{str(item.get('id') or '').strip() or f'evt-{len(events)}'}",
                "calendar_id": calendar_id,
                "summary": str(item.get("summary") or "Untitled event").strip()
                or "Untitled event",
                "description": str(item.get("description") or "").strip(),
                "location": str(item.get("location") or "").strip(),
                "start": start_val,
                "end": end_val or start_val,
                "isAllDay": is_all_day,
                "status": str(item.get("status") or "confirmed").strip() or "confirmed",
                "htmlLink": str(item.get("htmlLink") or "").strip(),
                "meetingLink": _extract_google_meeting_link(item),
                "colorId": str(item.get("colorId") or "").strip(),
                "organizer": str(
                    (item.get("organizer") or {}).get("email")
                    or (item.get("organizer") or {}).get("displayName")
                    or ""
                ).strip(),
                "attendees": [
                    {
                        "email": str(a.get("email") or "").strip(),
                        "display_name": str(
                            a.get("displayName") or a.get("email") or "Guest"
                        ).strip()
                        or "Guest",
                        "response_status": str(
                            a.get("responseStatus") or "needsAction"
                        ).strip()
                        or "needsAction",
                        "self": bool(a.get("self")),
                    }
                    for a in (item.get("attendees") or [])
                    if isinstance(a, dict)
                ],
            }
        )
    return events


@router.get("/internal/google/calendar/agenda")
async def get_calendar_agenda(request: Request):
    """Agenda snapshot endpoint for desktop UI.
    Replaces the desktop-local google_integration.get_google_calendar_agenda_snapshot().
    """
    _check_local_token(request)
    mgr = _get_manager(request)
    accounts = mgr.list_accounts("google")

    calendar_accounts: list[dict] = []
    all_events: list[dict] = []
    error_messages: list[str] = []

    for acct in accounts:
        account_id = acct["account_id"]
        has_refresh = acct.get("has_refresh_token", False)
        is_active = acct["status"] == "active" and has_refresh
        account_entry = {
            "account_id": account_id,
            "account_label": acct.get("account_label")
            or acct.get("display_name")
            or acct.get("email")
            or "Google account",
            "email": acct.get("email") or "",
            "display_name": acct.get("display_name") or "",
            "status": acct["status"],
            "is_primary": acct.get("is_primary", False),
            "tool_enabled": True,
            "has_refresh_token": has_refresh,
            "needs_reconnect": not is_active,
            "needs_scope_upgrade": False,
            "last_error": "",
            "upcoming_count": 0,
            "calendar_count": 0,
        }

        if is_active:
            try:
                account_errors: list[str] = []
                resolved = await mgr.resolve_credential(
                    provider="google",
                    required_scopes=[
                        "https://www.googleapis.com/auth/calendar",
                        "https://www.googleapis.com/auth/calendar.events",
                    ],
                    account_id=account_id,
                )
                if not resolved:
                    account_entry["needs_reconnect"] = True
                    account_entry["last_error"] = (
                        "Unable to resolve calendar credentials."
                    )
                else:
                    token = resolved["access_token"]
                    calendars = await _fetch_calendar_list(token)
                    if not calendars:
                        calendars = [
                            {
                                "id": "primary",
                                "name": "Primary",
                                "color": "",
                                "primary": True,
                                "access_role": "owner",
                            }
                        ]
                    events: list[dict] = []
                    for cal in calendars:
                        try:
                            cal_events = await _fetch_events(token, cal["id"])
                            for evt in cal_events:
                                evt["account_id"] = account_id
                                evt["account_label"] = account_entry["account_label"]
                                evt["email"] = account_entry["email"]
                                evt["calendar_name"] = cal["name"]
                                evt["calendar_color"] = cal["color"]
                                evt["calendar_primary"] = cal["primary"]
                            events.extend(cal_events)
                        except Exception as exc:
                            err = str(exc).strip() or "Calendar event sync failed."
                            label = cal.get("name") or cal.get("id") or "Calendar"
                            logger.warning(
                                "Calendar event fetch failed for account=%s calendar=%s: %s",
                                account_id,
                                cal.get("id"),
                                err,
                            )
                            account_errors.append(f"{label}: {err}")
                    events.sort(
                        key=lambda e: (e.get("start") or "", e.get("summary") or "")
                    )
                    events = events[:96]
                    account_entry["calendar_count"] = len(calendars)
                    account_entry["upcoming_count"] = len(events)
                    if account_errors:
                        error_messages.extend(
                            f"{account_entry['account_label']} / {err}"
                            for err in account_errors
                        )
                    if not events and account_errors and not account_entry["last_error"]:
                        account_entry["last_error"] = account_errors[0]
                    all_events.extend(events)
            except Exception as exc:
                err = str(exc).strip() or "Calendar sync failed."
                account_entry["last_error"] = err
                error_messages.append(f"{account_entry['account_label']}: {err}")

        calendar_accounts.append(account_entry)

    all_events.sort(key=lambda e: (e.get("start") or "", e.get("summary") or ""))

    tool_enabled = [a for a in calendar_accounts if a["tool_enabled"]]
    active = [a for a in tool_enabled if not a["needs_reconnect"]]
    reconnect = [a for a in tool_enabled if a["needs_reconnect"]]

    if not tool_enabled:
        message = "Connect a Google account with Calendar enabled in Settings."
    elif active and all_events:
        message = f"{len(all_events)} events across {len(active)} account{'s' if len(active) != 1 else ''}."
    elif active:
        message = "No upcoming events in the current calendar window."
    elif reconnect:
        message = "Reconnect Google Calendar to resume schedule sync."
    else:
        message = "Calendar sync is not ready yet."

    if error_messages and not all_events:
        message = error_messages[0]

    return {
        "state": "ready",
        "generated_at": time.time(),
        "message": message,
        "accounts": calendar_accounts,
        "events": all_events,
    }


# ── GitHub repository registry (internal) ────────────────────────────────────


async def _background_github_repo_sync(mgr, account_id: str) -> None:
    try:
        await mgr.sync_github_repositories(account_id)
    except Exception:
        logger.exception(
            "credentials.github_repo_sync_background_failed account_id=%s",
            account_id,
        )


def _schedule_github_repo_sync(request: Request, account_id: str) -> None:
    """Fire-and-forget repository enumeration after a connect/reconnect.

    The OAuth callback must not block on GitHub API pagination; the repo list
    lands a moment later and the tool surface reads it from the store.
    Best-effort by design: the connect already succeeded, and a failed
    enumeration retries on the next reconnect or webhook.
    """
    try:
        mgr = _get_manager(request)
    except Exception:
        return
    task = asyncio.create_task(_background_github_repo_sync(mgr, account_id))
    runtime = getattr(request.app.state, "gateway_runtime", None)
    background = getattr(runtime, "_background_tasks", None)
    if background is not None:
        background.add(task)


class GitHubRepoSyncRequest(BaseModel):
    account_id: str | None = None
    max_pages: int | None = None


class GitHubRepoProgressRequest(BaseModel):
    local_path: str | None = None
    branch: str | None = None
    commit: dict[str, Any] | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool | None = None
    task_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    source: str | None = None
    sync_error: str | None = None

# ── GitHub repository registry (internal — orchestrator / alpha) ────────────


def _public_github_repo(
    repo: dict[str, Any], git_identity: dict[str, str] | None = None
) -> dict[str, Any]:
    """Projection for tool-facing payloads: no token material, ids and state only."""
    payload = {
        "repo_row_id": repo.get("repo_row_id"),
        "account_id": repo.get("account_id"),
        "github_repo_id": repo.get("github_repo_id"),
        "full_name": repo.get("full_name"),
        "owner": repo.get("owner"),
        "name": repo.get("name"),
        "private": bool(repo.get("private")),
        "clone_url": repo.get("clone_url"),
        "ssh_url": repo.get("ssh_url"),
        "html_url": repo.get("html_url"),
        "default_branch": repo.get("default_branch"),
        "permissions": repo.get("permissions") or {},
        "can_push": bool(repo.get("can_push")),
        "local_path": repo.get("local_path"),
        "branch": repo.get("branch"),
        "last_commit": repo.get("last_commit"),
        "last_ahead": repo.get("last_ahead"),
        "last_behind": repo.get("last_behind"),
        "last_dirty": bool(repo.get("last_dirty")),
        "last_task_id": repo.get("last_task_id"),
        "last_session_id": repo.get("last_session_id"),
        "alpha_project_id": repo.get("alpha_project_id"),
        "last_progress_source": repo.get("last_progress_source"),
        "last_progress_at": repo.get("last_progress_at"),
        "status": repo.get("status"),
        "sync_error": repo.get("sync_error"),
        "synced_at": repo.get("synced_at"),
    }
    if git_identity:
        # Commits in this checkout must land as the connected user; Alpha
        # applies these as repo-local git config at checkout time. The login
        # additionally pins which connected account's token performs pushes.
        payload["git_author_name"] = git_identity.get("name") or ""
        payload["git_author_email"] = git_identity.get("email") or ""
        payload["git_author_login"] = git_identity.get("login") or ""
    return payload


@router.get("/internal/github/repositories")
async def list_github_repositories_route(
    request: Request,
    query: str = Query(""),
    limit: int = Query(50),
    status: str = Query("active"),
):
    """List connected GitHub repositories with their local progress.

    The orchestrator uses this to resolve repo references and report where a
    repository lives on the VM and what the last Alpha progress was; the
    desktop settings panel uses it to show which repositories are connected.
    Reads also nudge a background re-enumeration when the registry is stale —
    the webhook-free freshness model (a GitHub App has exactly one webhook
    URL, so per-user gateways pull instead).
    """
    _check_local_or_internal_token(request)
    mgr = _get_manager(request)
    await mgr.ensure_github_registry_fresh(blocking=False)
    mgr = _get_manager(request)
    statuses = ["all"] if status in ("all", "*") else [item.strip() for item in status.split(",") if item.strip()] or ["active"]
    repositories = mgr.list_github_repositories(
        statuses=tuple(statuses),
        query=query,
        limit=limit,
    )
    identity_cache: dict[str, dict[str, str] | None] = {}

    def _identity_for(account_id: Any) -> dict[str, str] | None:
        key = str(account_id or "")
        if key not in identity_cache:
            identity_cache[key] = mgr.github_git_identity(key) if key else None
        return identity_cache[key]

    return {
        "repositories": [
            _public_github_repo(item, _identity_for(item.get("account_id")))
            for item in repositories
        ],
        "count": len(repositories),
    }


@router.get("/internal/github/repositories/resolve")
async def resolve_github_repository_route(
    request: Request,
    ref: str = Query(""),
):
    """Resolve a repo id, owner/name, or clone/html/ssh URL to one repository.

    A miss first triggers one bounded re-enumeration of the installation, so
    a repository added on GitHub moments ago resolves without a webhook:
    this is the pull-model replacement for push freshness, at the exact
    moment freshness matters (a task is about to use the repo).
    """
    _check_internal_token(request)
    mgr = _get_manager(request)
    repo = mgr.find_github_repository(ref) if ref else None
    identity: dict[str, str] | None = None
    if repo is None and ref:
        await mgr.ensure_github_registry_fresh(blocking=True)
        repo = mgr.find_github_repository(ref)
    if repo is not None:
        identity = mgr.github_git_identity(str(repo.get("account_id") or ""))
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "repository_not_found",
                "message": "No connected GitHub repository matched that reference.",
            },
        )
    return {"found": True, "repository": _public_github_repo(repo, identity)}


@router.post("/internal/github/repositories/sync")
async def sync_github_repositories_route(body: GitHubRepoSyncRequest, request: Request):
    """Re-enumerate installation repositories from GitHub on demand.

    Called by agent tooling with the internal token and by the desktop
    settings panel's refresh action with the local token.
    """
    _check_local_or_internal_token(request)
    mgr = _get_manager(request)
    max_pages = max(1, min(int(body.max_pages or 10), 30))
    try:
        result = await mgr.sync_github_repositories(
            body.account_id or None, max_pages=max_pages
        )
    except Exception as exc:
        logger.exception("credentials.github_repo_sync_route_failed")
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    return result


@router.post("/internal/credentials/github/auth-health")
async def github_auth_health(request: Request):
    """Probe GitHub auth health for every connected account, actively.

    The GitHub counterpart of /internal/credentials/google/auth-health. Each
    account's credential is resolved (refreshing if near expiry) and then
    verified with one ``GET /user`` call, so a revoked or expired grant is
    reported as ``reauth_required`` before a user-visible task hits it.
    The desktop settings panel calls this on open; agent heartbeats may call
    it with the internal token.
    """
    _check_local_or_internal_token(request)
    mgr = _get_manager(request)
    accounts = [
        account
        for account in mgr.list_accounts("github")
        if account.get("status") != "revoked"
    ]
    if not accounts:
        return {
            "status": "healthy",
            "healthy": True,
            "available": False,
            "provider": "github",
            "reason": "no_connected_accounts",
            "account_count": 0,
            "accounts": [],
        }
    account_results: list[dict[str, Any]] = []
    for account in accounts:
        account_id = str(account.get("account_id") or "").strip()
        try:
            account_results.append(await mgr.probe_github_account_health(account_id))
        except Exception as exc:
            # The probe itself must never take the gateway down; a crashing
            # probe reports provider_error like any other API failure.
            logger.exception(
                "github_auth_health.probe_failed account_id=%s", account_id
            )
            account_results.append(
                {
                    "account_id": account_id,
                    "login": "",
                    "status": "provider_error",
                    "needs_reconnect": False,
                    "error": str(exc)[:300] or "Health probe failed.",
                }
            )
    statuses = {str(item.get("status") or "") for item in account_results}
    if "reauth_required" in statuses:
        overall = "reauth_required"
    elif "provider_error" in statuses or "unknown" in statuses:
        overall = "provider_error"
    else:
        overall = "healthy"
    return {
        "status": overall,
        "healthy": overall == "healthy",
        "available": any(item.get("status") == "healthy" for item in account_results),
        "provider": "github",
        "account_count": len(account_results),
        "accounts": account_results,
    }


@router.post("/internal/github/repositories/{repo_row_id}/progress")
async def github_repository_progress(
    repo_row_id: str, body: GitHubRepoProgressRequest, request: Request
):
    """Alpha reports a repository's last known local state (clone path, branch,
    last commit, ahead/behind, dirty flag) so the orchestrator can reason
    about progress without shelling out to git."""
    _check_internal_token(request)
    mgr = _get_manager(request)
    commit = body.commit if isinstance(body.commit, dict) else {}
    updated = mgr.record_github_repository_progress(
        str(repo_row_id or "").strip(),
        local_path=(body.local_path or "").strip() or None,
        branch=(body.branch or "").strip() or None,
        commit_sha=str(commit.get("sha") or "").strip() if commit else None,
        commit_message=(str(commit.get("message") or "").strip()[:500] if commit else None),
        commit_author=(str(commit.get("author") or "").strip() or None) if commit else None,
        commit_at=(str(commit.get("committed_at") or "").strip() or None) if commit else None,
        ahead=body.ahead,
        behind=body.behind,
        dirty=body.dirty,
        task_id=(body.task_id or "").strip() or None,
        session_id=(body.session_id or "").strip() or None,
        alpha_project_id=(body.project_id or "").strip() or None,
        source=(body.source or "").strip() or None,
        sync_error=body.sync_error,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Unknown GitHub repository.")
    return {"updated": True, "repository": _public_github_repo(updated)}
