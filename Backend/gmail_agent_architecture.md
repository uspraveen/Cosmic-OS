# COSMIC Gmail Agent Architecture

## Purpose

Gmail is a first-class user-owned inbox surface for COSMIC. It is separate from Cosmic Mail:

- **Cosmic Mail** handles COSMIC-owned mailboxes, inbound agent email, approval queues, and trusted recipients.
- **Gmail Agent** handles the user's connected Gmail / Google Workspace inboxes through Google OAuth.

The Gmail agent lets COSMIC search, read, summarize, triage, and draft replies against one or more user-connected Gmail accounts while preserving explicit approval before high-impact actions.

## Final Autonomy Model

Gmail should be a first-class event surface without making the orchestrator inspect every email. The production split is:

- **Gmail Agent**: inbox sensor, semantic triage, thread reader, draft creator, sender/noise learning.
- **Gateway**: webhook receiver, credential owner, surfaced-item ledger, delivery router, future semantic trigger matcher.
- **Orchestrator**: cross-domain reasoning and action planner when a surfaced Gmail item, user follow-up, heartbeat, or standing automation requires broader context.

This means COSMIC can stay quiet for noise, surface important mail quickly, and still escalate to the orchestrator when an email affects active goals, projects, calendar, docs, Alpha work, or user-facing decisions.

## Context Stack

The Gmail agent must not be blind, but it also should not receive the whole orchestrator prompt or raw session history on every inbound message. Gmail tasks use a focused context stack:

1. **Native email context**: Gmail thread, participants, subject, timestamps, labels, snippets, body previews, and attachment metadata.
2. **Shared memory retrieval**: sender/domain, subject, snippet, entities, project names, and current email facts are used as search keys against COSMIC memory. This lets a YC email retrieve prior YC/S26/application context without hardcoding YC.
3. **Current user state**: Gateway injects a compact active-working-set brief into inbound Gmail processing so the agent knows current goals, open loops, and focus entities.
4. **Gmail-local state**: prior triage decisions, learned sender/domain prefilters, watch cursor state, and `learnings.md`.

The Gmail agent should use this stack to decide whether an inbound email is actionable. If the email requires cross-domain work, the agent returns a surfaced item and Gateway/orchestrator take over from the compact Gmail reference.

## Surfaced Gmail Continuity

When Gmail Agent marks an inbound item `surface_to_user=true`, Gateway stores a compact reference in `gateway/gmail_context.db` before notifying Desktop/Mobile. The record contains only metadata:

- `account_id`, `account_email`
- Gmail `thread_id`, `message_id`
- subject, sender, category, priority, reason, suggested action
- source task id and timestamps

Full email bodies remain in Gmail. Later user turns receive a **Recent Surfaced Gmail Items** context block. If the user says "reply to this", "open that email", or "make the doc from it", the orchestrator can resolve the most recent matching surfaced item and delegate `gmail.read_thread` or `gmail.draft_reply` with exact refs. If multiple surfaced items plausibly match, it should ask a concise clarifying question.

User-facing Gmail interruptions are orchestrator-owned. After Gateway stores surfaced refs, it creates a private orchestrator decision turn containing the Gmail Agent evidence, active working set, and memory context. The orchestrator returns a structured decision to either `deliver` a concise note/question to the user or `suppress` the interruption. Gateway may broadcast low-level sync events to clients, but conversational Gmail messages and push-worthy wording should come from this orchestrator decision path, not directly from the Gmail Agent.

Attachment continuity follows the same rule. Gmail triage and thread reads include attachment metadata (`message_id`, `attachment_id`, filename, MIME type, size), but the agent does not push raw attachment bytes into prompts or auto-parse every attachment. When the user/task actually needs the file, the orchestrator delegates `gmail.fetch_attachment`; Gmail Agent downloads that single attachment into a private COSMIC artifact and returns it through the normal `TaskEnvelope` artifact path so docs, tabular, Alpha, or another specialist can consume it via `input_artifacts`.

