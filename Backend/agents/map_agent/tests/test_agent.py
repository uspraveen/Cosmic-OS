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
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MapAgent:
    cfg = MapAgentConfig(
        redis_url="redis://127.0.0.1:6379/0",
        artifacts_root=tmp_path / "artifacts",
        enable_internal_llm=False,
    )
    redis_client = MagicMock()
    instance = MapAgent(redis_client=redis_client, config=cfg)
    monkeypatch.setattr("agents.map_agent.agent.AGENT_ROOT", tmp_path / "agent_root")
    return instance


@pytest.mark.asyncio
async def test_map_render_with_structured_markers(agent: MapAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    await agent.on_startup()

    async def fake_geocode_query(**kwargs):
        query = kwargs["query"]
        if "Paris" in query:
            return {"query": query, "label": "Paris", "lat": 48.8566, "lng": 2.3522, "position": [2.3522, 48.8566]}
        return {"query": query, "label": "Lyon", "lat": 45.764, "lng": 4.8357, "position": [4.8357, 45.764]}

    async def fake_fetch_routes(**kwargs):
        return [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[2.3522, 48.8566], [4.8357, 45.764]],
                },
                "distance_m": 465000,
                "duration_s": 16200,
            }
        ]

    monkeypatch.setattr("agents.map_agent.agent.geocode_query", fake_geocode_query)
    monkeypatch.setattr("agents.map_agent.agent.fetch_routes", fake_fetch_routes)

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
async def test_map_render_with_route_alternatives(agent: MapAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    await agent.on_startup()

    async def fake_geocode_query(**kwargs):
        query = kwargs["query"]
        if "Little Rock" in query:
            return {
                "query": query,
                "label": "Little Rock",
                "lat": 34.7465,
                "lng": -92.2896,
                "position": [-92.2896, 34.7465],
            }
        return {
            "query": query,
            "label": "Chicago",
            "lat": 41.8781,
            "lng": -87.6298,
            "position": [-87.6298, 41.8781],
        }

    async def fake_fetch_routes(**kwargs):
        assert kwargs["alternatives"] == 3
        return [
            {
                "geometry": {"type": "LineString", "coordinates": [[-92.2896, 34.7465], [-87.6298, 41.8781]]},
                "distance_m": 1040000,
                "duration_s": 38700,
            },
            {
                "geometry": {"type": "LineString", "coordinates": [[-92.2896, 34.7465], [-89.4, 38.6], [-87.6298, 41.8781]]},
                "distance_m": 1090000,
                "duration_s": 40500,
            },
            {
                "geometry": {"type": "LineString", "coordinates": [[-92.2896, 34.7465], [-90.1, 36.0], [-87.6298, 41.8781]]},
                "distance_m": 1120000,
                "duration_s": 42300,
            },
        ]

    monkeypatch.setattr("agents.map_agent.agent.geocode_query", fake_geocode_query)
    monkeypatch.setattr("agents.map_agent.agent.fetch_routes", fake_fetch_routes)

    task = TaskEnvelope(
        task_id="task_map_alternatives",
        task_list_id="tl_map_alternatives",
        session_id="sess_map_alternatives",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/map-agent:1.0.0",
        intent="map.render",
        input={
            "query": "Little Rock to Chicago driving route with alternatives",
        },
        idempotency_key="idem_map_alternatives",
        signature="test_sig",
        source="agent",
        source_id="orch_1",
        channel="desktop:test",
    )

    result = await agent.handle_map_render(task)
    assert result.status == "completed"
    assert "Routes: 3 options." in result.output["summary"]

    map_path = agent.artifacts_root / task.task_id / "map_agent" / "map.cosmic-map.json"
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert [route["label"] for route in payload["routes"]] == ["Route", "Alternative 2", "Alternative 3"]
    assert len(payload["routes"]) == 3


@pytest.mark.asyncio
async def test_map_render_with_named_route_options(agent: MapAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    await agent.on_startup()

    coordinates = {
        "Little Rock, Arkansas": (-92.2896, 34.7465),
        "Memphis, Tennessee": (-90.0490, 35.1495),
        "St. Louis, Missouri": (-90.1994, 38.6270),
        "Chicago, Illinois": (-87.6298, 41.8781),
        "Jonesboro, Arkansas": (-90.7043, 35.8423),
        "Farmington, Missouri": (-90.4218, 37.7809),
    }

    async def fake_geocode_query(**kwargs):
        query = kwargs["query"]
        lng, lat = coordinates[query]
        return {"query": query, "label": query, "lat": lat, "lng": lng, "position": [lng, lat]}

    async def fake_fetch_routes(**kwargs):
        coords = kwargs["coordinates"]
        return [
            {
                "geometry": {"type": "LineString", "coordinates": [[lng, lat] for lng, lat in coords]},
                "distance_m": 1000000 + len(coords) * 1000,
                "duration_s": 36000 + len(coords) * 60,
            }
        ]

    monkeypatch.setattr("agents.map_agent.agent.geocode_query", fake_geocode_query)
    monkeypatch.setattr("agents.map_agent.agent.fetch_routes", fake_fetch_routes)

    task = TaskEnvelope(
        task_id="task_map_route_options",
        task_list_id="tl_map_route_options",
        session_id="sess_map_route_options",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/map-agent:1.0.0",
        intent="map.render",
        input={
            "title": "Little Rock to Chicago options",
            "route_options": [
                {
                    "label": "Fastest via Memphis and St. Louis",
                    "route_waypoints": [
                        "Little Rock, Arkansas",
                        "Memphis, Tennessee",
                        "St. Louis, Missouri",
                        "Chicago, Illinois",
                    ],
                },
                {
                    "label": "US-67 via Jonesboro",
                    "route_waypoints": [
                        "Little Rock, Arkansas",
                        "Jonesboro, Arkansas",
                        "Farmington, Missouri",
                        "Chicago, Illinois",
                    ],
                },
            ],
        },
        idempotency_key="idem_map_route_options",
        signature="test_sig",
        source="agent",
        source_id="orch_1",
        channel="desktop:test",
    )

    result = await agent.handle_map_render(task)
    assert result.status == "completed"

    map_path = agent.artifacts_root / task.task_id / "map_agent" / "map.cosmic-map.json"
    payload = json.loads(map_path.read_text(encoding="utf-8"))
    assert [route["label"] for route in payload["routes"]] == [
        "Fastest via Memphis and St. Louis",
        "US-67 via Jonesboro",
    ]
    assert len(payload["markers"]) == 6


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
