"""Tests for the human-takeover relay between the desktop and the browser agent.

Covers the two gateway-side halves directly, the way test_browser_interrupts
covers the interrupt manager rather than the FastAPI routes:

- GatewayRuntime.publish_browser_takeover_control, which is the only way a
  desktop click reaches a running agent, and
- the `browser.input` websocket branch, which is the hot path (a drag is
  dozens of events) and the one that must never forward something unbounded.

The gateway deliberately does not interpret input events. Validation lives in
the agent, against a CDP method whitelist - see cosmic-browser-use's
tests/test_human_input.py. What is tested here is that the relay is addressed,
bounded, and honest about whether anything was listening.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.channels.routes import _MAX_TAKEOVER_INPUT_BATCH, _handle_realtime_websocket_message


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, dict]] = []
        self.fail = fail

    async def publish(self, channel: str, data: str) -> None:
        if self.fail:
            raise RuntimeError("redis is down")
        self.published.append((channel, json.loads(data)))


class FakeRuntime:
    """Just enough GatewayRuntime for the control publisher under test."""

    def __init__(self, *, redis=None, known_task: str | None = "task-1") -> None:
        self._redis = redis
        self._known_task = known_task

    def _resolve_specialist_request_context(self, task_id: str):
        if self._known_task and task_id == self._known_task:
            return {"request_id": "req-1", "session_id": "sess-1"}
        return None

    # The real method, bound onto this stand-in.
    from gateway.runtime import GatewayRuntime as _Real

    publish_browser_takeover_control = _Real.publish_browser_takeover_control


class RecordingRuntime:
    """Captures control calls made by the websocket branch."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_browser_takeover_control(self, *, task_id: str, payload: dict) -> bool:
        self.calls.append({"task_id": task_id, "payload": payload})
        return True

    async def update_user_timezone(self, *args, **kwargs) -> None:
        return None


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message, channel=None) -> None:
        self.sent.append(message)


async def _dispatch(runtime, payload: dict) -> None:
    await _handle_realtime_websocket_message(
        payload,
        runtime=runtime,
        adapter=FakeAdapter(),
        channel="desktop:abc",
        platform="desktop",
    )


# ------------------------------------------------------- control publisher


def test_pause_reaches_the_agent_channel_for_that_task():
    redis = FakeRedis()
    runtime = FakeRuntime(redis=redis)
    delivered = asyncio.run(
        runtime.publish_browser_takeover_control(task_id="task-1", payload={"op": "pause"})
    )
    assert delivered is True
    channel, message = redis.published[0]
    assert channel == "browser_takeover:task-1"
    assert message == {"op": "pause"}


def test_control_is_refused_when_no_run_owns_the_task():
    # Otherwise the desktop would show a paused card for a run that ended, and
    # wait forever for a handover that is never coming.
    redis = FakeRedis()
    runtime = FakeRuntime(redis=redis, known_task=None)
    delivered = asyncio.run(
        runtime.publish_browser_takeover_control(task_id="ghost", payload={"op": "pause"})
    )
    assert delivered is False
    assert redis.published == []


def test_control_is_refused_without_redis():
    runtime = FakeRuntime(redis=None)
    delivered = asyncio.run(
        runtime.publish_browser_takeover_control(task_id="task-1", payload={"op": "pause"})
    )
    assert delivered is False


def test_a_redis_failure_reports_undelivered_rather_than_raising():
    runtime = FakeRuntime(redis=FakeRedis(fail=True))
    delivered = asyncio.run(
        runtime.publish_browser_takeover_control(task_id="task-1", payload={"op": "pause"})
    )
    assert delivered is False


def test_resume_carries_the_note():
    redis = FakeRedis()
    runtime = FakeRuntime(redis=redis)
    asyncio.run(
        runtime.publish_browser_takeover_control(
            task_id="task-1", payload={"op": "resume", "note": "signed in"}
        )
    )
    assert redis.published[0][1] == {"op": "resume", "note": "signed in"}


# --------------------------------------------------------- websocket input


def test_input_events_are_forwarded_to_the_owning_task():
    runtime = RecordingRuntime()
    asyncio.run(
        _dispatch(
            runtime,
            {
                "type": "browser.input",
                "task_id": "task-1",
                "events": [{"kind": "mouse", "type": "mousePressed", "x": 0.5, "y": 0.5}],
            },
        )
    )
    assert runtime.calls[0]["task_id"] == "task-1"
    assert runtime.calls[0]["payload"]["op"] == "input"
    assert len(runtime.calls[0]["payload"]["events"]) == 1


def test_an_oversized_input_batch_is_capped():
    runtime = RecordingRuntime()
    asyncio.run(
        _dispatch(
            runtime,
            {
                "type": "browser.input",
                "task_id": "task-1",
                "events": [{"kind": "mouse", "seq": i} for i in range(5000)],
            },
        )
    )
    assert len(runtime.calls[0]["payload"]["events"]) == _MAX_TAKEOVER_INPUT_BATCH


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "browser.input", "events": [{"kind": "mouse"}]},          # no task
        {"type": "browser.input", "task_id": "task-1"},                     # no events
        {"type": "browser.input", "task_id": "task-1", "events": []},       # empty
        {"type": "browser.input", "task_id": "task-1", "events": "click"},  # not a list
        {"type": "browser.input", "task_id": "   ", "events": [{"a": 1}]},  # blank task
    ],
)
def test_malformed_input_messages_are_dropped(payload):
    runtime = RecordingRuntime()
    asyncio.run(_dispatch(runtime, payload))
    assert runtime.calls == []


def test_the_gateway_does_not_inspect_the_events_it_relays():
    # Deliberate: validation belongs at the agent, immediately before the CDP
    # send, so there is exactly one gate rather than two that can disagree.
    runtime = RecordingRuntime()
    hostile = [{"kind": "evaluate", "js": "fetch('/etc/passwd')"}]
    asyncio.run(
        _dispatch(runtime, {"type": "browser.input", "task_id": "task-1", "events": hostile})
    )
    assert runtime.calls[0]["payload"]["events"] == hostile
