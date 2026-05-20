from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.map_agent.config import MapAgentConfig
from agents.map_agent.internal_llm import parse_map_request


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"draw_route": true, "route_waypoints": ["Little Rock", "Chicago"]}'
                    }
                }
            ]
        }


class _FakeHttpClient:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.payload = kwargs["json"]
        return _FakeResponse()


@pytest.mark.asyncio
async def test_parse_map_request_omits_temperature_for_gpt5_compatibility() -> None:
    cfg = MapAgentConfig(
        internal_llm_api_key="test-key",
        internal_llm_base_url="https://api.openai.com/v1",
        internal_llm_model="gpt-5-mini",
    )
    client = _FakeHttpClient()

    parsed = await parse_map_request(
        cfg=cfg,
        http_client=client,  # type: ignore[arg-type]
        query="Route from Little Rock to Chicago",
    )

    assert parsed == {"draw_route": True, "route_waypoints": ["Little Rock", "Chicago"]}
    assert client.payload is not None
    assert "temperature" not in client.payload
