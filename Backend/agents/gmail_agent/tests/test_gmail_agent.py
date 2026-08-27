"""Focused tests for the Gmail specialist agent."""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.contracts import TaskEnvelope, utcnow


class _StepPlanProbe:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.events: list[dict] = []

    async def create(self, steps: list[str]) -> dict:
        self.steps = [
            {"step": index, "text": text, "status": "pending", "note": None}
            for index, text in enumerate(steps, start=1)
        ]
        self.events.append({"type": "agent_plan_created", "steps": list(self.steps)})
        return {"plan_active": True, "total_steps": len(self.steps), "steps": list(self.steps)}

    async def update(self, step: int, status: str, note: str | None = None) -> dict:
        entry = self.steps[step - 1]
        entry["status"] = status
        entry["note"] = note
        self.events.append({"type": "agent_step_update", "step": step, "status": status, "note": note})
        completed = sum(1 for item in self.steps if item["status"] in {"completed", "failed", "skipped"})
        return {"step": step, "status": status, "completed": completed, "total": len(self.steps)}

    def has_pending_steps(self) -> bool:
        return any(item["status"] in {"pending", "in_progress"} for item in self.steps)


def _attach_step_plan(agent) -> _StepPlanProbe:
    probe = _StepPlanProbe()
    agent.step_plan = probe
    return probe


def test_sender_prefilter_matches_sender_and_domain() -> None:
    from agents.gmail_agent.sender_prefilter import SenderPrefilter

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        prefilter = SenderPrefilter(temp_dir / "sender_prefilter.json")
        assert prefilter.add_sender(
            "newsletter@example.com",
            reason="LLM classified repeated noise.",
            source="llm",
        )
        assert prefilter.add_domain(
            "updates.example.org",
            reason="LLM classified repeated automated updates.",
            source="llm",
        )

        sender_match = prefilter.match("Newsletter <newsletter@example.com>")
        assert sender_match is not None
        assert sender_match["type"] == "sender"
        assert sender_match["value"] == "newsletter@example.com"

        domain_match = prefilter.match("alerts@updates.example.org")
        assert domain_match is not None
        assert domain_match["type"] == "domain"
        assert domain_match["value"] == "updates.example.org"
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def test_normalize_message_extracts_thread_metadata() -> None:
    from agents.gmail_agent.google_gmail_client import normalize_message

    raw = {
        "id": "msg_1",
        "threadId": "thr_1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Can you review this?",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Launch review"},
                {"name": "From", "value": "Alex <alex@example.com>"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Date", "value": "Thu, 21 May 2026 10:00:00 -0500"},
            ],
            "body": {"data": "Q2FuIHlvdSByZXZpZXcgdGhpcz8="},
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "brief.pdf",
                    "headers": [
                        {
                            "name": "Content-Disposition",
                            "value": 'attachment; filename="brief.pdf"',
                        }
                    ],
                    "body": {"attachmentId": "att_1", "size": 1234},
                }
            ],
        },
    }

    message = normalize_message(raw)
    assert message["message_id"] == "msg_1"
    assert message["thread_id"] == "thr_1"
    assert message["subject"] == "Launch review"
    assert message["from"] == "Alex <alex@example.com>"
    assert message["body_text"] == "Can you review this?"
    assert "UNREAD" in message["label_ids"]
    assert message["has_attachments"] is True
    assert message["attachments"][0]["attachment_id"] == "att_1"
    assert message["attachments"][0]["filename"] == "brief.pdf"


def test_all_gmail_schemas_are_valid_json() -> None:
    schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
    schema_files = sorted(schemas_dir.glob("gmail.*.json"))
    assert schema_files
    for path in schema_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("type") == "object", f"Invalid root type in {path.name}"


