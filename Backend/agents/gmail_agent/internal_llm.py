"""Internal LLM helpers for Gmail triage and drafting."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import GmailAgentConfig


TRIAGE_SYSTEM_PROMPT = """You are COSMIC's Gmail triage specialist.
Classify inbox items with judgment, not regex. Treat spam/noise as a semantic decision.
Use thread context, sender relationship, time sensitivity, and memory context when available. Surface direct asks, approvals, scheduling pressure, important people, customer/founder/investor/school/work messages, security/account issues, and receipts tied to active projects. Prefer not surfacing routine newsletters, promotions, login codes, social alerts, and automated bulk messages unless they are clearly important to the user's active goals.
Return strict JSON only."""

DRAFT_SYSTEM_PROMPT = """You are COSMIC's Gmail drafting specialist.
Draft concise, context-aware email replies. Preserve the user's intent and do not invent facts.
Return strict JSON only."""


async def invoke_gmail_triage_llm(
    *,
    cfg: GmailAgentConfig,
    http_client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
    context_brief: str = "",
    memory_context: str = "",
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
    context_brief: str = "",
    memory_context: str = "",
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
    )
    return raw


async def _chat_json(
    *,
    cfg: GmailAgentConfig,
    http_client: httpx.AsyncClient,
    system_prompt: str,
    user_payload: dict[str, Any],
    temperature: float,
) -> dict[str, Any]:
    if not cfg.enable_internal_llm:
        raise RuntimeError("Gmail internal LLM is disabled.")
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        raise RuntimeError("Gmail internal LLM is not configured.")
    url = f"{cfg.internal_llm_base_url.rstrip('/')}/chat/completions"
    response = await http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {cfg.internal_llm_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.internal_llm_model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        },
        timeout=cfg.internal_llm_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
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
