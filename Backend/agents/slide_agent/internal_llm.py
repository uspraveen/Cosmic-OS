"""Slide-agent internal LLM via LangChain OpenAI-compatible client.

Defaults to Fireworks Kimi K2.5 for:
- Deck planning: analyze input → DeckPlan JSON
- Edit planning: translate edit requests → operation list
- Vision validation: check rendered slide PNGs for quality
- Deck repair: regenerate plan based on validation feedback
"""

from __future__ import annotations

import json
import logging
import time
from io import BytesIO
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image, ImageStat

from shared.usage import UsageEvent, post_usage_event, serialize_usage_metadata

from .config import SlideAgentConfig

logger = logging.getLogger(__name__)


def _smart_truncate_json(data: Any, *, limit: int) -> str:
    """Truncate a JSON-serializable object to `limit` chars, cutting at structural
    boundaries (array items, object keys) instead of mid-value.

    Falls back to naive truncation if the data is a flat string.
    """
    full = json.dumps(data, indent=2, ensure_ascii=False)
    if len(full) <= limit:
        return full

    if isinstance(data, dict):
        result: dict[str, Any] = {}
        budget = limit - 10  # reserve for braces/whitespace
        for key, value in data.items():
            entry = json.dumps({key: value}, indent=2, ensure_ascii=False)
            if len(entry) > budget:
                # Try to include a truncated version of this value
                if isinstance(value, str) and len(value) > 200:
                    truncated_value = value[: budget - 50].rsplit(" ", 1)[0] + "..."
                    result[key] = truncated_value
                elif isinstance(value, list) and value:
                    # Include as many list items as fit
                    items: list[Any] = []
                    for item in value:
                        item_str = json.dumps(item, ensure_ascii=False)
                        budget -= len(item_str) + 5
                        if budget < 0:
                            break
                        items.append(item)
                    if items:
                        result[key] = items
                break
            budget -= len(entry)
            result[key] = value
        return json.dumps(result, indent=2, ensure_ascii=False)[:limit]

    if isinstance(data, list):
        items = []
        budget = limit - 10
        for item in data:
            item_str = json.dumps(item, indent=2, ensure_ascii=False)
            if len(item_str) > budget:
                break
            budget -= len(item_str) + 3
            items.append(item)
        return json.dumps(items, indent=2, ensure_ascii=False)[:limit]

    # Fallback: word-boundary truncation
    clipped = full[:limit].rsplit(" ", 1)[0]
    return clipped + "..." if clipped != full else full


def _is_openrouter_base_url(base_url: str) -> bool:
    return "openrouter.ai" in (base_url or "").strip().lower()


def _is_fireworks_base_url(base_url: str) -> bool:
    return "fireworks.ai" in (base_url or "").strip().lower()


def _usage_provider_name(cfg: SlideAgentConfig) -> str:
    if _is_openrouter_base_url(cfg.mimo_base_url):
        return "openrouter"
    if _is_fireworks_base_url(cfg.mimo_base_url):
        return "fireworks"
    return "openai_compatible"


def _supports_temperature(model_name: str) -> bool:
    normalized = (model_name or "").strip().lower()
    return not normalized.startswith("gpt-5")


def _effective_temperature(cfg: SlideAgentConfig) -> float:
    raw = max(0.0, float(cfg.mimo_temperature))
    if _is_fireworks_base_url(cfg.mimo_base_url) and "kimi" in cfg.mimo_model.lower():
        return min(raw, 0.6)
    if _is_openrouter_base_url(cfg.mimo_base_url) and "qwen/" in cfg.mimo_model.lower():
        return min(raw, 1.0)
    return raw


def _extra_body(cfg: SlideAgentConfig) -> dict[str, Any] | None:
    if not _is_openrouter_base_url(cfg.mimo_base_url):
        return None
    if not cfg.mimo_reasoning_enabled or cfg.mimo_reasoning_max_tokens <= 0:
        return None
    return {
        "reasoning": {
            "enabled": True,
            "max_tokens": int(cfg.mimo_reasoning_max_tokens),
        }
    }


