"""Email-agent internal LLM via LangChain OpenAI-compatible client + Gateway usage logging."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

import httpx

from shared.usage import UsageEvent, post_usage_event, serialize_usage_metadata

from .config import EmailAgentConfig

logger = logging.getLogger(__name__)


async def invoke_email_internal_llm(
    *,
    cfg: EmailAgentConfig,
    http_client: httpx.AsyncClient,
    system_content: str,
    user_message: str,
    task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
    operation: str = "email.internal_llm",
    max_output_chars: int = 24_000,
    temperature: float = 0.2,
) -> str | None:
    if not cfg.enable_internal_llm or not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("email_agent.langchain_unavailable: %s", exc)
        return None

    messages = [
        SystemMessage(content=system_content.strip()),
        HumanMessage(content=user_message[:120_000]),
    ]
    started = time.perf_counter()
    llm_call_id = f"email_internal_llm_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.internal_llm_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as llm_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.internal_llm_model,
                "api_key": cfg.internal_llm_api_key,
                "base_url": cfg.internal_llm_base_url,
                "http_async_client": llm_http,
            }
            # OpenAI GPT-5 chat-completions models reject temperature/top_p style sampling knobs.
            if not _is_gpt5_chat_model(cfg.internal_llm_model):
                llm_kwargs["temperature"] = temperature
            llm = ChatOpenAI(**llm_kwargs)
            result = await llm.ainvoke(messages)
    except Exception as exc:
        await _post_usage(
            cfg=cfg,
            http_client=http_client,
            ok=False,
            task_id=task_id,
            session_id=session_id,
            request_id=request_id,
            source=source,
            source_id=source_id,
            channel=channel,
            llm_call_id=llm_call_id,
            model=cfg.internal_llm_model,
            operation=operation,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc)[:200],
        )
        logger.warning("email_agent.internal_llm_invoke_failed: %s", exc)
        return None

    text = getattr(result, "content", None) or str(result)
    usage = getattr(result, "usage_metadata", None) or {}
    if not usage and isinstance(getattr(result, "response_metadata", None), dict):
        response_metadata = result.response_metadata  # type: ignore[union-attr]
        usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    await _post_usage(
        cfg=cfg,
        http_client=http_client,
        ok=True,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        source=source,
        source_id=source_id,
        channel=channel,
        llm_call_id=llm_call_id,
        model=cfg.internal_llm_model,
        operation=operation,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error=None,
    )
    return str(text).strip()[:max_output_chars] or None


async def invoke_email_internal_llm_json(
    *,
    cfg: EmailAgentConfig,
    http_client: httpx.AsyncClient,
    system_content: str,
    user_message: str,
    task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
    operation: str,
) -> dict[str, Any] | None:
    text = await invoke_email_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=system_content,
        user_message=user_message,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        source=source,
        source_id=source_id,
        channel=channel,
        operation=operation,
        max_output_chars=16_000,
        temperature=0.1,
    )
    if not text:
        return None
    try:
        return _extract_json_object(text)
    except ValueError:
        logger.warning("email_agent.internal_llm_json_parse_failed operation=%s text=%s", operation, text[:400])
        return None


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
        raise ValueError("JSON payload is not an object")
    return parsed


def _is_gpt5_chat_model(model: str | None) -> bool:
    normalized = str(model or "").strip().casefold()
    return normalized.startswith("gpt-5")


async def _post_usage(
    *,
    cfg: EmailAgentConfig,
    http_client: httpx.AsyncClient,
    ok: bool,
    task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
    llm_call_id: str,
    model: str,
    operation: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    error: str | None,
) -> None:
    if not cfg.gateway_internal_token:
        return
    from datetime import datetime, timezone

    placed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata: dict[str, Any] = {"channel": channel, "source": source}
    if error:
        metadata["error"] = error
    event = UsageEvent(
        llm_call_id=llm_call_id,
        user_id=None,
        source_component="cosmic/email-agent:1.0.0",
        source_id=source_id,
        task_id=task_id,
        plan_id=None,
        parent_task_id=None,
        session_id=session_id,
        route="email",
        operation=operation,
        usage_kind="llm",
        provider="internal_llm",
        model=model,
        request_id=request_id,
        provider_request_id=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=max(0, prompt_tokens + completion_tokens),
        cached_tokens=0,
        reasoning_tokens=0,
        estimated_cost_usd=None,
        latency_ms=latency_ms,
        success=ok,
        error_code=None if ok else "LLM_ERROR",
        metadata_json=serialize_usage_metadata(metadata),
        llm_call_placed_at=placed_at,
    )
    await post_usage_event(
        client=http_client,
        gateway_url=cfg.gateway_url,
        internal_token=cfg.gateway_internal_token,
        event=event,
    )
