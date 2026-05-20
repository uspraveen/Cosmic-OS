# COSMIC Visual Response Enhancement Architecture

Last updated: 2026-04-27

This document replaces the original pre-implementation plan. The feature is now partially shipped and has gone through several reliability fixes. The current goal is no longer "design the feature from scratch"; it is to keep the implemented visual-response path understandable, testable, and production-safe.

## Current Status

Visual Response Enhancement is implemented as a desktop-gated, backend-controlled response mode.

Implemented:

- Gateway-backed VM-global preference for enabling/disabling visual enhancement.
- Desktop Settings `Preferences` page for the toggle.
- Gateway request-time preference resolution and metadata pinning.
- Orchestrator prompt gating through `visual_response_policy.md`.
- Runtime-only `[[visual_slot {...json...}]]` directives.
- Streaming `response.blocks.snapshot` events with monotonic snapshot sequencing.
- Inline `image_slot` and `chart_slot` placeholders.
- Runtime sidecar coordinator for images and charts.
- Artifact-backed inline visuals with `supporting_artifacts`.
- Gateway hydration of inline-only artifacts for signed preview URLs.
- Desktop rendering for inline image/chart slots and image artifacts.
- Deterministic local chart renderer.
- Direct image-search fallback, Firecrawl image scraping, and Fireworks/Kimi verification.
- Failure handling for stale, failed, late, or timed-out slots.

Still intentionally constrained:

- The rich inline visual path is desktop-only for now.
- Charts are limited to one chart slot per turn by default.
- Image search quality is heuristic and will continue to need tuning.
- Generated images are not automatically used for visual enhancement unless a separate image-generation path is explicitly wired in.

## Product Behavior

When Visual Response Enhancement is enabled:

- COSMIC may proactively add one useful inline visual when it materially improves the response.
- If the user explicitly asks for images, COSMIC may emit up to five inline image slots.
- Charts are preferred for quantitative comparisons.
- Images are preferred for concrete people, products, places, events, UI screenshots, anime/game references, and other visual subjects.
- The answer must remain complete even if visuals fail.
- The model must not promise that a visual will appear.

When disabled:

- The normal text/artifact response path should remain unchanged.

## Backend Preference Store

The visual preference is stored by the Gateway, not by desktop-local UI settings.

Primary implementation files:

- `Backend/gateway/preferences/store.py`
- `Backend/gateway/preferences/routes.py`
- `Backend/gateway/config.py`
- `Backend/gateway/runtime.py`
- `src/GatewayPreferencesSettings.tsx`
- `src/Settings.tsx`
- `electron/main.ts`
- `electron/preload.ts`

Database:

- Default path comes from Gateway config as `preferences_db_path`.
- On the VM this is a Gateway SQLite DB, commonly `Backend/gateway/preferences.db` unless configured otherwise.
- The store initializes with WAL mode.

Table:

```sql
CREATE TABLE IF NOT EXISTS app_preferences (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_source TEXT,
    updated_device_id TEXT
);
```

Stored key:

```text
visual_response_enhancement
```

Stored value:

```json
{"enabled": true}
```

Default:

- `enabled: true`
- `revision: 1`
- `updated_source: "system_default"`

Desktop API:

- `GET /desktop/preferences`
- `PATCH /desktop/preferences`

Current response shape:

```json
{
  "visual_response_enhancement": {
    "enabled": true,
    "revision": 1,
    "updated_at": "2026-04-27T00:00:00Z",
    "updated_source": "system_default",
    "updated_device_id": null
  }
}
```

Update payload:

```json
{
  "visual_response_enhancement_enabled": true
}
```

Runtime behavior:

- Gateway reads the preference once per accepted user turn.
- The resolved boolean is pinned into request state.
- The resolved boolean is stored in user/assistant message metadata.
- The resolved boolean is passed into orchestrator task input.
- Toggle changes affect future turns only, never an in-flight response.
- If preference DB reads fail, Gateway falls back safely to enabled rather than crashing the request.

Realtime event:

```json
{
  "type": "preferences.updated",
  "preferences": {
    "visual_response_enhancement": {
      "enabled": true,
      "revision": 2
    }
  }
}
```

