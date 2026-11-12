"""Mid-run user messages: steer into the running run, or queue the next turn.

These drive the real GatewayRuntime + websocket message handler end to end,
with a fake orchestrator that uses the real orchestrator SteerInbox, so the
gateway/orchestrator delivery contract is exercised exactly as in production.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gateway.channels.desktop import DesktopAdapter
from gateway.channels.routes import _handle_realtime_websocket_message
from gateway.mid_run_queue import HeldMessage, HeldMessageQueue
from orchestrator.steering import SteerInbox
from test_gateway_desktop_ws import build_runtime

CHANNEL = "desktop:desk_midrun"


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.sent if event.get("type") == event_type]


class SteeringFakeOrchestrator:
    """Scripted orchestrator run with one tool step and a real steer inbox.

    Per request id a test can hold the tool step (``step``) and the end of the
    run (``end``), pick a behavior, and observe what the model was shown.
    """

    def __init__(self) -> None:
        self.inboxes: dict[str, SteerInbox] = {}
        self.gates: dict[tuple[str, str], asyncio.Event] = {}
        self.behavior: dict[str, str] = {}
        self.model_saw: dict[str, list[str]] = {}
        self.queries: list[str] = []
        self.cancelled: set[str] = set()
        self.submit_calls: list[str] = []

    def gate(self, request_id: str, name: str) -> asyncio.Event:
        key = (request_id, name)
        if key not in self.gates:
            event = asyncio.Event()
            event.set()  # runs proceed on their own unless a test holds them
            self.gates[key] = event
        return self.gates[key]

    def hold(self, request_id: str, name: str) -> None:
        self.gate(request_id, name).clear()

    def release(self, request_id: str, name: str) -> None:
        self.gate(request_id, name).set()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def list_active_tasks(self, **_: Any) -> list[dict]:
        return []

    async def stream_task(self, task):
        request_id = str(task.input.get("request_id"))
        query = str(task.input.get("query"))
        self.queries.append(query)
        inbox = SteerInbox()
        self.inboxes[task.task_id] = inbox
        base = {
            "task_id": task.task_id,
            "request_id": request_id,
            "session_id": task.session_id,
            "channel": task.channel,
            "route": "orchestrator",
        }
        behavior = self.behavior.get(query, "normal")
        yield {**base, "type": "task.created", "status": "running"}
        # The "tool call" is running; the user may type now.
        await self.gate(request_id, "step").wait()
        if behavior == "fail":
            inbox.close()
            raise RuntimeError("orchestrator exploded")
        if behavior != "no_boundary":
            drained = inbox.drain()
            if drained:
                self.model_saw.setdefault(request_id, []).extend(item.content for item in drained)
                yield {
                    **base,
                    "type": "task.user_message_delivered",
                    "message_ids": [item.message_id for item in drained],
                }
        inbox.close()  # last boundary passed
        await self.gate(request_id, "end").wait()
        answer = f"answer to {query}"
        yield {**base, "type": "response.chunk", "content": answer, "done": False}
        yield {
            **base,
            "type": "response.complete",
            "content": answer,
            "awaiting_reply": False,
        }
        yield {**base, "type": "task.completed", "status": "completed"}

    async def submit_task_message(self, task_id: str, *, message_id: str, content: str) -> bool:
        self.submit_calls.append(message_id)
        inbox = self.inboxes.get(task_id)
        return bool(inbox and inbox.submit(message_id, content))

    async def withdraw_task_message(self, task_id: str, *, message_id: str) -> bool:
        inbox = self.inboxes.get(task_id)
        return bool(inbox and inbox.withdraw(message_id))

    async def cancel_task(self, task_id: str) -> bool:
        self.cancelled.add(task_id)
        return True


async def wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class Harness:
    def __init__(self, tmp_path, *, route: str = "opus") -> None:
        self.runtime = build_runtime(tmp_path, route=route)
        self.orchestrator = SteeringFakeOrchestrator()
        self.runtime.orchestrator = self.orchestrator
        self.socket = RecordingSocket()
        self.adapter: DesktopAdapter | None = None

    async def __aenter__(self) -> "Harness":
        await self.runtime.start()
        adapter = self.runtime.registry.adapters.get("desktop")
        assert isinstance(adapter, DesktopAdapter)
        self.adapter = adapter
        await adapter.register_connection(self.socket, device_id="desk_midrun", channel=CHANNEL)  # type: ignore[arg-type]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for event in self.orchestrator.gates.values():
            event.set()
        await self.runtime.stop()

    async def send(self, payload: dict[str, Any]) -> None:
        assert self.adapter is not None
        await _handle_realtime_websocket_message(
            payload,
            runtime=self.runtime,
            adapter=self.adapter,
            channel=CHANNEL,
            platform="desktop",
        )

    async def query(self, request_id: str, content: str, **extra: Any) -> None:
        await self.send({"type": "query", "request_id": request_id, "content": content, **extra})

    def history(self) -> list[dict[str, Any]]:
        session_id = self.runtime._current_session_id()  # noqa: SLF001
        return self.runtime.session_store.get_history(session_id)

    def transcript(self) -> list[tuple[str, str]]:
        return [(item["role"], item["content"]) for item in self.history()]

    def running(self, request_id: str) -> bool:
        return request_id in self.runtime.active_requests

    def queue_items(self) -> list[HeldMessage]:
        return self.runtime.mid_run.queue.items(CHANNEL)

    async def wait_idle(self) -> None:
        await wait_for(
            lambda: not self.runtime.active_requests
            and not self.queue_items()
            and CHANNEL not in self.runtime.mid_run._dispatching  # noqa: SLF001
        )


@pytest.mark.asyncio
async def test_idle_channel_query_runs_exactly_as_before(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        await h.query("req_a", "first task")
        await h.wait_idle()
        assert [e["type"] for e in h.socket.sent if e["type"].startswith("user_message")] == []
        assert h.socket.of_type("route_result")[0]["request_id"] == "req_a"
        assert h.transcript() == [("user", "first task"), ("assistant", "answer to first task")]


@pytest.mark.asyncio
async def test_message_sent_mid_run_is_steered_into_the_next_step(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "research flights")
        await wait_for(lambda: h.socket.of_type("task.created"))

        # The composer is not locked: this is accepted while A is mid-tool.
        await h.query("req_b", "only nonstop please")
        queued = h.socket.of_type("user_message.queued")
        assert queued and queued[0]["request_id"] == "req_b"
        assert queued[0]["target_request_id"] == "req_a"
        await wait_for(lambda: h.queue_items() and h.queue_items()[0].state == "injecting")

        h.orchestrator.release("req_a", "step")
        await h.wait_idle()

        assert h.orchestrator.model_saw["req_a"] == ["only nonstop please"]
        delivered = h.socket.of_type("user_message.delivered")
        assert [e["request_id"] for e in delivered] == ["req_b"]
        assert delivered[0]["into_request_id"] == "req_a"
        # No separate turn was started for the steer...
        assert h.orchestrator.queries == ["research flights"]
        assert not [e for e in h.socket.of_type("route_result") if e["request_id"] == "req_b"]
        # ...and history lists it where the model read it: after A's query,
        # before A's answer, exactly once.
        assert h.transcript() == [
            ("user", "research flights"),
            ("user", "only nonstop please"),
            ("assistant", "answer to research flights"),
        ]
        steer_row = h.history()[1]
        assert steer_row["metadata"]["steered_into_request_id"] == "req_a"
        # The model-facing delivery event is never forwarded raw.
        assert not h.socket.of_type("task.user_message_delivered")


@pytest.mark.asyncio
async def test_burst_of_messages_is_delivered_once_and_in_order(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "plan the offsite")
        await wait_for(lambda: h.socket.of_type("task.created"))
        for index, text in enumerate(["budget is 5k", "in Austin", "first week of May"]):
            await h.query(f"req_s{index}", text)
        await wait_for(lambda: all(item.state == "injecting" for item in h.queue_items()) and len(h.queue_items()) == 3)
        # A duplicate resend of the same request id is absorbed.
        await h.query("req_s1", "in Austin")
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()

        assert h.orchestrator.model_saw["req_a"] == ["budget is 5k", "in Austin", "first week of May"]
        assert [row[1] for row in h.transcript()] == [
            "plan the offsite",
            "budget is 5k",
            "in Austin",
            "first week of May",
            "answer to plan the offsite",
        ]
        assert h.orchestrator.queries == ["plan the offsite"]


@pytest.mark.asyncio
async def test_queue_mode_waits_for_the_run_then_runs_as_its_own_turn(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "draft the memo")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "then email it to Sam", delivery="queue")
        await asyncio.sleep(0.05)
        assert h.queue_items()[0].state == "waiting"  # never injected
        assert h.orchestrator.submit_calls == []
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()

        assert h.orchestrator.queries == ["draft the memo", "then email it to Sam"]
        types = [e["type"] for e in h.socket.sent]
        assert types.index("user_message.dispatched") > max(
            i for i, e in enumerate(h.socket.sent) if e.get("type") == "task.completed" and e.get("request_id") == "req_a"
        )
        assert h.transcript() == [
            ("user", "draft the memo"),
            ("assistant", "answer to draft the memo"),
            ("user", "then email it to Sam"),
            ("assistant", "answer to then email it to Sam"),
        ]


@pytest.mark.asyncio
async def test_steer_that_misses_the_last_boundary_runs_next_exactly_once(tmp_path) -> None:
    """The race at run end: A passed its last step boundary (inbox closed)
    but has not finished yet when the message arrives."""
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "end")
        await h.query("req_a", "summarize the doc")
        await wait_for(lambda: "req_a" in h.orchestrator.model_saw or any(
            inbox.open is False for inbox in h.orchestrator.inboxes.values()
        ))
        await h.query("req_b", "also add a TL;DR")
        await wait_for(lambda: "req_b" in h.orchestrator.submit_calls)
        await asyncio.sleep(0.02)
        assert h.queue_items()[0].state == "waiting"  # refused: inbox closed
        h.orchestrator.release("req_a", "end")
        await h.wait_idle()

        assert h.orchestrator.queries == ["summarize the doc", "also add a TL;DR"]
        assert [row for row in h.transcript() if row[1] == "also add a TL;DR"] == [("user", "also add a TL;DR")]
        assert not h.socket.of_type("user_message.delivered")


@pytest.mark.asyncio
async def test_accepted_but_never_drained_steer_is_redispatched(tmp_path) -> None:
    """The run accepted the steer but ended without another boundary."""
    async with Harness(tmp_path) as h:
        h.orchestrator.behavior["quick answer"] = "no_boundary"
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "quick answer")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "and cite sources")
        await wait_for(lambda: h.queue_items() and h.queue_items()[0].state == "injecting")
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()

        assert h.orchestrator.queries == ["quick answer", "and cite sources"]
        assert sum(1 for row in h.transcript() if row[1] == "and cite sources") == 1


@pytest.mark.asyncio
async def test_stop_mid_run_keeps_the_queue_and_sends_it_next(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "scrape 500 pages")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "actually just the first 10", delivery="queue")
        await h.send({"type": "cancel", "request_id": "cancel_1", "target_request_id": "req_a"})
        await h.wait_idle()

        assert h.socket.of_type("task.cancelled")
        assert h.orchestrator.queries == ["scrape 500 pages", "actually just the first 10"]
        assert ("assistant", "answer to actually just the first 10") in h.transcript()


@pytest.mark.asyncio
async def test_run_failure_mid_run_does_not_lose_held_messages(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.behavior["flaky job"] = "fail"
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "flaky job")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "steer that never lands")
        await wait_for(lambda: h.queue_items() and h.queue_items()[0].state == "injecting")
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()

        assert h.orchestrator.queries == ["flaky job", "steer that never lands"]
        assert sum(1 for row in h.transcript() if row[1] == "steer that never lands") == 1


@pytest.mark.asyncio
async def test_withdraw_before_delivery_removes_the_message(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "write the report")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_q", "queued thing", delivery="queue")
        await h.query("req_s", "steer thing")
        await wait_for(lambda: any(item.state == "injecting" for item in h.queue_items()))

        await h.send({"type": "query.withdraw", "target_request_id": "req_q"})
        await h.send({"type": "query.withdraw", "target_request_id": "req_s"})
        withdrawn = {e["request_id"]: e for e in h.socket.of_type("user_message.withdrawn")}
        assert withdrawn["req_q"]["withdrawn"] is True
        assert withdrawn["req_s"]["withdrawn"] is True
        assert withdrawn["req_s"]["content"] == "steer thing"

        h.orchestrator.release("req_a", "step")
        await h.wait_idle()
        assert "req_a" not in h.orchestrator.model_saw
        assert h.orchestrator.queries == ["write the report"]
        assert [row[1] for row in h.transcript()] == ["write the report", "answer to write the report"]


@pytest.mark.asyncio
async def test_withdraw_after_delivery_reports_already_delivered(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        h.orchestrator.hold("req_a", "end")
        await h.query("req_a", "long task")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_s", "steer")
        await wait_for(lambda: h.queue_items() and h.queue_items()[0].state == "injecting")
        h.orchestrator.release("req_a", "step")
        await wait_for(lambda: h.socket.of_type("user_message.delivered"))
        await h.send({"type": "query.withdraw", "target_request_id": "req_s"})
        result = h.socket.of_type("user_message.withdrawn")[-1]
        assert result["withdrawn"] is False
        h.orchestrator.release("req_a", "end")
        await h.wait_idle()


@pytest.mark.asyncio
async def test_backgrounding_the_run_frees_the_composer_for_queued_turns(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "big research job")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "meanwhile, what's 2+2", delivery="queue")
        await h.send({"type": "background", "request_id": "req_a"})
        # B starts right away as the new foreground turn while A keeps running.
        await wait_for(lambda: "meanwhile, what's 2+2" in h.orchestrator.queries)
        assert h.running("req_a")
        h.orchestrator.release("req_b", "step")
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()
        assert sorted(h.orchestrator.queries) == sorted(["big research job", "meanwhile, what's 2+2"])


@pytest.mark.asyncio
async def test_message_while_only_a_background_task_runs_starts_immediately(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "background me")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.send({"type": "background", "request_id": "req_a"})
        await h.query("req_b", "new foreground question")
        assert not h.socket.of_type("user_message.queued")
        assert any(e["request_id"] == "req_b" for e in h.socket.of_type("route_result"))
        h.orchestrator.release("req_b", "step")
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()


@pytest.mark.asyncio
async def test_direct_model_answer_cannot_be_steered_so_message_runs_next(tmp_path) -> None:
    async with Harness(tmp_path, route="haiku") as h:
        release = asyncio.Event()
        original_stream = h.runtime.haiku_adapter.stream

        async def slow_stream(**kwargs):
            await release.wait()
            await original_stream(**kwargs)

        h.runtime.haiku_adapter.stream = slow_stream  # type: ignore[method-assign]
        await h.query("req_a", "hello")
        await wait_for(lambda: h.running("req_a"))
        await h.query("req_b", "and another thing")
        assert h.socket.of_type("user_message.queued")
        release.set()
        await h.wait_idle()
        assert [row[0] for row in h.transcript()] == ["user", "assistant", "user", "assistant"]
        assert h.transcript()[2] == ("user", "and another thing")


@pytest.mark.asyncio
async def test_message_during_classification_window_is_held_not_rejected(tmp_path) -> None:
    """Between 'query accepted' and 'worker started' the channel is busy too."""
    async with Harness(tmp_path) as h:
        gate = asyncio.Event()
        original = h.runtime.process_incoming_user_message

        async def slow_process(message):
            if message.get("request_id") == "req_a":
                await gate.wait()
            return await original(message)

        h.runtime.process_incoming_user_message = slow_process  # type: ignore[method-assign]
        h.runtime.registry.adapters["desktop"]._callback = slow_process  # noqa: SLF001
        first = asyncio.create_task(h.query("req_a", "first"))
        await wait_for(lambda: CHANNEL in h.runtime.mid_run._dispatching)  # noqa: SLF001
        await h.query("req_b", "second", delivery="queue")
        assert h.socket.of_type("user_message.queued")
        gate.set()
        await first
        await h.wait_idle()
        assert not h.socket.of_type("error")
        assert h.orchestrator.queries == ["first", "second"]


@pytest.mark.asyncio
async def test_resume_payload_carries_the_held_queue(tmp_path) -> None:
    async with Harness(tmp_path) as h:
        h.orchestrator.hold("req_a", "step")
        await h.query("req_a", "task")
        await wait_for(lambda: h.socket.of_type("task.created"))
        await h.query("req_b", "follow up", delivery="queue")
        payload = await h.runtime.build_resume_payload(channel=CHANNEL)
        assert [item["request_id"] for item in payload["mid_run_queue"]] == ["req_b"]
        assert payload["mid_run_queue"][0]["state"] == "queued"
        h.orchestrator.release("req_a", "step")
        await h.wait_idle()


def test_queue_release_only_touches_the_finished_runs_messages() -> None:
    queue = HeldMessageQueue()
    base = dict(channel=CHANNEL, payload={}, platform="desktop", created_at="t")
    a = HeldMessage(request_id="m1", content="x", mode="steer", **base)
    b = HeldMessage(request_id="m2", content="y", mode="steer", **base)
    assert queue.add(a) and queue.add(b)
    assert queue.add(HeldMessage(request_id="m1", content="dup", mode="steer", **base)) is False
    a.state, a.target_request_id = "injecting", "req_1"
    b.state, b.target_request_id = "injecting", "req_2"
    assert queue.release_target("req_1") == [a]
    assert a.state == "waiting" and b.state == "injecting"
    assert [item.request_id for item in queue.waiting(CHANNEL)] == ["m1"]
