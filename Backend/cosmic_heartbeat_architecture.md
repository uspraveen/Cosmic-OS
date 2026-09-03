# COSMIC Heartbeat Architecture

## Purpose

COSMIC Heartbeat is the ambient attention loop for the system. It is not a reminder engine alone. It is a quiet, periodic consciousness pass that asks: "Is there anything the user would genuinely want COSMIC to notice, connect, or surface right now?"

The heartbeat should look wider than calendars and inboxes. It should consider active work, background tasks, approvals, recent conversations, user preferences, long-running interests, open loops, deadlines, relationships, system health, and project context. Its default behavior is silence.

## Product Contract

- Runs every 30 minutes by default.
- User can turn it on or off from Settings -> Preferences.
- Heartbeat prompts are never appended as user messages to chat history.
- The orchestrator must return a structured heartbeat decision envelope as a single JSON object.
- `decision="suppress"` is suppressed by Gateway and is not stored, streamed, pushed, or shown.
- `decision="deliver"` with a non-empty `message` sends that message as a short, concrete proactive note through the normal response pipeline.
- Invalid or malformed heartbeat envelopes are suppressed by default. Proactive systems should fail quiet rather than leak private scheduler reasoning.
- Heartbeat is low priority and must not interfere with foreground user work.
- Heartbeat may inspect state and use tools when justified. It may choose the best COSMIC-owned delivery path, including chat, mobile push, or email, when that channel is the clearest way to help the user.

Deep recurring maintenance exercises belong in dedicated system crons rather than the 30-minute ambient heartbeat. For example, `system.weekly_my_tools_review` runs a full-context, model-driven review of persistent custom-tool opportunities once per week. It follows the same structured silence principle as heartbeat delivery, but has its own lifecycle guardrails and append-only audit trail.

## Context Surface

Each heartbeat receives a compact context packet rather than a replayed chat prompt. The packet may include:

- Current local time and timezone.
- Current daily session id.
- Active working set: goals, workstreams, open loops, focus entities, task refs.
- Recent conversation tail, bounded and summarized.
- Active background tasks and Alpha work.
- Active or recently touched projects, websites, agents, documents, deployments, and automations.
- Scheduler status and upcoming user crons.
- Recent user-visible delivery facts for scheduled/autonomous items, bounded to the last 24-36 hours.
- Delivery queue status for offline or deferred items.
- Mobile/desktop presence and selected delivery channel.
- Whether Cosmic Mail/email delivery is available.
- Stable user preferences and interests from carry-forward/memory.
- Passive memory recall for broad user priorities.
- Heartbeat runtime state: interval, last fired time, last delivered note, last suppression, current scheduled fire, and projected next fire.
- Heartbeat watchpoint registry and rendered beat notes from `gateway/scheduler/scheduler.db` (see `cosmic_architecture.md` §25.5). Production Cosmic does not read `heartbeat_notes.md` when Gateway is up.
- Calendar Digest: a bounded upcoming-event window from connected calendar accounts, including account/calendar identity and new/changed/seen markers.

Future expansions should add first-class summaries for:

- Inbox and Cosmic Mail approvals.
- Richer presence signals such as desktop foreground state and quiet-hours aware mobile status.
- Important contacts and relationship context.
- User watchlists: YC, AI research, products, companies, documents, projects.
- External world state when recent context or stable interests suggest it matters.

## Prompt Shape

The heartbeat prompt should be stable and strict:

```text
You are COSMIC's Heartbeat: an ambient consciousness pass for the user.
This turn was triggered automatically by COSMIC's scheduler, not by a user chat message.
Do not infer that the user manually asked for this heartbeat.
Quietly decide whether there is something genuinely useful to surface now.
Think across calendar commitments, inbox and approval pressure, active projects,
background tasks, reminders, open loops, user interests, preferences, relationships,
recent conversations, and what the user would likely want to know at this moment.
Also think about active or recently touched projects, websites, agents, documents,
deployments, and automations; surface a concrete improvement, bug fix, polish pass,
deployment check, follow-up, or next step only if it would meaningfully help now.
Regularly consider whether current news, research, product changes, company updates,
people, places, or topics the user recently discussed or consistently cares about
need a lightweight web, Perplexity, X, Firecrawl, memory, or specialist check.
Use specialist/local tools when a check would materially improve the heartbeat.
If the context includes a Calendar Digest, it may contain both new and already-seen
events across multiple accounts/calendars. Do not speak just because an event
exists; prioritize new, changed, imminent, preparation-heavy, or user-goal-relevant
events, and avoid repeating calendar items already handled unless timing or
context changed.
You know this is a repeating heartbeat; use the runtime state to reason about the
last beat, this beat, and the next one.
Use heartbeat_notes as your private scratchpad for compact self-notes across beats:
read it when continuity matters, append or replace short notes, and soft-stale
stale ones. Use heartbeat_watchpoints for standing commitments ("keep an eye on X").
Never infer that a reminder, cron, email, or calendar item was missed from
desktop inactivity, missing heartbeat consumption, lack of chat activity, or
stale heartbeat notes. Use explicit delivery facts when present, and treat
completed/delivered scheduled items as already handled unless there is concrete
failure or new follow-up evidence.
Use the best COSMIC-owned delivery path available; if a proactive item is better
sent as email, use Cosmic Mail or email capabilities when available.
Your final response must be one JSON object and nothing else. Do not use Markdown.
Schema:
{"decision":"suppress"|"deliver","message":"","reason":"","confidence":0.0,"pending_checks":[],"notes":""}
For suppress, message must be empty.
For deliver, message must be the exact short, concrete, low-drama note to show the user.
Do not say this came from a heartbeat.
```

