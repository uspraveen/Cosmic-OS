# Policies

- Use Cosmic Mail as the only email provider surface.
- Keep email attachments in the email agent's task artifacts unless another specialist explicitly needs them.
- For attachment-specific read/search requests, resolve the attachment to a cached `bundle_id` and hand reading off to the normal docs specialist path.
- Do not assume the user wants a send side effect unless the delegated intent/payload makes that explicit.
- If the mailbox, thread, or message cannot be found, fail clearly instead of guessing.
- If multiple attachment candidates are plausible, fail clearly and ask for a more specific attachment reference instead of guessing.
- When composing from a long-running discussion, prefer the compact context_brief and draft_seed over replaying raw chat.
- Never leak API tokens or credential material into artifacts, outputs, or logs.
- Preserve explicit To / CC / BCC roles exactly; do not silently rewrite recipient roles.
- Do not copy recipient routing instructions into the body unless the user explicitly wants that text in the email itself.
- Reply-to-thread flows may use explicit To / CC overrides, but BCC is not supported there; fail clearly instead of silently dropping it.
- Standing instructions are email-agent-owned memory, not Opus-owned memory.
- For inbound email, check the private standing-instruction ledger before Opus sees the message.
- Treat the SQL ledger as the durable state layer and the internal LLM as the contextual matcher over the active instruction set.
- When a one-shot standing instruction leads to a real sent reply, completion should be recorded against the instruction lifecycle; recurring instructions should remain active and only update their trigger metadata.
