"""Calendar-agent internal LLM via LangChain OpenAI-compatible client + Gateway usage logging.

Uses gpt-5-mini for natural language scheduling intent parsing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

import httpx

from shared.usage import UsageEvent, post_usage_event, serialize_usage_metadata

from .config import CalendarAgentConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a calendar scheduling assistant. Parse the user's natural language request into a
structured JSON execution plan for Google Calendar operations.

Output ONLY valid JSON with this shape:
{
  "operation": "list" | "create" | "find_free" | "update" | "cancel",
  "params": {
    // operation-specific parameters:
    // list:    { "time_range": { "start": ISO, "end": ISO }, "search_query": str|null, "calendar_id": str|null,
    //            "account_hint": str|null }
    // create:  { "summary": str, "start": ISO, "end": ISO, "timezone": str, "attendees": [email],
    //            "location": str, "description": str, "is_all_day": bool, "reminders": [min],
    //            "add_google_meet": bool, "calendar_id": str|null, "account_hint": str|null }
    // find_free: { "duration_min": int, "date_range": { "start": ISO, "end": ISO },
    //              "timezone": str, "working_hours": { "start": int, "end": int },
    //              "calendar_ids": [str], "account_hint": str|null }
    // update:  { "event_id": str|null, "event_query": str|null, "calendar_id": str|null,
    //            "account_hint": str|null, "time_range": { "start": ISO, "end": ISO }|null,
    //            "patch": { "summary": str?, "description": str?, "location": str?, "start": ISO?, "end": ISO?,
    //                       "timezone": str?, "is_all_day": bool?, "attendees": [email]?, "reminders": [min]?,
    //                       "add_google_meet": bool? } }
    // cancel:  { "event_id": str|null, "event_query": str|null, "calendar_id": str|null,
    //            "account_hint": str|null, "time_range": { "start": ISO, "end": ISO }|null,
    //            "notify_attendees": bool }
  },
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}

Rules:
- ISO means ISO 8601 datetime string.
- If the user says "tomorrow" or "next week", compute from the current date context.
- If duration is unspecified for a meeting, default to 30 minutes.
- If no timezone is specified, use the default.
- Keep `account_hint` human-friendly. Good examples: "work", "personal", "alex@company.com". Do not invent internal account ids.
- For "schedule a meeting with X", operation=create.
- For "what do I have" / "show my calendar", operation=list.
- Set `search_query` only when the user is actually searching by title or keyword. Do not copy the whole natural-language agenda request into `search_query`.
- For "find a 30 minute slot", operation=find_free.
- For "change the title" / "move to 3pm", operation=update.
- For "cancel" / "delete the event", operation=cancel.
- If the user asks for a Google Meet / Meet link / video conference, set `add_google_meet=true` on create or inside the update patch.
- For update/cancel without a raw event_id, provide the best available `event_query` and a narrow `time_range` that helps bounded event lookup.
- If the user implies a specific account like "work" or "personal", set `account_hint`.
- If the user implies multiple calendars for availability, populate `calendar_ids`.
- Keep patches minimal. Only include fields the user actually wants changed.
- Set confidence < 0.7 if the request is ambiguous and would still need user clarification after bounded lookup.
"""


async def invoke_calendar_mimo(
    *,
    cfg: CalendarAgentConfig,
    http_client: httpx.AsyncClient,
    user_message: str,
    task_id: str | None,
    session_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
    current_time_iso: str = "",
    timezone: str = "",
) -> dict[str, Any] | None:
    """Parse natural language into a structured calendar operation plan.

    Returns dict with operation/params/confidence, or None if LLM unavailable/fails.
    """
    if not cfg.enable_internal_llm or not cfg.mimo_api_key or not cfg.mimo_base_url:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("calendar_agent.langchain_unavailable: %s", exc)
        return None

    context_block = ""
    if current_time_iso:
        context_block += f"\nCurrent time (UTC): {current_time_iso}"
    if timezone:
        context_block += f"\nUser timezone: {timezone}"
    context_block += (
        f"\nDefault event duration: {cfg.default_event_duration_min} minutes"
    )
    context_block += f"\nDefault working hours: {cfg.working_hour_start}:00-{cfg.working_hour_end}:00"

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT.strip()),
        HumanMessage(
            content=f"{user_message[:80_000]}\n\n---\nContext:{context_block}"
        ),
    ]

    started = time.perf_counter()
    llm_call_id = f"cal_mimo_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.mimo_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as mimo_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.mimo_model,
                "api_key": cfg.mimo_api_key,
                "base_url": cfg.mimo_base_url,
                "http_async_client": mimo_http,
            }
            llm = ChatOpenAI(**llm_kwargs)
            result = await llm.ainvoke(messages)
    except Exception as exc:
        logger.warning("calendar_agent.internal_llm_error: %s", exc)
        return None

    latency_ms = (time.perf_counter() - started) * 1000
    raw_content = (result.content or "").strip() if result else ""

    # Extract usage metadata for logging
    usage_meta = {}
    if result:
        usage_meta = serialize_usage_metadata(result)

    # Post usage event
    try:
        await post_usage_event(
            http_client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=UsageEvent(
                llm_call_id=llm_call_id,
                llm_call_placed_at=time.time() - (latency_ms / 1000),
                agent_id="cosmic/calendar-agent:1.0.0",
                task_id=task_id or "",
                session_id=session_id or "",
                source=source or "",
                source_id=source_id or "",
                channel=channel or "",
                provider="openai_compatible",
                model=cfg.mimo_model,
                usage_kind="chat_completion",
                ok=True,
                latency_ms=latency_ms,
                usage=usage_meta,
            ),
        )
    except Exception:
        pass

    if not raw_content:
        return None

    # Parse JSON from response (handle markdown code blocks)
    json_str = raw_content
    if "```" in json_str:
        parts = json_str.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                json_str = stripped
                break

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, dict) and "operation" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    return None
