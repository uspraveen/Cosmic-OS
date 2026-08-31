from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
    "image",
    "images",
    "include",
    "inline",
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
# Images the run itself captured are addressed by this scheme so they travel the
# same candidate/download/artifact path as anything pulled off the web.
_RUN_CAPTURE_SCHEME = "cosmic-run://"
_RUN_CAPTURE_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/webp")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _slugify_filename(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def _parse_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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


# Function words carry no topical signal, but the old scorer counted them, so any
# two English sentences overlapped enough to saturate the relevance term. A forum
# post titled "If I haven't heard back from Bain consultant interview..." scored a
# perfect 1.0 against "We still haven't heard back!" on `heard/back/they/that/the`
# alone, and shipped inline under an answer about YC's Fall 2026 batch.
_STOPWORDS = frozenset(
    """
    about above after again against all also am an and any are aren as at be because been
    before being below between both but by can cant could couldnt did didnt do does doesnt
    doing dont down during each few for from further had hadnt has hasnt have havent having
    he her here hers herself him himself his how however i if in into is isnt it its itself
    just me might more most must my myself no nor not now of off on once only or other ought
    our ours ourselves out over own same shall she should shouldnt so some such than that
    the their theirs them themselves then there these they this those through to too under
    until up very was wasnt we were werent what when where which while who whom why will
    with wont would wouldnt you your yours yourself yourselves
    already back get got heard hear know let like make new one still take tell thing things
    want way well
    """.split()
)


def _content_tokens(value: Any) -> set[str]:
    """Topical tokens only: stopwords stripped, single characters dropped."""
    return {token for token in _tokenize(value) if token not in _STOPWORDS and len(token) >= 2}


def _distinctiveness(token: str) -> float:
    """Cheap standin for IDF.

    No corpus is available here, but the tokens that actually identify a subject
    are the long ones and the ones carrying digits ("ycombinator", "2026"),
    while short generic words match almost anything.
    """
    if any(ch.isdigit() for ch in token):
        return 1.5
    if len(token) >= 9:
        return 1.5
    if len(token) >= 6:
        return 1.2
    return 1.0


def _weighted_coverage(topic_tokens: set[str], candidate_tokens: set[str]) -> float:
    """Share of the topic's distinctiveness mass that the candidate actually matches.

    Unlike the old `overlap / min(len(query), 6)`, the denominator is the whole
    topic, so matching a couple of incidental words cannot look like a match.
    """
    if not topic_tokens:
        return 0.0
    total = sum(_distinctiveness(token) for token in topic_tokens)
    if total <= 0:
        return 0.0
    matched = sum(_distinctiveness(token) for token in topic_tokens & candidate_tokens)
    return max(0.0, min(1.0, matched / total))


def _tokenize(value: Any) -> set[str]:
    text = str(value or "").lower()
    text = re.sub(r"\bmha\b", "my hero academia", text)
    text = re.sub(r"\bofa\b", "one for all", text)
    return {match.group(0).lower() for match in _WORD_RE.finditer(text)}


def _text_explicitly_requests_image(value: Any) -> bool:
    text = _safe_text(value).lower()
    if not text:
        return False
    return any(marker in text for marker in _EXPLICIT_IMAGE_REQUEST_MARKERS)


