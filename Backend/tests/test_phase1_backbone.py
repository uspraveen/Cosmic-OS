from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from orchestrator.store.ledger import TaskLedger
from registry.live_state import find_available_instance, register_intent_index, write_heartbeat
from registry.store import RegistryStore
from shared import (
    BackpressureError,
    EventEnvelope,
    Heartbeat,
    TaskEnvelope,
    dispatch_task,
    emit_event,
    heartbeat_key,
    intent_members_key,
    parse_event_envelope,
    parse_task_envelope,
    prepare_for_redispatch,
    sign_task_envelope,
    task_stream_name,
    utcnow,
)


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.expirations: dict[str, int] = {}
        self._counter = 0

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

    async def sadd(self, key: str, value: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return 1 if len(bucket) != before else 0

    async def srem(self, key: str, value: str) -> int:
        bucket = self.sets.setdefault(key, set())
        if value in bucket:
            bucket.remove(value)
            return 1
        return 0

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


def _sample_task(secret: str = "signing-secret") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_phase1",
        task_list_id="sess_phase1",
        session_id="sess_phase1",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/research-agent:1.0.0",
        intent="research.topic",
        input={"query": "Find recent YC news"},
        idempotency_key="idem_phase1",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:desk_phase1",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, secret)})


def _sample_card() -> dict[str, object]:
    return {
        "agent_id": "cosmic/research-agent:1.0.0",
        "display_name": "Research Agent",
        "sla": {
            "max_concurrency": 3,
            "heartbeat_ttl_sec": 30,
            "max_task_duration_sec": 180,
        },
        "intents": [
            {"name": "research.topic", "timeout_sec": 180},
            {"name": "research.extract", "timeout_sec": 90},
        ],
    }


def _sample_card_with_id(agent_id: str, display_name: str, intent_name: str) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "sla": {
            "max_concurrency": 2,
            "heartbeat_ttl_sec": 30,
            "max_task_duration_sec": 180,
        },
        "intents": [
            {"name": intent_name, "timeout_sec": 180},
        ],
    }


@pytest.mark.asyncio
async def test_shared_redis_bus_dispatch_and_parse_roundtrip() -> None:
    client = FakeRedis()
    task = _sample_task()

    result = await dispatch_task(task, client)

    assert result.stream == task_stream_name(task.recipient, task.priority)
    stored = client.streams[result.stream][0][1]
    parsed = parse_task_envelope(stored)
    assert parsed.task_id == task.task_id
    assert parsed.intent == "research.topic"


@pytest.mark.asyncio
async def test_shared_redis_bus_enforces_backpressure() -> None:
    client = FakeRedis()
    stream = task_stream_name("cosmic/research-agent:1.0.0", "high")
    client.streams[stream] = [("1-0", {"envelope": "{}"})]
    task = _sample_task()

    with pytest.raises(BackpressureError):
        await dispatch_task(task, client, stream_maxlen=0, stream_maxlen_approx=1)


@pytest.mark.asyncio
async def test_shared_event_envelope_emit_and_parse_roundtrip() -> None:
    client = FakeRedis()
    event = EventEnvelope(
        task_id="tsk_phase1",
        agent_id="cosmic/research-agent:1.0.0",
        event_type="task.progress",
        seq=1,
        payload={"message": "Searching sources"},
    )

    result = await emit_event(event, client)
    parsed = parse_event_envelope(client.streams[result.stream][0][1])

    assert result.stream == "streams:events"
    assert parsed.event_type == "task.progress"
    assert parsed.payload["message"] == "Searching sources"


def test_prepare_for_redispatch_preserves_idempotency_and_updates_epoch() -> None:
    original = _sample_task()
    updated = prepare_for_redispatch(original, new_epoch=7, signing_secret="signing-secret")

    assert updated.idempotency_key == original.idempotency_key
    assert updated.leader_epoch == 7
    assert updated.contract_version == "1.0"
    assert updated.signature != ""


