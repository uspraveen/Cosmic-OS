from __future__ import annotations

import hashlib
import hmac

import pytest

from gateway.channels.agent_email import AgentEmailAdapter


def _sample_webhook() -> dict[str, object]:
    return {
        "mailbox_id": "mbx_123",
        "mailbox_address": "support@example.com",
        "thread": {"id": "thr_123", "subject": "Need help"},
        "message": {
            "id": "msg_123",
            "thread_id": "thr_123",
            "subject": "Need help",
            "direction": "inbound",
            "from_recipients": [{"email": "sender@example.com", "name": "Sender"}],
            "to_recipients": [{"email": "support@example.com", "name": "Support"}],
            "text_body": "Can you help me with the latest invoice?",
            "attachments": [
                {
                    "id": "att_1",
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 2048,
                }
            ],
        },
    }


def test_agent_email_adapter_verifies_signature_and_normalizes_payload() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        webhook_secret="super-secret",
    )
    body = b'{"message":"payload"}'
    signature = hmac.new(b"super-secret", body, hashlib.sha256).hexdigest()

    adapter.verify_webhook_signature({"X-Cosmic-Mail-Signature": signature}, body)

    with pytest.raises(PermissionError):
        adapter.verify_webhook_signature({"X-Cosmic-Mail-Signature": "deadbeef"}, body)

    normalized = adapter.normalize_message(_sample_webhook())
    assert normalized["session_id"] == "email-thread:support@example.com:thr_123"
    assert normalized["channel"] == "agent-email:support@example.com"
    assert normalized["route_override"] == "opus"
    metadata = normalized["metadata"]
    assert metadata["session_scope"] == "email_thread"
    assert metadata["rollover_exempt"] is True
    assert metadata["message_id"] == "msg_123"
    assert metadata["thread_id"] == "thr_123"
    assert metadata["attachment_count"] == 1
    assert metadata["attachments"][0]["filename"] == "invoice.pdf"
    assert "Need help" in normalized["content"]


def test_agent_email_adapter_normalizes_approval_webhook() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    normalized = adapter.normalize_approval_notification(
        {
            "event": "approval.created",
            "timestamp": "2026-05-10T12:00:00Z",
            "organization_id": "org_123",
            "approval": {
                "id": "apr_123",
                "status": "pending",
                "agent_id": "agent_1",
                "mailbox_id": "mbx_123",
                "draft_id": "draft_123",
                "created_at": "2026-05-10T12:00:00Z",
            },
            "draft": {
                "id": "draft_123",
                "subject": "Review needed",
                "to_recipients": [{"email": "owner@example.com"}],
                "text_body": "Please review this outbound reply.",
            },
        }
    )

    assert normalized["kind"] == "approval"
    assert normalized["approval_id"] == "apr_123"
    assert normalized["event"] == "approval.created"
    assert normalized["organization_id"] == "org_123"
    assert normalized["mailbox_address"] == "assistant@example.com"
    assert normalized["recipient_summary"] == "owner@example.com"
    assert normalized["snippet"] == "Please review this outbound reply."


def test_agent_email_adapter_normalizes_current_cosmic_mail_message_payload() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    normalized = adapter.normalize_message(
        {
            "event": "message.received",
            "timestamp": "2026-05-10T12:00:00Z",
            "mailbox_id": "mbx_123",
            "thread_id": "thr_123",
            "message": {
                "id": "msg_123",
                "internet_message_id": "<msg@example.com>",
                "direction": "inbound",
                "subject": "Hello",
                "from_address": "sender@example.com",
                "from_name": "Sender",
                "to_recipients": [{"email": "assistant@example.com"}],
                "preview_text": "This is the webhook preview.",
                "received_at": "2026-05-10T12:00:00Z",
            },
            "thread": {"id": "thr_123", "subject": "Hello"},
        }
    )

    metadata = normalized["metadata"]
    assert normalized["channel"] == "agent-email:assistant@example.com"
    assert metadata["from_address"] == "sender@example.com"
    assert metadata["from_name"] == "Sender"
    assert metadata["received_at"] == "2026-05-10T12:00:00Z"
    assert "This is the webhook preview." in normalized["content"]


