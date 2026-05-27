# Gmail Agent Policies

- Never store Google refresh tokens. Credentials arrive only through `TaskEnvelope.input.auth`.
- Include `account_id`, `account_email`, and `account_label` in outputs.
- Use Gmail threads as the primary conversation unit.
- Keep full message bodies out of final output unless the user clearly asks to read the email.
- Treat attachments as refs first: surface filename/MIME/size/attachment_id metadata, and download a specific attachment only when a downstream task needs the file.
- Triage spam/noise with the LLM. Prefilters only skip learned repeated noise.
- Add senders/domains to the prefilter only when the user asks or when the LLM gives high confidence for recurring low-value noise.
- Do not send Gmail messages from autonomous reasoning. Create Gmail drafts and mark them as pending user approval; only `gmail.send_draft` may send, and only when Gateway calls it after an explicit user approval from the Gmail approval surface.
- Do not archive, delete, or bulk-label without an explicit approval path.
- If multiple Gmail accounts are connected and no account can be resolved, let the orchestrator ask the user which account to use.