def test_agent_card_references_all_gmail_schemas() -> None:
    yaml = pytest.importorskip("yaml")

    agent_dir = Path(__file__).resolve().parent.parent
    card = yaml.safe_load((agent_dir / "agent_card.yaml").read_text(encoding="utf-8"))
    assert card["agent_id"] == "cosmic/gmail-agent:1.0.0"
    schemas_dir = agent_dir / "schemas" / "intents"
    intent_names = {intent["name"] for intent in card["intents"]}
    assert {
        "gmail.search",
        "gmail.read_thread",
        "gmail.fetch_attachment",
        "gmail.triage_inbox",
        "gmail.draft_reply",
        "gmail.process_inbound",
        "gmail.heartbeat_digest",
        "gmail.morning_briefing_digest",
        "gmail.manage_prefilter",
        "gmail.sync_watch",
        "gmail.stop_watch",
        "gmail.recall_session",
    }.issubset(intent_names)
    for intent in card["intents"]:
        assert (schemas_dir / intent["input_schema"].split("/")[-1]).exists()
        assert (schemas_dir / intent["output_schema"].split("/")[-1]).exists()
    authz = card["policies"]["intent_authorization"]
    assert "cosmic/gateway:1.0.0" in authz["gmail.sync_watch"]
    assert "cosmic/gateway:1.0.0" in authz["gmail.stop_watch"]
    assert card["model_requirements"]["internal_llm"]["default_model_key"] == "openai:gpt-5-mini"


