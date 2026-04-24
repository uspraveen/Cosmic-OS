from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class VisualEnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FirecrawlVisualConfig:
    api_key: str
    base_url: str
    request_timeout_sec: float


@dataclass(frozen=True, slots=True)
class FireworksVisualConfig:
    api_key: str
    base_url: str
    model: str
    vision_model: str
    reasoning_effort: str
    timeout_sec: float


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


class FireworksVisualClient:
    def __init__(
        self,
        config: FireworksVisualConfig,
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
            raise VisualEnrichmentError("Fireworks visual verifier is not configured.")
        prompt = (
            "You are validating whether an image is appropriate to place inline inside an assistant response.\n"
            "Return exactly one JSON object with keys: accept (boolean), confidence (0-1 number), "
            "alt_text (string), caption (string), selection_reason (string).\n"
            "Be strict. Reject decorative, generic, logo-only, unrelated, or low-information images.\n\n"
            f"Slot intent: {slot_query}\n"
            f"Original user question: {user_query}\n"
            f"Answer context excerpt: {context_excerpt[:1200]}\n"
            f"Source URL: {source_url}\n"
            f"Source title: {source_title}\n"
            f"Source domain: {source_domain}\n"
            f"Candidate alt text: {candidate_alt_text}\n"
            f"Candidate title: {candidate_title}\n"
            f"Candidate nearby text: {candidate_nearby_text[:800]}\n"
        )
        response = await self._client.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.vision_model or self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return only JSON. Do not add prose or markdown fences.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": candidate_image_url}},
                        ],
                    },
                ],
                "temperature": 0.1,
                "max_tokens": 350,
                "response_format": {"type": "json_object"},
                "reasoning_effort": self.config.reasoning_effort,
                "stream": False,
            },
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
            raise VisualEnrichmentError("Fireworks visual verifier returned non-JSON.") from exc
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VisualEnrichmentError("Fireworks visual verifier returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "").strip()
            if not content and isinstance(message.get("reasoning_content"), str):
                content = str(message.get("reasoning_content") or "").strip()
        if not content:
            raise VisualEnrichmentError("Fireworks visual verifier returned empty content.")
        try:
            return _extract_json_object(content)
        except ValueError as exc:
            logger.warning("visual_enrichment.fireworks_parse_failed: %s", exc)
            raise VisualEnrichmentError("Fireworks visual verifier returned invalid JSON.") from exc

    @staticmethod
    def _extract_http_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip()[:500] or f"Fireworks request failed ({response.status_code})."
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
        return f"Fireworks request failed ({response.status_code})."
