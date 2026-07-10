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


def test_heartbeat_context_renders_silent_chat_email_reach_suggestion() -> None:
    runtime = object.__new__(GatewayRuntime)

    block = runtime._render_heartbeat_context_block(
        {
            "current_session_id": "sess_20260710",
            "user_timezone": "America/Chicago",
            "delivery_channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
            "email_delivery_available": True,
            "delivery_state": {
                "desktop_connection_count": 0,
                "desktop_fresh_connection_count": 0,
                "mobile_connection_count": 0,
                "mobile_push_target_count": 2,
                "selected_channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
                "selection_reason": "agent_email_offline_fallback",
                "suggested_reach_path": "agent-email",
                "chat_channels_silent": True,
                "email_delivery_available": True,
                "agent_email_channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
                "proactive_email_recipient": "uspraveenraj@gmail.com",
                "gateway_reach_suggestion": (
                    "Desktop and mobile chat are currently silent (no live connections). "
                    "If this heartbeat finds something genuinely worth surfacing, Gateway "
                    "can reach the user by Cosmic Mail/email to uspraveenraj@gmail.com."
                ),
            },
        }
    )

    assert block is not None
    assert "### Reachability" in block
    assert "Desktop/mobile chat silent: True" in block
    assert "Email delivery available: True" in block
    assert "Proactive email recipient: uspraveenraj@gmail.com" in block
    assert "Gateway suggestion:" in block
    assert "Do not suppress solely because chat looks offline" in block or (
        "can reach the user by Cosmic Mail/email" in block
    )


def test_heartbeat_delivery_falls_back_to_email_when_chat_silent() -> None:
    runtime = object.__new__(GatewayRuntime)

    class _Registry:
        adapters: dict = {}

    class _MobileStore:
        def list_push_targets(self, session_id=None):
            return [{"device_id": "mob_offline"}]

    class _EmailStore:
        def get_primary(self):
            return type(
                "Rec",
                (),
                {
                    "trusted_senders": ("uspraveenraj@gmail.com", "other@example.com"),
                },
            )()

    runtime.registry = _Registry()
    runtime.mobile_device_store = _MobileStore()
    runtime.agent_email_integration_store = _EmailStore()
    runtime._agent_email_effectively_enabled = lambda: True  # type: ignore[method-assign]
    runtime._agent_email_reach_channel = (  # type: ignore[method-assign]
        lambda: "agent-email:iamcosmic001@mail.thelearnchain.com"
    )
    runtime._primary_proactive_email_recipient = (  # type: ignore[method-assign]
        lambda: "uspraveenraj@gmail.com"
    )
    channel, state = asyncio.run(
        runtime._heartbeat_delivery_target(
            {"delivery_channel": "desktop"},
            session_id="sess_test",
        )
    )

    assert channel == "agent-email:iamcosmic001@mail.thelearnchain.com"
    assert state["selection_reason"] == "agent_email_offline_fallback"
    assert state["chat_channels_silent"] is True
    assert state["suggested_reach_path"] == "agent-email"
    assert "Cosmic Mail/email" in (state.get("gateway_reach_suggestion") or "")
    assert state["mobile_push_target_count"] == 1


def test_ensure_proactive_email_recipients_sets_owner_inbox() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime._primary_proactive_email_recipient = (  # type: ignore[method-assign]
        lambda: "uspraveenraj@gmail.com"
    )

    event: dict = {"type": "response.complete", "content": "Hello"}
    runtime._ensure_proactive_email_recipients(event)
    assert event["recipient_email"] == "uspraveenraj@gmail.com"
    assert event["to_recipients"] == [{"email": "uspraveenraj@gmail.com", "name": None}]

    already = {
        "type": "response.complete",
        "to_recipients": [{"email": "someone@example.com", "name": None}],
    }
    runtime._ensure_proactive_email_recipients(already)
    assert already["to_recipients"] == [{"email": "someone@example.com", "name": None}]


def test_offline_presence_suppress_reason_detection() -> None:
    runtime = object.__new__(GatewayRuntime)
    assert runtime._is_offline_presence_suppress_reason(
        "User still offline (0 connections). Nothing time-ripe for a mobile push."
    )
    assert not runtime._is_offline_presence_suppress_reason(
        "No material change since last beat; Gmail drafts unchanged."
    )


def test_email_reach_policy_forces_checkin_on_prolonged_silence() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_heartbeat_test": {
            "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
            "message": {
                "metadata": {
                    "delivery_state": {
                        "chat_channels_silent": True,
                        "email_delivery_available": True,
                        "email_checkin_due": True,
                        "hours_since_last_delivered": 240,
                        "selected_channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
                    }
                }
            },
        }
    }

    decision = runtime._apply_heartbeat_email_reach_policy(
        decision={
            "decision": "suppress",
            "message": "",
            "reason": "User still offline (0 connections). Nothing time-ripe for a mobile push.",
        },
        request_id="req_heartbeat_test",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
    )

    assert decision["decision"] == "deliver"
    assert "Checking in by email" in decision["message"]
    assert decision["policy"] == "force_email_checkin_on_offline_suppress"


def test_email_reach_policy_sanitizes_offline_suppress_when_checkin_not_due() -> None:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_heartbeat_test": {
            "channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
            "message": {
                "metadata": {
                    "delivery_state": {
                        "chat_channels_silent": True,
                        "email_delivery_available": True,
                        "email_checkin_due": False,
                        "hours_since_last_delivered": 2,
                        "selected_channel": "agent-email:iamcosmic001@mail.thelearnchain.com",
                    }
                }
            },
        }
    }

    decision = runtime._apply_heartbeat_email_reach_policy(
        decision={
            "decision": "suppress",
            "message": "",
            "reason": "User still offline. Nothing time-ripe for a mobile push.",
        },
        request_id="req_heartbeat_test",
        channel="agent-email:iamcosmic001@mail.thelearnchain.com",
    )

    assert decision["decision"] == "suppress"
    assert decision["reason"].startswith("No material user-facing change")
    assert "mobile push" not in decision["reason"].lower()
    assert "0 connections" not in decision["reason"].lower()
    assert decision["policy"] == "sanitize_offline_suppress_reason"
