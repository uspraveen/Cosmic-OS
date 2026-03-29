from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from agents.email_agent.agent import EmailAgent, EmailAgentError
from agents.email_agent.config import EmailAgentConfig
from shared.contracts import TaskEnvelope
from shared import AgentEmailIntegrationStore
from shared.cosmic_mail_client import CosmicMailClientError
from shared.sqlite_client import connect_sync


class _FakeRedis:
    pass


def _make_task(
    *,
    intent: str,
    input_payload: dict[str, Any],
    task_id: str,
    session_id: str = "sess_email_1",
    input_artifacts: list[dict[str, Any]] | None = None,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        task_list_id=session_id,
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/email-agent:1.0.0",
        intent=intent,
        input=input_payload,
        input_artifacts=input_artifacts or [],
        idempotency_key=f"idem_{task_id}",
        signature="test-signature",
        source="user",
        source_id="desktop",
        channel="desktop:local",
    )


def _build_agent(tmp_path: Path, *, config: EmailAgentConfig) -> EmailAgent:
    agent = EmailAgent(
        redis_client=_FakeRedis(),
        config=config,
        registry_db_path=tmp_path / "registry.db",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )
    agent.data_root.mkdir(parents=True, exist_ok=True)
    agent.runtime_root.mkdir(parents=True, exist_ok=True)
    agent.artifacts_root.mkdir(parents=True, exist_ok=True)
    agent._init_db()
    return agent


@pytest.mark.asyncio
async def test_email_agent_manage_instruction_roundtrip_and_recall(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    set_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_set_1",
        input_payload={
            "action": "set",
            "mailbox_address": "support@example.com",
            "label": "Auto reply invoices",
            "match": {"from_address": "billing@example.com"},
            "behavior": {"mode": "auto_reply", "reply_template": "Thanks, we received it."},
        },
    )
    set_result = await agent.execute(set_task)
    assert set_result.status == "completed"
    instruction = set_result.output["instruction"]
    assert instruction["label"] == "Auto reply invoices"
    instruction_id = instruction["instruction_id"]

    disable_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_disable_1",
        input_payload={
            "action": "disable",
            "instruction_id": instruction_id,
            "mailbox_address": "support@example.com",
        },
    )
    disable_result = await agent.execute(disable_task)
    assert disable_result.status == "completed"
    assert disable_result.output["instruction"]["enabled"] is False

    list_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_list_1",
        input_payload={
            "action": "list",
            "mailbox_address": "support@example.com",
        },
    )
    list_result = await agent.execute(list_task)
    assert list_result.status == "completed"
    assert len(list_result.output["instructions"]) == 1

    recall_task = _make_task(
        intent="email.recall_session",
        task_id="tsk_recall_1",
        input_payload={"limit": 5},
    )
    recall_result = await agent.execute(recall_task)
    assert recall_result.status == "completed"
    assert any(run["task_id"] == "tsk_disable_1" for run in recall_result.output["runs"])
    assert any(run["task_id"] == "tsk_set_1" for run in recall_result.output["runs"])


