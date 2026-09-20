from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.prophet.archive import (
    ARCHIVE_BRIEFING_HEADER,
    ProphetArchiveService,
    estimate_tokens,
    pack_archive_lines,
)
from gateway.prophet.context import render_prophet_context_block
from gateway.prophet.store import ProphetStore


def _store(tmp_path: Path) -> ProphetStore:
    store = ProphetStore(tmp_path / "prophet.db")
    store.initialize()
    return store


def _edition(*, slot: str = "morning", headline: str = "Lead story about agents") -> dict:
    return {
        "edition_date": "2026-09-11",
        "slot": slot,
        "lead": {
            "headline": headline,
            "dek": "Why this matters",
            "importance": 90,
            "source": {"name": "Reuters", "url": "https://example.com/lead"},
        },
        "sections": [
            {
                "id": "tech",
                "label": "Technology",
                "stories": [
                    {
                        "headline": "Follow-on agent tooling",
                        "dek": "A related note",
                        "importance": 70,
                        "source": {"name": "The Verge", "url": "https://example.com/follow"},
                    }
                ],
            }
        ],
    }


class _FakeMemoryClient:
    enabled = True

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [1.0, 0.0]
        self.calls = 0

    async def generate_embeddings(self, payload: dict) -> dict:
        self.calls += 1
        texts = payload.get("texts") if isinstance(payload, dict) else []
        count = len(texts) if isinstance(texts, list) else 1
        return {"items": [{"vector": list(self.vector)} for _ in range(count)]}


@pytest.mark.asyncio
async def test_archive_briefing_is_labelled_and_token_capped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    older = store.publish_edition(_edition(headline="Old agent research recap"))
    newer = _edition(slot="evening", headline="Evening wrap on something else")
    newer["lead"]["source"] = {"name": "FT", "url": "https://example.com/other"}
    newer["sections"][0]["stories"][0]["headline"] = "Unrelated bond move"
    newer["sections"][0]["stories"][0]["source"] = {"name": "FT", "url": "https://example.com/bonds"}
    store.publish_edition(newer)

    for row in store.list_story_index_for_edition(older["edition_id"]):
        store.update_story_embedding(
            row["story_row_id"],
            embedding_model="test",
            embedding_dimensions=2,
            embedding_vector=[1.0, 0.0],
        )

    import sqlite3

    # Backdate the older edition outside the 3-day recent window but inside
    # the 28-day semantic window, relative to *now* — a fixed date here rots
    # as the calendar advances toward it.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE prophet_story_index SET shown_at = ? WHERE edition_id = ?",
            (cutoff, older["edition_id"]),
        )
        connection.commit()

    service = ProphetArchiveService(store=store, memory_client=_FakeMemoryClient([1.0, 0.0]))
    related = await service.related_archive_for_briefing(interests=["agents"])
    rendered = pack_archive_lines(related, token_budget=1000)
    assert "Previously shown Daily Prophet archive (semantic)" in rendered
    assert "not the user's long-term memory" in rendered
    assert "Old agent research recap" in rendered
    assert estimate_tokens(rendered) <= 1000

    briefing = render_prophet_context_block(
        store,
        slot="morning",
        max_stories=15,
        related_archive_rendered=rendered,
    )
    assert "## Daily Prophet Briefing" in briefing
    assert ARCHIVE_BRIEFING_HEADER.splitlines()[0] in briefing


def test_pack_archive_lines_honors_token_budget() -> None:
    items = [
        {
            "edition_date": "2026-09-01",
            "slot": "morning",
            "section_id": "tech",
            "headline": f"Story number {index} about a long running topic",
            "dek": "Extra detail " * 8,
            "semantic_similarity": 0.9,
        }
        for index in range(40)
    ]
    budget = estimate_tokens(ARCHIVE_BRIEFING_HEADER) + 80
    rendered = pack_archive_lines(items, token_budget=budget)
    assert ARCHIVE_BRIEFING_HEADER.splitlines()[0] in rendered
    assert estimate_tokens(rendered) <= budget
    assert rendered.count("\n- ") < 40
    assert rendered.count("\n- ") >= 1


@pytest.mark.asyncio
async def test_after_publish_adds_semantic_duplicate_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish_edition(_edition())
    for row in store.list_story_index_for_edition(first["edition_id"]):
        store.update_story_embedding(
            row["story_row_id"],
            embedding_model="test",
            embedding_dimensions=2,
            embedding_vector=[1.0, 0.0],
        )
    second_payload = _edition(slot="evening", headline="Agents take another leap")
    second_payload["lead"]["source"] = {"name": "Reuters", "url": "https://example.com/lead-2"}
    second_payload["sections"][0]["stories"][0]["source"] = {
        "name": "The Verge",
        "url": "https://example.com/follow-2",
    }
    second = store.publish_edition(second_payload)
    service = ProphetArchiveService(store=store, memory_client=_FakeMemoryClient([1.0, 0.0]))
    updated = await service.after_publish(second, request_id="req_prophet")
    assert any("previously shown coverage" in warning for warning in updated["warnings"])


@pytest.mark.asyncio
async def test_search_archive_returns_labelled_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    published = store.publish_edition(_edition())
    for row in store.list_story_index_for_edition(published["edition_id"]):
        store.update_story_embedding(
            row["story_row_id"],
            embedding_model="test",
            embedding_dimensions=2,
            embedding_vector=[1.0, 0.0],
        )
    service = ProphetArchiveService(store=store, memory_client=_FakeMemoryClient([1.0, 0.0]))
    result = await service.search("agent tooling")
    assert result["archive"] is True
    assert "not live web search" in result["purpose"].casefold()
    assert result["matches"]
    assert "Previously shown Daily Prophet archive" in result["rendered"]
