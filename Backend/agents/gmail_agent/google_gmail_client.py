"""Thin Gmail REST API client for the Gmail specialist agent."""

from __future__ import annotations

import base64
import html
import re
from email.message import EmailMessage
from typing import Any

import httpx


_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class GoogleGmailClient:
    def __init__(self, access_token: str, *, timeout_sec: float = 20.0) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout_sec

    async def list_messages(
        self,
        *,
        query: str = "",
        label_ids: list[str] | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max(1, min(int(max_results), 100))}
        if query:
            params["q"] = query
        if label_ids:
            params["labelIds"] = label_ids
        if page_token:
            params["pageToken"] = page_token
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/me/messages",
                headers=self._headers,
                params=params,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return resp.json()

    async def get_message(self, message_id: str, *, fmt: str = "full") -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/me/messages/{message_id}",
                headers=self._headers,
                params={"format": fmt},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Gmail message not found: {message_id}")
            resp.raise_for_status()
            return normalize_message(resp.json())

    async def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if not message_id:
            raise ValueError("message_id is required.")
        if not attachment_id:
            raise ValueError("attachment_id is required.")
        async with httpx.AsyncClient(timeout=max(30.0, self._timeout)) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/me/messages/{message_id}/attachments/{attachment_id}",
                headers=self._headers,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Gmail attachment not found: {attachment_id}")
            resp.raise_for_status()
            payload = resp.json()
        return _decode_body_bytes(str(payload.get("data") or ""))

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/me/threads/{thread_id}",
                headers=self._headers,
                params={"format": "full"},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            if resp.status_code == 404:
                raise ValueError(f"Gmail thread not found: {thread_id}")
            resp.raise_for_status()
            payload = resp.json()
        messages = [normalize_message(item) for item in payload.get("messages") or []]
        messages.sort(key=lambda item: int(item.get("internal_date_ms") or 0))
        return {
            "thread_id": str(payload.get("id") or thread_id),
            "history_id": str(payload.get("historyId") or ""),
            "messages": messages,
            "message_count": len(messages),
            "latest_message": messages[-1] if messages else None,
        }

    async def search_messages(self, *, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        listing = await self.list_messages(query=query, max_results=max_results)
        refs = listing.get("messages") or []
        results: list[dict[str, Any]] = []
        for ref in refs[: max(1, min(max_results, 100))]:
            message_id = str(ref.get("id") or "").strip()
            if not message_id:
                continue
            results.append(await self.get_message(message_id))
        return results

    async def create_draft(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        if not to and not cc and not bcc:
            raise ValueError("At least one recipient is required to create a Gmail draft.")
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        async with httpx.AsyncClient(timeout=max(30.0, self._timeout)) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/users/me/drafts",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return resp.json()

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        normalized_id = str(draft_id or "").strip()
        if not normalized_id:
            raise ValueError("Gmail draft_id is required to send a draft.")
        async with httpx.AsyncClient(timeout=max(30.0, self._timeout)) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/users/me/drafts/{normalized_id}/send",
                headers={**self._headers, "Content-Type": "application/json"},
                json={},
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return resp.json()

    async def modify_message(
        self,
        message_id: str,
        *,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        body = {
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/users/me/messages/{message_id}/modify",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return normalize_message(resp.json())

    async def start_watch(
        self,
        *,
        topic_name: str,
        label_ids: list[str] | None = None,
        label_filter_behavior: str = "INCLUDE",
    ) -> dict[str, Any]:
        if not topic_name:
            raise ValueError("Gmail watch topic_name is required.")
        body: dict[str, Any] = {
            "topicName": topic_name,
            "labelFilterBehavior": label_filter_behavior,
        }
        if label_ids:
            body["labelIds"] = label_ids
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/users/me/watch",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return resp.json()

    async def stop_watch(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{_GMAIL_BASE}/users/me/stop",
                headers=self._headers,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return {"status": "stopped"}

    async def list_history(
        self,
        *,
        start_history_id: str,
        history_types: list[str] | None = None,
        label_id: str | None = None,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not start_history_id:
            raise ValueError("start_history_id is required.")
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": max(1, min(max_results, 500)),
        }
        for item in history_types or ["messageAdded", "labelAdded", "labelRemoved"]:
            params.setdefault("historyTypes", [])
            params["historyTypes"].append(item)
        if label_id:
            params["labelId"] = label_id
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/me/history",
                headers=self._headers,
                params=params,
            )
            if resp.status_code == 401:
                raise PermissionError("Google access token expired.")
            resp.raise_for_status()
            return resp.json()


def normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = _headers_map(payload.get("headers") or [])
    body_text = extract_text_from_payload(payload)
    attachments = extract_attachments_from_payload(payload)
    snippet = str(raw.get("snippet") or "").strip()
    if not snippet and body_text:
        snippet = _SPACE_RE.sub(" ", body_text).strip()[:220]
    return {
        "message_id": str(raw.get("id") or "").strip(),
        "thread_id": str(raw.get("threadId") or "").strip(),
        "history_id": str(raw.get("historyId") or "").strip(),
        "label_ids": [str(item) for item in (raw.get("labelIds") or [])],
        "internal_date_ms": str(raw.get("internalDate") or "").strip(),
        "snippet": snippet,
        "headers": headers,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id_header": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "body_text": body_text,
        "attachments": attachments,
        "has_attachments": bool(attachments),
    }


def extract_email_address(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"<([^<>@\s]+@[^<>\s]+)>", text)
    if match:
        return match.group(1).strip().lower()
    match = re.search(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", text, re.I)
    return match.group(1).strip().lower() if match else text.lower()


def extract_domain(email_address: str) -> str:
    value = extract_email_address(email_address)
    return value.split("@", 1)[1].lower() if "@" in value else ""


def compact_message_for_llm(message: dict[str, Any], *, max_body_chars: int = 1200) -> dict[str, Any]:
    body = str(message.get("body_text") or "")
    return {
        "message_id": message.get("message_id"),
        "thread_id": message.get("thread_id"),
        "from": message.get("from"),
        "from_email": extract_email_address(str(message.get("from") or "")),
        "to": message.get("to"),
        "subject": message.get("subject"),
        "date": message.get("date"),
        "label_ids": message.get("label_ids") or [],
        "snippet": message.get("snippet"),
        "body_preview": body[:max_body_chars],
        "attachments": [
            {
                "attachment_id": item.get("attachment_id"),
                "filename": item.get("filename"),
                "mime_type": item.get("mime_type"),
                "size": item.get("size"),
                "disposition": item.get("disposition"),
            }
            for item in (message.get("attachments") or [])
            if isinstance(item, dict)
        ],
        "has_attachments": bool(message.get("attachments")),
    }


def _headers_map(headers: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers:
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name:
            result[name] = value
    return result


def extract_text_from_payload(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    _walk_payload(payload, parts)
    text = "\n\n".join(part for part in parts if part.strip())
    return _SPACE_RE.sub(" ", text).strip()


def extract_attachments_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    _walk_attachments(payload, attachments)
    return attachments


def _walk_payload(payload: dict[str, Any], parts: list[str]) -> None:
    mime_type = str(payload.get("mimeType") or "").lower()
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    data = str(body.get("data") or "")
    if data and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_body(data)
        if mime_type == "text/html":
            decoded = html.unescape(_TAG_RE.sub(" ", decoded))
        if decoded.strip():
            parts.append(decoded.strip())
    for child in payload.get("parts") or []:
        if isinstance(child, dict):
            _walk_payload(child, parts)


def _walk_attachments(payload: dict[str, Any], attachments: list[dict[str, Any]]) -> None:
    mime_type = str(payload.get("mimeType") or "").strip()
    filename = str(payload.get("filename") or "").strip()
    part_id = str(payload.get("partId") or "").strip()
    headers = _headers_map(payload.get("headers") or [])
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    attachment_id = str(body.get("attachmentId") or "").strip()
    size = body.get("size")
    content_disposition = headers.get("content-disposition", "")
    is_attachment = bool(filename and attachment_id)
    if is_attachment:
        disposition = "inline" if "inline" in content_disposition.lower() else "attachment"
        attachments.append(
            {
                "attachment_id": attachment_id,
                "filename": filename,
                "mime_type": mime_type or "application/octet-stream",
                "size": size,
                "part_id": part_id,
                "content_id": headers.get("content-id", ""),
                "disposition": disposition,
            }
        )
    for child in payload.get("parts") or []:
        if isinstance(child, dict):
            _walk_attachments(child, attachments)


def _decode_body(data: str) -> str:
    try:
        return _decode_body_bytes(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _decode_body_bytes(data: str) -> bytes:
    padded = data + ("=" * ((4 - len(data) % 4) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return b""
