from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from registry.store import RegistryStore
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentResult, TaskEnvelope, sign_task_envelope, utcnow
from shared.idempotency import execute_with_idempotency
from shared.memory_tools import MemoryRead, MemoryWrite
from shared.redis_bus import parse_event_envelope, task_stream_name


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.expirations: dict[str, int] = {}
        self.groups: set[tuple[str, str]] = set()
        self.acks: list[tuple[str, str, str]] = []
        self.lists: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}
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

    async def xgroup_create(self, stream: str, group: str, *, id: str = "0", mkstream: bool = False) -> bool:
        del id
        if mkstream:
            self.streams.setdefault(stream, [])
        if (stream, group) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add((stream, group))
        return True

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int = 1,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del groupname, consumername, block
        for stream in streams:
            bucket = self.streams.get(stream, [])
            if not bucket:
                continue
            items = bucket[:count]
            del bucket[:count]
            return [(stream, items)]
        return []

    async def xautoclaim(
        self,
        stream: str,
        groupname: str,
        consumername: str,
        *,
        min_idle_time: int,
        start_id: str,
        count: int = 1,
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        del stream, groupname, consumername, min_idle_time, start_id, count
        return "0-0", []

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acks.append((stream, group, message_id))
        return 1

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

    async def set(self, key: str, value: str, *, nx: bool | None = None, ex: int | None = None) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        existed = key in self.kv
        self.kv.pop(key, None)
        return 1 if existed else 0

    async def incr(self, key: str) -> int:
        current = int(self.kv.get(key, "0"))
        current += 1
        self.kv[key] = str(current)
        return current

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)


def _write_agent_card(tmp_path: Path) -> Path:
    card = tmp_path / "agent_card.yaml"
    card.write_text(
        """
agent_id: cosmic/test-agent:1.0.0
display_name: Test Agent
description: Test worker for runtime coverage.
intents:
  - name: test.echo
    description: Echo the request
    input_schema: schemas/intents/test.echo.input.json
    output_schema: schemas/intents/test.echo.output.json
    timeout_sec: 30
policies:
  network_access: false
  writable_paths:
    - agents/test_agent/store
    - agents/test_agent/runtime
  tool_access: []
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  intent_authorization:
    test.echo:
      - cosmic/orchestrator:1.0.0
sla:
  max_concurrency: 2
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: 30
  health_endpoint: /health
  retry_policy:
    max_attempts: 3
    backoff: exponential
    backoff_base_sec: 2
    backoff_max_sec: 60
    retryable_codes: [TIMEOUT]
    non_retryable_codes: [INVALID_INPUT]
stream_key: streams:cosmic/test-agent:1.0.0
version_info:
  semver: 1.0.0
  released_at: 2026-03-15
  deprecated_at: null
  remove_after: null
  changelog: CHANGELOG.md
""".strip(),
        encoding="utf-8",
    )
    return card


def _write_google_agent_card(tmp_path: Path) -> Path:
    card = tmp_path / "agent_card.yaml"
    card.write_text(
        """
agent_id: cosmic/gmail-agent:1.0.0
display_name: Gmail Agent
description: Test Google-backed worker.
intents:
  - name: gmail.search
    description: Search Gmail
auth_requirements:
  gmail.search:
    provider: google
    scopes:
      - https://www.googleapis.com/auth/gmail.modify
policies:
  allowed_senders:
    - cosmic/orchestrator:1.0.0
sla:
  max_concurrency: 2
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: 30
  provider_health_probe_interval_sec: 30
stream_key: streams:cosmic/gmail-agent:1.0.0
""".strip(),
        encoding="utf-8",
    )
    return card


def _signed_task(*, agent_id: str, secret: str, sender: str = "cosmic/orchestrator:1.0.0") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id="tsk_agent_runtime",
        task_list_id="sess_agent_runtime",
        session_id="sess_agent_runtime",
        sender=sender,
        recipient=agent_id,
        intent="test.echo",
        input={"query": "hello"},
        idempotency_key="idem_agent_runtime",
        priority="high",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, secret)})


