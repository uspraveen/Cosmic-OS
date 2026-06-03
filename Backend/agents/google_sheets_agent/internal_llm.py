"""Internal LLM planner for the Google Sheets specialist."""

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

from .config import GoogleSheetsAgentConfig

logger = logging.getLogger(__name__)


SHEETS_PLANNER_SYSTEM_PROMPT = """You are COSMIC's Google Sheets specialist planner.

You are a principal data architect and spreadsheet automation engineer. You
reason over live workbook structure, tabs, ranges, headers, permissions, and
user intent, then return a precise structured plan for the deterministic Google
Sheets executor.

Core behavior:
- Think structurally. A spreadsheet is tabs, headers, ranges, formulas, formats,
  validation, permissions, and audit history.
- Check before acting. Prefer resolving or reading workbook state before
  surgical edits, especially if a tab/range/header is mentioned.
- Preserve user intent while making the result native to Google Sheets.
- Tables, trackers, matrices, schedules, budgets, outreach lists, CRM-like
  pipelines, and reports should become real cell grids.
- Use update_cells only when the target range is clear.
- Use append_rows for adding new records under existing headers.
- Use add_sheet before writing to a new tab.
- Use format_header_row when the first row should be frozen/bold/colored.
- Never make a file public, domain-visible, or grant writer/commenter access
  unless approval_confirmed=true after explicit user approval.
- If the request is underspecified, return needs_clarification=true instead of
  guessing dangerously.

Return strict JSON only with this shape:
{
  "intent": "sheets.resolve_resource|sheets.create|sheets.read|sheets.edit",
  "operation": "resolve_resource|create|read_structure|read_range|update_cells|append_rows|add_sheet|format_header_row|clear_range|share_file|list_permissions|get_link",
  "params": {},
  "confidence": 0.0,
  "needs_clarification": false,
  "clarifying_question": "",
  "needs_approval": false,
  "approval_reason": "",
  "reasoning": "brief operational rationale"
}

Supported params include:
- resolve: query, spreadsheet_id, resource_hint
- create: title, sheets=[{title, values, has_header}], values, range, has_header
- read_structure: spreadsheet_id
- read_range: spreadsheet_id, range, sheet_name
- update_cells: spreadsheet_id, range, values
- append_rows: spreadsheet_id, range, values
- add_sheet: spreadsheet_id, title, row_count, column_count
- format_header_row: spreadsheet_id, sheet_name, range, background_color
- clear_range: spreadsheet_id, range
- sharing: spreadsheet_id, role, type, email_address, domain, send_notification_email, approval_confirmed
"""


async def invoke_google_sheets_planner_llm(
    *,
    cfg: GoogleSheetsAgentConfig,
    http_client: httpx.AsyncClient,
    user_payload: dict[str, Any],
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not cfg.enable_internal_llm:
        raise RuntimeError("Google Sheets internal LLM is disabled.")
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        raise RuntimeError("Google Sheets internal LLM is not configured.")

    request_body: dict[str, Any] = {
        "model": cfg.internal_llm_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SHEETS_PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    if not _is_gpt5_chat_model(cfg.internal_llm_model):
        request_body["temperature"] = 0.12

    url = f"{cfg.internal_llm_base_url.rstrip('/')}/chat/completions"
    metered = begin_metered_call(prefix="google_sheets_llm")
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
            operation="google_sheets.internal_llm.plan",
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
        operation="google_sheets.internal_llm.plan",
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
        raise RuntimeError(f"Google Sheets internal LLM returned invalid JSON: {content[:300]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Google Sheets internal LLM returned a non-object JSON value.")
    params = decoded.get("params")
    if not isinstance(params, dict):
        decoded["params"] = {}
    return decoded


async def _post_usage(
    *,
    cfg: GoogleSheetsAgentConfig,
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
        source_id="cosmic/google-sheets-agent:1.0.0",
        task_id=_optional_text(context.get("task_id")),
        parent_task_id=_optional_text(context.get("parent_task_id")),
        session_id=_optional_text(context.get("session_id")),
        route="google_sheets",
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
        logger.debug("google_sheets_agent.internal_llm_usage_post_failed operation=%s", operation, exc_info=True)


def _infer_provider(base_url: str, model: str) -> str:
    normalized = str(base_url or "").lower()
    model_text = str(model or "").lower()
    if "api.openai.com" in normalized or model_text.startswith("gpt-"):
        return "openai"
    if "fireworks" in normalized:
        return "fireworks"
    return "openai_compatible"


def _is_gpt5_chat_model(model: str) -> bool:
    normalized = str(model or "").lower()
    return normalized.startswith("gpt-5")


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

