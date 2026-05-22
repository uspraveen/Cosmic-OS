from __future__ import annotations

import asyncio
import json

from gateway.runtime import GatewayRuntime


class FakeHaikuAdapter:
    api_key = "test"
    model = "claude-test"

    async def generate_text(self, **kwargs):
        return (
            json.dumps(
                {
                    "decision": "matched",
                    "confidence": 0.93,
                    "reason": "Sender and subject clearly satisfy the instruction.",
                    "evidence": [
                        "Sender display name is Arun.",
                        "Subject asks for the portfolio document.",
                    ],
                    "identity": {"resolved": True, "basis": "sender display/email"},
                    "safety": {
                        "requires_user_approval": True,
                        "approval_reason": "External delivery would require approval.",
                    },
                }
            ),
            {},
            "end_turn",
        )


def test_gmail_event_automation_semantic_match_accepts_llm_decision() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.haiku_adapter = FakeHaikuAdapter()
    automation = {
        "automation_id": "evt_123",
        "raw_instruction": "When Arun emails about the portfolio, prepare the requested doc.",
        "condition": {
            "person_ref": "Arun",
            "subject_contains": "portfolio",
            "resolution_mode": "resolve_on_event",
        },
        "action": {"goal": "Prepare the requested doc."},
    }
    event = {
        "event_type": "gmail.inbound",
        "event_ref": "gmail:test:msg_1",
        "sender": "Arun Kumar <arun@example.com>",
        "sender_email": "arun@example.com",
        "sender_domain": "example.com",
        "subject": "Portfolio request",
        "snippet": "Can you prepare this?",
    }

    match = asyncio.run(
        runtime._adjudicate_gmail_event_automation_match(automation, event)
    )

    assert match is not None
    assert match["decision"] == "matched"
    assert match["confidence"] == 0.93
    assert match["evidence"]["matcher"] == "llm"


def test_gmail_event_automation_match_blocks_exact_sender_mismatch() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    automation = {
        "raw_instruction": "When Arun emails about the portfolio, prepare the requested doc.",
        "condition": {
            "sender_email": "arun@example.com",
            "subject_contains": "portfolio",
        },
    }
    event = {
        "sender": "Someone Else <other@example.com>",
        "sender_email": "other@example.com",
        "sender_domain": "example.com",
        "subject": "Portfolio request",
        "snippet": "Can you prepare this?",
    }

    match = runtime._fallback_gmail_event_automation_match(automation, event)

    assert match["confidence"] == 0
    assert match["decision"] == "ignore"
    assert match["evidence"]["hard_identity_mismatch"] is True


def test_gmail_event_automation_fallback_uses_sender_and_subject_evidence() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    automation = {
        "raw_instruction": "When Arun emails about the portfolio, prepare the requested doc.",
        "condition": {
            "person_ref": "Arun",
            "subject_contains": "portfolio",
            "resolution_mode": "resolve_on_event",
        },
    }
    event = {
        "sender": "Arun Kumar <arun@example.com>",
        "sender_email": "arun@example.com",
        "sender_domain": "example.com",
        "subject": "Portfolio request",
        "snippet": "Can you prepare this?",
    }

    match = runtime._fallback_gmail_event_automation_match(automation, event)

    assert match["confidence"] >= 0.78
    signal_names = {signal["name"] for signal in match["evidence"]["signals"]}
    assert "person_ref_strong" in signal_names
    assert "subject_match" in signal_names