def test_registry_store_persists_card_and_intents(tmp_path: Path) -> None:
    store = RegistryStore(tmp_path / "registry.db")
    store.initialize()
    store.upsert_agent_card(_sample_card())

    card = store.get_card("cosmic/research-agent:1.0.0")
    assert card is not None
    assert card["display_name"] == "Research Agent"
    intents = store.list_intents("cosmic/research-agent:1.0.0")
    assert [item["intent"] for item in intents] == ["research.extract", "research.topic"]
    matches = store.list_agents_for_intent("research.topic")
    assert len(matches) == 1
    assert matches[0]["agent_id"] == "cosmic/research-agent:1.0.0"


def test_registry_store_tracks_usage_and_refreshes_featured_specialists(tmp_path: Path) -> None:
    store = RegistryStore(tmp_path / "registry.db")
    store.initialize()
    store.upsert_agent_card(_sample_card())

    now = utcnow()
    store.record_agent_usage(
        "cosmic/research-agent:1.0.0",
        "research.topic",
        used_at=now,
    )
    store.record_agent_usage(
        "cosmic/research-agent:1.0.0",
        "research.extract",
        used_at=now - timedelta(days=1),
    )

    featured = store.refresh_featured_specialists(limit=3, lookback_days=14, refreshed_at=now)

    assert len(featured) == 1
    assert featured[0]["agent_id"] == "cosmic/research-agent:1.0.0"
    assert featured[0]["usage_count"] == 2
    assert featured[0]["common_intents"] == ["research.extract", "research.topic"] or featured[0]["common_intents"] == ["research.topic", "research.extract"]


