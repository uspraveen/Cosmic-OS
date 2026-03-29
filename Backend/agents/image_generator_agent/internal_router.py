from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from shared import begin_metered_call, build_model_key, build_usage_event, post_usage_event, serialize_usage_metadata

from .config import ImageGeneratorAgentConfig

logger = logging.getLogger(__name__)

_PRECISION_OPENAI_HINTS = {
    "exact text",
    "wordmark",
    "logo",
    "typography",
    "spelled",
    "caption",
    "headline",
    "label",
    "diagram",
    "chart",
    "map",
    "ui",
    "interface",
    "dashboard",
    "wireframe",
    "blueprint",
    "technical illustration",
    "technical drawing",
}

_MULTIPANEL_HINTS = {
    "multi-panel",
    "multiple panels",
    "storyboard",
    "comic",
    "panel",
}


@dataclass(frozen=True, slots=True)
class ImageRouteDecision:
    provider: str
    model: str
    reason: str
    router_mode: str


async def route_image_request(
    *,
    cfg: ImageGeneratorAgentConfig,
    http_client: httpx.AsyncClient,
    agent_id: str,
    task_id: str | None,
    parent_task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    payload: dict[str, Any],
) -> ImageRouteDecision:
    explicit = _explicit_route(payload, cfg)
    if explicit is not None:
        return _ensure_provider_available(cfg, explicit)

    if cfg.enable_internal_router_llm and cfg.router_api_key and cfg.router_base_url:
        llm_decision = await _route_with_llm(
            cfg=cfg,
            http_client=http_client,
            agent_id=agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            session_id=session_id,
            request_id=request_id,
            payload=payload,
        )
        if llm_decision is not None:
            return _ensure_provider_available(cfg, llm_decision)

    heuristic = _heuristic_route(payload, cfg)
    return _ensure_provider_available(cfg, heuristic)


async def _route_with_llm(
    *,
    cfg: ImageGeneratorAgentConfig,
    http_client: httpx.AsyncClient,
    agent_id: str,
    task_id: str | None,
    parent_task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    payload: dict[str, Any],
) -> ImageRouteDecision | None:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("image_generator_agent.langchain_unavailable: %s", exc)
        return None

    system_content = (
        "You are routing COSMIC image generation requests between two backends.\n"
        "Choose xai for almost all normal text-to-image and stylistic generation prompts.\n"
        "OpenAI GPT Image 1.5 is substantially more expensive and should be treated as an emergency precision path.\n"
        "Choose openai only when the request truly requires exact text rendering, strict layout control, diagram/chart/dashboard fidelity, "
        "logo or wordmark accuracy, multi-panel composition, or unusually precise reference-image editing.\n"
        "Do not choose openai just because a prompt is long, artistic, polished, or generally complex.\n"
        "Return JSON only: {\"provider\":\"xai|openai\",\"reason\":\"short reason\"}."
    )
    user_content = json.dumps(
        {
            "prompt": str(payload.get("prompt") or "").strip(),
            "negative_prompt": str(payload.get("negative_prompt") or "").strip() or None,
            "style_hint": str(payload.get("style_hint") or "").strip() or None,
            "use_case": str(payload.get("use_case") or "").strip() or None,
            "complexity_hint": str(payload.get("complexity_hint") or "").strip() or None,
            "size": str(payload.get("size") or "").strip() or None,
            "quality": str(payload.get("quality") or "").strip() or None,
            "count": int(payload.get("count") or 1),
            "reference_image_count": int(payload.get("reference_image_count") or 0),
        },
        ensure_ascii=False,
        indent=2,
    )

    metered_call = begin_metered_call(prefix="img_route")
    try:
        async with httpx.AsyncClient(
            timeout=cfg.router_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as router_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.router_model,
                "api_key": cfg.router_api_key,
                "base_url": cfg.router_base_url,
                "http_async_client": router_http,
            }
            if not _is_gpt5_chat_model(cfg.router_model):
                llm_kwargs["temperature"] = 0.1
            llm = ChatOpenAI(**llm_kwargs)
            response = await llm.ainvoke(
                [
                    SystemMessage(content=system_content),
                    HumanMessage(content=user_content),
                ]
            )
    except Exception as exc:
        await _post_router_usage(
            cfg=cfg,
            http_client=http_client,
            agent_id=agent_id,
            metered_call=metered_call,
            task_id=task_id,
            parent_task_id=parent_task_id,
            session_id=session_id,
            request_id=request_id,
            raw_usage=None,
            provider_request_id=None,
            success=False,
            error_code="LLM_ERROR",
            metadata={"error": str(exc)[:200], "router_mode": "llm"},
        )
        logger.warning("image_generator_agent.router_llm_failed: %s", exc)
        return None

    content = getattr(response, "content", None) or str(response)
    usage = getattr(response, "usage_metadata", None) or {}
    response_metadata = getattr(response, "response_metadata", None)
    if not usage and isinstance(response_metadata, dict):
        usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    provider_request_id = None
    if isinstance(response_metadata, dict):
        provider_request_id = str(response_metadata.get("id") or "").strip() or None
    await _post_router_usage(
        cfg=cfg,
        http_client=http_client,
        agent_id=agent_id,
        metered_call=metered_call,
        task_id=task_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        request_id=request_id,
        raw_usage=usage,
        provider_request_id=provider_request_id,
        success=True,
        error_code=None,
        metadata={"router_mode": "llm"},
    )

    try:
        parsed = _extract_json_object(str(content))
    except ValueError:
        logger.warning("image_generator_agent.router_llm_parse_failed: %s", str(content)[:400])
        return None
    provider = str(parsed.get("provider") or "").strip().lower()
    if provider not in {"xai", "openai"}:
        return None
    model = cfg.xai_image_model if provider == "xai" else cfg.openai_image_model
    reason = str(parsed.get("reason") or "").strip() or "Router LLM selected this provider."
    return ImageRouteDecision(provider=provider, model=model, reason=reason, router_mode="llm")


