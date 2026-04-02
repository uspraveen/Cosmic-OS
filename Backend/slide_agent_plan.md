# Slide Deck Specialist Agent — Implementation Plan

## Agent Identity

- **Agent ID:** `cosmic/slide-agent:1.0.0`
- **Display Name:** Slide Agent
- **Location:** `Backend/agents/slide_agent/`
- **Entry point:** `python -m agents.slide_agent`
- **LLM:** gpt-5-mini (planning + vision validation)
- **Rendering:** python-pptx → LibreOffice headless → PNG for validation
- **Output:** Editable `.pptx` + optional `.pdf` export

## Intents

| Intent | Description | Timeout |
|---|---|---|
| `slide.create` | Create a new slide deck from a description, document, or data | 300s |
| `slide.edit` | Edit an existing PPTX (add/remove/reorder, restyle, modify content) | 300s |
| `slide.recall_session` | Recall prior slide operations from session ledger | 30s |

## Dependencies

| Package | Status | Purpose |
|---|---|---|
| `python-pptx` | **NOT in project** — must add to `requirements.txt` | PPTX creation/editing |
| `Pillow` | Available (requirements.txt:13) | Image resize/crop/format |
| `LibreOffice` | Available (used by docs_parser) | PPTX→PDF, PDF→PNG rendering |
| `pdftoppm` | System binary (poppler-utils) | PDF pages → PNG for validation |
| `docling` | Available | Document parsing for input PDFs |
| `diagram-agent` | Built in this session | Generate diagrams for embedding |
| `image-generator-agent` | Built previously | Generate custom images from prompts |

## Rendering Architecture

```
LLM plan (JSON) → python-pptx builder → .pptx → LibreOffice → .pdf (optional)
                                               → pdftoppm → per-slide PNG → vision validation
```

**python-pptx native capabilities (73 chart types, full image/layout/edit support):**

| Capability | Support |
|---|---|
| Chart creation (bar, line, pie, scatter, area, stock, radar, bubble, surface, 3D) | Native — 73 types |
| Image placement/sizing (EMU system) | Native |
| Slide layouts/placeholders | Native |
| Edit existing PPTX | Native |
| Cell fill/text formatting | Native |
| Solid/pattern fill, theme colors | Native |
| Slide backgrounds | Native |
| Font styling | Native |
| Cell borders | XML workaround (OxmlElement) |
| Slide transitions | XML workaround (parse_xml) |
| Shape animations | **NOT possible** — skip for Phase 1 |

## JSON Slide Definition Format

The LLM produces a `DeckPlan` JSON. The deterministic builder executes it.

```json
{
  "deck": {
    "title": "COSMIC Architecture Overview",
    "template": "corporate-dark",
    "theme": {
      "primary_color": "#1a1a2e",
      "accent_color": "#e94560",
      "text_color": "#ffffff",
      "font_family": "Calibri",
      "font_size_title": 28,
      "font_size_body": 16
    },
    "dimensions": {"width": 13.333, "height": 7.5}
  },
  "slides": [
    {
      "slide_number": 1,
      "layout": "title_slide",
      "title": "COSMIC: Personal AI Architecture",
      "subtitle": "Multi-Agent System Design",
      "background": {"type": "solid", "color": "#1a1a2e"},
      "speaker_notes": "Welcome to the COSMIC architecture overview..."
    },
    {
      "slide_number": 2,
      "layout": "two_content",
      "title": "System Components",
      "left_content": {
        "type": "bullets",
        "items": ["Gateway — OAuth + routing", "Orchestrator — intent dispatch", "Model Router — cost optimization"]
      },
      "right_content": {
        "type": "image",
        "source": {"kind": "generate", "prompt": "Clean system architecture diagram showing...", "agent": "diagram"},
        "placement": {"width_inches": 5.5, "height_inches": 4}
      },
      "speaker_notes": "Walk through each component..."
    },
    {
      "slide_number": 3,
      "layout": "content_with_chart",
      "title": "Agent Response Times",
      "content": {
        "type": "chart",
        "chart_type": "column_clustered",
        "data": {
          "categories": ["Email", "Calendar", "Diagram", "Tabular"],
          "series": [{"name": "Avg Latency (ms)", "values": [1200, 800, 3500, 2100]}]
        },
        "style": {"has_legend": false, "data_labels": true}
      },
      "speaker_notes": "The diagram agent is slowest due to rendering..."
    },
    {
      "slide_number": 4,
      "layout": "content_with_image",
      "title": "Credential Flow",
      "content": {
        "type": "bullets",
        "items": ["Desktop initiates OAuth", "Gateway stores encrypted tokens", "Orchestrator resolves at dispatch"]
      },
      "image": {
        "source": {"kind": "from_asset", "asset_ref": "docs_parser:figure_03"},
        "placement": {"x_inches": 6, "y_inches": 1.5, "width_inches": 6, "height_inches": 4}
      }
    },
    {
      "slide_number": 5,
      "layout": "table_slide",
      "title": "Agent Capabilities Matrix",
      "table": {
        "headers": ["Agent", "Intents", "Auth", "Renderer"],
        "rows": [
          ["Email Agent", "5", "Google OAuth", "N/A"],
          ["Diagram Agent", "3", "None", "Mermaid/D2/Excalidraw"],
          ["Calendar Agent", "6", "Google OAuth", "N/A"]
        ],
        "style": {
          "header_bg": "#1a1a2e",
          "header_text": "#ffffff",
          "row_alt_bg": "#f0f0f0",
          "border_color": "#cccccc"
        }
      }
    }
  ]
}
```

