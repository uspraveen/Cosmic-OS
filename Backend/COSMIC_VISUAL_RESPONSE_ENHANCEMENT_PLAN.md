# COSMIC Visual Response Enhancement Plan

## Problem Statement

COSMIC currently produces strong text responses, can render inline images when `response_blocks` already contain `image_artifact` blocks, and can surface generated or discovered files as produced artifacts. What it cannot do yet is deliver a production-grade "visually enhanced response" experience where:

1. the user can opt in from Settings
2. the main answer starts with no extra first-token delay
3. image search, image selection, and chart rendering happen in parallel with answer generation
4. inline visuals appear at the exact intended positions during streaming
5. visuals are artifact-backed and replayable across history/resume
6. inline visuals do not pollute the normal `Produced Files` surface

The current system is good at final text delivery and final artifact surfacing. It is not yet designed for live, slot-based, inline visual enrichment.

This document defines the current state, the production constraints, and the final architecture required to ship visually enhanced responses safely.

## Why This Matters

The target experience is not "sometimes show a file at the bottom." The target experience is:

- answer first
- visuals only when they materially help
- visuals inserted at the right explanatory point
- no messy jumps, no broken streaming, no extra latency on ordinary turns

Examples:

- A researched answer can show one relevant inline image from a trusted source page.
- A market summary can include a small inline chart generated from structured numbers.
- A product explanation can place the image between the introduction and the detailed explanation instead of dumping it at the end.

This has to work in production without destabilizing the current text-first chat path.

## Goals

1. Add a user preference that enables or disables visual response enhancement.
2. Preserve the current behavior exactly when the preference is off.
3. When the preference is on, allow inline images and inline charts to appear at intended positions during the response.
4. Keep first-token latency effectively unchanged.
5. Reuse existing research work instead of running a separate, duplicated research stack.
6. Keep final message persistence, history hydration, resume, and downloads deterministic.
7. Ensure inline visuals are sourced, attributable, and safe to replay later.

## Non-Goals

1. This plan does not aim to make every answer visual.
2. This plan does not require full visual validation of every candidate image.
3. This plan does not require exposing every inline visual as a downloadable produced file.
4. This plan does not require replacing the current orchestrator, gateway, or renderer architecture.
5. This plan does not require a generic post-complete mutation model for all message types.

## Current System

## Desktop Renderer

The desktop UI already supports structured assistant rendering:

- markdown blocks
- code blocks
- image artifact blocks
- file artifact blocks

This is implemented in `src/App.tsx`.

Important current behavior:

1. `response.chunk` appends only plain text into `message.content`.
2. `response.complete` is where `produced_artifacts` and `response_blocks` are normalized and attached to the message.
3. once `msg.responseBlocks` exists, the renderer prefers block mode over plain markdown content

That means the current renderer can place images anywhere in the block order, but only after the response is complete in the normal streaming path.

## Gateway Persistence And Hydration

The gateway already does several useful things:

- stores `produced_artifacts` in session metadata
- stores `response_blocks` in session metadata
- rebuilds stable response blocks from `content + produced_artifacts`
- hydrates image artifacts with signed `preview_url` values for the client

This is implemented in `Backend/gateway/runtime.py`.

Important current behavior:

1. final response blocks are built at `response.complete`
2. hydration of image blocks currently depends on looking them up against `produced_artifacts`
3. history replay and resume are aligned around final persisted message metadata

This is good for final artifact-backed responses, but insufficient for inline visual artifacts that must be replayable without appearing in `Produced Files`.

## Orchestrator Runtime

The orchestrator already supports:

- normal text streaming from Opus
- native Anthropic server-side tools such as `web_search`, `web_fetch`, and `code_execution`
- specialist delegation through local tool handlers
- collection of deliverable artifacts from specialist outputs
- promotion of code-execution files into produced artifacts

This is implemented in `Backend/orchestrator/runtime.py` and `Backend/orchestrator/tools`.

Important current behavior:

1. a normal model `tool_use` pauses the answer loop until the tool result returns
2. local read-only tools can run concurrently via `asyncio.gather(...)`
3. Anthropic server-side tools can appear together in a turn, but that concurrency is provider-controlled, not runtime-controlled
4. specialist artifacts are only surfaced into the current response when they are treated as deliverable artifacts