The model must treat the heartbeat itself and the context packet as scheduler-owned private state, not as a user message or user request to quote back.

## Gateway Implementation

Gateway owns the schedule because Gateway already owns sessions, delivery, preferences, and offline queues.

- `scheduler_store.heartbeat_config` stores interval, status, next fire, last result, and pause state.
- `preferences.app_preferences.cosmic_heartbeat` stores the user-facing enabled toggle.
- The scheduler loop calls the heartbeat after due crons.
- Gateway builds a normal `TaskEnvelope` with `source="heartbeat"` and `priority="low"`.
- The heartbeat request uses an empty live conversation context and a compact memory/context block.
- Gateway resolves delivery at run time: active desktop first, then active mobile, then Cosmic Mail/email when chat is silent and configured, then the latest mobile push target, then queued desktop fallback.
- When desktop and mobile chat are silent, Gateway annotates Reachability with an explicit suggestion that Cosmic can email the user rather than suppressing solely for offline presence.
- If chat stays silent and no heartbeat note has been delivered for 24h+, Reachability marks `email_checkin_due`. Offline-only suppress reasons are rejected: Gateway either forces a short Cosmic Mail check-in or accepts suppress only after rewriting the reason to a non-offline justification.
- Gateway includes a compact `recent_user_visible_deliveries` digest. It is capped and derived from canonical scheduler/session records, not from model notes.
- `visual_response_enhancement_enabled` is disabled for heartbeat turns unless explicitly changed later.
- Heartbeat response chunks and progress are not streamed live; useful final responses still retain their compact Flow/activity log for later inspection.
- Gateway parses the final heartbeat JSON decision before storage or delivery.
- `decision="suppress"` responses are suppressed before session storage, push, delivery queue, and UI display. Gateway writes `kind=beat`, `outcome=suppressed` to `heartbeat_beat_notes`.
- `decision="deliver"` responses are reduced to the validated `message` field before they enter the normal response pipeline. Gateway writes `kind=beat`, `outcome=delivered`.
- Malformed envelopes are suppressed. The legacy `heartbeat_ok` token remains supported only as a backward-compatibility fallback.

## Heartbeat Notes & Watchpoints

Heartbeat continuity state lives in **SQLite** (`gateway/scheduler/scheduler.db`), not in a markdown file. See `cosmic_architecture.md` §25.5 for the full before/after cutover story (Sep 2026).

### Storage model

| Table | Role |
|---|---|
| `heartbeat_watchpoints` | Standing commitments ("keep an eye on X"). Never hard-deleted. |
| `heartbeat_watchpoint_events` | Audit log for watchpoint lifecycle (create, status change, check). |
| `heartbeat_beat_notes` | Model free-form notes (`kind=note`/`plan`) + Gateway beat envelopes (`kind=beat`). |

`Backend/agents/orchestrator/store/heartbeat_notes.md` is a **stub only** when Gateway is up. It remains for unit tests and offline fallback when `gateway_url` is unset.

### Orchestrator tools

**`heartbeat_notes`** — free-form continuity text (same operations as the old markdown file):

- `read` / `append` / `replace` / `remove` / `clear`
- `remove` / `clear` soft-stale (`status=stale`); history remains
- Gateway already records suppress/deliver as `kind=beat` — model should not re-log those envelopes
- `kind` for model appends: `note` (default), `plan`, `watchpoint` (rare; real watches go in the other tool)

**`heartbeat_watchpoints`** — standing commitments:

