from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime
from gateway.runtime import ActiveRequest
from gateway.runtime import GMAIL_SURFACE_DECISION_SOURCE
from gateway.runtime import SYSTEM_CRON_WEEKLY_MY_TOOLS_REVIEW
from gateway.scheduler import SchedulerStore
from gateway.session_store import SessionStore


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


def test_historical_weekly_decision_envelope_hydrates_as_user_message() -> None:
    runtime = object.__new__(GatewayRuntime)
    note = "Weekly My Tools review found one live CRM link worth checking."
    payload = json.dumps(
        {
            "decision": "deliver",
            "message": note,
            "reason": "weekly_review_test",
            "confidence": 0.9,
            "notes": "",
        }
    )

    hydrated = runtime._hydrate_history_message_for_client(
        {
            "role": "assistant",
            "content": payload,
            "metadata": {
                "source": "cron",
                "source_id": SYSTEM_CRON_WEEKLY_MY_TOOLS_REVIEW,
                "response_blocks": [
                    {
                        "id": "markdown_1",
                        "type": "markdown",
                        "text": payload,
                    }
                ],
            },
        }
    )

    assert hydrated["content"] == note
    response_blocks = hydrated["metadata"]["response_blocks"]
    assert response_blocks == [
        {
            "id": "markdown_1",
            "type": "markdown",
            "text": note,
        }
    ]


def test_gmail_surface_decision_uses_valid_task_envelope_source() -> None:
    runtime = object.__new__(GatewayRuntime)

    assert (
        runtime._task_envelope_source({"source": GMAIL_SURFACE_DECISION_SOURCE})
        == "webhook"
    )
    assert runtime._task_envelope_source({"source": "heartbeat"}) == "heartbeat"
    assert runtime._task_envelope_source({"source": "unknown_private_source"}) == "user"


def test_autonomous_gmail_surface_backgrounds_when_foreground_user_response_active() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.active_requests = {
        "req_user": ActiveRequest(
            request_id="req_user",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="user",
            foreground=True,
        )
    }

    assert runtime._should_background_autonomous_request(
        {
            "request_id": "req_gmail",
            "session_id": "sess_1",
            "channel": "desktop",
            "source": GMAIL_SURFACE_DECISION_SOURCE,
            "source_id": "gmail_surface:abc",
        }
    )


def test_autonomous_event_automation_backgrounds_when_foreground_user_response_active() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.active_requests = {
        "req_user": ActiveRequest(
            request_id="req_user",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="user",
            foreground=True,
        )
    }

    assert runtime._should_background_autonomous_request(
        {
            "request_id": "req_event",
            "session_id": "sess_1",
            "channel": "desktop",
            "source": "webhook",
            "source_id": "event_automation:auto_123",
        }
    )


def test_autonomous_request_backgrounds_across_sessions_on_same_visible_channel() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.active_requests = {
        "req_user": ActiveRequest(
            request_id="req_user",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="user",
            foreground=True,
        )
    }

    assert runtime._should_background_autonomous_request(
        {
            "request_id": "req_heartbeat",
            "session_id": "sess_2",
            "channel": "desktop",
            "source": "heartbeat",
            "source_id": "default",
        }
    )
    assert not runtime._should_background_autonomous_request(
        {
            "request_id": "req_heartbeat",
            "session_id": "sess_1",
            "channel": "mobile:device_1",
            "source": "heartbeat",
            "source_id": "default",
        }
    )


def test_user_and_plain_webhook_records_do_not_auto_background() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.active_requests = {
        "req_user": ActiveRequest(
            request_id="req_user",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="user",
            foreground=True,
        )
    }

    assert not runtime._should_background_autonomous_request(
        {
            "request_id": "req_next_user",
            "session_id": "sess_1",
            "channel": "desktop",
            "source": "user",
        }
    )
    assert not runtime._should_background_autonomous_request(
        {
            "request_id": "req_webhook",
            "session_id": "sess_1",
            "channel": "desktop",
            "source": "webhook",
            "source_id": "generic-webhook",
        }
    )


