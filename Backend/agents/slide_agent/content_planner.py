"""Content planner — Stage 1 of the cosmic-slides-2 pipeline.

Given any of:
  - A bare topic          ("Climate change impacts on agriculture")
  - A short brief/summary ("Q3 results were strong, revenue up 24%, ...")
  - Dense source content  (paragraphs, notes, research excerpts)

...produces a structured slide-by-slide content plan as JSON.

The LLM figures out which kind of input it received and behaves accordingly:
  - Bare topic   → researches, elaborates, invents sensible content
  - Brief/summary → expands into full slide-ready content
  - Full content  → structures and condenses without losing key points

Output per slide
────────────────
  title         str        headline for the slide
  content_role  str        semantic role (see CONTENT_ROLES below)
  full_content  list[dict] one or more content blocks (see BLOCK TYPES)
  speaker_notes str        what the presenter would say (2–4 sentences)

Standalone usage
────────────────
  python content_planner.py "your topic or content here"
  python content_planner.py "your topic" --slides 8
  python content_planner.py --file notes.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from agent_tools import ToolContext, planner_tools
from llm_client import env_int, parse_json_response
from llm_tool_harness import run_json_stage_with_tools

# ── Config ────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

MODEL_BASE_URL: str = os.getenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference/v1").rstrip("/")
MODEL_API_KEY: str  = os.getenv("MODEL_API_KEY", "")
MODEL_NAME: str     = os.getenv("MODEL_NAME", "accounts/fireworks/models/glm-5p3-flash")
# Deck JSON is large; low limits yield truncated JSON and json.loads failures.
MODEL_MAX_TOKENS: int = env_int("MODEL_MAX_TOKENS", 16384)

logger = logging.getLogger(__name__)

# ── Content vocabulary ────────────────────────────────────────────────────────

CONTENT_ROLES = {
    "opening":       "Title / cover slide — topic, subtitle, presenter",
    "agenda":        "Overview of what the deck covers",
    "section_break": "Visual divider between major sections",
    "narrative":     "Prose-driven story or background context",
    "data_story":    "Statistics, metrics, or chart-based insight",
    "comparison":    "Two or more options / sides set against each other",
    "highlight":     "Single powerful stat, quote, or key takeaway",
    "timeline":      "Chronological sequence of events or phases",
    "steps":         "Numbered process or how-to sequence",
    "visual":        "Image-dominant slide — concept conveyed through a photo/illustration",
    "people":        "Team, speakers, or profile showcase",
    "closing":       "Summary, call-to-action, or final thought",
}

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert presentation strategist and content architect.

You are planning slides for a real human who will stand in front of a room and
present this deck. Their reputation rides on it. Your default must be the
version they would be proud to show — not the version that fills the slide.

─────────────────────────────────────────────────────────────────────────────
DESIGN PHILOSOPHY  (these are the defaults you must fight against. The user's
brief, when explicit, OVERRIDES every default below. If the brief asks for
maximalist, gradient-heavy, emoji-rich, dense, or otherwise unusual style,
honor it.)
─────────────────────────────────────────────────────────────────────────────
1. SLIDES ARE NOT DOCUMENTS. A slide is a visual aid for a speaker. Every
   slide must answer: "what is the speaker pointing at right now?" If the
   answer is "they are reading the slide to me," the slide has failed. Lean
   toward fewer words, larger type, more whitespace.

2. EVERY SLIDE HAS EXACTLY ONE JOB. Articulate the slide's job in one
   sentence (in `speaker_notes` or implicitly through structure). If you
   cannot, split the slide or cut it.

3. TITLE IS A COMPLETE THOUGHT, NOT A CATEGORY. The title alone should be
   skimmable as a deck summary.
     Bad:  "Results"
     Good: "Refusal collapses across every model we tested"
     Bad:  "Methodology"
     Good: "We measured five models on three benchmarks"

4. AUDIENCE CALIBRATION FIRST. Before drafting, identify three things and
   embed them in `deck_theme`:
     • Who is the audience? (peers / executives / beginners / mixed)
     • What do they already know vs. need explained?
     • What should they do or believe afterward?
   A "beginners" deck on a topic should share almost no slides with a
   "peers" deck on the same topic — different vocabulary, examples,
   evidence. Do not generate a generic deck and adjust the tone.

5. DECK ARC. Default shape for an explanatory deck:
   title → hook (why this matters now) → thesis in one sentence →
   minimal background the audience needs → the thing itself, simple→complex →
   evidence (real data) → counterpoint or limitation (this builds trust) →
   implications → conclusion. Adjust to length but keep the shape.

6. DATA DISCIPLINE. Never invent statistics. If you do not have a real
   number, either omit the figure or use a clearly labeled estimate.
   When reproducing numbers from a source, keep the precision the source
   used; do not silently round. If a source is contradictory, say so on
   the slide rather than picking one. When a figure, chart, or quote comes
   from a known source, include that source (e.g. a "source" field on stat
   blocks, or the source named in the chart title/context) so the deck can
   attribute it — a deck that cites its numbers reads as credible; one
   that doesn't reads as invented.

Your job is to turn raw input into a structured, compelling slide deck plan.
The input might be any of three things — you must detect which and act accordingly:

INPUT TYPE A — BARE TOPIC (a short phrase or sentence with no real detail)
  → You must invent intelligent, well-researched, realistic content.
    Use your knowledge to populate each slide with genuine facts, figures,
    examples, and narrative. Do NOT use placeholder text like "insert data here".

INPUT TYPE B — BRIEF OR SUMMARY (a paragraph or two with key points)
  → Expand each point into full slide-ready content. Add context, supporting
    detail, and structure. Fill gaps with plausible, relevant material.

INPUT TYPE C — FULL CONTENT (dense paragraphs, notes, research excerpts)
  → Extract, structure, and condense. Preserve all key data and insights.
    Reorganise into a logical narrative arc. Do not invent new facts.

─────────────────────────────────────────────────────────────────────────────
OUTPUT FORMAT
─────────────────────────────────────────────────────────────────────────────
Return ONLY a valid JSON object, no markdown fences, no commentary.

{
  "input_type_detected": "topic|brief|content",
  "deck_title": "...",
  "deck_theme": "one sentence describing the overall tone/narrative arc",
  "slides": [
    {
      "slide_number": 1,
      "title": "...",
      "content_role": "<one of the roles listed below>",
      "full_content": [ <one or more content blocks — see schema below> ],
      "speaker_notes": "What the presenter says. 2-4 sentences. Conversational."
    }
  ]
}

─────────────────────────────────────────────────────────────────────────────
CONTENT ROLES  (pick the single best fit)
─────────────────────────────────────────────────────────────────────────────
opening        Title / cover — topic, subtitle, presenter context
agenda         Overview of what the deck covers (use sparingly, only if useful)
section_break  Visual divider between major sections
narrative      Prose-driven story, background, or context
data_story     Statistics, metrics, or chart-based insight
comparison     Two or more options/approaches set side by side
highlight      Single powerful stat, quote, or key takeaway (one big idea only)
timeline       Chronological sequence of events or phases
steps          Numbered process or how-to sequence
visual         Concept best conveyed through an image or illustration
people         Team, speakers, or profile showcase
closing        Summary, call-to-action, or final thought

─────────────────────────────────────────────────────────────────────────────
CONTENT BLOCK SCHEMA  (full_content is an array of these)
─────────────────────────────────────────────────────────────────────────────
Most slides have 1–2 blocks. Do not over-stuff a slide.

{ "type": "bullets",
  "heading": "optional subheading",
  "items": ["point one", "point two", "point three"] }   ← 3–6 items max

{ "type": "stat",
  "value": "47%",
  "label": "of companies report...",
  "context": "one sentence explaining significance",
  "source": "IDC Worldwide Tracker, 2025 — omit if genuinely unknown" }

{ "type": "comparison",
  "left":  { "label": "Option A", "points": ["...", "..."] },
  "right": { "label": "Option B", "points": ["...", "..."] } }

{ "type": "quote",
  "text": "The actual quote text.",
  "attribution": "Name, Title" }

{ "type": "timeline",
  "items": [
    { "label": "2020", "description": "Event or milestone" },
    { "label": "2022", "description": "Next milestone" }
  ] }

{ "type": "steps",
  "items": [
    { "step": 1, "title": "Step name", "description": "Brief detail" },
    { "step": 2, "title": "Step name", "description": "Brief detail" }
  ] }

{ "type": "chart",
  "chart_type": "bar|line|pie|area",
  "title": "Chart heading",
  "x_label": "X-axis label — required, never omit",
  "y_label": "Y-axis label — required, never omit",
  "series": [
    { "name": "Series A", "data": [["Label1", 42], ["Label2", 67]] }
  ] }
  ← All data rows must be populated with real, specific numbers.
  ← x_label and y_label are REQUIRED — never leave them empty or "optional".
  ← If a slide needs two separate charts, add two chart dicts to full_content.
    Each chart dict is independent with its own title, axes, and data.

{ "type": "image_prompt",
  "description": "Detailed, specific description for image generation — subject, setting, style, lighting",
  "mood": "professional|dramatic|warm|minimal|energetic",
  "composition": "full-bleed|inset|left-half|right-half" }
  ← composition hints at how the image will be placed on the slide layout.

{ "type": "text",
  "body": "A short paragraph of prose (2–4 sentences max)." }

─────────────────────────────────────────────────────────────────────────────
CHARTS AND IMAGES — WHEN TO USE THEM
─────────────────────────────────────────────────────────────────────────────
Charts and images are powerful but must be earned. Apply these rules strictly:

CHARTS — use only when ALL of these are true:
  • The slide is making a data-driven point (trends, comparisons, distributions)
  • You have specific, realistic numbers to populate every data row
  • A visual representation genuinely reveals something bullets cannot
  DO NOT use a chart just to decorate a qualitative point.
  DO NOT invent vague round numbers (10, 20, 30) — use realistic figures.
  A 10-slide deck should have at most 2–3 chart slides.

IMAGES - use only when the visual itself carries meaning:
  • content_role is "visual", "opening", "closing", or "highlight"
  • The concept is emotional, spatial, or physical (a city, a product, a person)
  • A strong image would genuinely replace 50 words of explanation
  • If the user explicitly asks for an image-led slide, full-bleed background, hero background, generated visual, or photo background on a specific slide, include an image_prompt for that slide unless the slide is primarily a dense chart/data slide.
  DO NOT add image_prompt to every slide - narrative and data slides rarely need one.
  DO NOT use images on slides that already have a chart.
  The description must be specific and detailed enough to generate the image
  (subject, environment, style, lighting, perspective).

─────────────────────────────────────────────────────────────────────────────
QUALITY RULES
─────────────────────────────────────────────────────────────────────────────
1. The slides must tell a STORY — beginning, middle, end. Not a random list.
2. Every stat, fact, or figure you use must be realistic and defensible.
   Never fabricate a number to fill a chart row. If you don't have it,
   change the slide.
3. Titles are punchy headlines (≤8 words) AND complete thoughts, not topic
   labels. Prefer "X dropped 47% in three weeks" over "Performance Decline".
4. Bullets are short (≤12 words each), parallel in structure, 3–5 per slide.
   If a point needs more than 12 words, it is a paragraph and probably
   belongs in `text` or `speaker_notes`, not bullets.
5. speaker_notes must sound like a real human talking, not reading bullets.
   They are what the speaker says; the slide is what they show.
6. Never use filler phrases: "In today's fast-paced world", "In conclusion",
   "As we can see", "It is important to note".
7. The opening slide must have a compelling subtitle, not just the topic repeated.
8. The closing slide must have a concrete takeaway or call to action.
9. Do NOT add an agenda slide unless the deck has 8+ slides.
10. Across the whole deck: charts on at most 30% of slides, images on at
    most 40%. The majority of slides carry their weight through words
    and structure alone.
11. SENTINEL TEST. Before finalising, ask: could this slide appear unchanged
    in a generic SaaS pitch deck on a different topic? If yes, it is too
    generic. Specificity is what makes a slide good — concrete numbers,
    named entities, real examples.
"""

