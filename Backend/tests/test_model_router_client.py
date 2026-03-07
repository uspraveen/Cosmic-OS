from __future__ import annotations

import httpx
import pytest

from gateway.router_client import ModelRouterClient


@pytest.mark.asyncio
async def test_model_router_client_unwraps_classification_payload() -> None:
    expected = {
        "route": "perplexity",
        "needs_latest": True,
        "needs_citations": True,
        "is_task": False,
        "is_continuation": False,
        "confidence": 0.92,
        "signals": ["current_events"],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/classify"
        return httpx.Response(
            200,
            json={
                "classification": expected,
                "metrics": {"rtt_ms": 18.5},
                "classifier_model": "openai/gpt-oss-20b",
            },
        )

    client = ModelRouterClient("http://router.internal")
    client._client = httpx.AsyncClient(  # noqa: SLF001 - explicit test seam
        base_url="http://router.internal",
        transport=httpx.MockTransport(handler),
    )

    try:
        result = await client.classify(
            query="what is the latest release?",
            conversation_context=[],
        )
    finally:
        await client.stop()

    assert result == expected
