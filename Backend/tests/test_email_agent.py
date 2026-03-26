from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agents.email_agent.agent import EmailAgent, EmailAgentError
from agents.email_agent.config import EmailAgentConfig
from shared.contracts import TaskEnvelope
from shared import AgentEmailIntegrationStore


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
        async def resolve_mailbox(self, *, mailbox_id=None, mailbox_address=None):
            assert mailbox_id is None
            assert mailbox_address == "assistant@example.com"
            return {"id": "mbx_primary"}

        async def create_draft(self, payload):
            assert payload["mailbox_id"] == "mbx_primary"
            assert payload["subject"] == "YC company sheet"
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
        return None

    monkeypatch.setattr(agent, "_compose_new_email", fake_compose_new_email)
    monkeypatch.setattr(agent, "_create_outbound_draft", fake_create_outbound_draft)
    monkeypatch.setattr(agent, "_upload_input_artifacts_to_draft", fake_upload_input_artifacts_to_draft)

    class FakeMailClient:
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
        return None

    monkeypatch.setattr(agent, "_compose_new_email", fake_compose_new_email)
    monkeypatch.setattr(agent, "_create_outbound_draft", fake_create_outbound_draft)
    monkeypatch.setattr(agent, "_upload_input_artifacts_to_draft", fake_upload_input_artifacts_to_draft)

    class FakeMailClient:
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
        subject="Hello from COSMIC",
    )

    assert drafted["subject"] == "Hello from COSMIC"
    assert drafted["body"] == "Hey Praveen!\n\nJust your AI system COSMIC dropping in to say hello.\n\n— COSMIC"
    assert "Send an email to uspraveenraj@gmail.com" not in drafted["body"]


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