## Gateway Session DB And Response Persistence

The visual path also uses the normal Gateway session DB.

Relevant persisted metadata lives in the `messages.metadata_json` field in the Gateway session store. In local/VM debugging this is typically inspected through `Backend/gateway/sessions.db`.

Important metadata fields:

- `visual_response_enhancement_enabled`
- `gateway_preferences`
- `response_blocks`
- `produced_artifacts`
- `supporting_artifacts`

`response_blocks` is the canonical final rendering model for visual responses. It can contain:

- `markdown`
- `code`
- `image_slot`
- `chart_slot`
- `image_artifact`
- `file_artifact`

`produced_artifacts` are user-deliverable files and appear in `Produced Files`.

`supporting_artifacts` are inline-only backing assets. They are persisted for replay/hydration but should not appear in `Produced Files`.

Gateway responsibilities:

- Accept `response.blocks.snapshot` events from orchestrator.
- Track latest snapshot per request.
- Hydrate preview URLs for blocks backed by `produced_artifacts`.
- Hydrate preview URLs for blocks backed by `supporting_artifacts`.
- Persist final `response_blocks` and `supporting_artifacts` on response completion.
- Preserve visual blocks across history replay and resume.
- Avoid writing partial snapshot events as completed assistant messages.

Recent bug fixed:

- Some completed responses persisted a failed automatic `image_slot` before successful image artifacts. This caused old placeholders to spin forever. The coordinator now removes stale failed automatic image slots when real image artifacts exist, and the desktop renders failed slots as terminal rather than loading forever.

## Prompt Gating

The visual prompt is gated in the real orchestrator prompt builder, not in a legacy static prompt.

Primary files:

- `Backend/orchestrator/prompts/__init__.py`
- `Backend/orchestrator/prompts/visual_response_policy.md`
- `Backend/orchestrator/runtime.py`
- `Backend/tests/test_orchestrator_runtime.py`

Prompt builder:

```python
build_agentic_system_prompt(
    ...,
    visual_response_enhancement_enabled=True,
    visual_supported_slot_kinds=["image", "chart"],
)
```

Behavior:

- If `visual_response_enhancement_enabled` is false, no visual-response policy is injected.
- If true, `visual_response_policy.md` is injected.
- The prompt also appends the supported runtime slot kinds for that turn.

The visual prompt tells Opus:

- Visual enhancement is enabled for this turn.
- Visuals are optional and must materially improve the answer.
- Do not mention the preference to the user.
- Never promise that a visual will appear.
- Do not wait for visuals before continuing the answer.
- Use runtime-only `[[visual_slot {...json...}]]` directives.
- Prefer charts for quantitative comparisons.
- Prefer images for concrete visual references.
- Emit at most five inline images when explicitly requested; otherwise prefer one strong visual or two to three distinct visuals.
- Keep the directive valid JSON and place it on its own line.

Directive examples:

```text
[[visual_slot {"id":"img_1","kind":"image","query":"OpenAI operator interface screenshot","caption":"Operator interface from a trusted source page"}]]
```

```text
[[visual_slot {"id":"chart_1","kind":"chart","chart_type":"bar","title":"Quarterly revenue","x_label":"Quarter","y_label":"Revenue","series":[{"label":"Revenue","points":[{"x":"Q1","y":12.4},{"x":"Q2","y":14.1}]}]}]]
```

The directive is stripped by the runtime and never shown to the user.

## Streaming Contract

Visual mode adds a block-snapshot event:

```json
{
  "type": "response.blocks.snapshot",
  "snapshot_seq": 3,
  "request_id": "req_123",
  "session_id": "sess_20260427",
  "blocks": [
    {"id": "markdown_1", "type": "markdown", "text": "Intro text."},
    {
      "id": "img_1",
      "type": "image_slot",
      "status": "pending",
      "slot_kind": "image",
      "loading_label": "Finding a relevant image",
      "timeout_ms": 30000
    }
  ]
}
```

Rules:

- `snapshot_seq` is monotonic per response.
- Desktop keeps the highest snapshot sequence for a request.
- Older or duplicate snapshots are ignored.
- Late snapshots after a completed response are allowed to update the cached completed message if they belong to the same request.
- On reconnect, Gateway can replay the latest known block snapshot and final canonical blocks.

