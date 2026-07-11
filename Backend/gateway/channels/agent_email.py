from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from shared import (
    CosmicMailClient,
    CosmicMailClientError,
    render_markdown_email_html,
    render_markdown_email_text,
)

from .base import ChannelAdapter, ChannelUnavailableError, MessageCallback, PermanentDeliveryError


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


_PROCESS_NARRATION_LINE_RE = re.compile(
    r"(?im)^(?:let me|still over|the agent |now i |good —|good -|"
    r"draft is prepared|goal field|now i understand|the draft is prepared|"
    r"i'll delegate|i will delegate|passing everything together|"
    r"try one more approach|usage hint says)\b"
)
_PROCESS_NARRATION_MARKERS = (
    "draft_id",
    "draft_seed",
    "input schema",
    "delegate again",
    "explicit send instruction",
    "tool call",
    "correct input format",
)


def looks_like_email_process_narration(text: str) -> bool:
    """Detect internal tool-loop monologue that must never be emailed."""
    body = str(text or "").strip()
    if not body:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    narration_lines = sum(1 for line in lines if _PROCESS_NARRATION_LINE_RE.search(line))
    marker_hits = sum(1 for marker in _PROCESS_NARRATION_MARKERS if marker in body.lower())
    let_me_hits = len(re.findall(r"(?i)\blet me\b", body))
    if narration_lines >= 3:
        return True
    if let_me_hits >= 3 and narration_lines >= 2:
        return True
    if marker_hits >= 2 and narration_lines >= 1:
        return True
    if let_me_hits >= 5:
        return True
    return False


def _normalize_hex_signature(raw: str) -> str:
    text = str(raw or "").strip()
    if "=" in text:
        _, _, text = text.partition("=")
    return text.strip().lower()


def _contact_name(contact: Any) -> str | None:
    if isinstance(contact, dict):
        name = _safe_text(contact.get("name"))
        return name or None
    return None


def _contact_email(contact: Any) -> str | None:
    if isinstance(contact, dict):
        for key in ("email", "address", "mail"):
            value = _safe_text(contact.get(key))
            if value:
                return value
    if isinstance(contact, str):
        value = _safe_text(contact)
        return value or None
    return None


def _normalize_contact_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    contacts: list[dict[str, Any]] = []
    for item in value:
        email = _contact_email(item)
        if not email:
            continue
        contacts.append({"email": email, "name": _contact_name(item)})
    return contacts