def _default_headers(cfg: SlideAgentConfig) -> dict[str, str] | None:
    if not _is_openrouter_base_url(cfg.mimo_base_url):
        return None
    headers: dict[str, str] = {}
    app_name = (cfg.mimo_app_name or "").strip()
    if app_name:
        headers["X-Title"] = app_name
    site_url = (cfg.mimo_site_url or "").strip()
    if site_url:
        headers["HTTP-Referer"] = site_url
    return headers or None


async def _invoke_llm(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    system_content: str,
    user_content: str | list[dict[str, Any]],
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Core LLM invocation with usage logging. Returns parsed JSON or None."""
    if not cfg.enable_internal_llm or not cfg.mimo_api_key or not cfg.mimo_base_url:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("slide_agent.langchain_unavailable: %s", exc)
        return None

    if isinstance(user_content, str):
        human_msg = HumanMessage(content=user_content[:120_000])
    else:
        human_msg = HumanMessage(content=user_content)

    messages = [SystemMessage(content=system_content.strip()), human_msg]

    started = time.perf_counter()
    llm_call_id = f"slide_mimo_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.mimo_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as mimo_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.mimo_model,
                "api_key": cfg.mimo_api_key,
                "base_url": cfg.mimo_base_url,
                "http_async_client": mimo_http,
            }
            if _supports_temperature(cfg.mimo_model):
                llm_kwargs["temperature"] = _effective_temperature(cfg)
            extra_body = _extra_body(cfg)
            if extra_body is not None:
                llm_kwargs["extra_body"] = extra_body
            default_headers = _default_headers(cfg)
            if default_headers is not None:
                llm_kwargs["default_headers"] = default_headers
            llm = ChatOpenAI(**llm_kwargs)
            result = await llm.ainvoke(messages)
    except Exception as exc:
        logger.warning("slide_agent.internal_llm_error: %s", exc)
        return None

    latency_ms = (time.perf_counter() - started) * 1000
    raw_content = (result.content or "").strip() if result else ""

    usage_meta = {}
    if result:
        usage_meta = serialize_usage_metadata(result)

    try:
        await post_usage_event(
            http_client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=UsageEvent(
                llm_call_id=llm_call_id,
                llm_call_placed_at=time.time() - (latency_ms / 1000),
                agent_id="cosmic/slide-agent:1.0.0",
                task_id=task_id or "",
                session_id=session_id or "",
                source=source or "",
                source_id=source_id or "",
                channel=channel or "",
                provider=_usage_provider_name(cfg),
                model=cfg.mimo_model,
                usage_kind="chat_completion",
                ok=True,
                latency_ms=latency_ms,
                usage=usage_meta,
            ),
        )
    except Exception:
        pass

    if not raw_content:
        return None

    json_str = raw_content
    if "```" in json_str:
        parts = json_str.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                json_str = stripped
                break

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return None


# ── Deck Planning ─────────────────────────────────────────────────────────────

_PLAN_SYSTEM = """\
You are a world-class presentation designer. Given a request to create a slide deck,
produce a complete DeckPlan JSON that results in polished, professional slides.

## TEMPLATE-GUIDED DESIGN (MANDATORY)

You will receive `_template_layouts` showing the available layouts and their placeholder
zones. Every slide MUST use template-guided `assignments`.

**The `assignments` field is REQUIRED for every non-BLANK slide.**

Each layout has named placeholders with fixed positions and indices. Your job is to:
1. Choose the right layout for each slide from `_template_layouts[].name` (EXACT match)
2. Assign content to placeholders using ONLY indices from that layout's `placeholders[].idx`
3. NEVER invent x/y coordinates — the template controls all positioning
4. NEVER use placeholder indices 10, 11, 12 (those are footer/date/slide_number)

If you use a layout name not in the template, the slide BREAKS.
If you assign content to a non-existent placeholder index, the content is SILENTLY DROPPED.

## Output JSON Structure

Output ONLY valid JSON:
{
  "action": "generate",
  "deck": {
    "title": "string",
    "template": "template-name"
  },
  "slides": [
    {
      "slide_number": 1,
      "layout": "EXACT layout name from _template_layouts",
      "title": "string",
      "subtitle": "string (for TITLE layout only)",
      "assignments": {
        "0": { "type": "title", "text": "Slide Title" },
        "1": { "type": "body", "items": ["Point 1", "Point 2"] }
      },
      "speaker_notes": "string"
    }
  ]
}

## Assignment Rules

Map placeholder idx → content using the `assignments` field:

For **title/subtitle** placeholders:
  `{"type": "title", "text": "..."}`
  `{"type": "subtitle", "text": "..."}`

For **body/content** placeholders:
  `{"type": "body", "items": ["Point 1", "Point 2", ...]}` — bullet points
  `{"type": "numbered", "items": ["Step 1", "Step 2", ...]}` — numbered list
  `{"type": "paragraph", "text": "Full paragraph text"}` — prose

For **chart** in a body placeholder:
  `{"type": "chart", "chart_type": "column_clustered", "data": {"categories": [...], "series": [{"name": "...", "values": [...]}]}}`

For **table** in a body placeholder:
  `{"type": "table", "headers": ["Col1", "Col2"], "rows": [["val", "val"], ...]}`

For **image** in a picture/body placeholder:
  `{"type": "image", "source": {"kind": "from_asset", "asset_ref": "..."}}` — reuse existing
  `{"type": "image", "source": {"kind": "generate", "prompt": "...", "agent": "image"}}` — generate new

## EXCEPTIONS — Free-Flow Content (ONLY these three cases)

These content types need custom positioning and should use top-level fields instead of assignments:

1. **code_chart** (matplotlib/seaborn rendered as image):
   `"content": {"type": "code_chart", "code": "import matplotlib...", "data": {...}}`

2. **flow_diagram** (connected boxes with arrows):
   `"content": {"type": "flow_diagram", "boxes": [{"text": "Step", "shape": "rounded_rectangle", "fill": "#hex"}], "direction": "horizontal"}`

3. **BLANK layout** (no placeholders): Use `"content"` field with custom content.

For ALL other layouts, use `assignments`. Period.

## Template Selection

Prefer premium templates (★) over legacy ones. Match template to content:
- **business-meeting** ★: Default for general business, meetings, proposals, reports
- **tech-trends** ★: Data-heavy, technical, startup pitches, investor decks, product roadmaps (21 layouts)
- **science-lesson** ★: Educational, training, workshops, tutorials, onboarding
- **tech-infographics** ★: Infographics, visual comparisons, feature overviews, process flows

Legacy templates (corporate-dark, corporate-light, minimal, pitch-deck) have no real design.
Only use them if the user explicitly requests a dark theme or names them specifically.

## Slide Count Guidance

Match the number of slides to the scope of the request:
- Single intro/cover slide: 1 slide (title + subtitle, no bullets)
- Quick overview or summary: 3-5 slides
- Standard presentation (5-10 min talk): 7-12 slides
- Detailed report or training session: 15-25 slides
- Comprehensive workshop: 20-35 slides
- Always respect an explicit user request for slide count.
- When in doubt, err on the side of FEWER slides with stronger content.

## Aesthetic Standards

This must look like a professionally designed presentation, not auto-generated content:

- **Hierarchy**: One dominant headline, one clear secondary element, restrained supporting content.
- **Economy**: If a slide can say it in fewer words, say it in fewer words. Max 5-6 bullets, 5-8 words each.
- **Balance**: Content should feel centered within the template zones. No top-left-heavy dumps.
- **Breathing room**: Empty space is premium. Don't fill every placeholder just because it exists.
- **Title slides**: One bold title + one crisp subtitle. No bullet stacks.
- **Data slides**: Clear chart titles, labeled series, readable axes. No chart without context.
- **Closing slides**: End with a clear call-to-action, summary, or "thank you" — not an abrupt content dump.
- **Speaker notes**: Include meaningful notes for every slide — the presenter's talking points, not a repeat of the slide text.

## Font Constraint

ONLY system fonts: Calibri (default), Arial, Segoe UI, Helvetica, Verdana, Cambria,
Times New Roman, Georgia, Consolas, Courier New.
No emoji, dingbats, checkmark glyphs, or icon-like Unicode in titles or bullets.

## Rules Summary
- Every slide MUST have a `layout` that exists EXACTLY in `_template_layouts`
- Every non-BLANK slide MUST have `assignments` (not legacy `content`/`chart`/`table`/`image` fields)
- Every slide MUST have a `title` (except BLANK)
- Use `"action": "generate"` for single-deck requests
- Use `"action": "create_plan"` with `"steps": [...]` for complex multi-deck requests

## Source Materials & Document Context

If `_source_materials` is provided:
- Reuse existing visuals: `"source": {"kind": "from_asset", "asset_ref": "..."}`
- Only use `asset_ref` values from `_source_materials.visual_assets`
- Generate new images: `"source": {"kind": "generate", "prompt": "...", "agent": "image"}`

If you need more context, return:
{
  "action": "request_doc_context",
  "doc_request": {"intent": "docs.search_bundle" | "docs.read_bundle", "bundle_id": "...", ...}
}
Only use known `bundle_id`/`doc_id`/`asset_ref` values from `_source_materials`.

## Example — Good DeckPlan (3-slide overview)

Given request: "Create a brief project status update" with template `business-meeting` having
layouts: "Title Slide" (idx 0, 1), "Title and Content" (idx 0, 1), "Section Header" (idx 0, 1):

```json
{
  "action": "generate",
  "deck": {"title": "Project Alpha — Status Update", "template": "business-meeting"},
  "slides": [
    {
      "slide_number": 1,
      "layout": "Title Slide",
      "title": "Project Alpha",
      "subtitle": "Weekly Status Update — April 2026",
      "assignments": {
        "0": {"type": "title", "text": "Project Alpha"},
        "1": {"type": "subtitle", "text": "Weekly Status Update — April 2026"}
      },
      "speaker_notes": "Welcome everyone. This is our weekly sync on Project Alpha progress."
    },
    {
      "slide_number": 2,
      "layout": "Title and Content",
      "title": "Key Milestones",
      "assignments": {
        "0": {"type": "title", "text": "Key Milestones"},
        "1": {"type": "body", "items": [
          "API integration complete",
          "Performance benchmarks exceeded target by 15%",
          "User testing begins next Monday",
          "Launch target: April 28"
        ]}
      },
      "speaker_notes": "Walk through each milestone. Emphasize the performance win — stakeholders care about this."
    },
    {
      "slide_number": 3,
      "layout": "Section Header",
      "title": "Next Steps",
      "assignments": {
        "0": {"type": "title", "text": "Next Steps"},
        "1": {"type": "subtitle", "text": "User testing kickoff Monday — all hands welcome"}
      },
      "speaker_notes": "End with clear call-to-action. Invite team to join user testing sessions."
    }
  ]
}
```

Notice: short bullets, meaningful speaker notes, breathing room, clear hierarchy, no overcrowding.
"""


async def plan_deck(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    description: str,
    template: str = "",
    input_data: dict[str, Any] | None = None,
    learnings_context: str = "",
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Plan a full slide deck from a description."""
    context = f"Request: {description}\n"
    if template:
        context += f"Preferred template: {template}\n"
    desc_lower = description.lower()
    derived_design_brief: list[str] = []
    if not template and any(
        token in desc_lower for token in ("pitch deck", "pitchdeck", "investor")
    ):
        derived_design_brief.append(
            "Prefer the `tech-trends` template for pitch/investor decks — it has 21 layouts including big-number, data viz, and section headers designed for this purpose."
        )
    if any(
        token in desc_lower
        for token in (
            "intro slide",
            "cover slide",
            "title slide",
            "one slide",
            "1 slide",
            "single slide",
        )
    ):
        derived_design_brief.append(
            "Treat this as a premium one-slide cover: large headline, crisp subtitle, at most one short supporting line, strong contrast, and balanced composition."
        )
    if any(
        token in desc_lower
        for token in (
            "simple slide",
            "simple deck",
            "test slide",
            "test deck",
            "smoke test",
        )
    ):
        derived_design_brief.append(
            "Even if the request is simple, the design must feel polished, intentional, and not like placeholder output."
        )
    if derived_design_brief:
        context += "\nDesign steering:\n- " + "\n- ".join(derived_design_brief) + "\n"
    if input_data:
        extra_data = dict(input_data)
        template_layouts = extra_data.pop("_template_layouts", None)
        available_templates = extra_data.pop("_available_templates", None)
        source_materials = extra_data.pop("_source_materials", None)
        document_context = extra_data.pop("_document_context", None)
        if extra_data:
            context += (
                f"\nAdditional input data:\n"
                f"{_smart_truncate_json(extra_data, limit=3000)}\n"
            )
        if available_templates:
            context += (
                f"\nAvailable templates:\n"
                f"{_smart_truncate_json(available_templates, limit=3000)}\n"
            )
        if template_layouts:
            context += (
                f"\nTemplate layouts (ONLY use layout names and placeholder indices from this list):\n"
                f"{_smart_truncate_json(template_layouts, limit=8000)}\n"
            )
        if source_materials:
            context += (
                f"\nSource materials:\n"
                f"{_smart_truncate_json(source_materials, limit=8000)}\n"
            )
        if document_context:
            context += (
                f"\nRetrieved document context:\n"
                f"{_smart_truncate_json(document_context, limit=6000)}\n"
            )
    if learnings_context:
        context += learnings_context

    return await _invoke_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_PLAN_SYSTEM,
        user_content=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


# ── Edit Planning ─────────────────────────────────────────────────────────────

_EDIT_SYSTEM = """\
You are a presentation editing assistant. Given an existing deck structure and an edit
request, produce a structured list of operations that result in a polished, professional deck.

## TEMPLATE-GUIDED EDITS

When adding or updating slides, use template-guided `assignments` — not hardcoded x/y positions.
If `_template_layouts` is provided, choose layout names EXACTLY from that list and assign content
ONLY to placeholder indices that exist in the chosen layout. Never use indices 10, 11, 12
(footer/date/slide_number).

Output ONLY valid JSON:
{
  "action": "create_plan" | "edit",
  "steps": [...] — only for "create_plan"
  "operations": [
    {
      "action": "add_slide" | "remove_slide" | "move_slide" | "update_slide" | "update_text" | "replace_image" | "update_chart" | "update_table" | "restyle_deck",
      "slide_number": int — 1-indexed,
      ...
    }
  ]
}

## Operations

- **add_slide**: `after_slide` (int), `layout` (EXACT template layout name), `title`, `assignments` (idx → content)
- **remove_slide**: `slide_number`
- **move_slide**: `from` (int), `to` (int)
- **update_slide**: `slide_number`, `changes` (partial slide definition with `assignments`)
- **update_text**: `slide_number`, `shape_name` (string), `text` (string)
- **replace_image**: `slide_number`, `shape_name`, `new_image` (source definition)
- **update_chart**: `slide_number`, `shape_name`, `new_data` (chart data)
- **update_table**: `slide_number`, `shape_name`, `new_rows` (array of arrays)
- **restyle_deck**: `template` (string), `theme` (optional theme override)

## Rules

- Preserve existing content not mentioned in the edit request.
- When adding slides, use the same template and style as the existing deck.
- Keep edits surgical — only modify what the user asked for.
- For image replacements, prefer `source.kind: "from_asset"` for matching source materials
  before requesting `source.kind: "generate"`.
- Use ONLY system fonts: Calibri, Arial, Segoe UI, Helvetica, Verdana, Cambria,
  Times New Roman, Georgia, Consolas, Courier New.

## Document Context

You may receive `_source_materials` and `_document_context` with available visuals and text.
If insufficient, return:
{
  "action": "request_doc_context",
  "doc_request": {"intent": "docs.search_bundle" | "docs.read_bundle", "bundle_id": "...", ...}
}
Only reference `bundle_id`/`doc_id`/`asset_ref` values already surfaced to you.

## Example — Editing a deck

Existing deck: 5 slides. User says: "Add a slide about Q2 revenue after slide 3, and fix the typo on slide 1 title."

```json
{
  "action": "edit",
  "operations": [
    {
      "action": "update_text",
      "slide_number": 1,
      "shape_name": "Title 1",
      "text": "Quarterly Business Review"
    },
    {
      "action": "add_slide",
      "after_slide": 3,
      "layout": "Title and Content",
      "title": "Q2 Revenue Highlights",
      "assignments": {
        "0": {"type": "title", "text": "Q2 Revenue Highlights"},
        "1": {"type": "body", "items": [
          "Revenue grew 23% YoY to $4.2M",
          "Enterprise segment led with 31% growth",
          "New customer acquisition up 18%"
        ]}
      },
      "speaker_notes": "Emphasize enterprise growth — this is what the board cares about."
    }
  ]
}
```
"""


async def plan_edit(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    existing_structure: dict[str, Any],
    edit_request: str,
    source_materials: dict[str, Any] | None = None,
    document_context: dict[str, Any] | None = None,
    template_layouts: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Plan edits to an existing presentation."""
    context = (
        f"Existing deck structure:\n{json.dumps(existing_structure, indent=2)[:8000]}\n\n"
        f"Edit request: {edit_request}\n"
    )
    if template_layouts:
        context += (
            f"\nTemplate layouts (use ONLY these layout names and placeholder indices for new/updated slides):\n"
            f"{_smart_truncate_json(template_layouts, limit=8000)}\n"
        )
    if source_materials:
        context += (
            f"\nAvailable source materials for reuse:\n"
            f"{_smart_truncate_json(source_materials, limit=4000)}\n"
        )
    if document_context:
        context += (
            f"\nAdditional retrieved document context:\n"
            f"{_smart_truncate_json(document_context, limit=4000)}\n"
        )

    return await _invoke_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_EDIT_SYSTEM,
        user_content=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


# ── Vision Validation ─────────────────────────────────────────────────────────

_VALIDATE_SYSTEM = """\
You are a presentation quality reviewer. Examine the rendered slide image and assess
its quality with a STRICT professional bar. You are the last gate before this slide
ships to the user — if it's not good enough for a client meeting, it's not good enough.

Output ONLY valid JSON:
{
  "pass": true | false,
  "issues": ["list of specific, actionable issues — include what is wrong AND where on the slide"],
  "suggestion": "concrete fix instruction (e.g. 'increase title font to 28pt', 'move image 2 inches right')",
  "confidence": 0.0-1.0
}

## Evaluation Criteria (ordered by severity)

1. **Content visibility**: Is ANY text invisible, clipped, or unreadable? Is the slide blank
   or near-blank? FAIL immediately if so.
2. **Text contrast**: Can every text element be read against its background? Title text must
   have strong contrast. Body text must be easily legible. Specify which text fails.
3. **Layout balance**: Is content distributed across the slide, or is everything jammed into
   one corner with large dead space elsewhere? The slide should feel intentionally composed.
4. **Element overlap**: Do any elements overlap each other? Text over images without contrast?
   Shapes covering text?
5. **Hierarchy**: Is there a clear visual hierarchy? One dominant headline, then secondary
   content? Or does everything compete at the same visual weight?
6. **Font sizing**: Title >= 24pt equivalent, body >= 14pt equivalent. Tiny text = FAIL.
7. **Image quality**: Are images properly sized, not distorted, not pixelated, not cut off?
8. **Chart/table clarity**: Labels visible? Axes labeled? Headers distinguishable?
9. **Professional polish**: Does it look like a real presentation or like placeholder content?
   Would you show this to a client or executive?

## Judgment Guidelines
- PASS: Slide is clearly readable, balanced, professional-looking. The template background
  and decorative elements are visible. Content sits naturally within the template's zones.
- FAIL: Any of these: blank slide, invisible text, major overlap, everything crammed top-left
  with large dead space, text blends into background, no clear hierarchy, looks like an
  unstyled template dump, decorative template elements are covered/hidden by content.
- When in doubt, FAIL. It's better to repair and rebuild than to ship a bad slide.
- Be SPECIFIC in issues. Not "poor layout" — say "title is crammed into top-left 20% of slide,
  bottom 60% is empty dead space" or "body text is #333 on #1a1a2e background, nearly invisible".

## Examples

FAIL response:
```json
{
  "pass": false,
  "issues": [
    "Title text 'Q3 Results' is white (#fff) on a light green background — nearly invisible",
    "Body bullets are jammed into the top-left 25% of the slide, bottom-right 60% is empty",
    "No visual hierarchy — title and body text appear to be the same font size"
  ],
  "suggestion": "Change title color to dark (#333) for contrast, redistribute body content to use the full content zone, increase title font to at least 28pt",
  "confidence": 0.92
}
```

PASS response:
```json
{
  "pass": true,
  "issues": [],
  "suggestion": "",
  "confidence": 0.95
}
```
"""


def _detect_blank_or_low_contrast_slide(png_bytes: bytes) -> dict[str, Any] | None:
    """Fast heuristic guard for obviously blank or near-blank slides."""
    try:
        image = Image.open(BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return None

    stat = ImageStat.Stat(image)
    avg_brightness = sum(stat.mean) / 3.0
    avg_stddev = sum(stat.stddev) / 3.0

    if avg_brightness >= 248 and avg_stddev <= 3.0:
        return {
            "pass": False,
            "issues": [
                "Rendered slide appears blank or nearly blank.",
                "Slide has extremely low visual contrast.",
            ],
            "suggestion": (
                "Apply an explicit contrasting background and ensure text uses a "
                "readable color with visible hierarchy."
            ),
            "confidence": 0.99,
        }
    return None


async def validate_slide(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    slide_number: int,
    png_bytes: bytes,
    slide_plan: dict[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Validate a single rendered slide using vision."""
    if not png_bytes or len(png_bytes) < 100:
        return None

    heuristic_failure = _detect_blank_or_low_contrast_slide(png_bytes)
    if heuristic_failure is not None:
        return heuristic_failure

    import base64

    b64_image = base64.b64encode(png_bytes).decode("utf-8")

    plan_text = ""
    if slide_plan:
        plan_text = f"\nSlide plan:\n{json.dumps(slide_plan, indent=2)[:2000]}"

    user_content = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64_image}",
                "detail": "high",
            },
        },
        {
            "type": "text",
            "text": f"Slide {slide_number}. Validate this presentation slide for quality.{plan_text}",
        },
    ]

    return await _invoke_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_VALIDATE_SYSTEM,
        user_content=user_content,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


