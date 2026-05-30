from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gateway.runtime import GatewayRuntime
from gateway.memory.client import MemoryPromptContext


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


def test_gmail_attachment_refs_use_event_message_id_and_dedupe() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    event = {
        "message_id": "msg_1",
        "attachments": [
            {
                "attachment_id": "att_1",
                "filename": "brief.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            {
                "attachment_id": "att_1",
                "filename": "brief.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            {"attachment_id": "att_2", "message_id": "msg_2", "filename": "notes.pdf"},
            {"filename": "metadata-only.pdf"},
        ],
    }

    refs = runtime._gmail_attachment_refs(event)

    assert refs == [
        {
            "message_id": "msg_1",
            "attachment_id": "att_1",
            "filename": "brief.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": None,
        },
        {
            "message_id": "msg_2",
            "attachment_id": "att_2",
            "filename": "notes.pdf",
            "mime_type": None,
            "size": None,
        },
    ]


def test_gmail_fetched_attachment_manifest_marks_spreadsheets_as_inputs() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.config = type(
        "Config",
        (),
        {"artifacts_root": Path("C:/tmp/cosmic-artifacts")},
    )()
    event = {
        "thread_id": "thr_1",
        "account_id": "acct_1",
        "account_email": "user@example.com",
    }
    ref = {
        "message_id": "msg_1",
        "attachment_id": "att_1",
        "filename": "sheet.csv",
        "mime_type": "text/csv",
    }
    result = {
        "status": "completed",
        "task_id": "tsk_fetch",
        "output": {
            "filename": "sheet.csv",
            "mime_type": "text/csv",
            "artifact": {
                "artifact_id": "art_gmail_raw",
                "task_id": "tsk_fetch",
                "mime": "text/csv",
                "sha256": "abc",
                "path": "runs/artifacts/tsk_fetch/gmail_agent/sheet.csv",
            },
        },
    }

    manifest = runtime._gmail_fetched_attachment_manifest(
        request_id="req_evt_test",
        index=1,
        event=event,
        ref=ref,
        result=result,
    )

    assert manifest is not None
    assert manifest["kind"] == "spreadsheet"
    assert manifest["ingest_state"] == "staged"
    assert manifest["path"] == "runs/artifacts/tsk_fetch/gmail_agent/sheet.csv"
    assert manifest["gmail_attachment_id"] == "att_1"
    assert manifest["source_artifact_id"] == "art_gmail_raw"


def test_event_automation_request_record_carries_fetched_gmail_artifacts() -> None:
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.request_records = {}

    class FakeRoutingAuditStore:
        def __init__(self) -> None:
            self.rows = []

        def append(self, **kwargs):
            self.rows.append(kwargs)

    runtime.routing_audit_store = FakeRoutingAuditStore()
    runtime._current_session_id = lambda: "sess_test"
    runtime._ensure_session_state_seeded = lambda session_id: {}
    runtime._compose_prompt_context = (
        lambda *, active_working_set, memory_context: memory_context
    )

    async def assemble_memory_prompt_context(*, query: str):
        assert "Fetched attachment artifacts" in query
        return MemoryPromptContext(rendered="memory context")

    async def fetch_gmail_attachment_input_artifacts(**kwargs):
        assert kwargs["request_id"].startswith("req_evt_")
        return [
            {
                "artifact_id": "art_gmail_input_1",
                "kind": "spreadsheet",
                "mime": "text/csv",
                "mime_type": "text/csv",
                "filename": "request.csv",
                "path": "runs/artifacts/tsk_fetch/gmail_agent/request.csv",
                "ingest_state": "staged",
            }
        ]

    runtime._assemble_memory_prompt_context = assemble_memory_prompt_context
    runtime._fetch_gmail_attachment_input_artifacts = (
        fetch_gmail_attachment_input_artifacts
    )

    automation = {
        "automation_id": "evt_attachments",
        "raw_instruction": "When Arun emails a file, create the requested sheet.",
        "action": {"goal": "Create the requested Google Sheet from the email file."},
    }
    event = {
        "event_ref": "gmail:acct:msg_1",
        "account_email": "user@example.com",
        "sender": "Arun <arun@example.com>",
        "subject": "Create this sheet",
        "message_id": "msg_1",
        "attachments": [
            {
                "attachment_id": "att_1",
                "filename": "request.csv",
                "mime_type": "text/csv",
            }
        ],
    }
    match = {"match_id": "match_1", "decision": "matched", "confidence": 0.95}

    record = asyncio.run(
        runtime._build_event_automation_request_record(
            automation=automation,
            event=event,
            match=match,
        )
    )

    assert record["input_artifacts"][0]["artifact_id"] == "art_gmail_input_1"
    assert "TaskEnvelope.input_artifacts" in record["message"]["content"]
    assert record["message"]["metadata"]["input_artifact_ids"] == [
        "art_gmail_input_1"
    ]
