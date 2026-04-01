# Policies

## Error Handling

- Return `AgentError` with `retryable=True` for: `TIMEOUT`, `NETWORK_ERROR`, `RATE_LIMITED`
- Return `AgentError` with `retryable=False` for: `INVALID_INPUT`, `AUTH_ERROR`, `SCHEMA_VIOLATION`
- Always include `next_action`: `'retry'`, `'escalate'`, or `'skip'`

## Diagram-Specific Rules

### Renderer Selection
- Always use the internal LLM to select the best renderer based on content.
- If the user explicitly requests a renderer, honor it unless it's clearly wrong (e.g., D2 for a sequence diagram).
- Default to Mermaid for ambiguous requests — it covers the most diagram types.

### Definition Quality
- Generate complete, valid definitions. No placeholders like "// add more nodes here".
- Use descriptive node/element IDs, not generic ones.
- Keep diagrams focused — if the description is too broad, ask for clarification.
- Include a title/diagram header when appropriate.

### Rendering
- Always attempt to render via CLI before returning.
- If CLI fails, return the source code as an artifact with a clear note about the render failure.
- Clean up temp files after rendering.
- Support `svg` and `png` output formats; default to SVG.
- Surface concrete renderer/runtime causes when available (for example missing Chrome for Mermaid CLI), not just generic exit codes.

### Excalidraw Specifics
- Use consistent seed values based on element IDs for deterministic rendering.
- Place text labels inside shapes using `containerId`.
- Use `roughness: 1` for standard hand-drawn feel.
- Keep canvas coordinates within reasonable bounds (0-4000).

### Modification
- When modifying an existing diagram, preserve its structure as much as possible.
- Only change what the user explicitly requested.
- Maintain valid syntax — validate before returning.

## Artifact Output

- Write rendered files to `runs/artifacts/<task_id>/`.
- Include `ArtifactManifest` in `AgentResult` with:
  - `artifact_id`: generated UUID
  - `filename`: descriptive filename (e.g., `auth_flow_sequence.svg`)
  - `mime_type`: `image/svg+xml` or `image/png` or `application/json`
  - `size_bytes`: file size
  - `sha256`: content hash
- Also include the source definition as a separate artifact (e.g., `auth_flow_sequence.mmd`).
