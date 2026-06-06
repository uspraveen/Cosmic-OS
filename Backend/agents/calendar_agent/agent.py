"""Calendar Agent — Google Calendar specialist for COSMIC.

Handles calendar.list_events, calendar.create_event, calendar.find_free_slots,
calendar.update_event, calendar.cancel_event, calendar.recall_session.

Uses self.auth.access_token from orchestrator-injected credentials.
Internal LLM (gpt-5-mini) for natural language scheduling parsing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from shared import utcnow
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, TaskEnvelope
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, CalendarAgentConfig
from .google_calendar_client import GoogleCalendarClient
from .internal_llm import invoke_calendar_internal_llm

logger = logging.getLogger(__name__)

_CALENDAR_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS calendar_sessions (
    session_id TEXT,
    task_id TEXT,
    intent TEXT NOT NULL,
    query TEXT,
    operation TEXT,
    result_summary TEXT,
    event_ids_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_cal_sessions_session_created
ON calendar_sessions (session_id, created_at DESC);
"""


class CalendarAgent(AgentRuntime):
    """Google Calendar specialist agent."""

    def __init__(self, redis_client, config: CalendarAgentConfig | None = None):
        self._cfg = config or CalendarAgentConfig.from_env()
        super().__init__(
            agent_card_path=str(AGENT_ROOT / "agent_card.yaml"),
            redis_client=redis_client,
        )
        self.prompts_dir = AGENT_ROOT / "prompts"
        self.learnings_path = AGENT_ROOT / "store" / "learnings.md"
        self.system_prompt: str | None = None
        self.policies: str | None = None
        self.learnings: str | None = None
        self.db = None
        self._http_client: httpx.AsyncClient | None = None

    async def on_startup(self):
        """Initialize databases and ensure directories exist."""
        self.learnings_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text("# Calendar Agent — Learnings\n")

        data_dir = AGENT_ROOT / "store" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect_sync(str(data_dir / "calendar_sessions.db"))
        self.db.executescript(_CALENDAR_SESSIONS_SQL)
        self.db.commit()

        runtime_dir = AGENT_ROOT / "runtime"
        (runtime_dir / "cache").mkdir(parents=True, exist_ok=True)
        (runtime_dir / "logs").mkdir(parents=True, exist_ok=True)

    def _load_task_context(self):
        """Reload prompt and learnings at task start."""
        self.system_prompt = (self.prompts_dir / "system.md").read_text()
        self.policies = (self.prompts_dir / "policies.md").read_text()
        self.learnings = self.learnings_path.read_text()

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30, http2=False)
        return self._http_client

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        """Core execution. Dispatches to intent handlers."""
        self._load_task_context()

        if task.intent not in {"calendar.recall_session", "calendar.heartbeat_digest"} and self._cfg.calendar_use_langgraph:
            try:
                from .calendar_graph import run_calendar_langgraph
            except ImportError as exc:
                logger.warning("calendar.langgraph_unavailable: %s", exc)
            else:
                return await run_calendar_langgraph(agent=self, task=task)

        handler_name = f"handle_{task.intent.replace('.', '_')}"
        handler = getattr(self, handler_name, None)
        if not handler:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message=f"Unknown intent: {task.intent}",
                    next_action="escalate",
                ),
            )

        try:
            return await handler(task)
        except PermissionError:
            return await self._handle_auth_error(task)
        except ValueError as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message=str(exc),
                    next_action="escalate",
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return await self._handle_auth_error(task)
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="NETWORK_ERROR",
                    retryable=True,
                    message=f"Google API error: {exc.response.status_code}",
                    next_action="retry",
                ),
            )
        except httpx.TimeoutException:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="TIMEOUT",
                    retryable=True,
                    message="Google Calendar API timed out.",
                    next_action="retry",
                ),
            )
        except Exception as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=False,
                    message=str(exc),
                    next_action="escalate",
                ),
            )

    # ── Auth helpers ──────────────────────────────────────────────────

    def _require_auth(self) -> str:
        """Extract access token from self.auth. Raises on missing auth."""
        if not self.auth or not self.auth.get("access_token"):
            raise PermissionError(
                "No Google credentials provided. The orchestrator must resolve "
                "credentials before dispatching calendar intents."
            )
        return self.auth["access_token"]

    def _get_calendar_client(self) -> GoogleCalendarClient:
        return GoogleCalendarClient(self._require_auth())

    async def _handle_auth_error(self, task: TaskEnvelope) -> AgentResult:
        """Request credential refresh via orchestrator reverse task."""
        if self.auth and self.auth.get("credential_ref"):
            await self._request_credential_refresh(task)
            return AgentResult(
                status="failed",
                output={
                    "message": "Credential expired. Requested refresh via orchestrator."
                },
                artifacts=[],
                error=AgentError(
                    code="AUTH_ERROR",
                    retryable=True,
                    message="Access token expired. Suspended pending credential refresh.",
                    next_action="retry",
                ),
            )
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="AUTH_ERROR",
                retryable=False,
                message="No Google credentials available for calendar operation.",
                next_action="escalate",
            ),
        )

    async def _request_credential_refresh(self, task: TaskEnvelope):
        """Suspend task and request orchestrator to refresh credentials."""
        await self.submit_reverse_task(
            intent="orchestrator.refresh_credential",
            input_data={
                "credential_ref": self.auth.get("credential_ref", ""),
                "provider": "google",
                "parent_task_id": task.task_id,
            },
            parent_task=task,
        )

    # ── Internal LLM helper ───────────────────────────────────────────

    async def _parse_natural_language(
        self, task: TaskEnvelope
    ) -> dict[str, Any] | None:
        """Use internal LLM to parse natural language into structured plan."""
        query = task.input.get("query") or task.input.get("description") or ""
        if not query:
            return None
        tz = task.input.get("timezone") or self._cfg.default_timezone
        return await invoke_calendar_internal_llm(
            cfg=self._cfg,
            http_client=self._http(),
            user_message=query,
            task_id=task.task_id,
            session_id=task.session_id,
            source=task.source,
            source_id=task.source_id,
            channel=task.channel,
            current_time_iso=utcnow().isoformat(),
            timezone=tz,
        )

    def _resume_meta(self, task: TaskEnvelope) -> dict[str, Any]:
        resume_payload = task.input.get("_resume")
        return dict(resume_payload) if isinstance(resume_payload, dict) else {}

    async def _maybe_create_plan(self, task: TaskEnvelope, steps: list[str]) -> None:
        if self.step_plan is None or len(steps) < 3:
            return
        await self.step_plan.create(steps)

    async def _maybe_update_step(
        self,
        step: int,
        status: str,
        note: str | None = None,
    ) -> None:
        if self.step_plan is None:
            return
        await self.step_plan.update(step, status, note)

    def _default_lookup_window(self) -> tuple[str, str]:
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(days=30)
        window_end = now + timedelta(days=120)
        return window_start.isoformat(), window_end.isoformat()

    def _build_lookup_window(
        self,
        task: TaskEnvelope,
        params: dict[str, Any] | None,
    ) -> tuple[str, str]:
        time_min = str(task.input.get("time_min") or "").strip()
        time_max = str(task.input.get("time_max") or "").strip()
        if isinstance(params, dict):
            time_range = params.get("time_range")
            if isinstance(time_range, dict):
                time_min = time_min or str(time_range.get("start") or "").strip()
                time_max = time_max or str(time_range.get("end") or "").strip()
        if time_min and time_max:
            return time_min, time_max
        return self._default_lookup_window()

    def _coerce_list_search_query(
        self,
        *,
        raw_query: Any,
        explicit_search_query: Any = None,
        llm_params: dict[str, Any] | None = None,
    ) -> str | None:
        if explicit_search_query is not None:
            explicit = str(explicit_search_query or "").strip()
            if explicit:
                return explicit

        if isinstance(llm_params, dict):
            llm_search = str(llm_params.get("search_query") or "").strip()
            if llm_search:
                return llm_search

        candidate = str(raw_query or "").strip()
        if not candidate:
            return None
        lowered = candidate.lower()
        agenda_markers = (
            "calendar",
            "schedule",
            "events",
            "what's on",
            "what is on",
            "what do i have",
            "show my",
            "check my",
            "today",
            "tomorrow",
            "this week",
            "next week",
            "this month",
            "free slot",
            "availability",
            "available",
        )
        if any(marker in lowered for marker in agenda_markers):
            return None
        if any(symbol in candidate for symbol in "?!."):
            return None
        if len(candidate.split()) > 6:
            return None
        return candidate

    def _resolved_account_output(self) -> dict[str, Any] | None:
        if not isinstance(self.auth, dict):
            return None
        account_id = str(self.auth.get("account_id") or "").strip()
        account_email = str(self.auth.get("account_email") or "").strip()
        account_display_name = str(self.auth.get("account_display_name") or "").strip()
        account_label = str(self.auth.get("account_label") or "").strip()
        account_is_primary = bool(self.auth.get("account_is_primary"))
        if not any((account_id, account_email, account_display_name, account_label)):
            return None
        return {
            "account_id": account_id,
            "email": account_email,
            "display_name": account_display_name,
            "account_label": account_label,
            "is_primary": account_is_primary,
        }

    def _merge_patch_from_params(
        self,
        patch: dict[str, Any],
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            return patch
        llm_patch = params.get("patch")
        if not isinstance(llm_patch, dict):
            return patch
        merged = dict(llm_patch)
        merged.update(patch)
        return merged

    def _normalize_patch_value(
        self,
        field: str,
        value: Any,
        *,
        timezone_name: str,
    ) -> Any:
        if field not in {"start", "end"}:
            return value
        if isinstance(value, dict):
            return value
        normalized = str(value or "").strip()
        if not normalized:
            return value
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            return {"date": normalized}
        return {"dateTime": normalized, "timeZone": timezone_name}

    def _normalize_patch(
        self,
        patch: dict[str, Any],
        *,
        timezone_name: str,
    ) -> dict[str, Any]:
        normalized = dict(patch)
        normalized_timezone = str(normalized.pop("timezone", "") or timezone_name).strip() or timezone_name
        is_all_day = bool(normalized.pop("is_all_day", False))

        attendees = normalized.get("attendees")
        if isinstance(attendees, list):
            normalized_attendees = []
            for attendee in attendees:
                if isinstance(attendee, str):
                    email = attendee.strip()
                    if email:
                        normalized_attendees.append({"email": email})
                elif isinstance(attendee, dict):
                    email = str(attendee.get("email") or "").strip()
                    if email:
                        normalized_attendees.append({"email": email})
            normalized["attendees"] = normalized_attendees

        reminders = normalized.get("reminders")
        if isinstance(reminders, list):
            overrides = []
            for item in reminders:
                try:
                    minutes = int(item)
                except (TypeError, ValueError):
                    continue
                overrides.append({"method": "popup", "minutes": minutes})
            normalized["reminders"] = {
                "useDefault": False,
                "overrides": overrides,
            }

        if "add_google_meet" in normalized:
            value = normalized.get("add_google_meet")
            if isinstance(value, str):
                normalized["add_google_meet"] = value.strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "y",
                    "on",
                }
            else:
                normalized["add_google_meet"] = bool(value)

        for field in ("start", "end"):
            if field in normalized:
                value = normalized[field]
                if is_all_day:
                    if isinstance(value, dict):
                        raw_date = str(
                            value.get("date")
                            or value.get("dateTime")
                            or ""
                        ).strip()
                    else:
                        raw_date = str(value or "").strip()
                    normalized[field] = {"date": raw_date[:10]}
                else:
                    normalized[field] = self._normalize_patch_value(
                        field,
                        value,
                        timezone_name=normalized_timezone,
                    )
        return normalized

    def _workflow_steps_for_intent(self, intent: str) -> list[str]:
        if intent == "calendar.list_events":
            return [
                "Normalize the calendar lookup request.",
                "Query Google Calendar for matching events.",
                "Summarize the matching events.",
            ]
        if intent == "calendar.create_event":
            return [
                "Normalize the event request.",
                "Check the target slot for conflicts.",
                "Create the calendar event.",
            ]
        if intent == "calendar.find_free_slots":
            return [
                "Normalize the scheduling request.",
                "Query busy periods across the selected calendars.",
                "Compute free slots within working-hour rules.",
            ]
        if intent == "calendar.update_event":
            return [
                "Normalize the event update request.",
                "Resolve the target event.",
                "Check the new slot for conflicts.",
                "Apply the patch in Google Calendar.",
            ]
        if intent == "calendar.cancel_event":
            return [
                "Normalize the cancellation request.",
                "Resolve the target event.",
                "Cancel the event in Google Calendar.",
            ]
        return []

    def _derive_event_query(
        self,
        task: TaskEnvelope,
        params: dict[str, Any] | None,
    ) -> str:
        direct_candidates = [
            task.input.get("event_query"),
            task.input.get("query"),
            task.input.get("summary"),
        ]
        if isinstance(params, dict):
            direct_candidates.extend(
                [
                    params.get("event_query"),
                    params.get("summary"),
                    params.get("search_query"),
                ]
            )
        for candidate in direct_candidates:
            normalized = str(candidate or "").strip()
            if normalized:
                return normalized
        return ""

    def _score_event_match(self, query: str, event: dict[str, Any]) -> int:
        normalized_query = str(query or "").strip().lower()
        if not normalized_query:
            return 0
        summary = str(event.get("summary") or "").strip().lower()
        description = str(event.get("description") or "").strip().lower()
        haystack = f"{summary} {description}".strip()
        if normalized_query == summary:
            return 100
        score = 0
        if normalized_query in haystack:
            score += 40
        query_tokens = [token for token in re.split(r"\W+", normalized_query) if token]
        for token in query_tokens:
            if token in summary:
                score += 12
            elif token in haystack:
                score += 4
        return score

    async def _resolve_target_event(
        self,
        *,
        client: GoogleCalendarClient,
        task: TaskEnvelope,
        calendar_id: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        query = self._derive_event_query(task, params)
        if not query:
            return None, [], ""

        time_min, time_max = self._build_lookup_window(task, params)
        candidates = await client.list_events(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=25,
            query=query,
        )
        if not candidates:
            return None, [], query
        if len(candidates) == 1:
            return candidates[0], candidates, query

        scored = sorted(
            candidates,
            key=lambda item: self._score_event_match(query, item),
            reverse=True,
        )
        if len(scored) >= 2 and self._score_event_match(query, scored[0]) > self._score_event_match(query, scored[1]):
            return scored[0], candidates, query

        exact_matches = [
            item
            for item in candidates
            if str(item.get("summary") or "").strip().lower() == query.lower()
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], candidates, query
        return None, candidates, query

    def _ambiguous_event_error(
        self,
        *,
        action: str,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> AgentResult:
        if not candidates:
            message = (
                f"Could not find a matching event to {action}. "
                "Provide a clearer event title, time range, or event_id."
            )
        else:
            preview = ", ".join(
                f"{str(item.get('summary') or 'Untitled').strip() or 'Untitled'} ({str(item.get('start') or '').strip()})"
                for item in candidates[:4]
            )
            message = (
                f"Multiple events match {query!r}. "
                f"Provide a clearer event title, time range, or event_id. Matches: {preview}."
            )
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code="INVALID_INPUT",
                retryable=False,
                message=message,
                next_action="escalate",
            ),
        )

    # ── Intent Handlers ───────────────────────────────────────────────

    async def handle_calendar_list_events(self, task: TaskEnvelope) -> AgentResult:
        """List calendar events. Supports date range, title search, calendar selection."""
        client = self._get_calendar_client()

        # Extract parameters
        time_min = task.input.get("time_min")
        time_max = task.input.get("time_max")
        raw_query = task.input.get("query")
        search_query = self._coerce_list_search_query(
            raw_query=raw_query,
            explicit_search_query=task.input.get("search_query"),
        )
        calendar_id = task.input.get("calendar_id", "primary")
        max_results = min(
            task.input.get("max_results", self._cfg.max_events_per_list),
            250,
        )

        # If no time bounds, default to current month + next month
        if not time_min or not time_max:
            now = datetime.now(tz=timezone.utc)
            time_min = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_after = (next_month.replace(day=28) + timedelta(days=4)).replace(
                day=1
            )
            time_max = month_after.isoformat()

        # If natural language query without structured params, try internal LLM
        if (
            not task.input.get("time_min")
            and raw_query
            and not task.input.get("calendar_id")
        ):
            llm_result = await self._parse_natural_language(task)
            if llm_result and llm_result.get("operation") == "list":
                params = llm_result.get("params", {})
                time_min = params.get("time_range", {}).get("start", time_min)
                time_max = params.get("time_range", {}).get("end", time_max)
                search_query = self._coerce_list_search_query(
                    raw_query=raw_query,
                    explicit_search_query=task.input.get("search_query"),
                    llm_params=params,
                )
                calendar_id = params.get("calendar_id", calendar_id)

        events = await client.list_events(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            query=search_query,
        )

        # Save session
        self._save_session(
            task,
            "list",
            query=search_query or str(raw_query or ""),
            event_ids=[e["event_id"] for e in events],
        )

        return AgentResult(
            status="completed",
            output={
                "events": events,
                "count": len(events),
                "calendar_id": calendar_id,
                "time_min": time_min,
                "time_max": time_max,
                "search_query": search_query,
                "resolved_account": self._resolved_account_output(),
            },
            artifacts=[],
            error=None,
        )

    async def handle_calendar_heartbeat_digest(self, task: TaskEnvelope) -> AgentResult:
        """Build a bounded, structured agenda digest for Gateway heartbeats."""
        client = self._get_calendar_client()
        time_min = str(task.input.get("time_min") or "").strip()
        time_max = str(task.input.get("time_max") or "").strip()
        if not time_min or not time_max:
            now = datetime.now(tz=timezone.utc)
            time_min = now.isoformat()
            time_max = (now + timedelta(hours=24)).isoformat()
        max_total = self._bounded_int(
            task.input.get("max_results"),
            default=10,
            minimum=1,
            maximum=50,
        )
        max_per_calendar = self._bounded_int(
            task.input.get("max_results_per_calendar"),
            default=max_total,
            minimum=1,
            maximum=25,
        )
        raw_selected_only = task.input.get("selected_only")
        selected_only = (
            raw_selected_only
            if isinstance(raw_selected_only, bool)
            else str(raw_selected_only or "true").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        account = (
            task.input.get("account")
            if isinstance(task.input.get("account"), dict)
            else {}
        )
        account_id = str(account.get("account_id") or "").strip()
        account_label = (
            str(
                account.get("account_label")
                or account.get("display_name")
                or account.get("email")
                or account_id
                or "Google account"
            ).strip()
            or "Google account"
        )
        account_email = str(account.get("email") or "").strip()

        calendars = await client.list_calendars()
        if selected_only:
            calendars = [
                item
                for item in calendars
                if bool(item.get("primary")) or bool(item.get("selected"))
            ]
        if not calendars:
            calendars = [
                {
                    "id": "primary",
                    "name": "Primary",
                    "color": "",
                    "primary": True,
                    "selected": True,
                    "access_role": "owner",
                }
            ]

        events: list[dict[str, Any]] = []
        calendar_errors: list[dict[str, str]] = []
        for calendar in calendars[:25]:
            calendar_id = str(calendar.get("id") or "").strip()
            if not calendar_id:
                continue
            try:
                calendar_events = await client.list_events(
                    calendar_id=calendar_id,
                    time_min=time_min,
                    time_max=time_max,
                    max_results=max_per_calendar,
                )
            except Exception as exc:
                calendar_errors.append(
                    {
                        "calendar_id": calendar_id,
                        "calendar_name": str(calendar.get("name") or calendar_id),
                        "message": str(exc).strip()[:240],
                    }
                )
                continue
            for event in calendar_events:
                payload = dict(event)
                payload.update(
                    {
                        "account_id": account_id,
                        "account_label": account_label,
                        "email": account_email,
                        "calendar_id": calendar_id,
                        "calendar_name": str(calendar.get("name") or calendar_id),
                        "calendar_color": str(calendar.get("color") or ""),
                        "calendar_primary": bool(calendar.get("primary")),
                    }
                )
                events.append(payload)

        events.sort(
            key=lambda item: (
                str(item.get("start") or ""),
                str(item.get("summary") or ""),
            )
        )
        events = events[:max_total]
        return AgentResult(
            status="completed",
            output={
                "events": events,
                "count": len(events),
                "calendars": calendars,
                "calendar_count": len(calendars),
                "calendar_errors": calendar_errors,
                "account": {
                    "account_id": account_id,
                    "account_label": account_label,
                    "email": account_email,
                    "display_name": str(account.get("display_name") or "").strip(),
                    "is_primary": bool(account.get("is_primary")),
                },
                "time_min": time_min,
                "time_max": time_max,
            },
            artifacts=[],
            error=None,
        )

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    async def handle_calendar_create_event(self, task: TaskEnvelope) -> AgentResult:
        """Create a calendar event. Supports structured params or natural language."""
        client = self._get_calendar_client()
        llm_result = None

        summary = task.input.get("summary", "")
        start = task.input.get("start")
        end = task.input.get("end")
        calendar_id = task.input.get("calendar_id", "primary")
        tz = task.input.get("timezone") or self._cfg.default_timezone
        is_all_day = task.input.get("is_all_day", False)
        description = task.input.get("description", "")
        location = task.input.get("location", "")
        attendees = task.input.get("attendees", [])
        reminders = task.input.get("reminders")
        add_google_meet = bool(task.input.get("add_google_meet", False))

        # If natural language input, parse with internal LLM
        if not summary or not start or task.input.get("query"):
            llm_result = await self._parse_natural_language(task)
            if llm_result and llm_result.get("operation") == "create":
                params = llm_result.get("params", {})
                summary = summary or params.get("summary", "")
                start = start or params.get("start")
                end = end or params.get("end")
                is_all_day = params.get("is_all_day", is_all_day)
                description = description or params.get("description", "")
                location = location or params.get("location", "")
                attendees = attendees or params.get("attendees", [])
                reminders = (
                    reminders if reminders is not None else params.get("reminders")
                )
                if "add_google_meet" in params:
                    add_google_meet = bool(params.get("add_google_meet"))
                tz = params.get("timezone", tz)
                calendar_id = params.get("calendar_id", calendar_id)

        if not summary:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="Event summary (title) is required.",
                    next_action="escalate",
                ),
            )

        if not start:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="Event start time is required.",
                    next_action="escalate",
                ),
            )

        # Build start/end dicts
        if is_all_day:
            start_dict = {"date": start}
            if not end:
                start_date = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_date = start_date + timedelta(days=1)
                end = end_date.strftime("%Y-%m-%d")
            end_dict = {"date": end}
        else:
            start_dict = {"dateTime": start, "timeZone": tz}
            if not end:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = start_dt + timedelta(
                    minutes=self._cfg.default_event_duration_min
                )
                end = end_dt.isoformat()
            end_dict = {"dateTime": end, "timeZone": tz}

        await self._maybe_create_plan(
            task,
            [
                "Normalize the event request.",
                "Check the target slot for conflicts.",
                "Create the calendar event.",
            ],
        )
        await self._maybe_update_step(1, "completed", "Request normalized.")

        # Conflict detection
        try:
            await self._maybe_update_step(2, "in_progress", "Scanning overlapping events.")
            conflict_check_start = start if is_all_day else start
            conflict_check_end = end if is_all_day else end
            existing = await client.list_events(
                calendar_id=calendar_id,
                time_min=conflict_check_start,
                time_max=conflict_check_end,
                max_results=5,
            )
            conflicts = [e for e in existing if e.get("status") != "cancelled"]
        except Exception:
            conflicts = []
        await self._maybe_update_step(2, "completed", f"Found {len(conflicts)} overlapping event(s).")

        await self._maybe_update_step(3, "in_progress", "Creating event in Google Calendar.")
        event = await client.create_event(
            calendar_id=calendar_id,
            summary=summary,
            start=start_dict,
            end=end_dict,
            timezone=tz if not is_all_day else None,
            description=description,
            location=location,
            attendees=attendees,
            reminders=reminders,
            add_google_meet=add_google_meet,
        )

        self._save_session(
            task,
            "create",
            query=summary,
            event_ids=[event["event_id"]],
        )
        await self._maybe_update_step(3, "completed", f"Created {event['event_id']}.")

        result: dict[str, Any] = {
            "event": event,
            "created": True,
        }
        account = self._resolved_account_output()
        if account:
            result["account"] = account
        if conflicts:
            result["conflict_warning"] = (
                f"{len(conflicts)} overlapping event(s) found in this time slot."
            )
            result["conflicting_events"] = [
                {"event_id": c["event_id"], "summary": c["summary"]} for c in conflicts
            ]

        return AgentResult(
            status="completed",
            output=result,
            artifacts=[],
            error=None,
        )

    async def handle_calendar_find_free_slots(self, task: TaskEnvelope) -> AgentResult:
        """Find available time slots considering existing events, working hours, buffers."""
        client = self._get_calendar_client()

        duration_min = task.input.get(
            "duration_min", self._cfg.default_event_duration_min
        )
        date_range_start = task.input.get("date_range_start")
        date_range_end = task.input.get("date_range_end")
        calendar_ids = task.input.get("calendar_ids", ["primary"])
        tz = task.input.get("timezone") or self._cfg.default_timezone
        working_start = task.input.get(
            "working_hour_start", self._cfg.working_hour_start
        )
        working_end = task.input.get("working_hour_end", self._cfg.working_hour_end)
        buffer_min = task.input.get("buffer_min", self._cfg.buffer_between_events_min)

        # Default date range: next 5 business days
        if not date_range_start or not date_range_end:
            now = datetime.now(tz=timezone.utc)
            date_range_start = now.isoformat()
            date_range_end = (now + timedelta(days=7)).isoformat()

        # Query free/busy from Google
        busy_periods = await client.query_free_busy(
            calendar_ids=calendar_ids,
            time_min=date_range_start,
            time_max=date_range_end,
        )

        # Flatten all busy periods across calendars
        all_busy: list[tuple[datetime, datetime]] = []
        for cal_id, periods in busy_periods.items():
            for period in periods:
                try:
                    bs = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
                    be = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
                    all_busy.append((bs, be))
                except Exception:
                    continue

        # Sort busy periods
        all_busy.sort(key=lambda x: x[0])

        # Find free slots
        free_slots: list[dict[str, str]] = []
        range_start = datetime.fromisoformat(date_range_start.replace("Z", "+00:00"))
        range_end = datetime.fromisoformat(date_range_end.replace("Z", "+00:00"))
        duration = timedelta(minutes=duration_min)
        buffer_td = timedelta(minutes=buffer_min)

        current = range_start
        while current + duration <= range_end:
            # Check if within working hours (skip if outside)
            hour = current.hour
            if hour < working_start or hour >= working_end:
                current = current + timedelta(hours=1)
                continue

            # Skip weekends
            if current.weekday() >= 5:
                current = current + timedelta(days=1)
                current = current.replace(hour=working_start, minute=0, second=0)
                continue

            slot_end = current + duration
            # Check overlap with busy periods (including buffer)
            overlap = False
            for busy_start, busy_end in all_busy:
                buffered_start = busy_start - buffer_td
                buffered_end = busy_end + buffer_td
                if current < buffered_end and slot_end > buffered_start:
                    overlap = True
                    # Jump past this busy period
                    current = buffered_end
                    break

            if not overlap:
                free_slots.append(
                    {
                        "start": current.isoformat(),
                        "end": slot_end.isoformat(),
                        "duration_min": duration_min,
                    }
                )
                if len(free_slots) >= 10:
                    break
                current = slot_end + buffer_td
            elif not overlap:
                current = slot_end

        self._save_session(
            task, "find_free", query=f"{duration_min}min slots", event_ids=[]
        )

        return AgentResult(
            status="completed",
            output={
                "free_slots": free_slots,
                "count": len(free_slots),
                "duration_min": duration_min,
                "date_range_start": date_range_start,
                "date_range_end": date_range_end,
                "timezone": tz,
                "working_hours": {"start": working_start, "end": working_end},
            },
            artifacts=[],
            error=None,
        )

    async def handle_calendar_update_event(self, task: TaskEnvelope) -> AgentResult:
        """Update an existing calendar event (partial update via PATCH)."""
        client = self._get_calendar_client()

        event_id = task.input.get("event_id")
        calendar_id = task.input.get("calendar_id", "primary")
        patch: dict[str, Any] = {}
        llm_params: dict[str, Any] | None = None

        if task.input.get("query") or not event_id:
            llm_result = await self._parse_natural_language(task)
            if llm_result and llm_result.get("operation") == "update":
                llm_params = llm_result.get("params", {})
                event_id = event_id or llm_params.get("event_id")
                calendar_id = llm_params.get("calendar_id", calendar_id)

        # Build patch from provided fields
        if "summary" in task.input:
            patch["summary"] = task.input["summary"]
        if "description" in task.input:
            patch["description"] = task.input["description"]
        if "location" in task.input:
            patch["location"] = task.input["location"]
        if "start" in task.input:
            start_val = task.input["start"]
            is_all_day = task.input.get("is_all_day", False)
            if is_all_day:
                patch["start"] = {"date": start_val}
            else:
                tz = task.input.get("timezone", self._cfg.default_timezone)
                patch["start"] = {"dateTime": start_val, "timeZone": tz}
        if "end" in task.input:
            end_val = task.input["end"]
            is_all_day = task.input.get("is_all_day", False)
            if is_all_day:
                patch["end"] = {"date": end_val}
            else:
                tz = task.input.get("timezone", self._cfg.default_timezone)
                patch["end"] = {"dateTime": end_val, "timeZone": tz}
        if "attendees" in task.input:
            patch["attendees"] = [{"email": email} for email in task.input["attendees"]]
        if "reminders" in task.input:
            reminders = task.input["reminders"]
            if reminders is not None:
                patch["reminders"] = {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": m} for m in reminders],
                }
        if "add_google_meet" in task.input:
            patch["add_google_meet"] = bool(task.input.get("add_google_meet"))

        patch = self._merge_patch_from_params(patch, llm_params)
        patch = self._normalize_patch(
            patch,
            timezone_name=str(task.input.get("timezone") or self._cfg.default_timezone),
        )

        await self._maybe_create_plan(
            task,
            [
                "Normalize the event update request.",
                "Resolve the target event.",
                "Apply the patch in Google Calendar.",
            ],
        )
        await self._maybe_update_step(1, "completed", "Update request normalized.")

        if not event_id:
            await self._maybe_update_step(2, "in_progress", "Searching for the target event.")
            matched_event, candidates, query = await self._resolve_target_event(
                client=client,
                task=task,
                calendar_id=calendar_id,
                params=llm_params,
            )
            if matched_event is None:
                await self._maybe_update_step(2, "skipped", "Target event could not be resolved.")
                return self._ambiguous_event_error(action="update", query=query, candidates=candidates)
            event_id = matched_event["event_id"]
            calendar_id = matched_event.get("calendar_id", calendar_id)
            await self._maybe_update_step(2, "completed", f"Resolved event {event_id}.")
        else:
            await self._maybe_update_step(2, "completed", f"Using explicit event {event_id}.")

        if not patch:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="No fields to update. Provide at least one of: summary, description, location, start, end, attendees, reminders, add_google_meet.",
                    next_action="escalate",
                ),
            )

        # Conflict detection if time is being changed
        conflict_warning = None
        if "start" in patch or "end" in patch:
            try:
                existing = await client.list_events(
                    calendar_id=calendar_id,
                    time_min=patch.get("start", {}).get("dateTime", ""),
                    time_max=patch.get("end", {}).get("dateTime", ""),
                    max_results=5,
                )
                conflicts = [
                    e
                    for e in existing
                    if e["event_id"] != event_id and e.get("status") != "cancelled"
                ]
                if conflicts:
                    conflict_warning = f"Warning: {len(conflicts)} overlapping event(s) at the new time."
            except Exception:
                pass

        await self._maybe_update_step(3, "in_progress", "Applying update in Google Calendar.")
        event = await client.update_event(
            calendar_id=calendar_id,
            event_id=event_id,
            patch=patch,
        )

        self._save_session(
            task, "update", query=event.get("summary", ""), event_ids=[event_id]
        )
        await self._maybe_update_step(3, "completed", f"Updated {event_id}.")

        result: dict[str, Any] = {"event": event, "updated": True}
        account = self._resolved_account_output()
        if account:
            result["account"] = account
        if conflict_warning:
            result["conflict_warning"] = conflict_warning

        return AgentResult(
            status="completed",
            output=result,
            artifacts=[],
            error=None,
        )

    async def handle_calendar_cancel_event(self, task: TaskEnvelope) -> AgentResult:
        """Cancel a calendar event."""
        client = self._get_calendar_client()

        event_id = task.input.get("event_id")
        calendar_id = task.input.get("calendar_id", "primary")
        notify_attendees = task.input.get("notify_attendees", True)
        llm_params: dict[str, Any] | None = None

        if task.input.get("query") or not event_id:
            llm_result = await self._parse_natural_language(task)
            if llm_result and llm_result.get("operation") == "cancel":
                llm_params = llm_result.get("params", {})
                event_id = event_id or llm_params.get("event_id")
                calendar_id = llm_params.get("calendar_id", calendar_id)
                if "notify_attendees" in llm_params:
                    notify_attendees = bool(llm_params.get("notify_attendees"))

        await self._maybe_create_plan(
            task,
            [
                "Normalize the cancellation request.",
                "Resolve the target event.",
                "Cancel the event in Google Calendar.",
            ],
        )
        await self._maybe_update_step(1, "completed", "Cancellation request normalized.")

        if not event_id:
            await self._maybe_update_step(2, "in_progress", "Searching for the target event.")
            matched_event, candidates, query = await self._resolve_target_event(
                client=client,
                task=task,
                calendar_id=calendar_id,
                params=llm_params,
            )
            if matched_event is None:
                await self._maybe_update_step(2, "skipped", "Target event could not be resolved.")
                return self._ambiguous_event_error(action="cancel", query=query, candidates=candidates)
            event_id = matched_event["event_id"]
            calendar_id = matched_event.get("calendar_id", calendar_id)
            await self._maybe_update_step(2, "completed", f"Resolved event {event_id}.")
        else:
            await self._maybe_update_step(2, "completed", f"Using explicit event {event_id}.")

        await self._maybe_update_step(3, "in_progress", "Cancelling event in Google Calendar.")
        deleted = await client.delete_event(
            calendar_id=calendar_id,
            event_id=event_id,
            notify_attendees=notify_attendees,
        )

        self._save_session(task, "cancel", query=event_id, event_ids=[event_id])
        await self._maybe_update_step(3, "completed", f"Cancelled {event_id}.")

        return AgentResult(
            status="completed",
            output={
                "cancelled": deleted,
                "event_id": event_id,
                "calendar_id": calendar_id,
                "attendees_notified": notify_attendees,
            },
            artifacts=[],
            error=None,
        )

    async def handle_calendar_recall_session(self, task: TaskEnvelope) -> AgentResult:
        """Recall prior calendar operations from the session ledger."""
        session_id = task.input.get("session_id")
        limit = task.input.get("limit", 10)

        rows = self.db.execute(
            """SELECT * FROM calendar_sessions
               WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            [session_id, limit],
        ).fetchall()

        return AgentResult(
            status="completed",
            output={
                "history": [dict(r) for r in rows],
                "count": len(rows),
            },
            artifacts=[],
            error=None,
        )

    # ── Session Persistence ───────────────────────────────────────────

    def _save_session(
        self,
        task: TaskEnvelope,
        operation: str,
        *,
        query: str = "",
        event_ids: list[str] | None = None,
    ) -> None:
        """Save operation to session ledger for recall."""
        try:
            self.db.execute(
                """INSERT OR REPLACE INTO calendar_sessions
                   (session_id, task_id, intent, query, operation, result_summary, event_ids_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    task.session_id,
                    task.task_id,
                    task.intent,
                    query,
                    operation,
                    f"{operation}: {query}",
                    json.dumps(event_ids or []),
                    utcnow().isoformat(),
                ],
            )
            self.db.commit()
        except Exception as exc:
            logger.warning("Failed to save calendar session: %s", exc)