async def _post_router_usage(
    *,
    cfg: ImageGeneratorAgentConfig,
    http_client: httpx.AsyncClient,
    agent_id: str,
    metered_call,
    task_id: str | None,
    parent_task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    raw_usage: Any,
    provider_request_id: str | None,
    success: bool,
    error_code: str | None,
    metadata: dict[str, Any],
) -> None:
    if not cfg.gateway_internal_token:
        return
    event = build_usage_event(
        metered_call=metered_call,
        source_component="agent",
        source_id=agent_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        route="specialist",
        operation="agent.image.route",
        model_key=build_model_key("openai", cfg.router_model),
        request_id=request_id,
        provider_request_id=provider_request_id,
        raw_usage=raw_usage,
        success=success,
        error_code=error_code if not success else None,
        metadata_json=serialize_usage_metadata(metadata),
    )
    try:
        await post_usage_event(
            client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=event,
        )
    except Exception:
        logger.exception(
            "image_generator_agent.router_usage_post_failed task_id=%s llm_call_id=%s",
            task_id,
            event.llm_call_id,
        )


def _explicit_route(payload: dict[str, Any], cfg: ImageGeneratorAgentConfig) -> ImageRouteDecision | None:
    prefer_model = str(payload.get("prefer_model") or "").strip().lower()
    if prefer_model in {"", "auto"}:
        return None
    if prefer_model in {"openai", "gpt-image-1.5", cfg.openai_image_model.strip().lower()}:
        return ImageRouteDecision(
            provider="openai",
            model=cfg.openai_image_model,
            reason="Caller explicitly preferred GPT Image 1.5.",
            router_mode="explicit",
        )
    if prefer_model in {"xai", "grok", "grok-imagine-image-pro", cfg.xai_image_model.strip().lower()}:
        return ImageRouteDecision(
            provider="xai",
            model=cfg.xai_image_model,
            reason="Caller explicitly preferred Grok Imagine Image Pro.",
            router_mode="explicit",
        )
    return None


def _heuristic_route(payload: dict[str, Any], cfg: ImageGeneratorAgentConfig) -> ImageRouteDecision:
    prompt = " ".join(
        filter(
            None,
            [
                str(payload.get("prompt") or "").strip(),
                str(payload.get("negative_prompt") or "").strip(),
                str(payload.get("style_hint") or "").strip(),
                str(payload.get("use_case") or "").strip(),
                str(payload.get("complexity_hint") or "").strip(),
            ],
        )
    ).lower()
    reference_image_count = int(payload.get("reference_image_count") or 0)
    complexity_hint = str(payload.get("complexity_hint") or "").strip().lower()
    quoted_text = prompt.count('"') >= 2
    has_precision_hint = any(hint in prompt for hint in _PRECISION_OPENAI_HINTS)
    has_multipanel_hint = any(hint in prompt for hint in _MULTIPANEL_HINTS)
    has_reference_pressure = reference_image_count >= 2
    has_complexity_pressure = complexity_hint == "complex" and (quoted_text or len(prompt) >= 220)

    should_use_openai = (
        has_precision_hint
        or (has_multipanel_hint and (complexity_hint == "complex" or quoted_text or int(payload.get("count") or 1) >= 3))
        or (reference_image_count >= 1 and has_precision_hint)
        or has_reference_pressure
        or has_complexity_pressure
    )

    if should_use_openai:
        return ImageRouteDecision(
            provider="openai",
            model=cfg.openai_image_model,
            reason="Request needs unusually precise text, layout, or reference-image control, so GPT Image 1.5 is justified despite cost.",
            router_mode="heuristic",
        )
    provider = cfg.default_provider if cfg.default_provider in {"xai", "openai"} else "xai"
    model = cfg.xai_image_model if provider == "xai" else cfg.openai_image_model
    reason = (
        "Defaulting to Grok Imagine Image Pro for a standard image-generation request."
        if provider == "xai"
        else "Default provider override selected GPT Image 1.5."
    )
    return ImageRouteDecision(provider=provider, model=model, reason=reason, router_mode="heuristic")


def _ensure_provider_available(cfg: ImageGeneratorAgentConfig, decision: ImageRouteDecision) -> ImageRouteDecision:
    available = {
        "xai": bool(cfg.xai_api_key),
        "openai": bool(cfg.openai_api_key),
    }
    if available.get(decision.provider):
        return decision
    fallback_provider = "openai" if decision.provider == "xai" else "xai"
    if available.get(fallback_provider):
        fallback_model = cfg.openai_image_model if fallback_provider == "openai" else cfg.xai_image_model
        return ImageRouteDecision(
            provider=fallback_provider,
            model=fallback_model,
            reason=f"{decision.provider} image credentials are unavailable, so the request fell back to {fallback_provider}.",
            router_mode="availability_fallback",
        )
    return decision


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            candidate = "\n".join(lines[1:-1]).strip()
    first = candidate.find("{")
    last = candidate.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("No JSON object found")
    parsed = json.loads(candidate[first : last + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Router payload is not a JSON object")
    return parsed


def _is_gpt5_chat_model(model: str | None) -> bool:
    return str(model or "").strip().casefold().startswith("gpt-5")
