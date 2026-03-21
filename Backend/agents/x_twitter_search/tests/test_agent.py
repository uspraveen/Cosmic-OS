from __future__ import annotations

from pathlib import Path

import pytest

from agents.x_twitter_search.agent import XTwitterSearchAgent
from agents.x_twitter_search.config import XTwitterSearchConfig
from shared import TaskEnvelope, sign_task_envelope, utcnow


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}
        self._counter = 0
        self._sequence = 0

    async def incr(self, key: str) -> int:
        self._counter += 1
        return self._counter

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        del maxlen, approximate
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


class FakeResponse:
    def __init__(self) -> None:
        self.id = "resp_x_123"
        self.content = (
            '{"summary":"Cursor sentiment on X is sharply positive after the launch.",'
            '"key_findings":["Most discussion focused on speed and agentic coding quality."],'
            '"notable_posts":[{"author_handle":"cursor_ai","post_url":"https://x.com/cursor_ai/status/123","posted_at":"2026-03-21T10:00:00Z","excerpt":"Composer 2 is live.","why_it_matters":"Primary announcement from the vendor."}]}'
        )
        self.citations = [
            {"title": "Cursor launch post", "url": "https://x.com/cursor_ai/status/123", "description": "Launch thread"}
        ]
        self.usage = {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}
        self.tool_calls = [{"tool": "x_search"}]
        self.server_side_tool_usage = {"x_search_queries": 3}


class FakeChat:
    def __init__(self) -> None:
        self.items: list[str] = []

    def append(self, value: str) -> None:
        self.items.append(value)

    def sample(self) -> FakeResponse:
        return FakeResponse()


class FakeClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.last_kwargs = None

    @property
    def chat(self) -> "FakeClient":
        return self

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return FakeChat()


def _make_task(*, intent: str, payload: dict[str, object], session_id: str = "sess_x") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id=f"tsk_{intent.replace('.', '_')}",
        task_list_id=session_id,
        parent_task_id="tsk_parent",
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/x-twitter-search-agent:1.0.0",
        intent=intent,
        input=payload,
        input_artifacts=[],
        idempotency_key=f"idem_{intent.replace('.', '_')}",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


@pytest.mark.asyncio
async def test_x_search_agent_search_persists_artifacts_and_returns_structured_briefing(tmp_path: Path) -> None:
    fake_client = FakeClient("xai-key")
    agent = XTwitterSearchAgent(
        redis_client=FakeRedis(),
        config=XTwitterSearchConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            xai_api_key="xai-key",
            x_search_model="grok-4.20-beta-0309-reasoning",
        ),
        xai_client_factory=lambda api_key: fake_client,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        result = await agent.execute(
            _make_task(
                intent="x.search",
                payload={
                    "query": "What is X saying about Cursor Composer 2 today?",
                    "allowed_x_handles": ["cursor_ai"],
                    "max_posts": 4,
                },
            )
        )
    finally:
        await agent.stop()

    assert result.status == "completed"
    assert result.output["query"] == "What is X saying about Cursor Composer 2 today?"
    assert "positive" in result.output["summary"].lower()
    assert result.output["notable_posts"][0]["author_handle"] == "cursor_ai"
    assert result.output["citations"][0]["url"] == "https://x.com/cursor_ai/status/123"
    assert fake_client.last_kwargs["model"] == "grok-4.20-beta-0309-reasoning"
    assert len(result.artifacts) == 3


@pytest.mark.asyncio
async def test_x_search_agent_recall_session_reads_private_ledger(tmp_path: Path) -> None:
    agent = XTwitterSearchAgent(
        redis_client=FakeRedis(),
        config=XTwitterSearchConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            xai_api_key="xai-key",
        ),
        xai_client_factory=lambda api_key: FakeClient(api_key),
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        await agent.execute(_make_task(intent="x.search", payload={"query": "Search X for Grok feedback"}))
        recall_result = await agent.execute(
            _make_task(intent="x.recall_session", payload={"session_id": "sess_x", "limit": 5})
        )
    finally:
        await agent.stop()

    assert recall_result.status == "completed"
    assert recall_result.output["session_id"] == "sess_x"
    assert len(recall_result.output["entries"]) == 1
    assert recall_result.output["entries"][0]["intent"] == "x.search"


@pytest.mark.asyncio
async def test_x_search_agent_rejects_invalid_date_range_and_missing_session_id(tmp_path: Path) -> None:
    agent = XTwitterSearchAgent(
        redis_client=FakeRedis(),
        config=XTwitterSearchConfig(
            redis_url="redis://unused",
            gateway_url="http://gateway",
            gateway_internal_token="internal-token",
            xai_api_key="xai-key",
        ),
        xai_client_factory=lambda api_key: FakeClient(api_key),
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        artifacts_root=tmp_path / "runs" / "artifacts",
        agent_secret="agent-secret",
    )
    await agent.on_startup()
    try:
        search_result = await agent.execute(
            _make_task(
                intent="x.search",
                payload={
                    "query": "Search X for launch reactions",
                    "from_date": "2026-03-22",
                    "to_date": "2026-03-21",
                },
            )
        )
        recall_result = await agent.execute(_make_task(intent="x.recall_session", payload={}))
    finally:
        await agent.stop()

    assert search_result.status == "failed"
    assert search_result.error is not None
    assert search_result.error.code == "INVALID_INPUT"
    assert recall_result.status == "failed"
    assert recall_result.error is not None
    assert recall_result.error.code == "INVALID_INPUT"