This means a normal "image_enrichment" tool call would block Opus, which is the opposite of what we want.

## Existing Media And Specialist Capabilities

The system already has important building blocks:

- native Opus web search and web fetch
- Firecrawl specialist for scrape and extract
- X/Twitter search specialist
- image generator specialist
- tabular specialist
- provider-side code execution support

Useful current capabilities:

- Firecrawl scrape supports `formats: ["images"]`
- the image generator agent can persist generated image artifacts
- code execution outputs can become artifacts
- the renderer already knows how to display inline image artifact blocks

Important current gap:

Firecrawl currently gives us image metadata and stores `images.json`, but there is no end-to-end production path that:

1. finds the best candidate image
2. ingests the winning image bytes into COSMIC artifact storage
3. treats that image as inline-only support media
4. places it into the streaming response at the right slot

## Existing Parallelism

There is already some concurrency, but not enough for this feature.

### What already runs in parallel

- local tool batches when every tool is marked `read_only=True`
- some provider-side server tools may occur together in a single Anthropic turn

### What does not yet give us reliable visual enrichment parallelism

- a normal model tool call for image enrichment would block the main answer
- Firecrawl and X specialist wrappers are not currently part of the orchestrator's parallel-safe local tool set
- tabular reasoning is not currently a fast sidecar path for chart production
- there is no separate visual sidecar coordinator

## Current Blockers

The key blockers are architectural, not cosmetic.

### 1. Final-only block application

Today, inline images arrive in the normal desktop flow only on `response.complete`.

### 2. Content vs block mode split

The renderer switches from plain `content` to `responseBlocks` once blocks exist. A naive partial-block implementation can hide in-progress text.

### 3. No placeholder slot protocol

There is currently no first-class notion of:

- pending image slot
- pending chart slot
- replace-this-slot-when-ready

### 4. No non-blocking visual enrichment lane

The orchestrator has no runtime-managed background visual worker tied to the active response.

### 5. No inline-only artifact persistence model

The current system has:

- `produced_artifacts` for deliverable files

It does not yet have:

- `supporting_artifacts` or equivalent for artifact-backed inline visuals that should not show up in `Produced Files`

## Final Plan

## Core Design Principles

1. Text remains the primary response lane.
2. Visuals are optional enrichments, not required for every answer.
3. Visual mode must not increase first-token latency.
4. Visual placement must be explicit and stable.
5. Inline visuals must still be artifact-backed internally.
6. The final persisted message must remain canonical and replayable.
7. Visual mode must never degrade the current non-visual production path.

## Product Behavior

The product should expose a dedicated `Preferences` page in the desktop Settings panel with:

- `Visual Response Enhancement: On`
- `Visual Response Enhancement: Off`

Default state:

- `On`

Behavior:

- When `Off`, the current response path remains exactly as it is today.
- When `On`, the request enters visual mode and uses the new block-streaming and sidecar architecture.

This is not two separate COSMIC products. It is one product with two rendering/orchestration modes.

## Preference Gating

The new preference should not live only in the existing desktop-local settings database.

Reason:

- current desktop settings in `resources/user_data.db` are device-local UI/app settings
- this new preference is intended to be VM-global behavior for the user's backend
- the backend prompt and visual coordinator are backend-owned concerns

So the final design should use a new Gateway-backed preference store.

## Gateway-Backed Preference Store

Create a new dedicated Gateway SQLite database:

- `Backend/gateway/preferences.db`

Recommended table:

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

Rules:

1. do not add `user_id` for this store in the initial implementation
2. this COSMIC deployment is single-user-per-VM, so the VM boundary already scopes the preference
3. the Gateway is the source of truth for VM-global product preferences
4. the desktop reads and writes this preference through Gateway APIs
5. the desktop-local settings DB remains the source of truth only for device/UI-specific behavior

Preferred stored key:

- `key = "visual_response_enhancement"`
- `value_json = {"enabled": true}`

The table can be generic, but the API exposed to the desktop should remain typed and explicit. The desktop should not send arbitrary free-form setting blobs into the Gateway.

