"""Bounded LangGraph workflow for calendar specialist intents.

This keeps multi-step calendar execution inside the specialist without turning the
agent into a generic orchestration layer. The graph normalizes one request,
resolves targets when needed, checks conflicts, performs the Google Calendar
operation, and then stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from shared.contracts import AgentError, AgentResult, TaskEnvelope

logger = logging.getLogger(__name__)


class CalendarWorkflowState(TypedDict, total=False):
    intent: str
    tool_round: int
    max_tool_rounds: int
    next_action: str
    agent_result: AgentResult | None
    llm_result: dict[str, Any] | None
    llm_params: dict[str, Any] | None
    listed_done: bool
    normalized_done: bool
    busy_queried: bool
    free_slots_computed: bool
    create_conflict_checked: bool
    update_conflict_checked: bool
    resolved_done: bool
    created_done: bool
    updated_done: bool
    cancelled_done: bool
    calendar_id: str
    calendar_ids: list[str]
    query: str
    time_min: str
    time_max: str
    max_results: int
    summary: str
    start: str
    end: str
    timezone: str
    is_all_day: bool
    description: str
    location: str
    attendees: list[str]
    reminders: list[int] | None
    duration_min: int
    date_range_start: str
    date_range_end: str
    working_start: int
    working_end: int
    buffer_min: int
    patch: dict[str, Any]
    event_id: str
    notify_attendees: bool
    conflicts: list[dict[str, Any]]
    busy_periods: dict[str, list[dict[str, str]]]
    free_slots: list[dict[str, Any]]
    matched_event: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    ambiguity_query: str


@dataclass(frozen=True, slots=True)
class _GraphCtx:
    agent: Any
    task: TaskEnvelope


def _result_error(*, code: str, message: str, retryable: bool = False, next_action: str = "escalate") -> AgentResult:
    return AgentResult(
        status="failed",
        output={},
        artifacts=[],
        error=AgentError(
            code=code,
            retryable=retryable,
            message=message,
            next_action=next_action,
        ),
    )


def _bump_round(state: CalendarWorkflowState, *, action: str) -> dict[str, Any]:
    rounds = int(state.get("tool_round") or 0) + 1
    max_rounds = int(state.get("max_tool_rounds") or 6)
    if rounds > max_rounds:
        return {
            "tool_round": rounds,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INTERNAL_ERROR",
                retryable=False,
                message=f"Calendar workflow exceeded max tool rounds while running {action}.",
            ),
        }
    return {"tool_round": rounds}


def _default_list_window() -> tuple[str, str]:
    now = datetime.now(tz=timezone.utc)
    time_min = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_after = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return time_min.isoformat(), month_after.isoformat()


def _default_find_free_window() -> tuple[str, str]:
    now = datetime.now(tz=timezone.utc)
    return now.isoformat(), (now + timedelta(days=7)).isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _normalize_request(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    agent = ctx.agent
    task = ctx.task
    raw = dict(task.input)
    intent = str(state["intent"])
    tz = str(raw.get("timezone") or agent._cfg.default_timezone).strip() or agent._cfg.default_timezone  # noqa: SLF001
    llm_result = None
    llm_params: dict[str, Any] | None = None

    if raw.get("query") or raw.get("description") or intent in {"calendar.create_event", "calendar.update_event", "calendar.cancel_event"}:
        llm_result = await agent._parse_natural_language(task)  # noqa: SLF001
        if isinstance(llm_result, dict):
            llm_params = llm_result.get("params") if isinstance(llm_result.get("params"), dict) else None

    steps = agent._workflow_steps_for_intent(intent)  # noqa: SLF001
    await agent._maybe_create_plan(task, steps)  # noqa: SLF001
    if steps:
        await agent._maybe_update_step(1, "completed", "Calendar request normalized.")  # noqa: SLF001

    updates: CalendarWorkflowState = {
        "normalized_done": True,
        "llm_result": llm_result,
        "llm_params": llm_params,
        "timezone": tz,
        "calendar_id": str(raw.get("calendar_id") or "primary").strip() or "primary",
        "calendar_ids": list(raw.get("calendar_ids") or ["primary"]),
    }

    if intent == "calendar.list_events":
        time_min = str(raw.get("time_min") or "").strip()
        time_max = str(raw.get("time_max") or "").strip()
        query = str(raw.get("query") or "").strip()
        max_results = min(_safe_int(raw.get("max_results", agent._cfg.max_events_per_list), agent._cfg.max_events_per_list), 250)  # noqa: SLF001
        if isinstance(llm_params, dict) and llm_result and llm_result.get("operation") == "list":
            time_range = llm_params.get("time_range") if isinstance(llm_params.get("time_range"), dict) else {}
            time_min = time_min or str(time_range.get("start") or "").strip()
            time_max = time_max or str(time_range.get("end") or "").strip()
            query = query or str(llm_params.get("search_query") or "").strip()
            updates["calendar_id"] = str(llm_params.get("calendar_id") or updates["calendar_id"]).strip() or "primary"
        if not time_min or not time_max:
            time_min, time_max = _default_list_window()
        updates.update({
            "time_min": time_min,
            "time_max": time_max,
            "query": query,
            "max_results": max_results,
        })
        return updates

    if intent == "calendar.create_event":
        summary = str(raw.get("summary") or "").strip()
        start = str(raw.get("start") or "").strip()
        end = str(raw.get("end") or "").strip()
        is_all_day = bool(raw.get("is_all_day", False))
        description = str(raw.get("description") or "").strip()
        location = str(raw.get("location") or "").strip()
        attendees = _coerce_attendee_emails(raw.get("attendees"))
        reminders = _coerce_int_list(raw.get("reminders"))
        if isinstance(llm_params, dict) and llm_result and llm_result.get("operation") == "create":
            summary = summary or str(llm_params.get("summary") or "").strip()
            start = start or str(llm_params.get("start") or "").strip()
            end = end or str(llm_params.get("end") or "").strip()
            is_all_day = bool(llm_params.get("is_all_day", is_all_day))
            description = description or str(llm_params.get("description") or "").strip()
            location = location or str(llm_params.get("location") or "").strip()
            attendees = attendees or _coerce_attendee_emails(llm_params.get("attendees"))
            if reminders is None:
                reminders = _coerce_int_list(llm_params.get("reminders"))
            updates["timezone"] = str(llm_params.get("timezone") or updates["timezone"]).strip() or tz
            updates["calendar_id"] = str(llm_params.get("calendar_id") or updates["calendar_id"]).strip() or "primary"
        updates.update({
            "summary": summary,
            "start": start,
            "end": end,
            "is_all_day": is_all_day,
            "description": description,
            "location": location,
            "attendees": attendees,
            "reminders": reminders,
        })
        return updates

    if intent == "calendar.find_free_slots":
        date_range_start = str(raw.get("date_range_start") or "").strip()
        date_range_end = str(raw.get("date_range_end") or "").strip()
        duration_min = _safe_int(raw.get("duration_min", agent._cfg.default_event_duration_min), agent._cfg.default_event_duration_min)  # noqa: SLF001
        working_start = _safe_int(raw.get("working_hour_start", agent._cfg.working_hour_start), agent._cfg.working_hour_start)  # noqa: SLF001
        working_end = _safe_int(raw.get("working_hour_end", agent._cfg.working_hour_end), agent._cfg.working_hour_end)  # noqa: SLF001
        buffer_min = _safe_int(raw.get("buffer_min", agent._cfg.buffer_between_events_min), agent._cfg.buffer_between_events_min)  # noqa: SLF001
        if isinstance(llm_params, dict) and llm_result and llm_result.get("operation") == "find_free":
            date_range = llm_params.get("date_range") if isinstance(llm_params.get("date_range"), dict) else {}
            working_hours = llm_params.get("working_hours") if isinstance(llm_params.get("working_hours"), dict) else {}
            date_range_start = date_range_start or str(date_range.get("start") or "").strip()
            date_range_end = date_range_end or str(date_range.get("end") or "").strip()
            duration_min = _safe_int(llm_params.get("duration_min"), duration_min)
            working_start = _safe_int(working_hours.get("start"), working_start)
            working_end = _safe_int(working_hours.get("end"), working_end)
            if isinstance(llm_params.get("calendar_ids"), list) and llm_params.get("calendar_ids"):
                updates["calendar_ids"] = [str(item).strip() for item in llm_params["calendar_ids"] if str(item).strip()]
            updates["timezone"] = str(llm_params.get("timezone") or updates["timezone"]).strip() or tz
        if not date_range_start or not date_range_end:
            date_range_start, date_range_end = _default_find_free_window()
        updates.update({
            "date_range_start": date_range_start,
            "date_range_end": date_range_end,
            "duration_min": duration_min,
            "working_start": working_start,
            "working_end": working_end,
            "buffer_min": buffer_min,
        })
        return updates

    if intent == "calendar.update_event":
        event_id = str(raw.get("event_id") or "").strip()
        patch: dict[str, Any] = {}
        if "summary" in raw:
            patch["summary"] = raw["summary"]
        if "description" in raw:
            patch["description"] = raw["description"]
        if "location" in raw:
            patch["location"] = raw["location"]
        if "start" in raw:
            patch["start"] = raw["start"]
        if "end" in raw:
            patch["end"] = raw["end"]
        if "timezone" in raw:
            patch["timezone"] = raw["timezone"]
        if "is_all_day" in raw:
            patch["is_all_day"] = raw["is_all_day"]
        if "attendees" in raw:
            patch["attendees"] = raw["attendees"]
        if "reminders" in raw:
            patch["reminders"] = raw["reminders"]
        if isinstance(llm_params, dict) and llm_result and llm_result.get("operation") == "update":
            event_id = event_id or str(llm_params.get("event_id") or "").strip()
            updates["calendar_id"] = str(llm_params.get("calendar_id") or updates["calendar_id"]).strip() or "primary"
        patch = agent._merge_patch_from_params(patch, llm_params)  # noqa: SLF001
        patch = agent._normalize_patch(patch, timezone_name=str(raw.get("timezone") or updates["timezone"]))  # noqa: SLF001
        updates.update({
            "event_id": event_id,
            "patch": patch,
        })
        return updates

    if intent == "calendar.cancel_event":
        event_id = str(raw.get("event_id") or "").strip()
        notify_attendees = bool(raw.get("notify_attendees", True))
        if isinstance(llm_params, dict) and llm_result and llm_result.get("operation") == "cancel":
            event_id = event_id or str(llm_params.get("event_id") or "").strip()
            updates["calendar_id"] = str(llm_params.get("calendar_id") or updates["calendar_id"]).strip() or "primary"
            if "notify_attendees" in llm_params:
                notify_attendees = bool(llm_params.get("notify_attendees"))
        updates.update({
            "event_id": event_id,
            "notify_attendees": notify_attendees,
        })
        return updates

    return updates


def _route_after_decide(state: CalendarWorkflowState) -> str:
    return str(state.get("next_action") or "finish")


def _coerce_attendee_emails(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    emails: list[str] = []
    for item in value:
        if isinstance(item, str):
            email = item.strip()
            if email:
                emails.append(email)
        elif isinstance(item, dict):
            email = str(item.get("email") or "").strip()
            if email:
                emails.append(email)
    return emails


def _coerce_int_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    values: list[int] = []
    for item in value:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    return values


def _time_changed(patch: dict[str, Any]) -> bool:
    return "start" in patch or "end" in patch


def _decide_next_action(state: CalendarWorkflowState) -> CalendarWorkflowState:
    if state.get("agent_result") is not None:
        return {"next_action": "finish"}

    intent = str(state.get("intent") or "")

    if intent == "calendar.list_events":
        return {"next_action": "finish" if state.get("listed_done") else "list_events"}

    if intent == "calendar.find_free_slots":
        if not state.get("busy_queried"):
            return {"next_action": "query_busy"}
        if not state.get("free_slots_computed"):
            return {"next_action": "compute_free_slots"}
        return {"next_action": "finish"}

    if intent == "calendar.create_event":
        if not state.get("create_conflict_checked"):
            return {"next_action": "check_create_conflicts"}
        if not state.get("created_done"):
            return {"next_action": "create_event"}
        return {"next_action": "finish"}

    if intent == "calendar.update_event":
        if not str(state.get("event_id") or "").strip():
            return {"next_action": "resolve_target_event"}
        if _time_changed(state.get("patch") or {}) and not state.get("update_conflict_checked"):
            return {"next_action": "check_update_conflicts"}
        if not state.get("updated_done"):
            return {"next_action": "update_event"}
        return {"next_action": "finish"}

    if intent == "calendar.cancel_event":
        if not str(state.get("event_id") or "").strip():
            return {"next_action": "resolve_target_event"}
        if not state.get("cancelled_done"):
            return {"next_action": "cancel_event"}
        return {"next_action": "finish"}

    return {
        "next_action": "finish",
        "agent_result": _result_error(
            code="INVALID_INPUT",
            message=f"Unsupported calendar intent for LangGraph: {intent}",
        ),
    }


async def _list_events(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="list_events")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    client = agent._get_calendar_client()  # noqa: SLF001
    await agent._maybe_update_step(2, "in_progress", "Querying Google Calendar for matching events.")  # noqa: SLF001
    events = await client.list_events(
        calendar_id=str(state.get("calendar_id") or "primary"),
        time_min=str(state.get("time_min") or ""),
        time_max=str(state.get("time_max") or ""),
        max_results=int(state.get("max_results") or agent._cfg.max_events_per_list),  # noqa: SLF001
        query=str(state.get("query") or "") or None,
    )
    agent._save_session(task, "list", query=str(state.get("query") or ""), event_ids=[e["event_id"] for e in events])  # noqa: SLF001
    await agent._maybe_update_step(2, "completed", f"Found {len(events)} matching event(s).")  # noqa: SLF001
    await agent._maybe_update_step(3, "completed", "Calendar lookup summarized.")  # noqa: SLF001
    return {
        **round_update,
        "listed_done": True,
        "next_action": "finish",
        "agent_result": AgentResult(
            status="completed",
            output={
                "events": events,
                "count": len(events),
                "calendar_id": str(state.get("calendar_id") or "primary"),
                "time_min": str(state.get("time_min") or ""),
                "time_max": str(state.get("time_max") or ""),
                "workflow": "langgraph",
                "rounds_used": int(round_update["tool_round"]),
            },
            artifacts=[],
            error=None,
        ),
    }


async def _query_busy(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="query_busy")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    client = agent._get_calendar_client()  # noqa: SLF001
    await agent._maybe_update_step(2, "in_progress", "Querying busy periods across selected calendars.")  # noqa: SLF001
    busy_periods = await client.query_free_busy(
        calendar_ids=list(state.get("calendar_ids") or ["primary"]),
        time_min=str(state.get("date_range_start") or ""),
        time_max=str(state.get("date_range_end") or ""),
    )
    await agent._maybe_update_step(2, "completed", f"Queried busy periods across {len(busy_periods)} calendar(s).")  # noqa: SLF001
    return {
        **round_update,
        "busy_queried": True,
        "busy_periods": busy_periods,
    }


async def _compute_free_slots(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="compute_free_slots")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    await agent._maybe_update_step(3, "in_progress", "Computing free slots within working-hour rules.")  # noqa: SLF001

    all_busy: list[tuple[datetime, datetime]] = []
    for periods in dict(state.get("busy_periods") or {}).values():
        for period in periods:
            try:
                start = datetime.fromisoformat(str(period["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(period["end"]).replace("Z", "+00:00"))
            except Exception:
                continue
            all_busy.append((start, end))
    all_busy.sort(key=lambda item: item[0])

    range_start = datetime.fromisoformat(str(state.get("date_range_start") or "").replace("Z", "+00:00"))
    range_end = datetime.fromisoformat(str(state.get("date_range_end") or "").replace("Z", "+00:00"))
    duration = timedelta(minutes=int(state.get("duration_min") or agent._cfg.default_event_duration_min))  # noqa: SLF001
    buffer_td = timedelta(minutes=int(state.get("buffer_min") or agent._cfg.buffer_between_events_min))  # noqa: SLF001
    working_start = int(state.get("working_start") or agent._cfg.working_hour_start)  # noqa: SLF001
    working_end = int(state.get("working_end") or agent._cfg.working_hour_end)  # noqa: SLF001

    current = range_start
    free_slots: list[dict[str, Any]] = []
    while current + duration <= range_end:
        if current.weekday() >= 5:
            current = (current + timedelta(days=1)).replace(hour=working_start, minute=0, second=0, microsecond=0)
            continue
        if current.hour < working_start or current.hour >= working_end:
            current = current + timedelta(hours=1)
            continue

        slot_end = current + duration
        overlap = False
        for busy_start, busy_end in all_busy:
            buffered_start = busy_start - buffer_td
            buffered_end = busy_end + buffer_td
            if current < buffered_end and slot_end > buffered_start:
                overlap = True
                current = buffered_end
                break

        if not overlap:
            free_slots.append(
                {
                    "start": current.isoformat(),
                    "end": slot_end.isoformat(),
                    "duration_min": int(state.get("duration_min") or duration.total_seconds() / 60),
                }
            )
            if len(free_slots) >= 10:
                break
            current = slot_end + buffer_td

    agent._save_session(task, "find_free", query=f"{int(state.get('duration_min') or duration.total_seconds() / 60)}min slots", event_ids=[])  # noqa: SLF001
    await agent._maybe_update_step(3, "completed", f"Computed {len(free_slots)} free slot(s).")  # noqa: SLF001
    return {
        **round_update,
        "free_slots_computed": True,
        "free_slots": free_slots,
        "next_action": "finish",
        "agent_result": AgentResult(
            status="completed",
            output={
                "free_slots": free_slots,
                "count": len(free_slots),
                "duration_min": int(state.get("duration_min") or agent._cfg.default_event_duration_min),  # noqa: SLF001
                "date_range_start": str(state.get("date_range_start") or ""),
                "date_range_end": str(state.get("date_range_end") or ""),
                "timezone": str(state.get("timezone") or agent._cfg.default_timezone),  # noqa: SLF001
                "working_hours": {
                    "start": int(state.get("working_start") or agent._cfg.working_hour_start),  # noqa: SLF001
                    "end": int(state.get("working_end") or agent._cfg.working_hour_end),  # noqa: SLF001
                },
                "workflow": "langgraph",
                "rounds_used": int(round_update["tool_round"]),
            },
            artifacts=[],
            error=None,
        ),
    }


async def _resolve_target_event(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="resolve_target_event")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    client = agent._get_calendar_client()  # noqa: SLF001
    await agent._maybe_update_step(2, "in_progress", "Searching for the target event.")  # noqa: SLF001
    matched_event, candidates, query = await agent._resolve_target_event(  # noqa: SLF001
        client=client,
        task=task,
        calendar_id=str(state.get("calendar_id") or "primary"),
        params=state.get("llm_params"),
    )
    if matched_event is None:
        await agent._maybe_update_step(2, "skipped", "Target event could not be resolved.")  # noqa: SLF001
        return {
            **round_update,
            "next_action": "finish",
            "agent_result": agent._ambiguous_event_error(  # noqa: SLF001
                action="update" if state.get("intent") == "calendar.update_event" else "cancel",
                query=query,
                candidates=candidates,
            ),
        }
    event_id = str(matched_event.get("event_id") or "").strip()
    calendar_id = str(matched_event.get("calendar_id") or state.get("calendar_id") or "primary").strip() or "primary"
    await agent._maybe_update_step(2, "completed", f"Resolved event {event_id}.")  # noqa: SLF001
    return {
        **round_update,
        "resolved_done": True,
        "event_id": event_id,
        "calendar_id": calendar_id,
        "matched_event": matched_event,
        "candidates": candidates,
        "ambiguity_query": query,
    }


async def _check_create_conflicts(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="check_create_conflicts")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    client = agent._get_calendar_client()  # noqa: SLF001
    summary = str(state.get("summary") or "").strip()
    start = str(state.get("start") or "").strip()
    if not summary:
        return {**round_update, "next_action": "finish", "agent_result": _result_error(code="INVALID_INPUT", message="Event summary (title) is required.")}
    if not start:
        return {**round_update, "next_action": "finish", "agent_result": _result_error(code="INVALID_INPUT", message="Event start time is required.")}

    end = str(state.get("end") or "").strip()
    is_all_day = bool(state.get("is_all_day", False))
    timezone_name = str(state.get("timezone") or agent._cfg.default_timezone)  # noqa: SLF001
    if not end:
        if is_all_day:
            end_date = datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(days=1)
            end = end_date.strftime("%Y-%m-%d")
        else:
            end_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=agent._cfg.default_event_duration_min)  # noqa: SLF001
            end = end_dt.isoformat()

    await agent._maybe_update_step(2, "in_progress", "Scanning overlapping events.")  # noqa: SLF001
    try:
        existing = await client.list_events(
            calendar_id=str(state.get("calendar_id") or "primary"),
            time_min=start,
            time_max=end,
            max_results=5,
        )
        conflicts = [item for item in existing if item.get("status") != "cancelled"]
    except Exception:
        conflicts = []
    await agent._maybe_update_step(2, "completed", f"Found {len(conflicts)} overlapping event(s).")  # noqa: SLF001
    return {
        **round_update,
        "create_conflict_checked": True,
        "conflicts": conflicts,
        "end": end,
        "timezone": timezone_name,
    }


async def _create_event(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="create_event")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    client = agent._get_calendar_client()  # noqa: SLF001
    summary = str(state.get("summary") or "").strip()
    start = str(state.get("start") or "").strip()
    end = str(state.get("end") or "").strip()
    is_all_day = bool(state.get("is_all_day", False))
    timezone_name = str(state.get("timezone") or agent._cfg.default_timezone)  # noqa: SLF001
    start_dict = {"date": start} if is_all_day else {"dateTime": start, "timeZone": timezone_name}
    end_dict = {"date": end} if is_all_day else {"dateTime": end, "timeZone": timezone_name}

    await agent._maybe_update_step(3, "in_progress", "Creating event in Google Calendar.")  # noqa: SLF001
    event = await client.create_event(
        calendar_id=str(state.get("calendar_id") or "primary"),
        summary=summary,
        start=start_dict,
        end=end_dict,
        timezone=None if is_all_day else timezone_name,
        description=str(state.get("description") or ""),
        location=str(state.get("location") or ""),
        attendees=list(state.get("attendees") or []),
        reminders=state.get("reminders"),
    )
    agent._save_session(task, "create", query=summary, event_ids=[event["event_id"]])  # noqa: SLF001
    await agent._maybe_update_step(3, "completed", f"Created {event['event_id']}.")  # noqa: SLF001
    result: dict[str, Any] = {"event": event, "created": True, "workflow": "langgraph", "rounds_used": int(round_update["tool_round"])}
    conflicts = list(state.get("conflicts") or [])
    if conflicts:
        result["conflict_warning"] = f"{len(conflicts)} overlapping event(s) found in this time slot."
        result["conflicting_events"] = [{"event_id": c["event_id"], "summary": c.get("summary", "")} for c in conflicts]
    return {**round_update, "created_done": True, "next_action": "finish", "agent_result": AgentResult(status="completed", output=result, artifacts=[], error=None)}


async def _check_update_conflicts(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="check_update_conflicts")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    client = agent._get_calendar_client()  # noqa: SLF001
    patch = dict(state.get("patch") or {})
    start = patch.get("start") if isinstance(patch.get("start"), dict) else {}
    end = patch.get("end") if isinstance(patch.get("end"), dict) else {}
    time_min = str(start.get("dateTime") or start.get("date") or "").strip()
    time_max = str(end.get("dateTime") or end.get("date") or "").strip()
    conflicts: list[dict[str, Any]] = []
    if time_min and time_max:
        await agent._maybe_update_step(3, "in_progress", "Checking the new slot for overlaps.")  # noqa: SLF001
        try:
            existing = await client.list_events(
                calendar_id=str(state.get("calendar_id") or "primary"),
                time_min=time_min,
                time_max=time_max,
                max_results=5,
            )
            event_id = str(state.get("event_id") or "").strip()
            conflicts = [item for item in existing if item.get("event_id") != event_id and item.get("status") != "cancelled"]
        except Exception:
            conflicts = []
        await agent._maybe_update_step(3, "completed", f"Found {len(conflicts)} overlapping event(s).")  # noqa: SLF001
    return {
        **round_update,
        "update_conflict_checked": True,
        "conflicts": conflicts,
    }


async def _update_event(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="update_event")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    client = agent._get_calendar_client()  # noqa: SLF001
    patch = dict(state.get("patch") or {})
    if not patch:
        return {
            **round_update,
            "next_action": "finish",
            "agent_result": _result_error(
                code="INVALID_INPUT",
                message="No fields to update. Provide at least one of: summary, description, location, start, end, attendees, reminders.",
            ),
        }

    step_no = 4 if _time_changed(patch) else 3
    await agent._maybe_update_step(step_no, "in_progress", "Applying update in Google Calendar.")  # noqa: SLF001
    event = await client.update_event(
        calendar_id=str(state.get("calendar_id") or "primary"),
        event_id=str(state.get("event_id") or ""),
        patch=patch,
    )
    agent._save_session(task, "update", query=event.get("summary", ""), event_ids=[str(state.get("event_id") or "")])  # noqa: SLF001
    await agent._maybe_update_step(step_no, "completed", f"Updated {state.get('event_id')}.")  # noqa: SLF001
    result: dict[str, Any] = {"event": event, "updated": True, "workflow": "langgraph", "rounds_used": int(round_update["tool_round"])}
    conflicts = list(state.get("conflicts") or [])
    if conflicts:
        result["conflict_warning"] = f"Warning: {len(conflicts)} overlapping event(s) at the new time."
    return {**round_update, "updated_done": True, "next_action": "finish", "agent_result": AgentResult(status="completed", output=result, artifacts=[], error=None)}


async def _cancel_event(state: CalendarWorkflowState, ctx: _GraphCtx) -> CalendarWorkflowState:
    round_update = _bump_round(state, action="cancel_event")
    if round_update.get("agent_result") is not None:
        return round_update

    agent = ctx.agent
    task = ctx.task
    client = agent._get_calendar_client()  # noqa: SLF001
    await agent._maybe_update_step(3, "in_progress", "Cancelling event in Google Calendar.")  # noqa: SLF001
    deleted = await client.delete_event(
        calendar_id=str(state.get("calendar_id") or "primary"),
        event_id=str(state.get("event_id") or ""),
        notify_attendees=bool(state.get("notify_attendees", True)),
    )
    event_id = str(state.get("event_id") or "")
    agent._save_session(task, "cancel", query=event_id, event_ids=[event_id])  # noqa: SLF001
    await agent._maybe_update_step(3, "completed", f"Cancelled {event_id}.")  # noqa: SLF001
    return {
        **round_update,
        "cancelled_done": True,
        "next_action": "finish",
        "agent_result": AgentResult(
            status="completed",
            output={
                "cancelled": deleted,
                "event_id": event_id,
                "calendar_id": str(state.get("calendar_id") or "primary"),
                "attendees_notified": bool(state.get("notify_attendees", True)),
                "workflow": "langgraph",
                "rounds_used": int(round_update["tool_round"]),
            },
            artifacts=[],
            error=None,
        ),
    }


def _build_graph(ctx: _GraphCtx):
    graph = StateGraph(CalendarWorkflowState)

    async def normalize_request_node(state: CalendarWorkflowState):
        return await _normalize_request(state, ctx)

    async def list_events_node(state: CalendarWorkflowState):
        return await _list_events(state, ctx)

    async def query_busy_node(state: CalendarWorkflowState):
        return await _query_busy(state, ctx)

    async def compute_free_slots_node(state: CalendarWorkflowState):
        return await _compute_free_slots(state, ctx)

    async def resolve_target_event_node(state: CalendarWorkflowState):
        return await _resolve_target_event(state, ctx)

    async def check_create_conflicts_node(state: CalendarWorkflowState):
        return await _check_create_conflicts(state, ctx)

    async def create_event_node(state: CalendarWorkflowState):
        return await _create_event(state, ctx)

    async def check_update_conflicts_node(state: CalendarWorkflowState):
        return await _check_update_conflicts(state, ctx)

    async def update_event_node(state: CalendarWorkflowState):
        return await _update_event(state, ctx)

    async def cancel_event_node(state: CalendarWorkflowState):
        return await _cancel_event(state, ctx)

    graph.add_node("normalize_request", normalize_request_node)
    graph.add_node("decide_next", _decide_next_action)
    graph.add_node("list_events", list_events_node)
    graph.add_node("query_busy", query_busy_node)
    graph.add_node("compute_free_slots", compute_free_slots_node)
    graph.add_node("resolve_target_event", resolve_target_event_node)
    graph.add_node("check_create_conflicts", check_create_conflicts_node)
    graph.add_node("create_event", create_event_node)
    graph.add_node("check_update_conflicts", check_update_conflicts_node)
    graph.add_node("update_event", update_event_node)
    graph.add_node("cancel_event", cancel_event_node)

    graph.add_edge(START, "normalize_request")
    graph.add_edge("normalize_request", "decide_next")
    graph.add_conditional_edges(
        "decide_next",
        _route_after_decide,
        {
            "list_events": "list_events",
            "query_busy": "query_busy",
            "compute_free_slots": "compute_free_slots",
            "resolve_target_event": "resolve_target_event",
            "check_create_conflicts": "check_create_conflicts",
            "create_event": "create_event",
            "check_update_conflicts": "check_update_conflicts",
            "update_event": "update_event",
            "cancel_event": "cancel_event",
            "finish": END,
        },
    )
    for node in (
        "list_events",
        "query_busy",
        "compute_free_slots",
        "resolve_target_event",
        "check_create_conflicts",
        "create_event",
        "check_update_conflicts",
        "update_event",
        "cancel_event",
    ):
        graph.add_edge(node, "decide_next")

    return graph.compile()


async def run_calendar_langgraph(*, agent: Any, task: TaskEnvelope) -> AgentResult:
    """Run a bounded LangGraph workflow for calendar specialist tasks."""
    ctx = _GraphCtx(agent=agent, task=task)
    app = _build_graph(ctx)
    initial_state: CalendarWorkflowState = {
        "intent": str(task.intent),
        "tool_round": 0,
        "max_tool_rounds": int(agent._cfg.calendar_max_tool_rounds),  # noqa: SLF001
        "next_action": "finish",
        "agent_result": None,
    }
    final_state = await app.ainvoke(initial_state)
    result = final_state.get("agent_result")
    if isinstance(result, AgentResult):
        return result
    logger.warning("calendar.langgraph_missing_result", extra={"intent": task.intent, "task_id": task.task_id})
    return _result_error(
        code="INTERNAL_ERROR",
        message="Calendar LangGraph finished without producing a result.",
    )