def _pick_default_mailbox(mailboxes: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [mailbox for mailbox in mailboxes if _safe_text(mailbox.get("status")).lower() == "active"]
    pool = active or mailboxes
    return pool[0] if pool else None


def _delivery_message_id(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for source in (
        payload,
        payload.get("message") if isinstance(payload.get("message"), dict) else None,
        payload.get("draft") if isinstance(payload.get("draft"), dict) else None,
    ):
        if not isinstance(source, dict):
            continue
        for key in ("id", "message_id", "sent_message_id"):
            value = _safe_text(source.get(key))
            if value:
                return value
    return None


def _normalize_email_delivery(
    *,
    payload: dict[str, Any] | None,
    fallback_thread_id: str | None = None,
    fallback_draft_id: str | None = None,
) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    queued_for_approval = bool(body.get("queued_for_approval"))
    approval_id = _safe_text(body.get("approval_id")) or None
    status = "queued_for_approval" if queued_for_approval or approval_id else "sent"
    thread = body.get("thread") if isinstance(body.get("thread"), dict) else {}
    draft = body.get("draft") if isinstance(body.get("draft"), dict) else {}
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    thread_id = (
        _safe_text(thread.get("id"))
        or _safe_text(body.get("thread_id"))
        or _safe_text(message.get("thread_id"))
        or _safe_text(draft.get("thread_id"))
        or _safe_text(fallback_thread_id)
        or None
    )
    draft_id = _safe_text(draft.get("id")) or _safe_text(body.get("draft_id")) or _safe_text(fallback_draft_id) or None
    message_id = _delivery_message_id(body)
    return {
        "status": status,
        "queued_for_approval": queued_for_approval,
        "approval_id": approval_id,
        "thread_id": thread_id,
        "draft_id": draft_id,
        "message_id": message_id,
    }


class AgentEmailAdapter(ChannelAdapter):
    platform = "agent-email"

    def __init__(
        self,
        *,
        cosmic_mail_base_url: str,
        cosmic_mail_api_token: str,
        timeout_sec: float = 20.0,
        primary_mailbox_address: str = "",
        webhook_secret: str = "",
        webhook_signature_header: str = "X-Cosmic-Mail-Signature",
    ) -> None:
        self.client = CosmicMailClient(
            base_url=cosmic_mail_base_url,
            api_token=cosmic_mail_api_token,
            timeout_sec=timeout_sec,
        )
        self.primary_mailbox_address = _safe_text(primary_mailbox_address)
        self.webhook_secret = _safe_text(webhook_secret)
        self.webhook_signature_header = _safe_text(webhook_signature_header) or "X-Cosmic-Mail-Signature"
        self._inbound_callback: MessageCallback | None = None
        self._auth_context: dict[str, Any] | None = None

    async def start(self) -> None:
        self._auth_context = await self.client.get_auth_context()
        if self.primary_mailbox_address:
            await self.client.resolve_mailbox(mailbox_address=self.primary_mailbox_address)

    async def stop(self) -> None:
        await self.client.aclose()

    async def on_message(self, callback: MessageCallback) -> None:
        self._inbound_callback = callback

    def channel_id(self, platform_context: dict[str, Any]) -> str:
        mailbox_address = _safe_text(platform_context.get("mailbox_address")) or self.primary_mailbox_address or "default"
        return f"{self.platform}:{mailbox_address}"

    @staticmethod
    def build_thread_session_id(*, mailbox_id: str | None = None, mailbox_address: str | None = None, thread_id: str) -> str:
        normalized_thread_id = _safe_text(thread_id)
        if not normalized_thread_id:
            raise ValueError("thread_id is required")
        mailbox_key = _safe_text(mailbox_address).casefold() or _safe_text(mailbox_id) or "default"
        return f"email-thread:{mailbox_key}:{normalized_thread_id}"

    def verify_webhook_signature(self, headers: dict[str, str] | Any, body: bytes) -> None:
        if not self.webhook_secret:
            return
        header_name = self.webhook_signature_header
        header_value = ""
        if isinstance(headers, dict):
            header_value = _safe_text(headers.get(header_name))
        else:
            try:
                header_value = _safe_text(headers.get(header_name))
            except Exception:
                header_value = ""
        if not header_value:
            raise PermissionError("Missing Cosmic Mail webhook signature.")
        expected = hmac.new(self.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(_normalize_hex_signature(header_value), expected):
            raise PermissionError("Invalid Cosmic Mail webhook signature.")

    def normalize_message(self, raw_webhook: Any) -> dict[str, Any]:
        if not isinstance(raw_webhook, dict):
            raise TypeError("Cosmic Mail webhook payload must be a JSON object.")

        raw_message = raw_webhook.get("message")
        message = raw_message if isinstance(raw_message, dict) else raw_webhook
        if not isinstance(message, dict):
            raise TypeError("Cosmic Mail webhook payload is missing message context.")

        mailbox = raw_webhook.get("mailbox")
        mailbox_dict = mailbox if isinstance(mailbox, dict) else {}
        thread = raw_webhook.get("thread")
        thread_dict = thread if isinstance(thread, dict) else {}

        mailbox_id = _safe_text(raw_webhook.get("mailbox_id")) or _safe_text(mailbox_dict.get("id"))
        mailbox_address = (
            _safe_text(raw_webhook.get("mailbox_address"))
            or _safe_text(mailbox_dict.get("address"))
            or _safe_text(mailbox_dict.get("email"))
            or self.primary_mailbox_address
        )
        message_id = _safe_text(message.get("id")) or _safe_text(raw_webhook.get("message_id"))
        thread_id = _safe_text(message.get("thread_id")) or _safe_text(thread_dict.get("id")) or _safe_text(raw_webhook.get("thread_id"))
        if not message_id or not thread_id:
            raise ValueError("Cosmic Mail webhook payload is missing message_id or thread_id.")

        attachments = self._normalize_attachments(message.get("attachments"), message_id=message_id)
        from_contacts = _normalize_contact_list(message.get("from_recipients"))
        if not from_contacts:
            from_address = _safe_text(message.get("from_address"))
            if from_address:
                from_contacts = [
                    {
                        "email": from_address,
                        "name": _safe_text(message.get("from_name")) or None,
                    }
                ]
        to_contacts = _normalize_contact_list(message.get("to_recipients"))
        cc_contacts = _normalize_contact_list(message.get("cc_recipients"))
        bcc_contacts = _normalize_contact_list(message.get("bcc_recipients"))

        normalized = {
            "content": self._build_content_summary(message, from_contacts=from_contacts),
            "session_id": self.build_thread_session_id(
                mailbox_id=mailbox_id,
                mailbox_address=mailbox_address,
                thread_id=thread_id,
            ),
            "channel": f"agent-email:{mailbox_address or mailbox_id or 'default'}",
            "route_override": "opus",
            "metadata": {
                "platform": "agent-email",
                "message_id": message_id,
                "thread_id": thread_id,
                "mailbox_id": mailbox_id,
                "mailbox_address": mailbox_address,
                "subject": _safe_text(message.get("subject")) or _safe_text(thread_dict.get("subject")),
                "direction": _safe_text(message.get("direction")) or "inbound",
                "from_address": from_contacts[0]["email"] if from_contacts else None,
                "from_name": from_contacts[0]["name"] if from_contacts else None,
                "received_at": _safe_text(message.get("received_at")) or None,
                "sent_at": _safe_text(message.get("sent_at")) or None,
                "internet_message_id": _safe_text(message.get("internet_message_id")) or None,
                "attachments": attachments,
                "has_attachments": bool(attachments),
                "attachment_count": len(attachments),
                "to_recipients": to_contacts,
                "cc_recipients": cc_contacts,
                "bcc_recipients": bcc_contacts,
                "session_scope": "email_thread",
                "rollover_exempt": True,
            },
        }
        return normalized

    def normalize_approval_notification(self, raw_webhook: Any) -> dict[str, Any]:
        if not isinstance(raw_webhook, dict):
            raise TypeError("Cosmic Mail approval webhook payload must be a JSON object.")

        approval = raw_webhook.get("approval")
        approval_dict = approval if isinstance(approval, dict) else raw_webhook
        draft = raw_webhook.get("draft")
        draft_dict = draft if isinstance(draft, dict) else {}

        approval_id = _safe_text(approval_dict.get("id")) or _safe_text(raw_webhook.get("approval_id"))
        if not approval_id:
            raise ValueError("Cosmic Mail approval webhook payload is missing approval_id.")

        recipients = _normalize_contact_list(draft_dict.get("to_recipients"))
        cc_recipients = _normalize_contact_list(draft_dict.get("cc_recipients"))
        mailbox = raw_webhook.get("mailbox")
        mailbox_dict = mailbox if isinstance(mailbox, dict) else {}
        mailbox_id = (
            _safe_text(approval_dict.get("mailbox_id"))
            or _safe_text(draft_dict.get("mailbox_id"))
            or _safe_text(raw_webhook.get("mailbox_id"))
            or _safe_text(mailbox_dict.get("id"))
        )
        mailbox_address = (
            _safe_text(raw_webhook.get("mailbox_address"))
            or _safe_text(mailbox_dict.get("address"))
            or _safe_text(mailbox_dict.get("email"))
            or self.primary_mailbox_address
        )
        subject = (
            _safe_text(draft_dict.get("subject"))
            or _safe_text(approval_dict.get("subject"))
            or "Email approval required"
        )
        snippet = (
            _safe_text(draft_dict.get("text_body"))
            or re.sub(r"<[^>]+>", " ", _safe_text(draft_dict.get("html_body")))
        )
        snippet = re.sub(r"\s+", " ", snippet).strip()
        body_text = (
            str(draft_dict.get("text_body"))
            if isinstance(draft_dict.get("text_body"), str)
            else ""
        )

        return {
            "kind": "approval",
            "event": _safe_text(raw_webhook.get("event")) or "approval.created",
            "approval_id": approval_id,
            "status": _safe_text(approval_dict.get("status")) or "pending",
            "organization_id": _safe_text(raw_webhook.get("organization_id")) or None,
            "agent_id": _safe_text(approval_dict.get("agent_id")) or None,
            "mailbox_id": mailbox_id or None,
            "mailbox_address": mailbox_address or None,
            "draft_id": _safe_text(approval_dict.get("draft_id")) or _safe_text(draft_dict.get("id")) or None,
            "subject": subject,
            "recipients": recipients,
            "cc_recipients": cc_recipients,
            "body_text": body_text,
            "recipient_summary": ", ".join(
                contact["email"] for contact in recipients if contact.get("email")
            ),
            "snippet": snippet,
            "created_at": _safe_text(approval_dict.get("created_at"))
            or _safe_text(raw_webhook.get("timestamp"))
            or None,
        }

    async def send(self, message: dict[str, Any], channel: str | None = None) -> None:
        if not self._is_sendable_event(message):
            message["email_delivery"] = {
                "status": "skipped",
                "reason": "non_sendable_event",
            }
            message["email_delivery_status"] = "skipped"
            return
        target_channel = _safe_text(channel) or _safe_text(message.get("channel"))
        raw_body = self._build_text_body(message)
        text_body = self._render_plain_text_body(raw_body)
        html_body = self._build_html_body(raw_body)
        if not text_body.strip():
            message["email_delivery"] = {
                "status": "suppressed",
                "reason": "empty_body",
            }
            message["email_delivery_status"] = "suppressed"
            return
        if looks_like_email_process_narration(text_body):
            message["email_delivery"] = {
                "status": "suppressed",
                "reason": "process_narration",
            }
            message["email_delivery_status"] = "suppressed"
            return

        def record_delivery(
            delivery: dict[str, Any],
            *,
            subject: str,
            recipients: list[dict[str, Any]],
            cc_recipients: list[dict[str, Any]] | None = None,
            mailbox_address: str | None = None,
        ) -> None:
            message["email_delivery"] = delivery
            message["email_delivery_status"] = delivery["status"]
            message["email_queued_for_approval"] = bool(delivery.get("queued_for_approval"))
            if delivery.get("approval_id"):
                message["email_approval_id"] = delivery["approval_id"]
            if delivery.get("message_id"):
                message["email_sent_message_id"] = delivery["message_id"]
            if delivery.get("approval_id"):
                message["email_approval"] = {
                    "approval_id": delivery["approval_id"],
                    "status": "pending",
                    "draft_id": delivery.get("draft_id"),
                    "thread_id": delivery.get("thread_id"),
                    "subject": subject,
                    "recipients": recipients,
                    "cc_recipients": cc_recipients or [],
                    "body_text": text_body,
                    "body_preview": re.sub(r"\s+", " ", text_body).strip()[:700],
                    "mailbox_address": _safe_text(mailbox_address) or None,
                }

        reply_context = self._thread_reply_context(message)
        if reply_context is not None:
            if not reply_context["eligible"]:
                message["email_delivery"] = {
                    "status": "suppressed",
                    "reason": "thread_reply_not_eligible",
                    "thread_id": reply_context["thread_id"],
                }
                message["email_delivery_status"] = "suppressed"
                return
            mailbox = await self.client.resolve_mailbox(
                mailbox_id=reply_context["mailbox_id"],
                mailbox_address=reply_context["mailbox_address"],
            )
            reply_payload: dict[str, Any] = {
                "mailbox_id": mailbox["id"],
                "text_body": text_body,
                "html_body": html_body,
            }
            to_recipients = self._normalize_recipients(message, mailbox_address=reply_context["mailbox_address"] or "")
            if to_recipients:
                reply_payload["to_recipients"] = to_recipients
            cc_recipients = self._normalize_contact_field(message, "cc_recipients", "cc")
            if cc_recipients:
                reply_payload["cc_recipients"] = cc_recipients
            reply_result = await self.client.reply_to_thread(reply_context["thread_id"], reply_payload)
            delivery = _normalize_email_delivery(payload=reply_result, fallback_thread_id=reply_context["thread_id"])
            record_delivery(
                delivery,
                subject=self._build_subject(message),
                recipients=to_recipients,
                cc_recipients=cc_recipients,
                mailbox_address=_safe_text(mailbox.get("address"))
                or reply_context["mailbox_address"],
            )
            return

        try:
            mailbox_address = self._extract_mailbox_address(target_channel)
        except ChannelUnavailableError:
            fallback_mailbox = _pick_default_mailbox(await self.client.list_mailboxes())
            if not fallback_mailbox:
                raise
            mailbox_address = _safe_text(fallback_mailbox.get("address"))
        mailbox = await self.client.resolve_mailbox(mailbox_address=mailbox_address)
        recipients = self._normalize_recipients(message, mailbox_address=mailbox_address)
        subject = self._build_subject(message)
        draft = await self.client.create_draft(
            {
                "mailbox_id": mailbox["id"],
                "subject": subject,
                "to_recipients": recipients,
                "text_body": text_body,
                "html_body": html_body,
            }
        )
        draft_id = _safe_text(draft.get("id"))
        if not draft_id:
            raise PermanentDeliveryError("Cosmic Mail draft creation did not return an id.")
        send_result = await self.client.send_draft(draft_id)
        delivery = _normalize_email_delivery(payload=send_result, fallback_draft_id=draft_id)
        record_delivery(
            delivery,
            subject=subject,
            recipients=recipients,
            mailbox_address=_safe_text(mailbox.get("address")) or mailbox_address,
        )

    async def get_status(self) -> dict[str, Any]:
        auth_context = await self.client.get_auth_context()
        mailboxes = await self.client.list_mailboxes()
        primary_mailbox = None
        if self.primary_mailbox_address:
            try:
                primary_mailbox = await self.client.resolve_mailbox(mailbox_address=self.primary_mailbox_address)
            except CosmicMailClientError:
                primary_mailbox = None
        if primary_mailbox is None:
            primary_mailbox = _pick_default_mailbox(mailboxes)
        return {
            "connected": True,
            "base_url": self.client.base_url,
            "primary_mailbox_address": self.primary_mailbox_address or _safe_text(primary_mailbox.get("address")) or None,
            "primary_mailbox": primary_mailbox,
            "mailbox_count": len(mailboxes),
            "auth_context": auth_context,
        }

    def _extract_mailbox_address(self, channel: str) -> str:
        normalized = _safe_text(channel)
        if not normalized or normalized == "agent-email":
            if self.primary_mailbox_address:
                return self.primary_mailbox_address
            raise ChannelUnavailableError("No primary Agent Email mailbox is configured.")
        _, _, tail = normalized.partition(":")
        mailbox_address = tail.strip()
        if mailbox_address:
            return mailbox_address
        if self.primary_mailbox_address:
            return self.primary_mailbox_address
        raise ChannelUnavailableError("Agent Email channel is missing a mailbox address.")

    def _thread_reply_context(self, message: dict[str, Any]) -> dict[str, Any] | None:
        thread_id = _safe_text(message.get("thread_id"))
        if not thread_id:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            thread_id = _safe_text(metadata.get("thread_id"))
        if not thread_id:
            return None

        event_type = _safe_text(message.get("type"))
        trusted_sender = bool(message.get("trusted_sender"))
        auto_reply_sent = bool(message.get("email_auto_reply_sent"))
        explicit_eligible = message.get("email_thread_reply_eligible")
        mailbox_id = _safe_text(message.get("mailbox_id"))
        mailbox_address = _safe_text(message.get("mailbox_address"))
        if not mailbox_address:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            mailbox_address = _safe_text(metadata.get("mailbox_address"))
        if not mailbox_id:
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            mailbox_id = _safe_text(metadata.get("mailbox_id"))

        return {
            "thread_id": thread_id,
            "mailbox_id": mailbox_id or None,
            "mailbox_address": mailbox_address or None,
            "eligible": (
                bool(explicit_eligible)
                if explicit_eligible is not None
                else event_type == "response.complete" and trusted_sender and not auto_reply_sent
            ),
        }

    def _normalize_contact_field(self, message: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            recipients = _normalize_contact_list(message.get(key))
            if recipients:
                return recipients
        return []

    def _normalize_recipients(self, message: dict[str, Any], *, mailbox_address: str) -> list[dict[str, Any]]:
        for key in ("to_recipients", "to"):
            recipients = _normalize_contact_list(message.get(key))
            if recipients:
                return recipients
        for key in ("recipient_email", "email_to"):
            candidate = _safe_text(message.get(key))
            if candidate:
                return [{"email": candidate, "name": None}]
        fallback = _safe_text(mailbox_address)
        if fallback:
            return [{"email": fallback, "name": None}]
        return []

    def _build_subject(self, message: dict[str, Any]) -> str:
        explicit = _safe_text(message.get("subject"))
        if explicit:
            return explicit[:200]
        event_type = _safe_text(message.get("type"))
        if event_type == "response.complete":
            return "COSMIC update"
        if event_type == "task.failed":
            return "COSMIC task failed"
        return "COSMIC notification"

    def _build_text_body(self, message: dict[str, Any]) -> str:
        content = _safe_text(message.get("content"))
        if content:
            return content
        fallback = _safe_text(message.get("message"))
        if fallback:
            return fallback
        summary_parts = [
            f"Event: {_safe_text(message.get('type')) or 'notification'}",
            f"Request: {_safe_text(message.get('request_id')) or 'n/a'}",
            f"Task: {_safe_text(message.get('task_id')) or 'n/a'}",
        ]
        return "\n".join(summary_parts)

    def _render_plain_text_body(self, markdown_text: str) -> str:
        return render_markdown_email_text(markdown_text)

    def _is_sendable_event(self, message: dict[str, Any]) -> bool:
        event_type = _safe_text(message.get("type"))
        if not event_type:
            return True
        return event_type in {
            "response.complete",
            "task.failed",
            "task.cancelled",
            "error",
        }

    def _build_html_body(self, text_body: str) -> str:
        return render_markdown_email_html(text_body)

    def _normalize_attachments(self, raw: Any, *, message_id: str) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        attachments: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            attachment_id = _safe_text(item.get("id")) or _safe_text(item.get("attachment_id"))
            if not attachment_id:
                continue
            attachments.append(
                {
                    "id": attachment_id,
                    "filename": _safe_text(item.get("filename")) or _safe_text(item.get("name")) or f"{attachment_id}.bin",
                    "mime_type": _safe_text(item.get("content_type")) or _safe_text(item.get("mime_type")) or "application/octet-stream",
                    "size_bytes": int(item.get("size_bytes") or item.get("size") or 0),
                    "email_attachment_id": attachment_id,
                    "source_message_id": message_id,
                }
            )
        return attachments

    def _build_content_summary(self, message: dict[str, Any], *, from_contacts: list[dict[str, Any]]) -> str:
        subject = _safe_text(message.get("subject")) or "(no subject)"
        from_name = from_contacts[0].get("name") if from_contacts else None
        from_address = from_contacts[0].get("email") if from_contacts else None
        sender = " ".join(part for part in [from_name, f"<{from_address}>" if from_address else None] if part).strip()
        body = (
            _safe_text(message.get("text_body"))
            or _safe_text(message.get("body_text"))
            or _safe_text(message.get("snippet"))
            or _safe_text(message.get("preview_text"))
            or _safe_text(message.get("body_preview"))
            or _safe_text(message.get("body"))
        )
        excerpt = body[:800]
        lines = [f"Email subject: {subject}"]
        if sender:
            lines.insert(0, f"Email from: {sender}")
        if excerpt:
            lines.append("")
            lines.append(excerpt)
        return "\n".join(lines).strip()
