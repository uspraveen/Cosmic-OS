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


@pytest.mark.asyncio
async def test_model_router_client_preserves_usage_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/classify"
        return httpx.Response(
            200,
            json={
                "classification": {
                    "route": "haiku",
                    "needs_latest": False,
                    "needs_citations": False,
                    "is_task": False,
                    "is_continuation": False,
                    "confidence": 0.77,
                    "signals": [],
                },
                "metrics": {"rtt_ms": 15.0},
                "classifier_model": "openai/gpt-oss-20b",
                "provider_request_id": "groq_req_1",
                "llm_call_id": "call_router_1",
                "llm_call_placed_at": "2026-03-17T10:00:00Z",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "total_tokens": 16,
                },
            },
        )

    client = ModelRouterClient("http://router.internal")
    client._client = httpx.AsyncClient(  # noqa: SLF001 - explicit test seam
        base_url="http://router.internal",
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = await client.classify_with_metadata(
            query="route this",
            conversation_context=[],
        )
    finally:
        await client.stop()

    assert payload["provider_request_id"] == "groq_req_1"
    assert payload["llm_call_id"] == "call_router_1"
    assert payload["llm_call_placed_at"] == "2026-03-17T10:00:00Z"
    assert payload["usage"]["total_tokens"] == 16
