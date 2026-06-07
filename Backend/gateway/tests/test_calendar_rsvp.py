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
    assert result["response_block"]["can_respond"] is True
    runtime._dispatch_calendar_invite_response.assert_awaited_once()
    runtime._persist_response_action_block.assert_called_once()
    runtime._publish_response_action_update.assert_awaited_once()