# ── Core planner ──────────────────────────────────────────────────────────────

def _build_user_message(raw_input: str, num_slides: int | None) -> str:
    lines = ["Plan a slide deck for the following input:\n"]
    lines.append(raw_input.strip())
    if num_slides:
        lines.append(f"\nTarget slide count: {num_slides} slides (±1 is fine).")
    else:
        lines.append(
            "\nChoose an appropriate slide count. "
            "For a topic/brief: typically 6–10 slides. "
            "For dense content: as many as needed to cover it properly."
        )
    return "\n".join(lines)


# Fireworks rejects non-streaming requests when max_tokens > 4096.
_FIREWORKS_MAX_NONSTREAM_TOKENS = 4096


def _accumulate_chat_stream(response: httpx.Response) -> tuple[str, str | None]:
    """Read an OpenAI-style SSE chat completion stream. Returns (content, finish_reason)."""
    parts: list[str] = []
    finish_reason: str | None = None
    for line in response.iter_lines():
        if line is None:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr
    return "".join(parts).strip(), finish_reason


def _call_planner_llm(messages: list[dict], *, timeout: float, temperature: float) -> tuple[str, str | None]:
    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": MODEL_MAX_TOKENS,
    }

    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json",
    }

    url = f"{MODEL_BASE_URL}/chat/completions"
    finish_reason: str | None = None
    raw_text: str

    with httpx.Client(timeout=timeout) as client:
        if MODEL_MAX_TOKENS > _FIREWORKS_MAX_NONSTREAM_TOKENS:
            stream_payload = {**payload, "stream": True}
            with client.stream("POST", url, json=stream_payload, headers=headers) as response:
                if response.status_code >= 400:
                    # On a streaming response .text raises ResponseNotRead and
                    # masks the real API error — read the body first.
                    response.read()
                    raise ValueError(f"API error {response.status_code}: {response.text}")
                response.raise_for_status()
                raw_text, finish_reason = _accumulate_chat_stream(response)
        else:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                raise ValueError(f"API error {response.status_code}: {response.text}")
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            finish_reason = choice.get("finish_reason")
            msg = choice.get("message") or {}
            raw_text = (msg.get("content") or "").strip()
    return raw_text, finish_reason


