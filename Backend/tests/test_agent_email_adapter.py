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
            "body_text": "Needs review.",
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
async def test_agent_email_adapter_renders_markdown_tables_for_gmail_clients() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        created_payload = None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": "mbx_primary", "address": "assistant@example.com"}

        async def create_draft(self, payload):
            self.created_payload = payload
            return {"id": "draft_table"}

        async def send_draft(self, draft_id: str):
            return {"id": "msg_table", "draft_id": draft_id, "status": "sent"}

    fake_client = FakeClient()
    adapter.client = fake_client  # type: ignore[assignment]

    markdown = """# AI Hackathons

| Hackathon | Dates | Organizer |
|---|---|---|
| **MLH Global Hack Week** | June 12-18, 2026 | Major League Hacking |
"""

    await adapter.send(
        {
            "subject": "AI Hackathons",
            "content": markdown,
            "to": [{"email": "owner@example.com", "name": "Owner"}],
        },
        channel="agent-email",
    )

    assert fake_client.created_payload is not None
    assert "|---|---|---|" not in str(fake_client.created_payload["text_body"])
    assert "Hackathon: MLH Global Hack Week" in str(fake_client.created_payload["text_body"])
    assert "<table" in str(fake_client.created_payload["html_body"])
    assert "<strong>MLH Global Hack Week</strong>" in str(fake_client.created_payload["html_body"])


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


def test_looks_like_email_process_narration_detects_tool_loop_monologue() -> None:
    from gateway.channels.agent_email import looks_like_email_process_narration

    leaked = """
Saving this to memory and replying to the email thread now.

Let me retry the email delegation with the correct input format.

Goal field has a 500-char limit. Let me shorten it.

Still over 500. Let me trim significantly.

The agent searched but didn't send. Let me delegate again with the specific thread ID and explicit send instruction.

Let me provide the exact email body so the agent doesn't have to draft from scratch.

Let me check the email agent's input schema to understand the correct format.

Now I understand the schema. Let me use the proper fields — `goal` for the short instruction, `draft_seed` for the email body, and `thread_id` to target the right thread.
""".strip()
    assert looks_like_email_process_narration(leaked) is True
    assert (
        looks_like_email_process_narration(
            "Got it — thanks for the correction on YC Startup School. I'll drop that from the prep watchlist."
        )
        is False
    )


@pytest.mark.asyncio
async def test_agent_email_adapter_suppresses_process_narration_body() -> None:
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )

    class FakeClient:
        async def resolve_mailbox(self, **kwargs):
            raise AssertionError("should not send process narration")

        async def create_draft(self, payload):
            raise AssertionError("should not create draft for process narration")

        async def send_draft(self, draft_id):
            raise AssertionError("should not send draft for process narration")

        async def reply_to_thread(self, thread_id, payload):
            raise AssertionError("should not reply with process narration")

    adapter.client = FakeClient()  # type: ignore[assignment]
    message = {
        "type": "response.complete",
        "content": (
            "Let me retry the email delegation with the correct input format.\n\n"
            "Goal field has a 500-char limit. Let me shorten it.\n\n"
            "Still over 500. Let me trim significantly.\n\n"
            "The agent searched but didn't send. Let me delegate again.\n\n"
            "Let me check the email agent's input schema."
        ),
        "thread_id": "thr_123",
        "trusted_sender": True,
        "email_thread_reply_eligible": True,
        "channel": "agent-email:assistant@example.com",
    }
    await adapter.send(message, channel="agent-email:assistant@example.com")
    assert message["email_delivery_status"] == "suppressed"
    assert message["email_delivery"]["reason"] == "process_narration"


def _attachment_adapter(tmp_path):
    adapter = AgentEmailAdapter(
        cosmic_mail_base_url="http://cosmic-mail.local",
        cosmic_mail_api_token="token",
        primary_mailbox_address="assistant@example.com",
    )
    adapter.artifacts_root = tmp_path
    return adapter


@pytest.mark.asyncio
async def test_thread_reply_uploads_deliverable_artifacts_and_leaves_supporting_files(tmp_path) -> None:
    drawio = tmp_path / "tsk_draw" / "Figure_3.1_Research_Design.drawio"
    drawio.parent.mkdir(parents=True)
    drawio.write_bytes(b"<mxfile>diagram</mxfile>")
    notes = tmp_path / "tsk_draw" / "scrape.md"
    notes.write_text("internal notes", encoding="utf-8")

    adapter = _attachment_adapter(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.draft_payload: dict[str, object] | None = None
            self.uploads: list[tuple[str, str, bytes, str | None]] = []
            self.sent_draft_id: str | None = None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": "mbx_support", "address": "assistant@example.com"}

        async def reply_to_thread(self, thread_id: str, payload):
            raise AssertionError("text-only reply must not be used when a deliverable file exists")

        async def create_draft(self, payload):
            self.draft_payload = payload
            return {"id": "draft_with_file"}

        async def upload_draft_attachment(self, draft_id, *, filename, content, mime_type=None):
            self.uploads.append((draft_id, filename, content, mime_type))
            return {"id": "att_drawio"}

        async def send_draft(self, draft_id: str):
            self.sent_draft_id = draft_id
            return {"id": "msg_sent_file", "thread_id": "thr_123", "draft_id": draft_id}

    fake_client = FakeClient()
    adapter.client = fake_client  # type: ignore[assignment]
    message = {
        "type": "response.complete",
        "thread_id": "thr_123",
        "mailbox_id": "mbx_support",
        "mailbox_address": "assistant@example.com",
        "subject": "Fwd: Draw this on draw.io",
        "internet_message_id": "<draw@example.com>",
        "message_id": "msg_assistant_row",
        "content": "The draw.io file is attached.",
        "trusted_sender": True,
        "email_thread_reply_eligible": True,
        "to_recipients": [{"email": "owner@example.com", "name": "Owner"}],
        "produced_artifacts": [
            {
                "artifact_id": "art_drawio",
                "audience": "deliverable",
                "filename": "Figure_3.1_Research_Design.drawio",
                "mime": "application/octet-stream",
                "path": "runs/artifacts/tsk_draw/Figure_3.1_Research_Design.drawio",
                "downloadable": True,
            },
            {
                "artifact_id": "art_notes",
                "audience": "supporting",
                "filename": "scrape.md",
                "mime": "text/markdown",
                "path": "runs/artifacts/tsk_draw/scrape.md",
                "downloadable": True,
            },
        ],
        "supporting_artifacts": [
            {
                "artifact_id": "art_notes",
                "audience": "supporting",
                "filename": "scrape.md",
                "path": "runs/artifacts/tsk_draw/scrape.md",
            }
        ],
    }

    await adapter.send(message, channel="agent-email:assistant@example.com")

    assert fake_client.sent_draft_id == "draft_with_file"
    assert fake_client.draft_payload is not None
    assert fake_client.draft_payload["thread_id"] == "thr_123"
    assert fake_client.draft_payload["subject"] == "Re: Fwd: Draw this on draw.io"
    assert fake_client.draft_payload["reply_to_message_id"] == "<draw@example.com>"
    assert fake_client.draft_payload["text_body"] == "The draw.io file is attached."
    assert fake_client.uploads == [
        (
            "draft_with_file",
            "Figure_3.1_Research_Design.drawio",
            b"<mxfile>diagram</mxfile>",
            "application/octet-stream",
        )
    ]
    assert message["email_delivery_status"] == "sent"
    assert message["email_delivery"]["attachments"]["uploaded"] == [
        {
            "artifact_id": "art_drawio",
            "filename": "Figure_3.1_Research_Design.drawio",
            "mime": "application/octet-stream",
        }
    ]
    assert message["email_delivery"]["attachments"]["failed"] == []


@pytest.mark.asyncio
async def test_thread_reply_without_a_readable_file_stays_on_the_text_reply(tmp_path) -> None:
    adapter = _attachment_adapter(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.replied = False

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": "mbx_support", "address": "assistant@example.com"}

        async def reply_to_thread(self, thread_id: str, payload):
            self.replied = True
            assert thread_id == "thr_123"
            assert "produced_artifacts" not in payload
            return {"id": "msg_text_only"}

        async def create_draft(self, payload):
            raise AssertionError("missing deliverables must not open an attachment draft")

        async def send_draft(self, draft_id: str):
            raise AssertionError("missing deliverables must not send a draft")

    adapter.client = FakeClient()  # type: ignore[assignment]
    message = {
        "type": "response.complete",
        "thread_id": "thr_123",
        "mailbox_id": "mbx_support",
        "mailbox_address": "assistant@example.com",
        "content": "No file on disk.",
        "trusted_sender": True,
        "email_thread_reply_eligible": True,
        "to_recipients": [{"email": "owner@example.com", "name": "Owner"}],
        "produced_artifacts": [
            {
                "artifact_id": "art_missing",
                "audience": "deliverable",
                "filename": "missing.drawio",
                "path": "runs/artifacts/missing.drawio",
            }
        ],
    }

    await adapter.send(message, channel="agent-email:assistant@example.com")

    assert message["email_delivery_status"] == "sent"
    assert message["email_delivery"]["attachments"]["uploaded"] == []
    assert message["email_delivery"]["attachments"]["failed"][0]["reason"] == "artifact_unavailable"


@pytest.mark.asyncio
async def test_new_email_uploads_deliverables_before_send(tmp_path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4")
    adapter = _attachment_adapter(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.uploads: list[str] = []
            self.sent = False

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": "mbx_primary", "address": "assistant@example.com"}

        async def create_draft(self, payload):
            assert payload["subject"] == "Morning update"
            assert "thread_id" not in payload
            return {"id": "draft_new"}

        async def upload_draft_attachment(self, draft_id, *, filename, content, mime_type=None):
            assert draft_id == "draft_new"
            assert self.sent is False
            self.uploads.append(filename)
            assert content == b"%PDF-1.4"
            return {"id": "att_pdf"}

        async def send_draft(self, draft_id: str):
            assert self.uploads == ["report.pdf"]
            self.sent = True
            return {"id": "msg_new", "draft_id": draft_id}

    adapter.client = FakeClient()  # type: ignore[assignment]
    await adapter.send(
        {
            "subject": "Morning update",
            "content": "Report attached.",
            "to": [{"email": "owner@example.com", "name": "Owner"}],
            "produced_artifacts": [
                {
                    "artifact_id": "art_pdf",
                    "audience": "deliverable",
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                    "path": "runs/artifacts/report.pdf",
                }
            ],
        },
        channel="agent-email",
    )
