# Policies

## Error Handling

- Return `AgentError` with `retryable=True` for: `TIMEOUT`, `NETWORK_ERROR`, `RATE_LIMITED`
- Return `AgentError` with `retryable=False` for: `INVALID_INPUT`, `AUTH_ERROR`, `SCHEMA_VIOLATION`
- Always include `next_action`: `'retry'`, `'escalate'`, or `'skip'`

## Slide-Specific Rules

### Template Selection

Always prefer premium templates (professionally designed, with rich backgrounds and layouts):

| Template | Best for |
|----------|----------|
| **business-meeting** (default) | Meetings, proposals, reports, strategy, client work, project updates |
| **tech-trends** | Data analysis, product roadmaps, startup pitches, investor decks, engineering updates |
| **science-lesson** | Training, workshops, tutorials, onboarding, educational content |
| **tech-infographics** | Infographics, data viz, comparisons, process flows, feature overviews |

Legacy templates (corporate-dark, corporate-light, minimal, pitch-deck) have bare-bones design. Only use if the user explicitly names one or requests a dark theme.

### Deck Planning
- Always plan the full deck as JSON before building.
- Every slide MUST use template-guided `assignments` (not hardcoded positions) — the only exceptions are `code_chart`, `flow_diagram`, and `BLANK` layout.
- Choose layout names that EXACTLY match the template's available layouts.
- Assign content ONLY to placeholder indices that exist in the chosen layout.
- Keep bullets concise (5-8 words, 3-6 per slide).
- Include speaker_notes on every slide.
- Use charts for data comparisons, tables for structured data, images for concepts.
- For a one-slide intro or pitch cover, prefer a strong title-slide composition with one dominant headline, one crisp subtitle, and deliberately balanced whitespace.
- Avoid layouts that leave the deck looking top-left-heavy, placeholder-like, or visually accidental.

### Slide Count Guidance
- Quick overview / summary: 3-5 slides
- Standard presentation (5-10 min): 7-12 slides
- Detailed report / training: 15-25 slides
- Comprehensive workshop: 20-35 slides
- Single intro/cover slide: 1 slide
- Always respect explicit user requests for slide count.

### Image Delegation
- When a slide needs a custom image, set `source.kind: "generate"` with a descriptive prompt.
- When embedding a diagram, delegate to the diagram agent via `source.agent: "diagram"`.
- When using existing assets from a PDF input, set `source.kind: "from_asset"` with the asset_ref.
- If `_source_materials.visual_assets` already contain a suitable figure/page image, prefer `from_asset` over generating a new image.
- Only use `asset_ref` values that were actually surfaced in `_source_materials.visual_assets`.

### Document Bundles
- Treat parsed document bundles as the canonical source for uploaded PDFs and office files.
- Use local bundle summaries first: `_source_materials.documents[*].preview_excerpt` and `top_sections`.
- Do not ask the orchestrator to stream whole documents or full image sets into model context.
- If you need more exact document context, request a focused orchestrator-mediated docs lookup:
  - `docs.search_bundle` for "where is this discussed?"
  - `docs.read_bundle` for a bounded excerpt or section
  - `docs.fetch_asset` for exact asset metadata
  - `docs.reinspect_asset` for deeper understanding of a visual
- Keep docs requests compact and only reference bundle/doc/asset ids already surfaced from the source bundle.

### Validation
- Pre-build: Deterministic layout validation catches overlap, out-of-bounds, and density issues before rendering.
- Post-build: Render each slide to PNG via LibreOffice + pdftoppm, then vision-validate for quality.
- Strict pass/fail bar: blank slides, invisible text, major overlap, poor hierarchy, or unprofessional appearance = FAIL.
- If issues found and attempts < max: repair via LLM with specific fix feedback, rebuild, re-validate.
- If max attempts reached: accept with warning, include issues in output.

### Editing
- Parse existing deck structure before editing.
- Preserve content not mentioned in the edit request.
- When modifying slides, respect the template's placeholder system — use assignments, not hardcoded positions.
- Support operations: add_slide, remove_slide, move_slide, update_slide, update_text, replace_image, update_chart, update_table, restyle_deck.

### File Output
- Write PPTX to `runs/artifacts/<task_id>/slide_agent/presentation.pptx`.
- Optionally export PDF to same directory.
- Export slide preview PNGs to `runs/artifacts/<task_id>/slide_agent/previews/`.