class DummyAgent(AgentRuntime):
    def __init__(self, *args, result: AgentResult | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.result = result or AgentResult(status="completed", output={"reply": "ok"}, artifacts=[], error=None)
        self.started_up = False

    async def on_startup(self) -> None:
        self.started_up = True

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        if task.input.get("make_plan"):
            assert self.step_plan is not None
            await self.step_plan.create(["step one", "step two"])
        return self.result


class BlockingAgent(AgentRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started_event = asyncio.Event()
        self.finish_event = asyncio.Event()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        del task
        self.started_event.set()
        await self.finish_event.wait()
        return AgentResult(status="completed", output={"reply": "done"}, artifacts=[], error=None)


def test_agent_runtime_schema_summary_includes_numeric_constraints(tmp_path: Path) -> None:
    schema_dir = tmp_path / "schemas" / "intents"
    schema_dir.mkdir(parents=True)
    (schema_dir / "x.search.input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The X search query."},
                    "max_posts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 30,
                        "description": "Maximum number of notable posts.",
                    },
                },
                "required": ["query"],
            }
        ),
        encoding="utf-8",
    )
    card_path = tmp_path / "agent_card.yaml"
    card_path.write_text(
        """
agent_id: cosmic/x-twitter-search-agent:1.0.0
display_name: X Twitter Search Agent
description: Test X agent.
intents:
  - name: x.search
    description: Search X.
    input_schema: schemas/intents/x.search.input.json
stream_key: streams:cosmic/x-twitter-search-agent:1.0.0
""".strip(),
        encoding="utf-8",
    )

    agent = DummyAgent(
        agent_card_path=card_path,
        redis_client=FakeRedis(),
        registry_db_path=tmp_path / "registry.db",
        instance_id="inst_001",
        agent_secret="agent-secret",
    )

    properties = agent.agent_card["intents"][0]["input_schema_summary"]["properties"]
    max_posts = next(item for item in properties if item["name"] == "max_posts")
    assert max_posts["minimum"] == 1
    assert max_posts["maximum"] == 30
    assert max_posts["default"] == 30


@pytest.mark.asyncio
async def test_agent_runtime_registers_card_and_heartbeats(tmp_path: Path) -> None:
    card_path = _write_agent_card(tmp_path)
    client = FakeRedis()
    registry_db = tmp_path / "registry.db"
    agent = DummyAgent(
        agent_card_path=card_path,
        redis_client=client,
        registry_db_path=registry_db,
        instance_id="inst_001",
        agent_secret="agent-secret",
    )

    await agent.register()

    store = RegistryStore(registry_db)
    card = store.get_card("cosmic/test-agent:1.0.0")
    assert card is not None
    assert card["display_name"] == "Test Agent"
    assert agent.started_up is True
    assert ("streams:cosmic/test-agent:1.0.0:high", "workers") in client.groups
    assert client.hashes["registry:cosmic/test-agent:1.0.0:inst_001"]["status"] == "healthy"
    assert client.expirations["registry:cosmic/test-agent:1.0.0:inst_001"] == 35

    await agent.stop()


@pytest.mark.asyncio
async def test_agent_runtime_publishes_google_provider_auth_health(tmp_path: Path) -> None:
    card_path = _write_google_agent_card(tmp_path)
    client = FakeRedis()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "status": "reauth_required",
                "healthy": False,
                "available": False,
                "provider": "google",
                "tool": "gmail",
                "account_count": 1,
                "reauth_required_count": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        agent = DummyAgent(
            agent_card_path=card_path,
            redis_client=client,
            registry_db_path=tmp_path / "registry.db",
            instance_id="inst_001",
            agent_secret="agent-secret",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            http_client=http_client,
        )

        await agent.register()

        heartbeat_key = "registry:cosmic/gmail-agent:1.0.0:inst_001"
        assert client.hashes[heartbeat_key]["status"] == "reauth_required"
        assert client.hashes[heartbeat_key]["health_details"]
        assert requests[-1] == {
            "agent_id": "cosmic/gmail-agent:1.0.0",
            "tool": "gmail",
            "required_scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        }

        await agent.stop()


@pytest.mark.asyncio
async def test_agent_runtime_refreshes_heartbeat_on_load_transition(tmp_path: Path) -> None:
    card_path = _write_agent_card(tmp_path)
    client = FakeRedis()
    agent = BlockingAgent(
        agent_card_path=card_path,
        redis_client=client,
        registry_db_path=tmp_path / "registry.db",
        instance_id="inst_001",
        agent_secret="agent-secret",
    )
    await agent.register()

    task = _signed_task(agent_id=agent.agent_id, secret="agent-secret")
    stream = task_stream_name(agent.agent_id, task.priority)
    client.streams[stream] = [("1-0", {"envelope": task.model_dump_json()})]

    poll_task = asyncio.create_task(agent.poll_once())
    await asyncio.wait_for(agent.started_event.wait(), timeout=1.0)

    heartbeat_key = "registry:cosmic/test-agent:1.0.0:inst_001"
    assert client.hashes[heartbeat_key]["current_load"] == "1"

    agent.finish_event.set()
    handled = await asyncio.wait_for(poll_task, timeout=1.0)

    assert handled is True
    assert client.hashes[heartbeat_key]["current_load"] == "0"

    await agent.stop()


@pytest.mark.asyncio
async def test_agent_runtime_polls_stream_and_emits_terminal_event(tmp_path: Path) -> None:
    card_path = _write_agent_card(tmp_path)
    client = FakeRedis()
    agent = DummyAgent(
        agent_card_path=card_path,
        redis_client=client,
        registry_db_path=tmp_path / "registry.db",
        instance_id="inst_001",
        agent_secret="agent-secret",
    )
    await agent.register()

    task = _signed_task(agent_id=agent.agent_id, secret="agent-secret")
    stream = task_stream_name(agent.agent_id, task.priority)
    client.streams[stream] = [("1-0", {"envelope": task.model_dump_json()})]

    handled = await agent.poll_once()

    assert handled is True
    assert client.acks == [(stream, "workers", "1-0")]
    event_entries = client.streams["streams:events"]
    assert len(event_entries) == 2
    accepted = parse_event_envelope(event_entries[0][1])
    completed = parse_event_envelope(event_entries[1][1])
    assert accepted.event_type == "task.accepted"
    assert completed.event_type == "task.completed"
    assert completed.payload["output"]["reply"] == "ok"
    assert client.lists["task_events:tsk_agent_runtime"] == ["1-0", "2-0"]

    await agent.stop()


@pytest.mark.asyncio
async def test_agent_runtime_rejects_unauthorized_sender(tmp_path: Path) -> None:
    card_path = _write_agent_card(tmp_path)
    client = FakeRedis()
    agent = DummyAgent(
        agent_card_path=card_path,
        redis_client=client,
        registry_db_path=tmp_path / "registry.db",
        instance_id="inst_001",
        agent_secret="agent-secret",
    )
    await agent.register()

    task = _signed_task(agent_id=agent.agent_id, secret="agent-secret", sender="cosmic/other-agent:1.0.0")
    stream = task_stream_name(agent.agent_id, task.priority)
    client.streams[stream] = [("1-0", {"envelope": task.model_dump_json()})]

    handled = await agent.poll_once()

    assert handled is True
    event_entries = client.streams["streams:events"]
    rejected = parse_event_envelope(event_entries[0][1])
    assert rejected.event_type == "task.rejected"
    assert rejected.payload["reason"] == "unauthorized_sender"

    await agent.stop()


@pytest.mark.asyncio
async def test_agent_runtime_enforces_step_plan_completion(tmp_path: Path) -> None:
    card_path = _write_agent_card(tmp_path)
    client = FakeRedis()
    agent = DummyAgent(
        agent_card_path=card_path,
        redis_client=client,
        registry_db_path=tmp_path / "registry.db",
        instance_id="inst_001",
        agent_secret="agent-secret",
    )
    await agent.register()

    task = _signed_task(agent_id=agent.agent_id, secret="agent-secret").model_copy(
        update={"input": {"query": "hello", "make_plan": True}}
    )
    task = task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})
    stream = task_stream_name(agent.agent_id, task.priority)
    client.streams[stream] = [("1-0", {"envelope": task.model_dump_json()})]

    handled = await agent.poll_once()

    assert handled is True
    event_entries = client.streams["streams:events"]
    failed = parse_event_envelope(event_entries[-1][1])
    assert failed.event_type == "task.failed"
    assert failed.payload["error"]["code"] == "PLAN_INCOMPLETE"

    await agent.stop()


