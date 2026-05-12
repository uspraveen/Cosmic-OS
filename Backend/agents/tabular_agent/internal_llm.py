"""internal LLM-v2-pro via LangChain OpenAI-compatible client + Gateway usage logging."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

import httpx

from shared.usage import UsageEvent, post_usage_event, serialize_usage_metadata

from .config import TabularAgentConfig

logger = logging.getLogger(__name__)


async def invoke_tabular_internal_llm(
    *,
    cfg: TabularAgentConfig,
    http_client: httpx.AsyncClient,
    system_content: str,
    user_message: str,
    task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
    operation: str = "tabular.internal_llm",
    max_output_chars: int = 24_000,
    temperature: float = 0.2,
) -> str | None:
    """Call internal LLM with a dedicated httpx client (http2=False). Posts usage to Gateway when configured."""
    if not cfg.enable_internal_llm or not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage
    except Exception as exc:
        logger.warning("tabular_agent.langchain_unavailable: %s", exc)
        return None

    base_url = cfg.internal_llm_base_url
    messages = [
        SystemMessage(content=system_content.strip()),
        HumanMessage(content=user_message[:120_000]),
    ]
    started = time.perf_counter()
    llm_call_id = f"tabular_internal_llm_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.internal_llm_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as llm_http:
            llm = ChatOpenAI(
                model=cfg.internal_llm_model,
                api_key=cfg.internal_llm_api_key,
                base_url=base_url,
                temperature=temperature,
                http_async_client=llm_http,
            )
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
        logger.warning("tabular_agent.internal_llm_invoke_failed: %s", exc)
        return None

    text = getattr(result, "content", None) or str(result)
    usage = getattr(result, "usage_metadata", None) or {}
    if not usage and isinstance(getattr(result, "response_metadata", None), dict):
        rm = result.response_metadata  # type: ignore[union-attr]
        usage = rm.get("token_usage") if isinstance(rm, dict) else {}
    if not isinstance(usage, dict):
        usage = {}

    pt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) if isinstance(usage, dict) else 0
    ct = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0) if isinstance(usage, dict) else 0

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
        prompt_tokens=pt,
        completion_tokens=ct,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error=None,
    )
    return str(text).strip()[:max_output_chars] or None


async def maybe_enrich_preview_summary(
    *,
    cfg: TabularAgentConfig,
    http_client: httpx.AsyncClient,
    preview_excerpt: str,
    task_id: str | None,
    session_id: str | None,
    request_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
) -> str | None:
    from .prompt_assets import build_internal_context

    system_parts = [
        "You are the COSMIC tabular specialist. Summarize spreadsheet structure for the orchestrator. "
        "Be concise (under 12 lines). Do not invent numbers; only reflect what is in the excerpt.",
        build_internal_context(
            "summarize",
            include_fpna=cfg.include_financial_fpna_prompt,
        ),
    ]
    system_content = "\n\n".join(p.strip() for p in system_parts if p and str(p).strip())
    return await invoke_tabular_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=system_content,
        user_message=f"Workbook preview excerpt:\n\n{preview_excerpt[:12000]}",
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        source=source,
        source_id=source_id,
        channel=channel,
        operation="tabular.internal_llm.parse_preview",
        max_output_chars=8000,
        temperature=0.2,
    )


async def _post_usage(
    *,
    cfg: TabularAgentConfig,
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
    meta: dict[str, Any] = {"channel": channel}
    if error:
        meta["error"] = error
    event = UsageEvent(
        llm_call_id=llm_call_id,
        user_id=None,
        source_component="cosmic/tabular-agent:1.0.0",
        source_id=source_id,
        task_id=task_id,
        plan_id=None,
        parent_task_id=None,
        session_id=session_id,
        route="tabular",
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
        metadata_json=serialize_usage_metadata(meta),
        llm_call_placed_at=placed_at,
    )
    await post_usage_event(
        client=http_client,
        gateway_url=cfg.gateway_url,
        internal_token=cfg.gateway_internal_token,
        event=event,
    )