@pytest.mark.asyncio
async def test_gmail_search_completes_step_plan() -> None:
    from agents.gmail_agent.agent import GmailAgent

    class FakeGmailClient:
        async def search_messages(self, *, query: str, max_results: int) -> list[dict]:
            assert query == "from:n.mcgill.gardner@gmail.com"
            assert max_results == 5
            return [
                {
                    "message_id": "msg_eduardo",
                    "thread_id": "thr_eduardo",
                    "from": "Nicholas McGill-Gardner <n.mcgill.gardner@gmail.com>",
                    "to": "user@example.com",
                    "subject": "Intro: Praveen x Eduardo",
                    "date": "Wed, 27 May 2026 21:50:39 -0400",
                    "snippet": "I'd like to introduce you to Eduardo.",
                    "body_text": "Hi Ed - I'd like to introduce you to a former student.",
                    "label_ids": ["INBOX"],
                    "attachments": [],
                }
            ]

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        agent._client = MagicMock(return_value=FakeGmailClient())
        task = TaskEnvelope(
            task_id="tsk_gmail_search",
            task_list_id="sess_search",
            parent_task_id=None,
            session_id="sess_search",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.search",
            input={"query": "from:n.mcgill.gardner@gmail.com", "max_results": 5},
            input_artifacts=[],
            idempotency_key="idem_gmail_search",
            priority="normal",
            signature="sig",
            created_at=utcnow(),
            source="user",
            source_id=None,
            channel="desktop",
        )
        step_plan = _attach_step_plan(agent)

        result = await agent.handle_gmail_search(task)

        assert result.status == "completed"
        assert result.output["count"] == 1
        assert result.output["messages"][0]["thread_id"] == "thr_eduardo"
        assert agent.step_plan is not None
        assert agent.step_plan.has_pending_steps() is False
        completed_steps = [
            event["step"]
            for event in step_plan.events
            if event.get("type") == "agent_step_update"
            and event.get("status") == "completed"
        ]
        assert completed_steps == [1, 2, 3]
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_gmail_redraft_updates_single_existing_thread_draft() -> None:
    from agents.gmail_agent.agent import GmailAgent

    class FakeGmailClient:
        def __init__(self) -> None:
            self.updated: dict | None = None

        async def get_thread(self, thread_id: str) -> dict:
            assert thread_id == "thr_marat"
            message = {
                "message_id": "msg_original",
                "thread_id": thread_id,
                "from": "Marat <marat@example.com>",
                "to": "user@example.com",
                "subject": "Copper",
                "body_text": "Original thread context.",
                "message_id_header": "<original@example.com>",
                "references": "<original@example.com>",
            }
            return {
                "thread_id": thread_id,
                "messages": [message],
                "message_count": 1,
                "latest_message": message,
            }

        async def find_drafts_for_thread(self, thread_id: str) -> list[dict]:
            assert thread_id == "thr_marat"
            return [{"id": "draft_marat", "message": {"threadId": thread_id}}]

        async def get_draft(self, draft_id: str) -> dict:
            assert draft_id == "draft_marat"
            return {
                "id": draft_id,
                "message": {
                    "message_id": "msg_draft",
                    "thread_id": "thr_marat",
                    "from": "user@example.com",
                    "to": "marat@example.com",
                    "subject": "Re: Copper",
                    "body_text": "Earlier weak draft.",
                    "label_ids": ["DRAFT"],
                },
            }

        async def update_draft(self, draft_id: str, **kwargs) -> dict:
            assert draft_id == "draft_marat"
            self.updated = kwargs
            return {
                "id": draft_id,
                "message": {"id": "msg_draft", "threadId": "thr_marat"},
            }

        async def create_draft(self, **kwargs):
            raise AssertionError(f"redraft should update, not create: {kwargs}")

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        client = FakeGmailClient()
        agent._client = MagicMock(return_value=client)
        task = TaskEnvelope(
            task_id="tsk_gmail_redraft",
            task_list_id="sess_redraft",
            parent_task_id=None,
            session_id="sess_redraft",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.draft_reply",
            input={
                "request": "Redraft this into a better, concise email.",
                "thread_id": "thr_marat",
                "update_existing_draft": True,
                "to": ["marat@example.com"],
                "subject": "Re: Copper",
                "body": "A stronger draft.",
            },
            input_artifacts=[],
            idempotency_key="idem_gmail_redraft",
            priority="normal",
            signature="sig",
            created_at=utcnow(),
            source="user",
            source_id=None,
            channel="desktop",
        )
        step_plan = _attach_step_plan(agent)

        result = await agent.handle_gmail_draft_reply(task)

        assert result.status == "completed"
        assert result.output["status"] == "draft_updated"
        assert result.output["operation"] == "updated"
        assert result.output["draft_id"] == "draft_marat"
        assert result.output["approval_required"] is True
        assert client.updated is not None
        assert client.updated["body"] == "A stronger draft."
        assert client.updated["thread_id"] == "thr_marat"
        assert client.updated["in_reply_to"] == "<original@example.com>"
        assert agent.step_plan is not None
        assert agent.step_plan.has_pending_steps() is False
        assert step_plan.steps[2]["note"] == "Updated Gmail draft."
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_gmail_redraft_refuses_to_guess_between_multiple_thread_drafts() -> None:
    from agents.gmail_agent.agent import GmailAgent

    class FakeGmailClient:
        async def get_thread(self, thread_id: str) -> dict:
            return {"thread_id": thread_id, "messages": [], "message_count": 0}

        async def find_drafts_for_thread(self, thread_id: str) -> list[dict]:
            return [
                {"id": "draft_1", "message": {"threadId": thread_id}},
                {"id": "draft_2", "message": {"threadId": thread_id}},
            ]

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        agent._client = MagicMock(return_value=FakeGmailClient())
        task = TaskEnvelope(
            task_id="tsk_gmail_redraft_ambiguous",
            task_list_id="sess_redraft",
            parent_task_id=None,
            session_id="sess_redraft",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.draft_reply",
            input={
                "request": "Redraft this email.",
                "thread_id": "thr_marat",
                "update_existing_draft": True,
            },
            input_artifacts=[],
            idempotency_key="idem_gmail_redraft_ambiguous",
            priority="normal",
            signature="sig",
            created_at=utcnow(),
            source="user",
            source_id=None,
            channel="desktop",
        )

        with pytest.raises(ValueError, match="Multiple Gmail drafts exist"):
            await agent.handle_gmail_draft_reply(task)
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_gmail_internal_llm_omits_temperature_for_gpt5_and_logs_usage() -> None:
    from agents.gmail_agent.config import GmailAgentConfig
    from agents.gmail_agent.internal_llm import invoke_gmail_triage_llm

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeHttp:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def post(self, url: str, **kwargs):
            self.calls.append({"url": url, **kwargs})
            if url.endswith("/chat/completions"):
                return FakeResponse(
                    {
                        "id": "chatcmpl_test",
                        "model": "gpt-5-mini",
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 8,
                            "total_tokens": 20,
                        },
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "items": [],
                                            "summary": "No actionable mail.",
                                        }
                                    )
                                }
                            }
                        ],
                    }
                )
            return FakeResponse({"ok": True})

    cfg = GmailAgentConfig(
        gateway_url="http://gateway.local",
        gateway_internal_token="token",
        internal_llm_api_key="key",
        internal_llm_base_url="https://api.openai.com/v1",
        internal_llm_model="gpt-5-mini",
    )
    http = FakeHttp()

    result = await invoke_gmail_triage_llm(
        cfg=cfg,
        http_client=http,  # type: ignore[arg-type]
        messages=[],
        task_context={
            "task_id": "tsk_1",
            "session_id": "sess_1",
            "request_id": "req_1",
            "source": "webhook",
            "source_id": "gmail-pubsub",
            "channel": "desktop",
        },
    )

    assert result["summary"] == "No actionable mail."
    chat_call = http.calls[0]
    assert "temperature" not in chat_call["json"]
    usage_call = http.calls[1]
    assert usage_call["url"] == "http://gateway.local/internal/usage/log"
    assert usage_call["json"]["source_id"] == "cosmic/gmail-agent:1.0.0"
    assert usage_call["json"]["provider"] == "openai"
    assert usage_call["json"]["model"] == "gpt-5-mini"
    assert usage_call["json"]["prompt_tokens"] == 12
    assert usage_call["json"]["completion_tokens"] == 8


@pytest.mark.asyncio
async def test_fetch_attachment_writes_private_artifact() -> None:
    from agents.gmail_agent.agent import GmailAgent

    class FakeGmailClient:
        async def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
            assert message_id == "msg_1"
            assert attachment_id == "att_1"
            return b"hello attachment"

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(
            redis_client=MagicMock(),
            store_root=temp_dir / "store",
            artifacts_root=temp_dir / "artifacts",
        )
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        agent._client = MagicMock(return_value=FakeGmailClient())
        task = TaskEnvelope(
            task_id="tsk_gmail_attachment",
            task_list_id="sess_attachment",
            parent_task_id=None,
            session_id="sess_attachment",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.fetch_attachment",
            input={
                "message_id": "msg_1",
                "attachment_id": "att_1",
                "filename": "../brief.pdf",
                "mime_type": "application/pdf",
            },
            input_artifacts=[],
            idempotency_key="idem_gmail_attachment",
            priority="normal",
            signature="sig",
            created_at=utcnow(),
            source="user",
            source_id=None,
            channel="desktop",
        )
        _attach_step_plan(agent)

        result = await agent.handle_gmail_fetch_attachment(task)

        assert result.status == "completed"
        assert result.artifacts
        assert agent.step_plan is not None
        assert agent.step_plan.has_pending_steps() is False
        artifact = result.artifacts[0]
        assert artifact.mime == "application/pdf"
        assert artifact.created_by_agent == "cosmic/gmail-agent:1.0.0"
        assert artifact.path.endswith("brief.pdf")
        assert (temp_dir / "artifacts" / task.task_id / "gmail_agent" / "brief.pdf").exists()
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_memory_context_query_uses_sender_subject_and_snippet() -> None:
    from agents.gmail_agent.agent import GmailAgent

    class FakeMemoryRead:
        def __init__(self) -> None:
            self.query = ""

        async def search(self, query: str, max_results: int = 6) -> dict:
            self.query = query
            return {
                "results": [
                    {
                        "title": "YC S26 application",
                        "content": "User is waiting for YC S26 interview invite.",
                    }
                ]
            }

        async def close(self) -> None:
            return None

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        fake_memory = FakeMemoryRead()
        agent.memory_read = fake_memory
        context = await agent._memory_context_for_messages(
            [
                {
                    "from": "Y Combinator <interviews@ycombinator.com>",
                    "from_email": "interviews@ycombinator.com",
                    "subject": "YC S26 interview invite",
                    "snippet": "Schedule your interview for the Summer 2026 batch.",
                }
            ]
        )

        assert "YC S26 application" in context
        assert "interviews@ycombinator.com" in fake_memory.query
        assert "YC S26 interview invite" in fake_memory.query
        assert "Schedule your interview" in fake_memory.query
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_heartbeat_digest_uses_cached_triage_without_live_llm() -> None:
    from agents.gmail_agent.agent import GmailAgent

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        agent.auth = {
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
            "account_label": "Personal Gmail",
        }
        await agent.on_startup()
        agent._record_triage_decisions(
            [
                {
                    "message_id": "msg_cached_1",
                    "thread_id": "thr_cached_1",
                    "sender": "Founder <founder@example.com>",
                    "sender_email": "founder@example.com",
                    "sender_domain": "example.com",
                    "subject": "YC update",
                    "date": "Thu, 21 May 2026 10:00:00 -0500",
                    "snippet": "Can you send the deck?",
                    "category": "needs_reply",
                    "confidence": 0.91,
                    "priority": 88,
                    "surface_to_user": True,
                    "reason": "Relevant founder follow-up.",
                    "suggested_action": "Reply with the latest deck.",
                }
            ],
            source="process_inbound",
        )

        task = TaskEnvelope(
            task_id="tsk_gmail_heartbeat",
            task_list_id="sess_heartbeat",
            parent_task_id=None,
            session_id="sess_heartbeat",
            sender="cosmic/gateway:1.0.0",
            recipient="cosmic/gmail-agent:1.0.0",
            intent="gmail.heartbeat_digest",
            input={"max_items": 4, "lookback_hours": 24, "allow_live_check": False},
            input_artifacts=[],
            idempotency_key="idem_gmail_heartbeat",
            priority="low",
            signature="sig",
            created_at=utcnow(),
            source="heartbeat",
            source_id="default",
            channel="desktop",
        )

        with patch.object(
            agent,
            "handle_gmail_triage_inbox",
            AsyncMock(side_effect=AssertionError("heartbeat must not live-triage")),
        ):
            result = await agent.handle_gmail_heartbeat_digest(task)

        assert result.status == "completed"
        assert result.output["reason"] == "cached_triage_reconciliation"
        assert result.output["live_triage_used"] is False
        assert result.output["llm_used"] is False
        assert result.output["items"][0]["message_id"] == "msg_cached_1"
        assert result.output["items"][0]["account_email"] == "user@example.com"
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Draft provenance: who wrote the body, and who chose the recipient.
#
# On 2026-08-25 a follow-up that COSMIC had already written -- addressed to the
# account owner and signed "COSMIC" -- was delegated with a body and a thread_id
# but no `to`. The verbatim path required both, so the finished email fell
# through to the drafting model as a *prompt*, alongside the Chase alert thread
# that happened to be in context. The model read the email as the incoming
# message, answered it in the owner's voice, and addressed the reply to
# no.reply.alerts@chase.com. These tests pin both halves of that failure.
# ---------------------------------------------------------------------------