@pytest.mark.asyncio
async def test_execute_with_idempotency_replays_stored_result() -> None:
    client = FakeRedis()
    task = _signed_task(agent_id="cosmic/test-agent:1.0.0", secret="agent-secret")
    stored = AgentResult(status="completed", output={"reply": "cached"}, artifacts=[], error=None)
    client.kv[f"idempotency:result:{task.idempotency_key}"] = stored.model_dump_json()

    calls = 0

    async def handler(_: TaskEnvelope) -> AgentResult:
        nonlocal calls
        calls += 1
        return AgentResult(status="completed", output={"reply": "fresh"}, artifacts=[], error=None)

    result = await execute_with_idempotency(task, handler, client, agent_max_duration_sec=30)

    assert isinstance(result, AgentResult)
    assert result.output["reply"] == "cached"
    assert calls == 0


@pytest.mark.asyncio
async def test_memory_tools_use_gateway_internal_endpoints() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Internal-Token"] == "internal-token"
        if request.url.path == "/internal/memory/search":
            payload = json.loads(request.content)
            assert payload["query"] == "yc"
            assert payload["agent_id"] == "cosmic/test-agent:1.0.0"
            return httpx.Response(200, json={"items": [{"memory_id": "mem_001"}]})
        if request.url.path == "/internal/memory/core-facts":
            payload = json.loads(request.content)
            assert payload["writer_id"] == "cosmic/test-agent:1.0.0"
            return httpx.Response(201, json={"memory_id": "mem_cf_001", "deduplicated": False})
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        memory_read = MemoryRead(
            gateway_url="http://gateway.test",
            service_token="internal-token",
            agent_id="cosmic/test-agent:1.0.0",
            client=client,
        )
        memory_write = MemoryWrite(
            gateway_url="http://gateway.test",
            service_token="internal-token",
            agent_id="cosmic/test-agent:1.0.0",
            client=client,
        )

        search_result = await memory_read.search("yc", max_results=3)
        write_result = await memory_write.write_core_fact(
            fact="User cares about YC timelines.",
            title="YC preference",
            canonical_key="user.preference.yc",
        )

    assert search_result["items"][0]["memory_id"] == "mem_001"
    assert write_result["memory_id"] == "mem_cf_001"
