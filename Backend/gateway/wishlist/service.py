from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx
import yaml

from shared import UsageEvent, begin_metered_call, build_model_key, build_usage_event

from ..prompts import capability_wishlist_adjudicator_system_prompt
from .models import WishlistAdjudicationDecision
from .store import CapabilityWishlistStore

logger = logging.getLogger(__name__)

_MAX_STORE_SCAN = 5000
_MAX_CANDIDATES = 8
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class _HybridCandidate:
    item: dict[str, Any]
    rrf_score: float
    lexical_rank: int | None = None
    lexical_score: float | None = None
    semantic_rank: int | None = None
    semantic_similarity: float | None = None


class CapabilityWishlistService:
    """Gateway-owned capability wishlist with hybrid lookup and xAI adjudication."""

    def __init__(
        self,
        *,
        store: CapabilityWishlistStore,
        export_dir: Path,
        perplexity_api_key: str = "",
        embedding_model: str = "pplx-embed-v1-4b",
        embedding_dimensions: int = 1024,
        xai_api_key: str = "",
        adjudicator_model: str = "grok-4-1-fast-reasoning",
        usage_recorder: Callable[[UsageEvent | dict[str, Any]], None] | None = None,
        owner_user_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.export_dir = export_dir
        self.perplexity_api_key = str(perplexity_api_key or "").strip()
        self.embedding_model = str(embedding_model or "").strip() or "pplx-embed-v1-4b"
        self.embedding_dimensions = max(128, int(embedding_dimensions or 1024))
        self.xai_api_key = str(xai_api_key or "").strip()
        self.adjudicator_model = str(adjudicator_model or "").strip() or "grok-4-1-fast-reasoning"
        self._usage_recorder = usage_recorder
        self._owner_user_id = str(owner_user_id or "").strip() or None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), http2=True)
        self._owns_client = client is None
        self._capture_lock = asyncio.Lock()
        self._last_export_sync_at: str | None = None

    async def initialize(self) -> None:
        self.store.initialize()
        await self._sync_exports()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def summary(self) -> dict[str, Any]:
        return {
            **self.store.summary(),
            "export_dir": str(self.export_dir),
            "last_export_sync_at": self._last_export_sync_at,
            "embedding_enabled": bool(self.perplexity_api_key),
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "adjudicator_enabled": bool(self.xai_api_key),
            "adjudicator_model": self.adjudicator_model,
        }

    async def get_item(self, capability_id: str) -> dict[str, Any] | None:
        item = self.store.get_item(capability_id)
        return self._serialize_item(item) if item is not None else None

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [
            item
            for item in (
                self._serialize_item(raw_item)
                for raw_item in self.store.list_items(limit=max(1, min(int(limit), 500)))
            )
            if item is not None
        ]

    async def search(self, *, query: str, limit: int = 3) -> dict[str, Any]:
        query_text = self._clean_text(query)
        max_results = max(1, min(int(limit or 3), 10))
        if not query_text:
            return {"query": "", "matches": [], "count": 0, "embedding_used": False, "message": "Search query is empty."}
        lexical = self.store.search_lexical(query_text, limit=max(max_results, _MAX_CANDIDATES))
        query_embedding = await self._embed_text(
            query_text,
            operation="gateway.capability_wishlist.embed_query",
            metadata_json={"wishlist_operation": "search", "query": query_text[:200]},
        )
        semantic = self._semantic_matches(query_embedding, limit=max(max_results, _MAX_CANDIDATES))
        matches = self._fuse_candidates(lexical_matches=lexical, semantic_matches=semantic, limit=max_results)
        return {
            "query": query_text,
            "matches": matches,
            "count": len(matches),
            "embedding_used": query_embedding is not None,
            "message": f"Found {len(matches)} matching capability wishlist entries." if matches else "No matching capability wishlist entries found.",
        }

    async def capture(
        self,
        *,
        title: str,
        summary: str,
        desired_outcome: str | None = None,
        domain: str | None = None,
        tags: list[str] | None = None,
        evidence: str | None = None,
        source_component: str | None = None,
        source_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        route: str | None = None,
        created_by: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title_text = self._clean_text(title)
        summary_text = self._clean_text(summary)
        if not title_text:
            raise ValueError("title is required")
        if not summary_text:
            raise ValueError("summary is required")
        desired_outcome_text = self._clean_text(desired_outcome) or None
        domain_text = self._normalize_domain(domain)
        tag_list = self._normalize_tags(tags or [])
        alias_list = self._merge_string_lists([title_text], [tag.replace("_", " ") for tag in tag_list])
        fingerprint = self._canonical_fingerprint(title_text, summary_text, desired_outcome_text, domain_text)
        captured_at = self._utcnow_iso()
        evidence_text = self._build_evidence_text(
            title=title_text,
            summary=summary_text,
            desired_outcome=desired_outcome_text,
            evidence=evidence,
        )
        incoming = {
            "title": title_text,
            "summary": summary_text,
            "desired_outcome": desired_outcome_text,
            "domain": domain_text,
            "tags": tag_list,
            "aliases": alias_list,
            "canonical_fingerprint": fingerprint,
        }
        evidence_event = self._build_evidence_event(
            title=title_text,
            summary=summary_text,
            desired_outcome=desired_outcome_text,
            domain=domain_text,
            tags=tag_list,
            evidence_text=evidence_text,
            captured_at=captured_at,
            source_component=self._clean_text(source_component) or "orchestrator",
            source_id=source_id,
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            route=route,
            metadata=self._clean_mapping(metadata or {}),
        )
        actor = self._clean_text(created_by) or "cosmic/orchestrator:1.0.0"

        async with self._capture_lock:
            embedding = await self._embed_text(
                self._embedding_text(
                    title=title_text,
                    summary=summary_text,
                    desired_outcome=desired_outcome_text,
                    domain=domain_text,
                    tags=tag_list,
                ),
                operation="gateway.capability_wishlist.embed_item",
                metadata_json={"wishlist_operation": "capture", "title": title_text[:200]},
            )
            matches = self._fuse_candidates(
                lexical_matches=self.store.search_lexical(
                    self._lookup_query(title=title_text, summary=summary_text, desired_outcome=desired_outcome_text, domain=domain_text, tags=tag_list),
                    limit=_MAX_CANDIDATES,
                ),
                semantic_matches=self._semantic_matches(embedding, limit=_MAX_CANDIDATES),
                limit=3,
            )
            exact_item = self._find_exact_item(normalized_title=self._normalize_title(title_text), canonical_fingerprint=fingerprint)
            if exact_item is not None:
                result = await self._apply_exact_match(
                    existing=exact_item,
                    incoming=incoming,
                    embedding=embedding,
                    evidence_event=evidence_event,
                    updated_by=actor,
                    metadata=self._clean_mapping(metadata or {}),
                    matches=matches,
                )
                await self._sync_exports()
                return result

            adjudication = None
            if matches and self.xai_api_key:
                adjudication = await self._adjudicate_capture(
                    incoming=incoming,
                    evidence_text=evidence_text,
                    matches=matches,
                    request_id=request_id,
                    session_id=session_id,
                    task_id=task_id,
                )
            if adjudication is not None:
                result = await self._apply_adjudication(
                    adjudication=adjudication,
                    incoming=incoming,
                    embedding=embedding,
                    evidence_event=evidence_event,
                    updated_by=actor,
                    metadata=self._clean_mapping(metadata or {}),
                    matches=matches,
                )
                if result is not None:
                    await self._sync_exports()
                    return result

            created = self.store.create_item(
                title=title_text,
                normalized_title=self._normalize_title(title_text),
                summary=summary_text,
                desired_outcome=desired_outcome_text,
                domain=domain_text,
                tags=tag_list,
                aliases=alias_list,
                canonical_fingerprint=fingerprint,
                created_by=actor,
                embedding_model=self.embedding_model if embedding is not None else None,
                embedding_dimensions=self.embedding_dimensions if embedding is not None else None,
                embedding_vector=embedding,
                adjudication_mode="fallback_create",
                evidence_event=evidence_event,
                metadata=self._clean_mapping(metadata or {}),
            )
            await self._sync_exports()
            return self._capture_response(
                status="created_new",
                item=created,
                message=f'Added new capability wishlist entry {created["capability_id"]}: {created["title"]}.',
                decision_mode="fallback_create",
                reason="No existing capability wishlist entry was similar enough to merge safely.",
                matches=matches,
            )

    async def _apply_exact_match(
        self,
        *,
        existing: dict[str, Any],
        incoming: dict[str, Any],
        embedding: list[float] | None,
        evidence_event: dict[str, Any],
        updated_by: str,
        metadata: dict[str, Any],
        matches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        merged = self._merge_existing_with_incoming(existing=existing, incoming=incoming)
        if self._wishlist_item_changed(existing=existing, merged=merged):
            updated_item, evidence_inserted = self.store.update_item(
                existing["capability_id"],
                title=merged["title"],
                normalized_title=self._normalize_title(merged["title"]),
                summary=merged["summary"],
                desired_outcome=merged.get("desired_outcome"),
                domain=merged["domain"],
                tags=merged["tags"],
                aliases=merged["aliases"],
                canonical_fingerprint=merged["canonical_fingerprint"],
                updated_by=updated_by,
                embedding_model=self.embedding_model if embedding is not None else existing.get("embedding_model"),
                embedding_dimensions=self.embedding_dimensions if embedding is not None else existing.get("embedding_dimensions"),
                embedding_vector=embedding if embedding is not None else existing.get("embedding_vector"),
                adjudication_mode="deterministic_exact_update",
                evidence_event=evidence_event,
                metadata=metadata or existing.get("metadata"),
            )
            return self._capture_response(
                status="updated_existing",
                item=updated_item,
                message=(
                    f'Updated existing capability wishlist entry {updated_item["capability_id"]}: {updated_item["title"]}.'
                    if evidence_inserted else
                    f'Refined existing capability wishlist entry {updated_item["capability_id"]}: {updated_item["title"]}.'
                ),
                decision_mode="deterministic_exact_update",
                reason="An exact existing entry matched, and the new capture improved the canonical entry.",
                matches=matches,
            )
        appended_item, inserted = self.store.append_evidence_only(
            existing["capability_id"],
            updated_by=updated_by,
            adjudication_mode="deterministic_exact_append",
            event=evidence_event,
        )
        if inserted:
            return self._capture_response(
                status="appended_evidence",
                item=appended_item,
                message=f'Added supporting evidence to existing capability wishlist entry {appended_item["capability_id"]}: {appended_item["title"]}.',
                decision_mode="deterministic_exact_append",
                reason="The capability already existed, but the new capture added useful supporting evidence.",
                matches=matches,
            )
        duplicate_item = self.store.touch_duplicate(
            existing["capability_id"],
            updated_by=updated_by,
            adjudication_mode="deterministic_exact_duplicate",
            seen_at=evidence_event["captured_at"],
        )
        return self._capture_response(
            status="skipped_duplicate",
            item=duplicate_item,
            message=f'Capability wishlist entry already exists as {duplicate_item["capability_id"]}: {duplicate_item["title"]}.',
            decision_mode="deterministic_exact_duplicate",
            reason="The same capability gap and evidence were already captured.",
            matches=matches,
        )

    async def _apply_adjudication(
        self,
        *,
        adjudication: WishlistAdjudicationDecision,
        incoming: dict[str, Any],
        embedding: list[float] | None,
        evidence_event: dict[str, Any],
        updated_by: str,
        metadata: dict[str, Any],
        matches: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if adjudication.decision == "create_new":
            created = self.store.create_item(
                title=incoming["title"],
                normalized_title=self._normalize_title(incoming["title"]),
                summary=incoming["summary"],
                desired_outcome=incoming.get("desired_outcome"),
                domain=incoming["domain"],
                tags=incoming["tags"],
                aliases=incoming["aliases"],
                canonical_fingerprint=incoming["canonical_fingerprint"],
                created_by=updated_by,
                embedding_model=self.embedding_model if embedding is not None else None,
                embedding_dimensions=self.embedding_dimensions if embedding is not None else None,
                embedding_vector=embedding,
                adjudication_mode="xai_create_new",
                evidence_event=evidence_event,
                metadata=metadata,
            )
            return self._capture_response(
                status="created_new",
                item=created,
                message=f'Added new capability wishlist entry {created["capability_id"]}: {created["title"]}.',
                decision_mode="xai_create_new",
                reason=adjudication.reason or "The adjudicator determined the capability gap is distinct.",
                matches=matches,
            )

        target_item = self._match_target_from_results(adjudication.target_capability_id, matches)
        if target_item is None:
            logger.warning(
                "gateway.capability_wishlist.invalid_target decision=%s target=%s",
                adjudication.decision,
                adjudication.target_capability_id,
            )
            return None

        if adjudication.decision == "skip_duplicate":
            duplicate_item = self.store.touch_duplicate(
                target_item["capability_id"],
                updated_by=updated_by,
                adjudication_mode="xai_skip_duplicate",
                seen_at=evidence_event["captured_at"],
            )
            return self._capture_response(
                status="skipped_duplicate",
                item=duplicate_item,
                message=f'Capability wishlist entry already exists as {duplicate_item["capability_id"]}: {duplicate_item["title"]}.',
                decision_mode="xai_skip_duplicate",
                reason=adjudication.reason or "The adjudicator determined the capture is a duplicate.",
                matches=matches,
            )

        if adjudication.decision == "append_evidence":
            appended_item, inserted = self.store.append_evidence_only(
                target_item["capability_id"],
                updated_by=updated_by,
                adjudication_mode="xai_append_evidence",
                event=evidence_event,
            )
            return self._capture_response(
                status="appended_evidence" if inserted else "skipped_duplicate",
                item=appended_item,
                message=(
                    f'Added supporting evidence to existing capability wishlist entry {appended_item["capability_id"]}: {appended_item["title"]}.'
                    if inserted else
                    f'Capability wishlist entry already exists as {appended_item["capability_id"]}: {appended_item["title"]}.'
                ),
                decision_mode="xai_append_evidence",
                reason=adjudication.reason or "The adjudicator kept the canonical item and only added evidence.",
                matches=matches,
            )

        if adjudication.decision == "update_existing":
            merged = self._merge_from_adjudication(existing=target_item, incoming=incoming, decision=adjudication)
            merged_embedding = await self._embed_text(
                self._embedding_text(
                    title=merged["title"],
                    summary=merged["summary"],
                    desired_outcome=merged.get("desired_outcome"),
                    domain=merged["domain"],
                    tags=merged["tags"],
                ),
                operation="gateway.capability_wishlist.embed_item",
                metadata_json={"wishlist_operation": "update_existing", "target_capability_id": target_item["capability_id"]},
            )
            updated_item, evidence_inserted = self.store.update_item(
                target_item["capability_id"],
                title=merged["title"],
                normalized_title=self._normalize_title(merged["title"]),
                summary=merged["summary"],
                desired_outcome=merged.get("desired_outcome"),
                domain=merged["domain"],
                tags=merged["tags"],
                aliases=merged["aliases"],
                canonical_fingerprint=merged["canonical_fingerprint"],
                updated_by=updated_by,
                embedding_model=self.embedding_model if merged_embedding is not None else target_item.get("embedding_model"),
                embedding_dimensions=self.embedding_dimensions if merged_embedding is not None else target_item.get("embedding_dimensions"),
                embedding_vector=merged_embedding if merged_embedding is not None else target_item.get("embedding_vector"),
                adjudication_mode="xai_update_existing",
                evidence_event=evidence_event,
                metadata=metadata or target_item.get("metadata"),
            )
            return self._capture_response(
                status="updated_existing",
                item=updated_item,
                message=(
                    f'Updated existing capability wishlist entry {updated_item["capability_id"]}: {updated_item["title"]}.'
                    if evidence_inserted else
                    f'Refined existing capability wishlist entry {updated_item["capability_id"]}: {updated_item["title"]}.'
                ),
                decision_mode="xai_update_existing",
                reason=adjudication.reason or "The adjudicator decided the incoming capture should refine an existing entry.",
                matches=matches,
            )
        return None

    async def _adjudicate_capture(
        self,
        *,
        incoming: dict[str, Any],
        evidence_text: str,
        matches: list[dict[str, Any]],
        request_id: str | None,
        session_id: str | None,
        task_id: str | None,
    ) -> WishlistAdjudicationDecision | None:
        metered_call = begin_metered_call(prefix="call")
        headers = {"Authorization": f"Bearer {self.xai_api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.adjudicator_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": capability_wishlist_adjudicator_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "incoming": {**incoming, "evidence_text": evidence_text},
                            "candidates": [
                                {
                                    "capability_id": item["capability_id"],
                                    "title": item["title"],
                                    "summary": item["summary"],
                                    "desired_outcome": item.get("desired_outcome"),
                                    "domain": item["domain"],
                                    "tags": item.get("tags") or [],
                                    "aliases": item.get("aliases") or [],
                                    "evidence_count": item.get("evidence_count"),
                                    "latest_evidence_text": item.get("latest_evidence_text"),
                                }
                                for item in matches[:3]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "wishlist_adjudication_decision",
                    "strict": True,
                    "schema": WishlistAdjudicationDecision.model_json_schema(),
                },
            },
        }
        try:
            response = await self._client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=body)
            payload = self._response_json_or_none(response)
            if response.status_code == 400:
                body["response_format"] = {"type": "json_object"}
                response = await self._client.post("https://api.x.ai/v1/chat/completions", headers=headers, json=body)
                payload = self._response_json_or_none(response)
            await self._record_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="gateway",
                    source_id="gateway:capability_wishlist",
                    task_id=task_id,
                    session_id=session_id,
                    request_id=request_id,
                    route="internal",
                    operation="gateway.capability_wishlist.adjudicate",
                    model_key=build_model_key("xai", self.adjudicator_model),
                    provider_request_id=self._provider_request_id(response),
                    user_id=self._owner_user_id,
                    raw_usage=(payload or {}).get("usage") if isinstance(payload, dict) else None,
                    success=response.status_code < 400,
                    error_code=None if response.status_code < 400 else f"HTTP_{response.status_code}",
                    metadata_json={"wishlist_operation": "capture_adjudication"},
                )
            )
            if response.status_code >= 400 or not isinstance(payload, dict):
                return None
            return self._parse_adjudication_response(payload, matches=matches)
        except Exception:
            logger.exception("gateway.capability_wishlist.adjudication_failed")
            await self._record_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="gateway",
                    source_id="gateway:capability_wishlist",
                    task_id=task_id,
                    session_id=session_id,
                    request_id=request_id,
                    route="internal",
                    operation="gateway.capability_wishlist.adjudicate",
                    model_key=build_model_key("xai", self.adjudicator_model),
                    user_id=self._owner_user_id,
                    raw_usage=None,
                    success=False,
                    error_code="EXCEPTION",
                    metadata_json={"wishlist_operation": "capture_adjudication"},
                )
            )
            return None

    async def _embed_text(
        self,
        text: str,
        *,
        operation: str,
        metadata_json: dict[str, Any],
    ) -> list[float] | None:
        input_text = self._clean_text(text)
        if not input_text or not self.perplexity_api_key:
            return None
        metered_call = begin_metered_call(prefix="call")
        response = None
        payload: dict[str, Any] | None = None
        try:
            response = await self._client.post(
                "https://api.perplexity.ai/embeddings",
                headers={"Authorization": f"Bearer {self.perplexity_api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.embedding_model,
                    "input": input_text,
                    "encoding_format": "base64",
                    "dimensions": self.embedding_dimensions,
                },
            )
            payload = self._response_json_or_none(response)
            await self._record_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="gateway",
                    source_id="gateway:capability_wishlist",
                    route="internal",
                    operation=operation,
                    model_key=build_model_key("perplexity", self.embedding_model),
                    provider_request_id=self._provider_request_id(response),
                    user_id=self._owner_user_id,
                    raw_usage=(payload or {}).get("usage") if isinstance(payload, dict) else None,
                    success=response.status_code < 400,
                    error_code=None if response.status_code < 400 else f"HTTP_{response.status_code}",
                    metadata_json=metadata_json,
                )
            )
            if response.status_code >= 400:
                return None
            return self._parse_embedding_response(payload)
        except Exception:
            logger.exception("gateway.capability_wishlist.embedding_failed")
            await self._record_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="gateway",
                    source_id="gateway:capability_wishlist",
                    route="internal",
                    operation=operation,
                    model_key=build_model_key("perplexity", self.embedding_model),
                    user_id=self._owner_user_id,
                    raw_usage=(payload or {}).get("usage") if isinstance(payload, dict) else None,
                    success=False,
                    error_code="EXCEPTION",
                    metadata_json=metadata_json,
                )
            )
            return None

    def _parse_embedding_response(self, payload: dict[str, Any] | None) -> list[float] | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        item = data[0] if isinstance(data[0], dict) else None
        if item is None:
            return None
        raw_embedding = item.get("embedding")
        if isinstance(raw_embedding, list):
            vector = [float(value) for value in raw_embedding]
        elif isinstance(raw_embedding, str):
            vector = self._decode_base64_int8(raw_embedding)
        else:
            return None
        return self._normalize_vector(vector)

    def _decode_base64_int8(self, raw_value: str) -> list[float] | None:
        try:
            decoded = base64.b64decode(raw_value, validate=True)
        except (ValueError, binascii.Error):
            return None
        if not decoded:
            return None
        vector = [float(byte - 256 if byte > 127 else byte) for byte in decoded]
        if self.embedding_dimensions and len(vector) != self.embedding_dimensions:
            logger.warning(
                "gateway.capability_wishlist.embedding_dimension_mismatch expected=%s actual=%s",
                self.embedding_dimensions,
                len(vector),
            )
        return vector

    def _semantic_matches(self, embedding: list[float] | None, *, limit: int) -> list[dict[str, Any]]:
        if embedding is None:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self.store.list_embedding_items():
            item_vector = item.get("embedding_vector")
            if not isinstance(item_vector, list) or not item_vector:
                continue
            similarity = self._cosine_similarity(embedding, item_vector)
            if similarity <= 0:
                continue
            item_copy = dict(item)
            item_copy["semantic_similarity"] = similarity
            scored.append((similarity, item_copy))
        scored.sort(key=lambda entry: (entry[0], entry[1].get("updated_at") or ""), reverse=True)
        return [entry[1] for entry in scored[: max(1, min(limit, _MAX_CANDIDATES))]]

    def _fuse_candidates(
        self,
        *,
        lexical_matches: list[dict[str, Any]],
        semantic_matches: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        candidates: dict[str, _HybridCandidate] = {}
        for rank, item in enumerate(lexical_matches, start=1):
            capability_id = str(item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            existing = candidates.get(capability_id)
            rrf = 1.0 / (_RRF_K + rank)
            lexical_score = item.get("lexical_bm25")
            if existing is None:
                candidates[capability_id] = _HybridCandidate(
                    item=item,
                    rrf_score=rrf,
                    lexical_rank=rank,
                    lexical_score=float(lexical_score) if lexical_score is not None else None,
                )
            else:
                candidates[capability_id] = _HybridCandidate(
                    item=existing.item,
                    rrf_score=existing.rrf_score + rrf,
                    lexical_rank=existing.lexical_rank or rank,
                    lexical_score=(
                        existing.lexical_score
                        if existing.lexical_score is not None else
                        (float(lexical_score) if lexical_score is not None else None)
                    ),
                    semantic_rank=existing.semantic_rank,
                    semantic_similarity=existing.semantic_similarity,
                )
        for rank, item in enumerate(semantic_matches, start=1):
            capability_id = str(item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            existing = candidates.get(capability_id)
            rrf = 1.0 / (_RRF_K + rank)
            similarity = float(item.get("semantic_similarity") or 0.0)
            if existing is None:
                candidates[capability_id] = _HybridCandidate(
                    item=item,
                    rrf_score=rrf,
                    semantic_rank=rank,
                    semantic_similarity=similarity,
                )
            else:
                candidates[capability_id] = _HybridCandidate(
                    item=existing.item,
                    rrf_score=existing.rrf_score + rrf,
                    lexical_rank=existing.lexical_rank,
                    lexical_score=existing.lexical_score,
                    semantic_rank=existing.semantic_rank or rank,
                    semantic_similarity=existing.semantic_similarity if existing.semantic_similarity is not None else similarity,
                )
        ordered = sorted(
            candidates.values(),
            key=lambda entry: (
                entry.rrf_score,
                entry.semantic_similarity or 0.0,
                -(entry.lexical_score or 0.0),
                entry.item.get("updated_at") or "",
            ),
            reverse=True,
        )
        results: list[dict[str, Any]] = []
        for entry in ordered[: max(1, min(limit, 10))]:
            item = self._serialize_item(entry.item)
            item["rrf_score"] = round(entry.rrf_score, 6)
            item["lexical_rank"] = entry.lexical_rank
            item["semantic_rank"] = entry.semantic_rank
            item["lexical_score"] = round(entry.lexical_score, 6) if entry.lexical_score is not None else None
            item["semantic_similarity"] = round(entry.semantic_similarity, 6) if entry.semantic_similarity is not None else None
            results.append(item)
        return results

    async def _sync_exports(self) -> None:
        items = [self._serialize_item(item) for item in self.store.list_items(limit=_MAX_STORE_SCAN)]
        generated_at = self._utcnow_iso()
        snapshot = {"generated_at": generated_at, "total_items": len(items), "items": items}
        self.export_dir.mkdir(parents=True, exist_ok=True)
        (self.export_dir / "current.yaml").write_text(
            yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (self.export_dir / "current.md").write_text(self._render_markdown(items, generated_at=generated_at), encoding="utf-8")
        self._last_export_sync_at = generated_at

    def _render_markdown(self, items: list[dict[str, Any]], *, generated_at: str) -> str:
        lines = ["# COSMIC Capability Wishlist", "", f"Generated: {generated_at}", f"Total items: {len(items)}", ""]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(str(item.get("domain") or "general"), []).append(item)
        for domain in sorted(grouped):
            lines.append(f"## {domain}")
            lines.append("")
            for item in grouped[domain]:
                lines.append(
                    f'- `{item["capability_id"]}` [{item.get("status") or "candidate"} | {item.get("priority") or "normal"}] {item.get("title") or "Untitled capability"}'
                )
                if item.get("summary"):
                    lines.append(f'  Summary: {item["summary"]}')
                if item.get("desired_outcome"):
                    lines.append(f'  Desired outcome: {item["desired_outcome"]}')
                tags = item.get("tags") or []
                if isinstance(tags, list) and tags:
                    lines.append(f'  Tags: {", ".join(str(tag) for tag in tags)}')
                if item.get("evidence_count") is not None:
                    lines.append(f'  Evidence count: {item["evidence_count"]}')
                if item.get("updated_at"):
                    lines.append(f'  Updated: {item["updated_at"]}')
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _find_exact_item(self, *, normalized_title: str, canonical_fingerprint: str) -> dict[str, Any] | None:
        for item in self.store.list_items(limit=_MAX_STORE_SCAN):
            if str(item.get("canonical_fingerprint") or "").strip() == canonical_fingerprint:
                return item
            if self._normalize_title(item.get("normalized_title") or item.get("title")) == normalized_title:
                return item
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            if any(self._normalize_title(alias) == normalized_title for alias in aliases):
                return item
        return None

    def _match_target_from_results(self, capability_id: str | None, matches: list[dict[str, Any]]) -> dict[str, Any] | None:
        target = self._clean_text(capability_id)
        if not target:
            return None
        for item in matches:
            if str(item.get("capability_id") or "").strip() == target:
                return self.store.get_item(target) or item
        return None

    def _parse_adjudication_response(
        self,
        payload: dict[str, Any],
        *,
        matches: list[dict[str, Any]],
    ) -> WishlistAdjudicationDecision:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("xAI adjudicator returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        text = self._coerce_message_text(content)
        decision = WishlistAdjudicationDecision.model_validate(json.loads(text))
        if decision.target_capability_id and decision.target_capability_id not in {
            str(item.get("capability_id") or "").strip() for item in matches
        }:
            raise ValueError("xAI adjudicator returned an unknown target_capability_id")
        return decision

    def _coerce_message_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(value or "").strip()

    def _response_json_or_none(self, response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _provider_request_id(self, response: httpx.Response | None) -> str | None:
        if response is None:
            return None
        for header_name in ("x-request-id", "request-id", "x-perplexity-request-id"):
            value = response.headers.get(header_name, "").strip()
            if value:
                return value
        return None

    async def _record_usage_event(self, event: UsageEvent) -> None:
        if self._usage_recorder is None:
            return
        try:
            self._usage_recorder(event)
        except Exception:
            logger.exception(
                "gateway.capability_wishlist.usage_record_failed llm_call_id=%s operation=%s",
                event.llm_call_id,
                event.operation,
            )

    def _capture_response(
        self,
        *,
        status: str,
        item: dict[str, Any],
        message: str,
        decision_mode: str,
        reason: str,
        matches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "capability_id": item["capability_id"],
            "title": item["title"],
            "item": self._serialize_item(item),
            "message": message,
            "decision_mode": decision_mode,
            "reason": reason,
            "matches": matches[:3],
        }

    def _serialize_item(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        payload = dict(item)
        payload.pop("embedding_vector", None)
        payload.pop("normalized_title", None)
        payload.pop("canonical_fingerprint", None)
        return payload

    def _wishlist_item_changed(self, *, existing: dict[str, Any], merged: dict[str, Any]) -> bool:
        for key in ("title", "summary", "desired_outcome", "domain", "canonical_fingerprint"):
            if self._clean_text(existing.get(key)) != self._clean_text(merged.get(key)):
                return True
        if list(existing.get("tags") or []) != list(merged.get("tags") or []):
            return True
        if list(existing.get("aliases") or []) != list(merged.get("aliases") or []):
            return True
        return False

    def _merge_existing_with_incoming(self, *, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        title = str(existing.get("title") or incoming["title"]).strip() or incoming["title"]
        summary = self._prefer_summary(existing.get("summary"), incoming["summary"])
        desired_outcome = self._prefer_outcome(existing.get("desired_outcome"), incoming.get("desired_outcome"))
        domain = self._prefer_domain(existing.get("domain"), incoming["domain"])
        tags = self._merge_string_lists(existing.get("tags") or [], incoming.get("tags") or [])
        aliases = self._merge_string_lists(existing.get("aliases") or [], incoming.get("aliases") or [], [existing.get("title"), incoming["title"]])
        return {
            "title": title,
            "summary": summary,
            "desired_outcome": desired_outcome,
            "domain": domain,
            "tags": tags,
            "aliases": aliases,
            "canonical_fingerprint": self._canonical_fingerprint(title, summary, desired_outcome, domain),
        }

    def _merge_from_adjudication(
        self,
        *,
        existing: dict[str, Any],
        incoming: dict[str, Any],
        decision: WishlistAdjudicationDecision,
    ) -> dict[str, Any]:
        merged_fields = decision.merged_fields
        title = self._clean_text(merged_fields.title) or str(existing.get("title") or incoming["title"]).strip()
        summary = self._clean_text(merged_fields.summary) or self._prefer_summary(existing.get("summary"), incoming["summary"])
        desired_outcome = self._clean_text(merged_fields.desired_outcome) or self._prefer_outcome(
            existing.get("desired_outcome"),
            incoming.get("desired_outcome"),
        )
        domain = self._normalize_domain(merged_fields.domain or existing.get("domain") or incoming["domain"])
        tags = self._merge_string_lists(existing.get("tags") or [], incoming.get("tags") or [], merged_fields.tags)
        aliases = self._merge_string_lists(existing.get("aliases") or [], incoming.get("aliases") or [], [existing.get("title"), incoming["title"], merged_fields.title])
        return {
            "title": title,
            "summary": summary,
            "desired_outcome": desired_outcome,
            "domain": domain,
            "tags": tags,
            "aliases": aliases,
            "canonical_fingerprint": self._canonical_fingerprint(title, summary, desired_outcome, domain),
        }

    def _build_evidence_event(
        self,
        *,
        title: str,
        summary: str,
        desired_outcome: str | None,
        domain: str,
        tags: list[str],
        evidence_text: str,
        captured_at: str,
        source_component: str,
        source_id: str | None,
        request_id: str | None,
        session_id: str | None,
        task_id: str | None,
        route: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        hash_input = "\n".join(
            [
                self._normalize_title(title),
                self._normalize_text(summary),
                self._normalize_text(desired_outcome),
                self._normalize_text(domain),
                ",".join(tags),
                self._normalize_text(evidence_text),
            ]
        )
        return {
            "evidence_id": f"wlev_{uuid4().hex}",
            "content_hash": hashlib.sha256(hash_input.encode("utf-8")).hexdigest(),
            "captured_at": captured_at,
            "source_component": source_component,
            "source_id": self._clean_text(source_id) or None,
            "request_id": self._clean_text(request_id) or None,
            "session_id": self._clean_text(session_id) or None,
            "task_id": self._clean_text(task_id) or None,
            "route": self._clean_text(route) or None,
            "title": title,
            "summary": summary,
            "desired_outcome": desired_outcome,
            "domain": domain,
            "tags": tags,
            "evidence_text": evidence_text,
            "decision": "captured",
            "metadata": metadata,
        }

    def _lookup_query(self, *, title: str, summary: str, desired_outcome: str | None, domain: str, tags: list[str]) -> str:
        return " ".join(part for part in [title, summary, desired_outcome or "", domain, " ".join(tags)] if part).strip()

    def _embedding_text(self, *, title: str, summary: str, desired_outcome: str | None, domain: str, tags: list[str]) -> str:
        segments = [
            f"title: {title}",
            f"summary: {summary}",
            f"desired_outcome: {desired_outcome}" if desired_outcome else "",
            f"domain: {domain}",
            f"tags: {', '.join(tags)}" if tags else "",
        ]
        return "\n".join(segment for segment in segments if segment).strip()

    def _build_evidence_text(self, *, title: str, summary: str, desired_outcome: str | None, evidence: str | None) -> str:
        parts = [summary]
        if desired_outcome:
            parts.append(f"Desired outcome: {desired_outcome}")
        if evidence:
            parts.append(self._clean_text(evidence))
        if title and title not in summary:
            parts.insert(0, f"Capability gap: {title}")
        return "\n".join(part for part in parts if part).strip()

    def _canonical_fingerprint(self, title: str, summary: str, desired_outcome: str | None, domain: str) -> str:
        basis = "|".join(
            [
                self._normalize_domain(domain),
                self._normalize_title(title),
                self._normalize_text(desired_outcome) or self._normalize_text(summary)[:240],
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def _prefer_summary(self, existing: Any, incoming: Any) -> str:
        existing_text = self._clean_text(existing)
        incoming_text = self._clean_text(incoming)
        if not existing_text:
            return incoming_text
        if not incoming_text or existing_text == incoming_text:
            return existing_text
        if len(incoming_text) > len(existing_text) + 20 and existing_text.lower() in incoming_text.lower():
            return incoming_text
        return existing_text

    def _prefer_outcome(self, existing: Any, incoming: Any) -> str | None:
        existing_text = self._clean_text(existing)
        incoming_text = self._clean_text(incoming)
        if not existing_text:
            return incoming_text or None
        if not incoming_text or existing_text == incoming_text:
            return existing_text or None
        if len(incoming_text) > len(existing_text) and existing_text.lower() in incoming_text.lower():
            return incoming_text
        return existing_text or None

    def _prefer_domain(self, existing: Any, incoming: Any) -> str:
        existing_domain = self._normalize_domain(existing)
        incoming_domain = self._normalize_domain(incoming)
        if existing_domain == "general" and incoming_domain != "general":
            return incoming_domain
        return existing_domain or incoming_domain or "general"

    def _merge_string_lists(self, *groups: list[Any]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for group in groups:
            for item in group:
                text = self._clean_text(item)
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(text)
        return merged

    def _normalize_tags(self, values: list[Any]) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = self._clean_text(value)
            if not text:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            tags.append(slug)
        return tags

    def _normalize_domain(self, value: Any) -> str:
        text = self._clean_text(value).lower()
        if not text:
            return "general"
        slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
        return slug or "general"

    def _normalize_title(self, value: Any) -> str:
        return self._normalize_text(value)

    def _normalize_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", self._clean_text(value).lower()).strip()

    def _clean_text(self, value: Any) -> str:
        return str(value or "").strip()

    def _clean_mapping(self, value: dict[str, Any]) -> dict[str, Any]:
        return {str(key): item for key, item in value.items() if item not in ("", None, [], {})}

    def _normalize_vector(self, vector: list[float] | None) -> list[float] | None:
        if not vector:
            return None
        magnitude = math.sqrt(sum(component * component for component in vector))
        if magnitude <= 0:
            return None
        return [float(component / magnitude) for component in vector]

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        length = min(len(left), len(right))
        return sum(left[index] * right[index] for index in range(length))

    def _utcnow_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
