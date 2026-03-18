from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from gateway.wishlist import CapabilityWishlistService, CapabilityWishlistStore


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
    embedding_calls = {"count": 0}
    embedding_blob = base64.b64encode(bytes(range(128))).decode("ascii")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.perplexity.ai/embeddings"):
            embedding_calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": embedding_blob}],
                    "usage": {"prompt_tokens": 32, "total_tokens": 32},
                },
                headers={"x-request-id": f"pplx-{embedding_calls['count']}"},
            )
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
        perplexity_api_key="perplexity-key",
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
        updated = await service.capture(
            title="Cross-channel delivery target resolver",
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
        assert operations.count("gateway.capability_wishlist.embed_item") == 3
        assert "gateway.capability_wishlist.adjudicate" in operations
    finally:
        await service.close()
