"""Thin Google Sheets and Drive REST API client for the specialist agent."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import httpx


_SHEETS_BASE = "https://sheets.googleapis.com/v4"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"


class GoogleSheetsClient:
    def __init__(self, access_token: str, *, timeout_sec: float = 30.0) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout_sec

    async def list_spreadsheets(
        self,
        *,
        query: str = "",
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        q_parts = [f"mimeType='{_SHEETS_MIME}'", "trashed=false"]
        normalized_query = str(query or "").strip()
        if normalized_query:
            escaped = _drive_query_literal(normalized_query)
            q_parts.append(f"(name contains '{escaped}' or fullText contains '{escaped}')")
        params = {
            "q": " and ".join(q_parts),
            "pageSize": max(1, min(int(max_results), 50)),
            "orderBy": "viewedByMeTime desc,modifiedTime desc",
            "fields": (
                "files(id,name,mimeType,webViewLink,modifiedTime,viewedByMeTime,"
                "createdTime,owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress))"
            ),
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{_DRIVE_BASE}/files", headers=self._headers, params=params)
            self._raise_for_status(resp, "list Google Sheets")
            payload = resp.json()
        return [normalize_drive_file(item) for item in payload.get("files") or []]

    async def get_file(self, file_id: str) -> dict[str, Any]:
        if not file_id:
            raise ValueError("file_id is required.")
        encoded = urllib.parse.quote(file_id, safe="")
        params = {
            "fields": (
                "id,name,mimeType,webViewLink,modifiedTime,viewedByMeTime,"
                "createdTime,owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress)"
            ),
            "supportsAllDrives": "true",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{_DRIVE_BASE}/files/{encoded}", headers=self._headers, params=params)
            self._raise_for_status(resp, "get Google Drive file")
            return normalize_drive_file(resp.json())

    async def create_spreadsheet(
        self,
        *,
        title: str,
        sheet_titles: list[str] | None = None,
    ) -> dict[str, Any]:
        cleaned_titles = [str(item or "").strip() for item in sheet_titles or [] if str(item or "").strip()]
        if not cleaned_titles:
            cleaned_titles = ["Sheet1"]
        body = {
            "properties": {"title": str(title or "Untitled spreadsheet").strip() or "Untitled spreadsheet"},
            "sheets": [{"properties": {"title": sheet_title}} for sheet_title in cleaned_titles],
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_SHEETS_BASE}/spreadsheets",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(resp, "create Google Sheet")
            return normalize_spreadsheet(resp.json())

    async def get_spreadsheet(
        self,
        spreadsheet_id: str,
        *,
        include_grid_data: bool = False,
        ranges: list[str] | None = None,
    ) -> dict[str, Any]:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required.")
        encoded = urllib.parse.quote(spreadsheet_id, safe="")
        params: dict[str, Any] = {"includeGridData": "true" if include_grid_data else "false"}
        if ranges:
            params["ranges"] = ranges
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_SHEETS_BASE}/spreadsheets/{encoded}",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(resp, "read Google Sheet structure")
            return normalize_spreadsheet(resp.json())

    async def get_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        *,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> dict[str, Any]:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required.")
        if not range_name:
            raise ValueError("range is required.")
        encoded_id = urllib.parse.quote(spreadsheet_id, safe="")
        encoded_range = urllib.parse.quote(range_name, safe="")
        params = {"valueRenderOption": value_render_option}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_SHEETS_BASE}/spreadsheets/{encoded_id}/values/{encoded_range}",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(resp, "read Google Sheet range")
            return normalize_values_response(resp.json())

    async def update_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        self._validate_values(values)
        encoded_id = urllib.parse.quote(spreadsheet_id, safe="")
        encoded_range = urllib.parse.quote(range_name, safe="")
        params = {"valueInputOption": value_input_option}
        body = {"range": range_name, "majorDimension": "ROWS", "values": values}
        async with httpx.AsyncClient(timeout=max(self._timeout, 45.0)) as client:
            resp = await client.put(
                f"{_SHEETS_BASE}/spreadsheets/{encoded_id}/values/{encoded_range}",
                headers={**self._headers, "Content-Type": "application/json"},
                params=params,
                json=body,
            )
            self._raise_for_status(resp, "update Google Sheet cells")
            return resp.json()

    async def append_values(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
        insert_data_option: str = "INSERT_ROWS",
    ) -> dict[str, Any]:
        self._validate_values(values)
        encoded_id = urllib.parse.quote(spreadsheet_id, safe="")
        encoded_range = urllib.parse.quote(range_name, safe="")
        params = {
            "valueInputOption": value_input_option,
            "insertDataOption": insert_data_option,
        }
        body = {"range": range_name, "majorDimension": "ROWS", "values": values}
        async with httpx.AsyncClient(timeout=max(self._timeout, 45.0)) as client:
            resp = await client.post(
                f"{_SHEETS_BASE}/spreadsheets/{encoded_id}/values/{encoded_range}:append",
                headers={**self._headers, "Content-Type": "application/json"},
                params=params,
                json=body,
            )
            self._raise_for_status(resp, "append Google Sheet rows")
            return resp.json()

    async def clear_values(self, spreadsheet_id: str, range_name: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(spreadsheet_id, safe="")
        encoded_range = urllib.parse.quote(range_name, safe="")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_SHEETS_BASE}/spreadsheets/{encoded_id}/values/{encoded_range}:clear",
                headers={**self._headers, "Content-Type": "application/json"},
                json={},
            )
            self._raise_for_status(resp, "clear Google Sheet range")
            return resp.json()

    async def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required.")
        if not requests:
            raise ValueError("At least one Sheets batchUpdate request is required.")
        encoded = urllib.parse.quote(spreadsheet_id, safe="")
        async with httpx.AsyncClient(timeout=max(self._timeout, 45.0)) as client:
            resp = await client.post(
                f"{_SHEETS_BASE}/spreadsheets/{encoded}:batchUpdate",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"requests": requests},
            )
            self._raise_for_status(resp, "edit Google Sheet structure/formatting")
            return resp.json()

    async def list_permissions(self, file_id: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(file_id, safe="")
        params = {
            "fields": "permissions(id,type,role,emailAddress,domain,displayName,deleted)",
            "supportsAllDrives": "true",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_DRIVE_BASE}/files/{encoded}/permissions",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(resp, "list Google Drive permissions")
            payload = resp.json()
        return [normalize_permission(item) for item in payload.get("permissions") or []]

    async def create_permission(
        self,
        file_id: str,
        *,
        role: str,
        permission_type: str,
        email_address: str = "",
        domain: str = "",
        send_notification_email: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "role": role,
            "type": permission_type,
        }
        if permission_type == "user":
            if not email_address:
                raise ValueError("email_address is required for user permissions.")
            body["emailAddress"] = email_address
        elif permission_type == "domain":
            if not domain:
                raise ValueError("domain is required for domain permissions.")
            body["domain"] = domain
        encoded = urllib.parse.quote(file_id, safe="")
        params = {
            "sendNotificationEmail": "true" if send_notification_email else "false",
            "supportsAllDrives": "true",
            "fields": "id,type,role,emailAddress,domain,displayName",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_DRIVE_BASE}/files/{encoded}/permissions",
                headers={**self._headers, "Content-Type": "application/json"},
                params=params,
                json=body,
            )
            self._raise_for_status(resp, "share Google Drive file")
            return normalize_permission(resp.json())

    def _raise_for_status(self, resp: httpx.Response, action: str) -> None:
        if resp.status_code == 401:
            raise PermissionError("Google access token expired.")
        if resp.status_code == 404:
            raise ValueError(f"Google resource not found while trying to {action}.")
        resp.raise_for_status()

    @staticmethod
    def _validate_values(values: list[list[Any]]) -> None:
        if not isinstance(values, list) or not values:
            raise ValueError("values must be a non-empty list of rows.")
        if any(not isinstance(row, list) for row in values):
            raise ValueError("values must be a list of row lists.")


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit" if spreadsheet_id else ""


def normalize_drive_file(item: dict[str, Any]) -> dict[str, Any]:
    owners = item.get("owners") if isinstance(item.get("owners"), list) else []
    owner = owners[0] if owners else {}
    modifier = item.get("lastModifyingUser") if isinstance(item.get("lastModifyingUser"), dict) else {}
    file_id = str(item.get("id") or "").strip()
    return {
        "spreadsheet_id": file_id,
        "file_id": file_id,
        "title": str(item.get("name") or "").strip(),
        "mime_type": str(item.get("mimeType") or "").strip(),
        "url": str(item.get("webViewLink") or spreadsheet_url(file_id)).strip(),
        "created_time": str(item.get("createdTime") or "").strip(),
        "modified_time": str(item.get("modifiedTime") or "").strip(),
        "viewed_by_me_time": str(item.get("viewedByMeTime") or "").strip(),
        "owner": {
            "display_name": str(owner.get("displayName") or "").strip(),
            "email": str(owner.get("emailAddress") or "").strip(),
        },
        "last_modifying_user": {
            "display_name": str(modifier.get("displayName") or "").strip(),
            "email": str(modifier.get("emailAddress") or "").strip(),
        },
    }


def normalize_spreadsheet(payload: dict[str, Any]) -> dict[str, Any]:
    spreadsheet_id = str(payload.get("spreadsheetId") or "").strip()
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    sheets = []
    for sheet in payload.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        props = sheet.get("properties") if isinstance(sheet.get("properties"), dict) else {}
        grid = props.get("gridProperties") if isinstance(props.get("gridProperties"), dict) else {}
        sheets.append(
            {
                "sheet_id": props.get("sheetId"),
                "title": str(props.get("title") or "").strip(),
                "index": props.get("index"),
                "row_count": int(grid.get("rowCount") or 0),
                "column_count": int(grid.get("columnCount") or 0),
                "frozen_row_count": int(grid.get("frozenRowCount") or 0),
                "frozen_column_count": int(grid.get("frozenColumnCount") or 0),
            }
        )
    return {
        "spreadsheet_id": spreadsheet_id,
        "file_id": spreadsheet_id,
        "title": str(properties.get("title") or "").strip(),
        "url": spreadsheet_url(spreadsheet_id),
        "locale": str(properties.get("locale") or "").strip(),
        "time_zone": str(properties.get("timeZone") or "").strip(),
        "sheets": sheets,
        "raw": payload,
    }


def normalize_values_response(payload: dict[str, Any]) -> dict[str, Any]:
    values = payload.get("values") if isinstance(payload.get("values"), list) else []
    return {
        "range": str(payload.get("range") or "").strip(),
        "major_dimension": str(payload.get("majorDimension") or "").strip(),
        "values": values,
        "row_count": len(values),
        "column_count": max((len(row) for row in values if isinstance(row, list)), default=0),
    }


def normalize_permission(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "permission_id": str(item.get("id") or "").strip(),
        "type": str(item.get("type") or "").strip(),
        "role": str(item.get("role") or "").strip(),
        "email_address": str(item.get("emailAddress") or "").strip(),
        "domain": str(item.get("domain") or "").strip(),
        "display_name": str(item.get("displayName") or "").strip(),
        "deleted": bool(item.get("deleted")),
    }


def _drive_query_literal(value: str) -> str:
    return re.sub(r"['\\]", " ", value).strip()[:120]