Recent bug fixed:

- Late visual snapshots were dropped if they arrived after the assistant message was marked complete. Desktop now preserves completed state while still accepting newer block snapshots for the same request.

## Orchestrator Visual Coordinator

Primary files:

- `Backend/orchestrator/visual_enrichment/coordinator.py`
- `Backend/orchestrator/visual_enrichment/charting.py`
- `Backend/orchestrator/visual_enrichment/clients.py`
- `Backend/orchestrator/config.py`
- `Backend/tests/test_visual_enrichment.py`

The `VisualEnrichmentCoordinator` owns the visual sidecar lifecycle for a single request.

Responsibilities:

- Parse streamed text and strip visual directives.
- Convert visual directives to `image_slot` / `chart_slot` blocks.
- Emit block snapshots when slots are added, replaced, failed, or cleaned up.
- Start image/chart sidecars without blocking the main answer lane.
- Collect source hints from answer-lane research.
- Write inline visual artifacts into COSMIC artifact storage.
- Return final `response_blocks` and `supporting_artifacts`.
- Cancel unfinished sidecars after finalization budget expires.
- Prevent stale sidecars from mutating newer responses.

Current supported slot kinds:

- `image`
- `chart`

## Image Enrichment Path

Current image path:

1. Opus emits an image slot directive.
2. Runtime inserts an `image_slot`.
3. Coordinator starts an image sidecar.
4. Sidecar gathers candidates from trusted source pages and/or direct image search.
5. Candidates are filtered and scored.
6. Top candidates may be checked by Fireworks/Kimi verifier.
7. The best candidate is downloaded.
8. The bytes are stored as a COSMIC artifact.
9. The slot is replaced by an `image_artifact` block.

Inputs:

- user query
- slot query
- caption
- context excerpt
- optional source URLs
- source hints discovered during the response

Retrieval sources:

- Firecrawl image scrape for trusted source pages.
- Direct image search fallback using Bing Images HTML result parsing.
- Existing source titles can seed better direct-image queries for vague follow-up requests.

Current robustness behavior:

- Direct image search now starts even if no trusted source URLs are available.
- Explicit image requests run trusted-source scraping and direct image search in parallel.
- Multiple direct image-search query variants run concurrently.
- Firecrawl scraping across source pages runs concurrently.
- Download failures retry the next candidate.
- Tiny/low-information images are skipped.
- SVG/UI/decorative/logo/banner/cross-promo candidates are filtered earlier.
- `mha` expands to `My Hero Academia`; `ofa` expands to `One For All`.
- Explicit image requests use a relaxed fallback floor instead of failing on overly strict confidence gates.
- Kimi rejection no longer permanently blocks an explicit image request fallback.
- Failed slots are terminal in the UI.

Current defaults:

```text
VISUAL_ENHANCEMENT_MAX_VISUALS_PER_TURN=5
VISUAL_ENHANCEMENT_MAX_IMAGE_SLOTS_PER_TURN=5
VISUAL_ENHANCEMENT_IMAGE_SLOT_TIMEOUT_MS=6000
VISUAL_ENHANCEMENT_IMAGE_SOURCE_PAGE_LIMIT=3
VISUAL_ENHANCEMENT_IMAGE_CANDIDATE_LIMIT=24
VISUAL_ENHANCEMENT_IMAGE_MAX_BYTES=8388608
VISUAL_ENHANCEMENT_IMAGE_VERIFY_TOP_K=3
VISUAL_ENHANCEMENT_IMAGE_MIN_CONFIDENCE=0.58
VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED=true
VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL=https://www.bing.com/images/search
VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC=5
VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT=12
VISUAL_ENHANCEMENT_DOWNLOAD_TIMEOUT_SEC=6
```

Important distinction:

- Explicit user image requests get the full image timeout budget.
- Automatic/proactive image suggestions are capped shorter by the runtime so normal answers do not hang on optional visuals.

## Chart Enrichment Path

Charts are generated locally and deterministically.

Primary file:

- `Backend/orchestrator/visual_enrichment/charting.py`

Current chart behavior:

- Opus emits a chart slot with structured data.
- Coordinator renders the chart locally to PNG.
- The chart PNG is stored as a supporting artifact.
- The slot is replaced by an `image_artifact` block with `kind: "chart"`.

Current defaults:

```text
VISUAL_ENHANCEMENT_MAX_CHART_SLOTS_PER_TURN=1
VISUAL_ENHANCEMENT_CHART_SLOT_TIMEOUT_MS=4000
VISUAL_ENHANCEMENT_CHART_MAX_POINTS=200
VISUAL_ENHANCEMENT_CHART_MAX_BYTES=4194304
```

Recent chart design changes:

- The initial chart card renderer became too nested and low-contrast.
- The chart renderer was simplified toward a black/glassy COSMIC-aligned style.
- Current focus is readability over decorative containers.

## Visual Environment And Bootstrap

Primary files:

- `Backend/visual_enhancement.env.example`
- `Backend/visual_enhancement.env`
- `Backend/bootstrap.py`
- `Backend/systemd/cosmic-orchestrator.service.example`
- `Backend/orchestrator/config.py`

Runtime config is read through `OrchestratorConfig.from_env()`.

Dedicated env keys:

- `VISUAL_ENHANCEMENT_ENABLED`
- `VISUAL_ENHANCEMENT_MAX_VISUALS_PER_TURN`
- `VISUAL_ENHANCEMENT_MAX_IMAGE_SLOTS_PER_TURN`
- `VISUAL_ENHANCEMENT_MAX_CHART_SLOTS_PER_TURN`
- `VISUAL_ENHANCEMENT_MAX_CONCURRENT_SIDECARS`
- `VISUAL_ENHANCEMENT_IMAGE_SLOT_TIMEOUT_MS`
- `VISUAL_ENHANCEMENT_CHART_SLOT_TIMEOUT_MS`
- `VISUAL_ENHANCEMENT_FINALIZATION_GRACE_MS`
- `VISUAL_ENHANCEMENT_IMAGE_SOURCE_PAGE_LIMIT`
- `VISUAL_ENHANCEMENT_IMAGE_CANDIDATE_LIMIT`
- `VISUAL_ENHANCEMENT_IMAGE_VERIFY_TOP_K`
- `VISUAL_ENHANCEMENT_IMAGE_MIN_CONFIDENCE`
- `VISUAL_ENHANCEMENT_IMAGE_SEARCH_*`
- `VISUAL_ENHANCEMENT_FIREWORKS_*`
- `VISUAL_ENHANCEMENT_FIRECRAWL_*`

API-key fallback behavior:

- `VISUAL_ENHANCEMENT_FIREWORKS_API_KEY`
- falls back to `MODEL_API_KEY`, `FIREWORKS_API_KEY`, then `OPENAI_COMPAT_API_KEY`

- `VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY`
- falls back to `FIRECRAWL_API_KEY`

Known config hygiene note:

- `visual_enhancement.env.example` and `OrchestratorConfig` now reflect the latest image robustness defaults.
- Keep `bootstrap.py` defaults aligned whenever these values change, because bootstrap-generated VM env files can otherwise regress to older limits.

## Desktop Rendering

Primary files:

- `src/App.tsx`
- `src/spotlight.css`
- `electron/gatewayConnectionManager.ts`

Renderer behavior:

- Normalizes `response_blocks`.
- Renders pending `image_slot` and `chart_slot` as subtle inline placeholders.
- Replaces slots in place when an `image_artifact` arrives with the same stable id.
- Renders failed slots as terminal unavailable cards, not infinite loaders.
- Renders `image_artifact` blocks with signed preview URLs, caption, badge, source chip, and provenance.
- Keeps inline visuals separate from `Produced Files`.

Recent bug fixed:

- Hidden/closed response screens could lose streamed visual blocks when reopened. The snapshot cache now handles late updates more safely.

## Failure Model

Visual failures must not fail the answer.

Expected behavior:

- The text response completes normally.
- Failed visual slots are terminal.
- Automatic failed slots are removed if successful artifacts exist.
- Explicit failed slots may remain as a small unavailable card so the user can tell the requested visual failed.
- No raw directive syntax should leak into the response.
- No third-party image URLs are hotlinked directly in the UI.
- Downloaded visuals are stored as COSMIC artifacts and served through signed preview URLs.

