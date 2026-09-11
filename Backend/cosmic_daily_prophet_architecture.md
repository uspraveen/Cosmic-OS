# Cosmic Daily Prophet — Architecture

Status: implemented v1 · Surface: Spaces → My Prophet · No new specialist agent required in v1

## Goal

A personalized daily news digest, curated by the orchestrator on a schedule, persisted as a validated edition, and rendered as a broadsheet in the Spaces Prophet screen. Two editions per day by default (morning 05:00, evening 19:00 local), fully user-configurable.

## Principles

1. Contract first, renderer second. The edition is data; layout geometry is deterministic code.
2. The model owns editorial intent (roles and section layout intents); code owns geometry, downgrades, and safety.
3. No chat pollution. Publishing notifies through the existing overlay card; the run transcript stays in normal request traces.
4. Heartbeat-style memory: a fresh context packet goes in, a persisted edition comes out, and lookups happen through tools.
5. Reuse existing infrastructure: scheduler crons, orchestrator tools, gateway feature modules, IPC/WS patterns.

## Data contract

Edition payload (validated server-side, `Backend/gateway/prophet/store.py`):

```
ProphetEdition:
  schema_version, edition_date (YYYY-MM-DD), slot: morning|evening,
  editor_note,
  lead: Story            # optional; auto-promoted from the highest-importance story
  sections: [{ id, label, layout, quiet_day_text?, stories: [Story] }]
  footer?

Story:
  id, headline, dek?, body: [1-3 short paragraphs in cosmic's own words],
  role: lead|feature|standard|brief|pull_quote|image_led,
  importance: 0-100,
  source{name,url}, published_at?, image?{url,caption,credit},
  why_selected, tags[]
```

Section layout intents chosen by the model: `feature | columns | briefs | gallery | essay`.

Resolution rules:

- Intent + actual count + image presence → template `m1..m5` in `src/prophetFeed.ts`.
- Soft fixes downgrade deterministically and come back as warnings from `publish_prophet_edition`:
  `gallery` with fewer than two images → `columns`; `image_led` without an image → `standard`;
  `brief` with body → body dropped; no lead → highest-importance story promoted; multiple section
  `lead` roles → top importance wins, the rest become `feature`.
- Hard validation errors only: unknown enum, malformed story, empty edition, story cap exceeded.
- Story cap: default 15, user-configurable 5–30 (hard cap 30).
- Dedup: a story seen in the previous three editions produces a warning, not a rejection.

## Persistence

Dedicated gateway module `Backend/gateway/prophet/` with its own SQLite database `gateway/prophet.db`
(WAL). Scheduler data stays in `scheduler.db`.

| Table | Contents |
|---|---|
| `prophet_settings` | enabled, morning_time, evening_enabled/time, max_stories, notifications_enabled, sections_json |
| `prophet_interests` | topic, origin(`user`/`inferred`), weight, muted |
| `prophet_sources` | kind(`rss`/`x`/`site`), value, label, origin, active |
| `prophet_editions` | unique(date, slot), schema_version, status, payload_json, editor_note, story_count, generated_by_request_id, revision |
| `prophet_story_index` | url_hash/headline_key per edition for dedup and archive lookups |

Curator (orchestrator tool) may only add/remove/mute `inferred` entries; user-added entries are protected.

## Scheduling

- Managed system crons `prophet.morning` and `prophet.evening`, seeded from settings by
  `GatewayRuntime._sync_prophet_crons()`; times are converted to 5-field cron expressions in the
  user profile timezone.
- Settings save recomputes the expression and next fire and wakes the scheduler; the enable toggle
  pauses/resumes both crons. Autopilot lists them; the Prophet settings panel is the source of truth.
- Metadata carries `purpose=daily_prophet`, `prophet_slot`, a short prompt, and desktop delivery.

## Generation pipeline

- The cron prompt is compact but strict: write the user's own edition (rewrite headlines and body copy in
  COSMIC's voice, connect stories to the user's world), keep research bounded, include image URLs for the
  lead and most stories, and always end with `publish_prophet_edition`. Sources are unrestricted
  (web search, Perplexity, X, Firecrawl, browser, specialist delegation).
- Fresh context packet per run (`prophet/context.py`): settings summary, enabled sections, active
  interests, muted topics, preferred sources, and the last three days of shown headlines for dedup.
- The run response is suppressed from chat when `prophet_store.find_edition_by_request_id()` finds a
  published edition for the request; failures stay visible.

## Orchestrator tools

| Tool | Purpose |
|---|---|
| `publish_prophet_edition` | Validate, cap-check, publish, broadcast notification |
| `read_prophet_edition` | Full edition (defaults to latest) |
| `list_prophet_editions` | Recent editions with headlines for dedup checks |
| `update_prophet_preferences` | Add/remove/mute inferred interests and sources |

Tool group: `prophet` ("Daily Prophet") in the orchestrator prompt catalog.

## Gateway routes

- Desktop: `GET /desktop/prophet/edition`, `GET /desktop/prophet/editions`,
  `GET/PUT /desktop/prophet/settings`, `POST /desktop/prophet/preferences`
- Internal: `GET /internal/prophet/edition`, `GET /internal/prophet/editions`,
  `POST /internal/prophet/editions`, `POST /internal/prophet/preferences`
- WS: `prophet.edition.published` (desktop + mobile), gated by `notifications_enabled`

## Frontend

- `src/prophetFeed.ts`: pure normalizer, accent mapping, relative time, dateline, paper-style metadata,
  domain extraction, and the deterministic `resolveProphetLayout()` used by the renderer. Covered by
  `src/prophetFeed.test.ts`.
- `SpacesControlCenter.tsx`: feed-driven `renderProphetPage()` with masthead, lead, sections, image
  figures with captions/credits, empty/error states, refresh + polling, `onGatewayEvent` live refresh,
  and a glass settings panel (schedule, cap, sections, interests, sources, paper style).
- Story source links open a glass hover preview (headline, image, domain, open-in-browser) instead of a
  bare link.
- `spaces-control.css`: the paper canvas is scoped to `.prophet-page` with selectable styles
  (`parchment`, `newsprint`, `ivory`, `midnight`), layered grain/stain textures, ink rules, Newsreader
  body type bundled in `src/assets/fonts/`, layout modes `m1..m5`, and responsive rules. Toolbar and
  settings chrome use the app's glass design tokens.
- `App.tsx`: `prophet.edition.published` enqueues a "Daily Prophet ready" card; "Read edition" opens
  Spaces → My Prophet through `prophetNavigateSignal`.
- IPC: `electron/preload.ts` + `electron/main.ts` handlers for the desktop routes above.

## Out of scope (future)

- `prophet_gatherer` specialist if daily research becomes slow/costly.
- AI-generated images: artifacts created inside tool runs need an artifact → served-asset bridge.
- Mobile push deep-link polish; layout QA vision pass; per-section reordering UI.