def _normalize_image_search_query(value: Any, *, max_words: int = 18) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    text = re.sub(r"\bmha\b", "My Hero Academia", text, flags=re.IGNORECASE)
    text = re.sub(r"\bofa\b", "One For All", text, flags=re.IGNORECASE)
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
    thumbnail_url: str = ""
    alt_text: str = ""
    title: str = ""
    nearby_text: str = ""
    filename: str = ""
    width: int | None = None
    height: int | None = None
    score: float = 0.0
    # Ordering score and topical relevance are deliberately separate. They used to
    # be summed, which let a big, well-ranked, well-captioned image clear the bar
    # on structural quality alone: base + rank + search bonus = 0.59 against a
    # 0.58 threshold, before topicality was consulted at all.
    relevance: float = 0.0
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
        # Screenshots and images produced by this run's own tools. The answer was
        # frequently written *from* one of these, which makes it a better
        # illustration than anything an open-web image search can find.
        self._run_images: dict[str, dict[str, Any]] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._active_sidecars: dict[str, asyncio.Task[None]] = {}
        self._slot_deadlines: dict[str, float] = {}
        self._response_open = True
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
        # Start the automatic fallback while the answer is still streaming. Its
        # network and vision work remains an independent sidecar; this only moves
        # work that used to begin in finalize() into otherwise-idle generation time.
        if self._maybe_schedule_implicit_image_slot():
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
        events: list[dict[str, Any]] = []
        if changed and self._maybe_schedule_implicit_image_slot():
            events.append(self._build_snapshot_event())
        events.extend(self._drain_ready_updates())
        return events

    def note_run_images(self, artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Register images this run's tools captured, so they can be used inline.

        Called with the same artifact payloads the runtime already collects for
        the response. Previously these were only ever carried through to the
        client as attachments: a screenshot the answer was written from was never
        a candidate for the inline image slot sitting directly above it.
        """
        changed = False
        for item in artifacts or []:
            if not isinstance(item, dict):
                continue
            mime = _safe_text(item.get("mime_type") or item.get("mime")).lower()
            path = _safe_text(item.get("path"))
            if not path or not mime.startswith("image/"):
                continue
            if mime not in _RUN_CAPTURE_MIMES:
                continue
            artifact_id = _safe_text(item.get("artifact_id")) or hashlib.sha1(
                path.encode("utf-8")
            ).hexdigest()[:16]
            if artifact_id in self._run_images:
                continue
            self._run_images[artifact_id] = dict(item)
            changed = True
        if changed:
            self._schedule_waiting_image_slots()
        events: list[dict[str, Any]] = []
        if changed and self._maybe_schedule_implicit_image_slot():
            events.append(self._build_snapshot_event())
        events.extend(self._drain_ready_updates())
        return events

    def _resolve_run_capture_path(self, artifact: dict[str, Any]) -> Path | None:
        """Resolve a tool-reported artifact path, refusing anything outside the store.

        These paths arrive in tool output, and whatever comes back here is read
        off disk and published inline. Resolution is therefore constrained to the
        artifacts root: a payload naming some other file on the box does not get
        to become an image in the answer.
        """
        raw = _safe_text(artifact.get("path"))
        if not raw:
            return None
        try:
            root = Path(self.config.artifacts_root).resolve()
        except OSError:
            return None

        raw_path = Path(raw)
        attempts: list[Path] = []
        if raw_path.is_absolute():
            attempts.append(raw_path)
        else:
            parts = raw_path.parts
            # Tools report either "runs/artifacts/<task>/..." relative to the
            # backend root, or a path already relative to the artifacts root.
            if len(parts) >= 2 and parts[0] == "runs" and parts[1] == "artifacts":
                attempts.append(root.joinpath(*parts[2:]))
            attempts.append(root / raw_path)
            attempts.append(root.parent.parent / raw_path)

        for attempt in attempts:
            try:
                resolved = attempt.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            if not resolved.is_relative_to(root):
                logger.warning(
                    "visual_enrichment.run_capture_outside_artifacts_root path=%s",
                    resolved,
                )
                continue
            return resolved
        return None

    def _collect_run_capture_candidates(self, slot: VisualSlotDirective) -> list[ImageCandidate]:
        """Run captures of pages the answer actually cites.

        Citation is required: a screenshot of some page the run visited but did
        not draw on is no more relevant than a web result. Matching a cited page
        is what earns these candidates their provenance relevance.
        """
        if not self._run_images:
            return []
        cited_urls = {
            _safe_text(source.get("url")): source
            for source in self._candidate_source_infos(slot)
            if _safe_text(source.get("url"))
        }
        cited_domains: dict[str, dict[str, str]] = {}
        for url, source in cited_urls.items():
            domain = _safe_text(source.get("domain")) or _safe_text(urlparse(url).netloc)
            if domain:
                cited_domains.setdefault(domain.lower(), source)

        candidates: list[ImageCandidate] = []
        for rank, (artifact_id, artifact) in enumerate(self._run_images.items(), start=1):
            source_url = _safe_text(artifact.get("source_url"))
            if not source_url:
                continue
            source = cited_urls.get(source_url)
            if source is None:
                domain = _safe_text(urlparse(source_url).netloc).lower()
                source = cited_domains.get(domain)
            if source is None:
                continue
            if self._resolve_run_capture_path(artifact) is None:
                continue
            source_title = _safe_text(source.get("title")) or _safe_text(urlparse(source_url).netloc)
            candidate = ImageCandidate(
                image_url=f"{_RUN_CAPTURE_SCHEME}{artifact_id}",
                source_url=source_url,
                source_title=source_title,
                source_domain=_safe_text(source.get("domain")) or _safe_text(urlparse(source_url).netloc),
                source_rank=rank,
                alt_text=f"Screenshot of {source_title}",
                title=source_title,
                filename=_safe_text(artifact.get("filename")),
                width=_parse_int(artifact.get("width")),
                height=_parse_int(artifact.get("height")),
                retrieval_kind="run_capture",
            )
            # Each gatherer scores what it produces; ranking assumes it is done.
            candidate.score = self._score_candidate(
                slot,
                candidate,
                " ".join(filter(None, [candidate.alt_text, candidate.title])),
            )
            candidates.append(candidate)
        if candidates:
            logger.info(
                "visual_enrichment.run_capture_candidates slot_id=%s count=%s",
                slot.id,
                len(candidates),
            )
        return candidates

    def _image_min_runtime_sec(self) -> float:
        return max(0.25, int(self.config.visual_image_slot_timeout_ms) / 1000.0)

    def _slot_timeout_ms(self, slot: VisualSlotDirective | None) -> int:
        if slot is None:
            return 0
        if slot.kind == "chart":
            if slot.timeout_ms:
                return max(250, int(slot.timeout_ms))
            return max(250, int(self.config.visual_chart_slot_timeout_ms))
        return int(self._image_min_runtime_sec() * 1000)

    def _finalization_wait_timeout_sec(self) -> float:
        base_sec = max(
            0.0,
            self.config.visual_finalization_grace_ms / 1000.0,
        )
        if not self._active_sidecars:
            return base_sec
        now = time.monotonic()
        max_remaining_sec = max(
            (
                max(0.0, self._slot_deadlines.get(slot_id, now) - now)
                for slot_id in self._active_sidecars.keys()
            ),
            default=0.0,
        )
        if max_remaining_sec <= 0:
            return base_sec
        return max(base_sec, max_remaining_sec)

    def _remaining_slot_sec(self, slot_id: str) -> float:
        deadline = self._slot_deadlines.get(slot_id)
        remaining = (
            max(0.0, deadline - time.monotonic())
            if deadline is not None
            else self._image_min_runtime_sec()
        )
        if self._response_open:
            return max(remaining, self._image_min_runtime_sec())
        return remaining

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

    @staticmethod
    def _is_automatic_image_slot(slot: VisualSlotDirective) -> bool:
        return slot.kind == "image" and slot.id.startswith("img_auto_")

    async def _timed_stage(self, slot_id: str, stage: str, awaitable: Any) -> Any:
        started = time.perf_counter()
        try:
            result = await awaitable
        except asyncio.CancelledError:
            logger.warning(
                "visual_enrichment.stage_cancelled slot_id=%s stage=%s elapsed_ms=%.1f",
                slot_id,
                stage,
                (time.perf_counter() - started) * 1000.0,
            )
            raise
        except Exception as exc:
            logger.warning(
                "visual_enrichment.stage_failed slot_id=%s stage=%s elapsed_ms=%.1f error=%s",
                slot_id,
                stage,
                (time.perf_counter() - started) * 1000.0,
                exc,
            )
            raise
        logger.info(
            "visual_enrichment.stage_completed slot_id=%s stage=%s elapsed_ms=%.1f",
            slot_id,
            stage,
            (time.perf_counter() - started) * 1000.0,
        )
        return result

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
            timeout_ms=None,
            source_urls=source_urls,
            context_excerpt=visible_text[-1200:].strip(),
        )

    def _maybe_schedule_implicit_image_slot(self) -> bool:
        # This method is probed as text streams, so keep its common path O(1)
        # until enough context and provenance exist to make a real decision.
        if self._image_slot_count or self._chart_slot_count:
            return False
        if len(_safe_text(self._parser.visible_text)) < 180:
            return False
        if not _text_explicitly_requests_image(self.user_query) and not self._sources_by_url:
            return False
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
        self._response_open = False

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

        if self._active_sidecars:
            logger.warning(
                "visual_enrichment.finalization_timeout active_slots=%s timeout_sec=%.3f",
                sorted(self._active_sidecars.keys()),
                self._finalization_wait_timeout_sec(),
            )

        for task in list(self._active_sidecars.values()):
            task.cancel()
        if self._active_sidecars:
            await asyncio.gather(*self._active_sidecars.values(), return_exceptions=True)
        self._active_sidecars.clear()
        self._slot_deadlines.clear()
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
        final_blocks = self._clean_final_visual_blocks(final_blocks)

        return {
            "events": events,
            "content": self._parser.visible_text,
            "response_blocks": final_blocks,
            "supporting_artifacts": copy.deepcopy(self._supporting_artifacts),
        }

    @staticmethod
    def _clean_final_visual_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove stale automatic visual placeholders from completed responses."""
        has_image_artifact = any(
            str(block.get("type")) == "image_artifact"
            for block in blocks
            if isinstance(block, dict)
        )
        if not has_image_artifact:
            return blocks
        cleaned: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type"))
            block_id = _safe_text(block.get("id"))
            status = _safe_text(block.get("status")).lower()
            if (
                block_type == "image_slot"
                and block_id.startswith("img_auto_")
                and status == "failed"
            ):
                continue
            cleaned.append(block)
        return cleaned

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
        if candidate_sources or (
            self._image_search.available and bool(self._build_image_search_queries(slot))
        ):
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
            if (
                slot.kind == "image"
                and not self._candidate_source_infos(slot)
                and not (
                    self._image_search.available
                    and bool(self._build_image_search_queries(slot))
                )
            ):
                self._waiting_for_sources.add(slot.id)
                continue
            if slot.kind == "chart":
                self._start_chart_sidecar(slot)
            else:
                self._start_image_sidecar(slot)
        self._queued_slot_ids = remaining

    def _start_chart_sidecar(self, slot: VisualSlotDirective) -> None:
        self._slot_deadlines[slot.id] = time.monotonic() + (
            self._slot_timeout_ms(slot) / 1000.0
        )
        self._active_sidecars[slot.id] = asyncio.create_task(
            self._run_chart_sidecar(slot),
            name=f"visual-chart-{slot.id}",
        )

    def _start_image_sidecar(self, slot: VisualSlotDirective) -> None:
        self._slot_deadlines[slot.id] = time.monotonic() + self._image_min_runtime_sec()
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
        if candidate.retrieval_kind == "run_capture":
            return (
                f"Screenshot this run captured from {candidate.source_domain or 'a cited source'}, "
                "which the answer was written from."
            )
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

    async def _download_contact_sheet_preview(self, image_url: str) -> bytes:
        """Read only a tightly bounded preview, following ordinary redirects."""
        byte_budget = self.config.visual_image_contact_sheet_candidate_max_bytes
        if image_url.startswith(_RUN_CAPTURE_SCHEME):
            data, _mime, _width, _height = await asyncio.to_thread(
                self._read_run_capture_bytes, image_url
            )
            if len(data) > byte_budget:
                raise ValueError("Contact-sheet preview exceeded the byte budget.")
            return data

        # The enclosing sidecar is cancelled at its slot deadline; this per-request
        # timeout is simply the existing network ceiling.
        timeout_sec = self.config.visual_download_timeout_sec
        chunks = bytearray()
        async with self._http_client.stream(
            "GET",
            image_url,
            headers={"User-Agent": "COSMIC-OS/1.0"},
            follow_redirects=True,
            timeout=httpx.Timeout(
                timeout_sec,
                connect=min(timeout_sec, 10.0),
            ),
        ) as response:
            if response.status_code >= 400:
                raise ValueError(
                    f"Contact-sheet preview failed with status {response.status_code}."
                )
            content_length = _parse_int(response.headers.get("content-length"))
            if content_length is not None and content_length > byte_budget:
                raise ValueError("Contact-sheet preview exceeded the byte budget.")
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > byte_budget:
                    raise ValueError("Contact-sheet preview exceeded the byte budget.")
        if not chunks:
            raise ValueError("Contact-sheet preview returned empty bytes.")
        return bytes(chunks)

    @staticmethod
    def _render_contact_sheet_jpeg(
        prepared: list[tuple[int, ImageCandidate, bytes]],
    ) -> tuple[bytes, list[tuple[int, ImageCandidate]]]:
        cell_width = 240
        image_height = 150
        label_height = 34
        gap = 6
        rendered: list[tuple[int, ImageCandidate, Image.Image]] = []
        for marker, candidate, raw_bytes in prepared:
            try:
                with Image.open(BytesIO(raw_bytes)) as source:
                    width, height = source.size
                    if width <= 0 or height <= 0 or width * height > 30_000_000:
                        continue
                    source.load()
                    preview = ImageOps.contain(
                        source.convert("RGB"),
                        (cell_width, image_height),
                        method=getattr(Image, "Resampling", Image).LANCZOS,
                    )
            except Exception:
                continue
            rendered.append((marker, candidate, preview))
        if not rendered:
            raise ValueError("No contact-sheet previews could be decoded.")

        columns = min(5, len(rendered))
        rows = (len(rendered) + columns - 1) // columns
        sheet = Image.new(
            "RGB",
            (
                (columns * cell_width) + ((columns - 1) * gap),
                (rows * (image_height + label_height)) + ((rows - 1) * gap),
            ),
            (17, 18, 22),
        )
        draw = ImageDraw.Draw(sheet)
        try:
            label_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        except OSError:
            label_font = ImageFont.load_default()
        for index, (marker, _candidate, preview) in enumerate(rendered):
            column = index % columns
            row = index // columns
            x = column * (cell_width + gap)
            y = row * (image_height + label_height + gap)
            image_x = x + max(0, (cell_width - preview.width) // 2)
            image_y = y + max(0, (image_height - preview.height) // 2)
            sheet.paste(preview, (image_x, image_y))
            draw.rectangle(
                (x, y + image_height, x + cell_width - 1, y + image_height + label_height - 1),
                fill=(23, 27, 35),
            )
            draw.text(
                (x + 10, y + image_height + 3),
                f"#{marker}",
                fill=(245, 247, 250),
                font=label_font,
            )
        output = BytesIO()
        sheet.save(output, format="JPEG", quality=82, optimize=True)
        return output.getvalue(), [
            (marker, candidate) for marker, candidate, _preview in rendered
        ]

    async def _build_contact_sheet(
        self,
        ranked: list[ImageCandidate],
    ) -> tuple[
        bytes,
        list[tuple[int, ImageCandidate]],
        dict[str, bytes],
    ]:
        candidates = [
            candidate
            for candidate in ranked
            if candidate.retrieval_kind != "run_capture"
        ][: self.config.visual_image_contact_sheet_limit]
        if not candidates:
            raise ValueError("No remote candidates were available for a contact sheet.")

        semaphore = asyncio.Semaphore(4)

        async def prepare(
            marker: int,
            candidate: ImageCandidate,
        ) -> tuple[int, ImageCandidate, bytes] | None:
            preview_url = candidate.thumbnail_url or candidate.image_url
            try:
                async with semaphore:
                    preview_bytes = await self._download_contact_sheet_preview(preview_url)
            except Exception as exc:
                logger.info(
                    "visual_enrichment.contact_sheet_preview_skipped marker=%s url=%s error=%s",
                    marker,
                    preview_url,
                    exc,
                )
                return None
            return marker, candidate, preview_bytes

        prepared_results = await asyncio.gather(
            *(prepare(index, candidate) for index, candidate in enumerate(candidates, start=1))
        )
        prepared = [item for item in prepared_results if item is not None]
        if not prepared:
            raise ValueError("No candidate previews were available for a contact sheet.")
        contact_sheet, marked_candidates = await asyncio.to_thread(
            self._render_contact_sheet_jpeg, prepared
        )
        valid_urls = {candidate.image_url for _marker, candidate in marked_candidates}
        original_byte_cache = {
            candidate.image_url: preview_bytes
            for _marker, candidate, preview_bytes in prepared
            if not candidate.thumbnail_url and candidate.image_url in valid_urls
        }
        return contact_sheet, marked_candidates, original_byte_cache

    async def _build_attempt_candidates(
        self,
        *,
        slot: VisualSlotDirective,
        candidates: list[ImageCandidate],
    ) -> list[tuple[ImageCandidate, dict[str, Any]]]:
        # Scores clamp at 1.0, so ties are common. Provenance, not list order,
        # decides them: a first-party capture outranks a web result it ties with.
        ranked = sorted(
            candidates,
            key=lambda item: (item.score, self._provenance_relevance(item)),
            reverse=True,
        )[: self.config.visual_image_candidate_limit]
        if not ranked:
            return []

        explicit_image_request = self._slot_explicitly_requests_image(slot)
        attempt_candidates: list[tuple[ImageCandidate, dict[str, Any]]] = []
        planned_urls: set[str] = set()
        verifier_rejected_urls: set[str] = set()
        # A verifier that said "no" and a verifier that fell over are different
        # facts and must not share a bucket.
        verifier_errored_urls: set[str] = set()

        def add_attempt(candidate: ImageCandidate, verdict: dict[str, Any]) -> None:
            if candidate.image_url in planned_urls:
                return
            planned_urls.add(candidate.image_url)
            attempt_candidates.append((candidate, dict(verdict)))

        primary_run_capture = bool(
            ranked
            and ranked[0].retrieval_kind == "run_capture"
            and ranked[0].score >= self.config.visual_image_min_confidence
            and ranked[0].relevance >= self.config.visual_image_min_relevance
        )
        contact_sheet_completed = False
        contact_sheet_timed_out = False
        if (
            self._fireworks.available
            and self.config.visual_image_contact_sheet_enabled
            and not primary_run_capture
        ):
            ranking_budget = max(1.5, self._remaining_slot_sec(slot.id) - 2.0)
            try:
                (
                    contact_sheet,
                    marked_candidates,
                    original_byte_cache,
                ) = await asyncio.wait_for(
                    self._build_contact_sheet(ranked),
                    timeout=max(1.0, ranking_budget * 0.55),
                )
                candidate_metadata = [
                    {
                        "marker": marker,
                        "title": candidate.title or candidate.source_title,
                        "alt_text": candidate.alt_text,
                        "nearby_text": candidate.nearby_text,
                        "source_domain": candidate.source_domain,
                    }
                    for marker, candidate in marked_candidates
                ]
                sheet_verdict = await asyncio.wait_for(
                    self._fireworks.rank_image_contact_sheet(
                        slot_query=_safe_text(slot.query) or self.user_query,
                        user_query=self.user_query,
                        context_excerpt=slot.context_excerpt,
                        contact_sheet_jpeg=contact_sheet,
                        candidates=candidate_metadata,
                    ),
                    timeout=max(1.0, ranking_budget * 0.45),
                )
                contact_sheet_completed = True
                marker_map = dict(marked_candidates)
                selected_marker = _parse_int(sheet_verdict.get("selected_marker")) or 0
                ranked_markers_raw = sheet_verdict.get("ranked_markers")
                ranked_markers: list[int] = []
                if isinstance(ranked_markers_raw, list):
                    for raw_marker in ranked_markers_raw:
                        marker = _parse_int(raw_marker) or 0
                        if marker in marker_map and marker not in ranked_markers:
                            ranked_markers.append(marker)
                if selected_marker in marker_map:
                    ranked_markers = [selected_marker] + [
                        marker for marker in ranked_markers if marker != selected_marker
                    ]
                vision_confidence = _parse_float(
                    sheet_verdict.get("confidence"), default=0.0
                )
                vision_accepted = sheet_verdict.get("accept") is True and selected_marker in marker_map
                accepted_markers = ranked_markers if vision_accepted else []
                for choice_index, marker in enumerate(accepted_markers):
                    candidate = marker_map[marker]
                    enriched_verdict = dict(sheet_verdict)
                    enriched_verdict.update(
                        {
                            "confidence": max(vision_confidence, candidate.score),
                            "verified": True,
                            "relevance": round(candidate.relevance, 4),
                            "retrieval_kind": candidate.retrieval_kind,
                            "selection_reason": (
                                _safe_text(sheet_verdict.get("selection_reason"))
                                if choice_index == 0
                                else "Vision-ranked alternate from the same contact sheet."
                            ),
                            "alt_text": (
                                _safe_text(sheet_verdict.get("alt_text"))
                                if choice_index == 0
                                else candidate.alt_text
                            ),
                            "caption": (
                                _safe_text(sheet_verdict.get("caption"))
                                if choice_index == 0
                                else _safe_text(slot.caption)
                            ),
                            "_downloaded_image_bytes": original_byte_cache.get(
                                candidate.image_url
                            ),
                        }
                    )
                    add_attempt(candidate, enriched_verdict)
                if not explicit_image_request:
                    if vision_accepted and accepted_markers:
                        accepted_urls = {
                            marker_map[marker].image_url for marker in accepted_markers
                        }
                        verifier_rejected_urls.update(
                            candidate.image_url
                            for candidate in ranked
                            if candidate.retrieval_kind != "run_capture"
                            and candidate.image_url not in accepted_urls
                        )
                    elif sheet_verdict.get("accept") is False:
                        # Vision compared the sheet and said none belong. That is
                        # authoritative. A timeout or parse miss must not take
                        # this branch.
                        verifier_rejected_urls.update(
                            candidate.image_url
                            for candidate in ranked
                            if candidate.retrieval_kind != "run_capture"
                        )
                logger.info(
                    "visual_enrichment.contact_sheet_ranked slot_id=%s candidates=%s accepted=%s confidence=%.3f",
                    slot.id,
                    len(marked_candidates),
                    len(accepted_markers),
                    vision_confidence,
                )
            except asyncio.TimeoutError:
                contact_sheet_timed_out = True
                logger.warning(
                    "visual_enrichment.contact_sheet_timed_out slot_id=%s budget_sec=%.2f",
                    slot.id,
                    ranking_budget,
                )
            except Exception as exc:
                logger.warning(
                    "visual_enrichment.contact_sheet_failed slot_id=%s error=%s",
                    slot.id,
                    exc,
                )

        top_k = min(
            max(
                self.config.visual_image_verify_top_k,
                2 if explicit_image_request and any(
                    item.retrieval_kind == "image_search" for item in ranked
                ) else 1,
            ),
            len(ranked),
        )
        if (
            self._fireworks.available
            and top_k > 0
            and not contact_sheet_completed
            and not contact_sheet_timed_out
            and not primary_run_capture
        ):
            for candidate in ranked[:top_k]:
                if candidate.retrieval_kind == "run_capture":
                    # The verifier fetches candidate_image_url itself, and a run
                    # capture lives on local disk, not a URL it could fetch. Its
                    # topicality is already settled by the cited source it was
                    # taken from, so it is not shipped to a third party to be
                    # re-judged.
                    continue
                verdict: dict[str, Any] = {}
                verifier_error = False
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
                    verifier_error = True
                confidence = max(
                    candidate.score,
                    _parse_float(verdict.get("confidence"), default=candidate.score),
                )
                if bool(verdict.get("accept")) and confidence >= self.config.visual_image_min_confidence:
                    enriched_verdict = dict(verdict)
                    enriched_verdict["confidence"] = confidence
                    enriched_verdict["verified"] = True
                    enriched_verdict["relevance"] = round(candidate.relevance, 4)
                    enriched_verdict["retrieval_kind"] = candidate.retrieval_kind
                    add_attempt(candidate, enriched_verdict)
                elif not explicit_image_request:
                    # Fail closed. This previously read `elif verdict and ...`, so a
                    # verifier crash left `verdict` as an empty dict, which is falsy,
                    # which meant the candidate was never marked rejected and fell
                    # through to the lexical-score path below completely unvetted.
                    # That is exactly how the Bain forum screenshot shipped.
                    if verifier_error or not verdict:
                        verifier_errored_urls.add(candidate.image_url)
                    else:
                        verifier_rejected_urls.add(candidate.image_url)

        relaxed_fallback_logged = False
        relevance_reject_logged = False
        min_relevance = self.config.visual_image_min_relevance
        for candidate in ranked:
            if candidate.image_url in verifier_rejected_urls and not explicit_image_request:
                continue
            if (
                candidate.image_url in verifier_errored_urls
                and not explicit_image_request
                and self._provenance_relevance(candidate) < 1.0
            ):
                # An unreachable verifier must not promote an unvetted web image.
                # It also must not discard a first-party capture, whose relevance
                # never depended on the verifier's opinion.
                continue
            if not explicit_image_request and candidate.relevance < min_relevance:
                if not relevance_reject_logged:
                    logger.info(
                        "visual_enrichment.image_relevance_rejected slot_id=%s relevance=%.3f score=%.3f source=%s",
                        slot.id,
                        candidate.relevance,
                        candidate.score,
                        candidate.source_url,
                    )
                    relevance_reject_logged = True
                continue
            if candidate.score >= self.config.visual_image_min_confidence:
                add_attempt(
                    candidate,
                    {
                        "confidence": candidate.score,
                        "relevance": round(candidate.relevance, 4),
                        "retrieval_kind": candidate.retrieval_kind,
                        "verified": False,
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
                        "relevance": round(candidate.relevance, 4),
                        "retrieval_kind": candidate.retrieval_kind,
                        "verified": False,
                        "selection_reason": self._selection_reason_for_candidate(
                            candidate=candidate,
                            explicit_fallback=True,
                        ),
                        "alt_text": candidate.alt_text,
                        "caption": slot.caption or "",
                    },
                )
        if explicit_image_request and not attempt_candidates:
            for candidate in ranked:
                if candidate.image_url in planned_urls:
                    continue
                if not self._should_allow_explicit_request_fallback(slot, candidate):
                    continue
                logger.info(
                    "visual_enrichment.image_confidence_retry slot_id=%s score=%.3f source=%s kind=%s",
                    slot.id,
                    candidate.score,
                    candidate.source_url,
                    candidate.retrieval_kind,
                )
                add_attempt(
                    candidate,
                    {
                        "confidence": candidate.score,
                        "relevance": round(candidate.relevance, 4),
                        "retrieval_kind": candidate.retrieval_kind,
                        "verified": False,
                        "selection_reason": self._selection_reason_for_candidate(
                            candidate=candidate,
                            explicit_fallback=True,
                        ),
                        "alt_text": candidate.alt_text,
                        "caption": slot.caption or "",
                    },
                )
                break
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

        async def run_query(query_index: int, query: str) -> tuple[int, str, list[dict[str, Any]]]:
            try:
                raw_results = await self._image_search.search_images(query)
            except Exception as exc:
                logger.warning(
                    "visual_enrichment.image_search_query_failed slot_id=%s query=%s error=%s",
                    slot.id,
                    query,
                    exc,
                )
                return query_index, query, []
            return query_index, query, raw_results

        query_results = await asyncio.gather(
            *(run_query(query_index, query) for query_index, query in enumerate(queries, start=1))
        )

        for query_index, _query, raw_results in query_results:
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
                    thumbnail_url=_safe_text(item.get("thumbnail_url")),
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
        logger.info(
            "visual_enrichment.image_search_candidates slot_id=%s count=%s",
            slot.id,
            len(candidates),
        )
        return candidates

    async def _collect_trusted_image_candidates(
        self,
        *,
        slot: VisualSlotDirective,
        source_infos: list[dict[str, str]],
    ) -> list[ImageCandidate]:
        if not self._firecrawl.available or not source_infos:
            return []

        async def scrape_source(source_rank: int, source: dict[str, str]) -> list[ImageCandidate]:
            try:
                scraped = await self._firecrawl.scrape_images(source["url"])
            except Exception as exc:
                logger.warning(
                    "visual_enrichment.firecrawl_source_failed slot_id=%s source=%s error=%s",
                    slot.id,
                    source.get("url"),
                    exc,
                )
                return []
            source_title = (
                _safe_text(scraped.get("metadata", {}).get("title"))
                if isinstance(scraped.get("metadata"), dict)
                else ""
            )
            return self._extract_candidates_from_scrape(
                slot=slot,
                source_url=source["url"],
                source_title=source_title or _safe_text(source.get("title")),
                source_domain=_safe_text(source.get("domain")) or _safe_text(urlparse(source["url"]).netloc),
                source_rank=source_rank,
                raw_images=scraped.get("images"),
            )

        results = await asyncio.gather(
            *(
                scrape_source(source_rank, source)
                for source_rank, source in enumerate(
                    source_infos[: self.config.visual_image_source_page_limit],
                    start=1,
                )
            )
        )
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()
        for source_candidates in results:
            for candidate in source_candidates:
                if candidate.image_url in seen_urls:
                    continue
                seen_urls.add(candidate.image_url)
                candidates.append(candidate)
        logger.info(
            "visual_enrichment.trusted_image_candidates slot_id=%s count=%s",
            slot.id,
            len(candidates),
        )
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
                source_title=_safe_text(spec.get("title")) or "Chart",
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
                    "source_title": _safe_text(spec.get("title")) or "Chart",
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
            self._slot_deadlines.pop(slot.id, None)
            self._drain_slot_queue()

    async def _run_image_sidecar(self, slot: VisualSlotDirective) -> None:
        try:
            # Gathered first and cheaply: these are local files, and having one
            # means the slot can be filled even with no scraper and no search.
            run_candidates = self._collect_run_capture_candidates(slot)
            image_search_allowed = self._image_search.available and bool(
                self._build_image_search_queries(slot)
            )
            if not run_candidates and not self._firecrawl.available and not image_search_allowed:
                raise ValueError(
                    "Image enrichment is unavailable because neither trusted-source scraping nor direct image search is configured."
                )
            source_infos = self._candidate_source_infos(slot)
            if not run_candidates and not source_infos and not image_search_allowed:
                raise ValueError("No trusted source URLs were available for this image slot.")

            explicit_image_request = self._slot_explicitly_requests_image(slot)
            automatic_image_slot = self._is_automatic_image_slot(slot)
            trusted_candidates: list[ImageCandidate] = []
            search_candidates: list[ImageCandidate] = []

            if explicit_image_request and image_search_allowed:
                trusted_result, search_result = await asyncio.gather(
                    self._timed_stage(
                        slot.id,
                        "trusted_image_candidates",
                        self._collect_trusted_image_candidates(
                            slot=slot,
                            source_infos=source_infos,
                        ),
                    ),
                    self._timed_stage(
                        slot.id,
                        "image_search_candidates",
                        self._search_image_candidates(slot),
                    ),
                    return_exceptions=True,
                )
                if isinstance(trusted_result, Exception):
                    logger.warning("visual_enrichment.trusted_image_collection_failed slot_id=%s error=%s", slot.id, trusted_result)
                else:
                    trusted_candidates = trusted_result
                if isinstance(search_result, Exception):
                    logger.warning("visual_enrichment.image_search_failed slot_id=%s error=%s", slot.id, search_result)
                else:
                    search_candidates = search_result
            elif automatic_image_slot and image_search_allowed:
                try:
                    search_candidates = await self._timed_stage(
                        slot.id,
                        "image_search_candidates",
                        self._search_image_candidates(slot),
                    )
                except Exception as exc:
                    logger.warning("visual_enrichment.image_search_failed slot_id=%s error=%s", slot.id, exc)
                if not search_candidates and source_infos:
                    trusted_candidates = await self._timed_stage(
                        slot.id,
                        "trusted_image_candidates",
                        self._collect_trusted_image_candidates(
                            slot=slot,
                            source_infos=source_infos,
                        ),
                    )
            else:
                trusted_candidates = await self._timed_stage(
                    slot.id,
                    "trusted_image_candidates",
                    self._collect_trusted_image_candidates(
                        slot=slot,
                        source_infos=source_infos,
                    ),
                )
                if self._should_use_image_search_fallback(slot, trusted_candidates):
                    try:
                        search_candidates = await self._timed_stage(
                            slot.id,
                            "image_search_candidates",
                            self._search_image_candidates(slot),
                        )
                    except Exception as exc:
                        logger.warning("visual_enrichment.image_search_failed slot_id=%s error=%s", slot.id, exc)

            # First-party captures lead: when the answer was written off a
            # screenshot this run took, that screenshot is the illustration.
            seen_image_urls = {item.image_url for item in run_candidates}
            all_candidates = list(run_candidates)
            for candidate in (*trusted_candidates, *search_candidates):
                if candidate.image_url in seen_image_urls:
                    continue
                seen_image_urls.add(candidate.image_url)
                all_candidates.append(candidate)

            if not all_candidates:
                raise ValueError(
                    "No usable image candidates were found on the trusted source pages or direct image search."
                )

            attempt_candidates = await self._timed_stage(
                slot.id,
                "image_candidate_ranking",
                self._build_attempt_candidates(
                    slot=slot,
                    candidates=all_candidates,
                ),
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
                    cached_image_bytes = verdict.get("_downloaded_image_bytes")
                    if isinstance(cached_image_bytes, bytes):
                        image_bytes = cached_image_bytes
                        detected_mime, width, height = await asyncio.to_thread(
                            self._inspect_image_bytes,
                            image_bytes,
                        )
                    else:
                        image_bytes, detected_mime, width, height = await self._timed_stage(
                            slot.id,
                            "image_download",
                            self._download_image_bytes(candidate.image_url),
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
            is_run_capture = selected_candidate.retrieval_kind == "run_capture"
            block = self._build_image_artifact_block(
                slot=slot,
                artifact=artifact,
                kind="reference_image",
                caption=_safe_text(selected_verdict.get("caption")) or _safe_text(slot.caption),
                provenance={
                    "source_url": selected_candidate.source_url,
                    "source_title": selected_candidate.source_title,
                    "source_domain": selected_candidate.source_domain,
                    # A run capture's image_url is an internal artifact handle, not
                    # something a client could resolve.
                    "source_image_url": (
                        ""
                        if is_run_capture
                        else selected_candidate.image_url
                    ),
                    "attribution_label": (
                        f"Screenshot of {selected_candidate.source_title or selected_candidate.source_domain}"
                        if is_run_capture
                        else f"Image from {selected_candidate.source_title or selected_candidate.source_domain}"
                    ),
                    "selection_reason": _safe_text(selected_verdict.get("selection_reason"))
                    or "Best match from a trusted source page.",
                    "confidence": max(
                        selected_candidate.score,
                        _parse_float(selected_verdict.get("confidence"), default=selected_candidate.score),
                    ),
                    # The numbers the choice was actually made on. The old payload
                    # reported confidence 1.0 for an image with no topical overlap
                    # at all, because the score had clamped, which left the field
                    # useless for working out why anything was picked.
                    "relevance": round(selected_candidate.relevance, 4),
                    "retrieval_kind": selected_candidate.retrieval_kind,
                    "verified": bool(selected_verdict.get("verified")),
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
            self._slot_deadlines.pop(slot.id, None)
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

    def _topic_tokens(self, slot: VisualSlotDirective) -> set[str]:
        """What the answer is about, rather than how the user phrased the question.

        Auto-injected slots search on the user's own sentence, so judging against
        that same sentence just re-confirms the phrasing. The answer text and the
        pages it actually cites are what the image has to match.
        """
        # Subject matter only. Source titles and domains are deliberately absent:
        # a candidate carries its own source metadata, so scoring it against a
        # topic built from that same metadata is circular — the domain matches
        # the domain and the image looks relevant to a page it merely came from.
        # Provenance is credited separately, in _provenance_relevance.
        parts = [
            _safe_text(slot.query),
            _clip_text(slot.context_excerpt, limit=1200),
            self.user_query,
        ]
        return _content_tokens(" ".join(part for part in parts if part))

    def _provenance_relevance(self, candidate: ImageCandidate) -> float:
        """Relevance a candidate inherits from where it came from.

        An image lifted off a page the answer cites is on-topic by construction,
        however thin its alt text. An open-web image-search hit has no such
        standing and has to earn its relevance lexically.
        """
        if candidate.retrieval_kind == "run_capture":
            return 1.0
        if candidate.retrieval_kind == "source_page":
            return 0.55
        return 0.0

    def _score_candidate(self, slot: VisualSlotDirective, candidate: ImageCandidate, corpus: str) -> float:
        topic_tokens = self._topic_tokens(slot)
        candidate_tokens = _content_tokens(
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
        source_tokens = _content_tokens(
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
        lexical_relevance = _weighted_coverage(topic_tokens, candidate_tokens | source_tokens)
        # Two different questions, deliberately answered separately:
        #   score     - how well does this candidate actually match? (ordering)
        #   relevance - is it topical enough to be eligible at all?   (gate)
        # Provenance answers only the second. Letting it raise the score would
        # inflate every trusted-source candidate and relax the strict threshold.
        candidate.relevance = max(lexical_relevance, self._provenance_relevance(candidate))
        candidate_overlap = len(topic_tokens & candidate_tokens)
        score = 0.16
        rank_step = 0.06 if candidate.retrieval_kind == "image_search" else 0.08
        rank_cap = 0.38 if candidate.retrieval_kind == "image_search" else 0.42
        score += max(0.0, rank_cap - ((candidate.source_rank - 1) * rank_step))
        # One relevance term on a real scale, replacing two terms whose shared
        # denominator was capped at 6 and therefore saturated on ~6 shared words.
        score += 0.66 * lexical_relevance
        if candidate.retrieval_kind == "run_capture":
            # First-party evidence: this is a picture of a page the answer cites,
            # taken by this run, not something matched by keyword.
            score += 0.30
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
        if topic_tokens and candidate_overlap == 0:
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
        relaxed_floor = max(0.28, self.config.visual_image_min_confidence - 0.25)
        if candidate.retrieval_kind == "image_search":
            relaxed_floor = max(0.24, relaxed_floor - 0.05)
        return candidate.score >= relaxed_floor

    async def _download_image_bytes(
        self,
        image_url: str,
    ) -> tuple[bytes, str | None, int | None, int | None]:
        if image_url.startswith(_RUN_CAPTURE_SCHEME):
            return await asyncio.to_thread(self._read_run_capture_bytes, image_url)
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

    @staticmethod
    def _inspect_image_bytes(data: bytes) -> tuple[str | None, int | None, int | None]:
        mime_type: str | None = None
        width: int | None = None
        height: int | None = None
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                mime_type = Image.MIME.get(image.format or "", None)
        except Exception:
            pass
        return mime_type, width, height

    def _read_run_capture_bytes(
        self,
        image_url: str,
    ) -> tuple[bytes, str | None, int | None, int | None]:
        artifact_id = image_url[len(_RUN_CAPTURE_SCHEME) :]
        artifact = self._run_images.get(artifact_id)
        if artifact is None:
            raise ValueError(f"Run capture {artifact_id} is no longer registered.")
        path = self._resolve_run_capture_path(artifact)
        if path is None:
            raise ValueError(f"Run capture {artifact_id} is missing on disk.")
        data = path.read_bytes()
        if not data:
            raise ValueError("Run capture file was empty.")
        if len(data) > self.config.visual_image_max_bytes:
            raise ValueError("Run capture exceeded the byte budget.")
        mime_type = _safe_text(artifact.get("mime_type") or artifact.get("mime")) or None
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
