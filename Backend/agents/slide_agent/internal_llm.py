"""Slide-agent internal LLM via LangChain OpenAI-compatible client.

Defaults to OpenAI gpt-5-mini for:
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


def _is_openrouter_base_url(base_url: str) -> bool:
    return "openrouter.ai" in (base_url or "").strip().lower()


def _usage_provider_name(cfg: SlideAgentConfig) -> str:
    if _is_openrouter_base_url(cfg.mimo_base_url):
        return "openrouter"
    return "openai_compatible"


def _supports_temperature(model_name: str) -> bool:
    normalized = (model_name or "").strip().lower()
    return not normalized.startswith("gpt-5")


def _effective_temperature(cfg: SlideAgentConfig) -> float:
    raw = max(0.0, float(cfg.mimo_temperature))
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
You are a presentation planning assistant. Given a request to create a slide deck,
produce a complete DeckPlan JSON.

## IMPORTANT: Template-Guided Design

You will receive a `_template_layouts` list in the input data showing the available
layouts and their placeholder zones. You MUST design within these zones.

Each layout has named placeholders with fixed positions. Your job is to:
1. Choose the right layout for each slide
2. Assign content to the correct placeholder by its role (title, body, content, image, chart)
3. NOT invent x/y coordinates — the template controls positioning

## Output JSON Structure

Output ONLY valid JSON:
{
  "action": "create_plan" | "generate",
  "steps": [...] — only for "create_plan"
  "deck": {
    "title": "string",
    "template": "template-name",
    "theme": { "primary_color": "#hex", "accent_color": "#hex", ... }
  },
  "slides": [
    {
      "slide_number": 1,
      "layout": "layout-name-from-template",
      "title": "string",
      "subtitle": "string (title_slide only)",
      "assignments": {
        "0": { "type": "title", "text": "Slide Title" },
        "1": { "type": "body", "items": ["Point 1", "Point 2"] },
        "2": { "type": "image", "source": {...} }
      },
      "content": { "type": "bullets", "items": [...] },
      "chart": { "chart_type": "...", "data": {...} },
      "table": { "headers": [...], "rows": [...] },
      "code_chart": { "code": "matplotlib code", "data": {...} },
      "flow_diagram": { "boxes": [...], "direction": "..." },
      "image": { "source": {...} },
      "background": { "type": "solid", "color": "#hex" },
      "speaker_notes": "string"
    }
  ]
}

## Layout Assignment Rules

Use the `assignments` field to map placeholder idx → content:
- `"0"` (title placeholder) → `{"type": "title", "text": "..."}`
- `"1"` (body/content placeholder) → `{"type": "body", "items": [...]}` or `{"type": "chart", ...}` or `{"type": "image", ...}`
- For two_content layouts: idx `"1"` = left, idx `"2"` = right

If `assignments` is not provided, use the legacy `content`/`image`/`chart` fields
and the builder will map them to the best-fit placeholder.

## Available Layouts
The `_template_layouts` field in your input shows all available layouts with their
placeholder zones. Use ONLY layouts from this list.

Common layouts:
- "Title Slide" (index 0): Title + Subtitle
- "Title and Content" (index 1): Title + single content area
- "Two Content" (index 3): Title + left content + right content
- "Blank" (index 6): No fixed placeholders — use for custom content

## Content Types in Placeholders
- body: bullets, numbered, paragraph
- chart: native pptx chart
- code_chart: matplotlib/seaborn code (rendered as image)
- image: generated or existing image
- table: styled data table
- flow_diagram: connected boxes with arrows

## Aesthetic Bar

Every slide must look intentional, polished, and presentation-ready. Avoid layouts that feel
like an unstyled template dump.

- Use strong hierarchy: one dominant headline, one clear secondary element, then restrained support.
- Use whitespace deliberately. Empty space should feel premium, not accidental.
- Avoid top-left-heavy compositions unless the template layout clearly demands it.
- For a simple intro slide, prefer one memorable title plus one crisp subtitle or proof line over a dense bullet stack.
- Keep supporting copy compact and punchy. If a slide can say less, say less.

## Font Constraint

ALWAYS use system fonts only. These render correctly on any machine with Office/PowerPoint:
- **Sans-serif**: Calibri (default), Arial, Segoe UI, Helvetica, Verdana
- **Serif**: Cambria, Times New Roman, Georgia
- **Monospace**: Consolas, Courier New

Do NOT use Google Fonts, custom fonts, or fonts not listed above.
If the user requests a custom font, substitute the closest system font from the list.
Do NOT use emoji, dingbats, checkmark glyphs, or icon-like Unicode symbols in titles
or bullets. Use plain text instead.

## Rules
- Each slide MUST have a layout that exists in the template
- Each slide MUST have a title (except blank)
- For a one-slide intro/cover slide, prefer a title-slide-compatible layout and keep
  the hierarchy simple: a large title, a clear subtitle, then at most one short
  supporting line or a few concise bullets only if the user explicitly asks.
- For a one-slide pitch/intro deck, center or intentionally balance the composition
  within the template zones; do not cram everything into the top-left with large dead space.
- Keep bullets concise (5-8 words per bullet, 3-6 per slide)
- Charts should have clear titles and labeled series
- Tables should have headers and 3-10 data rows
- Include speaker_notes for every slide
- Use "create_plan" for complex multi-deck requests
- Use "generate" for single-deck requests

## Code Charts (code_chart)
Use when native charts can't express what you need (waterfall, treemap, heatmap, seaborn).
The code creates a matplotlib figure. Has access to `DATA` dict and `OUTPUT_DIR`.

## Flow Diagrams (flow_diagram)
For connected boxes showing processes, pipelines, architectures.
boxes: [{"text": "Step", "shape": "rounded_rectangle", "fill": "#hex"}]

## Source Materials
You may receive `_source_materials` in the input data. It contains:
- `documents`: uploaded/parsed source docs
- `visual_assets`: reusable figures, page images, or uploaded images

Each source document may include:
- `bundle_id`: canonical parsed-bundle id
- `preview_excerpt`: a compact markdown/text preview from the document
- `top_sections`: the most useful section headings already discovered locally

When an existing source visual matches the slide, prefer reusing it instead of generating a new one.
To reuse a source visual, set:
{
  "source": {
    "kind": "from_asset",
    "asset_ref": "asset id from _source_materials.visual_assets"
  }
}

Do not invent asset_ref values. Only use values present in `_source_materials.visual_assets`.

## Additional Document Context
You may also receive `_document_context` in the input data. This is compact extra context
already fetched from the docs specialist through the orchestrator. Use it directly instead of
guessing details from the source document.

## When You Need More Document Context
If the local `_source_materials` and `_document_context` are insufficient, you may ask for a
bounded orchestrator-mediated docs lookup by returning:
{
  "action": "request_doc_context",
  "doc_request": {
    "intent": "docs.search_bundle" | "docs.read_bundle" | "docs.fetch_asset" | "docs.reinspect_asset",
    "bundle_id": "bundle_...",
    "...": "intent-specific fields"
  }
}

Rules for `request_doc_context`:
- Keep the request compact and specific.
- Only use `bundle_id`, `doc_id`, and `asset_ref` values already surfaced in `_source_materials`.
- Use `docs.search_bundle` to find where information lives.
- Use `docs.read_bundle` to pull a focused excerpt or section.
- Use `docs.fetch_asset` only when you need exact asset metadata or the asset was not already surfaced enough.
- Use `docs.reinspect_asset` only when a visual needs a deeper explanation.
- Do NOT return `request_doc_context` if the existing local source materials are already enough.
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
            "Prefer the `pitch-deck` template unless the source materials clearly demand another template."
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
                f"{json.dumps(extra_data, indent=2)[:2500]}\n"
            )
        if available_templates:
            context += (
                f"\nAvailable templates:\n"
                f"{json.dumps(available_templates, indent=2)[:2500]}\n"
            )
        if template_layouts:
            context += (
                f"\nTemplate layouts:\n"
                f"{json.dumps(template_layouts, indent=2)[:5000]}\n"
            )
        if source_materials:
            context += (
                f"\nSource materials:\n"
                f"{json.dumps(source_materials, indent=2)[:5000]}\n"
            )
        if document_context:
            context += (
                f"\nRetrieved document context:\n"
                f"{json.dumps(document_context, indent=2)[:4000]}\n"
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
request, produce a structured list of operations.

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

Operation-specific fields:
- add_slide: after_slide (int), layout (string), title, content (same as create)
- remove_slide: slide_number
- move_slide: from (int), to (int)
- update_slide: slide_number, changes (partial slide definition)
- update_text: slide_number, shape_name (string), text (string)
- replace_image: slide_number, shape_name, new_image (source definition)
- update_chart: slide_number, shape_name, new_data (chart data)
- update_table: slide_number, shape_name, new_rows (array of arrays)
- restyle_deck: template (string), theme (optional theme override)

Preserve existing content not mentioned in the edit request.

If source materials are provided, prefer `new_image.source = {"kind": "from_asset", "asset_ref": "..."}` for matching figures/page images before requesting a generated image.

You may also receive `document_context`, which contains compact excerpts or search hits already
retrieved from the docs specialist.

If the current source materials and document context are insufficient, you may return:
{
  "action": "request_doc_context",
  "doc_request": {
    "intent": "docs.search_bundle" | "docs.read_bundle" | "docs.fetch_asset" | "docs.reinspect_asset",
    "bundle_id": "bundle_...",
    "...": "intent-specific fields"
  }
}

Keep document requests compact and only reference `bundle_id`, `doc_id`, or `asset_ref`
values that were already surfaced to you.
"""


