"""A stopped turn keeps its cards.

On 2026-10-08 Stop made a live browser card disappear: the stored copy of a
stopped turn carried only prose, so any rebuild from history drew the message
without its card, and a card-only message was never stored at all. These pin
the stored shape: cards and inline progress survive, a still-running browser
card is stored as stopped (never a clock that ticks forever on reopen), and a
card with no prose is still a message.
"""
from __future__ import annotations

import pytest

from gateway.runtime import ActiveRequest
from test_gateway_desktop_ws import build_runtime


def _state(**overrides):
    state = ActiveRequest(
        request_id="req_stop_cards",
        session_id="sess_stop_cards",
        channel="desktop:stop_cards",
        route="orchestrator",
        task_id="tsk_stop_cards",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


@pytest.mark.asyncio
async def test_running_browser_card_is_stored_as_stopped(tmp_path):
    runtime = build_runtime(tmp_path)
    state = _state(
        browser_progress={
            "task_id": "tsk_browser", "phase": "running", "step": 7, "max_steps": 15,
            "interrupt": {"request_id": "bwc_1", "status": "pending"}, "takeover": "active",
        },
        browser_console_anchors={"tsk_browser": 120},
        activity_log=[{"id": "a1", "label": "Browser run", "stream_offset": 120}],
    )
    cards = runtime._stopped_turn_card_metadata(state)
    progress = cards["browser_progress"]
    assert progress["phase"] == "cancelled" and progress["status"] == "cancelled"
    assert progress["step"] == 7
    # A pending question or takeover from a dead run must not reappear.
    assert "interrupt" not in progress and "takeover" not in progress
    assert cards["browser_console_anchors"]
    assert cards["activity_log"][0]["id"] == "a1"
    # The live state itself is untouched.
    assert state.browser_progress["phase"] == "running"


@pytest.mark.asyncio
async def test_a_finished_card_keeps_its_own_outcome(tmp_path):
    runtime = build_runtime(tmp_path)
    state = _state(browser_progress={"task_id": "t", "phase": "finished", "status": "incomplete"})
    progress = runtime._stopped_turn_card_metadata(state)["browser_progress"]
    assert (progress["phase"], progress["status"]) == ("finished", "incomplete")


@pytest.mark.asyncio
async def test_nothing_to_keep_is_an_empty_dict(tmp_path):
    runtime = build_runtime(tmp_path)
    assert runtime._stopped_turn_card_metadata(_state()) == {}


@pytest.mark.asyncio
async def test_a_card_without_prose_is_still_stored(tmp_path):
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        message_id = runtime._append_session_message(
            "sess_stop_cards",
            role="assistant",
            content="",
            metadata={"interrupted": True, "browser_progress": {"task_id": "t", "phase": "cancelled"}},
        )
        assert message_id
        history = runtime.session_store.get_history("sess_stop_cards")
        stored = next(item for item in history if item["role"] == "assistant")
        assert stored["metadata"]["browser_progress"]["phase"] == "cancelled"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_an_empty_message_with_nothing_renderable_is_still_dropped(tmp_path):
    runtime = build_runtime(tmp_path)
    await runtime.start()
    try:
        assert runtime._append_session_message(
            "sess_stop_cards", role="assistant", content="", metadata={"interrupted": True},
        ) is None
    finally:
        await runtime.stop()