## Gateway Preference API

Add desktop/local-token Gateway endpoints:

- `GET /desktop/preferences`
- `PATCH /desktop/preferences`

The response should be a canonical snapshot, for example:

```json
{
  "preferences": {
    "visual_response_enhancement": {
      "enabled": true
    }
  },
  "revision": 7,
  "updated_at": "2026-04-23T16:20:00Z"
}
```

Behavior:

1. `GET /desktop/preferences` returns the current canonical preference snapshot
2. `PATCH /desktop/preferences` updates the stored preference, increments `revision`, and returns the new canonical snapshot
3. when the preference changes, the Gateway broadcasts a realtime event such as `preferences.updated` to connected desktop clients
4. the desktop treats Gateway responses and `preferences.updated` events as authoritative

## Preference Resolution Rules

The Gateway should resolve the effective visual mode once per accepted user request.

Required behavior:

1. when a new user message is accepted, the Gateway reads the latest stored `visual_response_enhancement` preference
2. it computes an effective boolean such as `visual_response_enhancement_enabled`
3. it stores that effective value in the request record
4. it persists that same effective value in final request/message metadata for replay, resume, debugging, and auditability
5. a toggle change only affects the next request; it must never mutate an in-flight response

This means the backend still works with a resolved boolean, but that boolean is resolved by the Gateway from its own preference store rather than guessed from local desktop state.

## Desktop Settings Integration

The desktop should expose this preference in a new top-level `Preferences` page, not under `UI Settings`.

Reason:

- `UI Settings` currently contains device-local presentation controls such as search position, stayback time, and island opacity
- `Visual Response Enhancement` is a backend/VM-global product preference, not a local rendering-only tweak

Desktop requirements:

1. add a new top-level Settings page named `Preferences`
2. add a `Visual Response Enhancement` card with default state `On`
3. fetch the preference snapshot from the Gateway when the app connects, resumes, or opens the settings page
4. save changes through new Gateway IPC methods, not through `window.cosmic.saveSetting(...)`
5. subscribe to `preferences.updated` so multiple open desktop surfaces stay in sync
6. if the Gateway is unavailable, show the preference as unavailable rather than silently falling back to a stale local value

Recommended Electron bridge additions:

- `getGatewayPreferences`
- `saveGatewayPreferences`

These should be implemented in Electron main/preload using the already stored Gateway base URL and local API token.

## Mode Semantics

### Visual Mode Off

Keep all current behavior:

- normal `response.chunk`
- normal `response.complete`
- final-only `response_blocks`
- normal `Produced Files`

### Visual Mode On

Use a different streaming contract:

- stream ordered response blocks from the start
- allow pending visual slots
- allow runtime replacement of a pending slot with a real inline visual
- persist the final canonical block list at completion

Initial scope rule:

- the Gateway-backed visual preference should enable the Opus/orchestrator visual path first
- direct non-Opus routes may ignore this preference until they gain visual support

## Recommended Architecture

The full production architecture should have four cooperating parts:

1. the main Opus answer lane
2. a runtime-managed visual enrichment coordinator
3. one or more background visual workers
4. a gateway/renderer block assembler

## Why This Must Not Be A Normal Tool Call

The visual enrichment request must not be implemented as a normal model `tool_use` call.

Reason:

- a normal tool call pauses the model loop until the tool result returns
- we want Opus to keep writing while the visual search/generation happens in parallel

So the correct model is:

- Opus or the runtime emits a non-blocking visual directive
- the orchestrator runtime launches the visual sidecar in the background
- the main answer stream continues

## Streaming Response Model

Visual mode must switch from plain text streaming to block-first streaming.

Recommended external event:

```json
{
  "type": "response.blocks.snapshot",
  "snapshot_seq": 1,
  "request_id": "req_123",
  "session_id": "sess_123",
  "blocks": [
    { "id": "md_1", "type": "markdown", "text": "Intro paragraph." },
    {
      "id": "img_1",
      "type": "image_slot",
      "status": "pending",
      "slot_kind": "image",
      "loading_label": "Finding a relevant image",
      "timeout_ms": 3500
    },
    { "id": "md_2", "type": "markdown", "text": "Follow-up explanation." }
  ]
}
```

