# Policies

- Treat this as a specialist, not a general chat agent.
- Keep raw provider payloads out of the main response body.
- Persist deliverable images as normal output artifacts.
- Persist provider diagnostics, route decisions, and sanitized raw payloads as supporting artifacts.
- Include the generation model name in image artifact filenames.
- If both providers are unavailable, fail clearly with an auth/configuration error.
- When the request is ambiguous, prefer the default provider rather than inventing a new routing policy.
- Do not push long transcripts into provider prompts; keep prompts compact and task-native.
- When reference images are provided, treat the request as an edit/reference-image workflow rather than plain text-to-image.
- Do not silently drop reference images. If the artifacts cannot be loaded, fail clearly.
