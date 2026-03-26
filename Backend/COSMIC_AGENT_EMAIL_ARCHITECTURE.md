# COSMIC Agent Email Architecture

Extends:
- [cosmic_architecture.md](./cosmic_architecture.md)

Status:
- This document describes the intended and implemented v1 architecture for Agent Email in COSMIC.
- Cosmic Mail is an external service. COSMIC is only an API client.

## 1. Final split

Agent Email in COSMIC is deliberately split into two pieces:

1. **Gateway email channel adapter**
- file: `gateway/channels/agent_email.py`
- role: transport only
- responsibilities:
  - verify Cosmic Mail webhooks
  - normalize inbound email into Gateway message shape
  - deliver already-final outbound content through Cosmic Mail
  - preserve email-thread session identity

2. **Email specialist**
- agent id: `cosmic/email-agent:1.0.0`
- directory: `agents/email_agent/`
- role: email cognition
- responsibilities:
  - read/search/summarize email threads
  - compose/reply/send email through Cosmic Mail
  - maintain standing instructions
  - store email-private artifacts and session runs
  - use internal MiMo reasoning where appropriate

This follows the same COSMIC design rule as other channels:
- adapters do transport and normalization
- specialists do domain reasoning

## 2. Design rules

1. **Same TaskEnvelope / EventEnvelope contract**
- The email specialist is a normal COSMIC Redis/AgentRuntime worker.
- No special transport contract is introduced.

2. **No always-on orchestrator email prompt block**
- Email-specific orchestration guidance is not embedded globally into Opus.
- Email guidance lives on the email agent card as `usage_hints`.
- Opus should only see those hints if:
  - the specialist is featured/high-relevance, or
  - it looks the specialist up through `agent_catalog_search`.

3. **Simple delivery can bypass the specialist**
- If the content is already final, cron/heartbeat/direct delivery to email can go through the Gateway adapter.
- That is transport, not email reasoning.

4. **Real email work goes through the specialist**
- reading a thread
- summarizing a thread
- drafting/replying/sending
- searching email
- tone/style handling
- standing-instruction auto-reply logic

6. **Inbound email is specialist-first even though the adapter still routes to Opus**
- The adapter normalizes inbound email into a normal Gateway request with `route_override = "opus"`.
- Before Gateway dispatches that request to Opus, it runs a best-effort `email.process_inbound` child task.
- If that specialist run succeeds, Gateway rewrites the effective Opus query into a compact specialist-generated brief.
- If that specialist run fails or the email specialist is unavailable, Gateway falls back to the raw adapter-normalized email summary.
- This preserves the normal Gateway -> Orchestrator entrypoint while keeping inbound email cognition specialist-owned.

5. **Opus should pass a compact brief, not a raw transcript**
- For long conversations that end with “write an email on this”, Opus should pass:
  - `goal`
  - optional `context_brief`
  - optional `draft_seed`
  - recipient/subject/send intent
- It should not dump the whole session transcript into the email specialist.

## 3. Session model

Email threads do **not** use the normal daily-reset session model.

The Gateway email adapter creates deterministic thread-scoped sessions:

```text
email-thread:<mailbox_key>:<thread_id>
```

Example:

```text
email-thread:support@example.com:thr_123
```

Required metadata on inbound normalized messages:
- `session_scope = "email_thread"`
- `rollover_exempt = true`
- `thread_id`
- `message_id`
- `mailbox_id`
- `mailbox_address`

This keeps:
- thread history stable
- 4 AM daily compaction/rollover from breaking live email threads
- email session lineage explicit and compatible with COSMIC’s session/task model

## 4. Gateway email adapter

Implemented at:
- `gateway/channels/agent_email.py`

The adapter is responsible for:
- webhook signature verification
- content summary construction for inbound email
- channel id generation:
  - `agent-email:<mailbox_address>`
- session id generation:
  - `email-thread:<mailbox_key>:<thread_id>`
- transport-only outbound send through:
  - Cosmic Mail draft creation
  - Cosmic Mail send

The adapter should **not**:
- decide how to reply
- own standing instructions
- parse attachments for Opus by default
- perform email-domain reasoning

Inbound runtime behavior:
- `normalize_message(...)` still produces a normal Gateway request and `route_override = "opus"`.
- Gateway then runs `email.process_inbound` before building the orchestrator task for that inbound email.
- Opus receives the specialist-generated brief as the effective query when that pre-pass succeeds.
- The raw adapter summary remains the fallback if the specialist path is unavailable.

## 5. Email specialist

Implemented at:
- `agents/email_agent/`

LLM choice:
- Xiaomi MiMo via OpenAI-compatible endpoint
- default model: `mimo-v2-pro`

Current specialist intents:
- `email.process_inbound`
- `email.reason`
- `email.manage_instruction`
- `email.recall_session`

### 5.1 `email.process_inbound`

Use for:
- inbound email understanding
- summarizing a real thread
- checking standing instructions
- optional auto-reply application

Expected inputs:
- `thread_id`
- `message_id`
- optional mailbox context

Expected behavior:
- fetch thread from Cosmic Mail
- download attachment files into the email-agent artifact area
- summarize the thread
- apply matching standing instruction if configured

Gateway uses this intent automatically for real inbound `agent-email` messages before dispatching the parent request to Opus.

### 5.2 `email.reason`

This is the main orchestrator-facing email entrypoint.

Use for:
- summarize a thread
- reply to a thread
- search mail
- draft/send a new email from a compact brief

Expected inputs:
- `goal`
- optional `thread_id`
- optional `query`
- optional `context_brief`
- optional `draft_seed`
- optional recipients / subject / send flag