Later, when the visual worker finishes:

```json
{
  "type": "response.blocks.snapshot",
  "snapshot_seq": 4,
  "request_id": "req_123",
  "session_id": "sess_123",
  "blocks": [
    { "id": "md_1", "type": "markdown", "text": "Intro paragraph." },
    {
      "id": "img_1",
      "type": "image_artifact",
      "artifact_id": "art_visual_1",
      "filename": "openai-operator-ui.png",
      "preview_url": "signed-preview-url",
      "kind": "reference_image",
      "caption": "Operator interface from the source article",
      "provenance": {
        "source_url": "https://example.com/operator-article",
        "source_title": "Operator launch article",
        "source_domain": "example.com",
        "source_image_url": "https://example.com/images/operator-ui.png",
        "attribution_label": "Image from Operator launch article",
        "selection_reason": "Best match from a cited source page",
        "confidence": 0.93
      }
    },
    { "id": "md_2", "type": "markdown", "text": "Follow-up explanation." }
  ]
}
```

For production simplicity, snapshots are preferred over ad hoc patch operations in the first full implementation.

### Snapshot Ordering And Delivery Rules

`response.blocks.snapshot` must have an explicit monotonic `snapshot_seq`.

Rules:

1. `snapshot_seq` starts at `1` for each response and increments by exactly `1` for every emitted snapshot.
2. The gateway and renderer treat the highest seen `snapshot_seq` for a given `request_id` as authoritative.
3. Duplicate deliveries with an already-applied `snapshot_seq` are ignored.
4. Out-of-order deliveries with an older `snapshot_seq` are ignored.
5. On reconnect or resume, the gateway should resend the latest full snapshot and its `snapshot_seq` before any newer updates.

This is required for:

- reconnect safety
- duplicate-delivery safety
- deterministic block replacement
- clean multi-surface replay semantics

### Block Schema Requirements

The block schema needs to carry provenance directly, not only through artifact metadata.

Required block-level fields for inline visuals:

- stable `id`
- `type`
- `kind`
- `caption`
- `provenance.source_url`
- `provenance.source_title`
- `provenance.source_domain`
- `provenance.attribution_label`

Optional but strongly recommended:

- `provenance.source_image_url`
- `provenance.selection_reason`
- `provenance.confidence`
- `provenance.alt_text`

This ensures replayed inline visuals remain explainable even if artifact metadata is unavailable, trimmed, or inspected separately from the message blocks.

## Placeholder Slot Model

The pending visual must be a real block, not an invisible gap.

Recommended slot types:

- `image_slot`
- `chart_slot`

Required slot properties:

- stable `id`
- `status`
- `slot_kind`
- `timeout_ms`
- optional short loading label
- optional fallback behavior

Renderer behavior:

1. show a subtle skeleton/loading card while pending
2. replace in place when the real block arrives with the same `id`
3. collapse the slot away if the visual fails or misses the finalization budget

## How Placement Should Work

Opus must be able to indicate where the visual belongs.

The recommended approach is:

1. in visual mode, Opus emits lightweight runtime-only slot directives in the streamed response
2. the runtime strips those directives from user-visible text
3. the runtime converts them into pending slot blocks

Example directive syntax:

```text
[[visual_slot:id=img_1;kind=image;query=OpenAI operator interface;placement=after_intro]]
```

This directive is not shown to the user. It is intercepted by the runtime and turned into a pending block.

The same directive can also be used to start or update the sidecar request.

## Visual Enrichment Coordinator

Add a runtime-owned component, for example:

`VisualEnrichmentCoordinator`

Responsibilities:

1. start only when visual mode is enabled
2. receive slot requests from the main answer stream
3. launch background image/chart workers
4. receive source hints from the answer lane
5. emit block snapshots back into the orchestrator stream
6. enforce per-turn latency budgets
7. finalize or drop pending slots before `response.complete`

This coordinator belongs in the orchestrator runtime, not in the renderer.

## Coordinator Lifecycle And Reconnect Semantics

The coordinator needs explicit lifecycle rules. A vague "background worker" model is not enough for production.

Identity and ownership:

- every coordinator instance is keyed to the active `request_id`
- every sidecar job is keyed to a stable `slot_id`
- any update without a matching live `request_id` is discarded immediately

Cancellation semantics:

1. if the user stops generation, cancel all active sidecars for that `request_id`
2. if the user retries/regenerates, cancel the old coordinator before starting the new one
3. if the response completes, cancel any unfinished sidecars after the grace window expires
4. if a slot is explicitly withdrawn by the answer lane, cancel only that slot's sidecar

Reconnect semantics:

1. the gateway persists the latest accepted `snapshot_seq` and block snapshot for the active response
2. on UI reconnect, the gateway replays the latest snapshot before forwarding live updates
3. if the response is still active, the coordinator may continue existing sidecars rather than restarting them
4. if the response has already finalized, reconnect only replays the final canonical blocks and artifact metadata

Stale update protection:

- sidecars must include `request_id`, `slot_id`, and the replacement target block id on every update
- the coordinator must reject late results from canceled or superseded requests
- a newer retry must never let an older sidecar mutate the visible message

## Image Enrichment Flow

The image lane should be source-first and budgeted.

### Inputs

The image worker should accept:

- user query
- slot id
- search prompt
- optional source URLs already trusted by the answer lane
- optional route context such as article/blog/product/news

### Search Strategy

Preferred order:

1. reuse source URLs already discovered by the answer lane
2. scrape those pages for images with Firecrawl
3. if needed, run a broader discovery fallback

Recommended normal flow:

1. answer lane begins research
2. trusted URLs are collected from answer-lane research
3. the image worker uses those trusted URLs first
4. for each strong source page, run `firecrawl.scrape` with `formats: ["images"]`
5. rank the candidate images
6. ingest the winning image into COSMIC artifact storage
7. emit a replacement block

### Ranking Strategy

Rank candidate images by:

- source trust
- page relevance to the user question
- image metadata relevance
- nearby page title/headings
- nearby caption/alt text when available
- image size and aspect ratio
- duplicate/logo/ad/banner filtering

### Validation Strategy

Do not visually validate every candidate.

Use:

- metadata-first ranking for all candidates
- optional cheap visual validation only for the top one candidate when confidence is low or ambiguity remains

This keeps the pipeline fast while avoiding obviously wrong picks.

### Image Search Budget

Hard defaults for production:

- max image slots per turn: `1`
- max concurrent image sidecars per turn: `1`
- max trusted source pages to inspect first: `3`
- max broader-discovery fallback pages: `2`
- max candidate images scored per slot: `12`
- per-slot soft timeout: `3500 ms`
- max downloaded image bytes before ingestion: `8 MB`

If no strong candidate is ready inside that budget, drop the slot cleanly.

### Important Production Rule

Do not hotlink external images directly into the UI.

The winning image must be:

1. downloaded
2. stored in COSMIC artifact storage
3. hashed and attributed
4. re-served through signed COSMIC preview URLs

This avoids:

- broken remote links
- expired third-party access
- CORS issues
- replay/resume failures

## Chart Enrichment Flow

Charts should use the same slot-and-replace model, but the source is structured data rather than web image candidates.

### Inputs

The chart worker should accept:

- slot id
- chart type
- chart title
- normalized data series
- axis labels
- short explanatory caption

### Data Sources

Chart data may come from:

- tabular agent outputs
- deterministic query results
- structured numeric data extracted during research
- explicit numeric lists prepared by the answer lane

### Rendering Strategy

The chart worker should be runtime-managed and non-blocking.

Recommended implementation:

- a dedicated chart worker using a deterministic Python/matplotlib sandbox
- or a dedicated internal chart renderer built on an existing trusted sandbox primitive

Do not use Opus provider-side `code_execution` as the primary production chart lane, because that remains tied to the model loop.

### Chart Outputs

The chart worker should produce:

- inline PNG artifact for the UI
- structured chart spec JSON for replay/debugging
- optional CSV/JSON data artifact for internal traceability

Only the inline PNG needs to appear in the response.

### Chart Budget

Hard defaults for production:

