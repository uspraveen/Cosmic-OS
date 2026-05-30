"""Internal LLM planner for the Google Docs specialist.

This is the intelligence layer above the deterministic Google Docs/Drive API
executor. It turns high-level document requests into structured COSMIC Docs
operations while preserving the executor's revision guards and approval gates.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from shared.usage import (
    begin_metered_call,
    build_model_key,
    build_usage_event,
    post_usage_event,
    serialize_usage_metadata,
)

from .config import GoogleDocsAgentConfig

logger = logging.getLogger(__name__)


DOCS_PLANNER_SYSTEM_PROMPT = """You are COSMIC's Google Docs specialist planner.

You are not a generic text formatter. You reason over live Google Docs state,
Drive metadata, comments, permissions, tables, images, and user intent, then
produce a precise structured plan for the deterministic Google Docs executor.

Your behavior mirrors COSMIC's older LangGraph Docs agent:
- Check before acting. Prefer reading or resolving a document before edits.
- Preserve user intent while making documents polished and native to Google Docs.
- Use markdown-like content only because the executor converts it into native Docs styling.
- Prefer block-targeted edits when the current document structure is available.
- Use expected snippets for safety when updating existing content.
- Never make a file public or grant writer/commenter access unless approval is explicit.
- If the user asks about comments and no comments are provided, remember that suggestions are different from comments; say when the executor cannot inspect them yet.
- Tables should be real Google Docs tables, not markdown pipe text.
- Images must use public http(s) URLs that Google Docs can fetch.
- If the request is underspecified, return needs_clarification=true instead of guessing dangerously.

Return strict JSON only with this shape:
{
  "intent": "docs.resolve_resource|docs.create|docs.read|docs.edit",
  "operation": "resolve_resource|create|read|overwrite_doc|replace_text|update_block|insert_table|insert_image|list_comments|add_comment|reply_to_comment|resolve_comment|reopen_comment|share_file|list_permissions|get_link",
  "params": {},
  "confidence": 0.0,
  "needs_clarification": false,
  "clarifying_question": "",
  "needs_approval": false,
  "approval_reason": "",
  "reasoning": "brief operational rationale"
}

Supported params include:
- resolve/read: query, document_id, resource_hint, include_comments
- create: title, body_markdown
- overwrite_doc: document_id, full_markdown_text
- replace_text: document_id, old_text, new_text
- update_block: document_id, block_id, expected_snippet, new_text
- insert_table: document_id, data, after_text, has_header
- insert_image: document_id, image_url, after_text, width_pt, height_pt
- comments: document_id, comment_id, content, quoted_text
- sharing: document_id, role, type, email_address, domain, send_notification_email, approval_confirmed
"""


async def invoke_google_docs_planner_llm(
    *,
    cfg: GoogleDocsAgentConfig,
    http_client: httpx.AsyncClient,
    user_payload: dict[str, Any],
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not cfg.enable_internal_llm:
        raise RuntimeError("Google Docs internal LLM is disabled.")
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        raise RuntimeError("Google Docs internal LLM is not configured.")

    request_body: dict[str, Any] = {
        "model": cfg.internal_llm_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": DOCS_PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    if not _is_gpt5_chat_model(cfg.internal_llm_model):
        request_body["temperature"] = 0.15

    url = f"{cfg.internal_llm_base_url.rstrip('/')}/chat/completions"
    metered = begin_metered_call(prefix="google_docs_llm")
    provider = _infer_provider(cfg.internal_llm_base_url, cfg.internal_llm_model)
    started = time.perf_counter()
    try:
        response = await http_client.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg.internal_llm_api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=cfg.internal_llm_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        await _post_usage(
            cfg=cfg,
            http_client=http_client,
            metered_call=metered,
            provider=provider,
            operation="google_docs.internal_llm.plan",
            raw_usage=None,
            task_context=task_context,
            success=False,
            error_code=exc.__class__.__name__,
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_request_id=None,
            metadata={"error": str(exc)[:300]},
        )
        raise

    await _post_usage(
        cfg=cfg,
        http_client=http_client,
        metered_call=metered,
        provider=provider,
        operation="google_docs.internal_llm.plan",
        raw_usage=payload.get("usage"),
        task_context=task_context,
        success=True,
        error_code=None,
        latency_ms=int((time.perf_counter() - started) * 1000),
        provider_request_id=str(payload.get("id") or "").strip() or None,
        metadata={"response_model": payload.get("model")},
    )

    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}").strip()
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Google Docs internal LLM returned invalid JSON: {content[:300]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Google Docs internal LLM returned a non-object JSON value.")
    params = decoded.get("params")
    if not isinstance(params, dict):
        decoded["params"] = {}
    return decoded


async def _post_usage(
    *,
    cfg: GoogleDocsAgentConfig,
    http_client: httpx.AsyncClient,
    metered_call,
    provider: str,
    operation: str,
    raw_usage: Any,
    task_context: dict[str, Any] | None,
    success: bool,
    error_code: str | None,
    latency_ms: int,
    provider_request_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not cfg.gateway_internal_token:
        return
    context = task_context if isinstance(task_context, dict) else {}
    event = build_usage_event(
        metered_call=metered_call,
        source_component="agent",
        source_id="cosmic/google-docs-agent:1.0.0",
        task_id=_optional_text(context.get("task_id")),
        parent_task_id=_optional_text(context.get("parent_task_id")),
        session_id=_optional_text(context.get("session_id")),
        route="google_docs",
        operation=operation,
        model_key=build_model_key(provider, cfg.internal_llm_model),
        provider=provider,
        model=cfg.internal_llm_model,
        usage_kind="chat_completion",
        request_id=_optional_text(context.get("request_id")),
        provider_request_id=provider_request_id,
        raw_usage=raw_usage,
        success=success,
        error_code=error_code,
        latency_ms=latency_ms,
        metadata_json=serialize_usage_metadata(
            {
                "channel": context.get("channel"),
                "source": context.get("source"),
                "source_id": context.get("source_id"),
                **(metadata or {}),
            }
        ),
    )
    try:
        await post_usage_event(
            client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=event,
        )
    except Exception:
        logger.debug("google_docs_agent.internal_llm_usage_post_failed operation=%s", operation, exc_info=True)


def _infer_provider(base_url: str, model: str) -> str:
    normalized = str(base_url or "").lower()
    model_text = str(model or "").lower()
    if "api.openai.com" in normalized or model_text.startswith("gpt-"):
        return "openai"
    if "fireworks.ai" in normalized:
        return "fireworks"
    if "api.x.ai" in normalized:
        return "xai"
    if "api.groq.com" in normalized:
        return "groq"
    if "perplexity.ai" in normalized:
        return "perplexity"
    return "openai_compatible"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_gpt5_chat_model(model: str | None) -> bool:
    return str(model or "").strip().casefold().startswith("gpt-5")