## Semantic Event Automations

Standing instructions such as "when Arun emails me, create the requested doc" should be represented as generic event automations, not Gmail-specific hardcoded rules.

Implemented core primitives:

- **Event**: `gmail.inbound` today; the registry is generic enough for calendar updates, files, custom webhooks, and other provider events later.
- **Condition**: compact semantic intent such as sender identity, subject/topic, attachment hint, urgency, or "needs reply".
- **Context resolver**: Gateway passes Gmail refs plus active working set and memory context to the orchestrator. The future task then delegates `gmail.read_thread`, `gmail.draft_reply`, Alpha, docs, memory, or other specialists as needed.
- **Action plan**: high-level orchestrator task such as draft reply, create doc, notify user, run Alpha, or schedule reminder.
- **Confidence policy**: auto-run high-confidence safe work, ask/verify on medium confidence, suppress low-confidence.
- **Approval policy**: prep work and draft creation can run autonomously; sending, deletion, sharing, external commitments, and irreversible changes require approval unless the user has given a standing trusted policy.
- **Learning loop**: confirmations/corrections update identity bindings and preferences so future matches become deterministic.

Unresolved people are valid. A rule can store `person_ref="Arun"` with no email address and `resolution_mode="resolve_on_event"`. On each inbound event, Gateway uses an LLM-first semantic adjudicator to decide whether the standing instruction applies to the Gmail event. The adjudicator sees the raw instruction, structured condition/action, sender identity, account, subject, snippet, Gmail-agent triage reason, and safety policy, then returns `matched`, `needs_confirmation`, or `ignore` with confidence and evidence. Exact sender email/domain mismatches remain deterministic safety rails, and a small deterministic fallback exists only for degraded operation when the LLM path is unavailable.

Current implementation:

- Gateway stores standing instructions in `gateway/event_automations.db`.
- Orchestrator tools: `create_event_automation`, `list_event_automations`, `delete_event_automation`.
- Gmail inbound surfacing writes `gateway/gmail_context.db`, broadcasts Desktop/Mobile, then evaluates active `gmail.inbound` automations.
- Match records are deduped by `(automation_id, event_ref)` so a single Gmail message does not repeatedly fire the same rule.
- Matching evidence is attached to the future orchestrator task; the orchestrator must read exact Gmail thread context before doing consequential work.

## Core Architecture

```
Desktop/Mobile Settings
  -> Gateway Google OAuth Credential Manager
  -> Orchestrator delegate_to_agent
  -> Gmail Agent
  -> Gmail REST API
```

The Gateway remains the owner of OAuth tokens. The Gmail agent never stores refresh tokens. For every Gmail task, the orchestrator resolves a short-lived access token from the Credential Manager using the agent card's `auth_requirements`, injects it into `TaskEnvelope.input.auth`, and dispatches the task over Redis Streams.

## Agent Identity

- Agent id: `cosmic/gmail-agent:1.0.0`
- Directory: `Backend/agents/gmail_agent/`
- Main service: `cosmic-gmail-agent.service`
- Env file: `/etc/cosmic/agents/gmail-agent.env`

## OAuth Scope Strategy

The Google Workspace settings UI currently enables Gmail with:

```
https://www.googleapis.com/auth/gmail.modify
```

The Gmail API treats Gmail scopes as separate strings, so an account granted `gmail.modify` will not satisfy a request for `gmail.readonly` if the credential resolver checks exact scope coverage. For compatibility, Gmail agent intents require `gmail.modify`.

`gmail.modify` allows reading and modifying messages, composing and sending messages, but not permanent deletion. The agent still treats mutations as gated operations and defaults to drafts rather than direct sending.

## Primary Intents

