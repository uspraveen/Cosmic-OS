"""Internal LLM helpers for Gmail triage and drafting."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from shared import normalized_reasoning_effort
from shared.usage import begin_metered_call, build_model_key, build_usage_event, post_usage_event, serialize_usage_metadata

from .config import GmailAgentConfig

logger = logging.getLogger(__name__)


TRIAGE_SYSTEM_PROMPT = """You are COSMIC's Gmail triage specialist.
Classify inbox items with judgment, not regex. Treat spam/noise as a semantic decision.
Use thread context, sender relationship, time sensitivity, and memory context when available. Surface direct asks, approvals, scheduling pressure, important people, customer/founder/investor/school/work messages, security/account issues, and receipts tied to active projects. Prefer not surfacing routine newsletters, promotions, login codes, social alerts, and automated bulk messages unless they are clearly important to the user's active goals.
Return strict JSON only."""

DRAFT_SYSTEM_PROMPT = """You are COSMIC's Gmail drafting specialist.

WHO YOU ARE WRITING AS
You draft outgoing mail from the mailbox given as `account_email`. Every draft you produce is authored by that mailbox's side of the conversation. Never write in the voice of another participant in the thread, and never sign as someone else.

WHAT `request` IS
`request` is an instruction from COSMIC describing the message to write. It is never an incoming message, so never answer it. If it already reads as a finished email, reproduce it faithfully rather than replying to it.

WHAT `thread` IS
`thread` is background context only. Its most recent message is not automatically the message you are answering -- only `request` decides what to write.

RECIPIENTS
Prefer the recipients named in `request`. Never address a draft to an automated no-reply, do-not-reply, mailer-daemon, or postmaster mailbox. If you cannot identify a real recipient, return an empty `to` and say so in `notes`.

Draft concise, context-aware email bodies. Preserve the requested intent and do not invent facts. Keep `notes` consistent with the fields you actually return.
Return strict JSON only."""


async def invoke_gmail_triage_llm(
    *,
    cfg: GmailAgentConfig,
    http_client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
    context_brief: str = "",
    memory_context: str = "",
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = {
        "items": [
            {
                "message_id": "string",
                "thread_id": "string",
                "category": "urgent|needs_reply|needs_review|read_later|notification|spam_or_noise",
                "confidence": 0.0,
                "priority": 0,
                "reason": "short reason",
                "surface_to_user": True,
                "suggested_action": "short action",
                "prefilter_sender": False,
                "prefilter_domain": False,
            }
        ],
        "summary": "short digest summary",
    }
    user_payload = {
        "context_brief": context_brief,
        "memory_context": memory_context,
        "messages": messages,
        "required_schema": schema,
    }
    raw = await _chat_json(
        cfg=cfg,
        http_client=http_client,
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        user_payload=user_payload,
        temperature=0.1,
        operation="gmail.internal_llm.triage",
        task_context=task_context,
    )
    if not isinstance(raw.get("items"), list):
        raw["items"] = []
    return raw


async def invoke_gmail_draft_llm(
    *,
    cfg: GmailAgentConfig,
    http_client: httpx.AsyncClient,
    request: str,
    thread: dict[str, Any] | None = None,
    account_email: str = "",
    context_brief: str = "",
    memory_context: str = "",
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = {
        "subject": "string",
        "body": "plain text email body",
        "to": ["email"],
        "cc": ["email"],
        "bcc": ["email"],
        "notes": "short note about assumptions",
    }
    user_payload = {
        "account_email": account_email,
        "request": request,
        "context_brief": context_brief,
        "memory_context": memory_context,
        "thread": thread or {},
        "required_schema": schema,
    }
    raw = await _chat_json(
        cfg=cfg,
        http_client=http_client,
        system_prompt=DRAFT_SYSTEM_PROMPT,
        user_payload=user_payload,
        temperature=0.25,
        operation="gmail.internal_llm.draft",
        task_context=task_context,
    )
    return raw


async def _chat_json(
    *,
    cfg: GmailAgentConfig,
    http_client: httpx.AsyncClient,
    system_prompt: str,
    user_payload: dict[str, Any],
    temperature: float,
    operation: str,
    task_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not cfg.enable_internal_llm:
        raise RuntimeError("Gmail internal LLM is disabled.")
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        raise RuntimeError("Gmail internal LLM is not configured.")
    url = f"{cfg.internal_llm_base_url.rstrip('/')}/chat/completions"
    request_body: dict[str, Any] = {
        "model": cfg.internal_llm_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ],
    }
    if not _is_gpt5_chat_model(cfg.internal_llm_model):
        request_body["temperature"] = temperature
    effort = normalized_reasoning_effort(cfg.internal_llm_model, cfg.internal_llm_reasoning_effort)
    if effort is not None:
        request_body["reasoning_effort"] = effort
    metered = begin_metered_call(prefix="gmail_llm")
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
            operation=operation,
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
        operation=operation,
        raw_usage=payload.get("usage"),
        task_context=task_context,
        success=True,
        error_code=None,
        latency_ms=int((time.perf_counter() - started) * 1000),
        provider_request_id=str(payload.get("id") or "").strip() or None,
        metadata={"response_model": payload.get("model")},
    )
    content = (
        ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        or "{}"
    )
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gmail internal LLM returned invalid JSON: {content[:200]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Gmail internal LLM returned a non-object JSON value.")
    return decoded


async def _post_usage(
    *,
    cfg: GmailAgentConfig,
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
    model_key = build_model_key(provider, cfg.internal_llm_model)
    event = build_usage_event(
        metered_call=metered_call,
        source_component="agent",
        source_id="cosmic/gmail-agent:1.0.0",
        task_id=_optional_text(context.get("task_id")),
        parent_task_id=_optional_text(context.get("parent_task_id")),
        session_id=_optional_text(context.get("session_id")),
        route="gmail",
        operation=operation,
        model_key=model_key,
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
        logger.debug("gmail_agent.usage_post_failed operation=%s", operation, exc_info=True)


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
