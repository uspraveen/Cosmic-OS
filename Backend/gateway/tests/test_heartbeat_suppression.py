from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime
from gateway.runtime import GMAIL_SURFACE_DECISION_SOURCE


def _runtime() -> GatewayRuntime:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_heartbeat_test": {
            "source": "heartbeat",
        }
    }
    return runtime


def test_heartbeat_ok_with_process_narration_is_suppressed() -> None:
    runtime = _runtime()

    decision = runtime._parse_heartbeat_decision(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": "I'll do a quick check for new YC chatter.\nheartbeat_ok",
        }
    )

    assert decision["decision"] == "suppress"
    assert decision["raw_format"] == "legacy_token"


def test_heartbeat_ok_embedded_in_other_word_is_not_suppressed() -> None:
    runtime = _runtime()

    decision = runtime._parse_heartbeat_decision(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": "heartbeat_okay, here is a real note.",
        }
    )

    assert decision["decision"] == "suppress"
    assert decision["reason"] == "invalid_heartbeat_decision_envelope"


def test_heartbeat_structured_suppress_decision_is_suppressed() -> None:
    runtime = _runtime()

    decision = runtime._parse_heartbeat_decision(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": '{"decision":"suppress","message":"","reason":"nothing changed","confidence":0.8,"pending_checks":[],"notes":""}',
        }
    )

    assert decision["decision"] == "suppress"
    assert decision["reason"] == "nothing changed"
    assert decision["raw_format"] == "json"


def test_heartbeat_structured_deliver_decision_extracts_message() -> None:
    runtime = _runtime()

    decision = runtime._parse_heartbeat_decision(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": '{"decision":"deliver","message":"Your portfolio site is down after the VM restart.","reason":"active project health issue","confidence":0.94,"pending_checks":[],"notes":""}',
        }
    )

    assert decision["decision"] == "deliver"
    assert decision["message"] == "Your portfolio site is down after the VM restart."


def test_heartbeat_malformed_structured_output_is_suppressed() -> None:
    runtime = _runtime()

    decision = runtime._parse_heartbeat_decision(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": "I checked a thing and maybe this matters.",
        }
    )

    assert decision["decision"] == "suppress"
    assert decision["reason"] == "invalid_heartbeat_decision_envelope"


def test_gmail_surface_decision_deliver_extracts_user_message() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_gmail_test": {
            "source": GMAIL_SURFACE_DECISION_SOURCE,
        }
    }

    decision = runtime._parse_gmail_surface_decision(
        {
            "type": "response.complete",
            "request_id": "req_gmail_test",
            "content": '{"decision":"deliver","message":"PearX sent a decision email about LearnChain. Want me to open it and help decide the next reply?","reason":"investor/application outcome","confidence":0.96,"notes":""}',
        }
    )

    assert decision["decision"] == "deliver"
    assert decision["message"].startswith("PearX sent a decision email")


def test_gmail_surface_decision_invalid_output_is_suppressed() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_gmail_test": {
            "source": GMAIL_SURFACE_DECISION_SOURCE,
        }
    }

    decision = runtime._parse_gmail_surface_decision(
        {
            "type": "response.complete",
            "request_id": "req_gmail_test",
            "content": "I should probably tell the user.",
        }
    )

    assert decision["decision"] == "suppress"
    assert decision["reason"] == "invalid_gmail_surface_decision_envelope"


def test_gmail_surface_decision_uses_valid_task_envelope_source() -> None:
    runtime = object.__new__(GatewayRuntime)

    assert (
        runtime._task_envelope_source({"source": GMAIL_SURFACE_DECISION_SOURCE})
        == "webhook"
    )
    assert runtime._task_envelope_source({"source": "heartbeat"}) == "heartbeat"
    assert runtime._task_envelope_source({"source": "unknown_private_source"}) == "user"
