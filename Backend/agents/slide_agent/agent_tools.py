from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx
from PIL import Image

from shared import (
    MeteredCall,
    begin_metered_call,
    build_model_key,
    build_usage_event,
    serialize_usage_metadata,
)

_HERE = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)

_SLIDE_AGENT_SOURCE_ID = "cosmic/slide-agent:1.0.0"


def _slide_gateway_credentials() -> tuple[str, str]:
    """Match gateway URL/token resolution used by SlideAgentConfig.from_env."""
    url = (os.getenv("GATEWAY_URL") or "").strip() or "http://127.0.0.1:8080"
    token = (os.getenv("GATEWAY_INTERNAL_TOKEN") or "").strip()
    return url, token


def _augment_xai_image_raw_usage(
    body: dict[str, Any] | None,
    *,
    output_image_count: int,
    quality: str,
    size: str,
) -> dict[str, Any]:
    """Align with image_generator_agent._augment_billing_usage_metadata for cost estimation."""
    base: dict[str, Any]
    if isinstance(body, dict):
        usage = body.get("usage")
        base = dict(usage) if isinstance(usage, dict) else {}
    else:
        base = {}
    if output_image_count > 0:
        if not base.get("output_images") and not base.get("images"):
            base["output_images"] = output_image_count
            base["images"] = output_image_count
    if quality and not str(base.get("generation_quality") or "").strip():
        base["generation_quality"] = quality
    if size and not str(base.get("generation_size") or "").strip():
        base["generation_size"] = size
    return base


def _sync_post_usage_to_gateway(gateway_url: str, internal_token: str, event: Any) -> None:
    """POST UsageEvent to gateway (sync); mirrors shared.usage.post_usage_event retries."""
    if not gateway_url.strip() or not internal_token.strip():
        return
    payload = event.model_dump(mode="json")
    url = gateway_url.rstrip("/") + "/internal/usage/log"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": internal_token,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=2.5)) as client:
            for attempt in range(3):
                try:
                    response = client.post(url, headers=headers, json=payload)
                    if response.status_code in {200, 201, 202}:
                        return
                except httpx.HTTPError:
                    if attempt >= 2:
                        logger.warning(
                            "slide_agent.image_generate.usage_post_http_failed llm_call_id=%s attempt=%s",
                            getattr(event, "llm_call_id", ""),
                            attempt + 1,
                        )
                if attempt < 2:
                    time.sleep(0.25 * (2**attempt))
    except Exception:
        logger.exception(
            "slide_agent.image_generate.usage_post_failed llm_call_id=%s",
            getattr(event, "llm_call_id", ""),
        )

try:  # Package import path in tests, flat import path in local runner.
    from .sandbox import (
        persist_slide_python_script,
        provision_venv,
        run_python_script,
        validate_pip_packages,
        write_execution_receipt,
    )
except ImportError:  # pragma: no cover - exercised by run_local.py style imports.
    from sandbox import (  # type: ignore
        persist_slide_python_script,
        provision_venv,
        run_python_script,
        validate_pip_packages,
        write_execution_receipt,
    )


class ToolExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolContext:
    output_dir: Path

    @property
    def tool_root(self) -> Path:
        path = self.output_dir / "tool_assets"
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    executor: Callable[[dict[str, Any], ToolContext], dict[str, Any]]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class FirecrawlConfig:
    api_key: str
    base_url: str
    request_timeout_sec: float
    extract_poll_interval_sec: float
    extract_max_wait_sec: float
    agent_poll_interval_sec: float
    agent_max_wait_sec: float

    @classmethod
    def from_env(cls) -> "FirecrawlConfig":
        return cls(
            api_key=os.getenv("FIRECRAWL_API_KEY", "").strip(),
            base_url=os.getenv("FIRECRAWL_API_BASE_URL", "https://api.firecrawl.dev").strip() or "https://api.firecrawl.dev",
            request_timeout_sec=max(15.0, _env_float("FIRECRAWL_REQUEST_TIMEOUT_SEC", 120.0)),
            extract_poll_interval_sec=max(0.5, _env_float("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC", 2.0)),
            extract_max_wait_sec=max(15.0, _env_float("FIRECRAWL_EXTRACT_MAX_WAIT_SEC", 120.0)),
            agent_poll_interval_sec=max(1.0, _env_float("FIRECRAWL_AGENT_POLL_INTERVAL_SEC", 3.0)),
            agent_max_wait_sec=max(30.0, _env_float("FIRECRAWL_AGENT_MAX_WAIT_SEC", 240.0)),
        )


