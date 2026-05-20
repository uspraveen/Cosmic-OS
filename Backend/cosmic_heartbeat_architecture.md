# COSMIC Heartbeat Architecture

## Purpose

COSMIC Heartbeat is the ambient attention loop for the system. It is not a reminder engine alone. It is a quiet, periodic consciousness pass that asks: "Is there anything Praveen would genuinely want COSMIC to notice, connect, or surface right now?"

The heartbeat should look wider than calendars and inboxes. It should consider active work, background tasks, approvals, recent conversations, user preferences, long-running interests, open loops, deadlines, relationships, system health, and project context. Its default behavior is silence.

## Product Contract

- Runs every 30 minutes by default.
- User can turn it on or off from Settings -> Preferences.
- Heartbeat prompts are never appended as user messages to chat history.
- If nothing is worth surfacing, the orchestrator must respond exactly `heartbeat_ok`.
- `heartbeat_ok` is suppressed by Gateway and is not stored, streamed, pushed, or shown.
- If there is something useful, COSMIC sends a short, concrete proactive note through the normal response pipeline.
- Heartbeat is low priority and must not interfere with foreground user work.
- Heartbeat may inspect state and use tools when justified, but it must not take external action without explicit standing authorization or approval.

## Context Surface

Each heartbeat receives a compact context packet rather than a replayed chat prompt. The packet may include:

- Current local time and timezone.
- Current daily session id.
- Active working set: goals, workstreams, open loops, focus entities, task refs.
- Recent conversation tail, bounded and summarized.
- Active background tasks and Alpha work.
- Scheduler status and upcoming user crons.
- Delivery queue status for offline or deferred items.
- Stable user preferences and interests from carry-forward/memory.
- Passive memory recall for broad user priorities.

Future expansions should add first-class summaries for:

- Calendar windows and travel/logistics pressure.
- Inbox and Cosmic Mail approvals.
- Mobile/desktop presence.
- Important contacts and relationship context.
- User watchlists: YC, AI research, products, companies, documents, projects.
- External world state only when recent context suggests it matters.

## Prompt Shape

The heartbeat prompt should be stable and strict:

```text
You are COSMIC's Heartbeat: an ambient consciousness pass for the user.
Quietly decide whether there is something genuinely useful to surface now.
Think across calendar commitments, inbox and approval pressure, active projects,
background tasks, reminders, open loops, user interests, preferences, relationships,
recent conversations, and what the user would likely want to know at this moment.
Use specialist/local tools only when a check is clearly worth it.
Do not take external actions unless the user already granted standing authorization.
If there is nothing useful enough to interrupt for, respond exactly heartbeat_ok and nothing else.
If there is something useful, respond with a short, concrete, low-drama note;
do not say this came from a heartbeat.
```

The model must treat the context packet as private state, not as a user message to quote back.

## Gateway Implementation

Gateway owns the schedule because Gateway already owns sessions, delivery, preferences, and offline queues.

- `scheduler_store.heartbeat_config` stores interval, status, next fire, last result, and pause state.
- `preferences.app_preferences.cosmic_heartbeat` stores the user-facing enabled toggle.
- The scheduler loop calls the heartbeat after due crons.
- Gateway builds a normal `TaskEnvelope` with `source="heartbeat"` and `priority="low"`.
- The heartbeat request uses an empty live conversation context and a compact memory/context block.
- `visual_response_enhancement_enabled` is disabled for heartbeat turns unless explicitly changed later.
- Heartbeat response chunks and progress are not streamed; only a useful final response is delivered.
- `heartbeat_ok` final responses are suppressed before session storage, push, delivery queue, and UI display.

## Delivery Behavior

Useful heartbeat responses use the existing response path. That means:

- Desktop receives the note when connected.
- If desktop is offline, the response can be queued like scheduled cron output.
- Mobile push can still alert the user through the normal response-complete push path.
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

- Add explicit calendar/inbox summarizers into the heartbeat context packet.
- Add a "quiet hours" policy once user preference UI exists.
- Add deduplication against recently surfaced heartbeat insights.
- Add a heartbeat insight ledger for analytics without polluting chat history.
- Add smarter salience scoring before calling the orchestrator, so some cycles can be skipped without an LLM call.
- Add active task health checks for long-running Alpha/Cursor/Codex jobs.
- Add user-tunable channels: desktop only, mobile push, both, or silent digest.
