from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.map_agent.agent import MapAgent
from agents.map_agent.config import MapAgentConfig
from shared.contracts import TaskEnvelope


@pytest.fixture
def agent(tmp_path: Path) -> MapAgent:
    cfg = MapAgentConfig(
        redis_url="redis://127.0.0.1:6379/0",
        artifacts_root=tmp_path / "artifacts",
        enable_internal_llm=False,
    )
    redis_client = MagicMock()
    instance = MapAgent(redis_client=redis_client, config=cfg)
    return instance


@pytest.mark.asyncio
async def test_map_render_with_structured_markers(agent: MapAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    await agent.on_startup()

    async def fake_geocode_query(**kwargs):
        query = kwargs["query"]
        if "Paris" in query:
            return {"query": query, "label": "Paris", "lat": 48.8566, "lng": 2.3522, "position": [2.3522, 48.8566]}
        return {"query": query, "label": "Lyon", "lat": 45.764, "lng": 4.8357, "position": [4.8357, 45.764]}

    async def fake_fetch_route(**kwargs):
        return {
            "geometry": {
                "type": "LineString",
                "coordinates": [[2.3522, 48.8566], [4.8357, 45.764]],
            },
            "distance_m": 465000,
            "duration_s": 16200,
        }

    monkeypatch.setattr("agents.map_agent.agent.geocode_query", fake_geocode_query)
    monkeypatch.setattr("agents.map_agent.agent.fetch_route", fake_fetch_route)

    task = TaskEnvelope(
        task_id="task_map_1",
        task_list_id="tl_map_1",
        session_id="sess_map_1",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/map-agent:1.0.0",
        intent="map.render",
        input={
            "title": "Paris to Lyon",
            "draw_route": True,
            "route_waypoints": ["Paris, France", "Lyon, France"],
        },
        idempotency_key="idem_map_1",
        signature="test_sig",
        source="agent",
        source_id="orch_1",
        channel="desktop:test",
    )

    result = await agent.handle_map_render(task)
    assert result.status == "completed"
    assert result.artifacts
    artifact = result.artifacts[0]
    assert artifact.kind == "output"
    assert artifact.mime == "application/vnd.cosmic.map+json"

    map_path = agent.artifacts_root / task.task_id / "map_agent" / "map.cosmic-map.json"
    assert map_path.exists()
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert payload["title"] == "Paris to Lyon"
    assert len(payload["routes"]) == 1
    assert len(payload["markers"]) >= 2


@pytest.mark.asyncio
async def test_map_render_requires_input(agent: MapAgent) -> None:
    await agent.on_startup()
    task = TaskEnvelope(
        task_id="task_map_2",
        task_list_id="tl_map_2",
        session_id="sess_map_2",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/map-agent:1.0.0",
        intent="map.render",
        input={},
        idempotency_key="idem_map_2",
        signature="test_sig",
        source="agent",
        source_id="orch_1",
        channel="desktop:test",
    )
    result = await agent.handle_map_render(task)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "INVALID_INPUT"
