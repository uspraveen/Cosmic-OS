from __future__ import annotations

import httpx
import pytest

from gateway.memory.client import CosmicMemoryClient


@pytest.mark.asyncio
async def test_memory_client_uses_longer_timeout_for_write_endpoints() -> None:
    captured: dict[str, dict[str, float]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout")
        if isinstance(timeout, dict):
            captured[request.url.path] = {
                key: float(value)
                for key, value in timeout.items()
                if value is not None
            }
        return httpx.Response(200, json={"ok": True})

    client = CosmicMemoryClient(
        base_url="http://memory.internal",
        timeout_sec=12.0,
        write_timeout_sec=45.0,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001 - explicit test seam
        base_url="http://memory.internal",
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.write_memory({"kind": "user_data"})
        await client.write_core_fact({"kind": "core_fact"})
        await client.ingest_episode({"session_id": "sess_123"})
        await client.get_memory("mem_123")
    finally:
        await client.stop()

    assert captured["/v1/memories"]["read"] == pytest.approx(45.0)
    assert captured["/v1/memories"]["connect"] == pytest.approx(5.0)
    assert captured["/v1/core-facts"]["read"] == pytest.approx(45.0)
    assert captured["/v1/episodes"]["read"] == pytest.approx(45.0)
    assert captured["/v1/memories/mem_123"]["read"] == pytest.approx(12.0)
