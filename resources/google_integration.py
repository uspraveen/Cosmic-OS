import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from database import db

load_dotenv()

# Google OAuth client credentials must come from the environment or DB-backed
# settings. Never commit live client credentials into the repo.
DEFAULT_GOOGLE_CLIENT_ID = ""
DEFAULT_GOOGLE_CLIENT_SECRET = ""
DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:8085/"
DEFAULT_GOOGLE_AUTH_TIMEOUT_SECONDS = 90
GOOGLE_CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
GOOGLE_CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
GOOGLE_CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_FULL_SCOPE = "https://www.googleapis.com/auth/calendar"


def _get_setting(key, env_key=None, fallback=""):
    db_value = str(db.get_setting(key, "") or "").strip()
    if db_value:
        return db_value
    if env_key:
        env_value = str(os.getenv(env_key, "") or "").strip()
        if env_value:
            return env_value
    return fallback


def get_google_oauth_config():
    redirect_uri = _get_setting("googleRedirectUri", "GOOGLE_REDIRECT_URI", DEFAULT_GOOGLE_REDIRECT_URI)
    if redirect_uri and not redirect_uri.endswith("/"):
        redirect_uri = f"{redirect_uri}/"
    client_id = _get_setting("googleClientId", "GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    client_secret = _get_setting("googleClientSecret", "GOOGLE_CLIENT_SECRET", DEFAULT_GOOGLE_CLIENT_SECRET)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


def _parse_redirect_uri(redirect_uri):
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8085
    return host, port


def _get_google_user_profile(access_token):
    response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _get_google_auth_timeout_seconds():
    raw_value = _get_setting(
        "googleAuthTimeoutSeconds",
        "GOOGLE_AUTH_TIMEOUT_SECONDS",
        str(DEFAULT_GOOGLE_AUTH_TIMEOUT_SECONDS),
    )
    try:
        timeout_value = int(raw_value)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_GOOGLE_AUTH_TIMEOUT_SECONDS
    return max(15, timeout_value)


def _patch_google_auth_metadata(account_id, message, status=None):
    patch = {"last_auth_error": str(message or "").strip()}
    try:
        db.update_integration_account_auth(account_id, status=status, metadata_patch=patch)
    except Exception:
        pass


def _refresh_google_access_token(account_id, refresh_token):
    oauth_config = get_google_oauth_config()
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": oauth_config["client_id"],
            "client_secret": oauth_config["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        error_payload = {}
        try:
            error_payload = response.json()
        except Exception:
            error_payload = {}
        error_code = str(error_payload.get("error") or "").strip().lower()
        error_description = str(error_payload.get("error_description") or response.text or "").strip()
        if error_code in {"invalid_grant", "invalid_client"}:
            _patch_google_auth_metadata(
                account_id,
                error_description or "Google access expired. Reconnect this account.",
                status="needs_auth",
            )
        else:
            _patch_google_auth_metadata(account_id, error_description or "Google token refresh failed.")
        response.raise_for_status()

    token_payload = response.json()
    access_token = str(token_payload.get("access_token") or "").strip()
    expires_in = int(token_payload.get("expires_in") or 3600)
    next_refresh_token = str(token_payload.get("refresh_token") or refresh_token or "").strip() or refresh_token
    expires_at = time.time() + max(60, expires_in)
    db.set_integration_credentials(
        account_id,
        access_token=access_token,
        refresh_token=next_refresh_token,
        access_token_expires_at=expires_at,
    )
    _patch_google_auth_metadata(account_id, "")
    return access_token


def ensure_google_access_token(account_id):
    account_id = str(account_id or "").strip()
    if not account_id:
        raise RuntimeError("Google account id is required.")

    credentials = db.get_integration_credentials(account_id)
    access_token = str(credentials.get("access_token") or "").strip()
    refresh_token = str(credentials.get("refresh_token") or "").strip()
    expires_at = credentials.get("access_token_expires_at")
    now = time.time()

    if access_token:
        try:
            expires_value = float(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            expires_value = None
        if expires_value is None or expires_value > now + 90:
            return access_token

    if not refresh_token:
        _patch_google_auth_metadata(account_id, "Google refresh token is missing. Reconnect this account.", status="needs_auth")
        raise RuntimeError("Google refresh token is missing. Reconnect this account.")

    return _refresh_google_access_token(account_id, refresh_token)


def _calendar_window_bounds():
    now = datetime.now().astimezone()
    window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_after_next = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return window_start.isoformat(), month_after_next.isoformat()


def _account_has_calendar_enabled(account):
    selected_tools = account.get("selected_tools") or []
    return "calendar" in selected_tools


def _account_has_calendar_scope(account):
    granted_scopes = account.get("granted_scopes") or []
    return (
        GOOGLE_CALENDAR_EVENTS_SCOPE in granted_scopes
        or GOOGLE_CALENDAR_READONLY_SCOPE in granted_scopes
        or GOOGLE_CALENDAR_FULL_SCOPE in granted_scopes
    )


def _account_has_calendar_list_scope(account):
    granted_scopes = account.get("granted_scopes") or []
    return (
        GOOGLE_CALENDAR_LIST_SCOPE in granted_scopes
        or GOOGLE_CALENDAR_READONLY_SCOPE in granted_scopes
        or GOOGLE_CALENDAR_FULL_SCOPE in granted_scopes
    )


def _google_get_json(access_token, url, *, params=None):
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
        timeout=20,
    )
    if response.status_code == 401:
        raise RuntimeError("Google access token expired.")
    response.raise_for_status()
    return response.json()


def fetch_google_calendar_list(account_id):
    account = db.get_integration_account(account_id)
    if not account:
        raise RuntimeError("Google account not found.")
    if not _account_has_calendar_list_scope(account):
        return []

    access_token = ensure_google_access_token(account_id)
    items = []
    page_token = None

    while True:
        params = {
            "showDeleted": "false",
            "minAccessRole": "reader",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            payload = _google_get_json(
                access_token,
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                params=params,
            )
        except RuntimeError:
            access_token = _refresh_google_access_token(
                account_id,
                str(db.get_integration_credentials(account_id).get("refresh_token") or "").strip(),
            )
            payload = _google_get_json(
                access_token,
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                params=params,
            )
        items.extend(payload.get("items") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    visible_calendars = []
    for item in items:
        access_role = str(item.get("accessRole") or "").strip()
        if access_role == "freeBusyReader":
            continue
        if item.get("hidden"):
            continue
        if item.get("selected") is False and not item.get("primary"):
            continue
        calendar_id = str(item.get("id") or "").strip()
        if not calendar_id:
            continue
        visible_calendars.append({
            "id": calendar_id,
            "name": str(item.get("summaryOverride") or item.get("summary") or "Calendar").strip() or "Calendar",
            "color": str(item.get("backgroundColor") or item.get("foregroundColor") or "").strip(),
            "primary": bool(item.get("primary")),
            "access_role": access_role or "reader",
        })

    return visible_calendars


def fetch_google_calendar_events(account_id, *, calendar_entry=None, time_min=None, time_max=None, max_results=24):
    account = db.get_integration_account(account_id)
    if not account:
        raise RuntimeError("Google account not found.")
    if not _account_has_calendar_enabled(account):
        return []
    if not _account_has_calendar_scope(account):
        raise RuntimeError("Calendar scope is not granted for this Google account.")

    if not time_min or not time_max:
        time_min, time_max = _calendar_window_bounds()

    calendar_id = str((calendar_entry or {}).get("id") or "primary").strip() or "primary"
    calendar_name = str((calendar_entry or {}).get("name") or ("Primary" if calendar_id == "primary" else "Calendar")).strip()
    calendar_color = str((calendar_entry or {}).get("color") or "").strip()
    calendar_primary = bool((calendar_entry or {}).get("primary")) or calendar_id == "primary"

    def request_events(access_token):
        return requests.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(calendar_id, safe='')}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "false",
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": max(1, int(max_results)),
            },
            timeout=20,
        )

    access_token = ensure_google_access_token(account_id)
    response = request_events(access_token)
    if response.status_code == 401:
        access_token = _refresh_google_access_token(
            account_id,
            str(db.get_integration_credentials(account_id).get("refresh_token") or "").strip(),
        )
        response = request_events(access_token)
    response.raise_for_status()
    _patch_google_auth_metadata(account_id, "")

    payload = response.json()
    items = payload.get("items") or []
    parsed_events = []
    for item in items:
        if str(item.get("status") or "").strip().lower() == "cancelled":
            continue

        start_payload = item.get("start") or {}
        end_payload = item.get("end") or {}
        is_all_day = bool(start_payload.get("date")) and not start_payload.get("dateTime")
        start_value = str(start_payload.get("dateTime") or start_payload.get("date") or "").strip()
        end_value = str(end_payload.get("dateTime") or end_payload.get("date") or "").strip()
        if not start_value:
            continue

        parsed_events.append({
            "id": f'{calendar_id}:{str(item.get("id") or "").strip() or f"evt-{len(parsed_events)}"}',
            "account_id": account["account_id"],
            "account_label": str(account.get("account_label") or account.get("display_name") or account.get("email") or "Google account"),
            "email": str(account.get("email") or ""),
            "calendar_id": calendar_id,
            "calendar_name": calendar_name,
            "calendar_color": calendar_color,
            "calendar_primary": calendar_primary,
            "summary": str(item.get("summary") or "Untitled event").strip() or "Untitled event",
            "description": str(item.get("description") or "").strip(),
            "organizer": str((item.get("organizer") or {}).get("email") or (item.get("organizer") or {}).get("displayName") or "").strip(),
            "location": str(item.get("location") or "").strip(),
            "start": start_value,
            "end": end_value or start_value,
            "htmlLink": str(item.get("htmlLink") or "").strip(),
            "meetingLink": str(item.get("hangoutLink") or "").strip(),
            "status": str(item.get("status") or "confirmed").strip() or "confirmed",
            "colorId": str(item.get("colorId") or "").strip(),
            "isAllDay": is_all_day,
            "attendees": [
                {
                    "email": str(attendee.get("email") or "").strip(),
                    "display_name": str(attendee.get("displayName") or attendee.get("email") or "Guest").strip() or "Guest",
                    "response_status": str(attendee.get("responseStatus") or "needsAction").strip() or "needsAction",
                    "self": bool(attendee.get("self")),
                }
                for attendee in (item.get("attendees") or [])
                if isinstance(attendee, dict)
            ],
        })

    return parsed_events


def get_google_calendar_agenda_snapshot():
    google_accounts = db.get_integration_accounts("google")
    calendar_accounts = []
    all_events = []
    error_messages = []

    for account in google_accounts:
        tool_enabled = _account_has_calendar_enabled(account)
        metadata = account.get("metadata") or {}
        has_refresh_token = bool(metadata.get("has_refresh_token"))
        needs_reconnect = bool(
            tool_enabled and (
                account.get("status") != "connected"
                or not has_refresh_token
                or not _account_has_calendar_scope(account)
            )
        )
        needs_scope_upgrade = bool(tool_enabled and not needs_reconnect and not _account_has_calendar_list_scope(account))
        account_entry = {
            "account_id": str(account.get("account_id") or ""),
            "account_label": str(account.get("account_label") or account.get("display_name") or account.get("email") or "Google account"),
            "email": str(account.get("email") or ""),
            "display_name": str(account.get("display_name") or ""),
            "status": str(account.get("status") or "needs_auth"),
            "is_primary": bool(account.get("is_primary")),
            "tool_enabled": tool_enabled,
            "has_refresh_token": has_refresh_token,
            "needs_reconnect": needs_reconnect,
            "needs_scope_upgrade": needs_scope_upgrade,
            "last_error": str(metadata.get("last_auth_error") or "").strip(),
            "upcoming_count": 0,
            "calendar_count": 0,
        }

        if tool_enabled and not needs_reconnect:
            try:
                if _account_has_calendar_list_scope(account):
                    calendars = fetch_google_calendar_list(account_entry["account_id"])
                else:
                    calendars = []
                if not calendars:
                    calendars = [{
                        "id": "primary",
                        "name": "Primary",
                        "color": "",
                        "primary": True,
                        "access_role": "owner",
                    }]
                events = []
                for calendar_entry in calendars:
                    events.extend(
                        fetch_google_calendar_events(
                            account_entry["account_id"],
                            calendar_entry=calendar_entry,
                        )
                    )
                events.sort(key=lambda event: (event.get("start") or "", event.get("summary") or ""))
                events = events[:96]
                account_entry["calendar_count"] = len(calendars)
                account_entry["upcoming_count"] = len(events)
                all_events.extend(events)
            except Exception as exc:
                error_text = str(exc).strip() or "Calendar sync failed."
                account_entry["last_error"] = error_text
                error_messages.append(f'{account_entry["account_label"]}: {error_text}')
        calendar_accounts.append(account_entry)

    all_events.sort(key=lambda event: (event.get("start") or "", event.get("summary") or ""))

    tool_enabled_accounts = [account for account in calendar_accounts if account["tool_enabled"]]
    active_accounts = [account for account in tool_enabled_accounts if not account["needs_reconnect"]]
    reconnect_accounts = [account for account in tool_enabled_accounts if account["needs_reconnect"]]
    upgrade_accounts = [account for account in tool_enabled_accounts if account["needs_scope_upgrade"]]

    if not tool_enabled_accounts:
        message = "Connect a Google account with Calendar enabled in Settings."
    elif active_accounts and all_events:
        message = f'{len(all_events)} events across {len(active_accounts)} account{"s" if len(active_accounts) != 1 else ""}.'
    elif active_accounts:
        message = "No upcoming events in the current calendar window."
    elif reconnect_accounts:
        message = "Reconnect Google Calendar to resume schedule sync."
    elif upgrade_accounts:
        message = "Reconnect Google Calendar to include secondary and shared calendars."
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


def connect_google_account(account_payload):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "google-auth-oauthlib is not installed. Add google-auth-oauthlib to the Python environment."
        ) from exc

    account = db.save_integration_account(account_payload)
    # Use scopes from the payload (frontend) so we request exactly what the user selected
    # (Calendar, Gmail, Docs, Drive). Fall back to saved account if payload omits them.
    scopes = (
        (account_payload.get("required_scopes") or [])
        or (account.get("required_scopes") or [])
    )
    if not scopes:
        raise RuntimeError("Select at least one Google tool before connecting an account.")

    oauth_config = get_google_oauth_config()
    if not oauth_config["client_id"] or not oauth_config["client_secret"]:
        raise RuntimeError("Google OAuth client credentials are not configured.")

    host, port = _parse_redirect_uri(oauth_config["redirect_uri"])
    existing_credentials = db.get_integration_credentials(account["account_id"])

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": oauth_config["client_id"],
                "client_secret": oauth_config["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [oauth_config["redirect_uri"]],
            }
        },
        scopes=scopes,
    )

    try:
        creds = flow.run_local_server(
            host=host,
            port=port,
            authorization_prompt_message="Opening browser for Google sign-in...",
            success_message="Google is now connected to Cosmic. You can return to the app.",
            open_browser=True,
            access_type="offline",
            prompt="consent select_account",
            include_granted_scopes="false",
            timeout_seconds=_get_google_auth_timeout_seconds(),
        )
    except Exception as exc:
        error_text = str(exc).lower()
        if isinstance(exc, TimeoutError) or (
            "nonetype" in error_text and "replace" in error_text
        ) or "timed out" in error_text:
            raise RuntimeError("Google sign-in was canceled or timed out.") from exc
        raise

    refresh_token = creds.refresh_token or existing_credentials.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google did not return a refresh token. Retry the flow and choose consent again.")

    profile = _get_google_user_profile(creds.token)
    granted_scopes = list(creds.scopes or [])

    db.set_integration_credentials(
        account["account_id"],
        access_token=creds.token,
        refresh_token=refresh_token,
        access_token_expires_at=creds.expiry.timestamp() if creds.expiry else None,
    )

    metadata_patch = {
        "provider_account_id": str(profile.get("id") or ""),
        "avatar_url": str(profile.get("picture") or ""),
        "hosted_domain": str(profile.get("hd") or ""),
        "last_connected_at": time.time(),
        "last_auth_error": "",
    }

    display_name = str(profile.get("name") or account.get("display_name") or "").strip()
    email = str(profile.get("email") or account.get("email") or "").strip()
    account_label = account.get("account_label") or display_name or email or "Google account"

    account = db.update_integration_account_auth(
        account["account_id"],
        status="connected",
        granted_scopes=granted_scopes,
        email=email,
        display_name=display_name,
        account_label=account_label,
        metadata_patch=metadata_patch,
    )
    _patch_google_auth_metadata(account["account_id"], "")

    # Backend token registration is intentionally disabled for now.
    # When the server-side credential manager is ready, restore the
    # registration call here instead of exposing backend fields in the UI.
    return db.get_integration_account(account["account_id"])


def disconnect_google_account(account_id):
    account = db.get_integration_account(account_id)
    if not account:
        raise RuntimeError("Google account not found.")

    credentials = db.get_integration_credentials(account_id)
    revoke_token = credentials.get("refresh_token") or credentials.get("access_token")
    revoke_error = ""
    if revoke_token:
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": revoke_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
            if response.status_code not in (200, 400):
                response.raise_for_status()
        except Exception as exc:
            revoke_error = str(exc)

    db.clear_integration_credentials(account_id)
    db.update_integration_account_auth(
        account_id,
        status="revoked",
        granted_scopes=[],
        metadata_patch={
            "last_auth_error": revoke_error,
            "last_disconnected_at": time.time(),
        },
    )
    return db.get_integration_account(account_id)