Recent fixes:

- Stale failed automatic image slots are removed from final response blocks when image artifacts exist.
- Failed slots no longer show infinite loaders.
- Completed responses can still receive valid late snapshot updates for the same request.
- Direct image-search-only slots now actually start without waiting for source URLs.

## Tests

Important test files:

- `Backend/tests/test_gateway_preferences.py`
- `Backend/tests/test_gateway_desktop_ws.py`
- `Backend/tests/test_orchestrator_runtime.py`
- `Backend/tests/test_visual_enrichment.py`

Current visual tests cover:

- preference store defaults and updates
- preference metadata pinning into request/task state
- prompt asset gating and prompt asset hashes
- snapshot hydration with supporting artifacts
- chart slot generation
- image slot generation
- explicit image relaxed fallback
- strict behavior for non-explicit image requests
- direct image search fallback
- query enrichment from source titles
- MHA/OFA alias expansion
- candidate retry after download failure
- tiny image skip and retry
- SVG/UI/decorative filtering
- finalization timeout budget
- stale slot cleanup

Recent focused validation:

```text
python -m pytest -q tests/test_visual_enrichment.py
```

Expected current result:

```text
20 passed
```

## Known Risks And Follow-Ups

1. Direct image search is still HTML-scrape based. It works without an API key, but it can break if Bing markup changes.
2. A formal image-search API would be more stable than HTML parsing.
3. Confidence scoring is still heuristic. Recent fixes made explicit requests less brittle, but candidate quality needs continuous tuning.
4. Anime/game images are especially sensitive to query wording, source quality, and cross-promo filtering.
5. Chart styling is improved but should keep moving toward a clearer Apple/Perplexity-like visual language.
6. Bootstrap defaults must stay synchronized with `OrchestratorConfig` and `visual_enhancement.env.example`.
7. The aggregate `visual_max_visuals_per_turn` is a policy/config knob; actual enforcement mainly happens through per-kind slot caps.
8. Supporting artifact retention/cleanup policy still needs explicit long-term rules.

## Operational Debugging Notes

When a visual fails, inspect in this order:

1. Gateway session DB message metadata:
   - `messages.metadata_json`
   - `response_blocks`
   - `supporting_artifacts`
   - `produced_artifacts`

2. Orchestrator logs:
   - `visual_enrichment.image_search_queries`
   - `visual_enrichment.image_search_candidates`
   - `visual_enrichment.trusted_image_candidates`
   - `visual_enrichment.image_verify_failed`
   - `visual_enrichment.image_candidate_failed`
   - `visual_enrichment.image_failed`
   - `visual_enrichment.finalization_timeout`

3. Gateway logs:
   - `response.blocks.snapshot` forwarding
   - preview URL hydration
   - signed artifact GETs

4. Desktop:
   - latest block snapshot for the request
   - whether a slot is `pending`, `failed`, or replaced by `image_artifact`
   - whether preview URLs are hydrated

Common interpretation:

- `image_slot` pending forever means frontend/gateway snapshot handling is wrong.
- `image_slot` failed with no image artifacts means retrieval/scoring/download failed.
- `image_artifact` exists without preview URL means gateway hydration failed.
- `image_artifact` exists and preview GET returns 200 but UI does not render means desktop rendering/state is wrong.

## Final Current Architecture

The shipped architecture is:

1. Desktop user toggles a VM-global Gateway preference.
2. Gateway resolves that preference once per request and pins it into metadata/task input.
3. Orchestrator prompt builder injects `visual_response_policy.md` only when enabled.
4. Opus emits runtime-only visual slot directives.
5. Orchestrator runtime strips directives and creates visual slot blocks.
6. `VisualEnrichmentCoordinator` runs image/chart sidecars in the background.
7. Gateway forwards `response.blocks.snapshot` events.
8. Desktop renders slots and replaces them in place.
9. Final response persists canonical `response_blocks`.
10. Inline-only visuals are stored in `supporting_artifacts`, hydrated on replay, and kept out of `Produced Files`.
