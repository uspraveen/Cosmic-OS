from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..memory.client import CosmicMemoryClient
from .store import (
    PROPHET_DEDUP_WINDOW_DAYS,
    PROPHET_SEMANTIC_WINDOW_DAYS,
    ProphetStore,
    _normalize_headline_key,
)

logger = logging.getLogger(__name__)

PROPHET_ARCHIVE_TOKEN_BUDGET = 1000
PROPHET_RELATED_MIN_SIMILARITY = 0.55
PROPHET_DUPLICATE_MIN_SIMILARITY = 0.78
PROPHET_DEFAULT_EMBEDDING_MODEL = "pplx-embed-v1-4b"
PROPHET_DEFAULT_EMBEDDING_DIMENSIONS = 1024

ARCHIVE_BRIEFING_HEADER = """## Previously shown Daily Prophet archive (semantic)
This block is Cosmic's own newspaper archive — not the user's long-term memory and not live research.
It lists stories already published to My Prophet in the last 4 weeks that are similar to recent editions and the user's interests.
Use it to: (1) skip the same topic unless there is a major update; (2) connect today's stories to that prior coverage when it helps the reader.
Each line is dated coverage Cosmic already showed."""


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def source_domain(url: str | None) -> str:
    host = urlparse(str(url or "").strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def embedding_text_for_story(story: dict[str, Any]) -> str:
    headline = str(story.get("headline") or "").strip()
    dek = str(story.get("dek") or "").strip()
    source_name = str(story.get("source_name") or "").strip()
    if not source_name:
        source = story.get("source") if isinstance(story.get("source"), dict) else {}
        source_name = str(source.get("name") or "").strip()
    url = str(story.get("source_url") or "").strip()
    if not url:
        source = story.get("source") if isinstance(story.get("source"), dict) else {}
        url = str(source.get("url") or "").strip()
    domain = source_domain(url)
    parts = [part for part in (headline, dek, source_name, domain) if part]
    return " | ".join(parts)


def format_archive_line(item: dict[str, Any]) -> str:
    section = item.get("section_id") or "general"
    dek = str(item.get("dek") or "").strip()
    dek_bit = f" — {dek}" if dek else ""
    similarity = item.get("semantic_similarity")
    score_bit = (
        f" (similarity {similarity:.2f})"
        if isinstance(similarity, (int, float))
        else ""
    )
    return (
        f"- {item.get('edition_date')} {item.get('slot')} [{section}] "
        f"{item.get('headline')}{dek_bit}{score_bit}"
    )


def pack_archive_lines(
    items: list[dict[str, Any]],
    *,
    header: str = ARCHIVE_BRIEFING_HEADER,
    token_budget: int = PROPHET_ARCHIVE_TOKEN_BUDGET,
) -> str:
    if not items:
        return ""
    budget = max(32, int(token_budget))
    lines = [header, ""]
    used = estimate_tokens("\n".join(lines))
    packed = 0
    for item in items:
        line = format_archive_line(item)
        cost = estimate_tokens(line) + 1
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
        packed += 1
    if packed <= 0:
        return ""
    return "\n".join(lines).rstrip()


class ProphetArchiveService:
    """Embed and retrieve previously shown Prophet stories from prophet.db."""

    def __init__(
        self,
        *,
        store: ProphetStore,
        memory_client: CosmicMemoryClient | None = None,
        embedding_model: str = PROPHET_DEFAULT_EMBEDDING_MODEL,
        embedding_dimensions: int = PROPHET_DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        self.store = store
        self.memory_client = memory_client
        self.embedding_model = str(embedding_model or "").strip() or PROPHET_DEFAULT_EMBEDDING_MODEL
        self.embedding_dimensions = max(128, int(embedding_dimensions or PROPHET_DEFAULT_EMBEDDING_DIMENSIONS))

    async def after_publish(
        self,
        result: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        edition_id = str(result.get("edition_id") or "").strip()
        if not edition_id:
            return result
        rows = await self.embed_missing_stories(
            self.store.list_story_index_for_edition(edition_id),
            operation="gateway.prophet.embed_story",
            request_id=request_id,
        )
        warnings = list(result.get("warnings") or [])
        warnings.extend(self._semantic_duplicate_warnings(rows, edition_id=edition_id))
        result["warnings"] = warnings
        return result

    async def related_archive_for_briefing(
        self,
        *,
        interests: list[str] | None = None,
        token_budget: int = PROPHET_ARCHIVE_TOKEN_BUDGET,
    ) -> list[dict[str, Any]]:
        recent = self.store.list_story_index(days=PROPHET_DEDUP_WINDOW_DAYS, limit=80)
        window = self.store.list_story_index(days=PROPHET_SEMANTIC_WINDOW_DAYS, limit=400)
        needs_vectors = [
            item
            for item in window
            if not (isinstance(item.get("embedding_vector"), list) and item.get("embedding_vector"))
        ]
        if needs_vectors:
            await self.embed_missing_stories(
                needs_vectors[:24],
                operation="gateway.prophet.embed_recent_backfill",
            )
            recent = self.store.list_story_index(days=PROPHET_DEDUP_WINDOW_DAYS, limit=80)
        query_vectors = [
            item["embedding_vector"]
            for item in recent
            if isinstance(item.get("embedding_vector"), list) and item.get("embedding_vector")
        ]
        interest_names = [str(item).strip() for item in (interests or []) if str(item).strip()]
        if interest_names:
            interest_vector = await self._embed_text(
                "User interests: " + ", ".join(interest_names[:24]),
                operation="gateway.prophet.embed_interests",
            )
            if interest_vector is not None:
                query_vectors.append(interest_vector)
        if not query_vectors:
            return []
        exclude_keys = {
            str(item.get("headline_key") or "")
            for item in recent
            if item.get("headline_key")
        }
        exclude_ids = {
            str(item.get("story_row_id") or "")
            for item in recent
            if item.get("story_row_id")
        }
        matches = self.store.similar_stories(
            query_vectors,
            days=PROPHET_SEMANTIC_WINDOW_DAYS,
            exclude_headline_keys=exclude_keys,
            exclude_story_row_ids=exclude_ids,
            min_similarity=PROPHET_RELATED_MIN_SIMILARITY,
            limit=40,
        )
        return self._fit_token_budget(matches, token_budget=token_budget)

    async def search(
        self,
        query: str,
        *,
        days: int = PROPHET_SEMANTIC_WINDOW_DAYS,
        token_budget: int = PROPHET_ARCHIVE_TOKEN_BUDGET,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned = str(query or "").strip()
        window = max(1, min(PROPHET_SEMANTIC_WINDOW_DAYS, int(days or PROPHET_SEMANTIC_WINDOW_DAYS)))
        purpose = (
            "Previously published Daily Prophet stories from Cosmic's newspaper archive. "
            "Not live web search and not the user's memory. Use to avoid repeats and to "
            "connect a candidate story to prior coverage."
        )
        empty = {
            "archive": True,
            "purpose": purpose,
            "window_days": window,
            "query": cleaned,
            "matches": [],
            "rendered": "",
            "embedding_used": False,
            "message": "No matching previously shown Prophet stories.",
        }
        if not cleaned:
            empty["message"] = "Search query is empty."
            return empty
        query_vector = await self._embed_text(
            cleaned,
            operation="gateway.prophet.embed_query",
            request_id=request_id,
        )
        if query_vector is None:
            empty["message"] = "Semantic archive search is unavailable right now."
            return empty
        matches = self.store.similar_stories(
            [query_vector],
            days=window,
            min_similarity=PROPHET_RELATED_MIN_SIMILARITY,
            limit=24,
        )
        fitted = self._fit_token_budget(matches, token_budget=token_budget)
        return {
            "archive": True,
            "purpose": purpose,
            "window_days": window,
            "query": cleaned,
            "matches": [self._public_match(item) for item in fitted],
            "rendered": pack_archive_lines(fitted, token_budget=token_budget),
            "embedding_used": True,
            "message": (
                f"Found {len(fitted)} previously shown Daily Prophet stor"
                f"{'y' if len(fitted) == 1 else 'ies'} in the last {window} days."
                if fitted
                else "No matching previously shown Prophet stories."
            ),
        }

    async def embed_missing_stories(
        self,
        stories: list[dict[str, Any]],
        *,
        operation: str,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        pending: list[tuple[int, dict[str, Any], str]] = []
        hydrated = [dict(item) for item in stories]
        for index, item in enumerate(hydrated):
            if isinstance(item.get("embedding_vector"), list) and item.get("embedding_vector"):
                continue
            text = embedding_text_for_story(item)
            if not text:
                continue
            pending.append((index, item, text))
        if not pending:
            return hydrated
        vectors = await self._embed_texts(
            [text for _index, _item, text in pending],
            operation=operation,
            request_id=request_id,
        )
        stamped = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for (index, item, _text), vector in zip(pending, vectors):
            if vector is None:
                continue
            story_row_id = str(item.get("story_row_id") or "").strip()
            if story_row_id:
                try:
                    self.store.update_story_embedding(
                        story_row_id,
                        embedding_model=self.embedding_model,
                        embedding_dimensions=self.embedding_dimensions,
                        embedding_vector=vector,
                        embedding_updated_at=stamped,
                    )
                except Exception:
                    logger.exception(
                        "gateway.prophet.embedding_store_failed story_row_id=%s",
                        story_row_id,
                    )
                    continue
            updated = dict(item)
            updated["embedding_vector"] = vector
            updated["embedding_model"] = self.embedding_model
            updated["embedding_dimensions"] = self.embedding_dimensions
            hydrated[index] = updated
        return hydrated

    def _semantic_duplicate_warnings(
        self,
        current_rows: list[dict[str, Any]],
        *,
        edition_id: str,
    ) -> list[str]:
        query_pairs = [
            (item, item.get("embedding_vector"))
            for item in current_rows
            if isinstance(item.get("embedding_vector"), list) and item.get("embedding_vector")
        ]
        if not query_pairs:
            return []
        warnings: list[str] = []
        seen: set[str] = set()
        for story, vector in query_pairs:
            if not isinstance(vector, list):
                continue
            matches = self.store.similar_stories(
                [vector],
                days=PROPHET_SEMANTIC_WINDOW_DAYS,
                exclude_edition_id=edition_id,
                min_similarity=PROPHET_DUPLICATE_MIN_SIMILARITY,
                limit=3,
            )
            if not matches:
                continue
            nearest = matches[0]
            headline = str(story.get("headline") or "").strip()
            prior = str(nearest.get("headline") or "").strip()
            key = _normalize_headline_key(headline) + "|" + str(nearest.get("story_row_id") or "")
            if not headline or key in seen:
                continue
            seen.add(key)
            warnings.append(
                f"'{headline[:70]}' looks like previously shown coverage "
                f"({nearest.get('edition_date')} {nearest.get('slot')}: '{prior[:70]}')."
            )
        return warnings

    def _fit_token_budget(
        self,
        items: list[dict[str, Any]],
        *,
        token_budget: int,
    ) -> list[dict[str, Any]]:
        fitted: list[dict[str, Any]] = []
        used = estimate_tokens(ARCHIVE_BRIEFING_HEADER) + 1
        budget = max(32, int(token_budget))
        for item in items:
            cost = estimate_tokens(format_archive_line(item)) + 1
            if used + cost > budget:
                break
            fitted.append(item)
            used += cost
        return fitted

    def _public_match(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "edition_date": item.get("edition_date"),
            "slot": item.get("slot"),
            "section_id": item.get("section_id"),
            "headline": item.get("headline"),
            "dek": item.get("dek"),
            "source_name": item.get("source_name"),
            "source_url": item.get("source_url"),
            "shown_at": item.get("shown_at"),
            "semantic_similarity": item.get("semantic_similarity"),
        }

    async def _embed_text(
        self,
        text: str,
        *,
        operation: str,
        request_id: str | None = None,
    ) -> list[float] | None:
        vectors = await self._embed_texts(
            [text],
            operation=operation,
            request_id=request_id,
        )
        return vectors[0] if vectors else None

    async def _embed_texts(
        self,
        texts: list[str],
        *,
        operation: str,
        request_id: str | None = None,
    ) -> list[list[float] | None]:
        cleaned = [str(text or "").strip() for text in texts]
        if not any(cleaned) or self.memory_client is None or not self.memory_client.enabled:
            return [None] * len(texts)
        try:
            response = await self.memory_client.generate_embeddings(
                {
                    "texts": [text or " " for text in cleaned],
                    "dimensions": self.embedding_dimensions,
                    "batch_size": max(1, min(len(cleaned), 128)),
                    "max_parallel_requests": 1,
                    "normalize": True,
                    "usage_operation": operation,
                    "usage_source_component": "gateway",
                    "usage_source_id": "gateway:prophet",
                    "usage_request_id": str(request_id or "").strip() or None,
                    "usage_route": "internal",
                    "usage_metadata": {
                        "embedding_via": "cosmic-memory",
                        "prophet_archive": True,
                    },
                }
            )
        except Exception:
            logger.exception("gateway.prophet.embedding_failed operation=%s", operation)
            return [None] * len(texts)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            return [None] * len(texts)
        results: list[list[float] | None] = []
        for index, _text in enumerate(cleaned):
            payload = items[index] if index < len(items) else None
            results.append(self._vector_from_response_item(payload))
        return results

    def _vector_from_response_item(self, payload: Any) -> list[float] | None:
        if not isinstance(payload, dict):
            return None
        raw_vector = payload.get("vector")
        if not isinstance(raw_vector, list) or not raw_vector:
            return None
        try:
            vector = [float(value) for value in raw_vector]
        except (TypeError, ValueError):
            return None
        magnitude = math.sqrt(sum(component * component for component in vector))
        if magnitude <= 0:
            return None
        return [component / magnitude for component in vector]
