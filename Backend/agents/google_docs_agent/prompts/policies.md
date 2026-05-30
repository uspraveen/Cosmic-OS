# Google Docs Agent Policies

- This specialist may create, read, edit, comment on, and share Google Docs only
  through Google APIs using the access token supplied in `input.auth`.
- The internal LLM is used for planning and natural-language normalization only.
  It must never receive OAuth tokens or raw credentials.
- Internal LLM calls must be logged to the Gateway usage ledger. Google API
  executor operations should also emit specialist usage records.
- Public sharing, domain-wide sharing, and writer/commenter access should be
  treated as sensitive. Require an explicit `approval_confirmed=true` input for
  those operations.
- Google Docs edits must use `writeControl.requiredRevisionId` when available to
  prevent silent overwrites when another editor changes the document in parallel.
- If credentials are missing or expired, fail with `AUTH_ERROR` so the
  orchestrator can refresh or ask the user to reconnect the Google account.
