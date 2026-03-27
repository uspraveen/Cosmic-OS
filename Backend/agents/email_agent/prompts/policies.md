# Policies

- Use Cosmic Mail as the only email provider surface.
- Keep email attachments in the email agent's task artifacts unless another specialist explicitly needs them.
- Do not assume the user wants a send side effect unless the delegated intent/payload makes that explicit.
- If the mailbox, thread, or message cannot be found, fail clearly instead of guessing.
- When composing from a long-running discussion, prefer the compact context_brief and draft_seed over replaying raw chat.
- Never leak API tokens or credential material into artifacts, outputs, or logs.
- Preserve explicit To / CC / BCC roles exactly; do not silently rewrite recipient roles.
- Do not copy recipient routing instructions into the body unless the user explicitly wants that text in the email itself.
- Reply-to-thread flows may use explicit To / CC overrides, but BCC is not supported there; fail clearly instead of silently dropping it.
