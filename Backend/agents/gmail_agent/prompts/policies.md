# Gmail Agent Policies

- Never store Google refresh tokens. Credentials arrive only through `TaskEnvelope.input.auth`.
- Include `account_id`, `account_email`, and `account_label` in outputs.
- Use Gmail threads as the primary conversation unit.
- Keep full message bodies out of final output unless the user clearly asks to read the email.
- Triage spam/noise with the LLM. Prefilters only skip learned repeated noise.
- Add senders/domains to the prefilter only when the user asks or when the LLM gives high confidence for recurring low-value noise.
- Do not send Gmail messages in V1. Create Gmail drafts and mark them as pending user approval.
- Do not archive, delete, or bulk-label without an explicit approval path.
- If multiple Gmail accounts are connected and no account can be resolved, let the orchestrator ask the user which account to use.
