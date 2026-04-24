from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

import httpx
from PIL import Image

from shared.response_blocks import build_response_blocks

from ..config import OrchestratorConfig
from .charting import normalize_chart_spec, render_chart_png
from .clients import (
    DirectImageSearchClient,
    DirectImageSearchConfig,
    FirecrawlVisualClient,
    FirecrawlVisualConfig,
    FireworksVisualClient,
    FireworksVisualConfig,
)

logger = logging.getLogger(__name__)

_VISUAL_SLOT_START = "[[visual_slot"
_VISUAL_SLOT_END = "]]"
_WORD_RE = re.compile(r"[a-z0-9]{2,}", re.IGNORECASE)
_EXPLICIT_IMAGE_REQUEST_MARKERS = (
    "inline image",
    "inline images",
    "show image",
    "show images",
    "show me an image",
    "show me images",
    "use your inline image",
    "use the inline image",
    "add image",
    "add images",
    "include image",
    "include images",
    "relevant image",
    "relevant images",
    "with images",
    "with an image",
    "photo",
    "photos",
    "picture",
    "pictures",
    "screenshot",
    "screenshots",
)
_GENERIC_VISUAL_QUERY_TOKENS = {
    "about",
    "complaint",
    "controversy",
    "deal",
    "details",
    "explain",
    "issue",
    "more",
    "problem",
    "situation",
    "story",
    "tell",
    "that",
    "them",
    "this",
    "thing",
    "what",
    "why",
}
_IMAGE_QUERY_NOISE_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(marker)}\b", flags=re.IGNORECASE)
    for marker in _EXPLICIT_IMAGE_REQUEST_MARKERS
)
_FAILED_INLINE_IMAGE_LABEL = "Couldn't find a reliable inline image for this answer."
_FAILED_INLINE_CHART_LABEL = "Couldn't generate a clear inline chart for this answer."
_TIMED_OUT_INLINE_IMAGE_LABEL = "This inline image took too long to finish."
_TIMED_OUT_INLINE_CHART_LABEL = "This inline chart took too long to finish."


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _slugify_filename(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _longest_visual_prefix_suffix(text: str) -> int:
    max_len = min(len(text), len(_VISUAL_SLOT_START) - 1)
    for size in range(max_len, 0, -1):
        if _VISUAL_SLOT_START.startswith(text[-size:]):
            return size
    return 0


def _clip_text(value: Any, *, limit: int = 800) -> str:
    text = _safe_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _tokenize(value: Any) -> set[str]:
    return {match.group(0).lower() for match in _WORD_RE.finditer(str(value or "").lower())}


def _text_explicitly_requests_image(value: Any) -> bool:
    text = _safe_text(value).lower()
    if not text:
        return False
    return any(marker in text for marker in _EXPLICIT_IMAGE_REQUEST_MARKERS)


def _normalize_image_search_query(value: Any, *, max_words: int = 18) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    for pattern in _IMAGE_QUERY_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s:'/-]+", " ", text)
    words = [word for word in text.split() if word]
    if max_words > 0:
        words = words[:max_words]
    return " ".join(words).strip(" -:|")


def _query_specificity_score(value: Any) -> int:
    tokens = _tokenize(value)
    return sum(1 for token in tokens if token not in _GENERIC_VISUAL_QUERY_TOKENS and len(token) >= 3)


def _looks_generic_for_image_search(value: Any) -> bool:
    text = _normalize_image_search_query(value)
    if not text:
        return True
    return _query_specificity_score(text) < 3


def _is_probably_decorative(candidate_url: str, text: str) -> bool:
    corpus = f"{candidate_url} {text}".lower()
    markers = (
        "logo",
        "icon",
        "avatar",
        "sprite",
        "banner",
        "thumbnail",
        "thumb",
        "placeholder",
        "advert",
        "ads",
    )
    return any(marker in corpus for marker in markers)


def _is_probably_ui_asset(candidate_url: str, text: str) -> bool:
    corpus = f"{candidate_url} {text}".lower()
    markers = (
        "spinner",
        "submit-spin",
        "loading",
        "loader",
        "favicon",
        "siteicon",
        "apple-touch-icon",
        "wpforms",
        "wp-content/plugins",
        "plugin",
        "sprite",
    )
    return any(marker in corpus for marker in markers)


def _is_probably_text_art(candidate_url: str, filename: str, text: str) -> bool:
    file_corpus = f"{Path(urlparse(candidate_url).path).name} {filename}".lower()
    text_corpus = str(text or "").lower()
    strong_file_markers = (
        "-text",
        "_text",
        "text.",
        "wordmark",
        "title-card",
        "titlecard",
        "masthead",
    )
    if any(marker in file_corpus for marker in strong_file_markers):
        return True
    photographic_markers = (
        "gpu",
        "gpus",
        "rack",
        "racks",
        "server",
        "servers",
        "cluster",
        "facility",
        "warehouse",
        "building",
        "buildings",
        "datacenter",
        "data center",
        "data-center",
        "chip",
        "chips",
        "turbine",
        "battery",
        "cooling",
        "supercomputer",
    )
    photographic_hits = sum(1 for marker in photographic_markers if marker in text_corpus)
    weaker_markers = (
        "banner",
        "headline",
        "header",
        "cover",
        "poster",
    )
    text_markers = (
        "wordmark",
        "title card",
        "headline graphic",
        "text graphic",
    )
    if any(marker in file_corpus for marker in weaker_markers) and photographic_hits < 2:
        return True
    if any(marker in text_corpus for marker in text_markers) and photographic_hits < 2:
        return True
    return False


def _is_probably_cross_promo(candidate_url: str, filename: str, text: str) -> bool:
    corpus = f"{candidate_url} {filename} {text}".lower()
    markers = (
        "anniversary",
        "lineup",
        "collection",
        "catalog",
        "crossover",
        "franchise",
        "all_",
        "all-",
        "bundle",
        "sale",
        "seasonal",
        "promo",
        "promotional",
    )
    return any(marker in corpus for marker in markers)


def _dimension_quality_penalty(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.0
    pixels = width * height
    min_dim = min(width, height)
    penalty = 0.0
    if pixels < 40_000:
        penalty += 0.70
    elif pixels < 120_000:
        penalty += 0.40
    elif pixels < 220_000:
        penalty += 0.18
    if min_dim < 100:
        penalty += 0.30
    elif min_dim < 180:
        penalty += 0.14
    if abs(width - height) <= max(width, height) * 0.08 and pixels < 180_000:
        penalty += 0.10
    return penalty


def _is_low_information_image_size(width: int | None, height: int | None) -> bool:
    if not width or not height:
        return False
    pixels = width * height
    min_dim = min(width, height)
    if pixels < 40_000:
        return True
    if min_dim < 100:
        return True
    return False


def _guess_filename_from_url(url: str, *, default_prefix: str) -> str:
    parsed = urlparse(str(url or "").strip())
    raw_name = Path(parsed.path).name
    filename = _slugify_filename(raw_name, fallback=f"{default_prefix}.png")
    if "." not in filename:
        filename = f"{filename}.png"
    return filename


def _normalize_scraped_image_url(source_url: str, image_url: str) -> str:
    absolute = urljoin(source_url, _safe_text(image_url))
    if not absolute.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(absolute)
    if parsed.path.rstrip("/").endswith("/_next/image"):
        proxied_target = _extract_proxy_target_url(parsed)
        if proxied_target:
            return proxied_target
    return absolute


def _extract_proxy_target_url(parsed_url: Any) -> str:
    query = parse_qs(str(getattr(parsed_url, "query", "") or ""))
    for key in ("url", "src", "image_url", "imageUrl"):
        raw_target = _safe_text((query.get(key) or [None])[0])
        if not raw_target:
            continue
        if raw_target.startswith(("http://", "https://")):
            return raw_target
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        return urljoin(origin, raw_target)
    return ""


@dataclass(slots=True)
class VisualSlotDirective:
    id: str
    kind: str
    query: str | None = None
    caption: str | None = None
    loading_label: str | None = None
    timeout_ms: int | None = None
    source_urls: list[str] = field(default_factory=list)
    chart_spec: dict[str, Any] | None = None
    context_excerpt: str = ""


@dataclass(slots=True)
class ImageCandidate:
    image_url: str
    source_url: str
    source_title: str
    source_domain: str
    source_rank: int
    alt_text: str = ""
    title: str = ""
    nearby_text: str = ""
    filename: str = ""
    width: int | None = None
    height: int | None = None
    score: float = 0.0
    retrieval_kind: str = "source_page"


class VisualDirectiveStreamParser:
    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._buffer = ""
        self._blocks: list[dict[str, Any]] = []
        self._visible_text = ""
        self._seen_slot_ids: set[str] = set()
        self._markdown_index = 1

    @property
    def visible_text(self) -> str:
        return self._visible_text

    def export_blocks(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._blocks)

    def append_slot(self, slot: VisualSlotDirective) -> None:
        normalized_slot_id = _safe_text(slot.id)
        if normalized_slot_id:
            self._seen_slot_ids.add(normalized_slot_id)
        self._append_slot_block(slot)

    def consume_text(self, chunk: str) -> tuple[str, list[VisualSlotDirective], bool]:
        raw_chunk = str(chunk or "")
        if not raw_chunk:
            return "", [], False
        self._buffer += raw_chunk
        visible_parts: list[str] = []
        new_slots: list[VisualSlotDirective] = []
        dirty = False

        while True:
            start_index = self._buffer.find(_VISUAL_SLOT_START)
            if start_index < 0:
                suffix_len = _longest_visual_prefix_suffix(self._buffer)
                flush_text = self._buffer[:-suffix_len] if suffix_len else self._buffer
                self._buffer = self._buffer[-suffix_len:] if suffix_len else ""
                if flush_text:
                    self._append_markdown_text(flush_text)
                    visible_parts.append(flush_text)
                    dirty = True
                break

            if start_index > 0:
                prefix = self._buffer[:start_index]
                self._append_markdown_text(prefix)
                visible_parts.append(prefix)
                dirty = True
                self._buffer = self._buffer[start_index:]

            end_index = self._buffer.find(_VISUAL_SLOT_END, len(_VISUAL_SLOT_START))
            if end_index < 0:
                break

            raw_directive = self._buffer[: end_index + len(_VISUAL_SLOT_END)]
            body = self._buffer[len(_VISUAL_SLOT_START) : end_index].strip()
            slot = self._parse_directive_body(body)
            if slot is None:
                self._append_markdown_text(raw_directive)
                visible_parts.append(raw_directive)
            else:
                self._append_slot_block(slot)
                new_slots.append(slot)
            dirty = True
            self._buffer = self._buffer[end_index + len(_VISUAL_SLOT_END) :]

        return "".join(visible_parts), new_slots, dirty

    def finalize_pending_text(self) -> tuple[str, bool]:
        if not self._buffer:
            return "", False
        trailing = self._buffer
        self._buffer = ""
        self._append_markdown_text(trailing)
        return trailing, True

    def replace_slot(self, slot_id: str, block: dict[str, Any]) -> bool:
        normalized_slot_id = _safe_text(slot_id)
        if not normalized_slot_id:
            return False
        for index, item in enumerate(self._blocks):
            if _safe_text(item.get("id")) != normalized_slot_id:
                continue
            self._blocks[index] = dict(block)
            return True
        return False

    def drop_slot(self, slot_id: str) -> bool:
        normalized_slot_id = _safe_text(slot_id)
        if not normalized_slot_id:
            return False
        before = len(self._blocks)
        self._blocks = [
            item
            for item in self._blocks
            if _safe_text(item.get("id")) != normalized_slot_id
        ]
        return len(self._blocks) != before

    def fail_slot(self, slot_id: str, *, label: str, detail: str | None = None) -> bool:
        normalized_slot_id = _safe_text(slot_id)
        if not normalized_slot_id:
            return False
        for item in self._blocks:
            if _safe_text(item.get("id")) != normalized_slot_id:
                continue
            if str(item.get("type")) not in {"image_slot", "chart_slot"}:
                return False
            item["status"] = "failed"
            if label:
                item["loading_label"] = label
            if detail:
                item["failure_detail"] = detail
            else:
                item.pop("failure_detail", None)
            return True
        return False

    def fail_all_pending_slots(
        self,
        *,
        image_label: str,
        chart_label: str,
    ) -> bool:
        changed = False
        for item in self._blocks:
            block_type = str(item.get("type"))
            if block_type not in {"image_slot", "chart_slot"}:
                continue
            if _safe_text(item.get("status")).lower() == "failed":
                continue
            item["status"] = "failed"
            item["loading_label"] = (
                chart_label if block_type == "chart_slot" else image_label
            )
            item.pop("failure_detail", None)
            changed = True
        return changed

    def drop_all_pending_slots(self) -> bool:
        before = len(self._blocks)
        self._blocks = [
            item
            for item in self._blocks
            if str(item.get("type")) not in {"image_slot", "chart_slot"}
        ]
        return len(self._blocks) != before

    def _append_markdown_text(self, text: str) -> None:
        if not text:
            return
        if self._blocks and str(self._blocks[-1].get("type")) == "markdown":
            self._blocks[-1]["text"] = str(self._blocks[-1].get("text") or "") + text
        else:
            self._blocks.append(
                {
                    "id": f"markdown_{self._markdown_index}",
                    "type": "markdown",
                    "text": text,
                }
            )
            self._markdown_index += 1
        self._visible_text += text

    def _append_slot_block(self, slot: VisualSlotDirective) -> None:
        loading_label = (
            _safe_text(slot.loading_label)
            or (
                "Generating a chart"
                if slot.kind == "chart"
                else "Finding a relevant image"
            )
        )
        block_type = "chart_slot" if slot.kind == "chart" else "image_slot"
        self._blocks.append(
            {
                "id": slot.id,
                "type": block_type,
                "status": "pending",
                "slot_kind": slot.kind,
                "loading_label": loading_label,
                "timeout_ms": slot.timeout_ms
                or (
                    self._config.visual_chart_slot_timeout_ms
                    if slot.kind == "chart"
                    else self._config.visual_image_slot_timeout_ms
                ),
            }
        )

    def _parse_directive_body(self, body: str) -> VisualSlotDirective | None:
        payload = self._parse_loose_json(body)
        if not isinstance(payload, dict):
            return None
        kind = _safe_text(payload.get("kind")).lower()
        if kind not in {"image", "chart"}:
            return None
        raw_id = _safe_text(payload.get("id")) or f"{kind}_{uuid4().hex[:10]}"
        slot_id = raw_id
        suffix = 1
        while slot_id in self._seen_slot_ids:
            suffix += 1
            slot_id = f"{raw_id}_{suffix}"
        self._seen_slot_ids.add(slot_id)
        source_urls = [
            _safe_text(item)
            for item in (payload.get("source_urls") if isinstance(payload.get("source_urls"), list) else [])
            if _safe_text(item)
        ]
        return VisualSlotDirective(
            id=slot_id,
            kind=kind,
            query=_safe_text(payload.get("query")) or None,
            caption=_safe_text(payload.get("caption")) or None,
            loading_label=_safe_text(payload.get("loading_label")) or None,
            timeout_ms=max(250, int(payload.get("timeout_ms")))
            if payload.get("timeout_ms") not in (None, "", [])
            else None,
            source_urls=source_urls,
            chart_spec=dict(payload) if kind == "chart" else None,
            context_excerpt=self._visible_text[-1200:].strip(),
        )

    @staticmethod
    def _parse_loose_json(raw: str) -> dict[str, Any] | None:
        text = _safe_text(raw)
        if not text:
            return None
        candidates = [text]
        if "{" in text and "}" in text:
            candidates.append(text[text.index("{") : text.rindex("}") + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    parsed = json.loads(fixed)
                except json.JSONDecodeError:
                    continue
            if isinstance(parsed, dict):
                return parsed
        return None


class VisualEnrichmentCoordinator:
    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        task_id: str,
        request_id: str | None,
        session_id: str | None,
        channel: str | None,
        user_query: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self.task_id = _safe_text(task_id)
        self.request_id = _safe_text(request_id) or None
        self.session_id = _safe_text(session_id) or None
        self.channel = _safe_text(channel) or None
        self.user_query = _safe_text(user_query)
        self._parser = VisualDirectiveStreamParser(config)
        self._snapshot_seq = 0
        self._supporting_artifacts: list[dict[str, Any]] = []
        self._supporting_artifact_ids: set[str] = set()
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._active_sidecars: dict[str, asyncio.Task[None]] = {}
        self._waiting_for_sources: set[str] = set()
        self._queued_slot_ids: list[str] = []
        self._slots: dict[str, VisualSlotDirective] = {}
        self._sources_by_url: dict[str, dict[str, str]] = {}
        self._image_slot_count = 0
        self._chart_slot_count = 0
        self._firecrawl = FirecrawlVisualClient(
            FirecrawlVisualConfig(
                api_key=config.visual_firecrawl_api_key,
                base_url=config.visual_firecrawl_base_url,
                request_timeout_sec=config.visual_firecrawl_request_timeout_sec,
            ),
            http_client=http_client,
        )
        self._image_search = DirectImageSearchClient(
            DirectImageSearchConfig(
                enabled=config.visual_image_search_enabled,
                base_url=config.visual_image_search_base_url,
                timeout_sec=config.visual_image_search_timeout_sec,
                result_limit=config.visual_image_search_result_limit,
            ),
            http_client=http_client,
        )
        self._fireworks = FireworksVisualClient(
            FireworksVisualConfig(
                api_key=config.visual_fireworks_api_key,
                base_url=config.visual_fireworks_base_url,
                model=config.visual_fireworks_model,
                vision_model=config.visual_fireworks_vision_model,
                reasoning_effort=config.visual_fireworks_reasoning_effort,
                timeout_sec=config.visual_fireworks_timeout_sec,
            ),
            http_client=http_client,
        )
        self._http_client = http_client

    @classmethod
    def is_enabled_for_task(cls, *, config: OrchestratorConfig, task_input: dict[str, Any]) -> bool:
        return bool(
            config.visual_enhancement_enabled
            and task_input.get("visual_response_enhancement_enabled")
        )

    @classmethod
    def supported_slot_kinds(cls, *, config: OrchestratorConfig) -> list[str]:
        kinds: list[str] = ["chart"]
        if _safe_text(config.visual_firecrawl_api_key) or (
            config.visual_image_search_enabled and _safe_text(config.visual_image_search_base_url)
        ):
            kinds.insert(0, "image")
        return kinds[:2]

    def consume_text(self, chunk: str) -> tuple[str, list[dict[str, Any]]]:
        visible_delta, new_slots, dirty = self._parser.consume_text(chunk)
        if self._register_slots(new_slots):
            dirty = True
        events: list[dict[str, Any]] = []
        if dirty:
            events.append(self._build_snapshot_event())
        events.extend(self._drain_ready_updates())
        return visible_delta, events

    def note_sources(self, sources: list[dict[str, str]] | None) -> list[dict[str, Any]]:
        changed = False
        for item in sources or []:
            if not isinstance(item, dict):
                continue
            url = _safe_text(item.get("url"))
            if not url:
                continue
            existing = self._sources_by_url.get(url)
            merged = {
                "url": url,
                "title": _safe_text(item.get("title"))
                or (existing.get("title") if isinstance(existing, dict) else "")
                or _safe_text(urlparse(url).netloc),
                "domain": _safe_text(item.get("domain"))
                or (existing.get("domain") if isinstance(existing, dict) else "")
                or _safe_text(urlparse(url).netloc),
            }
            if existing != merged:
                self._sources_by_url[url] = merged
                changed = True
        if changed:
            self._schedule_waiting_image_slots()
        return self._drain_ready_updates()

    def _slot_timeout_ms(self, slot: VisualSlotDirective | None) -> int:
        if slot is None:
            return 0
        if slot.timeout_ms:
            return max(250, int(slot.timeout_ms))
        if slot.kind == "chart":
            return max(250, int(self.config.visual_chart_slot_timeout_ms))
        return max(250, int(self.config.visual_image_slot_timeout_ms))

    def _finalization_wait_timeout_sec(self) -> float:
        base_sec = max(
            0.0,
            self.config.visual_finalization_grace_ms / 1000.0,
        )
        if not self._active_sidecars:
            return base_sec
        max_slot_timeout_ms = max(
            (
                self._slot_timeout_ms(self._slots.get(slot_id))
                for slot_id in self._active_sidecars.keys()
            ),
            default=0,
        )
        if max_slot_timeout_ms <= 0:
            return base_sec
        return max(base_sec, max_slot_timeout_ms / 1000.0)

    def _failure_label_for_slot(
        self,
        slot: VisualSlotDirective | None,
        *,
        timed_out: bool = False,
    ) -> str:
        kind = _safe_text(getattr(slot, "kind", "")).lower()
        if kind == "chart":
            return (
                _TIMED_OUT_INLINE_CHART_LABEL
                if timed_out
                else _FAILED_INLINE_CHART_LABEL
            )
        return (
            _TIMED_OUT_INLINE_IMAGE_LABEL
            if timed_out
            else _FAILED_INLINE_IMAGE_LABEL
        )

    def _has_any_visual_blocks(self) -> bool:
        return any(
            str(block.get("type")) in {"image_slot", "chart_slot", "image_artifact"}
            for block in self._parser.export_blocks()
        ) or bool(self._supporting_artifacts)

    def _build_implicit_image_slot(self) -> VisualSlotDirective | None:
        if self._has_any_visual_blocks():
            return None
        if self._image_slot_count >= self.config.visual_max_image_slots_per_turn:
            return None
        if not (self._firecrawl.available or self._image_search.available):
            return None

        visible_text = _safe_text(self._parser.visible_text)
        if len(visible_text) < 180:
            return None

        source_infos = list(self._sources_by_url.values())
        source_titles = [
            _normalize_image_search_query(source.get("title"))
            for source in source_infos[:3]
            if isinstance(source, dict)
            and _normalize_image_search_query(source.get("title"))
        ]
        explicit_image_request = _text_explicitly_requests_image(self.user_query)
        specificity = max(
            _query_specificity_score(self.user_query),
            _query_specificity_score(" ".join(source_titles[:2])),
            _query_specificity_score(_clip_text(visible_text, limit=260)),
        )
        if not explicit_image_request and not source_infos:
            return None
        if not explicit_image_request and specificity < 3:
            return None

        query = _normalize_image_search_query(self.user_query)
        if _looks_generic_for_image_search(query):
            query = source_titles[0] if source_titles else query
        if not query and source_titles:
            query = source_titles[0]
        if not query:
            query = _normalize_image_search_query(_clip_text(visible_text, limit=160))
        if not query:
            return None

        source_urls = [
            _safe_text(source.get("url"))
            for source in source_infos[: self.config.visual_image_source_page_limit]
            if isinstance(source, dict) and _safe_text(source.get("url"))
        ]
        return VisualSlotDirective(
            id=f"img_auto_{uuid4().hex[:10]}",
            kind="image",
            query=query,
            caption=None,
            loading_label="Finding a relevant image",
            source_urls=source_urls,
            context_excerpt=visible_text[-1200:].strip(),
        )

    def _maybe_schedule_implicit_image_slot(self) -> bool:
        slot = self._build_implicit_image_slot()
        if slot is None:
            return False
        self._parser.append_slot(slot)
        self._register_slots([slot])
        logger.info(
            "visual_enrichment.auto_slot_injected slot_id=%s query=%s source_count=%s",
            slot.id,
            slot.query,
            len(slot.source_urls),
        )
        return True

    async def finalize(
        self,
        *,
        produced_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        _, dirty = self._parser.finalize_pending_text()
        if dirty:
            events.append(self._build_snapshot_event())
        if self._maybe_schedule_implicit_image_slot():
            events.append(self._build_snapshot_event())

        deadline = asyncio.get_running_loop().time() + max(
            0.0,
            self._finalization_wait_timeout_sec(),
        )
        while self._active_sidecars:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                update = await asyncio.wait_for(self._event_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if self._apply_sidecar_update(update):
                events.append(self._build_snapshot_event())

        for task in list(self._active_sidecars.values()):
            task.cancel()
        if self._active_sidecars:
            await asyncio.gather(*self._active_sidecars.values(), return_exceptions=True)
        self._active_sidecars.clear()
        events.extend(self._drain_ready_updates())

        if self._parser.fail_all_pending_slots(
            image_label=_TIMED_OUT_INLINE_IMAGE_LABEL,
            chart_label=_TIMED_OUT_INLINE_CHART_LABEL,
        ):
            events.append(self._build_snapshot_event())

        final_blocks = self._parser.export_blocks()
        deliverable_blocks = build_response_blocks(
            content=None,
            produced_artifacts=produced_artifacts or [],
        )
        if deliverable_blocks:
            final_blocks.extend(deliverable_blocks)

        return {
            "events": events,
            "content": self._parser.visible_text,
            "response_blocks": final_blocks,
            "supporting_artifacts": copy.deepcopy(self._supporting_artifacts),
        }

    def _register_slots(self, slots: list[VisualSlotDirective]) -> bool:
        dirty = False
        for slot in slots:
            if slot.kind == "image":
                if self._image_slot_count >= self.config.visual_max_image_slots_per_turn:
                    dirty = self._parser.drop_slot(slot.id) or dirty
                    continue
                self._image_slot_count += 1
            elif slot.kind == "chart":
                if self._chart_slot_count >= self.config.visual_max_chart_slots_per_turn:
                    dirty = self._parser.drop_slot(slot.id) or dirty
                    continue
                self._chart_slot_count += 1
            self._slots[slot.id] = slot
            self._queue_or_start_slot(slot)
        return dirty

    def _queue_or_start_slot(self, slot: VisualSlotDirective) -> None:
        if len(self._active_sidecars) >= self.config.visual_max_concurrent_sidecars:
            self._queued_slot_ids.append(slot.id)
            return
        if slot.kind == "chart":
            self._start_chart_sidecar(slot)
            return
        candidate_sources = self._candidate_source_infos(slot)
        if candidate_sources:
            self._start_image_sidecar(slot)
            return
        self._waiting_for_sources.add(slot.id)

    def _schedule_waiting_image_slots(self) -> None:
        pending_ids = [slot_id for slot_id in list(self._waiting_for_sources) if self._candidate_source_infos(self._slots.get(slot_id))]
        for slot_id in pending_ids:
            if len(self._active_sidecars) >= self.config.visual_max_concurrent_sidecars:
                self._queued_slot_ids.append(slot_id)
                continue
            self._waiting_for_sources.discard(slot_id)
            slot = self._slots.get(slot_id)
            if slot is not None:
                self._start_image_sidecar(slot)
        self._drain_slot_queue()

    def _drain_slot_queue(self) -> None:
        if not self._queued_slot_ids:
            return
        remaining: list[str] = []
        for slot_id in self._queued_slot_ids:
            if len(self._active_sidecars) >= self.config.visual_max_concurrent_sidecars:
                remaining.append(slot_id)
                continue
            slot = self._slots.get(slot_id)
            if slot is None:
                continue
            if slot.kind == "image" and not self._candidate_source_infos(slot):
                self._waiting_for_sources.add(slot.id)
                continue
            if slot.kind == "chart":
                self._start_chart_sidecar(slot)
            else:
                self._start_image_sidecar(slot)
        self._queued_slot_ids = remaining

    def _start_chart_sidecar(self, slot: VisualSlotDirective) -> None:
        self._active_sidecars[slot.id] = asyncio.create_task(
            self._run_chart_sidecar(slot),
            name=f"visual-chart-{slot.id}",
        )

    def _start_image_sidecar(self, slot: VisualSlotDirective) -> None:
        self._active_sidecars[slot.id] = asyncio.create_task(
            self._run_image_sidecar(slot),
            name=f"visual-image-{slot.id}",
        )

    def _selection_reason_for_candidate(
        self,
        *,
        candidate: ImageCandidate,
        explicit_fallback: bool,
    ) -> str:
        if candidate.retrieval_kind == "image_search":
            if explicit_fallback:
                return (
                    "Best available direct image-search result selected because the user "
                    "explicitly requested inline imagery, even though it did not meet the "
                    "normal confidence threshold."
                )
            return "Best metadata-ranked image from direct image-search results."
        if explicit_fallback:
            return (
                "Best available trusted-source image selected because the user "
                "explicitly requested inline imagery, even though it did not meet "
                "the normal confidence threshold."
            )
        return "Best metadata-ranked image from a trusted source page."

    async def _build_attempt_candidates(
        self,
        *,
        slot: VisualSlotDirective,
        candidates: list[ImageCandidate],
    ) -> list[tuple[ImageCandidate, dict[str, Any]]]:
        ranked = sorted(
            candidates,
            key=lambda item: item.score,
            reverse=True,
        )[: self.config.visual_image_candidate_limit]
        if not ranked:
            return []

        explicit_image_request = self._slot_explicitly_requests_image(slot)
        attempt_candidates: list[tuple[ImageCandidate, dict[str, Any]]] = []
        planned_urls: set[str] = set()
        verifier_rejected_urls: set[str] = set()

        def add_attempt(candidate: ImageCandidate, verdict: dict[str, Any]) -> None:
            if candidate.image_url in planned_urls:
                return
            planned_urls.add(candidate.image_url)
            attempt_candidates.append((candidate, dict(verdict)))

        top_k = min(
            max(
                self.config.visual_image_verify_top_k,
                2 if explicit_image_request and any(
                    item.retrieval_kind == "image_search" for item in ranked
                ) else 1,
            ),
            len(ranked),
        )
        if self._fireworks.available and top_k > 0:
            for candidate in ranked[:top_k]:
                try:
                    verdict = await self._fireworks.verify_image_candidate(
                        slot_query=_safe_text(slot.query) or self.user_query,
                        user_query=self.user_query,
                        context_excerpt=slot.context_excerpt,
                        source_url=candidate.source_url,
                        source_title=candidate.source_title,
                        source_domain=candidate.source_domain,
                        candidate_image_url=candidate.image_url,
                        candidate_alt_text=candidate.alt_text,
                        candidate_title=candidate.title,
                        candidate_nearby_text=candidate.nearby_text,
                    )
                except Exception as exc:
                    logger.warning("visual_enrichment.image_verify_failed slot_id=%s error=%s", slot.id, exc)
                    verdict = {}
                confidence = max(
                    candidate.score,
                    _parse_float(verdict.get("confidence"), default=candidate.score),
                )
                if bool(verdict.get("accept")) and confidence >= self.config.visual_image_min_confidence:
                    enriched_verdict = dict(verdict)
                    enriched_verdict["confidence"] = confidence
                    add_attempt(candidate, enriched_verdict)
                elif verdict:
                    verifier_rejected_urls.add(candidate.image_url)

        relaxed_fallback_logged = False
        for candidate in ranked:
            if candidate.image_url in verifier_rejected_urls:
                continue
            if candidate.score >= self.config.visual_image_min_confidence:
                add_attempt(
                    candidate,
                    {
                        "confidence": candidate.score,
                        "selection_reason": self._selection_reason_for_candidate(
                            candidate=candidate,
                            explicit_fallback=False,
                        ),
                        "alt_text": candidate.alt_text,
                        "caption": slot.caption or "",
                    },
                )
                continue
            if self._should_allow_explicit_request_fallback(slot, candidate):
                if not relaxed_fallback_logged:
                    logger.info(
                        "visual_enrichment.image_relaxed_fallback slot_id=%s score=%.3f source=%s kind=%s",
                        slot.id,
                        candidate.score,
                        candidate.source_url,
                        candidate.retrieval_kind,
                    )
                    relaxed_fallback_logged = True
                add_attempt(
                    candidate,
                    {
                        "confidence": candidate.score,
                        "selection_reason": self._selection_reason_for_candidate(
                            candidate=candidate,
                            explicit_fallback=True,
                        ),
                        "alt_text": candidate.alt_text,
                        "caption": slot.caption or "",
                    },
                )
        return attempt_candidates

    def _should_use_image_search_fallback(
        self,
        slot: VisualSlotDirective,
        trusted_candidates: list[ImageCandidate],
    ) -> bool:
        if not self._image_search.available:
            return False
        if not self._build_image_search_queries(slot):
            return False
        if not trusted_candidates:
            return True
        top_candidate = max(trusted_candidates, key=lambda item: item.score)
        query_tokens = _tokenize(" ".join(filter(None, [self.user_query, slot.query])))
        candidate_tokens = _tokenize(
            " ".join(
                filter(
                    None,
                    [
                        top_candidate.alt_text,
                        top_candidate.title,
                        top_candidate.nearby_text,
                        top_candidate.filename,
                    ],
                )
            )
        )
        candidate_overlap = len(query_tokens & candidate_tokens)
        if top_candidate.score < max(self.config.visual_image_min_confidence + 0.08, 0.72):
            return True
        if query_tokens and candidate_overlap == 0:
            return True
        if _is_probably_cross_promo(
            top_candidate.image_url,
            top_candidate.filename,
            " ".join(filter(None, [top_candidate.alt_text, top_candidate.title, top_candidate.nearby_text])),
        ):
            return True
        return False

    def _build_image_search_queries(self, slot: VisualSlotDirective) -> list[str]:
        source_infos = self._candidate_source_infos(slot)
        source_titles = [
            _normalize_image_search_query(source.get("title"))
            for source in source_infos[:3]
            if isinstance(source, dict) and _normalize_image_search_query(source.get("title"))
        ]
        base_query = _normalize_image_search_query(_safe_text(slot.query) or self.user_query)
        context_hint = _normalize_image_search_query(_clip_text(slot.context_excerpt, limit=220))

        ordered_raw_queries: list[str] = []
        if _looks_generic_for_image_search(base_query):
            if source_titles:
                ordered_raw_queries.append(source_titles[0])
                if base_query:
                    ordered_raw_queries.append(f"{source_titles[0]} {base_query}")
            if base_query:
                ordered_raw_queries.append(base_query)
        else:
            if base_query:
                ordered_raw_queries.append(base_query)
            if source_titles:
                ordered_raw_queries.append(f"{base_query} {source_titles[0]}")
                ordered_raw_queries.append(source_titles[0])

        if len(source_titles) > 1:
            ordered_raw_queries.append(" ".join(source_titles[:2]))
        if context_hint and _query_specificity_score(context_hint) >= 4:
            if source_titles:
                ordered_raw_queries.append(f"{source_titles[0]} {context_hint}")
            ordered_raw_queries.append(context_hint)

        queries: list[str] = []
        seen_queries: set[str] = set()
        for raw_query in ordered_raw_queries:
            normalized = _normalize_image_search_query(raw_query)
            if not normalized or len(_tokenize(normalized)) < 2:
                continue
            key = normalized.lower()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(normalized)
        return queries[:4]

    async def _search_image_candidates(self, slot: VisualSlotDirective) -> list[ImageCandidate]:
        queries = self._build_image_search_queries(slot)
        if not queries:
            return []

        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()
        logger.info("visual_enrichment.image_search_queries slot_id=%s queries=%s", slot.id, queries)

        for query_index, query in enumerate(queries, start=1):
            raw_results = await self._image_search.search_images(query)
            for index, item in enumerate(raw_results, start=1):
                if not isinstance(item, dict):
                    continue
                image_url = _safe_text(item.get("image_url"))
                if not image_url.startswith(("http://", "https://")) or image_url in seen_urls:
                    continue
                seen_urls.add(image_url)
                source_url = _safe_text(item.get("source_url")) or image_url
                source_domain = _safe_text(item.get("source_domain")) or _safe_text(urlparse(source_url).netloc)
                title = _safe_text(item.get("title"))
                nearby_text = _safe_text(item.get("snippet") or item.get("nearby_text"))
                width = item.get("width") if isinstance(item.get("width"), int) else None
                height = item.get("height") if isinstance(item.get("height"), int) else None
                candidate = ImageCandidate(
                    image_url=image_url,
                    source_url=source_url,
                    source_title=title or source_domain,
                    source_domain=source_domain,
                    source_rank=((query_index - 1) * self.config.visual_image_search_result_limit) + index,
                    alt_text=title,
                    title=title,
                    nearby_text=nearby_text,
                    filename=_guess_filename_from_url(image_url, default_prefix=slot.id),
                    width=width,
                    height=height,
                    retrieval_kind="image_search",
                )
                prefilter_corpus = " ".join(
                    filter(
                        None,
                        [
                            candidate.alt_text,
                            candidate.title,
                            candidate.nearby_text,
                            candidate.filename,
                            source_domain,
                        ],
                    )
                )
                if candidate.image_url.lower().endswith(".svg"):
                    continue
                if _is_probably_ui_asset(candidate.image_url, prefilter_corpus):
                    continue
                if _is_probably_decorative(candidate.image_url, prefilter_corpus):
                    continue
                if _is_low_information_image_size(candidate.width, candidate.height):
                    continue
                candidate.score = self._score_candidate(slot, candidate, prefilter_corpus)
                if candidate.score <= 0:
                    continue
                candidates.append(candidate)
        return candidates

    async def _run_chart_sidecar(self, slot: VisualSlotDirective) -> None:
        try:
            spec = normalize_chart_spec(
                slot.chart_spec or {},
                max_points=self.config.visual_chart_max_points,
            )
            png_bytes = await asyncio.to_thread(render_chart_png, spec)
            if len(png_bytes) > self.config.visual_chart_max_bytes:
                raise ValueError("Rendered chart exceeded the image byte budget.")
            artifact = self._write_image_artifact_bytes(
                image_bytes=png_bytes,
                slot=slot,
                image_url="generated://chart",
                source_url="",
                source_title="Generated inline chart",
                source_domain="",
                caption=_safe_text(spec.get("caption")) or _safe_text(slot.caption),
                filename_hint=f"{slot.id}.png",
                default_kind="chart",
            )
            block = self._build_image_artifact_block(
                slot=slot,
                artifact=artifact,
                kind="chart",
                caption=_safe_text(spec.get("caption")) or _safe_text(slot.caption),
                provenance={
                    "source_title": "Generated inline chart",
                    "attribution_label": "Generated chart",
                    "selection_reason": "Generated from structured numeric data in the assistant response.",
                    "confidence": 1.0,
                },
            )
            await self._event_queue.put(
                {
                    "action": "replace_slot",
                    "slot_id": slot.id,
                    "artifact": artifact,
                    "block": block,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("visual_enrichment.chart_failed slot_id=%s error=%s", slot.id, exc)
            await self._event_queue.put({"action": "fail_slot", "slot_id": slot.id, "error": str(exc)})
        finally:
            self._active_sidecars.pop(slot.id, None)
            self._drain_slot_queue()

    async def _run_image_sidecar(self, slot: VisualSlotDirective) -> None:
        try:
            image_search_allowed = self._image_search.available and bool(
                self._build_image_search_queries(slot)
            )
            if not self._firecrawl.available and not image_search_allowed:
                raise ValueError(
                    "Image enrichment is unavailable because neither trusted-source scraping nor direct image search is configured."
                )
            source_infos = self._candidate_source_infos(slot)
            if not source_infos and not image_search_allowed:
                raise ValueError("No trusted source URLs were available for this image slot.")

            trusted_candidates: list[ImageCandidate] = []
            seen_urls: set[str] = set()
            if self._firecrawl.available:
                for source_rank, source in enumerate(source_infos[: self.config.visual_image_source_page_limit], start=1):
                    scraped = await self._firecrawl.scrape_images(source["url"])
                    source_title = _safe_text(scraped.get("metadata", {}).get("title")) if isinstance(scraped.get("metadata"), dict) else ""
                    for candidate in self._extract_candidates_from_scrape(
                        slot=slot,
                        source_url=source["url"],
                        source_title=source_title or _safe_text(source.get("title")),
                        source_domain=_safe_text(source.get("domain")) or _safe_text(urlparse(source["url"]).netloc),
                        source_rank=source_rank,
                        raw_images=scraped.get("images"),
                    ):
                        if candidate.image_url in seen_urls:
                            continue
                        seen_urls.add(candidate.image_url)
                        trusted_candidates.append(candidate)

            search_candidates: list[ImageCandidate] = []
            if self._should_use_image_search_fallback(slot, trusted_candidates):
                try:
                    search_candidates = await self._search_image_candidates(slot)
                except Exception as exc:
                    logger.warning("visual_enrichment.image_search_failed slot_id=%s error=%s", slot.id, exc)

            all_candidates = list(trusted_candidates)
            all_candidates.extend(
                candidate
                for candidate in search_candidates
                if candidate.image_url not in seen_urls
            )

            if not all_candidates:
                raise ValueError(
                    "No usable image candidates were found on the trusted source pages or direct image search."
                )

            attempt_candidates = await self._build_attempt_candidates(
                slot=slot,
                candidates=all_candidates,
            )

            if not attempt_candidates:
                raise ValueError("No image candidate passed the confidence threshold.")

            selected_candidate: ImageCandidate | None = None
            selected_verdict: dict[str, Any] | None = None
            last_attempt_error: Exception | None = None
            image_bytes: bytes | None = None
            detected_mime: str | None = None
            width: int | None = None
            height: int | None = None
            for candidate, verdict in attempt_candidates:
                try:
                    image_bytes, detected_mime, width, height = await self._download_image_bytes(
                        candidate.image_url
                    )
                    if _is_low_information_image_size(width, height):
                        raise ValueError(
                            f"Downloaded image was too small to be useful ({width}x{height})."
                        )
                except Exception as exc:
                    last_attempt_error = exc
                    logger.warning(
                        "visual_enrichment.image_candidate_failed slot_id=%s image_url=%s error=%s",
                        slot.id,
                        candidate.image_url,
                        exc,
                    )
                    continue
                selected_candidate = candidate
                selected_verdict = verdict
                break

            if selected_candidate is None or selected_verdict is None or image_bytes is None:
                if last_attempt_error is not None:
                    raise ValueError(f"All image candidates failed: {last_attempt_error}") from last_attempt_error
                raise ValueError("No image candidate passed the confidence threshold.")

            artifact = self._write_image_artifact_bytes(
                image_bytes=image_bytes,
                slot=slot,
                image_url=selected_candidate.image_url,
                source_url=selected_candidate.source_url,
                source_title=selected_candidate.source_title,
                source_domain=selected_candidate.source_domain,
                caption=_safe_text(selected_verdict.get("caption")) or _safe_text(slot.caption),
                filename_hint=selected_candidate.filename or _guess_filename_from_url(selected_candidate.image_url, default_prefix=slot.id),
                default_kind="reference_image",
                mime_override=detected_mime,
                width=width,
                height=height,
            )
            block = self._build_image_artifact_block(
                slot=slot,
                artifact=artifact,
                kind="reference_image",
                caption=_safe_text(selected_verdict.get("caption")) or _safe_text(slot.caption),
                provenance={
                    "source_url": selected_candidate.source_url,
                    "source_title": selected_candidate.source_title,
                    "source_domain": selected_candidate.source_domain,
                    "source_image_url": selected_candidate.image_url,
                    "attribution_label": f"Image from {selected_candidate.source_title or selected_candidate.source_domain}",
                    "selection_reason": _safe_text(selected_verdict.get("selection_reason"))
                    or "Best match from a trusted source page.",
                    "confidence": max(
                        selected_candidate.score,
                        _parse_float(selected_verdict.get("confidence"), default=selected_candidate.score),
                    ),
                    "alt_text": _safe_text(selected_verdict.get("alt_text")) or selected_candidate.alt_text,
                },
            )
            await self._event_queue.put(
                {
                    "action": "replace_slot",
                    "slot_id": slot.id,
                    "artifact": artifact,
                    "block": block,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("visual_enrichment.image_failed slot_id=%s error=%s", slot.id, exc)
            await self._event_queue.put({"action": "fail_slot", "slot_id": slot.id, "error": str(exc)})
        finally:
            self._active_sidecars.pop(slot.id, None)
            self._drain_slot_queue()

    def _extract_candidates_from_scrape(
        self,
        *,
        slot: VisualSlotDirective,
        source_url: str,
        source_title: str,
        source_domain: str,
        source_rank: int,
        raw_images: Any,
    ) -> list[ImageCandidate]:
        items = raw_images if isinstance(raw_images, list) else []
        candidates: list[ImageCandidate] = []
        for item in items:
            if isinstance(item, str):
                image_url = _safe_text(item)
                alt_text = ""
                title = ""
                nearby_text = ""
                width = None
                height = None
            elif isinstance(item, dict):
                image_url = _safe_text(
                    item.get("src")
                    or item.get("url")
                    or item.get("image_url")
                    or item.get("imageUrl")
                )
                alt_text = _safe_text(item.get("alt") or item.get("alt_text") or item.get("altText"))
                title = _safe_text(item.get("title"))
                nearby_text = _safe_text(
                    item.get("caption")
                    or item.get("description")
                    or item.get("context")
                    or item.get("nearby_text")
                    or item.get("source_text")
                )
                width = int(item.get("width")) if str(item.get("width") or "").isdigit() else None
                height = int(item.get("height")) if str(item.get("height") or "").isdigit() else None
            else:
                continue

            if not image_url:
                continue
            image_url = _normalize_scraped_image_url(source_url, image_url)
            if not image_url.startswith(("http://", "https://")):
                continue
            corpus = " ".join(filter(None, [alt_text, title, nearby_text]))
            candidate = ImageCandidate(
                image_url=image_url,
                source_url=source_url,
                source_title=source_title,
                source_domain=source_domain,
                source_rank=source_rank,
                alt_text=alt_text,
                title=title,
                nearby_text=nearby_text,
                filename=_guess_filename_from_url(image_url, default_prefix=slot.id),
                width=width,
                height=height,
            )
            prefilter_corpus = " ".join(
                filter(
                    None,
                    [
                        alt_text,
                        title,
                        nearby_text,
                        source_title,
                        source_domain,
                    ],
                )
            )
            if candidate.image_url.lower().endswith(".svg"):
                continue
            if _is_probably_ui_asset(candidate.image_url, prefilter_corpus):
                continue
            if _is_probably_decorative(candidate.image_url, prefilter_corpus):
                continue
            if _is_low_information_image_size(candidate.width, candidate.height):
                continue
            candidate.score = self._score_candidate(slot, candidate, corpus)
            if candidate.score <= 0:
                continue
            candidates.append(candidate)
        return candidates

    def _score_candidate(self, slot: VisualSlotDirective, candidate: ImageCandidate, corpus: str) -> float:
        query_tokens = _tokenize(
            " ".join(
                filter(
                    None,
                    [
                        self.user_query,
                        slot.query,
                        _clip_text(slot.context_excerpt, limit=500),
                    ],
                )
            )
        )
        candidate_tokens = _tokenize(
            " ".join(
                filter(
                    None,
                    [
                        candidate.alt_text,
                        candidate.title,
                        candidate.nearby_text,
                        candidate.filename,
                    ],
                )
            )
        )
        source_tokens = _tokenize(
            " ".join(
                filter(
                    None,
                    [
                        candidate.source_title,
                        candidate.source_domain,
                    ],
                )
            )
        )
        candidate_overlap = len(query_tokens & candidate_tokens)
        source_overlap = len(query_tokens & source_tokens)
        score = 0.16
        rank_step = 0.06 if candidate.retrieval_kind == "image_search" else 0.08
        rank_cap = 0.38 if candidate.retrieval_kind == "image_search" else 0.42
        score += max(0.0, rank_cap - ((candidate.source_rank - 1) * rank_step))
        if query_tokens:
            token_window = max(min(len(query_tokens), 6), 1)
            score += min(0.48, candidate_overlap / token_window)
            score += min(0.18, source_overlap / token_window)
        if candidate.retrieval_kind == "image_search":
            score += 0.05
        if candidate.width and candidate.height:
            pixels = candidate.width * candidate.height
            if pixels >= 200_000:
                score += 0.15
            if 0.55 <= (candidate.width / max(candidate.height, 1)) <= 2.2:
                score += 0.1
        if candidate.alt_text and len(candidate.alt_text.split()) >= 3:
            score += 0.1
        score -= _dimension_quality_penalty(candidate.width, candidate.height)
        if query_tokens and source_overlap > 0 and candidate_overlap == 0:
            score -= 0.24
        if _is_probably_text_art(
            candidate.image_url,
            candidate.filename,
            " ".join(filter(None, [candidate.alt_text, candidate.title, candidate.nearby_text])),
        ):
            score -= 0.35
        if _is_probably_cross_promo(
            candidate.image_url,
            candidate.filename,
            " ".join(filter(None, [candidate.alt_text, candidate.title, candidate.nearby_text])),
        ):
            score -= 0.40
        if _is_probably_decorative(candidate.image_url, corpus):
            score -= 0.45
        if candidate.image_url.lower().endswith(".svg"):
            score -= 0.3
        return max(0.0, min(1.0, score))

    def _slot_explicitly_requests_image(self, slot: VisualSlotDirective) -> bool:
        return any(
            _text_explicitly_requests_image(text)
            for text in (
                self.user_query,
                slot.query,
            )
        )

    def _should_allow_explicit_request_fallback(
        self,
        slot: VisualSlotDirective,
        candidate: ImageCandidate,
    ) -> bool:
        if not self._slot_explicitly_requests_image(slot):
            return False
        corpus = " ".join(
            filter(
                None,
                [
                    candidate.alt_text,
                    candidate.title,
                    candidate.nearby_text,
                    candidate.filename,
                    candidate.source_title,
                    candidate.source_domain,
                ],
            )
        )
        if _is_probably_decorative(candidate.image_url, corpus):
            return False
        if candidate.image_url.lower().endswith(".svg"):
            return False
        if _is_probably_cross_promo(candidate.image_url, candidate.filename, corpus):
            return False
        relaxed_floor = max(0.50, self.config.visual_image_min_confidence - 0.08)
        return candidate.score >= relaxed_floor

    async def _download_image_bytes(
        self,
        image_url: str,
    ) -> tuple[bytes, str | None, int | None, int | None]:
        response = await self._http_client.get(
            image_url,
            headers={"User-Agent": "COSMIC-OS/1.0"},
            follow_redirects=True,
            timeout=httpx.Timeout(
                self.config.visual_download_timeout_sec,
                connect=min(self.config.visual_download_timeout_sec, 10.0),
            ),
        )
        if response.status_code >= 400:
            raise ValueError(f"Image download failed with status {response.status_code}.")
        data = response.content
        if not data:
            raise ValueError("Image download returned empty bytes.")
        if len(data) > self.config.visual_image_max_bytes:
            raise ValueError("Image download exceeded the byte budget.")
        mime_type = _safe_text(response.headers.get("content-type")).split(";")[0] or None
        width: int | None = None
        height: int | None = None
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if not mime_type:
                    mime_type = Image.MIME.get(image.format or "", None)
        except Exception:
            pass
        return data, mime_type, width, height

    def _write_image_artifact_bytes(
        self,
        *,
        image_bytes: bytes,
        slot: VisualSlotDirective,
        image_url: str,
        source_url: str,
        source_title: str,
        source_domain: str,
        caption: str,
        filename_hint: str,
        default_kind: str,
        mime_override: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        artifact_id = f"art_visual_{uuid4().hex[:16]}"
        extension = mimetypes.guess_extension(mime_override or "") or Path(filename_hint).suffix or ".png"
        filename_base = Path(filename_hint).stem or slot.id
        filename = _slugify_filename(f"{filename_base}{extension}", fallback=f"{slot.id}{extension}")
        output_dir = self.config.artifacts_root / self.task_id / "visual_enrichment"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename
        output_path.write_bytes(image_bytes)
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        return {
            "artifact_id": artifact_id,
            "task_id": self.task_id,
            "mime": mime_override or mimetypes.guess_type(filename)[0] or "image/png",
            "mime_type": mime_override or mimetypes.guess_type(filename)[0] or "image/png",
            "path": str(output_path),
            "filename": filename,
            "kind": default_kind,
            "audience": "supporting",
            "downloadable": False,
            "created_by_agent": self.config.orchestrator_agent_id,
            "created_at": _utcnow_iso(),
            "sha256": sha256,
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "caption": caption or None,
            "source_url": source_url or None,
            "source_title": source_title or None,
            "source_domain": source_domain or None,
            "source_image_url": image_url or None,
        }

    def _build_image_artifact_block(
        self,
        *,
        slot: VisualSlotDirective,
        artifact: dict[str, Any],
        kind: str,
        caption: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        block = {
            "id": slot.id,
            "type": "image_artifact",
            "artifact_id": _safe_text(artifact.get("artifact_id")),
            "filename": _safe_text(artifact.get("filename")) or f"{slot.id}.png",
            "mime_type": _safe_text(artifact.get("mime_type") or artifact.get("mime")) or "image/png",
            "size_bytes": artifact.get("size_bytes"),
            "kind": kind,
            "downloadable": False,
            "caption": caption or None,
            "provenance": {
                key: value
                for key, value in provenance.items()
                if value not in (None, "", [], {})
            },
        }
        return {
            key: value
            for key, value in block.items()
            if value not in (None, "", [], {})
        }

    def _build_snapshot_event(self) -> dict[str, Any]:
        self._snapshot_seq += 1
        blocks = self._parser.export_blocks()
        event: dict[str, Any] = {
            "type": "response.blocks.snapshot",
            "snapshot_seq": self._snapshot_seq,
            "response_blocks": blocks,
            "blocks": blocks,
        }
        if self._supporting_artifacts:
            event["supporting_artifacts"] = copy.deepcopy(self._supporting_artifacts)
        return event

    def _drain_ready_updates(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                update = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self._apply_sidecar_update(update):
                events.append(self._build_snapshot_event())
        return events

    def _apply_sidecar_update(self, update: dict[str, Any]) -> bool:
        action = _safe_text(update.get("action"))
        slot_id = _safe_text(update.get("slot_id"))
        if not slot_id:
            return False
        if action == "replace_slot":
            artifact = update.get("artifact")
            if isinstance(artifact, dict):
                artifact_id = _safe_text(artifact.get("artifact_id"))
                if artifact_id and artifact_id not in self._supporting_artifact_ids:
                    self._supporting_artifact_ids.add(artifact_id)
                    self._supporting_artifacts.append(dict(artifact))
            block = update.get("block")
            if isinstance(block, dict):
                return self._parser.replace_slot(slot_id, dict(block))
            return False
        if action == "fail_slot":
            slot = self._slots.get(slot_id)
            detail = _clip_text(update.get("error"), limit=240) or None
            return self._parser.fail_slot(
                slot_id,
                label=self._failure_label_for_slot(slot, timed_out=False),
                detail=detail,
            )
        if action == "drop_slot":
            slot = self._slots.get(slot_id)
            detail = _clip_text(update.get("error"), limit=240) or None
            return self._parser.fail_slot(
                slot_id,
                label=self._failure_label_for_slot(slot, timed_out=False),
                detail=detail,
            )
        return False

    def _candidate_source_infos(self, slot: VisualSlotDirective | None) -> list[dict[str, str]]:
        if slot is None:
            return []
        ordered: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for raw_url in slot.source_urls:
            url = _safe_text(raw_url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            source = self._sources_by_url.get(url) or {
                "url": url,
                "title": _safe_text(urlparse(url).netloc),
                "domain": _safe_text(urlparse(url).netloc),
            }
            ordered.append(dict(source))
        for source in self._sources_by_url.values():
            url = _safe_text(source.get("url"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            ordered.append(dict(source))
        return ordered
