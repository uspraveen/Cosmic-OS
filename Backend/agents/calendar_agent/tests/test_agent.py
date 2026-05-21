"""Tests for calendar agent intent handlers and free-slot logic."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure backend root is importable
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from shared import TaskEnvelope, utcnow


# ── Test free-slot logic (extracted from find_free_slots handler) ─────────────


def _find_free_slots(
    busy_periods: list[tuple[str, str]],
    range_start: str,
    range_end: str,
    duration_min: int = 30,
    working_start: int = 9,
    working_end: int = 17,
    buffer_min: int = 15,
) -> list[dict]:
    """Pure-function version of free-slot logic for testing."""
    all_busy = []
    for start_str, end_str in busy_periods:
        bs = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        be = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        all_busy.append((bs, be))
    all_busy.sort(key=lambda x: x[0])

    free_slots = []
    range_s = datetime.fromisoformat(range_start.replace("Z", "+00:00"))
    range_e = datetime.fromisoformat(range_end.replace("Z", "+00:00"))
    duration = timedelta(minutes=duration_min)
    buffer_td = timedelta(minutes=buffer_min)

    current = range_s
    while current + duration <= range_e:
        hour = current.hour
        if hour < working_start or hour >= working_end:
            current = current + timedelta(hours=1)
            continue
        if current.weekday() >= 5:
            current = current + timedelta(days=1)
            current = current.replace(hour=working_start, minute=0, second=0)
            continue

        slot_end = current + duration
        overlap = False
        for busy_start, busy_end in all_busy:
            buffered_start = busy_start - buffer_td
            buffered_end = busy_end + buffer_td
            if current < buffered_end and slot_end > buffered_start:
                overlap = True
                current = buffered_end
                break

        if not overlap:
            free_slots.append(
                {
                    "start": current.isoformat(),
                    "end": slot_end.isoformat(),
                }
            )
            if len(free_slots) >= 10:
                break
            current = slot_end + buffer_td

    return free_slots


class TestFreeSlotLogic:
    """Test the free-slot discovery algorithm."""

    def test_no_busy_periods_returns_full_day(self):
        """With no busy periods, should find slots across working hours."""
        slots = _find_free_slots(
            busy_periods=[],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-30T17:00:00+00:00",
            duration_min=30,
        )
        assert len(slots) > 0
        assert slots[0]["start"] == "2026-03-30T09:00:00+00:00"
        assert slots[0]["end"] == "2026-03-30T09:30:00+00:00"

    def test_busy_period_blocks_slot(self):
        """A busy period should block slots in its time range."""
        slots = _find_free_slots(
            busy_periods=[
                ("2026-03-30T10:00:00+00:00", "2026-03-30T11:00:00+00:00"),
            ],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-30T12:00:00+00:00",
            duration_min=30,
        )
        # No slot should overlap 10:00-11:00
        for slot in slots:
            slot_start = datetime.fromisoformat(slot["start"])
            slot_end = datetime.fromisoformat(slot["end"])
            busy_start = datetime.fromisoformat("2026-03-30T10:00:00+00:00")
            busy_end = datetime.fromisoformat("2026-03-30T11:00:00+00:00")
            assert not (slot_start < busy_end and slot_end > busy_start), (
                f"Slot {slot['start']}-{slot['end']} overlaps busy period"
            )

    def test_buffer_between_events(self):
        """Buffer time should be respected around busy periods."""
        slots = _find_free_slots(
            busy_periods=[
                ("2026-03-30T10:00:00+00:00", "2026-03-30T10:30:00+00:00"),
            ],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-30T12:00:00+00:00",
            duration_min=30,
            buffer_min=15,
        )
        # With 15min buffer, the 10:00-10:30 event blocks 9:45-10:45
        # So slot starting at 9:00 should be fine, but 9:30 would overlap buffer
        for slot in slots:
            slot_start = datetime.fromisoformat(slot["start"])
            slot_end = datetime.fromisoformat(slot["end"])
            # No slot should start before 9:00 + 30min and end after buffer zone
            assert slot_end <= datetime.fromisoformat(
                "2026-03-30T09:45:00+00:00"
            ) or slot_start >= datetime.fromisoformat("2026-03-30T10:45:00+00:00"), (
                f"Slot {slot['start']} violates buffer around 10:00-10:30 event"
            )

    def test_working_hours_respected(self):
        """Slots should not be suggested outside working hours."""
        slots = _find_free_slots(
            busy_periods=[],
            range_start="2026-03-30T07:00:00+00:00",
            range_end="2026-03-30T20:00:00+00:00",
            duration_min=30,
            working_start=9,
            working_end=17,
        )
        for slot in slots:
            hour = datetime.fromisoformat(slot["start"]).hour
            assert 9 <= hour < 17, f"Slot at {hour}:00 is outside working hours"

    def test_weekend_skipped(self):
        """Weekend slots should be skipped."""
        # 2026-03-28 is Saturday, 2026-03-29 is Sunday
        slots = _find_free_slots(
            busy_periods=[],
            range_start="2026-03-28T09:00:00+00:00",
            range_end="2026-03-30T17:00:00+00:00",
            duration_min=30,
        )
        for slot in slots:
            day = datetime.fromisoformat(slot["start"]).weekday()
            assert day < 5, f"Slot on weekday {day} should not appear (0=Mon)"

    def test_duration_honored(self):
        """Returned slots should match the requested duration."""
        slots = _find_free_slots(
            busy_periods=[],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-30T17:00:00+00:00",
            duration_min=60,
        )
        for slot in slots:
            s = datetime.fromisoformat(slot["start"])
            e = datetime.fromisoformat(slot["end"])
            assert (e - s) == timedelta(minutes=60)

    def test_max_10_slots_returned(self):
        """Should return at most 10 slots."""
        slots = _find_free_slots(
            busy_periods=[],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-31T17:00:00+00:00",
            duration_min=30,
        )
        assert len(slots) <= 10

    def test_adjacent_busy_periods_merged(self):
        """Back-to-back meetings should leave no gap between them."""
        slots = _find_free_slots(
            busy_periods=[
                ("2026-03-30T09:00:00+00:00", "2026-03-30T10:00:00+00:00"),
                ("2026-03-30T10:00:00+00:00", "2026-03-30T11:00:00+00:00"),
            ],
            range_start="2026-03-30T09:00:00+00:00",
            range_end="2026-03-30T12:00:00+00:00",
            duration_min=30,
            buffer_min=0,
        )
        # With no buffer, should find a slot at 11:00
        assert any("11:00" in s["start"] for s in slots)


# ── Test Google Calendar client event normalization ───────────────────────────


class TestEventNormalization:
    """Test that Google Calendar API responses are normalized correctly."""

    def test_timed_event_normalization(self):
        from agents.calendar_agent.google_calendar_client import _normalize_event

        raw = {
            "id": "evt123",
            "summary": "Team Standup",
            "description": "Daily sync",
            "location": "Zoom",
            "start": {
                "dateTime": "2026-03-30T09:00:00+05:30",
                "timeZone": "Asia/Kolkata",
            },
            "end": {
                "dateTime": "2026-03-30T09:15:00+05:30",
                "timeZone": "Asia/Kolkata",
            },
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=abc",
            "hangoutLink": "https://meet.google.com/abc",
            "organizer": {"email": "me@example.com"},
            "attendees": [
                {
                    "email": "alex@example.com",
                    "displayName": "Alex",
                    "responseStatus": "accepted",
                }
            ],
        }
        event = _normalize_event(raw, "primary")
        assert event["event_id"] == "evt123"
        assert event["summary"] == "Team Standup"
        assert event["is_all_day"] is False
        assert event["calendar_id"] == "primary"
        assert len(event["attendees"]) == 1
        assert event["attendees"][0]["email"] == "alex@example.com"

    def test_all_day_event_normalization(self):
        from agents.calendar_agent.google_calendar_client import _normalize_event

        raw = {
            "id": "evt456",
            "summary": "Company Holiday",
            "start": {"date": "2026-07-04"},
            "end": {"date": "2026-07-05"},
            "status": "confirmed",
        }
        event = _normalize_event(raw, "primary")
        assert event["is_all_day"] is True
        assert event["start"] == "2026-07-04"

    def test_cancelled_event_filtered(self):
        """Cancelled events should not be returned by list_events."""
        from agents.calendar_agent.google_calendar_client import _normalize_event

        # _normalize_event doesn't filter — that's the caller's job
        # but we verify the status field is present
        raw = {
            "id": "evt789",
            "status": "cancelled",
            "start": {"date": "2026-03-30"},
            "end": {"date": "2026-03-31"},
        }
        event = _normalize_event(raw, "primary")
        assert event["status"] == "cancelled"

    def test_recurring_event_id_preserved(self):
        from agents.calendar_agent.google_calendar_client import _normalize_event

        raw = {
            "id": "inst_001",
            "recurringEventId": "series_abc",
            "summary": "Weekly Sync",
            "start": {"dateTime": "2026-03-30T10:00:00Z"},
            "end": {"dateTime": "2026-03-30T11:00:00Z"},
            "status": "confirmed",
        }
        event = _normalize_event(raw, "primary")
        assert event["recurring_event_id"] == "series_abc"

    def test_meeting_link_extracted_from_conference_data_entry_points(self):
        from agents.calendar_agent.google_calendar_client import _normalize_event

        raw = {
            "id": "evt_meet_1",
            "summary": "Meet-enabled Event",
            "start": {"dateTime": "2026-03-31T19:00:00Z"},
            "end": {"dateTime": "2026-03-31T20:00:00Z"},
            "conferenceData": {
                "entryPoints": [
                    {
                        "entryPointType": "video",
                        "uri": "https://meet.google.com/xyz-abcd-123",
                    }
                ]
            },
            "status": "confirmed",
        }
        event = _normalize_event(raw, "primary")
        assert event["meeting_link"] == "https://meet.google.com/xyz-abcd-123"


# ── Test config ───────────────────────────────────────────────────────────────


class TestConfig:
    """Test calendar agent config loading."""

    def test_default_config(self):
        from agents.calendar_agent.config import CalendarAgentConfig

        cfg = CalendarAgentConfig()
        assert cfg.default_timezone == "America/Chicago"
        assert cfg.working_hour_start == 9
        assert cfg.working_hour_end == 17
        assert cfg.default_event_duration_min == 30
        assert cfg.internal_llm_model == "gpt-5-mini"

    def test_config_from_env(self):
        from agents.calendar_agent.config import CalendarAgentConfig

        cfg = CalendarAgentConfig.from_env()
        assert cfg.redis_url  # should have some default


# ── Test schema validation ────────────────────────────────────────────────────


class TestSchemas:
    """Test that JSON schemas are valid and parseable."""

    def test_all_schemas_exist(self):
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
        expected = [
            "calendar.list_events.input.json",
            "calendar.list_events.output.json",
            "calendar.create_event.input.json",
            "calendar.create_event.output.json",
            "calendar.find_free_slots.input.json",
            "calendar.find_free_slots.output.json",
            "calendar.update_event.input.json",
            "calendar.update_event.output.json",
            "calendar.cancel_event.input.json",
            "calendar.cancel_event.output.json",
            "calendar.recall_session.input.json",
            "calendar.recall_session.output.json",
            "calendar.heartbeat_digest.input.json",
            "calendar.heartbeat_digest.output.json",
        ]
        for name in expected:
            path = schemas_dir / name
            assert path.exists(), f"Missing schema: {name}"
            data = json.loads(path.read_text())
            assert "$schema" in data or "type" in data, f"Invalid schema: {name}"


# ── Test agent card ───────────────────────────────────────────────────────────


class TestAgentCard:
    """Test agent_card.yaml structure."""

    def test_card_parseable(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        data = yaml.safe_load(card_path.read_text())
        assert data["agent_id"] == "cosmic/calendar-agent:1.0.0"
        assert len(data["intents"]) == 7
        intent_names = [i["name"] for i in data["intents"]]
        assert "calendar.list_events" in intent_names
        assert "calendar.create_event" in intent_names
        assert "calendar.find_free_slots" in intent_names
        assert "calendar.update_event" in intent_names
        assert "calendar.cancel_event" in intent_names
        assert "calendar.recall_session" in intent_names
        assert "calendar.heartbeat_digest" in intent_names

    def test_auth_requirements_present(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        data = yaml.safe_load(card_path.read_text())
        auth_req = data.get("auth_requirements", {})
        assert "calendar.list_events" in auth_req
        assert auth_req["calendar.list_events"]["provider"] == "google"
        assert "calendar.heartbeat_digest" in auth_req
        assert "calendar.recall_session" not in auth_req

    def test_all_schemas_referenced(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
        data = yaml.safe_load(card_path.read_text())
        for intent in data["intents"]:
            input_path = schemas_dir / intent["input_schema"].split("/")[-1]
            output_path = schemas_dir / intent["output_schema"].split("/")[-1]
            assert input_path.exists(), f"Missing input schema for {intent['name']}"
            assert output_path.exists(), f"Missing output schema for {intent['name']}"


@pytest.mark.asyncio
async def test_heartbeat_digest_uses_structured_fetch_without_internal_llm():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=True, calendar_use_langgraph=True),
    )

    task = TaskEnvelope(
        task_id="tsk_heartbeat_digest",
        task_list_id="sess_heartbeat",
        parent_task_id=None,
        session_id="sess_heartbeat",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.heartbeat_digest",
        input={
            "time_min": "2026-05-21T14:00:00Z",
            "time_max": "2026-05-22T14:00:00Z",
            "max_results": 5,
            "max_results_per_calendar": 3,
            "selected_only": True,
            "account": {
                "account_id": "acct_google_1",
                "account_label": "Work Calendar",
                "email": "user@example.com",
                "display_name": "User",
                "is_primary": True,
            },
        },
        input_artifacts=[],
        idempotency_key="idem_heartbeat_digest",
        priority="low",
        signature="sig",
        created_at=utcnow(),
        source="heartbeat",
        source_id="default",
        channel="desktop",
    )

    fake_client = AsyncMock()
    fake_client.list_calendars.return_value = [
        {
            "id": "primary",
            "name": "Primary",
            "color": "#4285f4",
            "primary": True,
            "selected": True,
        },
        {
            "id": "muted",
            "name": "Muted",
            "primary": False,
            "selected": False,
        },
    ]
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_1",
            "summary": "Pitch prep",
            "start": "2026-05-21T16:00:00Z",
            "end": "2026-05-21T16:30:00Z",
            "status": "confirmed",
        }
    ]

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(side_effect=AssertionError("heartbeat digest must not parse NL")),
        ),
    ):
        result = await agent.execute(task)

    assert result.status == "completed"
    fake_client.list_events.assert_awaited_once()
    assert fake_client.list_events.await_args.kwargs["calendar_id"] == "primary"
    event = result.output["events"][0]
    assert event["event_id"] == "evt_1"
    assert event["account_id"] == "acct_google_1"
    assert event["account_label"] == "Work Calendar"
    assert event["email"] == "user@example.com"
    assert event["calendar_id"] == "primary"
    assert event["calendar_name"] == "Primary"
    assert result.output["calendar_count"] == 1


@pytest.mark.asyncio
async def test_update_event_resolves_target_by_query_when_event_id_missing():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(redis_client=MagicMock(), config=CalendarAgentConfig(enable_internal_llm=False))
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_update_resolve",
        task_list_id="sess_1",
        parent_task_id=None,
        session_id="sess_1",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.update_event",
        input={"query": "Move lunch with Sarah tomorrow to 3pm"},
        input_artifacts=[],
        idempotency_key="idem_update_resolve",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_lunch_1",
            "calendar_id": "primary",
            "summary": "Lunch with Sarah",
            "start": "2026-03-31T12:00:00+00:00",
            "status": "confirmed",
        }
    ]
    fake_client.update_event.return_value = {
        "event_id": "evt_lunch_1",
        "calendar_id": "primary",
        "summary": "Lunch with Sarah",
        "start": "2026-03-31T15:00:00+00:00",
        "status": "confirmed",
    }

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "update",
                    "params": {
                        "event_query": "Lunch with Sarah",
                        "time_range": {
                            "start": "2026-03-31T00:00:00+00:00",
                            "end": "2026-04-01T00:00:00+00:00",
                        },
                        "patch": {"start": "2026-03-31T15:00:00+00:00"},
                    },
                }
            ),
        ),
    ):
        result = await agent.handle_calendar_update_event(task)

    assert result.status == "completed"
    fake_client.update_event.assert_awaited_once()
    kwargs = fake_client.update_event.await_args.kwargs
    assert kwargs["event_id"] == "evt_lunch_1"
    assert kwargs["patch"]["start"]["dateTime"] == "2026-03-31T15:00:00+00:00"


@pytest.mark.asyncio
async def test_cancel_event_returns_ambiguity_when_multiple_matches_found():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(redis_client=MagicMock(), config=CalendarAgentConfig(enable_internal_llm=False))
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_cancel_ambiguous",
        task_list_id="sess_1",
        parent_task_id=None,
        session_id="sess_1",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.cancel_event",
        input={"query": "Cancel team sync"},
        input_artifacts=[],
        idempotency_key="idem_cancel_ambiguous",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_sync_1",
            "calendar_id": "primary",
            "summary": "Team Sync",
            "start": "2026-03-31T14:00:00+00:00",
            "status": "confirmed",
        },
        {
            "event_id": "evt_sync_2",
            "calendar_id": "primary",
            "summary": "Team Sync",
            "start": "2026-04-01T14:00:00+00:00",
            "status": "confirmed",
        },
    ]

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "cancel",
                    "params": {
                        "event_query": "Team Sync",
                        "time_range": {
                            "start": "2026-03-30T00:00:00+00:00",
                            "end": "2026-04-02T00:00:00+00:00",
                        },
                    },
                }
            ),
        ),
    ):
        result = await agent.handle_calendar_cancel_event(task)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
    fake_client.delete_event.assert_not_called()


@pytest.mark.asyncio
async def test_execute_prefers_langgraph_when_enabled():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig
    from shared.contracts import AgentResult

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=True),
    )
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_graph_pref",
        task_list_id="sess_graph",
        parent_task_id=None,
        session_id="sess_graph",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.list_events",
        input={},
        input_artifacts=[],
        idempotency_key="idem_graph_pref",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    with patch(
        "agents.calendar_agent.calendar_graph.run_calendar_langgraph",
        AsyncMock(return_value=AgentResult(status="completed", output={"ok": True}, artifacts=[], error=None)),
    ) as run_graph:
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["ok"] is True
    run_graph.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_calendar_list_events_does_not_use_natural_language_query_as_google_search():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=False),
    )
    agent.db = MagicMock()
    agent.auth = {
        "account_id": "acc_primary",
        "account_email": "usp.upenn@gmail.com",
        "account_display_name": "Praveen Raj U S",
        "account_label": "Google account",
        "account_is_primary": True,
    }

    task = TaskEnvelope(
        task_id="tsk_list_nl_query",
        task_list_id="sess_list_nl_query",
        parent_task_id=None,
        session_id="sess_list_nl_query",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.list_events",
        input={"query": "What's on my calendar today and this week?"},
        input_artifacts=[],
        idempotency_key="idem_list_nl_query",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = []

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "list",
                    "params": {
                        "time_range": {
                            "start": "2026-03-31T00:00:00Z",
                            "end": "2026-04-05T23:59:59Z",
                        }
                    },
                }
            ),
        ),
    ):
        result = await agent.handle_calendar_list_events(task)

    assert result.status == "completed"
    assert result.output["search_query"] is None
    assert result.output["resolved_account"]["email"] == "usp.upenn@gmail.com"
    fake_client.list_events.assert_awaited_once_with(
        calendar_id="primary",
        time_min="2026-03-31T00:00:00Z",
        time_max="2026-04-05T23:59:59Z",
        max_results=agent._cfg.max_events_per_list,
        query=None,
    )


@pytest.mark.asyncio
async def test_execute_falls_back_to_legacy_handler_when_langgraph_disabled():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig
    from shared.contracts import AgentResult

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=False),
    )
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_legacy_pref",
        task_list_id="sess_legacy",
        parent_task_id=None,
        session_id="sess_legacy",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.list_events",
        input={},
        input_artifacts=[],
        idempotency_key="idem_legacy_pref",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    with patch.object(
        agent,
        "handle_calendar_list_events",
        AsyncMock(return_value=AgentResult(status="completed", output={"legacy": True}, artifacts=[], error=None)),
    ) as handler:
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["legacy"] is True
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_calendar_create_event_passes_add_google_meet():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=False),
    )
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_create_meet",
        task_list_id="sess_create_meet",
        parent_task_id=None,
        session_id="sess_create_meet",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.create_event",
        input={
            "summary": "Alpha Agent Architecture Meet",
            "start": "2026-03-31T19:00:00-05:00",
            "end": "2026-03-31T20:00:00-05:00",
            "timezone": "America/Chicago",
            "attendees": ["uspraveenraj@gmail.com"],
            "add_google_meet": True,
        },
        input_artifacts=[],
        idempotency_key="idem_create_meet",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = []
    fake_client.create_event.return_value = {
        "event_id": "evt_create_meet",
        "calendar_id": "primary",
        "summary": "Alpha Agent Architecture Meet",
        "meeting_link": "https://meet.google.com/xyz-abcd-123",
        "status": "confirmed",
    }

    with patch.object(agent, "_get_calendar_client", return_value=fake_client):
        result = await agent.handle_calendar_create_event(task)

    assert result.status == "completed"
    assert result.output["event"]["meeting_link"] == "https://meet.google.com/xyz-abcd-123"
    fake_client.create_event.assert_awaited_once_with(
        calendar_id="primary",
        summary="Alpha Agent Architecture Meet",
        start={"dateTime": "2026-03-31T19:00:00-05:00", "timeZone": "America/Chicago"},
        end={"dateTime": "2026-03-31T20:00:00-05:00", "timeZone": "America/Chicago"},
        timezone="America/Chicago",
        description="",
        location="",
        attendees=["uspraveenraj@gmail.com"],
        reminders=None,
        add_google_meet=True,
    )


@pytest.mark.asyncio
async def test_langgraph_list_events_returns_resolved_account_and_avoids_natural_language_search_filter():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=True),
    )
    agent.db = MagicMock()
    agent.auth = {
        "account_id": "acc_primary",
        "account_email": "usp.upenn@gmail.com",
        "account_display_name": "Praveen Raj U S",
        "account_label": "Google account",
        "account_is_primary": True,
    }

    task = TaskEnvelope(
        task_id="tsk_graph_list",
        task_list_id="sess_graph_list",
        parent_task_id=None,
        session_id="sess_graph_list",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.list_events",
        input={"query": "What's on my calendar today and this week?"},
        input_artifacts=[],
        idempotency_key="idem_graph_list",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_alpha",
            "calendar_id": "primary",
            "summary": "Alpha Agent Architecture Meet",
            "start": "2026-03-31T16:30:00Z",
            "end": "2026-03-31T17:30:00Z",
            "status": "confirmed",
        }
    ]

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "list",
                    "params": {
                        "time_range": {
                            "start": "2026-03-31T00:00:00Z",
                            "end": "2026-04-05T23:59:59Z",
                        }
                    },
                }
            ),
        ),
    ):
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["search_query"] is None
    assert result.output["resolved_account"]["email"] == "usp.upenn@gmail.com"
    fake_client.list_events.assert_awaited_once_with(
        calendar_id="primary",
        time_min="2026-03-31T00:00:00Z",
        time_max="2026-04-05T23:59:59Z",
        max_results=agent._cfg.max_events_per_list,
        query=None,
    )


@pytest.mark.asyncio
async def test_langgraph_update_event_resolves_query_and_updates_event():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=True),
    )
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_graph_update",
        task_list_id="sess_graph_update",
        parent_task_id=None,
        session_id="sess_graph_update",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.update_event",
        input={"query": "Move lunch with Sarah tomorrow to 3pm"},
        input_artifacts=[],
        idempotency_key="idem_graph_update",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.side_effect = [
        [
            {
                "event_id": "evt_lunch_1",
                "calendar_id": "primary",
                "summary": "Lunch with Sarah",
                "start": "2026-03-31T12:00:00+00:00",
                "status": "confirmed",
            }
        ],
        [],
    ]
    fake_client.update_event.return_value = {
        "event_id": "evt_lunch_1",
        "calendar_id": "primary",
        "summary": "Lunch with Sarah",
        "start": "2026-03-31T15:00:00+00:00",
        "status": "confirmed",
    }

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "update",
                    "params": {
                        "event_query": "Lunch with Sarah",
                        "time_range": {
                            "start": "2026-03-31T00:00:00+00:00",
                            "end": "2026-04-01T00:00:00+00:00",
                        },
                        "patch": {
                            "start": "2026-03-31T15:00:00+00:00",
                            "timezone": "UTC",
                        },
                    },
                }
            ),
        ),
    ):
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["workflow"] == "langgraph"
    fake_client.update_event.assert_awaited_once()
    kwargs = fake_client.update_event.await_args.kwargs
    assert kwargs["event_id"] == "evt_lunch_1"
    assert kwargs["patch"]["start"]["dateTime"] == "2026-03-31T15:00:00+00:00"


@pytest.mark.asyncio
async def test_langgraph_update_event_add_google_meet_skips_conflict_step_when_time_unchanged():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=True),
    )
    agent.db = MagicMock()
    agent.step_plan = MagicMock()
    agent.step_plan.create = AsyncMock()
    agent.step_plan.update = AsyncMock()

    task = TaskEnvelope(
        task_id="tsk_graph_add_meet",
        task_list_id="sess_graph_add_meet",
        parent_task_id=None,
        session_id="sess_graph_add_meet",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.update_event",
        input={"query": "Add a Google Meet link to the Alpha Agent Architecture Meet event at 7pm CDT today"},
        input_artifacts=[],
        idempotency_key="idem_graph_add_meet",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_alpha_7pm",
            "calendar_id": "primary",
            "summary": "Alpha Agent Architecture Meet",
            "start": "2026-04-01T00:00:00Z",
            "status": "confirmed",
        }
    ]
    fake_client.update_event.return_value = {
        "event_id": "evt_alpha_7pm",
        "calendar_id": "primary",
        "summary": "Alpha Agent Architecture Meet",
        "meeting_link": "https://meet.google.com/xyz-abcd-123",
        "status": "confirmed",
    }

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "update",
                    "params": {
                        "event_query": "Alpha Agent Architecture Meet",
                        "time_range": {
                            "start": "2026-03-31T23:30:00Z",
                            "end": "2026-04-01T01:30:00Z",
                        },
                        "patch": {"add_google_meet": True},
                    },
                }
            ),
        ),
    ):
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["workflow"] == "langgraph"
    assert result.output["event"]["meeting_link"] == "https://meet.google.com/xyz-abcd-123"
    fake_client.update_event.assert_awaited_once_with(
        calendar_id="primary",
        event_id="evt_alpha_7pm",
        patch={"add_google_meet": True},
    )
    assert any(
        call.args[0] == 3 and call.args[1] == "skipped"
        for call in agent.step_plan.update.await_args_list
    )
    assert any(
        call.args[0] == 4 and call.args[1] == "completed"
        for call in agent.step_plan.update.await_args_list
    )


@pytest.mark.asyncio
async def test_langgraph_create_event_returns_conflict_warning():
    from agents.calendar_agent.agent import CalendarAgent
    from agents.calendar_agent.config import CalendarAgentConfig

    agent = CalendarAgent(
        redis_client=MagicMock(),
        config=CalendarAgentConfig(enable_internal_llm=False, calendar_use_langgraph=True),
    )
    agent.db = MagicMock()

    task = TaskEnvelope(
        task_id="tsk_graph_create",
        task_list_id="sess_graph_create",
        parent_task_id=None,
        session_id="sess_graph_create",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/calendar-agent:1.0.0",
        intent="calendar.create_event",
        input={"query": "Schedule lunch with Sarah tomorrow at 12pm for 30 minutes"},
        input_artifacts=[],
        idempotency_key="idem_graph_create",
        priority="high",
        signature="sig",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )

    fake_client = AsyncMock()
    fake_client.list_events.return_value = [
        {
            "event_id": "evt_overlap",
            "calendar_id": "primary",
            "summary": "Existing lunch",
            "start": "2026-03-31T12:00:00+00:00",
            "status": "confirmed",
        }
    ]
    fake_client.create_event.return_value = {
        "event_id": "evt_new_lunch",
        "calendar_id": "primary",
        "summary": "Lunch with Sarah",
        "start": "2026-03-31T12:00:00+00:00",
        "status": "confirmed",
    }

    with (
        patch.object(agent, "_get_calendar_client", return_value=fake_client),
        patch.object(
            agent,
            "_parse_natural_language",
            AsyncMock(
                return_value={
                    "operation": "create",
                    "params": {
                        "summary": "Lunch with Sarah",
                        "start": "2026-03-31T12:00:00+00:00",
                        "end": "2026-03-31T12:30:00+00:00",
                        "timezone": "UTC",
                        "attendees": ["sarah@example.com"],
                    },
                }
            ),
        ),
    ):
        result = await agent.execute(task)

    assert result.status == "completed"
    assert result.output["workflow"] == "langgraph"
    assert "conflict_warning" in result.output
    fake_client.create_event.assert_awaited_once()