Recommended orchestrator handoff:

```json
{
  "goal": "Email Arun the latest YC spreadsheet and keep it concise.",
  "context_brief": "User wants to send Arun the most recent YC company sheet and keep the note professional and brief.",
  "draft_seed": "Attached is the latest YC company sheet we discussed.",
  "to_recipients": [{"email": "arun@example.com", "name": "Arun"}],
  "subject": "Latest YC company sheet",
  "send": false
}
```

### 5.3 `email.manage_instruction`

Use for:
- set/list/enable/disable/remove standing email rules

This is the email specialist’s private rule ledger, not a global Gateway feature.

### 5.4 `email.recall_session`

Use for:
- exact recall of prior email-agent runs from the email specialist’s private session ledger

## 6. Usage hints

The email agent card should carry compact `usage_hints`.

Canonical guidance:
- Use this specialist for almost anything involving reading, understanding, drafting, replying to, or sending email.
- Pass a compact `context_brief` and optional `draft_seed`; do not pass a long raw conversation transcript.
- Do not use this specialist for simple already-final cron or heartbeat delivery to an email channel.

These hints are specialist-time / lookup-time guidance.
They are **not** global orchestrator prompt text.

## 7. Orchestrator behavior

### 7.1 What Opus should do directly

Opus may directly use the Gateway email adapter only when the content is already final, for example:
- cron delivery to an email channel
- heartbeat/status delivery to an email channel
- already-final text that just needs transport

### 7.2 What Opus should delegate

Opus should delegate to the email specialist for:
- inbound email reasoning
- finding/searching email
- summarizing a thread
- drafting or replying with tone/context
- sending a message that needs email-native reasoning

Current v1 orchestrator exposure:
- No always-on dedicated `email_*` wrapper tools are required.
- The email specialist is reached through the normal COSMIC specialist path:
  - featured/high-relevance shortlist when available
  - `agent_catalog_search`
  - `delegate_to_agent`
- Email-specific usage guidance comes from the email agent card `usage_hints`, not a permanent global prompt block.

### 7.3 Long-session compose edge case

If the user has been discussing something for a long time and says “okay, write an email on this”:
- Opus should compress the conversation into:
  - `context_brief`
  - optional `draft_seed`
- then delegate to `email.reason`

This avoids passing a raw long transcript while still letting the specialist own final email behavior.

## 8. Attachments

Current v1 rule:
- inbound email attachments are downloaded into the email agent’s artifact area
- they are **not** automatically pushed into Opus-visible session artifacts on every email turn

Storage location:
- `runs/artifacts/<task_id>/email_agent/attachments/...`

Why:
- attachment handling is often email-private
- later, other specialists can receive attachments through normal COSMIC artifact rebinding if needed
- we avoid locking the whole system into “all email attachments are globally visible”

Future direction:
- add explicit attachment analysis/extraction policies
- optionally route specific attachments to docs/tabular only when requested or clearly needed

## 9. Cron and channel delivery

Cron email delivery should use:
- `delivery_channel = agent-email` or `agent-email:<mailbox>`
- final rendered content

At send time:
- Gateway resolves the email channel adapter
- adapter creates/sends through Cosmic Mail

This path does **not** require the email specialist unless send-time cognition is needed.

Gateway channel resolution already normalizes:
- `email`
- `primary_email`
- `agent-email`

into the email channel platform when the adapter is configured.

## 10. Redis / TaskEnvelope / usage logging

The email specialist must remain a standard COSMIC specialist:
- Redis Streams transport
- `TaskEnvelope` input
- `EventEnvelope` output
- registry heartbeat / health
- no side-channel orchestration

Usage logging:
- deterministic specialist operations log to Gateway usage DB
- internal MiMo calls also log usage to Gateway usage DB

This keeps email aligned with:
- tabular
- docs parser
- x search
- firecrawl

## 11. Gateway integration requirements

Gateway email support is considered active only when:
- `AGENT_EMAIL_ENABLED=true`
- `COSMIC_MAIL_BASE_URL` is configured
- `COSMIC_MAIL_API_TOKEN` is configured

Optional:
- `COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS`
- `COSMIC_MAIL_WEBHOOK_SECRET`
- `COSMIC_MAIL_WEBHOOK_SIGNATURE_HEADER`

If the email channel is not configured:
- there should be no always-on email-specific orchestrator prompt guidance
- the email specialist should not be treated as an active connected capability

## 12. Bootstrap / systemd

The email specialist should be provisioned like other COSMIC agents:
- repo env:
  - `agents/email_agent/agent.env`
- system env:
  - `/etc/cosmic/agents/email-agent.env`
- systemd unit:
  - `cosmic-email-agent.service`

Bootstrap should:
- render the env file
- install/sync it
- enable the service only when Cosmic Mail credentials are configured
- wait for orchestrator registry health on:
  - `cosmic/email-agent:1.0.0`

## 13. What is intentionally not in v1

- no raw SMTP/IMAP logic inside COSMIC
- no always-on global email prompt block in orchestrator
- no many-email-wrapper tool explosion as the default model
- no automatic global attachment fan-out on every inbound email
- no claim that cron delivery requires the specialist

## 14. Summary

The final COSMIC email shape is:
- **Gateway adapter for transport**
- **Email specialist for cognition**
- **MiMo inside the specialist**
- **thread-scoped sessions**
- **lookup-time usage hints**
- **compact Opus handoff briefs**
- **attachments kept inside the email agent unless explicitly rebound later**

That is the architecture this implementation should follow.