## Built-In Templates (Phase 1: 4 templates)

| Template | File | Layouts |
|---|---|---|
| `corporate-dark` | `templates/corporate-dark.pptx` | title_slide, content, two_content, section_divider, table_slide, blank_with_footer |
| `corporate-light` | `templates/corporate-light.pptx` | Same layouts, light theme |
| `minimal` | `templates/minimal.pptx` | Clean minimal layouts |
| `pitch-deck` | `templates/pitch-deck.pptx` | Problem, solution, market, team, ask |

Each template is a real `.pptx` file with slide master, layouts, placeholders, and theme colors. User-uploaded templates supported from day one — any `.pptx` works because python-pptx reads its layouts directly.

## Internal Tools

| Tool | Implementation | Purpose |
|---|---|---|
| `resize_image` | Pillow | Resize images to fit slide placement |
| `crop_image` | Pillow | Crop to target aspect ratio |
| `convert_image_format` | Pillow | PNG↔JPEG↔WEBP, RGBA→RGB |
| `build_chart` | python-pptx ChartData | Create chart from structured data |
| `apply_cell_borders` | OxmlElement XML workaround | Table cell borders |
| `apply_slide_transition` | parse_xml XML workaround | Slide transitions |
| `generate_diagram` | Delegates to diagram agent | Create diagram for embedding |
| `generate_image` | Delegates to image generator agent | Create custom image |

## LangGraph Workflow

```
START → analyze_request → [create_plan?] → prepare_assets → build_slides → render_and_validate → [fix?] → finalize → END
```

All logic lives in graph nodes and the `slide_builder.py` module. No separate `deck_inspector` or `slide_renderer` files — inspection and rendering are helper functions within nodes.

### Nodes

**1. `analyze_request`** — LLM analyzes input (text, PDF, existing PPTX, data) and produces the DeckPlan JSON. May also return `create_plan` for multi-deck requests.

**2. `prepare_assets`** — For slides needing images/charts/diagrams:
- Calls diagram agent for diagram source images
- Calls image generator agent for custom images
- Resizes/crops images to target dimensions using Pillow
- Loads existing assets from docs_parser bundles (PDF input → extracted figures)

**3. `build_slides`** — Deterministic builder (calls `SlideBuilder` from `slide_builder.py`):
- Loads template with `Presentation('template.pptx')` or creates blank
- For each slide in the plan:
  - Adds slide with specified layout
  - Sets title, subtitle, body text with formatting
  - Adds images at specified positions (EMU)
  - Creates charts from data (ChartData → add_chart)
  - Builds tables with styling (header fills, alternating rows, borders via XML)
  - Applies slide backgrounds
  - Sets speaker notes
- For edit intents: modifies existing slides (update text, replace images, reorder, delete, add new)
- Saves to `runs/artifacts/<task_id>/slide_agent/deck.pptx`

**4. `render_and_validate`** — Vision-based quality check:
- Renders each slide to PNG via LibreOffice + pdftoppm
- Sends each PNG to gpt-5-mini vision for quality assessment
- Checks: text readability, image placement, chart clarity, color contrast, whitespace balance
- If issues found and attempts < 2: fixes in build_slides and re-validates (same pattern as diagram agent)

**5. `finalize`** — Optionally exports to PDF via LibreOffice. Returns AgentResult with PPTX artifact + per-slide PNG previews.

### StepPlan Integration

Same pattern as tabular/diagram agents. For multi-step requests (e.g., "create 3 separate decks"), `create_plan` creates a StepPlan and loops through each deck.

## Editing Operations (slide.edit)

The edit intent accepts an existing PPTX artifact and an edit specification:

```json
{
  "source_pptx": "artifact_ref_here",
  "operations": [
    {"action": "add_slide", "after_slide": 3, "layout": "content", "content": {...}},
    {"action": "remove_slide", "slide_number": 5},
    {"action": "move_slide", "from": 2, "to": 4},
    {"action": "update_slide", "slide_number": 1, "changes": {"title": "New Title"}},
    {"action": "update_text", "slide_number": 3, "shape_name": "Content Placeholder", "text": "Updated content"},
    {"action": "replace_image", "slide_number": 4, "shape_name": "Picture", "new_image": {"kind": "generate", "prompt": "..."}},
    {"action": "restyle_deck", "template": "corporate-light"}
  ]
}
```

The LLM translates user requests ("add a summary slide at the end, change the title, restyle with the light template") into this structured operation list.

## Visual Validation (per-slide)

```
Slide N → LibreOffice → PNG → gpt-5-mini vision → {pass, issues, suggestion}
```

Vision checks:
1. **Text readability** — All text legible, font size adequate
2. **Image quality** — Images properly placed, not distorted
3. **Layout balance** — Good whitespace, no overcrowding
4. **Color contrast** — Text readable against background
5. **Chart clarity** — Chart labels visible, data readable
6. **Consistency** — Slides follow consistent style

If validation fails, suggestion goes back to builder for fixes (same loop pattern as diagram agent).

## File Structure

```
agents/slide_agent/
├── __init__.py
├── __main__.py
├── agent_card.yaml
├── agent.py                       # SlideAgent (AgentRuntime subclass)
├── agent.env.example
├── config.py                      # SlideAgentConfig
├── slide_graph.py                 # LangGraph workflow (all nodes + helpers)
├── slide_builder.py               # Deterministic PPTX builder (python-pptx)
├── internal_llm.py                # gpt-5-mini: planning, vision validation, regeneration
├── asset_manager.py               # Image resize/crop, diagram/image agent delegation
├── templates/
│   ├── corporate-dark.pptx
│   ├── corporate-light.pptx
│   ├── minimal.pptx
│   └── pitch-deck.pptx
├── prompts/
│   ├── system.md
│   └── policies.md
├── schemas/intents/
│   ├── slide.create.input.json
│   ├── slide.create.output.json
│   ├── slide.edit.input.json
│   ├── slide.edit.output.json
│   ├── slide.recall_session.input.json
│   └── slide.recall_session.output.json
├── store/
│   ├── data/
│   └── learnings.md
├── tests/
│   ├── __init__.py
│   ├── test_agent.py
│   ├── test_slide_builder.py
│   └── test_deck_inspector.py
└── agent.env.example
```

**No separate `deck_inspector.py` or `slide_renderer.py`.** Deck inspection logic (extracting structure from existing PPTX) is a helper function within `slide_graph.py` nodes. Slide rendering (LibreOffice → PNG) is a helper within the `render_and_validate` node. Both are called from within the graph, not as standalone modules.

## Integration Points

| Component | Change |
|---|---|
| `requirements.txt` | Add `python-pptx` |
| `bootstrap.py` | Add `SLIDE_AGENT_ENV_NAME`, `SLIDE_AGENT_SERVICE_NAME`, `SLIDE_AGENT_ID`, `SLIDE_AGENT_DEFAULT_INSTANCE_ID` constants; add `cosmic-slide-agent.service` to `CORE_BACKEND_SERVICE_UNITS` |
| `systemd/` | Add `cosmic-slide-agent.service.example` |
| Orchestrator | Agent self-registers via Redis heartbeats — discovers through `slide.create` intent |

## Phase Scope

| Phase | What |
|---|---|
| **Phase 1** | Create from description, 4 built-in templates, chart/image/table support, vision validation, PDF export, basic edit (add/remove/update slides) |
| **Phase 2** | Advanced editing (restyle deck, move slides, complex table styling), user-uploaded templates, slide transitions (XML workaround), multi-deck plans |
| **Phase 3** | Shape animations (COM automation or commercial library), collaborative editing, version tracking, design system enforcement |

## What Makes This "World-Best"

1. **LLM plans the entire deck** — not just text, but image prompts, chart types, data mapping, per-slide layout decisions
2. **Visual verification on every slide** — not just "does it render" but "does it look good"
3. **Delegated visual assets** — diagrams from diagram agent, images from image generator, not text-only slides
4. **Full edit capability** — add, remove, reorder, restyle, modify content, replace images
5. **Editable output** — .pptx files open in PowerPoint with full formatting preserved
6. **Template system** — consistent professional design, user-uploadable from day one
