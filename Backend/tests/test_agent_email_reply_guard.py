from __future__ import annotations

import json

import pytest

from orchestrator.tools.executor import ToolExecutionContext, ToolExecutor
from shared import AgentResult, TaskEnvelope, sign_task_envelope, utcnow
from shared.agent_email_reply_guard import blocked_agent_email_reply_send


def test_reply_send_is_refused_only_for_the_inbound_person() -> None:
    inbound = "Owner@Example.com"
    channel = "agent-email:iamcosmic@example.com"

    same_person = blocked_agent_email_reply_send(
        channel=channel,
        intent="email.send",
        payload={"to_recipients": [{"email": "owner@example.com"}], "subject": "Hi", "body": "Hi"},
        inbound_sender_email=inbound,
    )
    assert same_person is not None
    assert "Nothing was sent." in same_person

    someone_else = blocked_agent_email_reply_send(
        channel=channel,
        intent="email.send",
        payload={"to_recipients": [{"email": "arun@example.com"}], "subject": "Hi", "body": "Hi"},
        inbound_sender_email=inbound,
    )
    assert someone_else is None

    search = blocked_agent_email_reply_send(
        channel=channel,
        intent="email.handle",
        payload={"goal": "Search the inbox for the drawio thread", "send": False},
        inbound_sender_email=inbound,
    )
    assert search is None

    unspecified_send = blocked_agent_email_reply_send(
        channel=channel,
        intent="email.handle",
        payload={"goal": "Send the file again", "send": True, "artifact_ids": ["art_drawio"]},
        inbound_sender_email=inbound,
    )
    assert unspecified_send is not None
    assert "art_drawio" in unspecified_send
    assert "artifact_redeliver" in unspecified_send

    desktop = blocked_agent_email_reply_send(
        channel="desktop",
        intent="email.send",
        payload={"to_recipients": [{"email": "owner@example.com"}], "body": "Hi"},
        inbound_sender_email=inbound,
    )
    assert desktop is None


def _parent(*, channel: str, inbound: str | None = None) -> TaskEnvelope:
    task_input = {"query": "Send the attachment again", "request_id": "req_email"}
    if inbound:
        task_input["inbound_sender_email"] = inbound
    task = TaskEnvelope(
        task_id="tsk_parent",
        task_list_id="email-thread",
        parent_task_id=None,
        session_id="email-thread",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input=task_input,
        input_artifacts=[],
        idempotency_key="idem_parent",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id=None,
        channel=channel,
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "signing-secret")})


@pytest.mark.asyncio
async def test_delegate_refuses_the_second_send_before_the_email_agent_runs() -> None:
    async def dispatcher(**kwargs):
        raise AssertionError(f"dispatcher should not run for {kwargs.get('intent')}")

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = json.loads(
        await executor.execute(
            "delegate_to_agent",
            {
                "intent": "email.handle",
                "input": {
                    "goal": "Send the drawio file again",
                    "send": True,
                    "to_recipients": [{"email": "owner@example.com"}],
                },
                "artifact_ids": ["art_drawio"],
            },
            context=ToolExecutionContext(
                parent_task=_parent(channel="agent-email:iamcosmic@example.com", inbound="owner@example.com"),
                channel="agent-email:iamcosmic@example.com",
                session_id="email-thread",
            ),
        )
    )

    assert result["sent"] is False
    assert result.get("error") is not True
    assert result["code"] == "AGENT_EMAIL_REPLY_IS_THE_EMAIL"
    assert result["delegation"]["dispatched"] is False
    assert "art_drawio" in result["message"]


@pytest.mark.asyncio
async def test_delegate_still_sends_agent_email_to_someone_else() -> None:
    seen: list[str] = []

    async def dispatcher(**kwargs):
        seen.append(str(kwargs.get("intent")))
        payload = kwargs.get("input_payload")
        assert isinstance(payload, dict)
        assert payload.get("inbound_sender_email") == "owner@example.com"
        return AgentResult(status="completed", output={"sent": True, "response": "Sent to Arun."})

    executor = ToolExecutor(agent_dispatcher=dispatcher)
    result = json.loads(
        await executor.execute(
            "delegate_to_agent",
            {
                "intent": "email.send",
                "input": {
                    "to_recipients": [{"email": "arun@example.com"}],
                    "subject": "The file",
                    "body": "Attached for you.",
                },
            },
            context=ToolExecutionContext(
                parent_task=_parent(channel="agent-email:iamcosmic@example.com", inbound="owner@example.com"),
                channel="agent-email:iamcosmic@example.com",
                session_id="email-thread",
            ),
        )
    )

    assert seen == ["email.send"]
    assert result["sent"] is True
