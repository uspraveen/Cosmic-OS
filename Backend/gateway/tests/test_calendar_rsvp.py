from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.runtime import GatewayRuntime


def test_calendar_event_response_block_marks_verified_invitation_as_respondable():
    runtime = object.__new__(GatewayRuntime)

    block = runtime._calendar_event_response_block(
        {
            "event_id": "evt_1",
            "calendar_id": "primary",
            "summary": "Andrew Goldman chat",
            "attendees": [
                {
                    "email": "user@example.com",
                    "response_status": "needsAction",
                    "self": True,
                    "organizer": False,
                }
            ],
        },
        operation="listed",
        account={
            "account_id": "acct_1",
            "email": "user@example.com",
            "account_label": "Personal",
        },
    )

    assert block["type"] == "calendar_event"
    assert block["can_respond"] is True
    assert block["response_status"] == "needsAction"
    assert block["account_id"] == "acct_1"


def test_calendar_event_response_block_hides_actions_after_response():
    runtime = object.__new__(GatewayRuntime)

    block = runtime._calendar_event_response_block(
        {
            "event_id": "evt_1",
            "calendar_id": "primary",
            "summary": "Andrew Goldman chat",
            "attendees": [
                {
                    "email": "user@example.com",
                    "response_status": "accepted",
                    "self": True,
                    "organizer": False,
                }
            ],
        },
        operation="accepted",
        account={
            "account_id": "acct_1",
            "email": "user@example.com",
            "account_label": "Personal",
        },
    )

    assert block["response_status"] == "accepted"
    assert block["can_respond"] is False


@pytest.mark.asyncio
async def test_respond_to_calendar_invite_uses_selected_account_and_publishes_block():
    runtime = object.__new__(GatewayRuntime)
    runtime.credential_manager = MagicMock()
    runtime.credential_manager.resolve_credential = AsyncMock(
        return_value={"access_token": "token", "account_id": "acct_1"}
    )
    runtime._resolve_calendar_action_account = MagicMock(
        return_value={
            "account_id": "acct_1",
            "email": "user@example.com",
            "account_label": "Personal",
        }
    )
    runtime._dispatch_calendar_invite_response = AsyncMock(
        return_value={
            "status": "completed",
            "output": {
                "response_status": "accepted",
                "event": {
                    "event_id": "evt_1",
                    "calendar_id": "primary",
                    "summary": "Andrew Goldman chat",
                    "attendees": [
                        {
                            "email": "user@example.com",
                            "response_status": "accepted",
                            "self": True,
                            "organizer": False,
                        }
                    ],
                },
            },
        }
    )
    runtime._persist_response_action_block = MagicMock()
    runtime._publish_response_action_update = AsyncMock()

    result = await runtime.respond_to_calendar_invite(
        "evt_1",
        account_id="acct_1",
        calendar_id="primary",
        response_status="accepted",
    )

    assert result["status"] == "accepted"
    assert result["response_block"]["response_status"] == "accepted"
    assert result["response_block"]["can_respond"] is False
    runtime._dispatch_calendar_invite_response.assert_awaited_once()
    runtime._persist_response_action_block.assert_called_once()
    runtime._publish_response_action_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_calendar_dispatches_use_valid_user_source():
    runtime = object.__new__(GatewayRuntime)
    runtime._redis = object()
    runtime.config = MagicMock()
    runtime.config.calendar_agent_id = "cosmic/calendar-agent:1.0.0"
    runtime.config.signing_secret = "secret"
    runtime.config.gmail_process_inbound_poll_interval_sec = 0.01
    runtime._current_session_id = MagicMock(return_value="sess_1")
    runtime._wait_for_agent_terminal_result = AsyncMock(
        return_value={"status": "completed", "output": {}}
    )

    dispatched = []

    async def capture_task(task, _redis):
        dispatched.append(task)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("gateway.runtime.dispatch_task", capture_task)
        await runtime._dispatch_calendar_invite_response(
            event_id="evt_1",
            calendar_id="primary",
            response_status="accepted",
            account={"account_id": "acct_1"},
            auth={"access_token": "token"},
        )
        await runtime._dispatch_calendar_event_update(
            event_id="evt_1",
            calendar_id="primary",
            summary="Andrew Goldman chat",
            start="2026-06-09T13:30:00-05:00",
            end="2026-06-09T14:00:00-05:00",
            location="",
            description="",
            is_all_day=False,
            timezone_name="America/Chicago",
            account={"account_id": "acct_1"},
            auth={"access_token": "token"},
        )

    assert [task.source for task in dispatched] == ["user", "user"]
