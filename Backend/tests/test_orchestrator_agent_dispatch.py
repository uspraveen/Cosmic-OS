from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.runtime import OrchestratorRuntime
from registry.live_state import register_intent_index, write_heartbeat
from registry.store import RegistryStore
from shared import (
    AgentResult,
    EventEnvelope,
    Heartbeat,
    TaskEnvelope,
    TaskInProgress,
    parse_task_envelope,
    sign_task_envelope,
    verify_task_envelope,
    utcnow,
)


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], dict[str, object]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.expirations: dict[str, int] = {}
        self._counter = 0

    async def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = True) -> None:
        del id
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})

    async def xlen(self, stream: str) -> int:
        return len(self.streams.get(stream, []))

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        del approximate
        self._counter += 1
        message_id = f"{self._counter}-0"
        bucket = self.streams.setdefault(stream, [])
        bucket.append((message_id, dict(fields)))
        if maxlen is not None and len(bucket) > maxlen:
            del bucket[:-maxlen]
        return message_id

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del consumername
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        limit = count or 10**9
        for stream, start in streams.items():
            state = self.groups.setdefault((stream, groupname), {"delivered": set(), "acked": set()})
            delivered = state["delivered"]
            acked = state["acked"]
            messages: list[tuple[str, dict[str, str]]] = []
            for message_id, payload in self.streams.get(stream, []):
                if start == ">":
                    if message_id in delivered:
                        continue
                    delivered.add(message_id)
                    messages.append((message_id, payload))
                elif start == "0":
                    if message_id in delivered and message_id not in acked:
                        messages.append((message_id, payload))
                else:
                    raise AssertionError(f"Unsupported xreadgroup start value: {start}")
                if len(messages) >= limit:
                    break
            if messages:
                results.append((stream, messages))
        if not results and block:
            await asyncio.sleep(min(block / 1000.0, 0.02))
        return results

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        state = self.groups.setdefault((stream, group), {"delivered": set(), "acked": set()})
        state["acked"].add(message_id)
        return 1

    async def sadd(self, key: str, value: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return 1 if len(bucket) != before else 0

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket.update(mapping)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True

    async def scan(self, cursor: int = 0, *, match: str, count: int = 20) -> tuple[int, list[str]]:
        del count
        prefix = match[:-1] if match.endswith("*") else match
        keys = sorted(key for key in self.hashes if key.startswith(prefix))
        return 0, keys

    async def aclose(self) -> None:
        return


def _parent_task(secret: str) -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_parent_001",
        task_list_id="sess_20260315",
        parent_task_id=None,
        session_id="sess_20260315",
        sender="cosmic/gateway:1.0.0",
        recipient="cosmic/orchestrator:1.0.0",
        intent="orchestrator.process",
        input={"query": "Research YC S26 companies", "request_id": "req_parent_001"},
        input_artifacts=[],
        idempotency_key="idem_parent_001",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, secret)})


def _tabular_waiting_task(secret: str) -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_tabular_wait",
        task_list_id="sess_20260315",
        parent_task_id="tsk_parent_001",
        session_id="sess_20260315",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/tabular-agent:1.0.0",
        intent="tabular.reason_workbook",
        input={
            "bundle_id": "bundle_123",
            "artifact_id": "art_123",
            "goal": "Need tax context",
            "request_id": "req_parent_001",
        },
        input_artifacts=[],
        idempotency_key="idem_tabular_wait",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_001",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, secret)})


def _agent_card() -> dict[str, object]:
    return {
        "agent_id": "cosmic/research-agent:1.0.0",
        "display_name": "Research Agent",
        "description": "Specialist agent for deep research, web investigation, and session recall.",
        "sla": {
            "max_concurrency": 3,
            "heartbeat_ttl_sec": 30,
            "max_task_duration_sec": 180,
        },
        "intents": [
            {
                "name": "research.topic",
                "description": "Research a topic on the live web and return a structured summary.",
                "timeout_sec": 180,
                "input_schema_summary": {
                    "required": ["query"],
                    "properties": [
                        {"name": "query", "type": "string", "description": "Research query."},
                    ],
                },
            },
        ],
    }