class _DraftProbeClient:
    """Minimal Gmail client that records the draft it was asked to create."""

    def __init__(self, thread: dict) -> None:
        self._thread = thread
        self.created: dict | None = None

    async def get_thread(self, thread_id: str) -> dict:
        return self._thread

    async def create_draft(self, **kwargs) -> dict:
        if not kwargs.get("to") and not kwargs.get("cc") and not kwargs.get("bcc"):
            raise AssertionError("create_draft called with no recipient")
        self.created = dict(kwargs)
        return {"id": "draft_probe", "message": {"id": "msg_probe", "threadId": kwargs.get("thread_id")}}


def _thread_from(sender: str, *, thread_id: str = "thr_probe") -> dict:
    return {
        "thread_id": thread_id,
        "message_count": 1,
        "messages": [
            {
                "message_id": "msg_inbound",
                "thread_id": thread_id,
                "from": sender,
                "to": "user@example.com",
                "subject": "Your available balance is below your limit",
                "snippet": "Balance alert",
                "body": "Your account balance is low.",
                "message_id_header": "<alert@bank.example>",
                "references": "",
            }
        ],
    }


def _draft_task(task_input: dict, *, task_id: str) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=task_id,
        task_list_id="sess_draft_provenance",
        parent_task_id=None,
        session_id="sess_draft_provenance",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/gmail-agent:1.0.0",
        intent="gmail.draft_reply",
        input=task_input,
        input_artifacts=[],
        idempotency_key=f"idem_{task_id}",
        priority="normal",
        signature="sig",
        created_at=utcnow(),
        source="cron",
        source_id="cron_probe",
        channel="desktop",
    )