@pytest.mark.asyncio
async def test_agent_email_adapter_send_uses_cosmic_mail_draft_send_flow() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.created_payload: dict[str, object] | None = None
            self.sent_draft_id: str | None = None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id is None
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary"}

        async def create_draft(self, payload):
            self.created_payload = payload
            return {"id": "draft_123"}

        async def send_draft(self, draft_id: str):
            self.sent_draft_id = draft_id
            return {"id": "msg_sent"}

    fake_client = FakeClient()
    adapter.client = fake_client  # type: ignore[assignment]

    await adapter.send(
        {
            "subject": "Morning update",
            "content": "Everything is green.",
            "to": [{"email": "owner@example.com", "name": "Owner"}],
        },
        channel="agent-email",
    )

    assert fake_client.created_payload is not None
    assert fake_client.created_payload["mailbox_id"] == "mbx_primary"
    assert fake_client.created_payload["subject"] == "Morning update"
    assert fake_client.created_payload["to_recipients"] == [{"email": "owner@example.com", "name": "Owner"}]
    assert fake_client.created_payload["text_body"] == "Everything is green."
    assert fake_client.created_payload["html_body"] == "<div><p>Everything is green.</p></div>"
    assert fake_client.sent_draft_id == "draft_123"
    message = {
        "subject": "Morning update",
        "content": "Everything is green.",
        "to": [{"email": "owner@example.com", "name": "Owner"}],
    }

    await adapter.send(message, channel="agent-email")

    assert message["email_delivery_status"] == "sent"
    assert message["email_delivery"]["draft_id"] == "draft_123"


@pytest.mark.asyncio
async def test_agent_email_adapter_send_marks_approval_queue_status() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary"}

        async def create_draft(self, payload):
            return {"id": "draft_queued"}

        async def send_draft(self, draft_id: str):
            assert draft_id == "draft_queued"
            return {"queued_for_approval": True, "approval_id": "apr_123", "draft": {"id": "draft_queued"}}

    adapter.client = FakeClient()  # type: ignore[assignment]

    message = {
        "subject": "Pending approval",
        "content": "Needs review.",
        "to": [{"email": "owner@example.com", "name": "Owner"}],
    }

    await adapter.send(message, channel="agent-email")

    assert message["email_delivery_status"] == "queued_for_approval"
    assert message["email_queued_for_approval"] is True
    assert message["email_approval_id"] == "apr_123"
    assert message["email_approval"] == {
        "approval_id": "apr_123",
        "status": "pending",
        "draft_id": "draft_queued",
        "thread_id": None,
        "subject": "Pending approval",
        "recipients": [{"email": "owner@example.com", "name": "Owner"}],
        "cc_recipients": [],
        "body_preview": "Needs review.",
        "mailbox_address": "assistant@example.com",
    }


@pytest.mark.asyncio
async def test_agent_email_adapter_send_replies_in_thread_for_trusted_sender_response() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.replied_thread_id: str | None = None
            self.reply_payload: dict[str, object] | None = None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id == "mbx_support"
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_support"}

        async def reply_to_thread(self, thread_id: str, payload):
            self.replied_thread_id = thread_id
            self.reply_payload = payload
            return {"id": "msg_reply_1"}

        async def create_draft(self, payload):
            raise AssertionError("create_draft should not be used for trusted thread replies")

        async def send_draft(self, draft_id: str):
            raise AssertionError("send_draft should not be used for trusted thread replies")

    fake_client = FakeClient()
    adapter.client = fake_client  # type: ignore[assignment]

    await adapter.send(
        {
            "type": "response.complete",
            "thread_id": "thr_123",
            "mailbox_id": "mbx_support",
            "mailbox_address": "assistant@example.com",
            "content": "I got your reply.",
            "trusted_sender": True,
            "email_thread_reply_eligible": True,
            "to_recipients": [{"email": "owner@example.com", "name": "Owner"}],
        },
        channel="agent-email:assistant@example.com",
    )

    assert fake_client.replied_thread_id == "thr_123"
    assert fake_client.reply_payload == {
        "mailbox_id": "mbx_support",
        "text_body": "I got your reply.",
        "html_body": "<div><p>I got your reply.</p></div>",
        "to_recipients": [{"email": "owner@example.com", "name": "Owner"}],
    }