def plan_content(
    raw_input: str,
    *,
    num_slides: int | None = None,
    timeout: float = 120.0,
) -> dict:
    """Call the LLM and return the parsed content plan dict.

    Args:
        raw_input:  The topic, brief, or source content.
        num_slides: Optional target slide count.
        timeout:    HTTP timeout in seconds.

    Returns:
        Parsed JSON dict with keys: input_type_detected, deck_title,
        deck_theme, slides.

    Raises:
        ValueError: If the LLM response cannot be parsed as valid JSON.
        httpx.HTTPError: On network or API errors.
    """
    if not MODEL_API_KEY:
        raise ValueError(
            "MODEL_API_KEY is not set. Add it to cosmic-slides-2/.env"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": _build_user_message(raw_input, num_slides)},
    ]

    logger.info("content_planner: calling %s with model %s", MODEL_BASE_URL, MODEL_NAME)

    finish_reason: str | None = None
    raw_text: str = ""
    tools = planner_tools()
    if tools:
        try:
            result, _tool_assets = run_json_stage_with_tools(
                messages,
                call_llm=lambda msgs, temperature: _call_planner_llm(msgs, timeout=timeout, temperature=temperature)[0],
                parse_json=parse_json_response,
                is_final_result=lambda payload: isinstance(payload.get("slides"), list),
                tools=tools,
                tool_context=ToolContext(output_dir=_HERE / "output"),
                final_hint='{"input_type_detected":"topic|brief|content","deck_title":"...","deck_theme":"...","slides":[...]}',
                temperature=0.6,
                max_tool_rounds=3,
                allow_asset_ids=False,
            )
            _validate_plan(result)
            return result
        except Exception as exc:
            logger.warning("content_planner: tool-assisted planning failed, falling back to direct plan: %s", exc)

    raw_text, finish_reason = _call_planner_llm(messages, timeout=timeout, temperature=0.6)

    # Strip markdown fences if the model wrapped the JSON anyway
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        hint = ""
        if finish_reason == "length":
            hint = (
                " Output hit max_tokens and was cut mid-JSON. "
                f"Raise MODEL_MAX_TOKENS (currently {MODEL_MAX_TOKENS}) in .env."
            )
        tail = raw_text[-400:] if len(raw_text) > 400 else raw_text
        raise ValueError(
            f"LLM returned invalid or incomplete JSON.{hint}\n"
            f"finish_reason={finish_reason!r}\n"
            f"First 500 chars:\n{raw_text[:500]}\n"
            f"Last 400 chars:\n{tail}"
        ) from exc

    _validate_plan(result)
    return result