- max chart slots per turn: `1`
- max concurrent chart sidecars per turn: `1`
- per-slot soft timeout: `4000 ms`
- max data points rendered in a single chart: `200`
- max rendered chart image bytes before ingestion: `4 MB`

If the chart spec or data exceeds these bounds, the chart should be simplified or skipped rather than delaying the answer.

## Supporting Artifacts vs Produced Artifacts

This feature requires a second artifact class.

### Produced Artifacts

These remain what they are today:

- user-deliverable files
- shown in `Produced Files`
- typically downloadable

### Supporting Artifacts

These are new:

- artifact-backed inline visuals
- not shown in `Produced Files`
- usually not directly downloadable in the UI
- persisted so history/resume can remint preview URLs

Recommended metadata shape:

- `produced_artifacts`: deliverable files
- `supporting_artifacts`: inline-only backing assets

The final response block can refer to either class by `artifact_id`, but the UI should only render `produced_artifacts` in the produced-file surface.

## Why Supporting Artifacts Are Required

Today, response block hydration depends on artifact lookup against `produced_artifacts`.

That is not enough for inline-only visuals because:

1. they should not show up in `Produced Files`
2. they still need signed preview URLs on replay/resume
3. they still need stable storage and provenance

So the gateway must be extended to hydrate response blocks against both:

- `produced_artifacts`
- `supporting_artifacts`

## Gateway Responsibilities In The Final Plan

The gateway must stop treating visual mode as "just content plus final produced artifacts."

Required changes:

1. own a new `preferences.db` store for VM-global product preferences
2. expose typed desktop-facing preference endpoints such as `GET /desktop/preferences` and `PATCH /desktop/preferences`
3. broadcast a realtime `preferences.updated` event when the stored preference changes
4. resolve `visual_response_enhancement_enabled` once per accepted request from the stored Gateway preference
5. persist the resolved effective preference into request/message metadata
6. accept and forward `response.blocks.snapshot`
7. persist explicit final `response_blocks`
8. persist `supporting_artifacts`
9. hydrate block previews from both artifact collections
10. keep cross-channel history replay stable

The final `response.complete` event should contain:

- final `content`
- final `response_blocks`
- final `produced_artifacts`
- final `supporting_artifacts`

The gateway should persist those directly, not reconstruct visual placement from plain text after the fact.

Important preference rule:

- a preference change affects subsequent turns only
- the Gateway must not mutate an active response because the user flipped the toggle mid-stream

## Frontend Responsibilities In The Final Plan

The frontend must handle both Gateway-backed preference sync and renderer behavior.

### Preferences UI

Add:

- a new top-level `Preferences` page in desktop Settings
- a `Visual Response Enhancement` toggle there
- Gateway fetch/save wiring through Electron IPC
- startup/connect/resume refresh of Gateway-backed preferences
- realtime update handling for `preferences.updated`

Important rule:

- this preference must not be saved through the existing desktop-local `saveSetting` path
- local desktop storage remains for device/UI settings only

### Renderer Modes

### Non-Visual Mode

Keep the current behavior untouched.

### Visual Mode

Add:

- block-snapshot streaming
- slot skeleton rendering
- in-place slot replacement by stable block id
- final canonical block rendering on `response.complete`

Important rule:

In visual mode, the renderer must treat blocks as the primary response model from the start. It must not begin with plain `content` streaming and switch later.

## Orchestrator Responsibilities In The Final Plan

The orchestrator remains the main intelligence layer, but it gets one new responsibility:

- decide when a visual slot should exist

The orchestrator does not directly execute the visual enrichment work synchronously.

It should:

1. answer normally
2. emit slot directives where visuals belong
3. provide the sidecar with search hints and later source hints
4. continue writing while the sidecar works

## Prompt Changes

When visual mode is enabled, the orchestrator prompt must be extended with a strict visual policy.

It should tell Opus:

1. visuals are optional and should only be used when they materially help
2. use at most a small number of visuals per answer
3. emit slot directives only when placement matters
4. do not wait for visuals before answering
5. prefer charts for quantitative comparisons
6. prefer images for appearance/examples/reference screenshots
7. if no trustworthy visual is likely, skip the slot

This prompt change must be mode-gated. It should not affect normal text-only answers.