@pytest.mark.asyncio
async def test_email_agent_manage_instruction_accepts_natural_language_rule(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_set_nl_1",
        input_payload={
            "action": "set",
            "mailbox_address": "support@example.com",
            "instruction_text": "Keep an eye out for emails from Arun and let me know.",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    instruction = result.output["instruction"]
    assert instruction["raw_user_instruction"] == "Keep an eye out for emails from Arun and let me know."
    assert instruction["label"]
    assert instruction["behavior"]["mode"] == "notify_only"
    assert instruction["behavior"]["completion_mode"] == "perpetual"


@pytest.mark.asyncio
async def test_email_agent_record_delivery_completes_one_shot_instruction(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    set_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_set_one_shot_1",
        input_payload={
            "action": "set",
            "mailbox_address": "support@example.com",
            "label": "Notify once for board email",
            "match": {"subject_contains": "board"},
            "behavior": {"mode": "notify_only", "completion_mode": "one_shot"},
        },
    )
    set_result = await agent.execute(set_task)
    instruction_id = set_result.output["instruction"]["instruction_id"]

    record_task = _make_task(
        intent="email.manage_instruction",
        task_id="tsk_record_delivery_1",
        input_payload={
            "action": "record_delivery",
            "instruction_id": instruction_id,
            "mailbox_address": "support@example.com",
            "thread_id": "thr_board_1",
            "message_id": "msg_board_1",
        },
    )
    record_result = await agent.execute(record_task)

    assert record_result.status == "completed"
    instruction = next(
        item for item in record_result.output["instructions"] if item["instruction_id"] == instruction_id
    )
    assert instruction["enabled"] is False
    assert instruction["completed_at"]
    assert instruction["last_action_thread_id"] == "thr_board_1"
    assert instruction["last_action_message_id"] == "msg_board_1"


@pytest.mark.asyncio
async def test_email_agent_reason_compose_draft_uses_compact_brief_and_uploads_input_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    import agents.email_agent.agent as email_agent_module

    async def fake_invoke_email_mimo_json(**kwargs):
        return {
            "subject": "YC company sheet",
            "body": "Attached is the latest YC company sheet.",
            "summary": "Prepared an outbound email draft.",
        }

    monkeypatch.setattr(email_agent_module, "invoke_email_mimo_json", fake_invoke_email_mimo_json)

    uploaded: list[tuple[str, str, bytes, str | None]] = []

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id is None
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary"}

        async def create_draft(self, payload):
            assert payload["mailbox_id"] == "mbx_primary"
            assert payload["subject"] == "YC company sheet"
            assert payload["to_recipients"] == [{"email": "arun@example.com", "name": "Arun"}]
            assert payload["cc_recipients"] == [{"email": "finance@example.com", "name": "Finance"}]
            assert payload["bcc_recipients"] == [{"email": "audit@example.com", "name": None}]
            return {"id": "draft_123"}

        async def upload_draft_attachment(self, draft_id, *, filename, content, mime_type=None):
            uploaded.append((draft_id, filename, content, mime_type))
            return {"id": "upl_1"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    attachment_path = tmp_path / "yc.csv"
    attachment_path.write_text("company\nAcme\n", encoding="utf-8")
    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_1",
        input_payload={
            "goal": "Email the latest YC sheet to Arun.",
            "mailbox_address": "assistant@example.com",
            "to_recipients": [{"email": "arun@example.com", "name": "Arun"}],
            "cc_recipients": [{"email": "finance@example.com", "name": "Finance"}],
            "bcc_recipients": [{"email": "audit@example.com"}],
            "context_brief": "User wants to send the latest YC company list.",
        },
        input_artifacts=[
            {
                "artifact_id": "art_local",
                "task_id": "tsk_prev",
                "mime": "text/csv",
                "sha256": "dummy",
                "path": str(attachment_path),
                "created_by_agent": "cosmic/tabular-agent:1.0.0",
            }
        ],
    )

    result = await agent.execute(task)
    assert result.status == "completed"
    assert result.output["action"] == "compose_email"
    assert result.output["draft_id"] == "draft_123"
    assert result.output["attached_input_artifact_count"] == 1
    assert result.output["failed_input_artifact_count"] == 0
    assert result.output["attached_input_artifacts"] == [
        {
            "artifact_id": "art_local",
            "filename": "yc.csv",
            "mime": "text/csv",
        }
    ]
    assert "Attached 1 file to the draft." in result.output["response"]
    assert uploaded
    assert uploaded[0][0] == "draft_123"
    assert uploaded[0][1] == "yc.csv"
    assert result.artifacts
    assert result.artifacts[0].path.startswith("runs/artifacts/")


@pytest.mark.asyncio
async def test_email_agent_reason_infers_send_from_plain_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_compose_new_email(**kwargs):
        assert kwargs["recipients"] == [{"email": "uspraveenraj@gmail.com", "name": None}]
        assert kwargs["subject"] == "Hey from COSMIC 👋"
        assert kwargs["draft_seed"] == "Hello from COSMIC."
        return {
            "subject": "Hey from COSMIC 👋",
            "body": "Hello from COSMIC.",
            "summary": "Prepared an outbound email draft.",
        }

    async def fake_create_outbound_draft(**kwargs):
        assert kwargs["recipients"] == [{"email": "uspraveenraj@gmail.com", "name": None}]
        assert kwargs["subject"] == "Hey from COSMIC 👋"
        assert kwargs["text_body"] == "Hello from COSMIC."
        return {"id": "draft_456"}

    async def fake_upload_input_artifacts_to_draft(*args, **kwargs):
        return {"attempted": 0, "uploaded": [], "failed": []}

    monkeypatch.setattr(agent, "_ensure_mail_client_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_compose_new_email", fake_compose_new_email)
    monkeypatch.setattr(agent, "_create_outbound_draft", fake_create_outbound_draft)
    monkeypatch.setattr(agent, "_upload_input_artifacts_to_draft", fake_upload_input_artifacts_to_draft)

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def send_draft(self, draft_id):
            assert draft_id == "draft_456"
            return {"id": "msg_123", "thread_id": "thr_123"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_plain_goal",
        input_payload={
            "goal": "Send a test email to uspraveenraj@gmail.com. Subject: 'Hey from COSMIC 👋' — Body: 'Hello from COSMIC.'",
            "request_id": "req_plain_goal",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "compose_email"
    assert result.output["sent"] is True
    assert result.output["draft_id"] == "draft_456"
    assert result.output["message_id"] == "msg_123"
    assert result.output["attached_input_artifact_count"] == 0


@pytest.mark.asyncio
async def test_email_agent_reason_compose_reports_approval_queue_truthfully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_compose_new_email(**kwargs):
        return {
            "subject": "Queued update",
            "body": "Please review this before sending.",
            "summary": "Prepared an outbound email draft.",
        }

    async def fake_create_outbound_draft(**kwargs):
        return {"id": "draft_queued"}

    async def fake_upload_input_artifacts_to_draft(*args, **kwargs):
        return {"attempted": 0, "uploaded": [], "failed": []}

    monkeypatch.setattr(agent, "_compose_new_email", fake_compose_new_email)
    monkeypatch.setattr(agent, "_ensure_mail_client_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_create_outbound_draft", fake_create_outbound_draft)
    monkeypatch.setattr(agent, "_upload_input_artifacts_to_draft", fake_upload_input_artifacts_to_draft)

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def send_draft(self, draft_id):
            assert draft_id == "draft_queued"
            return {
                "queued_for_approval": True,
                "approval_id": "apr_123",
                "draft": {"id": "draft_queued"},
            }

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_queue_truth",
        input_payload={
            "goal": "Send an email to uspraveenraj@gmail.com saying this needs approval.",
            "request_id": "req_queue_truth",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "compose_email"
    assert result.output["sent"] is False
    assert result.output["delivery_status"] == "queued_for_approval"
    assert result.output["queued_for_approval"] is True
    assert result.output["approval_id"] == "apr_123"
    assert result.output["draft_id"] == "draft_queued"
    assert "queued for approval" in result.output["response"].lower()


@pytest.mark.asyncio
async def test_email_agent_reason_infers_following_content_body_from_plain_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_compose_new_email(**kwargs):
        assert kwargs["recipients"] == [{"email": "uspraveenraj@gmail.com", "name": None}]
        assert kwargs["subject"] == "Meta HyperAgents — What You Need to Know"
        assert kwargs["draft_seed"] == "Hey Praveen,\n\nYou asked about Meta's HyperAgents — here's the full breakdown:"
        return {
            "subject": kwargs["subject"],
            "body": kwargs["draft_seed"],
            "summary": "Prepared an outbound email draft.",
        }

    async def fake_create_outbound_draft(**kwargs):
        assert kwargs["text_body"] == "Hey Praveen,\n\nYou asked about Meta's HyperAgents — here's the full breakdown:"
        return {"id": "draft_following_content"}

    async def fake_upload_input_artifacts_to_draft(*args, **kwargs):
        return {"attempted": 0, "uploaded": [], "failed": []}

    monkeypatch.setattr(agent, "_compose_new_email", fake_compose_new_email)
    monkeypatch.setattr(agent, "_create_outbound_draft", fake_create_outbound_draft)
    monkeypatch.setattr(agent, "_upload_input_artifacts_to_draft", fake_upload_input_artifacts_to_draft)

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def send_draft(self, draft_id):
            assert draft_id == "draft_following_content"
            return {"id": "msg_following_content", "thread_id": "thr_following_content"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_following_content",
        input_payload={
            "goal": (
                "Send an email to uspraveenraj@gmail.com with the subject "
                "'Meta HyperAgents — What You Need to Know' and the following content:\n\n"
                "Hey Praveen,\n\nYou asked about Meta's HyperAgents — here's the full breakdown:"
            ),
            "request_id": "req_following_content",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "compose_email"
    assert result.output["sent"] is True


@pytest.mark.asyncio
async def test_email_agent_compose_new_email_never_uses_raw_goal_as_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    import agents.email_agent.agent as email_agent_module

    async def fake_invoke_email_mimo_json(**kwargs):
        return {}

    async def fake_invoke_email_mimo(**kwargs):
        return "Hey Praveen!\n\nJust your AI system COSMIC dropping in to say hello.\n\n— COSMIC"

    monkeypatch.setattr(email_agent_module, "invoke_email_mimo_json", fake_invoke_email_mimo_json)
    monkeypatch.setattr(email_agent_module, "invoke_email_mimo", fake_invoke_email_mimo)

    drafted = await agent._compose_new_email(
        task=_make_task(
            intent="email.reason",
            task_id="tsk_reason_compose_fallback",
            input_payload={
                "goal": (
                    "Send an email to uspraveenraj@gmail.com with subject 'Hello from COSMIC' "
                    "and a short friendly hello message from COSMIC."
                ),
            },
        ),
        goal="Send an email to uspraveenraj@gmail.com with subject 'Hello from COSMIC' and a short friendly hello message from COSMIC.",
        context_brief=None,
        draft_seed=None,
        tone_hint=None,
        recipients=[{"email": "uspraveenraj@gmail.com", "name": None}],
        cc_recipients=[],
        bcc_recipients=[],
        subject="Hello from COSMIC",
    )

    assert drafted["subject"] == "Hello from COSMIC"
    assert drafted["body"] == "Hey Praveen!\n\nJust your AI system COSMIC dropping in to say hello.\n\n— COSMIC"
    assert "Send an email to uspraveenraj@gmail.com" not in drafted["body"]


@pytest.mark.asyncio
async def test_email_agent_reason_reply_thread_sends_mailbox_and_cc_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_fetch_thread_context(*, thread_id, message_id=None):
        assert thread_id == "thr_123"
        return {
            "thread": {"id": "thr_123", "subject": "Re: Budget", "mailbox_id": "mbx_primary"},
            "subject": "Re: Budget",
            "latest_body": "Can you send the updated numbers?",
            "latest_message": {"id": "msg_latest"},
        }

    async def fake_compose_reply(**kwargs):
        assert kwargs["to_recipients"] == [{"email": "owner@example.com", "name": None}]
        assert kwargs["cc_recipients"] == [{"email": "finance@example.com", "name": None}]
        return {
            "body": "Here are the updated numbers.",
            "summary": "Prepared a reply draft for the existing email thread.",
        }

    monkeypatch.setattr(agent, "_fetch_thread_context", fake_fetch_thread_context)
    monkeypatch.setattr(agent, "_compose_reply", fake_compose_reply)

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def reply_to_thread(self, thread_id, payload):
            assert thread_id == "thr_123"
            assert payload == {
                "mailbox_id": "mbx_primary",
                "text_body": "Here are the updated numbers.",
                "to_recipients": [{"email": "owner@example.com", "name": None}],
                "cc_recipients": [{"email": "finance@example.com", "name": None}],
            }
            return {"id": "msg_reply_123"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_reply_cc",
        input_payload={
            "goal": "Reply to the thread with the updated numbers.",
            "thread_id": "thr_123",
            "send": True,
            "to_recipients": [{"email": "owner@example.com"}],
            "cc_recipients": [{"email": "finance@example.com"}],
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "reply_thread"
    assert result.output["sent"] is True
    assert result.output["message_id"] == "msg_reply_123"
    assert result.output["cc_recipients"] == [{"email": "finance@example.com", "name": None}]
    assert result.output["bcc_recipients"] == []


@pytest.mark.asyncio
async def test_email_agent_process_inbound_auto_reply_queued_does_not_claim_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    monkeypatch.setattr(agent, "_ensure_mail_client_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_fetch_thread_context", AsyncMock(return_value={
        "thread": {"id": "thr_queued", "subject": "Need approval", "mailbox_id": "mbx_primary"},
        "subject": "Need approval",
        "latest_body": "Please confirm.",
        "latest_message": {"id": "msg_latest"},
    }))
    monkeypatch.setattr(agent, "_download_message_attachments", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(agent, "_reconcile_inbound_attachments", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, "_resolve_matched_instructions", AsyncMock(return_value=(
        [{
            "instruction_id": "instr_queued",
            "label": "Auto reply",
            "behavior": {"mode": "auto_reply", "completion_mode": "one_shot", "reply_template": "Approved."},
        }],
        "Matched by auto-reply rule.",
    )))
    monkeypatch.setattr(agent, "_summarize_thread", AsyncMock(return_value="Summary"))
    monkeypatch.setattr(agent, "_apply_auto_reply", AsyncMock(return_value={
        "sent": False,
        "delivery_status": "queued_for_approval",
        "queued_for_approval": True,
        "approval_id": "apr_auto",
        "thread_id": "thr_queued",
        "message_id": None,
        "body": "Approved.",
    }))

    task = _make_task(
        intent="email.process_inbound",
        task_id="tsk_process_inbound_queue_truth",
        input_payload={
            "thread_id": "thr_queued",
            "message_id": "msg_queued",
            "mailbox_address": "assistant@example.com",
            "from_address": "sender@example.com",
            "trusted_sender": False,
            "sender_role": "external",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["sent"] is False
    assert result.output["delivery_status"] == "queued_for_approval"
    assert result.output["auto_reply"]["approval_id"] == "apr_auto"


def test_email_agent_infers_cc_and_bcc_from_plain_goal(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    inferred = agent._infer_reason_goal_hints(
        "Send an email to owner@example.com cc finance@example.com and ops@example.com "
        "bcc audit@example.com with subject 'Budget update' and the following content:\n\n"
        "Here is the latest budget update."
    )

    assert inferred["mode"] == "compose"
    assert inferred["send"] is True
    assert inferred["to_recipients"] == [{"email": "owner@example.com", "name": None}]
    assert inferred["cc_recipients"] == [
        {"email": "finance@example.com", "name": None},
        {"email": "ops@example.com", "name": None},
    ]
    assert inferred["bcc_recipients"] == [{"email": "audit@example.com", "name": None}]
    assert inferred["subject"] == "Budget update"


@pytest.mark.asyncio
async def test_email_agent_reason_read_goal_with_email_address_uses_search_not_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_search_email(**kwargs):
        assert "uspraveenraj@gmail.com" in kwargs["goal"]
        return [
            {
                "kind": "message",
                "id": "msg_reply_1",
                "thread_id": "thr_reply_1",
                "subject": "Re: Hey from COSMIC 👋",
                "snippet": "This is my reply.",
            }
        ]

    async def fake_summarize_search_results(**kwargs):
        assert kwargs["search_results"][0]["id"] == "msg_reply_1"
        return "Found the latest reply from uspraveenraj@gmail.com."

    async def fail_compose(*args, **kwargs):
        raise AssertionError("compose path should not be used for inbox-read goals")

    monkeypatch.setattr(agent, "_search_email", fake_search_email)
    monkeypatch.setattr(agent, "_summarize_search_results", fake_summarize_search_results)
    monkeypatch.setattr(agent, "_compose_new_email", fail_compose)

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_read_goal",
        input_payload={
            "goal": "Check the inbox for a reply from uspraveenraj@gmail.com to the test email I sent with subject 'Hey from COSMIC 👋'. Read the reply and tell me what Praveen said.",
            "request_id": "req_read_goal",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "search_email"
    assert result.output["sent"] is False
    assert result.output["draft_id"] is None
    assert result.output["response"] == "Found the latest reply from uspraveenraj@gmail.com."


@pytest.mark.asyncio
async def test_email_agent_reason_read_goal_falls_back_to_recent_threads_when_message_search_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def search_threads(self, *, query, mailbox_id=None, per_page=25):
            assert "replied" in query.casefold()
            return []

        async def search_messages(self, *, query, mailbox_id=None, per_page=25):
            raise CosmicMailClientError(status_code=500, message="Internal Server Error")

        async def list_threads(self, *, mailbox_id=None, page=1, per_page=25):
            return [
                {
                    "id": "thr_recent_reply",
                    "subject": "Re: Hey from COSMIC 👋",
                    "snippet": "Hey Cosmic, Yep, reading it loud and clear! Thanks, Praveen",
                    "last_message_at": "2026-03-26T16:30:03.501313Z",
                },
                {
                    "id": "thr_older",
                    "subject": "Weekly update",
                    "snippet": "Old thread",
                    "last_message_at": "2026-03-20T10:00:00.000000Z",
                },
            ]

    async def fake_resolve_mailbox(*, mailbox_address=None, mailbox_id=None, required=False):
        return {"id": "mbx_primary"}

    async def fake_summarize_search_results(**kwargs):
        results = kwargs["search_results"]
        assert results[0]["id"] == "thr_recent_reply"
        return "Found your latest reply in the recent thread list."

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_resolve_mailbox", fake_resolve_mailbox)
    monkeypatch.setattr(agent, "_summarize_search_results", fake_summarize_search_results)

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_recent_fallback",
        input_payload={
            "goal": "I replied to yours. Check the inbox and tell me what I said.",
            "request_id": "req_reason_recent_fallback",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "search_email"
    assert result.output["sent"] is False
    assert result.output["response"] == "Found your latest reply in the recent thread list."
    assert result.output["search_results"][0]["id"] == "thr_recent_reply"


@pytest.mark.asyncio
async def test_email_agent_reason_read_goal_with_stray_recipients_stays_on_search_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
        ),
    )

    async def fake_search_email(**kwargs):
        return [
            {
                "kind": "message",
                "id": "msg_last_1",
                "thread_id": "thr_last_1",
                "subject": "Re: Morning check-in",
                "snippet": "Latest message body.",
            }
        ]

    async def fake_summarize_search_results(**kwargs):
        assert kwargs["search_results"][0]["id"] == "msg_last_1"
        return "The latest email says: Latest message body."

    async def fail_compose(*args, **kwargs):
        raise AssertionError("compose path should not be used for read-like goals even if recipient fields are present")

    monkeypatch.setattr(agent, "_search_email", fake_search_email)
    monkeypatch.setattr(agent, "_summarize_search_results", fake_summarize_search_results)
    monkeypatch.setattr(agent, "_compose_new_email", fail_compose)

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_read_stray_recipients",
        input_payload={
            "goal": "What was last email you got from me?",
            "to_recipients": [{"email": "uspraveenraj@gmail.com"}],
            "subject": "Ignore this leaked subject",
            "request_id": "req_reason_read_stray_recipients",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "search_email"
    assert result.output["sent"] is False
    assert result.output["response"] == "The latest email says: Latest message body."


@pytest.mark.asyncio
async def test_email_agent_search_goal_from_me_expands_trusted_sender_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )
    agent._trusted_sender_set = {"uspraveenraj@gmail.com"}

    captured_queries: list[str] = []

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def search_threads(self, *, query, mailbox_id=None, per_page=25):
            captured_queries.append(query)
            return []

        async def search_messages(self, *, query, mailbox_id=None, per_page=25):
            captured_queries.append(query)
            return []

        async def list_threads(self, *, mailbox_id=None, page=1, per_page=25):
            return []

    async def fake_resolve_mailbox(*, mailbox_address=None, mailbox_id=None, required=False):
        return {"id": "mbx_primary"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_resolve_mailbox", fake_resolve_mailbox)

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_from_me_query",
        input_payload={
            "goal": "What was last email you got from me?",
            "request_id": "req_reason_from_me_query",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "search_email"
    assert captured_queries
    assert all("uspraveenraj@gmail.com" in query for query in captured_queries)


@pytest.mark.asyncio
async def test_email_agent_explicit_disconnect_blocks_env_fallback(tmp_path: Path) -> None:
    integration_db_path = tmp_path / "gateway" / "agent_email_integrations.db"
    AgentEmailIntegrationStore(integration_db_path).clear_primary()

    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://env-mail.local",
            cosmic_mail_api_token="env-token",
            primary_mailbox_address="assistant@example.com",
            gateway_internal_token="",
            enable_internal_llm=False,
            agent_email_integrations_db_path=integration_db_path,
        ),
    )

    await agent._refresh_mail_client_from_store()

    assert agent.config.cosmic_mail_base_url == ""
    assert agent.config.cosmic_mail_api_token == ""
    assert agent.config.primary_mailbox_address == ""

    with pytest.raises(EmailAgentError) as exc_info:
        await agent._ensure_mail_client_ready()

    assert exc_info.value.code == "AUTH_ERROR"


@pytest.mark.asyncio
async def test_email_agent_process_inbound_marks_trusted_sender_from_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_db_path = tmp_path / "gateway" / "agent_email_integrations.db"
    AgentEmailIntegrationStore(integration_db_path).save_trusted_senders(
        ["Owner@Example.com"],
        updated_at="2026-03-27T00:00:00Z",
    )
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
            attachment_docs_auto_parse_enabled=False,
            agent_email_integrations_db_path=integration_db_path,
        ),
    )

    async def fake_fetch_thread_context(*, thread_id, message_id=None, **kwargs):
        return {
            "thread": {"id": thread_id, "subject": "Owner follow-up"},
            "messages": [],
            "subject": "Owner follow-up",
            "latest_message": {"id": message_id, "from_address": "Owner@Example.com"},
            "latest_body": "Check this quickly.",
        }

    async def fake_summarize_thread(**kwargs):
        return "Inbound owner note."

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def list_message_attachments(self, message_id):
            return []

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_fetch_thread_context", fake_fetch_thread_context)
    monkeypatch.setattr(agent, "_summarize_thread", fake_summarize_thread)

    task = _make_task(
        intent="email.process_inbound",
        task_id="tsk_process_inbound_trusted_sender",
        input_payload={
            "thread_id": "thr_email_owner",
            "message_id": "msg_email_owner",
            "mailbox_address": "assistant@example.com",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["trusted_sender"] is True
    assert result.output["sender_role"] == "owner"
    assert result.output["from_address"] == "Owner@Example.com"


@pytest.mark.asyncio
async def test_email_agent_process_inbound_llm_matches_natural_language_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=True,
            attachment_docs_auto_parse_enabled=False,
        ),
    )

    agent._upsert_instruction(
        instruction_id="eminst_watch_arun",
        mailbox_address="assistant@example.com",
        label="Watch for Arun",
        match={},
        behavior={"mode": "notify_only", "completion_mode": "perpetual"},
        raw_user_instruction="Keep an eye out for emails from Arun and let me know.",
        enabled=True,
    )

    async def fake_fetch_thread_context(*, thread_id, message_id=None, **kwargs):
        return {
            "thread": {"id": thread_id, "subject": "Need your take"},
            "messages": [
                {
                    "from_address": "unknown@example.com",
                    "subject": "Need your take",
                    "text_body": "Hey Cosmic, Arun here. Can you review the latest numbers?",
                }
            ],
            "subject": "Need your take",
            "latest_message": {"id": message_id, "from_address": "unknown@example.com"},
            "latest_body": "Hey Cosmic, Arun here. Can you review the latest numbers?",
        }

    async def fake_summarize_thread(**kwargs):
        return "Arun emailed asking for a review."

    async def fake_invoke_email_mimo_json(**kwargs):
        if kwargs.get("operation") == "email.internal_llm.match_instructions":
            return {
                "matched_instruction_ids": ["eminst_watch_arun"],
                "rationale": "The inbound email explicitly identifies the sender as Arun in the body.",
                "ambiguous": False,
            }
        return {}

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def list_message_attachments(self, message_id):
            return []

    import agents.email_agent.agent as email_agent_module

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_fetch_thread_context", fake_fetch_thread_context)
    monkeypatch.setattr(agent, "_summarize_thread", fake_summarize_thread)
    monkeypatch.setattr(email_agent_module, "invoke_email_mimo_json", fake_invoke_email_mimo_json)

    task = _make_task(
        intent="email.process_inbound",
        task_id="tsk_process_inbound_llm_instruction",
        input_payload={
            "thread_id": "thr_watch_arun",
            "message_id": "msg_watch_arun",
            "mailbox_address": "assistant@example.com",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["matched_instruction"]["instruction_id"] == "eminst_watch_arun"
    assert result.output["matched_instructions"][0]["instruction_id"] == "eminst_watch_arun"
    assert result.output["instruction_match_reason"] == "The inbound email explicitly identifies the sender as Arun in the body."


@pytest.mark.asyncio
async def test_email_agent_fetch_thread_context_falls_back_when_get_thread_404() -> None:
    tmpdir = Path.cwd() / f"tmp_email_agent_thread_fallback_{uuid4().hex}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    agent = _build_agent(
        tmpdir,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def get_thread(self, thread_id):
            raise CosmicMailClientError(status_code=404, message="Not Found")

        async def get_thread_messages(self, thread_id):
            return [
                {
                    "id": "msg_email_owner",
                    "thread_id": thread_id,
                    "subject": "I am testing",
                    "from_name": "Praveen Raj U S",
                    "from_address": "Owner@Example.com",
                    "text_body": "Are you there cosmic?",
                    "preview_text": "Are you there cosmic?",
                }
            ]

        async def list_threads(self, *, mailbox_id=None, page=1, per_page=25):
            return [
                {
                    "id": "thr_email_owner",
                    "mailbox_id": mailbox_id,
                    "subject": "I am testing",
                    "snippet": "Are you there cosmic?",
                }
            ]

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            return {"id": mailbox_id or "mbx_owner", "address": mailbox_address or "assistant@example.com", "status": "active"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    context = await agent._fetch_thread_context(
        thread_id="thr_email_owner",
        message_id="msg_email_owner",
        mailbox_address="assistant@example.com",
        mailbox_id="mbx_owner",
    )

    assert context["thread"]["id"] == "thr_email_owner"
    assert context["subject"] == "I am testing"
    assert context["latest_body"] == "Are you there cosmic?"
    assert context["latest_message"]["from_address"] == "Owner@Example.com"


def test_email_agent_card_allows_gateway_process_inbound() -> None:
    card_path = Path(__file__).resolve().parents[1] / "agents" / "email_agent" / "agent_card.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))

    policies = card.get("policies") or {}
    allowed_senders = policies.get("allowed_senders") or []
    intent_auth = policies.get("intent_authorization") or {}
    process_inbound_senders = intent_auth.get("email.process_inbound") or []
    manage_instruction_senders = intent_auth.get("email.manage_instruction") or []

    assert "cosmic/gateway:1.0.0" in allowed_senders
    assert "cosmic/gateway:1.0.0" in process_inbound_senders
    assert "cosmic/gateway:1.0.0" in manage_instruction_senders


@pytest.mark.asyncio
async def test_email_agent_resolve_mailbox_falls_back_to_first_active_mailbox() -> None:
    tmpdir = Path.cwd() / f"tmp_email_agent_mailbox_fallback_{uuid4().hex}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    agent = _build_agent(
        tmpdir,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def list_mailboxes(self):
            return [
                {"id": "mbx_idle", "address": "idle@example.com", "status": "paused"},
                {"id": "mbx_primary", "address": "assistant@example.com", "status": "active"},
            ]

        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id is None
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary", "address": "assistant@example.com"}

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]

    mailbox = await agent._resolve_mailbox(mailbox_address=None, mailbox_id=None)

    assert mailbox["id"] == "mbx_primary"


@pytest.mark.asyncio
async def test_email_agent_process_inbound_auto_parses_supported_document_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
            attachment_docs_auto_parse_enabled=True,
        ),
    )

    async def fake_fetch_thread_context(*, thread_id, message_id=None):
        assert thread_id == "thr_email_1"
        assert message_id == "msg_email_1"
        return {
            "thread": {"id": "thr_email_1", "subject": "Quarterly update"},
            "messages": [],
            "subject": "Quarterly update",
            "latest_message": {"id": "msg_email_1", "from_address": "sender@example.com"},
            "latest_body": "Please review the attached deck.",
        }

    async def fake_summarize_thread(**kwargs):
        return "Inbound summary."

    dispatched_artifacts: list[dict[str, Any]] = []

    async def fake_dispatch_docs_parse_bundle(*, task, thread_id, message_id, input_artifacts):
        assert task.task_id == "tsk_process_inbound_parse"
        assert thread_id == "thr_email_1"
        assert message_id == "msg_email_1"
        assert len(input_artifacts) == 1
        dispatched_artifacts.extend(input_artifacts)
        return {
            "status": "completed",
            "task_id": "tsk_docs_parse_child",
            "output": {
                "bundle_id": "bundle_email_1",
                "documents": [
                    {
                        "artifact_id": input_artifacts[0]["artifact_id"],
                        "doc_id": "doc_email_1",
                        "title": "Quarterly Deck",
                        "chunk_count": 8,
                        "paths": {"document_md": "runs/artifacts/tsk_docs_parse_child/docs_parser/a1/document.md"},
                    }
                ],
            },
        }

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def list_message_attachments(self, message_id):
            assert message_id == "msg_email_1"
            return [
                {
                    "id": "att_pdf_1",
                    "filename": "quarterly-deck.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 17,
                },
                {
                    "id": "att_png_1",
                    "filename": "logo.png",
                    "content_type": "image/png",
                    "size_bytes": 8,
                },
            ]

        async def download_attachment(self, attachment_id):
            if attachment_id == "att_pdf_1":
                return (b"%PDF-email-deck%", "application/pdf", "quarterly-deck.pdf")
            if attachment_id == "att_png_1":
                return (b"\x89PNGmail", "image/png", "logo.png")
            raise AssertionError(f"Unexpected attachment id: {attachment_id}")

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_fetch_thread_context", fake_fetch_thread_context)
    monkeypatch.setattr(agent, "_summarize_thread", fake_summarize_thread)
    monkeypatch.setattr(agent, "_dispatch_docs_parse_bundle", fake_dispatch_docs_parse_bundle)

    task = _make_task(
        intent="email.process_inbound",
        task_id="tsk_process_inbound_parse",
        input_payload={
            "thread_id": "thr_email_1",
            "message_id": "msg_email_1",
            "mailbox_address": "assistant@example.com",
            "request_id": "req_email_1",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert len(dispatched_artifacts) == 1
    attachments = result.output["attachments"]
    assert len(attachments) == 2
    pdf_attachment = next(item for item in attachments if item["id"] == "att_pdf_1")
    png_attachment = next(item for item in attachments if item["id"] == "att_png_1")
    assert pdf_attachment["parse_status"] == "parsed"
    assert pdf_attachment["parse_cached"] is False
    assert pdf_attachment["parsed_bundle_id"] == "bundle_email_1"
    assert pdf_attachment["parsed_summary"]["doc_id"] == "doc_email_1"
    assert pdf_attachment["docs_tools"] == [
        "docs_browse",
        "docs_search",
        "docs_read",
        "docs_fetch_asset",
        "docs_reinspect_asset",
    ]
    assert png_attachment["parse_status"] == "skipped_unsupported"
    assert "Attachments: 1 attachment(s) were parsed" in result.output["summary"]

    raw_attachment_artifacts = [
        artifact for artifact in result.artifacts if "/email_agent/attachments/" in artifact.path.replace("\\", "/")
    ]
    assert len(raw_attachment_artifacts) == 2
    assert any("/attachments/msg_email_1/att_pdf_1__quarterly-deck.pdf" in artifact.path for artifact in raw_attachment_artifacts)
    assert any("/attachments/msg_email_1/att_png_1__logo.png" in artifact.path for artifact in raw_attachment_artifacts)

    with connect_sync(agent.session_db_path) as conn:
        attachment_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT attachment_id, parse_status, parsed_bundle_id FROM email_attachment_records ORDER BY attachment_id"
            ).fetchall()
        ]
        parse_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT status, parsed_bundle_id FROM email_attachment_parse_runs ORDER BY created_at"
            ).fetchall()
        ]
    assert attachment_rows == [
        ("att_pdf_1", "parsed", "bundle_email_1"),
        ("att_png_1", "skipped_unsupported", None),
    ]
    assert parse_rows == [("completed", "bundle_email_1")]


@pytest.mark.asyncio
async def test_email_agent_process_inbound_reuses_cached_attachment_parse_by_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
            attachment_docs_auto_parse_enabled=True,
        ),
    )

    async def fake_fetch_thread_context(*, thread_id, message_id=None):
        return {
            "thread": {"id": thread_id, "subject": "Attachment follow-up"},
            "messages": [],
            "subject": "Attachment follow-up",
            "latest_message": {"id": message_id, "from_address": "sender@example.com"},
            "latest_body": "See attachment.",
        }

    async def fake_summarize_thread(**kwargs):
        return "Inbound summary."

    dispatch_calls: list[list[dict[str, Any]]] = []

    async def fake_dispatch_docs_parse_bundle(*, task, thread_id, message_id, input_artifacts):
        dispatch_calls.append(input_artifacts)
        return {
            "status": "completed",
            "task_id": "tsk_docs_parse_cached",
            "output": {
                "bundle_id": "bundle_cached_1",
                "documents": [
                    {
                        "artifact_id": input_artifacts[0]["artifact_id"],
                        "doc_id": "doc_cached_1",
                        "title": "Invoice",
                    }
                ],
            },
        }

    class FakeMailClient:
        base_url = "http://cosmic-mail.local"
        api_token = "mail-token"
        timeout_sec = 20.0

        async def aclose(self):
            return None

        async def list_message_attachments(self, message_id):
            if message_id == "msg_email_1":
                return [
                    {
                        "id": "att_pdf_1",
                        "filename": "invoice.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 19,
                    }
                ]
            if message_id == "msg_email_2":
                return [
                    {
                        "id": "att_pdf_2",
                        "filename": "invoice-copy.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 19,
                    }
                ]
            raise AssertionError(f"Unexpected message id: {message_id}")

        async def download_attachment(self, attachment_id):
            if attachment_id in {"att_pdf_1", "att_pdf_2"}:
                return (b"%PDF-same-bytes-mail%", "application/pdf", "invoice.pdf")
            raise AssertionError(f"Unexpected attachment id: {attachment_id}")

    agent.mail_client = FakeMailClient()  # type: ignore[assignment]
    monkeypatch.setattr(agent, "_fetch_thread_context", fake_fetch_thread_context)
    monkeypatch.setattr(agent, "_summarize_thread", fake_summarize_thread)
    monkeypatch.setattr(agent, "_dispatch_docs_parse_bundle", fake_dispatch_docs_parse_bundle)

    first_result = await agent.execute(
        _make_task(
            intent="email.process_inbound",
            task_id="tsk_process_inbound_first",
            input_payload={
                "thread_id": "thr_email_cache",
                "message_id": "msg_email_1",
                "mailbox_address": "assistant@example.com",
            },
        )
    )
    second_result = await agent.execute(
        _make_task(
            intent="email.process_inbound",
            task_id="tsk_process_inbound_second",
            input_payload={
                "thread_id": "thr_email_cache",
                "message_id": "msg_email_2",
                "mailbox_address": "assistant@example.com",
            },
        )
    )

    assert first_result.status == "completed"
    assert second_result.status == "completed"
    assert len(dispatch_calls) == 1
    first_attachment = first_result.output["attachments"][0]
    second_attachment = second_result.output["attachments"][0]
    assert first_attachment["parse_status"] == "parsed"
    assert first_attachment["parse_cached"] is False
    assert second_attachment["parse_status"] == "parsed"
    assert second_attachment["parse_cached"] is True
    assert second_attachment["parsed_bundle_id"] == "bundle_cached_1"
    assert second_attachment["parsed_summary"]["doc_id"] == "doc_cached_1"


@pytest.mark.asyncio
async def test_email_agent_reason_resolves_cached_attachment_bundle_for_thread_goal(tmp_path: Path) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    source_path = tmp_path / "runs" / "artifacts" / "tsk_prev" / "email_agent" / "attachments" / "msg_att_1" / "att_pdf_1__deck.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"%PDF-deck%")
    manifest = agent._artifact_manifest(
        task_id="tsk_prev",
        path=source_path,
        mime="application/pdf",
        kind="input",
        audience="supporting",
    )
    agent._upsert_attachment_record(
        record_id=agent._attachment_record_id(message_id="msg_att_1", attachment_id="att_pdf_1"),
        mailbox_address="assistant@example.com",
        thread_id="thr_att_1",
        message_id="msg_att_1",
        attachment={
            "id": "att_pdf_1",
            "filename": "deck.pdf",
            "mime_type": "application/pdf",
            "size_bytes": source_path.stat().st_size,
            "sha256": manifest.sha256,
            "artifact_id": manifest.artifact_id,
            "path": manifest.path,
        },
        download_status="downloaded",
        parse_status="parsed",
        parse_task_id="tsk_docs_bundle_1",
        parsed_bundle_id="bundle_att_1",
        parsed_summary={"doc_id": "doc_att_1", "title": "Deck", "chunk_count": 6},
        parse_error=None,
    )

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_resolve_cached",
        input_payload={
            "goal": "Read the attached PDF from this thread and tell me what it says.",
            "thread_id": "thr_att_1",
            "message_id": "msg_att_1",
            "mailbox_address": "assistant@example.com",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "resolve_attachment"
    assert result.output["bundle_id"] == "bundle_att_1"
    assert result.output["attachment_resolution_status"] == "parsed"
    assert result.output["resolved_attachment"]["attachment_id"] == "att_pdf_1"
    assert result.output["resolved_attachment"]["parsed_summary"]["doc_id"] == "doc_att_1"
    assert result.output["docs_tools"] == [
        "docs_browse",
        "docs_search",
        "docs_read",
        "docs_fetch_asset",
        "docs_reinspect_asset",
    ]


@pytest.mark.asyncio
async def test_email_agent_reason_resolves_attachment_by_type_and_parses_on_demand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _build_agent(
        tmp_path,
        config=EmailAgentConfig(
            cosmic_mail_base_url="http://cosmic-mail.local",
            cosmic_mail_api_token="mail-token",
            gateway_internal_token="",
            enable_internal_llm=False,
        ),
    )

    pdf_path = tmp_path / "runs" / "artifacts" / "tsk_prev" / "email_agent" / "attachments" / "msg_att_2" / "att_pdf_2__deck.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-deck-2%")
    pdf_manifest = agent._artifact_manifest(
        task_id="tsk_prev",
        path=pdf_path,
        mime="application/pdf",
        kind="input",
        audience="supporting",
    )
    png_path = tmp_path / "runs" / "artifacts" / "tsk_prev" / "email_agent" / "attachments" / "msg_att_2" / "att_png_2__logo.png"
    png_path.write_bytes(b"\x89PNGlogo")
    png_manifest = agent._artifact_manifest(
        task_id="tsk_prev",
        path=png_path,
        mime="image/png",
        kind="input",
        audience="supporting",
    )
    agent._upsert_attachment_record(
        record_id=agent._attachment_record_id(message_id="msg_att_2", attachment_id="att_pdf_2"),
        mailbox_address="assistant@example.com",
        thread_id="thr_att_2",
        message_id="msg_att_2",
        attachment={
            "id": "att_pdf_2",
            "filename": "deck.pdf",
            "mime_type": "application/pdf",
            "size_bytes": pdf_path.stat().st_size,
            "sha256": pdf_manifest.sha256,
            "artifact_id": pdf_manifest.artifact_id,
            "path": pdf_manifest.path,
        },
        download_status="downloaded",
        parse_status="queued",
    )
    agent._upsert_attachment_record(
        record_id=agent._attachment_record_id(message_id="msg_att_2", attachment_id="att_png_2"),
        mailbox_address="assistant@example.com",
        thread_id="thr_att_2",
        message_id="msg_att_2",
        attachment={
            "id": "att_png_2",
            "filename": "logo.png",
            "mime_type": "image/png",
            "size_bytes": png_path.stat().st_size,
            "sha256": png_manifest.sha256,
            "artifact_id": png_manifest.artifact_id,
            "path": png_manifest.path,
        },
        download_status="downloaded",
        parse_status="skipped_unsupported",
    )

    async def fake_dispatch_docs_parse_bundle(*, task, thread_id, message_id, input_artifacts):
        assert task.task_id == "tsk_reason_resolve_ondemand"
        assert thread_id == "thr_att_2"
        assert message_id == "msg_att_2"
        assert [item["artifact_id"] for item in input_artifacts] == [pdf_manifest.artifact_id]
        return {
            "status": "completed",
            "task_id": "tsk_docs_parse_on_demand",
            "output": {
                "bundle_id": "bundle_att_2",
                "documents": [
                    {
                        "artifact_id": pdf_manifest.artifact_id,
                        "doc_id": "doc_att_2",
                        "title": "Deck",
                        "chunk_count": 9,
                    }
                ],
            },
        }

    monkeypatch.setattr(agent, "_dispatch_docs_parse_bundle", fake_dispatch_docs_parse_bundle)

    task = _make_task(
        intent="email.reason",
        task_id="tsk_reason_resolve_ondemand",
        input_payload={
            "goal": "Read the attached PDF from this email thread.",
            "thread_id": "thr_att_2",
            "message_id": "msg_att_2",
            "mailbox_address": "assistant@example.com",
        },
    )

    result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["action"] == "resolve_attachment"
    assert result.output["bundle_id"] == "bundle_att_2"
    assert result.output["attachment_resolution_status"] == "parsed"
    assert result.output["resolved_attachment"]["attachment_id"] == "att_pdf_2"
    assert result.output["resolved_attachment"]["parsed_summary"]["doc_id"] == "doc_att_2"

    with connect_sync(agent.session_db_path) as conn:
        row = conn.execute(
            "SELECT parse_status, parsed_bundle_id FROM email_attachment_records WHERE attachment_id = ?",
            ("att_pdf_2",),
        ).fetchone()
    assert tuple(row) == ("parsed", "bundle_att_2")
