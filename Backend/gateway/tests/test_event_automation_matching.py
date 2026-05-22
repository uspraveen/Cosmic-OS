from __future__ import annotations

from gateway.runtime import GatewayRuntime


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

    match = runtime._score_gmail_event_automation_match(automation, event)

    assert match["confidence"] == 0
    assert match["evidence"]["hard_identity_mismatch"] is True


def test_gmail_event_automation_match_uses_sender_and_subject_evidence() -> None:
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

    match = runtime._score_gmail_event_automation_match(automation, event)

    assert match["confidence"] >= 0.78
    signal_names = {signal["name"] for signal in match["evidence"]["signals"]}
    assert "person_ref_strong" in signal_names
    assert "subject_match" in signal_names