# ── Deck Repair ───────────────────────────────────────────────────────────────

_REPAIR_SYSTEM = """\
You are a presentation repair assistant. Given a slide plan, validation feedback,
and the template layout structure, produce a corrected version of the problematic slides.

Output ONLY valid JSON:
{
  "slides": [
    {
      "slide_number": int,
      "layout": "string — must be a layout name from the template",
      "title": "string",
      "assignments": { "idx": {...} },
      ...full slide definition for corrected slides only...
    }
  ]
}

## Rules
- Only include slides that need fixing (referenced in the issues)
- Keep the same layout unless the issue is specifically about layout choice
- Fix the SPECIFIC issues mentioned in the feedback — read each issue carefully
- Use ONLY placeholder indices from `_template_layouts` for the chosen layout
- If an index doesn't exist in the layout, content assigned to it is SILENTLY DROPPED
- Maintain professional quality and aesthetic standards

## Common Fix Recipes

| Issue Code | Cause | Fix |
|------------|-------|-----|
| OUT_OF_BOUNDS | Elements exceed 13.333 × 7.5 inches | Switch to `assignments` — template placeholders are always in-bounds |
| OVERLAP | Two elements share space | Use template `assignments` — placeholders are pre-balanced. Or reduce content count |
| HIGH_DENSITY | >100% of slide area used | Remove low-value bullets, split across 2 slides, or switch to a layout with fewer zones |
| Blank slide | Content on non-existent placeholder idx | Check `_template_layouts` for valid indices. Reassign to existing placeholders |
| Poor contrast | Text color ≈ background color | Set explicit text color with high contrast against template background |
| No hierarchy | Title and body same visual weight | Ensure title is in idx 0 (title placeholder), body in idx 1+ (content placeholder) |

## Content Reduction Strategy

When a slide is overcrowded (HIGH_DENSITY or too many overlapping elements):
1. First try: reduce bullet count to 4-5, shorten each to 5-8 words
2. If still overcrowded: remove the least important content element
3. Last resort: split into two slides with a clear narrative break
4. NEVER just shrink fonts to fit — that makes slides unreadable

## Font Constraint
Use ONLY system fonts: Calibri, Arial, Segoe UI, Helvetica, Verdana, Cambria,
Times New Roman, Georgia, Consolas, Courier New.

## Example

Input issue: "Slide 2: OVERLAP: 'S2:content' overlaps 'S2:image' (75% overlap); HIGH_DENSITY: 142% full"
Original slide 2 had both a long bullet list AND a large image competing for space.

Corrected output:
```json
{
  "slides": [
    {
      "slide_number": 2,
      "layout": "Title and Content",
      "title": "Key Findings",
      "assignments": {
        "0": {"type": "title", "text": "Key Findings"},
        "1": {"type": "body", "items": [
          "Revenue grew 23% year-over-year",
          "Customer retention at 94%",
          "Three new enterprise accounts signed",
          "Expansion into APAC on track"
        ]}
      },
      "speaker_notes": "Image moved to dedicated slide 3. Focus here on the numbers."
    }
  ]
}
```

The fix: removed the image (moved to its own slide), trimmed bullets to 4 concise points.
"""


