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
  - use internal internal LLM reasoning where appropriate

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
- Xiaomi internal LLM via OpenAI-compatible endpoint
- default model: `gpt-5.6-luna`

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
- load the active standing instructions for the mailbox from the email-agent ledger
- use the email-agent LLM to decide whether any standing instruction matches this inbound email
- return matched-instruction context, sender role, and any recommended next action back to Gateway/Opus
- apply matching standing instruction if configured

Gateway uses this intent automatically for real inbound `agent-email` messages before dispatching the parent request to Opus.

Important rule:
- Opus should **not** receive the raw inbound email first and then ask the email agent whether any standing instruction applies.
- Gateway should call `email.process_inbound` first.
- The email specialist is responsible for checking its own standing-instruction ledger before Opus sees the inbound email.

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

Standing instructions belong to the email specialist, not Opus.

This means:
- users may express these rules in plain language
  - example: `Keep an eye out for emails from Arun and let me know.`
  - example: `Watch for anything mentioning Q3 in email.`
- Opus should delegate that request once to `email.manage_instruction`
- the email specialist should persist it in its own ledger
- future inbound email should consult that ledger inside `email.process_inbound`
- Opus should not be the long-term memory holder for these rules

### 5.3.1 Standing instruction source of truth

The durable source of truth should be a private email-agent table, not a raw prompt-only todo file.

Current v1 implementation uses:

```sql
CREATE TABLE IF NOT EXISTS email_instructions (
    instruction_id TEXT PRIMARY KEY,
    mailbox_address TEXT,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_from_address TEXT,
    match_subject_contains TEXT,
    match_body_contains TEXT,
    behavior_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_instructions_mailbox
ON email_instructions (mailbox_address, enabled);
```

This table is the durable control plane for:
- create/list/enable/disable/remove
- mailbox scoping
- auditability
- future lifecycle fields

Recommended evolution for the standing-instruction ledger:
- keep the SQL table as the source of truth
- allow natural-language rules to be stored as structured instruction records plus an LLM-facing summary
- extend the table or a companion table with fields such as:
  - `raw_user_instruction`
  - `instruction_kind`
  - `completion_mode`
  - `last_triggered_at`
  - `completed_at`
  - `last_action_thread_id`
  - `last_action_message_id`
  - `matching_hints_json`

The key design rule is:
- persistence/state belongs in the DB
- contextual matching belongs in the email-agent LLM

### 5.3.2 Standing instruction matching model

Standing-instruction matching should be specialist-owned and LLM-assisted.

Recommended behavior:
1. `email.process_inbound` loads **all active instructions** for the mailbox from the private ledger.
2. It gives the inbound email snapshot plus those active instruction summaries to the email-agent LLM.
3. The email-agent LLM decides:
- matched instruction(s)
- why they matched
- whether the result is ambiguous
- which behavior mode applies
4. The structured result is returned to Gateway and then to Opus as part of the inbound brief.

Important note:
- the DB is the source of truth
- the LLM is the matcher/reasoner
- deterministic candidate filtering can exist later as a performance optimization
- it is **not** required for the core architecture

This allows user requests like:
- `Keep an eye out for emails from Arun and let me know.`
- `Watch for anything mentioning Q3.`
- `Tell me if someone asks for the deck.`

without forcing users to provide exact sender email addresses every time.

### 5.3.3 End-to-end standing instruction lifecycle

The intended end-to-end lifecycle is:

1. User tells COSMIC a standing email rule.
2. Opus delegates that request to `email.manage_instruction`.
3. The email specialist stores it in the private ledger.
4. A future inbound email reaches Gateway through the Agent Email webhook.
5. Gateway calls `email.process_inbound` before Opus sees the message.
6. `email.process_inbound`:
- loads active instructions from the ledger
- uses the email-agent LLM to determine matches
- returns the matched instruction context in a structured inbound brief
7. Gateway hands that enriched brief to Opus.
8. Opus reasons about the user-facing response and may delegate additional email-native work back to the email specialist.
9. If an outbound email is actually sent, the result should flow back to the email specialist so the instruction lifecycle can be updated.

Instruction lifecycle ownership rules:
- email-agent owns instruction status
- Opus does not own completion state
- successful outbound delivery, not just draft generation, is the authoritative signal for marking one-shot instructions complete
- recurring/perpetual instructions should remain active and only update fields like `last_triggered_at`

Recommended completion semantics:
- one-shot / finite instruction:
  - mark `completed`
  - record when and on which thread/message it was satisfied
- recurring / perpetual instruction:
  - keep `enabled`
  - update `last_triggered_at`
  - optionally record the last matching thread/message

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
- internal internal LLM calls also log usage to Gateway usage DB

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
- **internal LLM inside the specialist**
- **thread-scoped sessions**
- **lookup-time usage hints**
- **compact Opus handoff briefs**
- **attachments kept inside the email agent unless explicitly rebound later**

