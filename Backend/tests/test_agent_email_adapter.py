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
    assert fake_client.sent_draft_id == "draft_123"


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