def test_registry_store_seeds_new_specialists_and_drops_only_after_15_days_inactive(tmp_path: Path) -> None:
    store = RegistryStore(tmp_path / "registry.db")
    store.initialize()
    store.upsert_agent_card(_sample_card_with_id("cosmic/tabular-agent:1.0.0", "Tabular Agent", "tabular.query_workbook"))
    store.upsert_agent_card(_sample_card_with_id("cosmic/stale-agent:1.0.0", "Stale Agent", "stale.intent"))

    recent_now = utcnow()
    stale_then = recent_now - timedelta(days=16)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agents
            SET registered_at = ?, updated_at = ?
            WHERE agent_id = ?
            """,
            (
                stale_then.isoformat().replace("+00:00", "Z"),
                stale_then.isoformat().replace("+00:00", "Z"),
                "cosmic/stale-agent:1.0.0",
            ),
        )
        connection.commit()

    featured = store.refresh_featured_specialists(limit=5, lookback_days=15, refreshed_at=recent_now)
    featured_ids = [item["agent_id"] for item in featured]

    assert "cosmic/tabular-agent:1.0.0" in featured_ids
    assert "cosmic/stale-agent:1.0.0" not in featured_ids
    seeded = next(item for item in featured if item["agent_id"] == "cosmic/tabular-agent:1.0.0")
    assert seeded["usage_count"] == 0
    assert seeded["common_intents"] == ["tabular.query_workbook"]


@pytest.mark.asyncio
async def test_registry_live_state_finds_healthy_available_instance() -> None:
    client = FakeRedis()
    card = _sample_card()
    await register_intent_index("cosmic/research-agent:1.0.0", card, client)
    heartbeat = Heartbeat(
        agent_id="cosmic/research-agent:1.0.0",
        instance_id="inst_001",
        healthy=True,
        current_load=1,
        max_concurrency=3,
        heartbeat_ttl_sec=30,
        last_seen=utcnow(),
    )
    key = await write_heartbeat(heartbeat, client)

    assert key == heartbeat_key("cosmic/research-agent:1.0.0", "inst_001")
    assert client.expirations[key] == 35
    found = await find_available_instance("research.topic", client)
    assert found == ("cosmic/research-agent:1.0.0", "inst_001")


@pytest.mark.asyncio
async def test_registry_live_state_skips_stale_or_overloaded_instances() -> None:
    client = FakeRedis()
    card = _sample_card()
    await register_intent_index("cosmic/research-agent:1.0.0", card, client)
    stale = Heartbeat(
        agent_id="cosmic/research-agent:1.0.0",
        instance_id="inst_stale",
        healthy=True,
        current_load=0,
        max_concurrency=3,
        heartbeat_ttl_sec=30,
        last_seen=utcnow() - timedelta(seconds=31),
    )
    overloaded = Heartbeat(
        agent_id="cosmic/research-agent:1.0.0",
        instance_id="inst_busy",
        healthy=True,
        current_load=3,
        max_concurrency=3,
        heartbeat_ttl_sec=30,
        last_seen=utcnow(),
    )
    await write_heartbeat(stale, client)
    await write_heartbeat(overloaded, client)

    found = await find_available_instance("research.topic", client)
    assert found == (None, None)


@pytest.mark.asyncio
async def test_registry_live_state_treats_degraded_as_available_but_reauth_required_unavailable() -> None:
    client = FakeRedis()
    card = _sample_card()
    await register_intent_index("cosmic/research-agent:1.0.0", card, client)
    degraded = Heartbeat(
        agent_id="cosmic/research-agent:1.0.0",
        instance_id="inst_degraded",
        healthy=True,
        current_load=0,
        max_concurrency=3,
        heartbeat_ttl_sec=30,
        last_seen=utcnow(),
    )
    reauth_required = Heartbeat(
        agent_id="cosmic/research-agent:1.0.0",
        instance_id="inst_reauth",
        healthy=False,
        current_load=0,
        max_concurrency=3,
        heartbeat_ttl_sec=30,
        last_seen=utcnow(),
    )
    await write_heartbeat(degraded, client, status="degraded", details={"tool": "gmail"})
    await write_heartbeat(reauth_required, client, status="reauth_required", details={"tool": "gmail"})

    found = await find_available_instance("research.topic", client)
    assert found == ("cosmic/research-agent:1.0.0", "inst_degraded")


def test_task_ledger_initializes_plan_tables_and_roundtrips_steps(tmp_path: Path) -> None:
    ledger = TaskLedger(tmp_path / "task_ledger.db")
    ledger.initialize()
    ledger.create_plan(
        plan_id="plan_001",
        session_id="sess_phase1",
        original_query="Research YC batch companies",
        plan_json={"steps": [{"step_number": 1, "description": "Search sources"}]},
        source="user",
        source_id="desktop",
        channel="desktop:desk_phase1",
        status="planning",
        total_steps=1,
    )
    ledger.create_plan_step(
        step_id="plan_001_step_001",
        plan_id="plan_001",
        step_number=1,
        description="Search sources",
        intent="research.topic",
        agent_id="cosmic/research-agent:1.0.0",
        depends_on=[],
        input_json={"query": "YC S26 companies"},
    )

    plan = ledger.get_plan("plan_001")
    steps = ledger.list_plan_steps("plan_001")

    assert plan is not None
    assert plan["plan_json"]["steps"][0]["description"] == "Search sources"
    assert steps == [
        {
            "step_id": "plan_001_step_001",
            "plan_id": "plan_001",
            "step_number": 1,
            "description": "Search sources",
            "intent": "research.topic",
            "agent_id": "cosmic/research-agent:1.0.0",
            "depends_on": [],
            "status": "pending",
            "task_id": None,
            "attempt": 0,
            "max_attempts": 3,
            "input_json": {"query": "YC S26 companies"},
            "output_json": None,
            "started_at": None,
            "completed_at": None,
        }
    ]


def test_event_envelope_validates_positive_seq() -> None:
    with pytest.raises(ValueError):
        EventEnvelope(
            task_id="tsk_phase1",
            agent_id="cosmic/research-agent:1.0.0",
            event_type="task.progress",
            seq=0,
        )


def test_heartbeat_validates_non_negative_fields() -> None:
    with pytest.raises(ValueError):
        Heartbeat(
            agent_id="cosmic/research-agent:1.0.0",
            instance_id="inst_bad",
            healthy=True,
            current_load=-1,
            max_concurrency=1,
            heartbeat_ttl_sec=30,
        )


def test_intent_members_key_is_stable() -> None:
    assert intent_members_key("research.topic") == "intent:research.topic"
