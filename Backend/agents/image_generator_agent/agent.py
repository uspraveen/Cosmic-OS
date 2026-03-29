from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image

from shared import AgentError, AgentResult, ArtifactManifest, TaskEnvelope, begin_metered_call, build_model_key, build_usage_event, connect_sync, is_supported_image_artifact, post_usage_event, serialize_usage_metadata
from shared.agent_runtime import AgentRuntime

from .config import AGENT_ROOT, BACKEND_ROOT, ImageGeneratorAgentConfig
from .internal_router import ImageRouteDecision, route_image_request

logger = logging.getLogger(__name__)

_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS image_generation_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    prompt TEXT NOT NULL,
    summary TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_generation_session_runs_session_created
ON image_generation_session_runs (session_id, created_at DESC);
"""


@dataclass(slots=True)
class ProviderImage:
    data: bytes
    mime: str
    revised_prompt: str | None
    width: int | None
    height: int | None


@dataclass(slots=True)
class ProviderGenerationResult:
    provider: str
    model: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    raw_usage: Any
    provider_request_id: str | None
    images: list[ProviderImage]


@dataclass(slots=True)
class ReferenceImage:
    artifact_ref: dict[str, str]
    filename: str
    mime: str
    data: bytes


class ImageGeneratorAgentError(RuntimeError):
    def __init__(self, *, code: str, message: str, retryable: bool, next_action: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.status_code = status_code


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class ImageGeneratorAgent(AgentRuntime):
    GENERATE_INTENT = "image.generate"
    RECALL_SESSION_INTENT = "image.recall_session"

    def __init__(
        self,
        *,
        redis_client,
        config: ImageGeneratorAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.config = config or ImageGeneratorAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.prompts_root = self.agent_root / "prompts"
        self.skills_path = self.agent_root / "skills" / "SKILLS.md"
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.runtime_root = (Path(runtime_root).expanduser() if runtime_root else self.agent_root / "runtime").resolve()
        self.data_root = self.store_root / "data"
        self.cache_root = self.runtime_root / "cache"
        self.logs_root = self.runtime_root / "logs"
        self.learnings_path = self.store_root / "learnings.md"
        self.session_db_path = self.data_root / "image_generation_session_runs.db"
        self.artifacts_root = (Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts").resolve()

        super().__init__(
            agent_card_path=self.agent_root / "agent_card.yaml",
            redis_client=redis_client,
            instance_id=instance_id,
            agent_secret=agent_secret,
            registry_db_path=registry_db_path,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
            http_client=http_client,
        )

    async def on_startup(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store_root.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text(
                "# Image Generator Agent Learnings\n\n"
                "- Default to Grok Imagine Image Pro for normal text-to-image work.\n"
                "- Use GPT Image 1.5 when the prompt is unusually complex, text-heavy, or layout-sensitive.\n"
                "- Include the generation model name in artifact filenames.\n",
                encoding="utf-8",
            )
        self._initialize_store()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        try:
            if task.intent == self.GENERATE_INTENT:
                return await self._handle_generate(task)
            if task.intent == self.RECALL_SESSION_INTENT:
                return await self._handle_recall_session(task)
            return self._result_error(code="INVALID_INPUT", message=f"Unsupported intent: {task.intent}", retryable=False, next_action="escalate")
        except ImageGeneratorAgentError as exc:
            logger.warning(
                "image_generator_agent.handled_error task_id=%s intent=%s code=%s status=%s message=%s",
                task.task_id,
                task.intent,
                exc.code,
                exc.status_code,
                exc.message,
            )
            return self._result_error(code=exc.code, message=exc.message, retryable=exc.retryable, next_action=exc.next_action)
        except Exception as exc:
            logger.exception("image_generator_agent.unhandled_error task_id=%s intent=%s", task.task_id, task.intent)
            return self._result_error(
                code="INTERNAL_ERROR",
                message=str(exc).strip()[:500] or "Image generator agent failed unexpectedly.",
                retryable=False,
                next_action="escalate",
            )

    async def _handle_generate(self, task: TaskEnvelope) -> AgentResult:
        self._load_prompt_assets()
        normalized_input = self._normalize_generate_input(task.input)
        reference_images = await self._resolve_reference_images(task.input_artifacts if isinstance(task.input_artifacts, list) else [])
        if reference_images:
            normalized_input["reference_image_count"] = len(reference_images)

        await self._emit_progress(task.task_id, "Routing image generation request", prompt=normalized_input["prompt"])
        route = await route_image_request(
            cfg=self.config,
            http_client=self._http_client,
            agent_id=self.agent_id,
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            session_id=task.session_id,
            request_id=self._request_id(task),
            payload=normalized_input,
        )
        route = self._resolve_reference_route(route, reference_images)
        if route.provider == "xai" and not self.config.xai_api_key:
            raise ImageGeneratorAgentError(code="AUTH_ERROR", message="xAI image generation credentials are not configured for the image generator agent.", retryable=False, next_action="configure_credentials")
        if route.provider == "openai" and not self.config.openai_api_key:
            raise ImageGeneratorAgentError(code="AUTH_ERROR", message="OpenAI image generation credentials are not configured for the image generator agent.", retryable=False, next_action="configure_credentials")

        progress_message = (
            f"Editing from {len(reference_images)} reference image(s) via {route.model}"
            if reference_images
            else f"Generating {normalized_input['count']} image(s) via {route.model}"
        )
        await self._emit_progress(task.task_id, progress_message, provider=route.provider, model=route.model)
        fallback_from: dict[str, Any] | None = None
        try:
            generation = await self._generate_with_provider(
                task=task,
                normalized_input=normalized_input,
                route=route,
                reference_images=reference_images,
            )
        except ImageGeneratorAgentError as exc:
            fallback_route = self._fallback_image_route(
                route=route,
                normalized_input=normalized_input,
                reference_images=reference_images,
                error=exc,
            )
            if fallback_route is None:
                raise
            fallback_from = {
                "provider": route.provider,
                "model": route.model,
                "error_code": exc.code,
                "error_message": exc.message,
            }
            await self._emit_progress(
                task.task_id,
                f"{route.model} hit a retryable failure. Retrying via {fallback_route.model}",
                provider=fallback_route.provider,
                model=fallback_route.model,
                fallback_from=route.model,
                error_code=exc.code,
            )
            route = fallback_route
            generation = await self._generate_with_provider(
                task=task,
                normalized_input=normalized_input,
                route=route,
                reference_images=reference_images,
            )
        await self._emit_progress(task.task_id, "Persisting generated image artifacts", model=route.model)

        image_artifacts, image_refs = self._persist_generated_images(task=task, generation=generation, artifact_basename=normalized_input["artifact_basename"])
        report_artifact = self._write_json_artifact(
            task=task,
            name=f"generation_report__{self._sanitize_for_filename(route.model)}.json",
            payload={
                "prompt": normalized_input["prompt"],
                "negative_prompt": normalized_input["negative_prompt"],
                "style_hint": normalized_input["style_hint"],
                "use_case": normalized_input["use_case"],
                "complexity_hint": normalized_input["complexity_hint"],
                "size": normalized_input["size"],
                "quality": normalized_input["quality"],
                "count": normalized_input["count"],
                "prefer_model": normalized_input["prefer_model"],
                "reference_images": [item.artifact_ref for item in reference_images],
                "router_decision": {
                    "provider": route.provider,
                    "model": route.model,
                    "reason": route.reason,
                    "router_mode": route.router_mode,
                },
                "fallback_from": fallback_from,
                "provider_request_id": generation.provider_request_id,
                "provider_usage": serialize_usage_metadata(generation.raw_usage),
                "response_payload": self._sanitize_provider_payload_for_artifact(generation.response_payload),
                "images": image_refs,
            },
            mime="application/json",
            kind="output",
            audience="supporting",
        )

        verb = "Edited" if reference_images else "Generated"
        summary = f"{verb} {len(image_refs)} image{'s' if len(image_refs) != 1 else ''} via {generation.provider}:{generation.model}."
        if fallback_from:
            summary = f"{summary} Retried after {fallback_from['provider']}:{fallback_from['model']} returned a retryable error."
        output = {
            "response": summary,
            "message": summary,
            "prompt": normalized_input["prompt"],
            "provider": generation.provider,
            "model": generation.model,
            "router_decision": {
                "provider": route.provider,
                "model": route.model,
                "reason": route.reason,
                "router_mode": route.router_mode,
            },
            "fallback_from": fallback_from,
            "images": image_refs,
            "artifact_refs": image_refs,
            "reference_images": [item.artifact_ref for item in reference_images],
            "provider_request_id": generation.provider_request_id,
            "provider_usage": serialize_usage_metadata(generation.raw_usage),
            "report_artifact": self._artifact_ref(report_artifact),
            "parameters": {
                "size": normalized_input["size"],
                "quality": normalized_input["quality"],
                "count": normalized_input["count"],
            },
        }
        self._record_session_run(
            task=task,
            prompt=normalized_input["prompt"],
            summary=summary,
            provider=generation.provider,
            model=generation.model,
            artifact_refs=image_refs + [self._artifact_ref(report_artifact)],
            details={
                "provider_request_id": generation.provider_request_id,
                "router_mode": route.router_mode,
                "router_reason": route.reason,
                "reference_images": [item.artifact_ref for item in reference_images],
            },
        )
        return AgentResult(status="completed", output=output, artifacts=image_artifacts + [report_artifact])

    async def _handle_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = str(task.input.get("session_id") or "").strip()
        if not session_id:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message="session_id is required for image.recall_session.", retryable=False, next_action="revise_input")
        limit = self._optional_int(task.input.get("limit"), minimum=1, maximum=50) or 10
        entries = self._load_session_entries(session_id=session_id, limit=limit)
        response = (
            f"Loaded {len(entries)} image-generation run{'s' if len(entries) != 1 else ''} from {session_id}."
            if entries
            else f"No image-generation runs were recorded for {session_id}."
        )
        return AgentResult(status="completed", output={"response": response, "session_id": session_id, "entries": entries}, artifacts=[])

    async def _generate_with_provider(
        self,
        *,
        task: TaskEnvelope,
        normalized_input: dict[str, Any],
        route: ImageRouteDecision,
        reference_images: list[ReferenceImage],
    ) -> ProviderGenerationResult:
        if route.provider == "xai":
            return await self._generate_via_image_api(
                task=task,
                provider="xai",
                model=route.model,
                api_key=self.config.xai_api_key,
                base_url=self.config.xai_base_url,
                timeout_sec=self.config.xai_timeout_sec,
                normalized_input=normalized_input,
                route=route,
                reference_images=reference_images,
            )
        if route.provider == "openai":
            return await self._generate_via_image_api(
                task=task,
                provider="openai",
                model=route.model,
                api_key=self.config.openai_api_key,
                base_url=self.config.openai_base_url,
                timeout_sec=self.config.openai_timeout_sec,
                normalized_input=normalized_input,
                route=route,
                reference_images=reference_images,
            )
        raise ImageGeneratorAgentError(code="INVALID_INPUT", message=f"Unsupported image provider route: {route.provider}", retryable=False, next_action="revise_input")

    async def _generate_via_image_api(
        self,
        *,
        task: TaskEnvelope,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        timeout_sec: float,
        normalized_input: dict[str, Any],
        route: ImageRouteDecision,
        reference_images: list[ReferenceImage],
    ) -> ProviderGenerationResult:
        if not api_key:
            raise ImageGeneratorAgentError(code="AUTH_ERROR", message=f"{provider} image generation credentials are not configured.", retryable=False, next_action="configure_credentials")

        is_edit = bool(reference_images)
        endpoint_path = "/images/edits" if is_edit else "/images/generations"
        prompt = self._build_provider_prompt(normalized_input)
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": normalized_input["count"],
        }
        request_kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            "timeout": httpx.Timeout(timeout_sec, connect=min(timeout_sec, 15.0)),
        }
        request_payload_for_report = dict(payload)
        if provider == "openai":
            payload["size"] = normalized_input["size"]
            if normalized_input["quality"] != "auto":
                payload["quality"] = normalized_input["quality"]
            if is_edit:
                form_data: dict[str, str] = {
                    "model": model,
                    "prompt": prompt,
                    "n": str(normalized_input["count"]),
                    "size": str(normalized_input["size"]),
                    "input_fidelity": "high",
                }
                if normalized_input["quality"] != "auto":
                    form_data["quality"] = str(normalized_input["quality"])
                files = [
                    (
                        "image[]",
                        (
                            item.filename,
                            item.data,
                            item.mime,
                        ),
                    )
                    for item in reference_images
                ]
                request_kwargs = {
                    "headers": {"Authorization": f"Bearer {api_key}"},
                    "data": form_data,
                    "files": files,
                    "timeout": httpx.Timeout(timeout_sec, connect=min(timeout_sec, 15.0)),
                }
                request_payload_for_report = {
                    "model": model,
                    "prompt": prompt,
                    "n": normalized_input["count"],
                    "size": normalized_input["size"],
                    "quality": normalized_input["quality"],
                    "input_fidelity": "high",
                    "reference_images": [
                        {
                            "filename": item.filename,
                            "mime": item.mime,
                            "bytes": len(item.data),
                        }
                        for item in reference_images
                    ],
                }
        else:
            payload["response_format"] = "b64_json"
            payload.update(self._xai_request_fields_for_size(normalized_input["size"]))
            if is_edit:
                image_entries = [
                    {
                        "type": "image_url",
                        "url": self._image_to_data_url(item),
                    }
                    for item in reference_images
                ]
                if len(image_entries) == 1:
                    payload["image"] = image_entries[0]
                else:
                    payload["images"] = image_entries
            request_kwargs["json"] = payload

        metered_call = begin_metered_call(prefix=f"img_{provider}")
        response_json: dict[str, Any] | None = None
        try:
            response = await self._http_client.post(
                base_url.rstrip("/") + endpoint_path,
                **request_kwargs,
            )
            response_json = self._parse_json_response(response)
            if response.status_code >= 400:
                raise self._map_provider_response_error(provider=provider, response=response, payload=response_json)
        except httpx.TimeoutException as exc:
            await self._post_provider_usage(metered_call=metered_call, task=task, provider=provider, model=model, raw_usage=None, provider_request_id=None, success=False, error_code="TIMEOUT", metadata={"provider": provider, "model": model, "router_mode": route.router_mode})
            raise ImageGeneratorAgentError(code="TIMEOUT", message=f"{provider} image generation timed out before completing.", retryable=True, next_action="retry") from exc
        except ImageGeneratorAgentError:
            await self._post_provider_usage(
                metered_call=metered_call,
                task=task,
                provider=provider,
                model=model,
                raw_usage=(response_json or {}).get("usage"),
                provider_request_id=str((response_json or {}).get("id") or "").strip() or None,
                success=False,
                error_code="PROVIDER_ERROR",
                metadata={"provider": provider, "model": model, "router_mode": route.router_mode},
            )
            raise
        except httpx.HTTPError as exc:
            await self._post_provider_usage(metered_call=metered_call, task=task, provider=provider, model=model, raw_usage=None, provider_request_id=None, success=False, error_code="NETWORK_ERROR", metadata={"provider": provider, "model": model, "router_mode": route.router_mode, "error": str(exc)[:200]})
            raise ImageGeneratorAgentError(code="NETWORK_ERROR", message=f"{provider} image generation failed before a valid response was received.", retryable=True, next_action="retry") from exc

        raw_usage = self._augment_billing_usage_metadata(
            response_json.get("usage"),
            normalized_input=normalized_input,
            reference_images=reference_images,
            output_image_count=len(items) if isinstance(items, list) else 0,
        )
        provider_request_id = str(response_json.get("id") or "").strip() or None
        await self._post_provider_usage(metered_call=metered_call, task=task, provider=provider, model=model, raw_usage=raw_usage, provider_request_id=provider_request_id, success=True, error_code=None, metadata={"provider": provider, "model": model, "router_mode": route.router_mode})

        items = response_json.get("data")
        if not isinstance(items, list) or not items:
            raise ImageGeneratorAgentError(code="INTERNAL_ERROR", message=f"{provider} returned no image payloads.", retryable=False, next_action="escalate")
        images: list[ProviderImage] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_b64 = str(item.get("b64_json") or "").strip()
            if not raw_b64:
                continue
            try:
                image_bytes = base64.b64decode(raw_b64)
            except Exception as exc:
                raise ImageGeneratorAgentError(code="INTERNAL_ERROR", message=f"{provider} returned an invalid image payload.", retryable=False, next_action="escalate") from exc
            detected_mime, width, height = self._inspect_image_payload(image_bytes)
            images.append(
                ProviderImage(
                    data=image_bytes,
                    mime=detected_mime,
                    revised_prompt=str(item.get("revised_prompt") or "").strip() or None,
                    width=width,
                    height=height,
                )
            )
        if not images:
            raise ImageGeneratorAgentError(code="INTERNAL_ERROR", message=f"{provider} did not return any decodable images.", retryable=False, next_action="escalate")
        return ProviderGenerationResult(
            provider=provider,
            model=model,
            request_payload=request_payload_for_report,
            response_payload=response_json,
            raw_usage=raw_usage,
            provider_request_id=provider_request_id,
            images=images,
        )

    def _augment_billing_usage_metadata(
        self,
        raw_usage: Any,
        *,
        normalized_input: dict[str, Any],
        reference_images: list[ReferenceImage],
        output_image_count: int,
    ) -> dict[str, Any]:
        base: dict[str, Any]
        if isinstance(raw_usage, dict):
            base = dict(raw_usage)
        else:
            base = {}
        if output_image_count > 0 and not _coerce_nonnegative_int(base.get("output_images")):
            base["output_images"] = output_image_count
        if output_image_count > 0 and not _coerce_nonnegative_int(base.get("images")):
            base["images"] = output_image_count
        input_image_count = len(reference_images)
        if input_image_count > 0 and not _coerce_nonnegative_int(base.get("input_images")):
            base["input_images"] = input_image_count
        generation_quality = str(normalized_input.get("quality") or "").strip()
        if generation_quality and not str(base.get("generation_quality") or "").strip():
            base["generation_quality"] = generation_quality
        generation_size = str(normalized_input.get("size") or "").strip()
        if generation_size and not str(base.get("generation_size") or "").strip():
            base["generation_size"] = generation_size
        return base

    async def _resolve_reference_images(self, input_artifacts: list[dict[str, Any]]) -> list[ReferenceImage]:
        if not input_artifacts:
            return []
        references: list[ReferenceImage] = []
        for artifact in input_artifacts:
            if not isinstance(artifact, dict):
                continue
            if not is_supported_image_artifact(artifact):
                raise ImageGeneratorAgentError(
                    code="INVALID_INPUT",
                    message="image.generate reference inputs must be image artifacts only.",
                    retryable=False,
                    next_action="revise_input",
                )
            payload = await self._load_reference_image_payload(artifact)
            if payload is None:
                raise ImageGeneratorAgentError(
                    code="INVALID_INPUT",
                    message="One or more reference images could not be loaded from COSMIC artifact storage.",
                    retryable=False,
                    next_action="revise_input",
                )
            references.append(payload)
        if len(references) > self.config.max_reference_images:
            raise ImageGeneratorAgentError(
                code="INVALID_INPUT",
                message=f"image.generate supports up to {self.config.max_reference_images} reference images per request.",
                retryable=False,
                next_action="revise_input",
            )
        return references

    async def _load_reference_image_payload(self, artifact: dict[str, Any]) -> ReferenceImage | None:
        filename = self._safe_artifact_filename(
            str(artifact.get("filename") or "").strip(),
            fallback=Path(str(artifact.get("path") or "")).name or "reference.png",
        )
        mime = (
            str(artifact.get("mime") or artifact.get("mime_type") or "").strip()
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        content: bytes | None = None
        resolved_path = self._resolve_input_artifact_path(str(artifact.get("path") or "").strip())
        if resolved_path and resolved_path.is_file():
            try:
                content = resolved_path.read_bytes()
            except OSError:
                logger.exception("image_generator_agent.reference_image_read_failed path=%s", resolved_path)
                content = None
        if content is None:
            remote_url = str(artifact.get("provider_url") or artifact.get("download_url") or "").strip()
            if remote_url:
                try:
                    response = await self._http_client.get(remote_url, timeout=httpx.Timeout(60.0, connect=15.0))
                    response.raise_for_status()
                    content = response.content
                    if response.headers.get("content-type"):
                        mime = str(response.headers.get("content-type") or "").split(";", 1)[0].strip() or mime
                except Exception:
                    logger.exception("image_generator_agent.reference_image_fetch_failed url=%s", remote_url)
                    content = None
        if not content:
            return None
        return ReferenceImage(
            artifact_ref={
                "artifact_id": str(artifact.get("artifact_id") or "").strip(),
                "path": str(artifact.get("path") or "").strip(),
                "mime": mime,
            },
            filename=filename,
            mime=mime,
            data=content,
        )

    def _resolve_reference_route(self, route: ImageRouteDecision, reference_images: list[ReferenceImage]) -> ImageRouteDecision:
        if not reference_images:
            return route
        if route.provider == "xai" and len(reference_images) > 3 and self.config.openai_api_key:
            return ImageRouteDecision(
                provider="openai",
                model=self.config.openai_image_model,
                reason="Fell back to GPT Image 1.5 because this reference-image edit exceeds xAI's three-image edit limit.",
                router_mode="edit_fallback",
            )
        return route

    def _fallback_image_route(
        self,
        *,
        route: ImageRouteDecision,
        normalized_input: dict[str, Any],
        reference_images: list[ReferenceImage],
        error: ImageGeneratorAgentError,
    ) -> ImageRouteDecision | None:
        if not error.retryable:
            return None
        prefer_model = str(normalized_input.get("prefer_model") or "").strip().lower()
        if prefer_model not in {"", "auto"}:
            return None
        if route.provider == "xai" and self.config.openai_api_key:
            return ImageRouteDecision(
                provider="openai",
                model=self.config.openai_image_model,
                reason=f"Retried via GPT Image 1.5 after xAI returned a retryable {error.code.lower()} error.",
                router_mode="provider_fallback",
            )
        if route.provider == "openai" and self.config.xai_api_key and len(reference_images) <= 3:
            return ImageRouteDecision(
                provider="xai",
                model=self.config.xai_image_model,
                reason=f"Retried via Grok Imagine Image Pro after OpenAI returned a retryable {error.code.lower()} error.",
                router_mode="provider_fallback",
            )
        return None

    def _resolve_input_artifact_path(self, raw_path: str) -> Path | None:
        value = str(raw_path or "").strip()
        if not value:
            return None
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        normalized = value.replace("\\", "/").lstrip("./")
        if normalized.startswith("runs/artifacts/"):
            relative = normalized[len("runs/artifacts/") :].strip("/")
            if relative:
                return (self.artifacts_root / Path(relative)).resolve()
        return (BACKEND_ROOT / Path(normalized)).resolve()

    def _image_to_data_url(self, image: ReferenceImage) -> str:
        media_type = str(image.mime or "").strip() or "image/png"
        return f"data:{media_type};base64,{base64.b64encode(image.data).decode('ascii')}"

    def _safe_artifact_filename(self, filename: str, *, fallback: str) -> str:
        candidate = Path(filename or "").name.strip()
        if not candidate:
            candidate = fallback
        candidate = re.sub(r"[\x00-\x1f]+", "_", candidate)
        candidate = re.sub(r"[^A-Za-z0-9._() \-]+", "_", candidate).strip(" ._")
        return candidate or fallback

    async def _emit_progress(self, task_id: str, message: str, **payload: Any) -> None:
        progress_payload = {"message": message}
        progress_payload.update(payload)
        await self.emit_event(task_id, "task.progress", progress_payload)

    async def _post_provider_usage(
        self,
        *,
        metered_call,
        task: TaskEnvelope,
        provider: str,
        model: str,
        raw_usage: Any,
        provider_request_id: str | None,
        success: bool,
        error_code: str | None,
        metadata: dict[str, Any],
    ) -> None:
        if not self.gateway_internal_token:
            return
        event = build_usage_event(
            metered_call=metered_call,
            source_component="agent",
            source_id=self.agent_id,
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            session_id=task.session_id,
            route="specialist",
            operation="agent.image.generate.provider",
            model_key=build_model_key(provider, model),
            request_id=self._request_id(task),
            provider_request_id=provider_request_id,
            raw_usage=raw_usage,
            success=success,
            error_code=error_code if not success else None,
            metadata_json=serialize_usage_metadata(metadata),
        )
        try:
            await post_usage_event(client=self._http_client, gateway_url=self.gateway_url, internal_token=self.gateway_internal_token, event=event)
        except Exception:
            logger.exception(
                "image_generator_agent.provider_usage_post_failed task_id=%s llm_call_id=%s",
                task.task_id,
                event.llm_call_id,
            )

    def _normalize_generate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or payload.get("goal") or payload.get("query") or "").strip()
        if len(prompt) < 3:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message="prompt is required for image.generate.", retryable=False, next_action="revise_input")
        if len(prompt) > self.config.max_prompt_chars:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message=f"prompt exceeds the max supported length of {self.config.max_prompt_chars} characters.", retryable=False, next_action="revise_input")
        count = self._optional_int(payload.get("count"), minimum=1, maximum=self.config.max_images_per_request) or 1
        size = str(payload.get("size") or self.config.default_size).strip() or self.config.default_size
        if size not in {"1024x1024", "1024x1536", "1536x1024"}:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message="size must be one of 1024x1024, 1024x1536, or 1536x1024.", retryable=False, next_action="revise_input")
        quality = str(payload.get("quality") or self.config.default_quality).strip().lower() or self.config.default_quality
        if quality not in {"auto", "low", "medium", "high"}:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message="quality must be one of auto, low, medium, or high.", retryable=False, next_action="revise_input")
        artifact_basename = self._sanitize_for_filename(str(payload.get("artifact_basename") or "").strip()) or self._default_artifact_basename(prompt)
        return {
            "prompt": prompt,
            "negative_prompt": str(payload.get("negative_prompt") or "").strip() or None,
            "style_hint": str(payload.get("style_hint") or "").strip() or None,
            "use_case": str(payload.get("use_case") or "").strip() or None,
            "complexity_hint": str(payload.get("complexity_hint") or "").strip().lower() or "auto",
            "prefer_model": str(payload.get("prefer_model") or "").strip() or "auto",
            "count": count,
            "size": size,
            "quality": quality,
            "artifact_basename": artifact_basename,
        }

    def _build_provider_prompt(self, normalized_input: dict[str, Any]) -> str:
        lines = [normalized_input["prompt"]]
        if normalized_input.get("style_hint"):
            lines.append(f"Style / direction: {normalized_input['style_hint']}")
        if normalized_input.get("use_case"):
            lines.append(f"Use case: {normalized_input['use_case']}")
        if normalized_input.get("negative_prompt"):
            lines.append(f"Avoid: {normalized_input['negative_prompt']}")
        return "\n\n".join(lines).strip()

    def _xai_request_fields_for_size(self, size: str) -> dict[str, str]:
        if size == "1024x1536":
            return {"aspect_ratio": "2:3"}
        if size == "1536x1024":
            return {"aspect_ratio": "3:2"}
        return {"aspect_ratio": "1:1"}

    def _persist_generated_images(self, *, task: TaskEnvelope, generation: ProviderGenerationResult, artifact_basename: str) -> tuple[list[ArtifactManifest], list[dict[str, Any]]]:
        manifests: list[ArtifactManifest] = []
        refs: list[dict[str, Any]] = []
        model_slug = self._sanitize_for_filename(generation.model)
        for index, item in enumerate(generation.images, start=1):
            extension = mimetypes.guess_extension(item.mime or "") or ".bin"
            if extension == ".jpe":
                extension = ".jpg"
            filename = f"{artifact_basename}__{model_slug}__{index:02d}{extension}"
            path = self._task_artifact_dir(task.task_id) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.data)
            manifest = self._artifact_manifest(task_id=task.task_id, path=path, mime=item.mime, kind="output", audience="deliverable")
            manifests.append(manifest)
            refs.append(
                {
                    "artifact_id": manifest.artifact_id,
                    "path": manifest.path,
                    "mime": manifest.mime,
                    "filename": filename,
                    "provider": generation.provider,
                    "model": generation.model,
                    "width": item.width,
                    "height": item.height,
                    "revised_prompt": item.revised_prompt,
                }
            )
        return manifests, refs

    def _write_json_artifact(self, *, task: TaskEnvelope, name: str, payload: Any, mime: str, kind: str, audience: str) -> ArtifactManifest:
        target_dir = self._task_artifact_dir(task.task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._artifact_manifest(task_id=task.task_id, path=path, mime=mime, kind=kind, audience=audience)

    def _artifact_manifest(self, *, task_id: str, path: Path, mime: str, kind: str, audience: str) -> ArtifactManifest:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task_id,
            mime=mime,
            sha256=digest,
            path=self._logical_artifact_path(path),
            created_by_agent=self.agent_id,
            kind=kind,
            audience=audience,
        )

    def _logical_artifact_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative_to_artifacts = resolved.relative_to(self.artifacts_root.resolve())
            return (Path("runs") / "artifacts" / relative_to_artifacts).as_posix()
        except ValueError:
            return resolved.relative_to(BACKEND_ROOT.resolve()).as_posix()

    def _task_artifact_dir(self, task_id: str) -> Path:
        return self.artifacts_root / task_id / "image_generator_agent"

    def _initialize_store(self) -> None:
        with connect_sync(self.session_db_path) as connection:
            connection.executescript(_RUNS_TABLE_SQL)
            connection.commit()

    def _record_session_run(self, *, task: TaskEnvelope, prompt: str, summary: str, provider: str, model: str, artifact_refs: list[dict[str, Any]], details: dict[str, Any]) -> None:
        session_id = str(task.session_id or "").strip()
        if not session_id:
            return
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.session_db_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO image_generation_session_runs (
                    task_id,
                    session_id,
                    intent,
                    prompt,
                    summary,
                    provider,
                    model,
                    artifact_json,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    session_id,
                    task.intent,
                    prompt,
                    summary,
                    provider,
                    model,
                    json.dumps(artifact_refs, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    created_at,
                ),
            )
            connection.commit()

    def _load_session_entries(self, *, session_id: str, limit: int) -> list[dict[str, Any]]:
        with connect_sync(self.session_db_path) as connection:
            rows = connection.execute(
                """
                SELECT task_id, intent, prompt, summary, provider, model, artifact_json, details_json, created_at
                FROM image_generation_session_runs
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            entries.append(
                {
                    "task_id": row["task_id"],
                    "intent": row["intent"],
                    "prompt": row["prompt"],
                    "summary": row["summary"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "artifact_refs": self._json_loads(row["artifact_json"], default=[]),
                    "details": self._json_loads(row["details_json"], default={}),
                    "created_at": row["created_at"],
                }
            )
        return entries

    def _load_prompt_assets(self) -> dict[str, str]:
        return {
            "system": self._read_text_file(self.prompts_root / "system.md"),
            "policies": self._read_text_file(self.prompts_root / "policies.md"),
            "skills": self._read_text_file(self.skills_path),
            "learnings": self._read_text_file(self.learnings_path),
        }

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _default_artifact_basename(self, prompt: str) -> str:
        slug = self._sanitize_for_filename(prompt)
        return (slug[:48] if slug else "generated_image")

    def _sanitize_for_filename(self, value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip())
        return normalized.strip("._-").lower()[:80] or ""

    def _artifact_ref(self, artifact: ArtifactManifest) -> dict[str, str]:
        return {"artifact_id": artifact.artifact_id, "path": artifact.path, "mime": artifact.mime}

    def _sanitize_provider_payload_for_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = json.loads(json.dumps(payload))
        data = sanitized.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("b64_json"), str):
                    item["b64_json"] = f"<omitted:{len(item['b64_json'])}_chars>"
        return sanitized

    def _parse_json_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            return {"error": {"message": response.text[:1000]}}
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _map_provider_response_error(self, *, provider: str, response: httpx.Response, payload: dict[str, Any]) -> ImageGeneratorAgentError:
        status = response.status_code
        message = self._extract_provider_error_message(payload) or f"{provider} returned HTTP {status}."
        if status in {401, 403}:
            return ImageGeneratorAgentError(code="AUTH_ERROR", message=message, retryable=False, next_action="configure_credentials", status_code=status)
        if status in {400, 404, 409, 422}:
            return ImageGeneratorAgentError(code="INVALID_INPUT", message=message, retryable=False, next_action="revise_input", status_code=status)
        if status == 429:
            return ImageGeneratorAgentError(code="RATE_LIMITED", message=message, retryable=True, next_action="retry", status_code=status)
        if status >= 500:
            return ImageGeneratorAgentError(code="NETWORK_ERROR", message=message, retryable=True, next_action="retry", status_code=status)
        return ImageGeneratorAgentError(code="NETWORK_ERROR", message=message, retryable=True, next_action="retry", status_code=status)

    def _extract_provider_error_message(self, payload: dict[str, Any]) -> str | None:
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "").strip() or None
        return None

    def _inspect_image_payload(self, data: bytes) -> tuple[str, int | None, int | None]:
        try:
            with Image.open(BytesIO(data)) as image:
                mime = Image.MIME.get(image.format or "", "") or "application/octet-stream"
                return mime, int(image.width), int(image.height)
        except Exception:
            return "application/octet-stream", None, None

    def _request_id(self, task: TaskEnvelope) -> str | None:
        return str(task.input.get("request_id") or "").strip() or None if isinstance(task.input, dict) else None

    def _optional_int(self, value: Any, *, minimum: int, maximum: int) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ImageGeneratorAgentError(code="INVALID_INPUT", message=f"Expected an integer between {minimum} and {maximum}.", retryable=False, next_action="revise_input") from exc
        return max(minimum, min(maximum, parsed))

    def _json_loads(self, raw: str | None, *, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def _result_error(self, *, code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, retryable=retryable, message=message, next_action=next_action),
        )