That is the architecture this implementation should follow.

## 15. Planned Attachment Parsing Extension

This section defines the next attachment architecture that should be added on top of the current v1 download-only behavior.

### 15.1 Goal

Inbound email attachments should support:
- automatic intake into the email agent
- correct durable mapping to:
  - `mailbox_address`
  - `thread_id`
  - `message_id`
  - `attachment_id`
- best-effort automatic parsing for supported document attachments
- later granular reads using the **existing docs specialist surface**
  - chunk-level reads
  - full-document reads
  - exact asset fetch/reinspection

This must reuse existing COSMIC specialists and artifact contracts.
It must not introduce a second custom document-reading system inside the email agent.

### 15.2 Ownership split

The ownership model remains strict:

1. **Gateway email adapter**
- receives webhook payloads
- normalizes attachment metadata only
- does not download bytes
- does not parse with Docling
- does not decide cross-specialist attachment policy

2. **Email specialist**
- owns raw attachment intake
- owns attachment-to-thread/message/mailbox mapping
- decides which attachments are parse candidates
- owns the compact user-facing attachment summary returned to Opus

3. **Docs parser specialist**
- remains the only canonical Docling-based parse surface
- owns:
  - `docs.parse_bundle`
  - `docs.browse_bundle`
  - `docs.search_bundle`
  - `docs.read_bundle`
  - `docs.fetch_asset`
  - `docs.reinspect_asset`

This means:
- **email agent owns attachment intake**
- **docs parser owns parsed-document structure**

### 15.3 End-to-end inbound flow

The intended inbound attachment flow is:

1. Cosmic Mail webhook reaches Gateway.
2. Gateway email adapter normalizes:
- `message_id`
- `thread_id`
- `mailbox_id`
- `mailbox_address`
- attachment metadata list

3. Gateway dispatches `email.process_inbound`.

4. `email.process_inbound`:
- fetches the latest thread/message context
- downloads raw attachment bytes into the email-agent artifact area
- emits `ArtifactManifest` records for those raw attachment files
- writes durable attachment ledger rows
- classifies each attachment into one of:
  - `docs_parse_candidate`
  - `tabular_candidate`
  - `image_or_binary_only`
  - `unsupported_or_skip`

5. For supported document attachments, the system should run a best-effort follow-on `docs.parse_bundle` child task using normal `TaskEnvelope.input_artifacts`.

6. Docs parser emits canonical parsed bundle outputs under its own artifact area and returns:
- `bundle_id`
- parsed-document artifacts
- indexes/assets for later retrieval

7. The email attachment ledger is updated with:
- `parse_status`
- `parser_agent_id`
- `parser_task_id`
- `bundle_id`
- parse error info when parsing fails

8. Gateway then sends Opus a compact inbound brief that includes:
- email-thread summary
- attachment list
- parse status
- any parsed `bundle_id` values
- short extracted summaries where available

9. Later user questions about the attachment use the cached `bundle_id` and the existing docs specialist tools for precise reads.

### 15.4 Automatic parse policy

The automatic parse policy should be conservative.

Auto-parse by default:
- `application/pdf`
- `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `application/vnd.openxmlformats-officedocument.presentationml.presentation`

Do not auto-parse through docs parser:
- spreadsheets
  - `csv`
  - `tsv`
  - `xlsx`
  - `xlsb`
- standalone images
- audio/video
- arbitrary binaries

Reason:
- document attachments should reuse Docling/docs-parser
- spreadsheet attachments belong to the tabular path later
- images should not be forced through document parsing unless explicitly needed

### 15.5 File structure

Raw attachment files should remain in the email agent artifact area.

Recommended raw attachment layout:

```text
runs/artifacts/<email_task_id>/email_agent/
├── inbound_email.json
├── downloaded_attachments.json
├── attachment_index.json
└── attachments/
    └── <message_id>/
        ├── <attachment_id>__<safe_filename>
        ├── <attachment_id_2>__<safe_filename>
        └── ...
```

Rules:
- raw attachment bytes stay under the email agent’s own task artifacts
- the path must be deterministic enough to map back to:
  - `message_id`
  - `attachment_id`
- filenames must be sanitized but still human-readable

Parsed document outputs should **not** be copied into the email agent directory.
They should remain where the docs parser already owns them:

```text
runs/artifacts/<docs_task_id>/docs_parser/<source_artifact_id>/
├── document.md
├── manifest.json
├── chunk_index.json
├── tables.json
├── figures.json
└── assets/
    ├── tables/
    ├── figures/
    └── ...