async def repair_deck(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    slide_plans: list[dict[str, Any]],
    validation_results: list[dict[str, Any]],
    template_layouts: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Repair slide definitions based on validation feedback."""
    # Build detailed context with coordinates and specific issues per slide
    failed_slide_numbers = {
        item.get("slide_number") for item in validation_results if item.get("issues")
    }
    # Only send failed slides (full detail) + a summary of passing slides
    failed_plans = [
        s for s in slide_plans
        if s.get("slide_number") in failed_slide_numbers
    ]
    passing_summary = [
        {"slide_number": s.get("slide_number"), "layout": s.get("layout"), "title": s.get("title")}
        for s in slide_plans
        if s.get("slide_number") not in failed_slide_numbers
    ]

    context_parts = [
        f"## Slides that FAILED validation (fix these):\n{json.dumps(failed_plans, indent=2)[:10000]}",
        f"\n## Passing slides (do NOT modify):\n{json.dumps(passing_summary, indent=2)[:2000]}",
        f"\n## Validation issues to fix:\n{json.dumps(validation_results, indent=2)[:6000]}",
    ]
    if template_layouts:
        context_parts.append(
            f"\n## Template layouts (use ONLY these layout names and placeholder indices):\n"
            f"{json.dumps(template_layouts, indent=2)[:5000]}"
        )
    context_parts.append(
        "\nProduce corrected definitions ONLY for the failed slides. "
        "Use the exact placeholder indices from the template layouts above."
    )

    return await _invoke_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_REPAIR_SYSTEM,
        user_content="\n".join(context_parts),
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )
