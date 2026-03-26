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
- `email.reason`
- `email.manage_instruction`
- `email.recall_session`

## Attachment policy

Inbound email attachments stay inside the email agent artifact area by default. They are not automatically staged into Opus-visible `input_artifacts` on every inbound email turn.

## Orchestrator handoff

Opus should typically delegate with:

- `goal`
- optional `context_brief`
- optional `draft_seed`
- optional `thread_id`
- optional recipients / subject / send flag

Avoid passing long raw session transcripts.
Use `context_brief` and `draft_seed` instead when the user has been discussing something for a long time and then asks to send an email.
