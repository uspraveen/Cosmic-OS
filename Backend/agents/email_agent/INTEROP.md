# Email Agent Interop

`cosmic/email-agent:1.0.0` is a normal COSMIC specialist:

- Redis `TaskEnvelope` transport via `AgentRuntime`
- registry heartbeat / healthy instance discovery
- Gateway usage logging for both deterministic operations and MiMo calls
- per-task artifacts under `runs/artifacts/<task_id>/email_agent/`
- transport-only cron/final-content delivery still belongs to the `agent-email` Gateway adapter, not this specialist

Gateway inbound behavior:
- real inbound `agent-email` messages are normalized by the adapter first
- Gateway then runs `email.process_inbound` as a child specialist task before sending the parent request to Opus
- if that succeeds, Opus receives a compact specialist-generated brief instead of the raw adapter summary
- if that fails, Gateway falls back to the raw normalized email summary

## Primary intents

- `email.process_inbound`
- `email.handle`
- `email.reason` (legacy alias)
- `email.manage_instruction`
- `email.recall_session`

## Attachment policy

Inbound email attachments stay inside the email agent artifact area by default. They are not automatically staged into Opus-visible `input_artifacts` on every inbound email turn.

## Orchestrator handoff

Opus should typically delegate with:

- `intent = email.handle`
- `goal`
- optional `context_brief`
- optional `draft_seed`
- optional `draft_id` when continuing a previously created draft
- optional `thread_id`
- optional `to_recipients` / `cc_recipients` / `bcc_recipients`
- optional `subject` / `send` flag
- `artifact_ids` when reusing a previously produced COSMIC file as an attachment

Avoid passing long raw session transcripts.
Use `context_brief` and `draft_seed` instead when the user has been discussing something for a long time and then asks to send an email.

Attachment-note:
- for new outbound drafts, the specialist uploads any `TaskEnvelope.input_artifacts` to the draft automatically
- use `attached_input_artifact_count` / `attached_input_artifacts` in the result to determine whether those uploads succeeded
- `resolved_attachment` is for inbound email-thread attachment resolution, not for new outbound file uploads

Reply-note:
- new outbound drafts support To / CC / BCC
- reply-to-thread supports explicit To / CC overrides, but not BCC
