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

from shared import TaskEnvelope, utcnow


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
            shutil.rmtree(temp_dir)


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
        },
    }

    message = normalize_message(raw)
    assert message["message_id"] == "msg_1"
    assert message["thread_id"] == "thr_1"
    assert message["subject"] == "Launch review"
    assert message["from"] == "Alex <alex@example.com>"
    assert message["body_text"] == "Can you review this?"
    assert "UNREAD" in message["label_ids"]


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
        "gmail.triage_inbox",
        "gmail.draft_reply",
        "gmail.process_inbound",
        "gmail.heartbeat_digest",
        "gmail.morning_briefing_digest",
        "gmail.manage_prefilter",
        "gmail.recall_session",
    }.issubset(intent_names)
    for intent in card["intents"]:
        assert (schemas_dir / intent["input_schema"].split("/")[-1]).exists()
        assert (schemas_dir / intent["output_schema"].split("/")[-1]).exists()


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
            shutil.rmtree(temp_dir)
