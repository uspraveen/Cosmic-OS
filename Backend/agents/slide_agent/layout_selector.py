"""Layout selector — cosmic-slides-2.

Stage 2 of the pipeline: maps content plan slides to template slide numbers.

Given:
  - A content plan (output of content_planner.py)
  - A template catalog (output of template_cataloger.py)
  - An optional max_slides cap

Produces a complete build spec: every deck slide has a template_slide_number,
its full content, and a layout_reasoning field explaining the choice.
The slide builder consumes this directly — no further planning needed.

How it works
────────────
1. Load catalog → get collage image path + slim slide descriptions
2. Send to Kimi: collage image (vision) + catalog text + full content plan
3. Kimi assigns each content slide the best-matching template slide number
4. If plan_slide_count > max_slides, Kimi merges adjacent low-content slides
5. Output: build_spec.json ready for the slide builder

Output per slide in build spec
───────────────────────────────
  deck_slide_number       int    position in the output deck (1-indexed)
  template_slide_number   int    which template slide to clone
  template_archetype      str    archetype of the chosen template slide
  title                   str    slide title
  content_role            str    from content plan
  full_content            list   content blocks from content plan
  speaker_notes           str    from content plan
  layout_reasoning        str    LLM explanation of why this template slide was chosen

Usage
─────
  python layout_selector.py plan.json templates/Startup_pitch_deck.pptx
  python layout_selector.py plan.json templates/Startup_pitch_deck.pptx --max-slides 10
  python layout_selector.py plan.json templates/Startup_pitch_deck.pptx --out build_spec.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from copy import deepcopy
from pathlib import Path

import httpx

from llm_client import env_int
from template_cataloger import catalog_template, load_catalog, LAYOUT_ARCHETYPES

# ── Config ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent

MODEL_BASE_URL: str = os.getenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference/v1").rstrip("/")
MODEL_API_KEY: str  = os.getenv("MODEL_API_KEY", "")
MODEL_NAME: str     = os.getenv("MODEL_NAME", "accounts/fireworks/models/glm-5p3-flash")
MAX_SLIDES_DEFAULT  = env_int("MAX_SLIDES", 15)

logger = logging.getLogger(__name__)

# ── Content-role → archetype affinity ─────────────────────────────────────────
# Soft hints — the LLM uses these as guidance, not hard rules.

ROLE_ARCHETYPE_AFFINITY: dict[str, list[str]] = {
    "opening":       ["cover"],
    "agenda":        ["title-body", "grid", "four-column", "three-column"],
    "section_break": ["section-break"],
    "narrative":     ["title-body", "two-column"],
    "data_story":    ["chart-focus", "big-stat", "two-column"],
    "comparison":    ["comparison", "two-column", "three-column"],
    "highlight":     ["big-stat", "quote", "full-bleed-image"],
    "timeline":      ["timeline"],
    "steps":         ["three-column", "four-column", "grid", "timeline"],
    "visual":        ["full-bleed-image", "cover"],
    "people":        ["people-showcase", "grid", "three-column"],
    "closing":       ["closing", "cover", "big-stat"],
}

# ── Prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a presentation designer selecting template slide layouts for a content plan.

You will receive:
1. An image — a collage of all available template slides with numbered amber badges
2. A catalog — text descriptions of each template slide's visual layout and archetype
3. A content plan — the slides that need to be built, with their content and roles

Your job: for each content slide, choose the template slide number that best matches
visually and structurally. Think about:

MATCHING RULES
──────────────
- content_role maps naturally to layout archetypes (see affinity hints in the catalog)
- full_content blocks must fit the template's visible regions:
    • comparison content → comparison or two-column layout
    • 3-bullet points → three-column or title-body
    • single big stat → big-stat layout
    • image_prompt block → full-bleed-image or cover with image area
    • chart block → chart-focus or two-column
- Visual variety: avoid using the same template slide number more than twice
  unless the template has very few slides and no better option exists
- Deck coherence: the opening and closing slides should feel like bookends
  (matching style/color), section-breaks should look structurally different from content

SLIDE COUNT
───────────
- If the plan has more slides than max_slides, merge adjacent slides whose
  content is light or thematically adjacent. Preserve all key content — don't drop data.
- If the plan has fewer slides than max_slides, use all of them as-is.

OUTPUT FORMAT
─────────────
Return ONLY valid JSON — no markdown fences, no commentary:

{
  "deck_title": "...",
  "deck_theme": "...",
  "total_slides": <int>,
  "slides": [
    {
      "deck_slide_number": 1,
      "template_slide_number": <int — must be a valid slide number from the catalog>,
      "template_archetype": "<archetype of the chosen template slide>",
      "title": "<final slide title>",
      "content_role": "<role>",
      "full_content": [ <content blocks — carry over from plan, merge if needed> ],
      "speaker_notes": "<speaker notes>",
      "layout_reasoning": "<1-2 sentences: why this template slide fits this content>"
    }
  ]
}
"""