| Intent | Purpose |
| --- | --- |
| `gmail.search` | Search messages/threads by query, sender, subject, unread state, or time window. |
| `gmail.read_thread` | Fetch and normalize a complete Gmail thread. |
| `gmail.fetch_attachment` | Download one Gmail attachment into a private COSMIC artifact for downstream specialist handoff. |
| `gmail.triage_inbox` | Run LLM-based inbox triage across bounded recent messages. |
| `gmail.draft_reply` | Create a Gmail draft for a new message or thread reply. Sending remains approval-gated. |
| `gmail.send_draft` | Send a Gmail draft only after Gateway confirms explicit user approval from the Gmail approval surface. |
| `gmail.process_inbound` | Process a push/poll inbound notification with account, history, or message refs. |
| `gmail.heartbeat_digest` | Reconcile cached Gmail triage state for heartbeats without re-triaging the inbox by default. |
| `gmail.morning_briefing_digest` | Run a deliberate broader Gmail scan for morning briefings. |
| `gmail.manage_prefilter` | Add/remove/list durable sender/domain prefilters. |
| `gmail.sync_watch` | Register/renew `users.watch` for a connected Gmail account when Pub/Sub is configured. |
| `gmail.stop_watch` | Stop Gmail push notifications and clear local history cursor state. |
| `gmail.recall_session` | Recall prior Gmail-agent work from the agent ledger. |

## Outbound Approval Surface

Gmail outbound sends are approval-gated like Agent Email, but they use a Gmail-specific queue because the draft lives in the user's own Gmail account, not in Cosmic Mail.

Flow:

1. The orchestrator delegates `gmail.draft_reply` when the user or an automation asks COSMIC to compose/reply through Gmail.
2. Gmail Agent creates a Gmail draft and returns `approval_required=true`, the connected account identity, `draft_id`, recipients, subject, and a compact draft preview.
3. The orchestrator's specialist receipt preserves this Gmail draft metadata.
4. Gateway persists the receipt into `gateway/gmail_approvals.db` and broadcasts a Gmail approval notification to Desktop/Mobile. The chat response can continue normally; approval state is not hidden inside chat text.
5. Desktop Spaces -> Gmail and Mobile Spaces -> Gmail list pending/reviewed approvals. The user can approve or reject each draft.
6. On approve, Gateway resolves the exact Google account credential and dispatches `gmail.send_draft` to Gmail Agent. On reject, Gateway marks the local approval rejected; the Gmail draft remains available in Gmail unless a future explicit delete path is added.

This keeps autonomy and safety separate: the Gmail Agent can draft intelligently, Gateway owns the durable approval queue, and only an explicit user action unlocks sending.

## LLM Spam and Triage

Spam/noise classification belongs to the LLM, not brittle regexes. The deterministic layer is only a prefilter for senders/domains already learned by the Gmail agent.

Flow:

1. Fetch bounded recent messages or threads.
2. Apply the durable sender prefilter.
3. Send remaining compact message summaries to the Gmail agent's cheap internal LLM.
4. The LLM classifies each item as one of:
   - `urgent`
   - `needs_reply`
   - `needs_review`
   - `read_later`
   - `notification`
   - `spam_or_noise`
5. If the LLM is highly confident that a sender/domain is recurring low-value noise, the agent can add it to its sender prefilter with a reason and timestamp.

The prefilter is stored as JSON so it can be inspected, edited, and migrated. It is not a replacement for LLM judgment; it only prevents obvious repeated noise from consuming model calls.

## Multiple Gmail Accounts

Every result includes:

- `account_id`
- `account_email`
- `account_label`
- Gmail `thread_id`
- Gmail `message_id`
- sender/recipient identities
- label ids
- timestamps

If the user connects multiple Gmail accounts and does not provide an account hint, the orchestrator should let the Credential Manager enforce ambiguity. Read-only style work may use primary fallback only when existing auth policy allows it. Responses must name the account when results could be confused.

## Thread Understanding

The Gmail agent treats Gmail threads as the unit of conversation. It fetches messages through `users.threads.get`, normalizes headers and bodies, keeps message order, and gives the LLM compact thread context for summaries and draft replies.

