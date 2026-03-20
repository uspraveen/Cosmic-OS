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


def test_memory_client_render_prompt_context_includes_keys_and_sources() -> None:
    client = CosmicMemoryClient(base_url="", timeout_sec=12.0, write_timeout_sec=45.0)

    rendered = client.render_prompt_context(
        core_fact_items=[
            {
                "memory_id": "mem_core_1",
                "title": "Partner name",
                "content": "Praveen's partner is Priya.",
                "canonical_key": "relationships.partner.name",
            }
        ],
        recall_items=[
            {
                "memory_id": "mem_task_1",
                "kind": "task_summary",
                "title": "Newsletter investigation",
                "content": "The newsletter parse completed successfully.",
                "source_kind": "gateway",
                "canonical_key": "documents.newsletter.status",
            }
        ],
    )

    assert "[key=relationships.partner.name]" in rendered
    assert "source=gateway" in rendered
    assert "key=documents.newsletter.status" in rendered