_USER = """\
TEMPLATE CATALOG
────────────────
Template: {template_name} ({slide_count} slides)

{catalog_text}

AFFINITY HINTS (content_role → preferred archetypes)
─────────────────────────────────────────────────────
{affinity_text}

CONTENT PLAN
────────────
Deck title: {deck_title}
Theme: {deck_theme}
Slides in plan: {plan_slide_count}
Max slides allowed: {max_slides}

{plan_text}

Now look at the collage image above and assign each content slide to the best template slide.
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _catalog_to_text(catalog: dict) -> str:
    lines = []
    for s in catalog["slides"]:
        lines.append(
            f"Slide {s['slide_number']:>2} | {s['layout_archetype']:<20} | "
            f"{s['visual_description']}"
        )
    return "\n".join(lines)


def _affinity_to_text() -> str:
    lines = []
    for role, archetypes in ROLE_ARCHETYPE_AFFINITY.items():
        lines.append(f"  {role:<16} → {', '.join(archetypes)}")
    return "\n".join(lines)


def _plan_to_text(plan: dict) -> str:
    lines = []
    for s in plan["slides"]:
        lines.append(f"--- Plan Slide {s['slide_number']} ---")
        lines.append(f"Title       : {s['title']}")
        lines.append(f"Role        : {s['content_role']}")
        lines.append(f"Content     : {json.dumps(s['full_content'], ensure_ascii=False)}")
        lines.append(f"Notes       : {s['speaker_notes']}")
        lines.append("")
    return "\n".join(lines)


def _collage_b64(catalog: dict) -> str | None:
    collage_path = Path(catalog.get("collage_path", ""))
    if collage_path.exists():
        return base64.b64encode(collage_path.read_bytes()).decode("utf-8")
    return None


def _stream_response(payload: dict, headers: dict, client: httpx.Client) -> str:
    url = f"{MODEL_BASE_URL}/chat/completions"
    raw = ""
    with client.stream("POST", url, json=payload, headers=headers, timeout=180) as resp:
        if resp.status_code >= 400:
            raise ValueError(f"API {resp.status_code}: {resp.read().decode()[:400]}")
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
                raw += piece
            except Exception:
                continue
    return raw.strip()


def _parse_json_response(raw: str) -> dict:
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines()
                        if not l.strip().startswith("```")).strip()
    if "{" in raw and not raw.startswith("{"):
        raw = raw[raw.index("{"):]
    if "}" in raw and not raw.endswith("}"):
        raw = raw[:raw.rindex("}") + 1]
    return json.loads(raw)


def _collapse_slides_to_cap(result: dict, max_slides: int) -> dict:
    """Hard-cap slide count by merging adjacent slides as a last-resort fallback.

    This only runs if the model ignored the requested max_slides. It preserves
    ordering and content by grouping contiguous slides into at most max_slides buckets.
    """
    slides = result.get("slides") or []
    if max_slides <= 0 or len(slides) <= max_slides:
        return result

    total = len(slides)
    collapsed: list[dict] = []
    for bucket_index in range(max_slides):
        start = (bucket_index * total) // max_slides
        end = ((bucket_index + 1) * total) // max_slides
        group = slides[start:end]
        if not group:
            continue

        if len(group) == 1:
            merged = deepcopy(group[0])
        else:
            merged = deepcopy(group[0])
            merged["full_content"] = []
            merged["speaker_notes"] = " ".join(
                str(item.get("speaker_notes") or "").strip()
                for item in group
                if str(item.get("speaker_notes") or "").strip()
            ).strip()
            for item in group:
                merged["full_content"].extend(deepcopy(item.get("full_content") or []))
            if max_slides == 1 and result.get("deck_title"):
                merged["title"] = str(result["deck_title"])
            merged["layout_reasoning"] = (
                f"{merged.get('layout_reasoning', '').strip()} "
                f"Merged {len(group)} adjacent planned slides to enforce max_slides={max_slides}."
            ).strip()

        merged["deck_slide_number"] = len(collapsed) + 1
        collapsed.append(merged)

    result = deepcopy(result)
    result["slides"] = collapsed
    result["total_slides"] = len(collapsed)
    return result


# ── Core ───────────────────────────────────────────────────────────────────────

def select_layouts(
    plan: dict,
    catalog: dict,
    *,
    max_slides: int | None = None,
) -> dict:
    """Map content plan slides to template slide numbers.

    Args:
        plan:       Output dict from content_planner.plan_content().
        catalog:    Output dict from template_cataloger.catalog_template().
        max_slides: Hard cap on number of output slides. Defaults to MAX_SLIDES env var.

    Returns:
        Build spec dict with slides ready for the slide builder.
    """
    if not MODEL_API_KEY:
        raise ValueError("MODEL_API_KEY is not set in .env")

    max_slides = max_slides or MAX_SLIDES_DEFAULT
    collage_b64 = _collage_b64(catalog)
    if not collage_b64:
        raise FileNotFoundError(
            f"Collage not found at '{catalog.get('collage_path')}'. "
            "Re-run template_cataloger.py to regenerate."
        )

    valid_slide_numbers = {s["slide_number"] for s in catalog["slides"]}

    user_text = _USER.format(
        template_name   = catalog["template_name"],
        slide_count     = catalog["slide_count"],
        catalog_text    = _catalog_to_text(catalog),
        affinity_text   = _affinity_to_text(),
        deck_title      = plan.get("deck_title", ""),
        deck_theme      = plan.get("deck_theme", ""),
        plan_slide_count= len(plan["slides"]),
        max_slides      = max_slides,
        plan_text       = _plan_to_text(plan),
    )

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{collage_b64}"},
                },
                {"type": "text", "text": user_text},
            ],
        },
    ]

    payload = {
        "model":       MODEL_NAME,
        "messages":    messages,
        "temperature": 0.3,
        "max_tokens":  16384,
        "stream":      True,
    }
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "layout_selector: %d plan slides → max %d output slides, template '%s' (%d slides)",
        len(plan["slides"]), max_slides, catalog["template_name"], catalog["slide_count"],
    )

    for attempt in range(3):
        try:
            with httpx.Client() as client:
                raw = _stream_response(payload, headers, client)

            if not raw:
                raise ValueError("Empty response")

            result = _parse_json_response(raw)

            if "slides" not in result or not result["slides"]:
                raise ValueError("Response missing 'slides' list")

            # Validate and clamp template_slide_number to valid range
            for slide in result["slides"]:
                tsn = slide.get("template_slide_number")
                if tsn not in valid_slide_numbers:
                    logger.warning(
                        "deck slide %d: invalid template_slide_number %s — "
                        "clamping to slide 1",
                        slide.get("deck_slide_number"), tsn,
                    )
                    slide["template_slide_number"] = 1

            # Attach template visual description for builder reference
            arch_map = {s["slide_number"]: s for s in catalog["slides"]}
            for slide in result["slides"]:
                tsn = slide["template_slide_number"]
                if tsn in arch_map:
                    slide["template_visual_description"] = arch_map[tsn]["visual_description"]
                    if "template_archetype" not in slide:
                        slide["template_archetype"] = arch_map[tsn]["layout_archetype"]

            # Attach template metadata to top-level
            result["template_name"] = catalog["template_name"]
            result["template_path"] = catalog["template_path"]
            if len(result["slides"]) > max_slides:
                logger.warning(
                    "layout_selector: model returned %d slides > max_slides=%d; enforcing hard cap",
                    len(result["slides"]), max_slides,
                )
                result = _collapse_slides_to_cap(result, max_slides)

            logger.info(
                "layout_selector: mapped %d slides → done",
                len(result["slides"]),
            )
            return result

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("layout_selector attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2)

    raise RuntimeError("Layout selector failed after 3 attempts.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Select template layouts for a content plan."
    )
    parser.add_argument("plan",     type=Path, help="Path to plan.json (content_planner output).")
    parser.add_argument("template", type=Path, help="Path to the .pptx template file.")
    parser.add_argument("--max-slides", "-n", type=int, default=None,
                        help="Maximum number of slides in output deck.")
    parser.add_argument("--out", "-o", type=Path, default=None,
                        help="Write build spec JSON to this file (default: stdout).")
    parser.add_argument("--force-catalog", action="store_true",
                        help="Force re-catalog the template even if cached.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    # Load plan
    if not args.plan.exists():
        print(f"Error: plan file not found: {args.plan}", file=sys.stderr)
        sys.exit(1)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))

    # Load or generate catalog
    catalog = load_catalog(args.template.resolve())
    if catalog is None or args.force_catalog:
        logger.info("Generating catalog for '%s' …", args.template.name)
        catalog = catalog_template(args.template)

    try:
        build_spec = select_layouts(plan, catalog, max_slides=args.max_slides)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_json = json.dumps(build_spec, indent=2, ensure_ascii=False)

    if args.out:
        args.out.write_text(output_json, encoding="utf-8")
        print(f"Build spec written to {args.out}")
        print(f"  Deck     : {build_spec.get('deck_title', '?')}")
        print(f"  Slides   : {len(build_spec['slides'])}")
        print(f"  Template : {build_spec.get('template_name', '?')}")
        print()
        for s in build_spec["slides"]:
            print(f"  [{s['deck_slide_number']:02d}] -> template slide {s['template_slide_number']:>2}"
                  f"  ({s['template_archetype']:<20})  {s['title']}")
    else:
        print(output_json)


if __name__ == "__main__":
    _cli()
