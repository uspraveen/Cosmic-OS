from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest

from gateway.wishlist import CapabilityWishlistService, CapabilityWishlistStore


def _normalized_vector_for_text(text: str, *, dimensions: int = 128) -> list[float]:
    seed = sum(ord(character) for character in text) or 1
    vector = [float(((seed + (index * 7)) % 13) - 6) for index in range(dimensions)]
    magnitude = math.sqrt(sum(component * component for component in vector))
    return [component / magnitude for component in vector]


class StubMemoryClient:
    def __init__(self, *, enabled: bool = True, dimensions: int = 128) -> None:
        self.enabled = enabled
        self.dimensions = dimensions
        self.calls: list[dict[str, object]] = []

    async def generate_embeddings(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        texts = list(payload.get("texts") or [])
        dimensions = int(payload.get("dimensions") or self.dimensions)
        return {
            "model": "pplx-embed-v1-4b",
            "dimensions": dimensions,
            "items": [
                {
                    "index": index,
                    "vector": _normalized_vector_for_text(str(text), dimensions=dimensions),
                    "dimensions": dimensions,
                }
                for index, text in enumerate(texts)
            ],
            "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
        }


@pytest.mark.asyncio
async def test_capability_wishlist_service_creates_item_and_exports_snapshots(tmp_path: Path) -> None:
    store = CapabilityWishlistStore(tmp_path / "wishlist.db")
    service = CapabilityWishlistService(
        store=store,
        export_dir=tmp_path / "exports",
    )
    await service.initialize()
    try:
        result = await service.capture(
            title="Desktop task control center",
            summary="Give COSMIC desktop users one place to observe and manage active tasks, crons, and agent traffic.",
            desired_outcome="Users can inspect and manage tasks and schedules from a dedicated Spaces surface.",
            domain="desktop_ui",
            tags=["desktop", "tasks", "spaces"],
            evidence="The user explicitly asked for a Spaces control center.",
        )
        assert result["status"] == "created_new"
        assert result["capability_id"] == "cap_000001"

        search = await service.search(query="spaces task control center", limit=3)
        assert search["count"] == 1
        assert search["matches"][0]["capability_id"] == "cap_000001"

        yaml_text = (tmp_path / "exports" / "current.yaml").read_text(encoding="utf-8")
        md_text = (tmp_path / "exports" / "current.md").read_text(encoding="utf-8")
        assert "cap_000001" in yaml_text
        assert "Desktop task control center" in md_text
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capability_wishlist_service_skips_deterministic_duplicate(tmp_path: Path) -> None:
    store = CapabilityWishlistStore(tmp_path / "wishlist.db")
    service = CapabilityWishlistService(
        store=store,
        export_dir=tmp_path / "exports",
    )
    await service.initialize()
    try:
        first = await service.capture(
            title="Channel delivery target resolver",
            summary="Resolve a user-linked destination channel without needing raw channel ids.",
            domain="communications",
            tags=["channels", "reminders"],
            evidence="The user wanted reminders to reach any linked channel.",
        )
        second = await service.capture(
            title="Channel delivery target resolver",
            summary="Resolve a user-linked destination channel without needing raw channel ids.",
            domain="communications",
            tags=["channels", "reminders"],
            evidence="The user wanted reminders to reach any linked channel.",
        )
        assert first["status"] == "created_new"
        assert second["status"] == "skipped_duplicate"
        assert second["capability_id"] == first["capability_id"]
        assert service.summary()["total_items"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capability_wishlist_service_uses_xai_to_update_similar_entry(tmp_path: Path) -> None:
    usage_events: list[dict[str, object]] = []
    memory_client = StubMemoryClient(dimensions=128)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.x.ai/v1/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["messages"][0]["role"] == "system"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "decision": "update_existing",
                                        "target_capability_id": "cap_000001",
                                        "confidence": 0.92,
                                        "reason": "This is the same underlying capability gap with a better canonical description.",
                                        "merged_fields": {
                                            "title": "Cross-channel delivery target resolver",
                                            "summary": "Resolve a user-linked destination channel so reminders and future actions can target desktop, WhatsApp, or Telegram without raw channel ids.",
                                            "desired_outcome": "COSMIC can deliver reminders and future actions to a linked user channel by alias.",
                                            "domain": "communications",
                                            "tags": ["channels", "reminders", "delivery"],
                                        },
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 140, "completion_tokens": 48, "total_tokens": 188},
                },
                headers={"x-request-id": "xai-1"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = CapabilityWishlistStore(tmp_path / "wishlist.db")
    service = CapabilityWishlistService(
        store=store,
        export_dir=tmp_path / "exports",
        memory_client=memory_client,
        embedding_dimensions=128,
        xai_api_key="xai-key",
        usage_recorder=lambda event: usage_events.append(
            event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        ),
        client=client,
    )
    await service.initialize()
    try:
        created = await service.capture(
            title="Channel delivery target resolver",
            summary="Resolve a user-linked destination channel without needing raw channel ids.",
            domain="communications",
            tags=["channels", "reminders"],
            evidence="The user wanted reminders to reach any linked channel.",
        )
        service._fuse_candidates = lambda **_kwargs: [  # type: ignore[method-assign]
            service._serialize_item(store.get_item("cap_000001"))
        ]
        updated = await service.capture(
            title="Channel delivery target resolver for linked channels",
            summary="Resolve a user-linked destination channel so reminders and future actions can target desktop, WhatsApp, or Telegram without raw channel ids.",
            desired_outcome="COSMIC can deliver reminders and future actions to a linked user channel by alias.",
            domain="communications",
            tags=["channels", "reminders", "delivery"],
            evidence="The user explicitly asked for desktop-created reminders to reach WhatsApp too.",
        )

        assert created["status"] == "created_new"
        assert updated["status"] == "updated_existing"
        assert updated["capability_id"] == "cap_000001"
        assert updated["item"]["title"] == "Cross-channel delivery target resolver"
        assert updated["item"]["evidence_count"] == 2

        yaml_text = (tmp_path / "exports" / "current.yaml").read_text(encoding="utf-8")
        assert "Cross-channel delivery target resolver" in yaml_text
        assert "delivery" in yaml_text

        operations = [str(event["operation"]) for event in usage_events]
        assert "gateway.capability_wishlist.adjudicate" in operations
        assert len(memory_client.calls) == 3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capability_wishlist_search_backfills_missing_embeddings_via_memory_service(tmp_path: Path) -> None:
    store = CapabilityWishlistStore(tmp_path / "wishlist.db")
    bootstrap_service = CapabilityWishlistService(
        store=store,
        export_dir=tmp_path / "exports",
    )
    await bootstrap_service.initialize()
    try:
        created = await bootstrap_service.capture(
            title="Headless social feed monitor",
            summary="Watch social feeds and web sources headlessly so COSMIC can notify the user when a tracked list changes.",
            desired_outcome="COSMIC can monitor a feed or listing source over time and report deltas automatically.",
            domain="automation",
            tags=["monitoring", "feeds", "automation"],
            evidence="The user wanted COSMIC to track new YC company list additions.",
        )
        assert created["status"] == "created_new"
        assert store.get_item("cap_000001")["embedding_vector"] is None
    finally:
        await bootstrap_service.close()

    memory_client = StubMemoryClient(dimensions=128)
    service = CapabilityWishlistService(
        store=store,
        export_dir=tmp_path / "exports",
        memory_client=memory_client,
        embedding_dimensions=128,
    )
    await service.initialize()
    try:
        search = await service.search(query="headless social feed monitor", limit=3)
        assert search["embedding_used"] is True
        assert search["count"] == 1
        assert search["matches"][0]["capability_id"] == "cap_000001"

        stored = store.get_item("cap_000001")
        assert isinstance(stored["embedding_vector"], list)
        operations = [str(call.get("usage_operation")) for call in memory_client.calls]
        assert "gateway.capability_wishlist.embed_item" in operations
        assert "gateway.capability_wishlist.embed_query" in operations
    finally:
        await service.close()