async def _run_draft(task_input: dict, *, thread: dict, task_id: str, llm=None):
    """Build an agent, run gmail.draft_reply, and hand back the client + result."""
    from agents.gmail_agent.agent import GmailAgent

    temp_dir = Path(__file__).resolve().parent / f".tmp_{uuid.uuid4().hex}"
    agent = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=False)
        agent = GmailAgent(redis_client=MagicMock(), store_root=temp_dir / "store")
        agent.auth = {
            "access_token": "token",
            "account_id": "acct_gmail_1",
            "account_email": "user@example.com",
        }
        await agent.on_startup()
        agent.memory_read = None
        client = _DraftProbeClient(thread)
        agent._client = MagicMock(return_value=client)
        task = _draft_task(task_input, task_id=task_id)

        llm_mock = AsyncMock(side_effect=llm) if callable(llm) else AsyncMock(return_value=llm)
        with patch("agents.gmail_agent.agent.invoke_gmail_draft_llm", llm_mock) as patched:
            try:
                result = await agent.handle_gmail_draft_reply(task)
                error = None
            except Exception as exc:  # surfaced to the caller for assertion
                result = None
                error = exc
        return client, result, error, patched
    finally:
        if agent is not None:
            await agent.stop()
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_explicit_body_is_never_handed_to_the_drafting_model() -> None:
    """A finished body is content, not a prompt -- even with no recipient attached."""
    client, result, error, llm = await _run_draft(
        {
            "thread_id": "thr_probe",
            "subject": "Follow-up: still overdrawn",
            "body": "Praveen -- following up on my earlier note.\n\n-- Cosmic",
        },
        thread=_thread_from("Priya <priya@example.com>"),
        task_id="tsk_explicit_body_no_to",
    )

    assert error is None, error
    assert result is not None and result.status == "completed"
    llm.assert_not_awaited()
    assert client.created is not None
    # The words the caller wrote survive byte for byte, in the caller's voice.
    assert client.created["body"] == "Praveen -- following up on my earlier note.\n\n-- Cosmic"
    # With no recipient given, replying to the thread's correspondent is correct.
    assert client.created["to"] == ["priya@example.com"]