```

This keeps each specialist responsible for its own artifact tree.

### 15.6 Durable attachment mapping

The email agent should maintain durable attachment records in its private store DB.

Recommended new tables:

1. `email_attachment_records`
- `attachment_record_id`
- `mailbox_address`
- `thread_id`
- `message_id`
- `attachment_id`
- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- `raw_artifact_id`
- `raw_artifact_path`
- `source_task_id`
- `created_at`
- `updated_at`

2. `email_attachment_parse_runs`
- `attachment_record_id`
- `parse_kind`
  - `docs`
  - later `tabular`
- `parse_status`
  - `pending`
  - `parsed`
  - `failed`
  - `skipped`
- `parser_agent_id`
- `parser_task_id`
- `bundle_id`
- `error_code`
- `error_message`
- `created_at`
- `updated_at`

Required indexes:
- `(thread_id, created_at DESC)`
- `(message_id, created_at DESC)`
- `(attachment_id)`
- `(sha256)`

This gives the system:
- exact attachment recall by email thread/message
- dedupe by `attachment_id` or `sha256`
- stable rebinding into future specialist tasks

### 15.7 Artifact contract

Attachment parsing must use the normal COSMIC artifact contract:
- raw attachment files are emitted as `ArtifactManifest`
- parseable attachments are passed to docs parser via `TaskEnvelope.input_artifacts`
- later reads or downstream work reuse those normalized artifact descriptors

No component should browse raw artifact directories directly from prompts.
All attachment reuse must go through:
- attachment ledger lookup
- normalized artifact descriptors
- standard child-task handoff

This follows:
- `ArtifactManifest`
- `TaskEnvelope.input_artifacts`
- later artifact recall / rebinding
from [cosmic_architecture.md](./cosmic_architecture.md)

### 15.8 Opus-facing behavior

Opus should **not** automatically receive raw attachment bytes or full parsed bundles on every inbound email turn.

Instead, the inbound brief should include compact attachment metadata such as:
- filename
- mime type
- size
- whether it was downloaded
- whether it was parsed
- `bundle_id` if parsed
- short extracted summary if available

Example shape:

```json
{
  "attachments": [
    {
      "attachment_id": "att_123",
      "message_id": "msg_456",
      "thread_id": "thr_789",
      "filename": "Quarterly_Update.pdf",
      "mime_type": "application/pdf",
      "downloaded": true,
      "raw_artifact_id": "art_raw_123",
      "parse_status": "parsed",
      "parse_kind": "docs",
      "bundle_id": "bundle_abcd1234",
      "summary": "Quarterly update deck covering revenue, hiring, and risks."
    }
  ]
}
```

This keeps Opus lean while still making the attachment actionable.

### 15.9 How later reads should work

Once an attachment has a parsed `bundle_id`, later user questions should use the normal docs specialist flow.

Examples:
- “Read the attached PDF”
  - use `docs.read_bundle`
- “Search the deck for hiring plans”
  - use `docs.search_bundle`
- “What does page 7 say?”
  - use `docs.read_bundle`
- “Open the table from the attached report”
  - use `docs.fetch_asset`
- “Reinspect the chart image from the attachment”
  - use `docs.reinspect_asset`

Important rule:
- do **not** invent a parallel email-only chunk-read API if a parsed `bundle_id` already exists
- the existing docs specialist remains the structured read surface

### 15.10 Tool surface

No large new always-visible orchestrator tool set is required for this.

The tool behavior should be:
- inbound automatic parsing happens behind `email.process_inbound`
- deeper document interaction uses existing docs tools
- later spreadsheet-specific attachment handling can use the tabular specialist

So the orchestrator model remains:
- email specialist for email cognition and attachment ownership
- docs specialist for parsed document navigation
- tabular specialist later for spreadsheet attachments

### 15.11 Audience and UI rules

By default:
- raw email attachments
- downloaded attachment metadata
- parse support artifacts
- docs-parser intermediate outputs
should remain `supporting`, not user-deliverable download cards

Only explicit user-requested outputs should become deliverable.

This prevents inbound attachment plumbing from flooding the desktop UI with internal artifacts.

### 15.12 Non-goals

This extension should **not** do any of the following:
- make all inbound email attachments globally visible to Opus by default
- move Docling logic into Gateway
- duplicate Docling logic inside email agent
- create a second email-only document-reading stack
- force spreadsheets through docs parser
- silently reparse the same attachment on every revisit when a cached parsed result already exists

### 15.13 Final rule

The correct final shape is:
- **raw attachment ownership stays in email agent**
- **parsed document ownership stays in docs parser**
- **Opus sees compact attachment summaries, not raw bytes by default**
- **granular and full-document reads reuse the existing docs specialist surface**

That is the attachment architecture this system should implement.