## Approval Gating

Safe by default:

- Search
- Read
- Summarize
- Triage
- Create Gmail drafts
- Download specific Gmail attachments into private COSMIC artifacts

Approval required or explicit user confirmation required:

- Send a draft
- Archive/delete/label large batches
- Mark important/unimportant
- Modify filters or prefilters when confidence is low

V1 creates drafts and returns the Gmail `draft_id`. Sending can be layered into the existing approval surfaces once the UI has a Gmail-specific approval renderer.

## Heartbeats and Morning Briefings

Heartbeat checks should not read or LLM-triage the entire inbox. In steady state, Gmail push/poll processing is the primary inbox-awareness path: inbound messages are classified as they arrive, and decisions are saved in `gmail_triage_decisions`.

The gateway/orchestrator should ask `gmail.heartbeat_digest` for reconciliation only:

- Return recently cached actionable items.
- Check pending/high-priority triage decisions already known to the agent.
- Avoid live LLM triage unless `allow_live_check=true` and webhook/cursor state is missing or stale.
- Return an empty `items` list with `no_cached_actionable_items` when there is nothing worth surfacing.

Morning briefing is different. `gmail.morning_briefing_digest` may run a bounded live LLM triage scan over overnight/recent mail because it is a deliberate scheduled summary, not a 30-minute heartbeat. It should still respect sender prefilters, account identity, and max item limits.

## Push Notifications / Webhooks

Gmail push notifications require Google Cloud Pub/Sub:

- A topic must exist.
- Gmail API service account must be allowed to publish.
- `users.watch` must be registered per Gmail account.
- Watches expire and must be renewed.
- Pub/Sub push messages carry Gmail `emailAddress` and `historyId`; the agent then uses `users.history.list` to fetch changes since the last stored `historyId`.

COSMIC supports this path through Gateway endpoint:

```
POST /webhooks/gmail/pubsub?secret=<GATEWAY_GMAIL_WEBHOOK_SECRET>
```

The endpoint acknowledges Pub/Sub quickly, then dispatches `gmail.process_inbound` in the background. `gmail.process_inbound` replays Gmail history from the stored cursor, triages changed Inbox messages, records decisions, and advances the cursor. If the stored cursor is missing, the first notification seeds the cursor rather than replaying unknown history. If Gmail reports the cursor is stale, the agent runs a bounded recent-inbox fallback and stores the new cursor.

Watch lifecycle:

- Google OAuth connect or enabling the Gmail tool schedules `gmail.sync_watch`.
- Disabling the Gmail toggle or disconnecting a Google account calls `gmail.stop_watch` before Gmail use is disabled/revoked.
- Gateway periodically renews enabled Gmail watches with `gmail.sync_watch` so watch expiration does not silently disable inbound Gmail awareness.
- Heartbeats only use Gmail accounts whose Gmail tool is enabled.

COSMIC must not pretend webhook delivery is active unless the VM has a Pub/Sub topic configured in `GMAIL_WATCH_TOPIC_NAME` and the Pub/Sub push subscription points at the Gateway endpoint above. Until then, explicit triage and morning briefing scans can provide inbox awareness; heartbeat reconciliation should remain cheap and state-based.

## Persistent State

The Gmail agent owns:

- `store/data/gmail_agent.db` for session runs, history cursor state, and triage decisions.
- `store/sender_prefilter.json` for learned sender/domain prefilters.
- `store/learnings.md` for durable operational notes.

Refresh tokens remain only in the Gateway Credential Manager.

## Design Principles

- Reuse COSMIC's existing Google OAuth Credential Manager.
- Reuse Redis Streams, AgentRuntime, StepPlan, and MemoryRead/MemoryWrite.
- Keep full email bodies out of global memory unless a durable user-relevant fact should be remembered.
- Prefer compact message/thread summaries in orchestrator outputs.
- Make account identity explicit.
- Keep destructive Gmail operations out of V1 unless an explicit approval path exists.