async def plan_edit(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    existing_structure: dict[str, Any],
    edit_request: str,
    source_materials: dict[str, Any] | None = None,
    document_context: dict[str, Any] | None = None,
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
    if source_materials:
        context += (
            f"\nAvailable source materials for reuse:\n"
            f"{json.dumps(source_materials, indent=2)[:4000]}\n"
        )
    if document_context:
        context += (
            f"\nAdditional retrieved document context:\n"
            f"{json.dumps(document_context, indent=2)[:4000]}\n"
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
its quality.

Output ONLY valid JSON:
{
  "pass": true | false,
  "issues": ["list of specific issues found"],
  "suggestion": "brief actionable suggestion to fix issues (empty if pass)",
  "confidence": 0.0-1.0
}

Check for:
1. **Text readability**: Are all text elements clearly visible? Font size adequate?
2. **Image quality**: Are images properly placed, not distorted, not cut off?
3. **Layout balance**: Good whitespace distribution? No overcrowding?
4. **Color contrast**: Text readable against background?
5. **Chart clarity**: Chart labels visible? Data bars/lines readable?
6. **Table readability**: Headers distinguishable? Cell text legible?
7. **Consistency**: Does it match professional presentation standards?
8. **Cover-slide quality**: If this is an intro/title slide, does it have strong title hierarchy,
   visible subtitle/supporting line, and a centered or intentionally balanced composition?
9. **Visual intent**: Does the slide feel designed, or does it look like placeholder content was
   dumped into the top-left of a template?

Pass if the slide is clearly readable and professional-looking.
Fail if the slide appears blank, text blends into the background, images overlap,
or the layout is confusing or visually weak.
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
You are a presentation repair assistant. Given a slide plan and validation feedback,
produce a corrected version of the problematic slides.

Output ONLY valid JSON:
{
  "slides": [
    {
      "slide_number": int,
      "layout": "string",
      "title": "string",
      ...full slide definition for corrected slides only...
    }
  ]
}

Rules:
- Only include slides that need fixing (referenced in the issues)
- Keep the same layout unless the issue is about layout
- Fix specific issues mentioned in the feedback
- Maintain professional quality
- Strengthen hierarchy, balance, and contrast when the feedback suggests the slide feels weak,
  top-heavy, blank, or like placeholder output
"""


async def repair_deck(
    *,
    cfg: SlideAgentConfig,
    http_client: httpx.AsyncClient,
    slide_plans: list[dict[str, Any]],
    validation_results: list[dict[str, Any]],
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Repair slide definitions based on validation feedback."""
    context = (
        f"Slide plans:\n{json.dumps(slide_plans, indent=2)[:8000]}\n\n"
        f"Validation results:\n{json.dumps(validation_results, indent=2)[:4000]}\n"
        f"Produce corrected definitions for slides with issues."
    )

    return await _invoke_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_REPAIR_SYSTEM,
        user_content=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )
