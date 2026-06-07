"""Google Calendar API client wrapper.

Provides typed methods for Google Calendar v3 API operations.
Uses short-lived access tokens from TaskEnvelope.input.auth.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    """Thin wrapper around Google Calendar v3 REST API."""

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._headers = {"Authorization": f"Bearer {access_token}"}

    # ── Calendar List ─────────────────────────────────────────────────

    async def list_calendars(self) -> list[dict[str, Any]]:
        """Fetch all visible calendars for the authenticated user."""
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                params: dict[str, Any] = {
                    "showDeleted": "false",
                    "minAccessRole": "reader",
                    "maxResults": 250,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    f"{_CALENDAR_BASE}/users/me/calendarList",
                    headers=self._headers,
                    params=params,
                )
                if resp.status_code == 401:
                    raise PermissionError("Google access token expired.")
                resp.raise_for_status()
                data = resp.json()
                items.extend(data.get("items") or [])
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        visible = []
        for item in items:
            access_role = str(item.get("accessRole") or "").strip()
            if access_role == "freeBusyReader":
                continue
            if item.get("hidden"):
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
                    "selected": bool(item.get("primary")) or item.get("selected") is not False,
                    "access_role": access_role or "reader",
                }
            )
        return visible

    # ── List Events ───────────────────────────────────────────────────

    async def list_events(
        self,
        calendar_id: str = "primary",
        *,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 25,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """List events on a single calendar within a time window."""
        params: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "false",
            "timeMin": time_min or _now_rfc3339(),
            "timeMax": time_max or _future_rfc3339(days=30),
            "maxResults": max(1, min(max_results, 250)),
        }
        if query:
            params["q"] = query

        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events",
                headers=self._headers,
                params=params,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            data = resp.json()

        return [
            _normalize_event(item, calendar_id)
            for item in (data.get("items") or [])
            if str(item.get("status") or "").lower() != "cancelled"
        ]

    async def get_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        """Fetch one event by id."""
        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{encoded_event_id}",
                headers=self._headers,
                params={"maxAttendees": 250},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Event not found: {event_id}")
            resp.raise_for_status()
            return _normalize_event(resp.json(), calendar_id)

    # ── Create Event ──────────────────────────────────────────────────

    async def create_event(
        self,
        calendar_id: str = "primary",
        *,
        summary: str,
        start: dict[str, str],
        end: dict[str, str],
        timezone: str | None = None,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        reminders: list[int] | None = None,
        add_google_meet: bool = False,
    ) -> dict[str, Any]:
        """Create a new calendar event.

        Args:
            start: {"dateTime": "..."} for timed or {"date": "..."} for all-day
            end: same shape as start
            attendees: list of email addresses
            reminders: list of minutes before event
        """
        body: dict[str, Any] = {
            "summary": summary,
            "start": start,
            "end": end,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if timezone:
            body["start"]["timeZone"] = timezone
            body["end"]["timeZone"] = timezone
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]
        if reminders is not None:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in reminders],
            }
        else:
            body["reminders"] = {"useDefault": True}
        params: dict[str, Any] = {"sendUpdates": "all" if attendees else "none"}
        if add_google_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet_{uuid4().hex}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = 1

        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
                params=params,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return _normalize_event(resp.json(), calendar_id)

    # ── Update Event ──────────────────────────────────────────────────

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Partially update an event. Uses PATCH for partial update safety."""
        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        body = dict(patch)
        add_google_meet = _coerce_bool(body.pop("add_google_meet", False))
        params: dict[str, Any] = {"sendUpdates": "all"}
        if add_google_meet and "conferenceData" not in body:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet_{uuid4().hex}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        if "conferenceData" in body:
            params["conferenceDataVersion"] = 1
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{encoded_event_id}",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
                params=params,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Event not found: {event_id}")
            resp.raise_for_status()
            return _normalize_event(resp.json(), calendar_id)

    async def respond_to_invite(
        self,
        calendar_id: str,
        event_id: str,
        *,
        attendee_email: str,
        response_status: str,
        etag: str | None = None,
    ) -> dict[str, Any]:
        """Update only the authenticated attendee's RSVP response."""
        if response_status not in {"accepted", "declined", "tentative", "needsAction"}:
            raise ValueError(f"Unsupported calendar invitation response: {response_status}")
        normalized_email = str(attendee_email or "").strip()
        if not normalized_email:
            raise ValueError("The responding calendar account email is required.")

        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        body = {
            "attendees": [
                {
                    "email": normalized_email,
                    "responseStatus": response_status,
                }
            ],
            "attendeesOmitted": True,
        }
        headers = {**self._headers, "Content-Type": "application/json"}
        if str(etag or "").strip():
            headers["If-Match"] = str(etag).strip()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{encoded_event_id}",
                headers=headers,
                json=body,
                params={"sendUpdates": "none", "maxAttendees": 250},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Event not found: {event_id}")
            resp.raise_for_status()
            return _normalize_event(resp.json(), calendar_id)

    # ── Delete Event ──────────────────────────────────────────────────

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
        *,
        notify_attendees: bool = True,
    ) -> bool:
        """Delete/cancel an event. Returns True on success."""
        encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
        encoded_event_id = urllib.parse.quote(event_id, safe="")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.delete(
                f"{_CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{encoded_event_id}",
                headers=self._headers,
                params={"sendUpdates": "all" if notify_attendees else "none"},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Event not found: {event_id}")
            resp.raise_for_status()
            return resp.status_code == 204

    # ── Free Busy ─────────────────────────────────────────────────────

    async def query_free_busy(
        self,
        calendar_ids: list[str],
        time_min: str,
        time_max: str,
    ) -> dict[str, list[dict[str, str]]]:
        """Query free/busy info for multiple calendars.

        Returns {calendar_id: [{"start": ..., "end": ...}]} for busy periods.
        """
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cid} for cid in calendar_ids],
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_CALENDAR_BASE}/freeBusy/query",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            data = resp.json()

        result: dict[str, list[dict[str, str]]] = {}
        calendars = data.get("calendars", {})
        for cal_id, cal_data in calendars.items():
            result[cal_id] = [
                {"start": b["start"], "end": b["end"]}
                for b in (cal_data.get("busy") or [])
                if "start" in b and "end" in b
            ]
        return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_event(item: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    """Normalize a Google Calendar event API response to a clean dict."""
    start_payload = item.get("start") or {}
    end_payload = item.get("end") or {}
    is_all_day = bool(start_payload.get("date")) and not start_payload.get("dateTime")

    return {
        "event_id": str(item.get("id") or "").strip(),
        "etag": str(item.get("etag") or "").strip(),
        "calendar_id": calendar_id,
        "summary": str(item.get("summary") or "Untitled event").strip()
        or "Untitled event",
        "description": str(item.get("description") or "").strip(),
        "location": str(item.get("location") or "").strip(),
        "start": str(
            start_payload.get("dateTime") or start_payload.get("date") or ""
        ).strip(),
        "end": str(
            end_payload.get("dateTime") or end_payload.get("date") or ""
        ).strip(),
        "is_all_day": is_all_day,
        "status": str(item.get("status") or "confirmed").strip() or "confirmed",
        "html_link": str(item.get("htmlLink") or "").strip(),
        "meeting_link": _extract_meeting_link(item),
        "color_id": str(item.get("colorId") or "").strip(),
        "recurring_event_id": str(item.get("recurringEventId") or "").strip() or None,
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
                "response_status": str(a.get("responseStatus") or "needsAction").strip()
                or "needsAction",
                "self": bool(a.get("self")),
                "organizer": bool(a.get("organizer")),
            }
            for a in (item.get("attendees") or [])
            if isinstance(a, dict)
        ],
        "created": str(item.get("created") or "").strip(),
        "updated": str(item.get("updated") or "").strip(),
    }


def _now_rfc3339() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _future_rfc3339(days: int = 30) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()


def _extract_meeting_link(item: dict[str, Any]) -> str:
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


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)