def _calendar_agent_card() -> dict[str, object]:
    return {
        "agent_id": "cosmic/calendar-agent:1.0.0",
        "display_name": "Calendar Agent",
        "description": "Specialist agent for calendar scheduling and event management.",
        "sla": {
            "max_concurrency": 2,
            "heartbeat_ttl_sec": 30,
            "max_task_duration_sec": 180,
        },
        "intents": [
            {
                "name": "calendar.list_events",
                "description": "List calendar events.",
                "timeout_sec": 60,
            },
        ],
        "auth_requirements": {
            "calendar.list_events": {
                "provider": "google",
                "scopes": [
                    "https://www.googleapis.com/auth/calendar",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
            }
        },
    }


async def _register_agent(card: dict[str, object], redis_client: FakeRedis, registry_db_path) -> None:
    store = RegistryStore(registry_db_path)
    store.initialize()
    store.upsert_agent_card(card)
    await register_intent_index(str(card["agent_id"]), card, redis_client)
    await write_heartbeat(
        Heartbeat(
            agent_id=str(card["agent_id"]),
            instance_id="inst_research_001",
            healthy=True,
            current_load=0,
            max_concurrency=3,
            heartbeat_ttl_sec=30,
            last_seen=utcnow(),
        ),
        redis_client,
    )


async def _wait_for_stream(redis_client: FakeRedis, stream: str) -> dict[str, str]:
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        entries = redis_client.streams.get(stream, [])
        if entries:
            return entries[-1][1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for stream {stream}")


@pytest.mark.asyncio
async def test_orchestrator_dispatches_signed_child_task_and_waits_for_completed_event(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        agent_signing_secrets={"cosmic/research-agent:1.0.0": "research-secret"},
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger.db",
        agent_registry_db_path=tmp_path / "registry.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "YC S26 list"},
                wait_timeout_sec=1.0,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)

        assert child_task.parent_task_id == "tsk_parent_001"
        assert child_task.sender == "cosmic/orchestrator:1.0.0"
        assert child_task.recipient == "cosmic/research-agent:1.0.0"
        assert child_task.intent == "research.topic"
        assert child_task.session_id == "sess_20260315"
        assert child_task.channel == "desktop:desk_001"
        assert child_task.input["request_id"] == "req_parent_001"
        assert verify_task_envelope(child_task, "research-secret") is True
        assert verify_task_envelope(child_task, "gateway-secret") is False

        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.completed",
                    seq=1,
                    payload={"status": "completed", "output": {"sources": 2}, "artifacts": []},
                ).model_dump_json()
            },
        )

        result = await asyncio.wait_for(dispatch, timeout=2.0)

        assert isinstance(result, AgentResult)
        assert result.status == "completed"
        assert result.output == {"sources": 2}

        with sqlite3.connect(config.task_ledger_db_path) as connection:
            row = connection.execute(
                "SELECT status, result_json FROM tasks WHERE task_id = ?",
                (child_task.task_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "completed"
        stored_result = json.loads(row[1])
        assert stored_result["status"] == "completed"
        assert stored_result["output"] == {"sources": 2}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_dispatch_treats_rejected_event_as_failure(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_rejected.db",
        agent_registry_db_path=tmp_path / "registry_rejected.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "YC S26 list"},
                wait_timeout_sec=1.0,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)

        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.rejected",
                    seq=1,
                    payload={"reason": "unauthorized_sender", "sender": child_task.sender},
                ).model_dump_json()
            },
        )

        result = await asyncio.wait_for(dispatch, timeout=2.0)

        assert isinstance(result, AgentResult)
        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "TASK_REJECTED"

        with sqlite3.connect(config.task_ledger_db_path) as connection:
            row = connection.execute(
                "SELECT status, error_code FROM tasks WHERE task_id = ?",
                (child_task.task_id,),
            ).fetchone()
        assert row == ("failed", "TASK_REJECTED")
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_dispatch_returns_in_progress_for_deferred_event(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_deferred.db",
        agent_registry_db_path=tmp_path / "registry_deferred.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "YC S26 list"},
                wait_timeout_sec=1.0,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)

        deferred = TaskInProgress(
            task_id=child_task.task_id,
            idempotency_key=child_task.idempotency_key,
            executing_since=utcnow(),
            check_after_sec=7,
        )
        await fake_redis.xadd(
            "streams:events",
            {"event": EventEnvelope(
                task_id=child_task.task_id,
                agent_id="cosmic/research-agent:1.0.0",
                event_type="task.deferred",
                seq=1,
                payload=deferred.model_dump(mode="json"),
            ).model_dump_json()},
        )

        result = await asyncio.wait_for(dispatch, timeout=2.0)

        assert isinstance(result, TaskInProgress)
        assert result.task_id == child_task.task_id
        assert result.check_after_sec == 7

        with sqlite3.connect(config.task_ledger_db_path) as connection:
            row = connection.execute(
                "SELECT status, result_json FROM tasks WHERE task_id = ?",
                (child_task.task_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "deferred"
        stored_result = json.loads(row[1])
        assert stored_result["task_id"] == child_task.task_id
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_health_snapshot_reports_agent_dispatch_state(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_health.db",
        agent_registry_db_path=tmp_path / "registry_health.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        snapshot = await runtime.get_agent_dispatch_snapshot()
    finally:
        await runtime.stop()

    assert snapshot["enabled"] is True
    assert snapshot["consumer_running"] is True
    assert snapshot["registered_agents"] == 1
    assert snapshot["healthy_agents"] == 1
    assert snapshot["agents"][0]["agent_id"] == "cosmic/research-agent:1.0.0"
    assert snapshot["agents"][0]["healthy_instance"] is True


@pytest.mark.asyncio
async def test_search_agent_catalog_returns_intent_level_matches(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_catalog.db",
        agent_registry_db_path=tmp_path / "registry_catalog.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        result = await runtime.search_agent_catalog(
            query="deep research summary",
            limit=3,
            require_healthy=True,
        )
    finally:
        await runtime.stop()

    assert result["count"] == 1
    assert result["message"] == "Found 1 matching specialist intents."
    match = result["matches"][0]
    assert match["intent"] == "research.topic"
    assert match["display_name"] == "Research Agent"
    assert match["healthy"] is True
    assert match["input_schema_summary"]["required"] == ["query"]


@pytest.mark.asyncio
async def test_dispatch_agent_task_records_usage_and_refreshes_featured_specialists_on_successful_completion(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        agent_signing_secrets={"cosmic/research-agent:1.0.0": "research-secret"},
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_featured.db",
        agent_registry_db_path=tmp_path / "registry_featured.db",
        featured_specialists_refresh_sec=300,
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch_task = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "YC S26 companies"},
                agent_id="cosmic/research-agent:1.0.0",
                wait_timeout_sec=1.0,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)
        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.completed",
                    seq=1,
                    payload={"status": "completed", "output": {"sources": 2}, "artifacts": []},
                ).model_dump_json()
            },
        )
        result = await dispatch_task
        featured = runtime.registry_store.list_featured_specialists(limit=3)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_reverse_delegate_registers_and_resumes_waiting_specialist(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_reverse.db",
        agent_registry_db_path=tmp_path / "registry_reverse.db",
        agent_signing_secrets={
            "cosmic/tabular-agent:1.0.0": "tabular-secret",
            "cosmic/research-agent:1.0.0": "research-secret",
        },
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        waiting_task = _tabular_waiting_task("tabular-secret")
        runtime.task_ledger.create_task(waiting_task)

        reverse_task = TaskEnvelope(
            task_id="tsk_reverse_delegate",
            task_list_id=waiting_task.task_list_id,
            parent_task_id=waiting_task.task_id,
            session_id=waiting_task.session_id,
            sender="cosmic/tabular-agent:1.0.0",
            recipient="cosmic/orchestrator:1.0.0",
            intent="orchestrator.delegate",
            input={
                "target_intent": "research.topic",
                "target_input": {"query": "2026 tax filing threshold"},
                "resume_payload": {
                    "transcript": "Need an external definition.",
                    "tool_round": 1,
                    "clarify_used": False,
                    "delegate_used": True,
                    "steps_log": [],
                },
            },
            input_artifacts=[],
            idempotency_key="idem_reverse_delegate",
            priority="normal",
            signature="",
            created_at=utcnow(),
            source="agent",
            source_id="cosmic/tabular-agent:1.0.0",
            channel=waiting_task.channel,
        )
        reverse_task = reverse_task.model_copy(
            update={"signature": sign_task_envelope(reverse_task, "tabular-secret")}
        )

        ack = await runtime.accept_reverse_task(reverse_task)
        assert ack["status"] == "registered"
        assert ack["reverse_task_id"] == "tsk_reverse_delegate"

        await runtime._handle_agent_event(
            EventEnvelope(
                task_id=waiting_task.task_id,
                agent_id="cosmic/tabular-agent:1.0.0",
                event_type="task.suspended",
                seq=1,
                payload={
                    "reason": "tabular_delegate",
                    "reverse_task_id": reverse_task.task_id,
                },
            )
        )

        delegated_fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:normal")
        delegated_task = parse_task_envelope(delegated_fields)
        assert delegated_task.parent_task_id == reverse_task.task_id
        assert delegated_task.intent == "research.topic"
        assert delegated_task.input["query"] == "2026 tax filing threshold"
        assert verify_task_envelope(delegated_task, "research-secret") is True

        await runtime._handle_agent_event(
            EventEnvelope(
                task_id=delegated_task.task_id,
                agent_id="cosmic/research-agent:1.0.0",
                event_type="task.completed",
                seq=1,
                payload={
                    "status": "completed",
                    "output": {"summary": "Threshold found."},
                    "artifacts": [],
                },
            )
        )

        resumed_fields = fake_redis.streams["streams:cosmic/tabular-agent:1.0.0:high"][-1][1]
        resumed_task = parse_task_envelope(resumed_fields)
        assert resumed_task.intent == "agent.resume"
        assert resumed_task.parent_task_id == waiting_task.task_id
        assert resumed_task.input["reverse_task"]["reverse_task_id"] == reverse_task.task_id
        assert resumed_task.input["reverse_task"]["delegated_task_id"] == delegated_task.task_id
        assert resumed_task.input["reverse_result"]["status"] == "completed"
        assert resumed_task.input["reverse_result"]["output"] == {"summary": "Threshold found."}
        assert verify_task_envelope(resumed_task, "tabular-secret") is True

        reverse_wait = runtime.task_ledger.get_reverse_task_wait(reverse_task.task_id)
        assert reverse_wait is not None
        assert reverse_wait["status"] == "resumed"
        assert reverse_wait["delegated_task_id"] == delegated_task.task_id
        assert reverse_wait["resumed_task_id"] == resumed_task.task_id

        reverse_record = runtime.task_ledger.get_task(reverse_task.task_id)
        assert reverse_record is not None
        assert reverse_record["status"] == "completed"
    finally:
        await runtime.stop()
    assert isinstance(result, AgentResult)
    assert result.status == "completed"
    assert len(featured) == 1
    assert featured[0]["agent_id"] == "cosmic/research-agent:1.0.0"
    assert featured[0]["usage_count"] == 1


@pytest.mark.asyncio
async def test_orchestrator_dispatch_tracks_suspended_and_resumed_until_completed(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        agent_signing_secrets={"cosmic/research-agent:1.0.0": "research-secret"},
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_suspended.db",
        agent_registry_db_path=tmp_path / "registry_suspended.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "Which of these companies should I use?"},
                wait_timeout_sec=1.5,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)

        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.suspended",
                    seq=1,
                    payload={"reason": "clarify", "question": "Use source A or B?"},
                ).model_dump_json()
            },
        )

        deadline = asyncio.get_running_loop().time() + 1.0
        suspended_row = None
        while asyncio.get_running_loop().time() < deadline:
            suspended_row = runtime.task_ledger.get_task(child_task.task_id)
            if suspended_row and suspended_row["status"] == "suspended":
                break
            await asyncio.sleep(0.01)
        assert suspended_row is not None
        assert suspended_row["status"] == "suspended"
        assert dispatch.done() is False

        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.resumed",
                    seq=2,
                    payload={"clarify_status": "answered", "reply_excerpt": "source A"},
                ).model_dump_json()
            },
        )

        deadline = asyncio.get_running_loop().time() + 1.0
        resumed_row = None
        while asyncio.get_running_loop().time() < deadline:
            resumed_row = runtime.task_ledger.get_task(child_task.task_id)
            if resumed_row and resumed_row["status"] == "running":
                break
            await asyncio.sleep(0.01)
        assert resumed_row is not None
        assert resumed_row["status"] == "running"
        assert dispatch.done() is False

        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.completed",
                    seq=3,
                    payload={"status": "completed", "output": {"answer": "source A"}, "artifacts": []},
                ).model_dump_json()
            },
        )

        result = await asyncio.wait_for(dispatch, timeout=2.0)
        assert isinstance(result, AgentResult)
        assert result.status == "completed"
        assert result.output == {"answer": "source A"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_dispatch_passes_input_artifacts_to_child_task(tmp_path) -> None:
    fake_redis = FakeRedis()
    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        agent_signing_secrets={"cosmic/research-agent:1.0.0": "research-secret"},
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_artifacts.db",
        agent_registry_db_path=tmp_path / "registry_artifacts.db",
    )
    await _register_agent(_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        dispatch = asyncio.create_task(
            runtime.dispatch_agent_task(
                parent_task=_parent_task("gateway-secret"),
                intent="research.topic",
                input_payload={"query": "re-process this export"},
                input_artifacts=[
                    {
                        "artifact_id": "out_prev_1",
                        "task_id": "tsk_prev",
                        "mime": "text/csv",
                        "path": "runs/artifacts/tsk_prev/out/list.csv",
                        "filename": "list.csv",
                    }
                ],
                wait_timeout_sec=1.0,
            )
        )
        fields = await _wait_for_stream(fake_redis, "streams:cosmic/research-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)
        assert child_task.input_artifacts == [
            {
                "artifact_id": "out_prev_1",
                "task_id": "tsk_prev",
                "mime": "text/csv",
                "path": "runs/artifacts/tsk_prev/out/list.csv",
                "filename": "list.csv",
            }
        ]
        await fake_redis.xadd(
            "streams:events",
            {
                "event": EventEnvelope(
                    task_id=child_task.task_id,
                    agent_id="cosmic/research-agent:1.0.0",
                    event_type="task.completed",
                    seq=1,
                    payload={"status": "completed", "output": {"ok": True}, "artifacts": []},
                ).model_dump_json()
            },
        )
        result = await dispatch
        assert isinstance(result, AgentResult)
        assert result.output["ok"] is True
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_dispatch_injects_resolved_auth_into_child_task(tmp_path) -> None:
    fake_redis = FakeRedis()
    captured_requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/credentials/resolve":
            captured_requests.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "credential_ref": "cred_google_123",
                    "access_token": "token-google-123",
                    "provider": "google",
                    "scopes": [
                        "https://www.googleapis.com/auth/calendar",
                        "https://www.googleapis.com/auth/calendar.events",
                    ],
                    "expires_at": "2026-03-30T18:00:00+00:00",
                    "account_id": "acc_work",
                },
            )
        return httpx.Response(200)

    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        internal_token="internal-token",
        gateway_url="http://gateway.test",
        anthropic_api_key="anthropic-key",
        agent_signing_secrets={"cosmic/calendar-agent:1.0.0": "calendar-secret"},
        task_ledger_db_path=tmp_path / "task_ledger_calendar_auth.db",
        agent_registry_db_path=tmp_path / "registry_calendar_auth.db",
    )
    await _register_agent(_calendar_agent_card(), fake_redis, config.agent_registry_db_path)
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        result = await runtime.dispatch_agent_task(
            parent_task=_parent_task("gateway-secret"),
            intent="calendar.list_events",
            input_payload={"query": "What is on my work calendar tomorrow?", "account_hint": "work"},
            wait_timeout_sec=0.0,
        )
        assert isinstance(result, TaskInProgress)

        fields = await _wait_for_stream(fake_redis, "streams:cosmic/calendar-agent:1.0.0:high")
        child_task = parse_task_envelope(fields)
        assert child_task.input["auth"]["access_token"] == "token-google-123"
        assert child_task.input["auth"]["credential_ref"] == "cred_google_123"
        assert child_task.input["account_hint"] == "work"

        assert captured_requests == [
            {
                "provider": "google",
                "required_scopes": [
                    "https://www.googleapis.com/auth/calendar",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
                "session_id": "sess_20260315",
                "operation_mode": "read",
                "allow_primary_fallback": True,
                "account_hint": "work",
            }
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orchestrator_refresh_reverse_task_resumes_waiting_specialist_with_new_auth(tmp_path) -> None:
    fake_redis = FakeRedis()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/credentials/refresh":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"credential_ref": "cred_google_456"}
            return httpx.Response(
                200,
                json={
                    "credential_ref": "cred_google_456",
                    "access_token": "refreshed-token",
                    "provider": "google",
                    "scopes": ["https://www.googleapis.com/auth/calendar"],
                    "expires_at": "2026-03-30T19:00:00+00:00",
                },
            )
        return httpx.Response(200)

    config = OrchestratorConfig(
        signing_secret="gateway-secret",
        internal_token="internal-token",
        gateway_url="http://gateway.test",
        anthropic_api_key="anthropic-key",
        task_ledger_db_path=tmp_path / "task_ledger_refresh.db",
        agent_registry_db_path=tmp_path / "registry_refresh.db",
        agent_signing_secrets={"cosmic/calendar-agent:1.0.0": "calendar-secret"},
    )
    runtime = OrchestratorRuntime(
        config,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        redis_client=fake_redis,
    )
    await runtime.start()
    try:
        waiting_task = TaskEnvelope(
            task_id="tsk_calendar_wait",
            task_list_id="sess_20260315",
            parent_task_id="tsk_parent_001",
            session_id="sess_20260315",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/calendar-agent:1.0.0",
            intent="calendar.list_events",
            input={"query": "what is on my calendar tomorrow", "request_id": "req_parent_001"},
            input_artifacts=[],
            idempotency_key="idem_calendar_wait",
            priority="high",
            signature="",
            created_at=utcnow(),
            source="user",
            source_id="desktop",
            channel="desktop:desk_001",
        )
        waiting_task = waiting_task.model_copy(
            update={"signature": sign_task_envelope(waiting_task, "calendar-secret")}
        )
        runtime.task_ledger.create_task(waiting_task)

        reverse_task = TaskEnvelope(
            task_id="tsk_reverse_refresh",
            task_list_id=waiting_task.task_list_id,
            parent_task_id=waiting_task.task_id,
            session_id=waiting_task.session_id,
            sender="cosmic/calendar-agent:1.0.0",
            recipient="cosmic/orchestrator:1.0.0",
            intent="orchestrator.refresh_credential",
            input={"credential_ref": "cred_google_456", "provider": "google"},
            input_artifacts=[],
            idempotency_key="idem_reverse_refresh",
            priority="normal",
            signature="",
            created_at=utcnow(),
            source="agent",
            source_id="cosmic/calendar-agent:1.0.0",
            channel=waiting_task.channel,
        )
        reverse_task = reverse_task.model_copy(
            update={"signature": sign_task_envelope(reverse_task, "calendar-secret")}
        )

        ack = await runtime.accept_reverse_task(reverse_task)
        assert ack["status"] == "registered"

        await runtime._handle_agent_event(
            EventEnvelope(
                task_id=waiting_task.task_id,
                agent_id="cosmic/calendar-agent:1.0.0",
                event_type="task.suspended",
                seq=1,
                payload={"reason": "auth_refresh", "credential_ref": "cred_google_456"},
            )
        )

        resumed_fields = await _wait_for_stream(fake_redis, "streams:cosmic/calendar-agent:1.0.0:high")
        resumed_task = parse_task_envelope(resumed_fields)
        assert resumed_task.intent == "agent.resume"
        assert resumed_task.input["auth"]["access_token"] == "refreshed-token"
        assert resumed_task.input["reverse_task"]["intent"] == "orchestrator.refresh_credential"
        assert resumed_task.input["reverse_result"]["output"]["refreshed"] is True

        reverse_wait = runtime.task_ledger.get_reverse_task_wait(reverse_task.task_id)
        assert reverse_wait is not None
        assert reverse_wait["status"] == "resumed"
    finally:
        await runtime.stop()
