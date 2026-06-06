from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

import httpx


def normalize_cosmic_mail_base_url(raw: str) -> str:
    return str(raw or "").strip().rstrip("/")


class CosmicMailClientError(RuntimeError):
    def __init__(self, *, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(slots=True)
class CosmicMailClient:
    base_url: str
    api_token: str
    timeout_sec: float = 20.0
    client: httpx.AsyncClient | None = None
    _owns_client: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = normalize_cosmic_mail_base_url(self.base_url)
        self._owns_client = self.client is None
        if self.client is None:
            timeout = httpx.Timeout(self.timeout_sec, connect=min(self.timeout_sec, 10.0))
            self.client = httpx.AsyncClient(timeout=timeout, http2=True, follow_redirects=True)

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()

    async def get_auth_context(self) -> dict[str, Any]:
        return await self._request_json("GET", "/v1/system/auth-context")

    async def create_organization_api_key(
        self,
        organization_id: str,
        *,
        name: str,
    ) -> dict[str, Any]:
        normalized = str(organization_id or "").strip()
        key_name = str(name or "").strip()
        if not normalized:
            raise ValueError("organization_id is required")
        if not key_name:
            raise ValueError("name is required")
        return await self._request_json(
            "POST",
            f"/v1/organizations/{normalized}/api-keys",
            json_body={"name": key_name},
        )

    async def list_mailboxes(self, *, page: int = 1, per_page: int = 200) -> list[dict[str, Any]]:
        payload = await self._request_payload(
            "GET",
            "/v1/mailboxes",
            params={"page": max(1, int(page)), "per_page": max(1, min(int(per_page), 200))},
        )
        return self._extract_items(payload)

    async def resolve_mailbox(
        self,
        *,
        mailbox_id: str | None = None,
        mailbox_address: str | None = None,
    ) -> dict[str, Any]:
        normalized_id = str(mailbox_id or "").strip()
        normalized_address = str(mailbox_address or "").strip().casefold()
        if not normalized_id and not normalized_address:
            raise ValueError("mailbox_id or mailbox_address is required")
        mailboxes = await self.list_mailboxes()
        for mailbox in mailboxes:
            if normalized_id and str(mailbox.get("id") or "").strip() == normalized_id:
                return mailbox
            if normalized_address and str(mailbox.get("address") or "").strip().casefold() == normalized_address:
                return mailbox
        raise CosmicMailClientError(
            status_code=404,
            message="Mailbox not found in Cosmic Mail.",
        )

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        return await self._request_json("GET", f"/v1/threads/{normalized}")

    async def get_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        payload = await self._request_payload("GET", f"/v1/threads/{normalized}/messages")
        return self._extract_items(payload)

    async def list_threads(
        self,
        *,
        mailbox_id: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": max(1, int(page)),
            "per_page": max(1, min(int(per_page), 200)),
        }
        if mailbox_id:
            params["mailbox_id"] = str(mailbox_id).strip()
        payload = await self._request_payload("GET", "/v1/threads", params=params)
        return self._extract_items(payload)

    async def search_threads(
        self,
        *,
        query: str,
        mailbox_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": str(query or "").strip(),
            "page": max(1, int(page)),
            "per_page": max(1, min(int(per_page), 200)),
        }
        if mailbox_id:
            params["mailbox_id"] = str(mailbox_id).strip()
        if date_from:
            params["date_from"] = str(date_from).strip()
        if date_to:
            params["date_to"] = str(date_to).strip()
        payload = await self._request_payload("GET", "/v1/search/threads", params=params)
        return self._extract_items(payload)

    async def search_messages(
        self,
        *,
        query: str,
        mailbox_id: str | None = None,
        direction: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": str(query or "").strip(),
            "page": max(1, int(page)),
            "per_page": max(1, min(int(per_page), 200)),
        }
        if mailbox_id:
            params["mailbox_id"] = str(mailbox_id).strip()
        if direction:
            params["direction"] = str(direction).strip()
        if date_from:
            params["date_from"] = str(date_from).strip()
        if date_to:
            params["date_to"] = str(date_to).strip()
        payload = await self._request_payload("GET", "/v1/search/messages", params=params)
        return self._extract_items(payload)

    async def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/drafts", json_body=payload)

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        normalized = str(approval_id or "").strip()
        if not normalized:
            raise ValueError("approval_id is required")
        return await self._request_json("GET", f"/approvals/{normalized}")

    async def update_approval_draft(
        self,
        approval_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = str(approval_id or "").strip()
        if not normalized:
            raise ValueError("approval_id is required")
        return await self._request_json(
            "PATCH",
            f"/approvals/{normalized}",
            json_body=payload,
        )

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        normalized = str(draft_id or "").strip()
        if not normalized:
            raise ValueError("draft_id is required")
        return await self._request_json("POST", f"/v1/drafts/{normalized}/send")

    async def approve_approval(self, approval_id: str) -> dict[str, Any]:
        normalized = str(approval_id or "").strip()
        if not normalized:
            raise ValueError("approval_id is required")
        return await self._request_json("POST", f"/approvals/{normalized}/approve")

    async def reject_approval(
        self,
        approval_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(approval_id or "").strip()
        if not normalized:
            raise ValueError("approval_id is required")
        return await self._request_json(
            "POST",
            f"/approvals/{normalized}/reject",
            json_body={"note": str(note or "").strip() or None},
        )

    async def upload_draft_attachment(
        self,
        draft_id: str,
        *,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(draft_id or "").strip()
        safe_filename = str(filename or "").strip()
        if not normalized:
            raise ValueError("draft_id is required")
        if not safe_filename:
            raise ValueError("filename is required")
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ValueError("content is required")
        response = await self._request(
            "POST",
            f"/v1/attachments/drafts/{normalized}",
            files={
                "file": (
                    safe_filename,
                    bytes(content),
                    str(mime_type or "application/octet-stream").strip() or "application/octet-stream",
                )
            },
            content_type=None,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise CosmicMailClientError(status_code=response.status_code, message="Cosmic Mail returned a non-object payload.")
        return payload

    async def reply_to_thread(self, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        return await self._request_json("POST", f"/v1/threads/{normalized}/reply", json_body=payload)

    async def list_message_attachments(self, message_id: str) -> list[dict[str, Any]]:
        normalized = str(message_id or "").strip()
        if not normalized:
            raise ValueError("message_id is required")
        payload = await self._request_payload("GET", f"/v1/messages/{normalized}/attachments")
        return self._extract_items(payload)

    async def download_attachment(self, attachment_id: str) -> tuple[bytes, str | None, str | None]:
        normalized = str(attachment_id or "").strip()
        if not normalized:
            raise ValueError("attachment_id is required")
        response = await self._request(
            "GET",
            f"/v1/attachments/{normalized}/download",
        )
        filename = self._filename_from_response(response)
        return response.content, response.headers.get("content-type"), filename

    async def create_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/v1/webhooks", json_body=payload)

    async def list_webhooks(self) -> list[dict[str, Any]]:
        payload = await self._request_payload("GET", "/v1/webhooks")
        return self._extract_items(payload)

    async def update_webhook(self, webhook_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = str(webhook_id or "").strip()
        if not normalized:
            raise ValueError("webhook_id is required")
        return await self._request_json("PATCH", f"/v1/webhooks/{normalized}", json_body=payload)

    async def delete_webhook(self, webhook_id: str) -> None:
        normalized = str(webhook_id or "").strip()
        if not normalized:
            raise ValueError("webhook_id is required")
        await self._request("DELETE", f"/v1/webhooks/{normalized}", content_type=None)

    async def replace_trusted_recipients(
        self,
        organization_id: str,
        emails: list[str],
        *,
        note: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cosmic-OS is the source of truth for the trusted-recipients allowlist.

        This PUTs the entire list to Cosmic Mail's per-org endpoint; the server replaces
        whatever it had. Cosmic Mail uses the list to bypass outbound approval gating
        when every recipient on a draft is in the allowlist.
        """
        normalized = str(organization_id or "").strip()
        if not normalized:
            raise ValueError("organization_id is required")
        body: dict[str, Any] = {"emails": list(emails or [])}
        if note is not None:
            body["note"] = note
        payload = await self._request_payload(
            "PUT",
            f"/v1/organizations/{normalized}/trusted-recipients",
            json_body=body,
        )
        return self._extract_items(payload)

    async def _request_payload(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        content_type: str | None = "application/json",
    ) -> Any:
        response = await self._request(
            method,
            path,
            json_body=json_body,
            params=params,
            files=files,
            content_type=content_type,
        )
        return response.json()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        content_type: str | None = "application/json",
    ) -> dict[str, Any]:
        response = await self._request(
            method,
            path,
            json_body=json_body,
            params=params,
            files=files,
            content_type=content_type,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise CosmicMailClientError(status_code=response.status_code, message="Cosmic Mail returned a non-object payload.")
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        content_type: str | None = "application/json",
    ) -> httpx.Response:
        if not self.base_url:
            raise CosmicMailClientError(status_code=None, message="Cosmic Mail base URL is not configured.")
        if not self.api_token:
            raise CosmicMailClientError(status_code=None, message="Cosmic Mail API token is not configured.")
        assert self.client is not None
        try:
            response = await self.client.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                params=params,
                files=files,
                headers=self._headers(content_type=content_type),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CosmicMailClientError(
                status_code=exc.response.status_code,
                message=self._response_error_text(exc.response),
            ) from exc
        except httpx.HTTPError as exc:
            raise CosmicMailClientError(status_code=None, message=f"Cosmic Mail request failed: {exc}") from exc
        return response

    def _headers(self, *, content_type: str | None = "application/json") -> dict[str, str]:
        token = self.api_token.strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-API-Key": token,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        for key in ("items", "results", "mailboxes", "threads", "messages", "attachments", "webhooks"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        if isinstance(payload, dict) and all(isinstance(item, dict) for item in payload.values()):
            return list(payload.values())
        return []

    def _response_error_text(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip() or f"status={response.status_code}"
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return response.text.strip() or f"status={response.status_code}"

    def _filename_from_response(self, response: httpx.Response) -> str | None:
        content_disposition = response.headers.get("content-disposition", "")
        if "filename=" not in content_disposition:
            return None
        _, _, tail = content_disposition.partition("filename=")
        filename = tail.strip().strip('"').strip("'")
        return filename or None
