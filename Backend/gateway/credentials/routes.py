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
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credentials"])


# ── Request/Response models ──────────────────────────────────────────────────


class ConnectGoogleRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)
    account_label: str | None = None
    selected_tools: list[str] = Field(default_factory=list)
    is_primary: bool | None = None
    platform_key: str | None = None


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
    provided = request.headers.get("X-Local-Token", "")
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid local token")


def _check_internal_token(request: Request) -> None:
    """Require GATEWAY_INTERNAL_TOKEN for inter-service credential endpoints."""
    runtime = request.app.state.gateway_runtime
    expected = runtime.config.internal_token
    if not expected:
        return  # no token configured — open (dev mode)
    provided = request.headers.get("X-Internal-Token", "")
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid internal token")


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
                    "scope_match": all(scope in granted_scopes for scope in required_scopes) if required_scopes else True,
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
_USERINFO_API = "https://www.googleapis.com/oauth2/v2/userinfo"


def _calendar_window_bounds() -> tuple[str, str]:
    """Current month start → 2 months ahead (matches desktop behavior)."""
    now = datetime.now(tz=timezone.utc)
    window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_after_next = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return window_start.isoformat() + "Z", month_after_next.isoformat() + "Z"


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
                        except Exception:
                            continue
                    events.sort(
                        key=lambda e: (e.get("start") or "", e.get("summary") or "")
                    )
                    events = events[:96]
                    account_entry["calendar_count"] = len(calendars)
                    account_entry["upcoming_count"] = len(events)
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
