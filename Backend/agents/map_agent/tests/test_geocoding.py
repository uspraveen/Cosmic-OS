from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.map_agent.config import MapAgentConfig
from agents.map_agent.geocoding import geocode_query


class _FakeResponse:
    def __init__(self, payload: list[dict[str, Any]]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload: list[dict[str, Any]]) -> None:
        self.payload = payload
        self.params: dict[str, Any] | None = None

    async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.params = kwargs["params"]
        return _FakeResponse(self.payload)


@pytest.mark.asyncio
async def test_geocode_query_biases_ambiguous_places_toward_route_context() -> None:
    client = _FakeHttpClient(
        [
            {
                "display_name": "Farmington, San Juan County, New Mexico, United States",
                "lat": "36.7281",
                "lon": "-108.2187",
            },
            {
                "display_name": "Farmington, St. Francois County, Missouri, United States",
                "lat": "37.7809",
                "lon": "-90.4218",
            },
        ]
    )

    result = await geocode_query(
        cfg=MapAgentConfig(),
        http_client=client,  # type: ignore[arg-type]
        query="Farmington",
        bias_coordinates=[(-92.2896, 34.7465), (-87.6298, 41.8781)],
    )

    assert client.params is not None
    assert client.params["limit"] == 8
    assert result["label"].startswith("Farmington, St. Francois County")
