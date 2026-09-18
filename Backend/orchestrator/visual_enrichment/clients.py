from __future__ import annotations

import base64
import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from shared import is_openai_gpt5_chat_model, normalized_reasoning_effort

logger = logging.getLogger(__name__)


class VisualEnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FirecrawlVisualConfig:
    api_key: str
    base_url: str
    request_timeout_sec: float


@dataclass(frozen=True, slots=True)
class LunaVisualConfig:
    api_key: str
    base_url: str
    model: str
    vision_model: str
    reasoning_effort: str
    timeout_sec: float
    max_output_tokens: int = 200


@dataclass(frozen=True, slots=True)
class DirectImageSearchConfig:
    enabled: bool
    base_url: str
    timeout_sec: float
    result_limit: int


def _strip_markdown_json_fences(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = _strip_markdown_json_fences(raw)
    if not text:
        raise ValueError("empty JSON response")
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
    raise ValueError(f"no JSON object found in response: {text[:300]!r}")


class FirecrawlVisualClient:
    def __init__(
        self,
        config: FirecrawlVisualConfig,
        *,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self._client = http_client

    @property
    def available(self) -> bool:
        return bool(self.config.api_key)

    async def scrape_images(self, url: str) -> dict[str, Any]:
        normalized_url = str(url or "").strip()
        if not normalized_url.startswith(("http://", "https://")):
            raise VisualEnrichmentError("Firecrawl scrape requires a valid http(s) URL.")
        if not self.available:
            raise VisualEnrichmentError("Firecrawl image scrape is not configured.")
        response = await self._client.post(
            f"{self.config.base_url.rstrip('/')}/v2/scrape",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "url": normalized_url,
                "formats": ["images"],
                "onlyMainContent": True,
            },
            timeout=httpx.Timeout(
                self.config.request_timeout_sec,
                connect=min(self.config.request_timeout_sec, 10.0),
            ),
        )
        if response.status_code >= 400:
            raise VisualEnrichmentError(self._extract_http_error(response))
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise VisualEnrichmentError("Firecrawl returned non-JSON while scraping images.") from exc
        if payload.get("success") is False:
            raise VisualEnrichmentError(
                str(payload.get("error") or payload.get("message") or "Firecrawl scrape failed.")
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise VisualEnrichmentError("Firecrawl scrape response did not include a data object.")
        return data

    @staticmethod
    def _extract_http_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip()[:500] or f"Firecrawl request failed ({response.status_code})."
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
        return f"Firecrawl request failed ({response.status_code})."


class DirectImageSearchClient:
    def __init__(
        self,
        config: DirectImageSearchConfig,
        *,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self._client = http_client

    @property
    def available(self) -> bool:
        return bool(self.config.enabled and self.config.base_url)

    async def search_images(self, query: str) -> list[dict[str, Any]]:
        normalized_query = str(query or "").strip()
        if len(normalized_query) < 3:
            raise VisualEnrichmentError("Direct image search requires a non-empty query.")
        if not self.available:
            raise VisualEnrichmentError("Direct image search is not configured.")
        response = await self._client.get(
            self.config.base_url,
            params={
                "q": normalized_query,
                "form": "HDRSC2",
                "first": "1",
            },
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; COSMIC-OS/1.0; +https://cosmic.os)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(
                self.config.timeout_sec,
                connect=min(self.config.timeout_sec, 10.0),
            ),
        )
        if response.status_code >= 400:
            raise VisualEnrichmentError(self._extract_http_error(response))
        return self._parse_bing_image_results(response.text)[: self.config.result_limit]

    @staticmethod
    def _parse_bing_image_results(raw_html: str) -> list[dict[str, Any]]:
        text = str(raw_html or "")
        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        payload_pattern = re.compile(
            r"(?:\bm|\bdata-m)\s*=\s*(?P<quote>['\"])(?P<payload>.*?)(?P=quote)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in payload_pattern.finditer(text):
            raw_payload = match.group("payload")
            if "murl" not in raw_payload and "imgurl" not in raw_payload:
                continue
            decoded_payload = html.unescape(raw_payload).strip()
            if not decoded_payload.startswith("{"):
                continue
            try:
                payload = json.loads(decoded_payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            image_url = str(
                payload.get("murl")
                or payload.get("imgurl")
                or payload.get("image_url")
                or ""
            ).strip()
            if not image_url.startswith(("http://", "https://")) or image_url in seen_urls:
                continue
            source_url = str(
                payload.get("purl")
                or payload.get("hostPageUrl")
                or payload.get("source_url")
                or ""
            ).strip()
            title = str(payload.get("t") or payload.get("title") or "").strip()
            thumbnail_url = str(
                payload.get("turl")
                or payload.get("thumbnailUrl")
                or payload.get("thumbnail_url")
                or ""
            ).strip()
            if not thumbnail_url.startswith(("http://", "https://")):
                thumbnail_url = ""
            snippet = str(
                payload.get("desc")
                or payload.get("caption")
                or payload.get("s")
                or payload.get("snippet")
                or ""
            ).strip()
            width = DirectImageSearchClient._to_int(
                payload.get("imgw") or payload.get("width") or payload.get("w")
            )
            height = DirectImageSearchClient._to_int(
                payload.get("imgh") or payload.get("height") or payload.get("h")
            )
            source_domain = str(urlparse(source_url or image_url).netloc).strip()
            seen_urls.add(image_url)
            results.append(
                {
                    "image_url": image_url,
                    "thumbnail_url": thumbnail_url,
                    "source_url": source_url or image_url,
                    "title": title,
                    "snippet": snippet,
                    "source_domain": source_domain,
                    "width": width,
                    "height": height,
                }
            )
        return results

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _extract_http_error(response: httpx.Response) -> str:
        body = (response.text or "").strip()
        if body:
            return body[:500]
        return f"Direct image search request failed ({response.status_code})."


def _build_vision_chat_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Chat-completions body for the collage ranker / verifier.

    GPT-5.6 Luna rejects temperature/max_tokens. Keep output tiny so low
    reasoning still has room to emit the JSON object.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    token_budget = max(64, int(max_output_tokens))
    if is_openai_gpt5_chat_model(model):
        payload["max_completion_tokens"] = token_budget
        effort = normalized_reasoning_effort(model, reasoning_effort, default="low")
        if effort:
            payload["reasoning_effort"] = effort
    else:
        payload["temperature"] = 0.1
        payload["max_tokens"] = token_budget
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
    return payload


class LunaVisualClient:
    def __init__(
        self,
        config: LunaVisualConfig,
        *,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self._client = http_client

    @property
    def available(self) -> bool:
        return bool(self.config.api_key and self.config.base_url and self.config.vision_model)

    async def verify_image_candidate(
        self,
        *,
        slot_query: str,
        user_query: str,
        context_excerpt: str,
        source_url: str,
        source_title: str,
        source_domain: str,
        candidate_image_url: str,
        candidate_alt_text: str,
        candidate_title: str,
        candidate_nearby_text: str,
    ) -> dict[str, Any]:
        if not self.available:
            raise VisualEnrichmentError("Luna visual verifier is not configured.")
        prompt = (
            "Does this image match the search query? JSON only.\n"
            "Keys: accept (bool), confidence (0-1), alt_text (<=12 words), "
            "caption (<=12 words), selection_reason (<=8 words).\n"
            "Reject logos, word art, unrelated, or decorative shots.\n"
            f"Query: {slot_query}\n"
            f"Source: {source_domain} {source_title}\n"
            f"Alt: {candidate_alt_text}\n"
            f"Title: {candidate_title}\n"
        )
        response = await self._client.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=_build_vision_chat_payload(
                model=self.config.vision_model or self.config.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Return only a compact JSON object. No prose.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": candidate_image_url}},
                        ],
                    },
                ],
                reasoning_effort=self.config.reasoning_effort,
                max_output_tokens=self.config.max_output_tokens,
            ),
            timeout=httpx.Timeout(
                self.config.timeout_sec,
                connect=min(self.config.timeout_sec, 10.0),
            ),
        )
        if response.status_code >= 400:
            raise VisualEnrichmentError(self._extract_http_error(response))
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise VisualEnrichmentError("Luna visual verifier returned non-JSON.") from exc
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VisualEnrichmentError("Luna visual verifier returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "").strip()
            if not content and isinstance(message.get("reasoning_content"), str):
                content = str(message.get("reasoning_content") or "").strip()
        if not content:
            raise VisualEnrichmentError("Luna visual verifier returned empty content.")
        try:
            return _extract_json_object(content)
        except ValueError as exc:
            logger.warning("visual_enrichment.luna_parse_failed: %s", exc)
            raise VisualEnrichmentError("Luna visual verifier returned invalid JSON.") from exc

    async def rank_image_contact_sheet(
        self,
        *,
        slot_query: str,
        user_query: str,
        context_excerpt: str,
        contact_sheet_jpeg: bytes,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Choose among labeled local previews with one bounded vision request.

        Sending a composed data URL avoids asking the model provider to fetch
        third-party image URLs (which can redirect, expire, or block bots), and
        replaces N sequential verifier calls with a single comparison.
        """
        if not self.available:
            raise VisualEnrichmentError("Luna visual verifier is not configured.")
        if not contact_sheet_jpeg:
            raise VisualEnrichmentError("Contact sheet was empty.")
        marker_lines: list[str] = []
        for item in candidates:
            marker_lines.append(
                " | ".join(
                    [
                        f"marker={item.get('marker')}",
                        f"title={str(item.get('title') or '')[:180]}",
                        f"alt={str(item.get('alt_text') or '')[:180]}",
                        f"source={str(item.get('source_domain') or '')[:100]}",
                        f"context={str(item.get('nearby_text') or '')[:220]}",
                    ]
                )
            )
        prompt = (
            "Pick the one collage marker that matches this image search query. "
            "JSON only. Keys: accept (bool), selected_marker (int or 0), "
            "ranked_markers (max 3 ints), confidence (0-1), alt_text (<=12 words), "
            "caption (<=12 words), selection_reason (<=8 words). "
            "If none match, accept=false, selected_marker=0, ranked_markers=[]. "
            "Reject logos, word art, clocks, unrelated, or decorative shots.\n"
            f"Query: {slot_query}\n"
            "Markers:\n"
            + "\n".join(marker_lines)
        )
        encoded_sheet = base64.b64encode(contact_sheet_jpeg).decode("ascii")
        response = await self._client.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=_build_vision_chat_payload(
                model=self.config.vision_model or self.config.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Return only a compact JSON object. No prose.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{encoded_sheet}"
                                },
                            },
                        ],
                    },
                ],
                reasoning_effort=self.config.reasoning_effort,
                max_output_tokens=self.config.max_output_tokens,
            ),
            timeout=httpx.Timeout(
                self.config.timeout_sec,
                connect=min(self.config.timeout_sec, 10.0),
            ),
        )
        if response.status_code >= 400:
            raise VisualEnrichmentError(self._extract_http_error(response))
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise VisualEnrichmentError("Luna contact-sheet ranker returned non-JSON.") from exc
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VisualEnrichmentError("Luna contact-sheet ranker returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "").strip()
            if not content and isinstance(message.get("reasoning_content"), str):
                content = str(message.get("reasoning_content") or "").strip()
        if not content:
            raise VisualEnrichmentError("Luna contact-sheet ranker returned empty content.")
        try:
            return _extract_json_object(content)
        except ValueError as exc:
            logger.warning("visual_enrichment.luna_contact_sheet_parse_failed: %s", exc)
            raise VisualEnrichmentError("Luna contact-sheet ranker returned invalid JSON.") from exc

    @staticmethod
    def _extract_http_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip()[:500] or f"Luna request failed ({response.status_code})."
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()[:500]
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
        return f"Luna request failed ({response.status_code})."