@pytest.mark.asyncio
async def test_agent_email_adapter_renders_markdown_for_drafts_and_replies() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.created_payload: dict[str, object] | None = None
            self.reply_payload: dict[str, object] | None = None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": mailbox_id or "mbx_primary"}

        async def create_draft(self, payload):
            self.created_payload = payload
            return {"id": "draft_markdown"}

        async def send_draft(self, draft_id: str):
            return {"id": draft_id}

        async def reply_to_thread(self, thread_id: str, payload):
            self.reply_payload = payload
            return {"id": "msg_reply_markdown"}

    fake_client = FakeClient()
    adapter.client = fake_client  # type: ignore[assignment]

    markdown = "## Daily Update\n\n**Status**\n- Green\n- Shipping\n\n[Docs](https://example.com/docs)"

    await adapter.send(
        {
            "subject": "Daily update",
            "content": markdown,
            "to": [{"email": "owner@example.com", "name": "Owner"}],
        },
        channel="agent-email",
    )

    assert fake_client.created_payload is not None
    assert fake_client.created_payload["text_body"] == "Daily Update\n\nStatus\n• Green\n• Shipping\n\nDocs: https://example.com/docs"
    assert "<h2>Daily Update</h2>" in str(fake_client.created_payload["html_body"])
    assert "<strong>Status</strong>" in str(fake_client.created_payload["html_body"])
    assert "<li>Green</li>" in str(fake_client.created_payload["html_body"])
    assert '<a href="https://example.com/docs">Docs</a>' in str(fake_client.created_payload["html_body"])

    await adapter.send(
        {
            "type": "response.complete",
            "thread_id": "thr_markdown",
            "mailbox_id": "mbx_support",
            "mailbox_address": "assistant@example.com",
            "content": markdown,
            "trusted_sender": True,
            "email_thread_reply_eligible": True,
            "to_recipients": [{"email": "owner@example.com", "name": "Owner"}],
        },
        channel="agent-email:assistant@example.com",
    )

    assert fake_client.reply_payload is not None
    assert fake_client.reply_payload["text_body"] == "Daily Update\n\nStatus\n• Green\n• Shipping\n\nDocs: https://example.com/docs"
    assert "<h2>Daily Update</h2>" in str(fake_client.reply_payload["html_body"])


@pytest.mark.asyncio
async def test_agent_email_adapter_send_skips_untrusted_thread_responses() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            raise AssertionError("resolve_mailbox should not be called when thread delivery is not eligible")

        async def reply_to_thread(self, thread_id: str, payload):
            raise AssertionError("reply_to_thread should not be used when trusted sender policy is not satisfied")

        async def create_draft(self, payload):
            raise AssertionError("create_draft should not be used for inbound thread follow-ups")

        async def send_draft(self, draft_id: str):
            raise AssertionError("send_draft should not be used for inbound thread follow-ups")

    adapter.client = FakeClient()  # type: ignore[assignment]

    await adapter.send(
        {
            "type": "response.complete",
            "thread_id": "thr_999",
            "mailbox_address": "assistant@example.com",
            "content": "This should stay internal until external sender policy exists.",
            "trusted_sender": False,
        },
        channel="agent-email:assistant@example.com",
    )


@pytest.mark.asyncio
async def test_agent_email_adapter_send_ignores_internal_stream_events() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            raise AssertionError("resolve_mailbox should not be called for internal stream events")

        async def create_draft(self, payload):
            raise AssertionError("create_draft should not be called for internal stream events")

        async def send_draft(self, draft_id: str):
            raise AssertionError("send_draft should not be called for internal stream events")

        async def reply_to_thread(self, thread_id: str, payload):
            raise AssertionError("reply_to_thread should not be called for internal stream events")

    adapter.client = FakeClient()  # type: ignore[assignment]

    for event_type in ("route_result", "task.created", "tool.call", "response.chunk", "response.thinking.chunk"):
        await adapter.send(
            {
                "type": event_type,
                "request_id": "req_internal",
                "task_id": "tsk_internal",
                "channel": "agent-email:assistant@example.com",
            },
            channel="agent-email:assistant@example.com",
        )


@pytest.mark.asyncio
async def test_agent_email_adapter_get_status_falls_back_to_first_active_mailbox() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
    )

    class FakeClient:
        base_url = "http://cosmic-mail.local"

        async def get_auth_context(self):
            return {"is_admin": True}

        async def list_mailboxes(self):
            return [
                {"id": "mbx_idle", "address": "idle@example.com", "status": "paused"},
                {"id": "mbx_primary", "address": "assistant@example.com", "status": "active"},
            ]

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            raise AssertionError("resolve_mailbox should not be called when no primary mailbox is configured")

    adapter.client = FakeClient()  # type: ignore[assignment]

    status = await adapter.get_status()

    assert status["primary_mailbox_address"] == "assistant@example.com"
    assert status["primary_mailbox"]["id"] == "mbx_primary"