def _normalize_openai_like_base_url(raw: str, *, default: str = "") -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return default
    for suffix in (
        "/v1/images/edits",
        "/images/edits",
        "/v1/images/generations",
        "/images/generations",
        "/v1/chat/completions",
        "/chat/completions",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    if not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


@dataclass(frozen=True, slots=True)
class ImageGenConfig:
    xai_api_key: str
    xai_base_url: str
    xai_model: str
    xai_timeout_sec: float
    default_size: str
    default_quality: str
    max_images_per_request: int
    max_prompt_chars: int

    @classmethod
    def from_env(cls) -> "ImageGenConfig":
        base_url = _normalize_openai_like_base_url(
            (os.getenv("XAI_BASE_URL") or os.getenv("IMAGE_AGENT_XAI_BASE_URL") or "").strip(),
            default="https://api.x.ai/v1",
        )
        size = os.getenv("IMAGE_AGENT_DEFAULT_SIZE", "1024x1024").strip() or "1024x1024"
        if size not in {"1024x1024", "1024x1536", "1536x1024"}:
            size = "1024x1024"
        quality = os.getenv("IMAGE_AGENT_DEFAULT_QUALITY", "high").strip().lower() or "high"
        if quality not in {"auto", "low", "medium", "high"}:
            quality = "high"
        return cls(
            xai_api_key=(os.getenv("XAI_API_KEY") or os.getenv("IMAGE_AGENT_XAI_API_KEY") or "").strip(),
            xai_base_url=base_url,
            xai_model=(os.getenv("XAI_MODEL") or os.getenv("IMAGE_AGENT_XAI_MODEL") or "grok-imagine-image-pro").strip() or "grok-imagine-image-pro",
            xai_timeout_sec=max(10.0, _env_float("XAI_TIMEOUT_SEC", _env_float("IMAGE_AGENT_XAI_TIMEOUT_SEC", 180.0))),
            default_size=size,
            default_quality=quality,
            max_images_per_request=max(1, _env_int("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST", 4)),
            max_prompt_chars=max(200, _env_int("IMAGE_AGENT_MAX_PROMPT_CHARS", 6000)),
        )


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    timeout_sec: float
    max_files: int
    max_bytes_per_file: int
    max_script_bytes: int
    allow_network: bool
    allow_pip: bool
    pip_timeout_sec: float
    venv_cache_root: str

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        return cls(
            timeout_sec=max(5.0, _env_float("PYTHON_SANDBOX_TIMEOUT_SEC", 25.0)),
            max_files=max(1, _env_int("PYTHON_SANDBOX_MAX_FILES", 8)),
            max_bytes_per_file=max(4096, _env_int("PYTHON_SANDBOX_MAX_BYTES_PER_FILE", 10000000)),
            max_script_bytes=max(4096, _env_int("PYTHON_SANDBOX_MAX_SCRIPT_BYTES", 256000)),
            allow_network=_env_bool("PYTHON_SANDBOX_ALLOW_NETWORK", False),
            allow_pip=_env_bool("PYTHON_SANDBOX_ALLOW_PIP", False),
            pip_timeout_sec=max(10.0, _env_float("PYTHON_SANDBOX_PIP_TIMEOUT_SEC", 120.0)),
            venv_cache_root=os.getenv("PYTHON_SANDBOX_VENV_CACHE_ROOT", "").strip(),
        )


def _compact_string(value: Any, *, limit: int = 1400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _compact_error_string(value: Any, *, limit: int = 1400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head_len = max(200, limit // 2)
    tail_len = max(200, limit - head_len - 8)
    return f"{text[:head_len].rstrip()}\n...\n{text[-tail_len:].lstrip()}"


def _truncate_value(value: Any, *, max_depth: int = 4, max_items: int = 8, max_chars: int = 1200) -> Any:
    if max_depth <= 0:
        return _compact_string(value, limit=min(max_chars, 240))
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                out["__truncated__"] = f"{len(value) - max_items} more fields omitted"
                break
            out[str(key)] = _truncate_value(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
        return out
    if isinstance(value, list):
        items = [
            _truncate_value(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append(f"... {len(value) - max_items} more items omitted")
        return items
    if isinstance(value, str):
        return _compact_string(value, limit=max_chars)
    return value


def _inspect_image_payload(image_bytes: bytes) -> tuple[str, int | None, int | None]:
    mime = "image/png"
    width: int | None = None
    height: int | None = None
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
            fmt = str(image.format or "").upper()
            if fmt == "JPEG":
                mime = "image/jpeg"
            elif fmt == "WEBP":
                mime = "image/webp"
            elif fmt == "GIF":
                mime = "image/gif"
    except Exception:
        pass
    return mime, width, height


def _sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "asset"


class FirecrawlToolClient:
    def __init__(self, config: FirecrawlConfig | None = None) -> None:
        self.config = config or FirecrawlConfig.from_env()

    @property
    def available(self) -> bool:
        return bool(self.config.api_key)

    def scrape(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolExecutionError("firecrawl_scrape requires a valid http(s) url.")
        body: dict[str, Any] = {
            "url": url,
            "formats": payload.get("formats") or ["markdown", "links"],
            "onlyMainContent": bool(payload.get("only_main_content", True)),
            "mobile": bool(payload.get("mobile", False)),
        }
        for in_key, out_key in {
            "wait_for_ms": "waitFor",
            "timeout_ms": "timeout",
            "max_age_ms": "maxAge",
            "include_tags": "includeTags",
            "exclude_tags": "excludeTags",
            "proxy": "proxy",
        }.items():
            if payload.get(in_key) not in {None, "", []}:
                body[out_key] = payload[in_key]
        started = time.perf_counter()
        response = self._request("POST", "/v2/scrape", json_body=body)
        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        data = response.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("Firecrawl scrape response did not include a data object.")
        markdown = _compact_string(data.get("markdown") or "", limit=2200)
        html = _compact_string(data.get("html") or data.get("rawHtml") or "", limit=1400)
        links = data.get("links") if isinstance(data.get("links"), list) else []
        images = data.get("images") if isinstance(data.get("images"), list) else []
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        available_formats = [
            fmt for fmt in ("markdown", "html", "rawHtml", "links", "images", "screenshot")
            if data.get(fmt) not in {None, "", [], {}}
        ]
        title = str(metadata.get("title") or "").strip()
        summary = f"Scraped {url} via Firecrawl and captured {', '.join(available_formats) or 'page content'}."
        return {
            "summary": summary,
            "elapsed_ms": elapsed_ms,
            "url": url,
            "title": title or None,
            "available_formats": available_formats,
            "metadata": _truncate_value(metadata),
            "markdown_excerpt": markdown or None,
            "html_excerpt": html or None,
            "links": _truncate_value(links),
            "images": _truncate_value(images),
        }

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        urls = [str(item or "").strip() for item in urls if str(item or "").strip()]
        prompt = str(payload.get("prompt") or "").strip()
        if not urls or len(prompt) < 5:
            raise ToolExecutionError("firecrawl_extract requires non-empty urls and a prompt of at least 5 characters.")
        scrape_options: dict[str, Any] = {
            "formats": ["markdown"],
            "onlyMainContent": bool(payload.get("only_main_content", True)),
        }
        for in_key, out_key in {
            "wait_for_ms": "waitFor",
            "timeout_ms": "timeout",
            "max_age_ms": "maxAge",
        }.items():
            if payload.get(in_key) not in {None, ""}:
                scrape_options[out_key] = payload[in_key]
        body: dict[str, Any] = {
            "urls": urls,
            "prompt": prompt,
            "enableWebSearch": bool(payload.get("enable_web_search", False)),
            "showSources": bool(payload.get("show_sources", False)),
            "scrapeOptions": scrape_options,
        }
        if isinstance(payload.get("schema"), dict):
            body["schema"] = payload["schema"]
        started = time.perf_counter()
        submitted = self._request("POST", "/v2/extract", json_body=body)
        job_id = str(submitted.get("id") or "").strip()
        if not job_id:
            raise ToolExecutionError("Firecrawl extract response did not include a job ID.")
        deadline = time.monotonic() + self.config.extract_max_wait_sec
        latest = submitted
        status = str(submitted.get("status") or "processing").strip().lower() or "processing"
        while status not in {"completed", "failed", "cancelled", "canceled"}:
            if time.monotonic() >= deadline:
                raise ToolExecutionError(f"Firecrawl extract job {job_id} timed out.")
            time.sleep(self.config.extract_poll_interval_sec)
            latest = self._request("GET", f"/v2/extract/{job_id}")
            status = str(latest.get("status") or status).strip().lower() or status
        if status in {"failed", "cancelled", "canceled"}:
            raise ToolExecutionError(str(latest.get("error") or latest.get("message") or f"Firecrawl extract ended with {status}."))
        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        return {
            "summary": f"Firecrawl extracted structured data from {len(urls)} page{'s' if len(urls) != 1 else ''}.",
            "elapsed_ms": elapsed_ms,
            "job_id": job_id,
            "status": status,
            "urls": urls,
            "data": _truncate_value(latest.get("data")),
            "sources": _truncate_value(latest.get("sources") if isinstance(latest.get("sources"), list) else []),
            "invalid_urls": _truncate_value(submitted.get("invalidURLs") or submitted.get("invalid_urls") or []),
        }

    def agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if len(prompt) < 10:
            raise ToolExecutionError("firecrawl_agent requires a prompt of at least 10 characters.")
        body: dict[str, Any] = {"prompt": prompt}
        urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        urls = [str(item or "").strip() for item in urls if str(item or "").strip()]
        if urls:
            body["urls"] = urls
        if isinstance(payload.get("schema"), dict):
            body["schema"] = payload["schema"]
        started = time.perf_counter()
        submitted = self._request("POST", "/v2/agent", json_body=body)
        job_id = str(submitted.get("id") or "").strip()
        if not job_id:
            raise ToolExecutionError("Firecrawl agent response did not include a job ID.")
        deadline = time.monotonic() + self.config.agent_max_wait_sec
        latest = submitted
        status = str(submitted.get("status") or "processing").strip().lower() or "processing"
        while status not in {"completed", "failed", "cancelled", "canceled"}:
            if time.monotonic() >= deadline:
                raise ToolExecutionError(f"Firecrawl agent job {job_id} timed out.")
            time.sleep(self.config.agent_poll_interval_sec)
            latest = self._request("GET", f"/v2/agent/{job_id}")
            status = str(latest.get("status") or status).strip().lower() or status
        if status in {"failed", "cancelled", "canceled"}:
            raise ToolExecutionError(str(latest.get("error") or latest.get("message") or f"Firecrawl agent ended with {status}."))
        elapsed_ms = max(1, int((time.perf_counter() - started) * 1000))
        return {
            "summary": (
                "Firecrawl autonomous agent completed web research."
                if not urls else
                f"Firecrawl autonomous agent completed research across {len(urls)} seed page{'s' if len(urls) != 1 else ''}."
            ),
            "elapsed_ms": elapsed_ms,
            "job_id": job_id,
            "status": status,
            "prompt": _compact_string(prompt, limit=600),
            "urls": urls,
            "data": _truncate_value(latest.get("data")),
            "sources": _truncate_value(latest.get("sources") if isinstance(latest.get("sources"), list) else []),
        }

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available:
            raise ToolExecutionError("FIRECRAWL_API_KEY is not configured.")
        url = f"{self.config.base_url.rstrip('/')}{path}"
        timeout = httpx.Timeout(self.config.request_timeout_sec, connect=min(self.config.request_timeout_sec, 20.0))
        try:
            with httpx.Client(timeout=timeout, http2=True) as client:
                response = client.request(
                    method,
                    url,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise ToolExecutionError(f"Firecrawl request timed out for {path}.") from exc
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"Firecrawl request failed for {path}: {exc}") from exc
        if response.status_code >= 400:
            raise ToolExecutionError(self._extract_error_message(response))
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"Firecrawl returned non-JSON for {path}.") from exc
        if payload.get("success") is False:
            raise ToolExecutionError(str(payload.get("error") or payload.get("message") or "Firecrawl returned success=false."))
        return payload

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text.strip()[:500] or f"Firecrawl request failed with status={response.status_code}."
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
        return f"Firecrawl request failed with status={response.status_code}."


class XAIImageGenerator:
    def __init__(self, config: ImageGenConfig | None = None) -> None:
        self.config = config or ImageGenConfig.from_env()

    @property
    def available(self) -> bool:
        return bool(self.config.xai_api_key)

    def _post_xai_image_usage_event(
        self,
        *,
        metered_call: MeteredCall,
        raw_usage: Any,
        provider_request_id: str | None,
        success: bool,
        error_code: str | None,
        metadata_extra: dict[str, Any],
    ) -> None:
        gateway_url, token = _slide_gateway_credentials()
        if not token:
            return
        event = build_usage_event(
            metered_call=metered_call,
            source_component="agent",
            source_id=_SLIDE_AGENT_SOURCE_ID,
            task_id=None,
            session_id=None,
            route="specialist",
            operation="agent.image.generate.provider",
            model_key=build_model_key("xai", self.config.xai_model),
            request_id=None,
            provider_request_id=provider_request_id,
            raw_usage=raw_usage,
            success=success,
            error_code=error_code if not success else None,
            metadata_json=serialize_usage_metadata(
                {
                    "provider": "xai",
                    "model": self.config.xai_model,
                    "tool": "slide_agent.image_generate",
                    "router_mode": "direct_tool",
                    **metadata_extra,
                }
            ),
        )
        _sync_post_usage_to_gateway(gateway_url, token, event)

    def generate(self, payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if not self.available:
            raise ToolExecutionError("XAI_API_KEY is not configured.")
        prompt = str(payload.get("prompt") or "").strip()
        if len(prompt) < 3:
            raise ToolExecutionError("image_generate requires a prompt of at least 3 characters.")
        if len(prompt) > self.config.max_prompt_chars:
            raise ToolExecutionError(f"image_generate prompt exceeds {self.config.max_prompt_chars} characters.")
        count = max(1, min(int(payload.get("count") or 1), self.config.max_images_per_request))
        size = str(payload.get("size") or self.config.default_size).strip() or self.config.default_size
        if size not in {"1024x1024", "1024x1536", "1536x1024"}:
            size = self.config.default_size
        quality = str(payload.get("quality") or self.config.default_quality).strip().lower() or self.config.default_quality
        if quality not in {"auto", "low", "medium", "high"}:
            quality = self.config.default_quality
        style_hint = str(payload.get("style_hint") or "").strip()
        use_case = str(payload.get("use_case") or "").strip()
        negative_prompt = str(payload.get("negative_prompt") or "").strip()
        provider_prompt = prompt
        if style_hint:
            provider_prompt += f"\n\nStyle / direction: {style_hint}"
        if use_case:
            provider_prompt += f"\n\nUse case: {use_case}"
        if negative_prompt:
            provider_prompt += f"\n\nAvoid: {negative_prompt}"
        request_payload: dict[str, Any] = {
            "model": self.config.xai_model,
            "prompt": provider_prompt,
            "n": count,
            "response_format": "b64_json",
        }
        request_payload.update(self._xai_request_fields_for_size(size))
        timeout = httpx.Timeout(self.config.xai_timeout_sec, connect=min(self.config.xai_timeout_sec, 15.0))
        metered_call = begin_metered_call(prefix="img_xai")
        response: httpx.Response | None = None
        body: Any = None
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    self.config.xai_base_url.rstrip("/") + "/images/generations",
                    json=request_payload,
                    headers={
                        "Authorization": f"Bearer {self.config.xai_api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=None,
                provider_request_id=None,
                success=False,
                error_code="TIMEOUT",
                metadata_extra={"phase": "http"},
            )
            raise ToolExecutionError("xAI image generation timed out.") from exc
        except httpx.HTTPError as exc:
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=None,
                provider_request_id=None,
                success=False,
                error_code="NETWORK_ERROR",
                metadata_extra={"phase": "http", "error": str(exc)[:200]},
            )
            raise ToolExecutionError(f"xAI image generation request failed: {exc}") from exc

        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=None,
                provider_request_id=None,
                success=False,
                error_code="INVALID_JSON",
                metadata_extra={"phase": "parse_response"},
            )
            raise ToolExecutionError("xAI image generation returned non-JSON.") from exc

        provider_request_id: str | None = None
        if isinstance(body, dict):
            rid = body.get("id")
            if rid is not None:
                provider_request_id = str(rid).strip() or None

        if response.status_code >= 400:
            raw_usage = _augment_xai_image_raw_usage(
                body if isinstance(body, dict) else None,
                output_image_count=0,
                quality=quality,
                size=size,
            )
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=raw_usage,
                provider_request_id=provider_request_id,
                success=False,
                error_code=f"HTTP_{response.status_code}",
                metadata_extra={"phase": "provider_response"},
            )
            raise ToolExecutionError(self._provider_error_message(body, response.status_code))

        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raw_usage = _augment_xai_image_raw_usage(
                body if isinstance(body, dict) else None,
                output_image_count=0,
                quality=quality,
                size=size,
            )
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=raw_usage,
                provider_request_id=provider_request_id,
                success=False,
                error_code="EMPTY_DATA",
                metadata_extra={"phase": "provider_response"},
            )
            raise ToolExecutionError("xAI image generation returned no images.")

        target_dir = ctx.tool_root / "generated"
        target_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        generated_assets: list[dict[str, str]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            raw_b64 = str(item.get("b64_json") or "").strip()
            if not raw_b64:
                continue
            try:
                image_bytes = base64.b64decode(raw_b64)
            except Exception as exc:
                raw_usage = _augment_xai_image_raw_usage(
                    body if isinstance(body, dict) else None,
                    output_image_count=0,
                    quality=quality,
                    size=size,
                )
                self._post_xai_image_usage_event(
                    metered_call=metered_call,
                    raw_usage=raw_usage,
                    provider_request_id=provider_request_id,
                    success=False,
                    error_code="INVALID_IMAGE_PAYLOAD",
                    metadata_extra={"phase": "decode_b64"},
                )
                raise ToolExecutionError("xAI returned an invalid base64 image payload.") from exc
            mime, width, height = _inspect_image_payload(image_bytes)
            asset_id = f"gen_{uuid4().hex[:12]}"
            extension = mimetypes.guess_extension(mime or "") or ".png"
            if extension == ".jpe":
                extension = ".jpg"
            filename = f"{asset_id}__{_sanitize_filename(self.config.xai_model)}__{index:02d}{extension}"
            path = target_dir / filename
            path.write_bytes(image_bytes)
            images.append({
                "asset_id": asset_id,
                "filename": filename,
                "mime": mime,
                "width": width,
                "height": height,
                "model": self.config.xai_model,
                "revised_prompt": str(item.get("revised_prompt") or "").strip() or None,
            })
            generated_assets.append({"asset_id": asset_id, "path": str(path.resolve())})
        if not images:
            raw_usage = _augment_xai_image_raw_usage(
                body if isinstance(body, dict) else None,
                output_image_count=0,
                quality=quality,
                size=size,
            )
            self._post_xai_image_usage_event(
                metered_call=metered_call,
                raw_usage=raw_usage,
                provider_request_id=provider_request_id,
                success=False,
                error_code="NO_DECODABLE_IMAGES",
                metadata_extra={"phase": "persist_images"},
            )
            raise ToolExecutionError("xAI image generation returned no decodable images.")

        raw_usage = _augment_xai_image_raw_usage(
            body if isinstance(body, dict) else None,
            output_image_count=len(images),
            quality=quality,
            size=size,
        )
        self._post_xai_image_usage_event(
            metered_call=metered_call,
            raw_usage=raw_usage,
            provider_request_id=provider_request_id,
            success=True,
            error_code=None,
            metadata_extra={"phase": "complete"},
        )

        summary = f"Generated {len(images)} image{'s' if len(images) != 1 else ''} via xAI {self.config.xai_model}."
        return {
            "summary": summary,
            "provider": "xai",
            "model": self.config.xai_model,
            "prompt": _compact_string(prompt, limit=700),
            "size": size,
            "quality": quality,
            "images": images,
            "generated_assets": generated_assets,
        }

    def _xai_request_fields_for_size(self, size: str) -> dict[str, str]:
        if size == "1024x1536":
            return {"aspect_ratio": "2:3"}
        if size == "1536x1024":
            return {"aspect_ratio": "3:2"}
        return {"aspect_ratio": "1:1"}

    def _provider_error_message(self, body: Any, status_code: int) -> str:
        if isinstance(body, dict):
            for key in ("error", "message", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
        return f"xAI image generation failed with status={status_code}."


class PythonSandboxRunner:
    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig.from_env()

    def run(self, payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        purpose = str(payload.get("purpose") or "python_sandbox").strip() or "python_sandbox"
        if len(code) < 8:
            raise ToolExecutionError("python_sandbox requires a non-trivial code string.")
        run_dir = (ctx.tool_root / "sandbox" / f"run_{uuid4().hex[:10]}").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        execution_id = f"exec_{uuid4().hex[:14]}"
        packages = self._requested_packages(payload)
        packages_installed: list[str] = []
        pip_log: dict[str, Any] | None = None
        python_executable: Path | str | None = None

        try:
            script_path = persist_slide_python_script(
                sandbox_root=run_dir,
                execution_id=execution_id,
                code=code,
                allow_network=self.config.allow_network,
                max_script_bytes=self.config.max_script_bytes,
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

        if packages:
            if self.config.allow_pip:
                try:
                    python_executable, packages_installed, pip_log = provision_venv(
                        packages=packages,
                        cache_root=Path(self.config.venv_cache_root) if self.config.venv_cache_root else None,
                        pip_timeout_sec=self.config.pip_timeout_sec,
                    )
                except (RuntimeError, ValueError) as exc:
                    raise ToolExecutionError(f"python_sandbox package provisioning failed: {exc}") from exc
            else:
                pip_log = {
                    "packages_requested": packages,
                    "packages_installed": [],
                    "skipped": "PYTHON_SANDBOX_ALLOW_PIP is false",
                }

        completed = run_python_script(
            script_path=script_path,
            cwd=run_dir,
            timeout_sec=self.config.timeout_sec,
            sandbox_root=run_dir,
            python_executable=python_executable,
        )

        generated_assets: list[dict[str, str]] = []
        files: list[dict[str, Any]] = []
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or self._is_internal_file(path, run_dir, script_path):
                continue
            rel = path.relative_to(run_dir).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > self.config.max_bytes_per_file:
                logger.warning("python_sandbox skipped oversize file %s (%d bytes)", path, size)
                continue
            if len(files) >= self.config.max_files:
                break
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            asset_id = f"py_{uuid4().hex[:12]}"
            files.append({
                "asset_id": asset_id,
                "filename": rel,
                "mime": mime,
                "bytes": size,
            })
            generated_assets.append({"asset_id": asset_id, "path": str(path.resolve())})

        receipt_path = write_execution_receipt(
            sandbox_root=run_dir,
            execution_id=execution_id,
            receipt={
                "kind": "slide_sandbox",
                "purpose": purpose,
                "network_enabled": self.config.allow_network,
                "packages_requested": packages,
                "packages_installed": packages_installed,
                "pip_log": pip_log,
                "environment_mode": "isolated_minimal",
                "exit_code": completed["exit_code"],
                "stdout": completed["stdout"],
                "stderr": completed["stderr"],
                "duration_ms": completed["duration_ms"],
                "script_relative": script_path.relative_to(run_dir).as_posix(),
                "files": files,
            },
        )

        status = "completed" if completed["exit_code"] == 0 else "failed"
        summary = (
            f"python_sandbox {status} for {purpose} and produced {len(files)} file{'s' if len(files) != 1 else ''}."
            if files else
            f"python_sandbox {status} for {purpose} with no output files."
        )
        result = {
            "summary": summary,
            "status": status,
            "return_code": completed["exit_code"],
            "stdout_excerpt": _compact_string(completed["stdout"] or "", limit=1200) or None,
            "stderr_excerpt": _compact_error_string(completed["stderr"] or "", limit=1200) or None,
            "files": files,
            "generated_assets": generated_assets,
            "receipt_path": str(receipt_path.resolve()),
            "network_enabled": self.config.allow_network,
            "packages_installed": packages_installed,
        }
        if completed["exit_code"] != 0 and not files:
            raise ToolExecutionError(
                result["stderr_excerpt"] or
                result["stdout_excerpt"] or
                f"python_sandbox failed with return code {completed['exit_code']}."
            )
        return result

    def _requested_packages(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("pip_install")
        if raw is None or raw == "" or raw == []:
            return []
        if isinstance(raw, str):
            items = re.split(r"[,\n]+", raw)
        elif isinstance(raw, list):
            items = [str(item) for item in raw]
        else:
            raise ToolExecutionError("python_sandbox pip_install must be a string or array of strings.")
        try:
            return validate_pip_packages([item.strip() for item in items if item.strip()])
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def _is_internal_file(self, path: Path, run_dir: Path, script_path: Path) -> bool:
        if path == script_path:
            return True
        try:
            rel = path.relative_to(run_dir)
        except ValueError:
            return True
        internal_dirs = {"codes", "executions", ".sandbox_home", ".pip_cache", "__pycache__"}
        return bool(rel.parts and rel.parts[0] in internal_dirs)


def _firecrawl_scrape_tool(payload: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    return FirecrawlToolClient().scrape(payload)


def _firecrawl_extract_tool(payload: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    return FirecrawlToolClient().extract(payload)


def _firecrawl_agent_tool(payload: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    return FirecrawlToolClient().agent(payload)


def _image_generate_tool(payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return XAIImageGenerator().generate(payload, ctx)


def _python_sandbox_tool(payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return PythonSandboxRunner().run(payload, ctx)


def planner_tools() -> list[ToolDefinition]:
    firecrawl = FirecrawlToolClient()
    if not firecrawl.available:
        return []
    return [
        ToolDefinition(
            name="firecrawl_scrape",
            description=(
                "Scrape a single URL into clean page content like markdown, links, images, or html. "
                "Use this when you already know the URL you need."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "formats": {"type": "array", "items": {"type": "string"}},
                    "only_main_content": {"type": "boolean"},
                    "wait_for_ms": {"type": "integer"},
                    "timeout_ms": {"type": "integer"},
                    "max_age_ms": {"type": "integer"},
                },
                "required": ["url"],
            },
            executor=_firecrawl_scrape_tool,
        ),
        ToolDefinition(
            name="firecrawl_extract",
            description=(
                "Extract structured data from one or more known URLs. Prefer this when you know the target pages "
                "and want a schema-shaped result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "prompt": {"type": "string"},
                    "schema": {"type": "object"},
                    "show_sources": {"type": "boolean"},
                    "enable_web_search": {"type": "boolean"},
                },
                "required": ["urls", "prompt"],
            },
            executor=_firecrawl_extract_tool,
        ),
        ToolDefinition(
            name="firecrawl_agent",
            description=(
                "Run Firecrawl's autonomous research agent to search, navigate, and extract from the web. "
                "Use this when the right URLs are unknown or the research is multi-page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "schema": {"type": "object"},
                },
                "required": ["prompt"],
            },
            executor=_firecrawl_agent_tool,
        ),
    ]


def builder_tools() -> list[ToolDefinition]:
    tools = list(planner_tools())
    image_gen = XAIImageGenerator()
    if image_gen.available:
        tools.append(
            ToolDefinition(
                name="image_generate",
                description=(
                    "Generate a bespoke image via xAI Grok Imagine Image Pro. Use this when stock photos are too generic "
                    "or the slide needs a custom background, hero illustration, or thematic visual."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "style_hint": {"type": "string"},
                        "use_case": {"type": "string"},
                        "negative_prompt": {"type": "string"},
                        "size": {"type": "string", "enum": ["1024x1024", "1024x1536", "1536x1024"]},
                        "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                        "count": {"type": "integer"},
                    },
                    "required": ["prompt"],
                },
                executor=_image_generate_tool,
            )
        )
    if _env_bool("ENABLE_PYTHON_SANDBOX_TOOL", True):
        tools.append(
            ToolDefinition(
                name="python_sandbox",
                description=(
                    "Run isolated local Python in a bounded temporary run folder to generate files such as charts, diagrams, or synthetic image assets. "
                    "Use this only when a custom generated asset is materially better than the built-in chart/image paths. "
                    "Network and pip installs are disabled unless explicitly enabled by service env."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "purpose": {"type": "string"},
                        "code": {"type": "string"},
                        "pip_install": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["purpose", "code"],
                },
                executor=_python_sandbox_tool,
            )
        )
    return tools