def _validate_plan(plan: dict) -> None:
    """Light structural validation — raises ValueError on bad shape."""
    if "slides" not in plan or not isinstance(plan["slides"], list):
        raise ValueError("Plan missing 'slides' list.")
    for i, slide in enumerate(plan["slides"]):
        for field in ("title", "content_role", "full_content", "speaker_notes"):
            if field not in slide:
                raise ValueError(f"Slide {i+1} missing field '{field}'.")
        if slide["content_role"] not in CONTENT_ROLES:
            logger.warning(
                "Slide %d has unknown content_role '%s'", i + 1, slide["content_role"]
            )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Plan slide deck content from a topic, brief, or raw notes."
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Topic, brief, or content text. Omit if using --file.",
    )
    parser.add_argument(
        "--file", "-f",
        type=Path,
        help="Read input from a text file instead.",
    )
    parser.add_argument(
        "--slides", "-n",
        type=int,
        default=None,
        help="Target number of slides (optional).",
    )
    parser.add_argument(
        "--out", "-o",
        type=Path,
        default=None,
        help="Write JSON output to this file (default: print to stdout).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    if args.file:
        raw_input = args.file.read_text(encoding="utf-8")
    elif args.input:
        raw_input = args.input
    else:
        # Read from stdin
        print("Paste your input, then press Ctrl+D (or Ctrl+Z on Windows):")
        raw_input = sys.stdin.read()

    if not raw_input.strip():
        print("Error: no input provided.", file=sys.stderr)
        sys.exit(1)

    try:
        plan = plan_content(raw_input, num_slides=args.slides)
    except (ValueError, httpx.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_json = json.dumps(plan, indent=2, ensure_ascii=False)

    if args.out:
        args.out.write_text(output_json, encoding="utf-8")
        print(f"Plan written to {args.out}")
        print(f"  Slides: {len(plan['slides'])}")
        print(f"  Input type detected: {plan.get('input_type_detected', '?')}")
        print(f"  Deck title: {plan.get('deck_title', '?')}")
    else:
        print(output_json)


if __name__ == "__main__":
    _cli()
