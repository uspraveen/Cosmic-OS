# Policies

## Error Handling

- Return `AgentError` with `retryable=True` for: `TIMEOUT`, `NETWORK_ERROR`, `RATE_LIMITED`
- Return `AgentError` with `retryable=False` for: `INVALID_INPUT`, `AUTH_ERROR`, `SCHEMA_VIOLATION`
- Always include `next_action`: `'retry'`, `'escalate'`, or `'skip'`
- On Google API 401 → suspend task, request `orchestrator.refresh_credential`, let orchestrator resume with fresh token

## Credential Safety

- Access credentials ONLY via `self.auth`
- NEVER include credentials in: events, artifacts, logs, `store/`, `learnings.md`
- If access token expires mid-task: suspend and request refresh via `orchestrator.refresh_credential`
- Do NOT assume the base class refreshes provider tokens for you

## Calendar-Specific Rules

### Workflow Discipline
- Use the bounded LangGraph workflow for phase-1 calendar execution. Normalize first, then resolve targets, check conflicts when relevant, perform the Google action, and stop.
- Keep the internal workflow narrow. Do not turn a calendar task into general-purpose orchestration or open-ended research.
- Respect the configured round cap. If the workflow is still blocked after bounded resolution, fail clearly instead of looping.
- For `calendar.list_events`, treat the natural-language request as planning input, not as a Google Calendar `q` filter. Only send a search query to Google when the user is explicitly searching by title/keyword.

### Event Creation
- Always detect conflicts before creating. If conflicts exist, include a `conflict_warning` in the output.
- Default to 30-minute duration if user doesn't specify.
- Default to user's working hours (9am-5pm) for scheduling.
- For natural language input, use the internal LLM to parse, but validate its output before calling the API.
- If the user asks for a Google Meet / Meet link / video conference, set `add_google_meet` and create the event with conference data so attendees receive the join link.

### Event Updates
- Use PATCH (partial update) — never send a full event replacement.
- If updating event time, check for conflicts at the new time.
- For recurring events, the `recurring_event_id` field identifies the series. Do not modify the series unless explicitly asked.
- If the user does not provide `event_id`, do a bounded lookup using title/query + time window before failing.
- If multiple plausible events remain after lookup, stop and escalate instead of guessing.
- Support adding a Google Meet link by patching conference data when the user explicitly asks for Meet/video conferencing.

### Event Cancellation
- Default to notifying attendees (`notify_attendees: true`).
- If the user says "cancel without notifying", set `notify_attendees: false`.
- If `event_id` is missing, resolve the target event by query/title/time window before cancelling.

### Invitation Responses
- Use `calendar.respond_to_invite` for accept, decline, tentative/maybe, and reset-response requests.
- Read or boundedly resolve the exact event before responding.
- Verify the selected Google account is the event's `self` attendee and is not the organizer.
- Patch only the selected attendee response with `attendeesOmitted=true`; never replace the full attendee array.
- If the invitation belongs to another connected account or the target remains ambiguous, stop and return a precise error instead of guessing.

### Free Slot Discovery
- Respect working hours — do not suggest slots outside them.
- Include buffer time between meetings (default 15 min).
- Skip weekends unless the user explicitly asks for weekend slots.
- Check all specified calendars for conflicts.

### Multi-Account
- The orchestrator resolves which Google account to use and passes a single `input.auth`.
- You receive exactly one credential per task. Do not attempt to access multiple accounts.
- If the user needs events across accounts, the orchestrator will dispatch separate tasks.
- The user may refer to accounts with human-friendly labels like "work", "personal", or an email address. The orchestrator/Gateway resolve that to an internal account.
- Use the provided credential; do not invent your own account selection logic inside the Google API layer.
- Treat `account_hint` as a human-facing routing hint only. Never expect or require the user to provide raw internal account ids.
- When you return results, include the resolved account metadata if available so the orchestrator can truthfully say which account/calendar was checked.

### Time Semantics
- All timed events: `{"dateTime": "2026-03-30T14:00:00+05:30", "timeZone": "Asia/Kolkata"}`
- All-day events: `{"date": "2026-03-30"}`
- Never send `dateTime` for all-day events or `date` for timed events.
- Always include `timeZone` in timed event start/end dicts.

### Ambiguity Handling
- If multiple plausible events remain after bounded lookup, do not guess. Return an `INVALID_INPUT` error that names the ambiguity and gives the orchestrator enough detail to ask the user a crisp follow-up question.
- Do not claim `orchestrator.clarify` or any other reverse task path unless it is actually implemented in this runtime path.
