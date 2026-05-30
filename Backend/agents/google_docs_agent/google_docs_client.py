"""Thin Google Docs and Drive REST API client for the specialist agent."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

import httpx


_DOCS_BASE = "https://docs.googleapis.com/v1"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"


class GoogleDocsClient:
    def __init__(self, access_token: str, *, timeout_sec: float = 30.0) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout_sec

    async def list_documents(
        self,
        *,
        query: str = "",
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        q_parts = ["mimeType='application/vnd.google-apps.document'", "trashed=false"]
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
            self._raise_for_status(resp, "list Google Docs")
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

    async def create_document(self, *, title: str) -> dict[str, Any]:
        body = {"title": str(title or "Untitled document").strip() or "Untitled document"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_DOCS_BASE}/documents",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(resp, "create Google Doc")
            payload = resp.json()
        document_id = str(payload.get("documentId") or "")
        return {
            "document_id": document_id,
            "title": str(payload.get("title") or body["title"]),
            "url": document_url(document_id),
            "revision_id": str(payload.get("revisionId") or ""),
            "raw": payload,
        }

    async def get_document(
        self,
        document_id: str,
        *,
        suggestions_view_mode: str = "SUGGESTIONS_INLINE",
        fields: str | None = None,
    ) -> dict[str, Any]:
        if not document_id:
            raise ValueError("document_id is required.")
        encoded = urllib.parse.quote(document_id, safe="")
        params: dict[str, str] = {"suggestionsViewMode": suggestions_view_mode}
        if fields:
            params["fields"] = fields
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_DOCS_BASE}/documents/{encoded}",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(resp, "read Google Doc")
            return resp.json()

    async def get_revision_id(self, document_id: str) -> str:
        try:
            payload = await self.get_document(document_id, fields="revisionId")
        except Exception:
            return ""
        return str(payload.get("revisionId") or "")

    async def batch_update(
        self,
        document_id: str,
        requests: list[dict[str, Any]],
        *,
        required_revision_id: str = "",
    ) -> dict[str, Any]:
        if not document_id:
            raise ValueError("document_id is required.")
        if not requests:
            raise ValueError("At least one Docs batchUpdate request is required.")
        body: dict[str, Any] = {"requests": requests}
        if required_revision_id:
            body["writeControl"] = {"requiredRevisionId": required_revision_id}
        encoded = urllib.parse.quote(document_id, safe="")
        async with httpx.AsyncClient(timeout=max(self._timeout, 45.0)) as client:
            resp = await client.post(
                f"{_DOCS_BASE}/documents/{encoded}:batchUpdate",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(resp, "edit Google Doc")
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

    async def list_comments(
        self,
        file_id: str,
        *,
        include_deleted: bool = False,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(file_id, safe="")
        comments: list[dict[str, Any]] = []
        page_token = ""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                params: dict[str, Any] = {
                    "fields": (
                        "comments(id,content,author(displayName,emailAddress),"
                        "createdTime,modifiedTime,resolved,quotedFileContent,"
                        "replies(id,content,author(displayName,emailAddress),createdTime,modifiedTime,deleted)),nextPageToken"
                    ),
                    "pageSize": max(1, min(max_results - len(comments), 100)),
                    "includeDeleted": "true" if include_deleted else "false",
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    f"{_DRIVE_BASE}/files/{encoded}/comments",
                    headers=self._headers,
                    params=params,
                )
                self._raise_for_status(resp, "list Google Drive comments")
                payload = resp.json()
                comments.extend(normalize_comment(item) for item in payload.get("comments") or [])
                page_token = str(payload.get("nextPageToken") or "")
                if not page_token or len(comments) >= max_results:
                    break
        return comments[:max_results]

    async def create_comment(
        self,
        file_id: str,
        *,
        content: str,
        quoted_text: str = "",
    ) -> dict[str, Any]:
        if not content:
            raise ValueError("content is required to create a comment.")
        body: dict[str, Any] = {"content": content}
        if quoted_text:
            body["quotedFileContent"] = {"mimeType": "text/plain", "value": quoted_text[:4096]}
        encoded = urllib.parse.quote(file_id, safe="")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_DRIVE_BASE}/files/{encoded}/comments",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
                params={"fields": "id,content,author(displayName,emailAddress),createdTime,resolved,quotedFileContent"},
            )
            self._raise_for_status(resp, "create Google Drive comment")
            return normalize_comment(resp.json())

    async def reply_to_comment(self, file_id: str, comment_id: str, *, content: str) -> dict[str, Any]:
        if not comment_id:
            raise ValueError("comment_id is required.")
        if not content:
            raise ValueError("content is required.")
        encoded_file = urllib.parse.quote(file_id, safe="")
        encoded_comment = urllib.parse.quote(comment_id, safe="")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_DRIVE_BASE}/files/{encoded_file}/comments/{encoded_comment}/replies",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"content": content},
                params={"fields": "id,content,author(displayName,emailAddress),createdTime,modifiedTime,deleted"},
            )
            self._raise_for_status(resp, "reply to Google Drive comment")
            return resp.json()

    async def update_comment_resolved(self, file_id: str, comment_id: str, *, resolved: bool) -> dict[str, Any]:
        if not comment_id:
            raise ValueError("comment_id is required.")
        encoded_file = urllib.parse.quote(file_id, safe="")
        encoded_comment = urllib.parse.quote(comment_id, safe="")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.patch(
                f"{_DRIVE_BASE}/files/{encoded_file}/comments/{encoded_comment}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"resolved": bool(resolved)},
                params={"fields": "id,content,resolved,modifiedTime"},
            )
            self._raise_for_status(resp, "update Google Drive comment")
            return normalize_comment(resp.json())

    def _raise_for_status(self, resp: httpx.Response, action: str) -> None:
        if resp.status_code == 401:
            raise PermissionError("Google access token expired.")
        if resp.status_code == 404:
            raise ValueError(f"Google resource not found while trying to {action}.")
        resp.raise_for_status()


def document_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit" if document_id else ""


def normalize_drive_file(item: dict[str, Any]) -> dict[str, Any]:
    owners = item.get("owners") if isinstance(item.get("owners"), list) else []
    owner = owners[0] if owners else {}
    modifier = item.get("lastModifyingUser") if isinstance(item.get("lastModifyingUser"), dict) else {}
    file_id = str(item.get("id") or "").strip()
    return {
        "document_id": file_id,
        "file_id": file_id,
        "title": str(item.get("name") or "").strip(),
        "mime_type": str(item.get("mimeType") or "").strip(),
        "url": str(item.get("webViewLink") or document_url(file_id)).strip(),
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


def normalize_comment(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    quoted = item.get("quotedFileContent") if isinstance(item.get("quotedFileContent"), dict) else {}
    replies = []
    for reply in item.get("replies") or []:
        if not isinstance(reply, dict):
            continue
        reply_author = reply.get("author") if isinstance(reply.get("author"), dict) else {}
        replies.append(
            {
                "reply_id": str(reply.get("id") or "").strip(),
                "content": str(reply.get("content") or "").strip(),
                "author": {
                    "display_name": str(reply_author.get("displayName") or "").strip(),
                    "email": str(reply_author.get("emailAddress") or "").strip(),
                },
                "created_time": str(reply.get("createdTime") or "").strip(),
                "modified_time": str(reply.get("modifiedTime") or "").strip(),
                "deleted": bool(reply.get("deleted")),
            }
        )
    return {
        "comment_id": str(item.get("id") or "").strip(),
        "content": str(item.get("content") or "").strip(),
        "author": {
            "display_name": str(author.get("displayName") or "").strip(),
            "email": str(author.get("emailAddress") or "").strip(),
        },
        "created_time": str(item.get("createdTime") or "").strip(),
        "modified_time": str(item.get("modifiedTime") or "").strip(),
        "resolved": bool(item.get("resolved")),
        "quoted_text": str(quoted.get("value") or "").strip(),
        "replies": replies,
    }


def is_revision_conflict(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return (
        "requiredrevisionid" in msg
        or ("revision" in msg and "failed_precondition" in msg)
        or "409" in msg
        or "precondition" in msg
    )


def _drive_query_literal(value: str) -> str:
    return re.sub(r"['\\]", " ", value).strip()[:120]