@pytest.mark.asyncio
async def test_draft_is_refused_when_the_inferred_recipient_is_send_only() -> None:
    """The actual 2026-08-25 failure: the thread in context belonged to a bank alert."""
    client, result, error, llm = await _run_draft(
        {
            "thread_id": "1a0385eeea7db766",
            "subject": "Follow-up: Chase 8807 still overdrawn",
            "body": "Praveen -- the account is still overdrawn.\n\n-- Cosmic",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>", thread_id="1a0385eeea7db766"),
        task_id="tsk_inferred_send_only",
    )

    assert isinstance(error, ValueError), error
    assert "no.reply.alerts@chase.com" in str(error)
    assert "send-only" in str(error)
    # Nothing reached Gmail: a loud failure beats a plausible wrong draft.
    assert client.created is None
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_recipient_is_not_re_addressed_by_the_drafting_model() -> None:
    """The model may improve the words; it does not get to change who they go to."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "request": "Reply confirming receipt.",
            "to": ["uspraveenraj@gmail.com"],
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>"),
        task_id="tsk_explicit_to_wins",
        llm={
            "subject": "Re: alert",
            "body": "Confirmed.",
            "to": ["no.reply.alerts@chase.com"],
            "notes": "Replying to the thread sender.",
        },
    )

    assert error is None, error
    assert result is not None and result.status == "completed"
    assert client.created is not None
    assert client.created["to"] == ["uspraveenraj@gmail.com"]


@pytest.mark.asyncio
async def test_model_invented_send_only_recipient_is_refused() -> None:
    """A recipient the model chose is a guess, and this guess is never right."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "request": "Reply to this alert.",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>"),
        task_id="tsk_model_send_only",
        llm={
            "subject": "Re: alert",
            "body": "Got it.",
            "to": ["no-reply@chase.com"],
            "notes": "No recipient email was provided, so 'to' is left empty.",
        },
    )

    assert isinstance(error, ValueError), error
    assert "send-only" in str(error)
    assert client.created is None


@pytest.mark.asyncio
async def test_a_trusted_to_does_not_launder_a_model_invented_cc() -> None:
    """Trust is per field. Naming the `to` says nothing about a cc the model added."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "request": "Reply confirming receipt.",
            "to": ["uspraveenraj@gmail.com"],
        },
        thread=_thread_from("Priya <priya@example.com>"),
        task_id="tsk_model_cc_send_only",
        llm={
            "subject": "Re: alert",
            "body": "Confirmed.",
            "to": ["uspraveenraj@gmail.com"],
            "cc": ["no-reply@chase.com"],
            "notes": "Copied the alert sender.",
        },
    )

    assert isinstance(error, ValueError), error
    assert "'cc'" in str(error)
    assert client.created is None


@pytest.mark.asyncio
async def test_caller_may_still_address_a_send_only_mailbox_deliberately() -> None:
    """The guard is about guesses. An explicit recipient is the caller's call."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "to": ["no-reply@chase.com"],
            "subject": "Unsubscribe",
            "body": "Please stop sending these.",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>"),
        task_id="tsk_explicit_send_only_allowed",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["to"] == ["no-reply@chase.com"]


@pytest.mark.asyncio
async def test_body_with_no_recipient_and_no_thread_fails_clearly() -> None:
    """Refusing to guess is the point; the message has to say what to pass."""
    client, result, error, llm = await _run_draft(
        {"subject": "Hello", "body": "A message with nowhere to go."},
        thread={"thread_id": "thr_probe", "messages": []},
        task_id="tsk_body_no_recipient_no_thread",
    )

    assert isinstance(error, ValueError), error
    assert "Pass 'to' explicitly" in str(error)
    assert client.created is None
    llm.assert_not_awaited()


def test_send_only_mailbox_detection_covers_punctuation_variants() -> None:
    from agents.gmail_agent.agent import GmailAgent

    send_only = [
        "no.reply.alerts@chase.com",
        "no-reply@github.com",
        "noreply@google.com",
        "do-not-reply@amazon.com",
        "DoNotReply@Contoso.com",
        "Chase <no.reply.alerts@chase.com>",
        "mailer-daemon@googlemail.com",
        "postmaster@example.com",
    ]
    for address in send_only:
        assert GmailAgent._is_send_only_mailbox(address) is True, address

    # Real people and real support desks must keep working.
    addressable = [
        "uspraveenraj@gmail.com",
        "support@jlcpcb.com",
        "finn@jlcpcb.com",
        "notifications@github.com",
        "reply@intercom.io",
        "Praveen Raj <uspraveenraj@gmail.com>",
        "",
    ]
    for address in addressable:
        assert GmailAgent._is_send_only_mailbox(address) is False, address

# ---------------------------------------------------------------------------
# A note to the mailbox owner is never a reply to a third party.
#
# 2026-08-27 03:02: a Chase "Insufficient Funds Notice" arrived and a standing
# automation drafted a correct, correctly-addressed notice to the account owner
# -- and filed it inside Chase's thread (1a0412949cb9e219), because the caller
# passed the triggering alert's thread_id. The agent card asked callers not to
# do that. Guidance is not enforcement, so the rule lives in the agent now.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_notice_to_the_owner_is_detached_from_a_third_party_thread() -> None:
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "1a0412949cb9e219",
            "to": "user@example.com",
            "subject": "Chase 8807 overdrawn (-$3.80) + NSF notice posted",
            "body": "Praveen,\n\nYour Chase checking account is still overdrawn.\n\n- Cosmic",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>", thread_id="1a0412949cb9e219"),
        task_id="tsk_self_notice_detached",
    )

    assert error is None, error
    assert result is not None and result.status == "completed"
    assert client.created is not None
    # The message itself is untouched -- recipient, subject and body all correct.
    assert client.created["to"] == ["user@example.com"]
    assert "still overdrawn" in client.created["body"]
    # Only its placement changes: it is a new message, not a reply to the bank.
    assert client.created["thread_id"] is None
    assert client.created["in_reply_to"] is None
    assert client.created["references"] is None
    assert "does not belong in another correspondent's thread" in result.output["notes"]


@pytest.mark.asyncio
async def test_a_real_reply_to_a_correspondent_keeps_its_thread() -> None:
    """The guard must not touch ordinary replies -- that is most of what this does."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "to": "priya@example.com",
            "subject": "Re: lunch",
            "body": "Works for me.",
        },
        thread=_thread_from("Priya <priya@example.com>"),
        task_id="tsk_real_reply_keeps_thread",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["thread_id"] == "thr_probe"
    assert client.created["in_reply_to"] == "<alert@bank.example>"


@pytest.mark.asyncio
async def test_owner_copied_alongside_a_third_party_keeps_its_thread() -> None:
    """Only a message addressed *solely* to the owner is a self-notification."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_probe",
            "to": "priya@example.com",
            "cc": "user@example.com",
            "subject": "Re: lunch",
            "body": "Works for me.",
        },
        thread=_thread_from("Priya <priya@example.com>"),
        task_id="tsk_owner_cc_keeps_thread",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["thread_id"] == "thr_probe"


@pytest.mark.asyncio
async def test_a_thread_the_owner_is_alone_in_is_left_alone() -> None:
    """Owner-to-owner inside the owner's own thread is a genuine self-thread."""
    own_thread = _thread_from("Praveen <user@example.com>", thread_id="thr_self")
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "thr_self",
            "to": "user@example.com",
            "subject": "Re: my notes",
            "body": "One more thought.",
        },
        thread=own_thread,
        task_id="tsk_self_thread_kept",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["thread_id"] == "thr_self"


@pytest.mark.asyncio
async def test_detaching_drops_a_reply_subject_the_agent_added_itself() -> None:
    """A detached message should not still be titled as a reply."""
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "1a0412949cb9e219",
            "to": "user@example.com",
            "body": "Praveen, heads up.",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>", thread_id="1a0412949cb9e219"),
        task_id="tsk_detach_strips_re",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["thread_id"] is None
    assert not client.created["subject"].lower().startswith("re: ")
    assert client.created["subject"] == "Your available balance is below your limit"


@pytest.mark.asyncio
async def test_a_caller_supplied_subject_is_never_rewritten_when_detaching() -> None:
    client, result, error, _ = await _run_draft(
        {
            "thread_id": "1a0412949cb9e219",
            "to": "user@example.com",
            "subject": "Re: something the caller meant literally",
            "body": "Praveen, heads up.",
        },
        thread=_thread_from("Chase <no.reply.alerts@chase.com>", thread_id="1a0412949cb9e219"),
        task_id="tsk_detach_keeps_explicit_subject",
    )

    assert error is None, error
    assert client.created is not None
    assert client.created["subject"] == "Re: something the caller meant literally"