## Parallelism Model

The final system should not rely on provider-side concurrency alone.

The intended concurrency is:

1. main answer lane
2. image enrichment lane
3. chart enrichment lane

These run concurrently under runtime control.

The answer lane is always primary.

Production budgets:

- no waiting before first token
- max visuals per turn: `2`
- default visual mix per turn: `1` image slot and `1` chart slot
- max concurrent sidecars per turn: `2`
- sidecar launch target: within `150 ms` of slot creation
- finalization grace window: `750 ms`
- if a slot is still pending after the grace window, remove it from the final canonical block list

If a visual misses the budget, drop it cleanly.

## Failure And Fallback Behavior

The system must degrade safely.

If visual mode is on and a visual task fails:

1. do not block or cancel the answer
2. mark the slot failed internally
3. remove or collapse the slot before finalization
4. complete the response normally

The user should never see:

- dangling slot syntax
- raw runtime directives
- broken image URLs
- generic "preview unavailable" blocks for avoidable failures

If the visual is not ready in time, the response should complete without it.

## Security, Provenance, And Quality Rules

Every inline visual should have:

- block-level provenance in the response block itself
- source page URL
- source image URL when applicable
- source title/domain when available
- local artifact storage
- signed preview URL

The system should avoid:

- raw third-party hotlinks
- low-trust source images when a higher-trust page exists
- decorative hero images that are not semantically useful
- repeated duplicate visuals

Artifact metadata should remain the storage and delivery source of truth, but it must not be the only place where provenance exists. The response block itself should stay explainable on replay, export, debugging, and UI inspection.

## Definition Of Done

This feature is complete only when all of the following are true:

1. the user can toggle visual enhancement on and off in a dedicated `Preferences` settings page
2. the preference is stored in a Gateway-backed preference store and survives desktop reconnects/restarts
3. desktop clients refresh and stay in sync through Gateway reads plus `preferences.updated`
4. off mode behaves exactly like the current production system
5. on mode streams block-first responses with placeholder slots
6. images can appear inline at the intended slot during streaming
7. charts can appear inline at the intended slot during streaming
8. inline visuals are persisted and replayable
9. inline visuals do not appear in `Produced Files`
10. the response can complete cleanly when visuals are skipped or fail
11. first-token latency is not materially worse than current production behavior
12. reconnects and duplicate deliveries cannot corrupt block order or slot replacement
13. canceled or superseded sidecars cannot mutate a newer response
14. replayed inline visuals remain attributable from block-level provenance alone
15. a mid-turn preference change does not mutate the active response and only affects later turns

## Implementation Workstreams

The full implementation should be delivered through these required workstreams:

1. Gateway-backed preference store and desktop preference API
2. desktop `Preferences` page plus Electron IPC for Gateway preference sync
3. visual-mode prompt and slot-directive policy
4. orchestrator-side `VisualEnrichmentCoordinator`
5. block-snapshot streaming contract
6. frontend slot rendering and replacement
7. `supporting_artifacts` persistence and hydration
8. image enrichment worker
9. chart enrichment worker
10. end-to-end tests for preference sync, visual mode on/off, slot success, slot timeout, slot failure, and history replay

None of these should be treated as optional if the goal is a real production release.

## Final Recommendation

The correct production architecture is:

- store visual enhancement as a Gateway-backed VM-global preference, not as a local-only UI setting
- keep the current path unchanged when visual enhancement is off
- when visual enhancement is on, switch to block-first streaming
- use pending visual slots
- run image/chart enrichment in runtime-managed background sidecars
- store inline visuals as supporting artifacts
- persist the final response as explicit blocks, not reconstructed placement

This preserves the current production system while adding a fully capable visual-response mode without turning visuals into a latency or reliability hazard.

## Implementation Note

- The current implementation defines `visual_max_visuals_per_turn` as a top-level budget knob, but the runtime currently enforces the per-kind slot caps (`visual_max_image_slots_per_turn` and `visual_max_chart_slots_per_turn`) rather than the aggregate cap directly. With the default `1 + 1` limits this still behaves like a total cap of 2, so this is acceptable for now, but the aggregate knob is not independently authoritative yet.
