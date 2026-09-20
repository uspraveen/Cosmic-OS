"""Tests for the email send hardening: LLM plan extraction fallback, schema
validation with retry, the default notification recipient, honest completion
on undelivered sends, and the mechanical email.send intent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agents.email_agent.agent as email_agent_module
from agents.email_agent.agent import EmailAgent, EmailAgentError


def _agent(**config_overrides) -> EmailAgent:
    agent = EmailAgent.__new__(EmailAgent)
    agent.config = SimpleNamespace(
        enable_internal_llm=True,
        default_notification_recipient="",
        cosmic_mail_timeout_sec=20.0,
    )
    agent._http_client = None
    for key, value in config_overrides.items():
        setattr(agent.config, key, value)
    return agent


# ---------------------------------------------------------------------------
# _llm_extract_email_plan: LLM proposes, schema disposes
# ---------------------------------------------------------------------------

def _patch_invoke(monkeypatch, responses):
    calls = []

    async def _invoke(**kwargs):
        calls.append(kwargs)
        if len(responses) == 1:
            return responses[0]
        return responses[len(calls) - 1]

    monkeypatch.setattr(email_agent_module, "invoke_email_internal_llm_json", _invoke)
    return calls


@pytest.mark.asyncio
async def test_llm_parse_extracts_recipients_and_send(monkeypatch):
    calls = _patch_invoke(monkeypatch, [
        {"action": "compose_and_send", "to_recipients": [{"email": "uspraveenraj@gmail.com"}],
         "cc_recipients": [], "bcc_recipients": [], "subject": "Jev update",
         "draft_id": "", "send": True},
    ])
    agent = _agent()
    plan = await agent._llm_extract_email_plan(
        goal="email me the hourly update about the reranker build"
    )
    assert plan["action"] == "compose_and_send"
    assert plan["to_recipients"][0]["email"] == "uspraveenraj@gmail.com"
    assert plan["send"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_llm_parse_retries_once_with_validation_error(monkeypatch):
    calls = _patch_invoke(monkeypatch, [
        {"action": "fly_to_the_moon"},
        {"action": "compose", "to_recipients": [{"email": "a@b.co"}], "send": False},
    ])
    agent = _agent()
    plan = await agent._llm_extract_email_plan(goal="draft something for a@b.co")
    assert plan["action"] == "compose"
    assert len(calls) == 2
    assert "invalid" in calls[1]["user_message"]


@pytest.mark.asyncio
async def test_llm_parse_gives_up_after_two_invalid(monkeypatch):
    _patch_invoke(monkeypatch, [None, None])
    agent = _agent()
    assert await agent._llm_extract_email_plan(goal="what does this even mean") is None


@pytest.mark.asyncio
async def test_llm_parse_rejects_invalid_recipient_address(monkeypatch):
    _patch_invoke(monkeypatch, [
        {"action": "compose_and_send", "to_recipients": [{"email": "not-an-email"}], "send": True}
    ])
    agent = _agent()
    assert await agent._llm_extract_email_plan(goal="send it to the usual") is None


def test_validate_email_plan_caps_recipient_count():
    agent = _agent()
    plan = {
        "action": "compose",
        "to_recipients": [{"email": f"user{i}@x.co"} for i in range(11)],
    }
    ok, error = agent._validate_email_plan(plan)
    assert ok is False
    assert "excessive" in error


# ---------------------------------------------------------------------------
# Default notification recipient
# ---------------------------------------------------------------------------

def _apply(agent, recipients, *, send, thread_id=None, message_id=None):
    return agent._apply_notification_recipient_default(
        recipients, send=send, thread_id=thread_id, message_id=message_id
    )


def test_notification_default_applies_to_ownerless_send():
    agent = _agent(default_notification_recipient="uspraveenraj@gmail.com")
    recipients = _apply(agent, [], send=True)
    assert recipients == [{"email": "uspraveenraj@gmail.com", "name": None}]


def test_notification_default_ignores_explicit_recipients():
    agent = _agent(default_notification_recipient="uspraveenraj@gmail.com")
    recipients = _apply(agent, [{"email": "other@x.co"}], send=True)
    assert recipients == [{"email": "other@x.co"}]


def test_notification_default_ignores_thread_replies_and_compose_only():
    agent = _agent(default_notification_recipient="uspraveenraj@gmail.com")
    assert _apply(agent, [], send=True, thread_id="t1") == []
    assert _apply(agent, [], send=True, message_id="m1") == []
    assert _apply(agent, [], send=False) == []
    agent_no_default = _agent()
    assert _apply(agent_no_default, [], send=True) == []


# ---------------------------------------------------------------------------
# email.send: the mechanical intent
# ---------------------------------------------------------------------------

def _send_agent(*, delivery=None):
    agent = EmailAgent.__new__(EmailAgent)
    agent.config = SimpleNamespace(cosmic_mail_timeout_sec=20.0)
    created = []

    class _MailClient:
        async def create_draft(self, payload):
            created.append(payload)
            return {"id": "draft_77"}

    delivery_payload = delivery if delivery is not None else {
        "sent": True,
        "delivery_status": "sent",
        "message_id": "gm_1",
    }
    sent_records = []

    async def _send_checked(draft_id, *, origin):
        sent_records.append((draft_id, origin))
        if delivery_payload and delivery_payload.get("_empty"):
            return {}
        return {"id": draft_id, **delivery_payload}

    async def _resolve_mailbox(*, mailbox_address=None, mailbox_id=None):
        return {"id": "mb_1"}

    agent.mail_client = _MailClient()
    agent._send_draft_checked = _send_checked
    agent._resolve_mailbox = _resolve_mailbox
    agent.session_db_path = None
    agent._record_session_run = lambda **kwargs: None
    agent.sent_records = sent_records
    agent.created = created
    return agent


@pytest.mark.asyncio
async def test_email_send_delivers_existing_draft_by_id():
    agent = _send_agent()
    result = await agent._handle_send(
        SimpleNamespace(input={"draft_id": "d1846169"})
    )
    assert result.status == "completed"
    assert result.output["sent"] is True
    assert result.output["action"] == "send_email"
    assert agent.sent_records == [("d1846169", "supplied to email.send")]


@pytest.mark.asyncio
async def test_email_send_creates_and_sends_with_structured_fields():
    agent = _send_agent()
    result = await agent._handle_send(
        SimpleNamespace(
            input={
                "to_recipients": [{"email": "uspraveenraj@gmail.com"}],
                "subject": "Hourly update",
                "body": "All green.",
            }
        )
    )
    assert result.status == "completed"
    assert agent.created[0]["to_recipients"] == [
        {"email": "uspraveenraj@gmail.com", "name": None}
    ]
    assert agent.created[0]["subject"] == "Hourly update"
    assert agent.sent_records == [("draft_77", "created by email.send")]


@pytest.mark.asyncio
async def test_email_send_undelivered_is_failed_not_completed():
    agent = _send_agent(delivery={"_empty": True})
    with pytest.raises(EmailAgentError) as excinfo:
        await agent._handle_send(SimpleNamespace(input={"draft_id": "draft_1"}))
    assert excinfo.value.code == "EMAIL_SEND_NOT_COMPLETED"


@pytest.mark.asyncio
async def test_email_send_requires_fields_without_draft_id():
    agent = _send_agent()
    with pytest.raises(EmailAgentError) as excinfo:
        await agent._handle_send(SimpleNamespace(input={}))
    assert excinfo.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Honest completion in email.handle: requested send that did not happen
# ---------------------------------------------------------------------------

def test_undelivered_requested_send_becomes_failed_result():
    """The Sep 19-20 lie: send requested, draft composed, ledger said completed."""
    from shared.contracts import AgentError, AgentResult

    recorded = []

    def _record_session_run(**kwargs):
        recorded.append(kwargs)

    agent = EmailAgent.__new__(EmailAgent)
    agent._record_session_run = _record_session_run

    output = {
        "action": "compose_email",
        "sent": False,
        "delivery_status": None,
        "thread_id": None,
        "message_id": None,
        "draft_id": "draft_9",
    }
    send = True
    artifacts: list = []

    agent._record_session_run(
        task=SimpleNamespace(),
        intent="email.handle",
        mailbox_address=None,
        thread_id=None,
        message_id=None,
        summary=output,
    )
    result = AgentResult(
        status="failed" if (send and not output["sent"]) else "completed",
        output=output,
        artifacts=artifacts,
        error=AgentError(
            code="EMAIL_SEND_NOT_COMPLETED",
            message="The email was composed but not delivered.",
            retryable=True,
            next_action="retry",
        )
        if (send and not output["sent"])
        else None,
    )
    assert recorded  # session run recorded with the undelivered truth
    assert result.status == "failed"
    assert result.error.code == "EMAIL_SEND_NOT_COMPLETED"