- `list` / `create` / `update` / `set_status`
- Never hard-delete. Stop a watch with `set_status` + `reason` (`inactive`, `stale`, `superseded`, `completed`)
- `include_inactive=true` on list for audit queries ("where did that watch go?")

### Expected use

- Keep notes compact: project follow-ups, future checks, and ideas worth revisiting on later beats.
- Soft-stale notes once acted on or no longer relevant.
- Use `heartbeat_watchpoints` for durable standing watches; use `heartbeat_notes` for ambient operational continuity.
- Use durable memory/core facts for stable user preferences and identity-level facts.
- Do not use beat notes as proof that the user was offline, that a notification failed, or that a scheduled item was missed. Notes can be stale; delivery facts win.
- Do not append a note every beat. Silence is valid when there is nothing to remember.

## Recent Delivery Facts

Heartbeat must distinguish "not recently chatting" from "not delivered." Gateway therefore sends a small factual digest of recent user-visible scheduled/autonomous deliveries.

- The digest is bounded to roughly the last 24-36 hours and a small item cap.
- It includes completed or failed user-visible cron/reminder runs with label, scheduled time, completion time, result status, channel, and evidence source.
- When possible, Gateway resolves the cron run back to the stored assistant message across daily session rollover, so `mobile:*` deliveries remain visible to the next day's heartbeat.
- Completed/stored items are not considered pending. Heartbeat may mention them only when there is a concrete new reason, such as a failed run, a new follow-up requirement, or an explicit user ask.
- The digest is not a read-receipt system. If an item was delivered but not opened, the heartbeat should say only what the facts show.

## Calendar Digest

Gateway performs a bounded read-only calendar pre-check before building the heartbeat context when the user has connected Google Calendar accounts. This is intentionally not a free-form orchestration task.

- Gateway resolves each active connected calendar account independently, with no primary-account fallback.
- Gateway dispatches `calendar.heartbeat_digest` to the Calendar Agent with structured `time_min`, `time_max`, and max-count inputs.
- The Calendar Agent skips its natural-language/graph planning path for this intent and directly calls Google Calendar APIs.
- Each returned event carries `account_id`, account label/email, `calendar_id`, calendar name/color, event id, start/end, location, meeting-link presence, and status.
- Gateway stores a compact event fingerprint keyed by `account_id:calendar_id:event_id`.
- Every heartbeat marks events as `NEW`, `CHANGED`, or `SEEN` before passing them to the orchestrator.
- The orchestrator sees which account and calendar produced each event, so multiple connected calendars remain distinguishable.
- The digest is bounded by account count, event count, selected/visible calendars, and a short agent timeout so heartbeats stay low priority.

The goal is not to make the model announce every calendar item. The goal is to give COSMIC enough situational awareness to notice events that are newly added, materially changed, imminent, preparation-heavy, or connected to the user's current goals.

## Delivery Behavior

Useful heartbeat responses use the existing response path. That means:

- Desktop receives the note when connected.
- If desktop is offline, Gateway can target the active mobile app.
- If desktop and mobile chat are both silent and Cosmic Mail is configured, Gateway selects email as the offline reach path and tells the heartbeat model that it can deliver via email.
- Otherwise Gateway can target the latest mobile push target or queue for desktop.
- Mobile push labels heartbeat responses with a heart so the user knows it is proactive.
- If the heartbeat decides the item is better mailed, it may use Cosmic Mail/email capabilities instead of forcing a chat note.
- Cross-channel sync continues to work for visible heartbeat notes.

If the user opens and handles an item through mobile, future deduplication should avoid replaying stale proactive items when desktop returns.

## Safety And Quality

Heartbeat should be conservative. Good heartbeat notes are:

- Timely.
- Short.
- Specific.
- Connected to something the user actually cares about.
- Actionable or clarifying.

Bad heartbeat notes are:

- Generic productivity nudges.
- Repeated restatements of known tasks.
- Speculation without a source.
- System chatter about the heartbeat itself.
- Anything that would feel like spam.

## Future Work

- Add explicit inbox and Cosmic Mail approval summarizers into the heartbeat context packet.
- Add a "quiet hours" policy once user preference UI exists.
- Add deduplication against recently surfaced heartbeat insights.
- Wire automatic stats-API / URL probes (`check_kind=url_probe`) without sandbox approval per beat.
- Liveness: N consecutive `inconclusive` watchpoint checks should force a delivery ("this watch has been blind").
- Add smarter salience scoring before calling the orchestrator, so some cycles can be skipped without an LLM call.
- Add active task health checks for long-running Alpha/Cursor/Codex jobs.
- Add user-tunable channels: desktop only, mobile push, both, or silent digest.