def test_user_request_can_background_autonomous_foreground_on_same_channel() -> None:
    runtime = object.__new__(GatewayRuntime)
    events: list[dict] = []

    async def fake_deliver(event: dict, *, channel: str | None = None) -> str:
        events.append({**event, "delivered_channel": channel})
        return "sent"

    def fake_track(coroutine) -> None:
        asyncio.run(coroutine)

    runtime._deliver_or_queue_channel_event = fake_deliver
    runtime._track_background_task = fake_track
    runtime.active_requests = {
        "req_heartbeat": ActiveRequest(
            request_id="req_heartbeat",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="heartbeat",
            foreground=True,
            partial_content="Checking YC chatter...",
        ),
        "req_user": ActiveRequest(
            request_id="req_user",
            session_id="sess_1",
            channel="desktop",
            route="opus",
            source="user",
            foreground=True,
        ),
    }

    runtime._background_autonomous_foreground_requests_for_channel("desktop")

    assert not runtime.active_requests["req_heartbeat"].foreground
    assert runtime.active_requests["req_heartbeat"].backgrounded_at
    assert runtime.active_requests["req_user"].foreground
    assert events[0]["type"] == "task.backgrounded"
    assert events[0]["request_id"] == "req_heartbeat"


def test_heartbeat_recent_delivery_facts_include_completed_mobile_cron_across_rollover(tmp_path) -> None:
    runtime = object.__new__(GatewayRuntime)
    scheduler_store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler_store.initialize(default_timezone="America/Chicago")
    session_store = SessionStore(tmp_path / "sessions.db")
    session_store.initialize()
    runtime.scheduler_store = scheduler_store
    runtime.session_store = session_store

    scheduled_for = "2026-06-16T13:20:00Z"
    scheduler_store.upsert_cron(
        cron_id="cron_todo",
        name="Todo List - June 16",
        kind="reminder",
        description=None,
        cron_expr="20 8 16 6 *",
        timezone_name="America/Chicago",
        next_fire_at=scheduled_for,
        metadata={
            "one_shot": True,
            "delivery_channel": "mobile:mob_123",
            "delivery_target": "mobile:mob_123",
        },
    )
    scheduler_store.record_cron_result(
        cron_id="cron_todo",
        scheduled_for=scheduled_for,
        status="completed",
        summary="Reminder ran: Todo List - June 16",
        next_fire_at=None,
    )
    request_id, _ = runtime._cron_execution_identity("cron_todo", scheduled_for)
    message_id = session_store.append_message(
        "sess_20260616",
        role="assistant",
        content="Good morning. Here's your list for today.",
        channel="mobile:mob_123",
        metadata={
            "request_id": request_id,
            "source": "cron",
            "source_id": "cron_todo",
        },
    )

    items = runtime._build_recent_user_visible_deliveries(
        now=datetime.now(timezone.utc),
    )

    assert len(items) == 1
    item = items[0]
    assert item["label"] == "Todo List - June 16"
    assert item["scheduled_for"] == scheduled_for
    assert item["delivery_channel"] == "mobile:mob_123"
    assert item["result_status"] == "completed"
    assert item["state_for_heartbeat"] == "completed_and_stored"
    assert item["evidence"] == "session_message"
    assert item["message_id"] == message_id


def test_heartbeat_context_renders_delivery_facts_before_stale_notes() -> None:
    runtime = object.__new__(GatewayRuntime)

    block = runtime._render_heartbeat_context_block(
        {
            "current_session_id": "sess_20260617",
            "user_timezone": "America/Chicago",
            "recent_user_visible_deliveries": [
                {
                    "source": "cron",
                    "label": "Todo List - June 16",
                    "scheduled_for": "2026-06-16T13:20:00Z",
                    "delivered_at": "2026-06-16T13:20:17Z",
                    "delivery_channel": "mobile:mob_123",
                    "result_status": "completed",
                    "state_for_heartbeat": "completed_and_stored",
                    "evidence": "session_message",
                    "summary": "Reminder ran: Todo List - June 16",
                }
            ],
            "heartbeat_notes": "User offline since 8:20 AM CDT.",
        }
    )

    assert block is not None
    facts_index = block.index("### Recent User-Visible Delivery Facts")
    notes_index = block.index("### Heartbeat Notes")
    assert facts_index < notes_index
    assert "Completed/stored items are not pending" in block
    assert "may contain stale or inferential presence language" in block
