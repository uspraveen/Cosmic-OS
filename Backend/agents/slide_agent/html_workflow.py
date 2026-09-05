"""HTML-first slide workflow integrated alongside the native template builder."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from asset_manager import resolve_icon, resolve_photo
from content_planner import plan_content
from llm_client import call_llm_json
from ooxml_validator import validate_pptx
from pptx_writer import build_pptx_from_images
from template_cataloger import LIBREOFFICE_PATH, _run, create_numbered_collage

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

HTML_MAX_REPAIR_ROUNDS = int(os.getenv("HTML_MAX_REPAIR_ROUNDS", "1"))
HTML_RENDER_TIMEOUT_MS = int(os.getenv("HTML_RENDER_TIMEOUT_MS", "45000"))
HTML_VIEWPORT_WIDTH = int(os.getenv("HTML_VIEWPORT_WIDTH", "1440"))
HTML_VIEWPORT_HEIGHT = int(os.getenv("HTML_VIEWPORT_HEIGHT", "900"))
HTML_DEVICE_SCALE = float(os.getenv("HTML_DEVICE_SCALE", "1.5"))
MAX_SLIDES_DEFAULT = int(os.getenv("MAX_SLIDES", "50"))

logger = logging.getLogger(__name__)

BASE_HTML_CSS = """
:root {
  --bg: #111111;
  --fg: #f5f1e8;
  --muted: #b4aca3;
  --accent: #6ca0ff;
  --accent-2: #8ce0d4;
  --panel: rgba(255,255,255,0.08);
  --panel-strong: rgba(255,255,255,0.16);
  --line: rgba(255,255,255,0.18);
  --shadow: 0 24px 60px rgba(0,0,0,0.28);
  --font-display: Bahnschrift, Arial, sans-serif;
  --font-body: Georgia, 'Times New Roman', serif;
  --font-accent: Verdana, Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  width: 1280px;
  height: 720px;
  overflow: hidden;
  background: #0d0d0f;
}
body {
  font-family: var(--font-body);
}
.slide {
  position: relative;
  width: 1280px;
  height: 720px;
  overflow: hidden;
  background: var(--bg);
  color: var(--fg);
  isolation: isolate;
}
.abs { position: absolute; }
.fill { position: absolute; inset: 0; width: 100%; height: 100%; }
.photo-cover { width: 100%; height: 100%; object-fit: cover; }
.photo-contain { width: 100%; height: 100%; object-fit: contain; }
.overlay-soft { background: linear-gradient(180deg, rgba(0,0,0,0.06), rgba(0,0,0,0.32)); }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 26px;
  box-shadow: var(--shadow);
}
.glass {
  background: color-mix(in srgb, var(--panel-strong) 70%, transparent);
  border: 1px solid color-mix(in srgb, var(--fg) 14%, transparent);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
}
.eyebrow {
  font-family: var(--font-accent);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-size: 15px;
}
.hero {
  font-family: var(--font-display);
  font-weight: 800;
  line-height: 0.92;
  letter-spacing: -0.04em;
}
.copy {
  font-family: var(--font-body);
  line-height: 1.26;
}
.stat {
  font-family: var(--font-display);
  font-weight: 800;
  line-height: 0.9;
  letter-spacing: -0.05em;
}
.muted { color: var(--muted); }
.accent { color: var(--accent); }
.accent-2 { color: var(--accent-2); }
ul.clean {
  margin: 0;
  padding-left: 1.15em;
}
ul.clean li + li { margin-top: 10px; }
.divider {
  height: 1px;
  background: var(--line);
}
img.icon {
  width: 56px;
  height: 56px;
  object-fit: contain;
}
"""

THEME_SYSTEM = """\
You are defining a coherent visual system for a presentation deck that will be rendered in HTML/CSS.

You are picking a design system once, at deck creation time, and locking it in.
Every subsequent slide will use these tokens unchanged. A deck that introduces
new colors, new fonts, or new visual idioms slide-to-slide reads as 12 different
decks stitched together. Lock it down.

Return ONLY JSON:
{
  "theme_name": "...",
  "design_rationale": "...",
  "css_variables": {
    "bg": "#111111",
    "fg": "#f6f2ea",
    "muted": "#aaa39a",
    "accent": "#7db3ff",
    "accent_2": "#82e2d2",
    "panel": "rgba(255,255,255,0.08)",
    "panel_strong": "rgba(255,255,255,0.16)",
    "line": "rgba(255,255,255,0.16)"
  },
  "font_stacks": {
    "display": "Bahnschrift, Arial, sans-serif",
    "body": "Georgia, 'Times New Roman', serif",
    "accent": "Verdana, Arial, sans-serif"
  },
  "deck_guidelines": [
    "short design rule 1",
    "short design rule 2"
  ]
}

CONSTRAINTS
- Use render-safe fonts only: Bahnschrift, Georgia, Verdana, Arial, Calibri.
- Prefer strong palettes with real contrast (body text ≥ 4.5:1 against bg).
- One display font. One body font. One accent font (may equal display). No more.
- At most TWO accent colors total (`accent` and optional `accent_2`). Beyond
  that the deck looks like a colour-test card.
- PALETTE DOMINANCE. Build every slide on BACKGROUND → PRIMARY → ACCENT:
  the background carries 60–70% of the visual weight, the primary tone does
  the structural work, and the accent touches ~5–10% of the slide on the
  single most important element per slide. Never give colors equal weight.
  When you need more differentiation, use tints and shades of the primary —
  never new hues.
- DERIVE THE PALETTE FROM THE SUBJECT. The colors should feel designed for
  THIS topic — its industry, place, brand, or mood. Test: if the palette
  would work unchanged on an unrelated deck, it is not specific enough.
  Do not default to blue, and do not default to cream/beige backgrounds —
  white or a real brand tone beats a warm-neutral shrug.
- SANDWICH STRUCTURE. Default to dark backgrounds for the cover and closing
  slides and light backgrounds for content slides — or commit to dark
  throughout for a premium feel. Note the choice in deck_guidelines so every
  slide follows it.
- ONE SIGNATURE MOTIF. Pick a single repeatable visual idea (oversized
  numbers, rounded image frames, a consistent card treatment…) and state it
  in deck_guidelines. Repeated on every slide, it makes the deck feel
  authored.
- If the brief explicitly asks for a different style (light/airy, brutalist,
  editorial, brand-coloured, retro, maximalist…), that style WINS over every
  default below.
- Keep the theme usable across all slides, not just the cover.

DEFAULTS TO FIGHT  (only when the brief gives you no opinion)
- Avoid full-bleed slide-wide gradients on every slide. Subtle accent gradients
  on cards, dividers, or hero panels are fine.
- Avoid drop shadows on body text. Shadows on cards/panels for depth are fine.
- Avoid emoji as a structural element. Emoji are not icons.
- Avoid pastel "AI-generated" palettes (lavender + mint + peach). Pick a
  palette with conviction.

In `deck_guidelines`, write 3–6 SHORT rules the per-slide designer will follow.
Examples of good guidelines:
- "All titles use the display font at 64–96px, left-aligned."
- "Use accent only for the single most important word/number per slide."
- "Cards use panel with 1px line border; never drop-shadow the card."
- "Body bullets stay ≤ 12 words; long thoughts go to speaker_notes."
"""

THEME_USER = """\
Build a deck-wide HTML/CSS theme for this presentation.

Deck title: {deck_title}
Deck theme: {deck_theme}
Description: {description}

Slides:
{slides_text}
"""

SLIDE_SYSTEM = """\
You are a world-class presentation designer creating ONE slide as HTML/CSS for a 16:9 deck.

You are designing for a real human who will stand in a room and present this.
Their reputation is on the line. Your default is the slide they would be proud
to show — not the slide that fills every pixel.

Return ONLY JSON:
{
  "label": "short slide label",
  "design_notes": "what you are doing visually",
  "custom_css": "...",
  "body_html": "...",
  "speaker_notes": "optional revised notes or empty string"
}

CORE PHILOSOPHY
- The slide is a visual aid for a speaker. It should answer "what is the
  speaker pointing at right now?" If the slide reads itself to the audience,
  it has failed.
- Every slide has ONE job. Title, headline finding, supporting visual,
  attribution. If you can't articulate the slide's job in one sentence,
  the slide is doing too much.
- Design is hierarchy, not decoration. Before adding any visual element,
  ask: does this clarify what is most important, or just fill space?
  Gradients, glass, shadows, animations, ornaments — these are usually
  dressing on a confused message. Strengthen the hierarchy first.

FOCAL POINT AND SPACE
- Every slide needs a visual element — an image, chart, diagram, shape, or
  oversized figure. A text-only slide is a failed slide.
- Every slide has ONE clear visual focal point that dominates the
  composition; everything else is subordinate to it.
- No large blank areas. Resolve empty regions with CONTENT — bigger type, a
  wider visual, a promoted pull quote or caption — never with filler bars,
  strips, rules, or flat color blocks whose only purpose is occupying space.
- Vary the rhythm across the deck: alternate text-driven, image-driven, and
  data-driven slides. Do not reuse one layout twice in a row.

LAYOUT AND STRUCTURE
- The slide root already exists as <section class="slide">. You must only
  generate inner HTML and CSS.
- Design for a 1280×720 canvas. NOTHING extends past these boundaries.
- Use the THEME's CSS variables and font stacks. Never introduce a new
  color, gradient, or font that isn't in the theme tokens. The theme is
  locked — your job is to compose with it, not redecorate.
- The ONE sanctioned exception: if the deck guidelines define a dark/light
  inversion for cover, section-break, or closing slides, use the same
  palette roles inverted (background ↔ text colors) — never new hues.
- Use local asset paths exactly as provided; do not invent URLs.
- No scripts, no iframes, no external stylesheets, no external fonts.
- Curate ruthlessly. If the FULL CONTENT JSON has six bullets, surface the
  three most important ones; let the speaker say the rest.

TYPE AND READABILITY
- Title type: 64–96px on the display font, left-aligned by default.
- Body type: 16–24px on the body font, 1.4–1.6 line-height.
- Body text is left-aligned. Centered body text reads as a poster, not
  a slide. Titles MAY be centered when the layout calls for it.
- Bullets: ≤ 12 words each. If a bullet runs longer, shorten or move
  to speaker_notes.
- Body text contrast against its immediate background must be ≥ 4.5:1.
  If you put text over an image, add a panel/scrim or you have failed.
- The smallest text on any slide (captions, footers, source lines) stays
  legible: never below 14px, and keep it high-contrast against its background.
- `font-variant-numeric: tabular-nums` for any column of numbers.
- Use `white-space: pre` (or `pre-wrap`) for code blocks and verse;
  otherwise the line breaks are lost.

DEFAULTS TO FIGHT  (the brief or DECK GUIDELINES override these — when the
brief explicitly requests one of these, treat it as a feature, not a smell.)
- No emoji in titles, headers, bullets, or buttons. Emoji are not icons.
- No drop-shadows on body text.
- No more than 2 accent colors used in this slide.
- NEVER a decorative color bar, accent stripe, sidebar strip, or single-side
  edge border — not along a slide edge, not along a card edge, and never the
  same treatment on consecutive slides. Group with background tints,
  hairline separators (1px, between items only), or spacing instead. This is
  the single most recognizable "AI deck" tell.
- NEVER a decorative underline beneath a title. Whitespace carries hierarchy.
- No card/tile grids (2×2, 3×2, 4-up) more than rarely — at most one grid in
  five content slides, never on consecutive slides, and only when the
  content is genuinely peer items. Otherwise use columns, timelines, or a
  focal composition.
- No center-aligned multi-line body paragraphs.
- No three-column "feature card" layouts with circular icons unless the
  content is genuinely three parallel features. Don't fake it for symmetry.
- No phrases of the form "In today's fast-paced world", "Welcome to…",
  "Let's begin", "Unlocking the power of…".

ATTRIBUTION
- If FULL CONTENT gives a source for a figure, chart, or quote, render it as
  a small muted source line (12–16px, muted color) at the bottom of the
  slide. Never present an invented number as sourced; label estimates as
  "est." or "illustrative".

USING THE PROVIDED ASSETS
- Photos work for the OPENING, CLOSING, HIGHLIGHT, and VISUAL roles, or
  as a hero panel on a section break. They rarely belong on data slides.
- Icons must be paired with a label they reinforce. A row of unlabeled
  icons is decoration, not communication.
- If an asset doesn't earn its place, omit it. A blank quadrant beats a
  generic stock photo.

DELIVERABLE-GRADE BEHAVIOR
- The layout should feel intentional and presentation-grade, not like a
  webpage screenshot.
- You MAY use absolute positioning, gradients, panels, glass effects, and
  overlapping text when it's deliberate and readable.
- Before returning, mentally do a STRIP-ORNAMENT pass: remove any element
  that isn't carrying meaning. The slide should feel sparser after the
  pass, not poorer.
"""

SLIDE_USER = """\
THEME JSON:
{theme_json}

DECK GUIDELINES:
{guidelines_text}

SLIDE NUMBER: {slide_number}
TITLE: {title}
ROLE: {content_role}
LAYOUT REASONING: {layout_reasoning}
FULL CONTENT:
{content_json}
SPEAKER NOTES:
{speaker_notes}

AVAILABLE LOCAL ASSETS:
{assets_text}

PREVIOUS SLIDE LABELS:
{previous_labels}

Create the strongest possible slide for this content. Return JSON only.
"""

VALIDATION_SYSTEM = """\
You are reviewing a rendered HTML slide for presentation quality.

Be a hostile audience member. Imagine the speaker showing this slide on stage —
would you cringe, scroll past, or lean in? Your job is to flag what would
embarrass the presenter, not to enforce a style guide.

Distinguish deliberate overlap from accidental collisions. A design can use
large stacked display text, glass cards, or text over imagery if it remains
legible and intentional. Don't penalize bold choices that work.

Return ONLY JSON:
{
  "verdict": "pass" or "fail",
  "score": 1-10,
  "summary": "short summary",
  "issues": [
    {
      "severity": "low|medium|high",
      "description": "what is wrong",
      "fix_hint": "how to fix it"
    }
  ]
}

VERDICT POLICY
- "fail" only on HIGH-severity issues that would visibly embarrass the speaker:
  text cut off the slide, body text unreadable against its background,
  major content overlapping, hero asset broken/missing, the title or key
  number truncated.
- LOW or MEDIUM issues do not auto-fail. Note them so a future repair pass
  can tighten the slide if the budget allows.
- Do not flag legitimate creative choices as issues just because they break
  a "default rule." If the brief implied maximalist/cinematic/branded, the
  rendered slide should reflect that.

FIDELITY CHECKLIST  (walk through these before scoring)
- Boundaries: does any element extend past 1280×720?
- Title: complete and not truncated by container width or ellipsis?
- Body text contrast: ≥ 4.5:1 against its immediate background?
- Tabular columns: numbers right-aligned, decimal points lined up?
- Code blocks/verse: line breaks preserved (not collapsed to one line)?
- Footer / page-number / logo elements: not covered by content?
- Charts: axis labels present and legible? legend doesn't overlap data?
- Images: not stretched, not pixelated, behind a scrim if text sits on them?
- Focal point: is there one dominant element, or does everything compete?
- Blank areas: large empty regions left unresolved, or filler decoration
  parked in them instead of real content?
- Attribution: figures/quotes that carry a source rendered with a source line?

AI-SLIDE TELLS  (flag as MEDIUM; fail only if they dominate the deck's look)
- Decorative color bars, accent stripes, sidebar strips, single-side edge
  borders on cards, or an underline beneath a title
- The same card/tile grid layout on consecutive slides
- Three or more competing accent colors
- Content-free decoration occupying space where content should be

LIKELY-GENERIC SMELLS  (note as LOW issues unless they actively harm the
slide; do NOT auto-fail)
- Emoji used as a structural icon in title/headers
- Drop-shadows on body text
- Center-aligned multi-line body paragraphs
- "In today's fast-paced world…" / "Welcome to…" / generic SaaS clichés
- Stock-photo "diverse team smiling at laptop" aesthetic where the topic
  doesn't call for it
- Default-blue or beige palettes that carry no hint of the subject
"""

VALIDATION_USER = """\
Slide number: {slide_number}
Slide title: {title}
Design notes: {design_notes}
HTML summary:
{html_summary}

Review the rendered slide and decide if it is presentation-grade.
"""

REPAIR_SYSTEM = """\
You are repairing one HTML/CSS slide that failed visual review.

Return ONLY JSON:
{
  "label": "short slide label",
  "design_notes": "what changed",
  "custom_css": "...",
  "body_html": "...",
  "speaker_notes": "optional revised notes or empty string"
}

REPAIR PHILOSOPHY
- Do localized repairs. Keep the good parts. Do not redesign what is working.
- When in doubt: less content, bigger type, sharper claim. Most failures
  come from too much, not too little.
- Honor the locked theme — never introduce a new color, new font stack, or
  ornament outside the deck guidelines. The fix lives within the existing
  design system.
- Address the listed issues SPECIFICALLY. If validation flagged a
  truncation, fix the truncation; do not rewrite the whole slide because
  you wanted to.
- When validation flags an AI-slide tell (decorative stripes, edge bars,
  title underlines, filler blocks), REMOVE the ornament — replace it with
  whitespace, a 1px hairline, or a background tint. Never swap one decoration
  for another.
- A repaired slide should feel like the same slide — only the broken
  parts changed. If the original got the layout right but the title was
  cut off, the repair shortens the title; it does not pick a new layout.
"""

REPAIR_USER = """\
THEME JSON:
{theme_json}

SLIDE NUMBER: {slide_number}
TITLE: {title}
ROLE: {content_role}
FULL CONTENT:
{content_json}
AVAILABLE LOCAL ASSETS:
{assets_text}

VALIDATION SUMMARY:
{validation_summary}

ISSUES:
{issues_text}

CURRENT DESIGN JSON:
{current_design_json}
"""

THEME_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "theme_name": {"type": "string"},
        "design_rationale": {"type": "string"},
        "css_variables": {
            "type": "object",
            "properties": {
                "bg": {"type": "string"},
                "fg": {"type": "string"},
                "muted": {"type": "string"},
                "accent": {"type": "string"},
                "accent_2": {"type": "string"},
                "panel": {"type": "string"},
                "panel_strong": {"type": "string"},
                "line": {"type": "string"},
            },
            "required": ["bg", "fg", "muted", "accent", "accent_2", "panel", "panel_strong", "line"],
        },
        "font_stacks": {
            "type": "object",
            "properties": {
                "display": {"type": "string"},
                "body": {"type": "string"},
                "accent": {"type": "string"},
            },
            "required": ["display", "body", "accent"],
        },
        "deck_guidelines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["theme_name", "design_rationale", "css_variables", "font_stacks", "deck_guidelines"],
}

SLIDE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "design_notes": {"type": "string"},
        "custom_css": {"type": "string"},
        "body_html": {"type": "string"},
        "speaker_notes": {"type": "string"},
    },
    "required": ["label", "design_notes", "custom_css", "body_html", "speaker_notes"],
}

VALIDATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "score": {"type": "integer"},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "description": {"type": "string"},
                    "fix_hint": {"type": "string"},
                },
                "required": ["severity", "description", "fix_hint"],
            },
        },
    },
    "required": ["verdict", "score", "summary", "issues"],
}


def _plan_slides_text(plan: dict) -> str:
    lines = []
    for slide in plan.get("slides", []):
        lines.append(
            f"- Slide {slide['slide_number']}: {slide['title']} ({slide['content_role']}) "
            f"{json.dumps(slide['full_content'], ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _safe_name(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (text or "slide"))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "slide"


def _copy_asset(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _safe_resolve_photo(description: str) -> Path | None:
    try:
        return resolve_photo(description)
    except Exception as exc:  # defensive: never let asset lookup kill the deck
        logger.warning("html_workflow: resolve_photo('%s') failed: %s", description, exc)
        return None


def _safe_resolve_icon(query: str, *, color: str = "#FFFFFF") -> Path | None:
    try:
        return resolve_icon(query, color=color)
    except Exception as exc:
        logger.warning("html_workflow: resolve_icon('%s') failed: %s", query, exc)
        return None


def _prepare_slide_assets(slide: dict, slide_dir: Path) -> list[dict]:
    assets_dir = slide_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict] = []
    photo_idx = 0
    icon_idx = 0

    for block in slide.get("full_content", []):
        block_type = block.get("type")
        if block_type == "image_prompt":
            path = _safe_resolve_photo(str(block.get("description") or ""))
            if path:
                photo_idx += 1
                ext = path.suffix.lower() or ".jpg"
                dst = _copy_asset(path, assets_dir / f"photo-{photo_idx}{ext}")
                prepared.append({
                    "id": f"photo_{photo_idx}",
                    "type": "photo",
                    "path": f"assets/{dst.name}",
                    "hint": str(block.get("description") or ""),
                })
        elif block_type == "stat":
            query = str(block.get("label") or slide.get("title") or "").strip()
            if query:
                path = _safe_resolve_icon(query, color="#FFFFFF")
                if path:
                    icon_idx += 1
                    dst = _copy_asset(path, assets_dir / f"icon-{icon_idx}.png")
                    prepared.append({
                        "id": f"icon_{icon_idx}",
                        "type": "icon",
                        "path": f"assets/{dst.name}",
                        "hint": query,
                    })
        elif block_type == "comparison":
            for label in (block.get("left", {}) or {}).get("label", ""), (block.get("right", {}) or {}).get("label", ""):
                query = str(label or "").strip()
                if not query or icon_idx >= 4:
                    continue
                path = _safe_resolve_icon(query, color="#FFFFFF")
                if path:
                    icon_idx += 1
                    dst = _copy_asset(path, assets_dir / f"icon-{icon_idx}.png")
                    prepared.append({
                        "id": f"icon_{icon_idx}",
                        "type": "icon",
                        "path": f"assets/{dst.name}",
                        "hint": query,
                    })
        elif block_type == "steps":
            for item in block.get("items", [])[:3]:
                query = str(item.get("title") or "").strip()
                if not query or icon_idx >= 4:
                    continue
                path = _safe_resolve_icon(query, color="#FFFFFF")
                if path:
                    icon_idx += 1
                    dst = _copy_asset(path, assets_dir / f"icon-{icon_idx}.png")
                    prepared.append({
                        "id": f"icon_{icon_idx}",
                        "type": "icon",
                        "path": f"assets/{dst.name}",
                        "hint": query,
                    })
    return prepared


def _assets_text(assets: list[dict]) -> str:
    if not assets:
        return "- none"
    lines = []
    for asset in assets:
        lines.append(f"- {asset['id']} | {asset['type']} | {asset['path']} | hint: {asset['hint']}")
    return "\n".join(lines)


def _theme_css(theme: dict) -> str:
    css_vars = theme.get("css_variables", {}) or {}
    fonts = theme.get("font_stacks", {}) or {}
    pairs = {
        "--bg": css_vars.get("bg"),
        "--fg": css_vars.get("fg"),
        "--muted": css_vars.get("muted"),
        "--accent": css_vars.get("accent"),
        "--accent-2": css_vars.get("accent_2"),
        "--panel": css_vars.get("panel"),
        "--panel-strong": css_vars.get("panel_strong"),
        "--line": css_vars.get("line"),
        "--font-display": fonts.get("display"),
        "--font-body": fonts.get("body"),
        "--font-accent": fonts.get("accent"),
    }
    rules = [f"  {name}: {value};" for name, value in pairs.items() if value]
    return ":root {\n" + "\n".join(rules) + "\n}\n"


def _write_slide_html(slide_dir: Path, theme: dict, design: dict) -> Path:
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=1280, initial-scale=1.0" />
  <style>
{BASE_HTML_CSS}
{_theme_css(theme)}
{design.get("custom_css", "")}
  </style>
</head>
<body>
  <section class="slide">
{design.get("body_html", "")}
  </section>
</body>
</html>
"""
    path = slide_dir / "slide.html"
    path.write_text(html, encoding="utf-8")
    return path


def _deck_previous_labels(manifest: list[dict]) -> str:
    if not manifest:
        return "- none"
    return "\n".join(f"- Slide {item['slide_number']}: {item.get('label', '')}" for item in manifest[-3:])


def _design_slide(theme: dict, slide: dict, assets: list[dict], previous_labels: str) -> dict:
    messages = [
        {"role": "system", "content": SLIDE_SYSTEM},
        {"role": "user", "content": SLIDE_USER.format(
            theme_json=json.dumps(theme, indent=2, ensure_ascii=False),
            guidelines_text="\n".join(f"- {item}" for item in theme.get("deck_guidelines", []) or []),
            slide_number=slide["slide_number"],
            title=slide["title"],
            content_role=slide["content_role"],
            layout_reasoning=slide.get("layout_reasoning", ""),
            content_json=json.dumps(slide["full_content"], indent=2, ensure_ascii=False),
            speaker_notes=slide.get("speaker_notes", ""),
            assets_text=_assets_text(assets),
            previous_labels=previous_labels,
        )},
    ]
    result = call_llm_json(messages, temperature=0.25, response_schema=SLIDE_JSON_SCHEMA)
    result.setdefault("label", slide["title"])
    result.setdefault("design_notes", "")
    result.setdefault("custom_css", "")
    result.setdefault("body_html", "")
    result.setdefault("speaker_notes", slide.get("speaker_notes", ""))
    return result


def _validate_slide_render(png_path: Path, slide: dict, design: dict) -> dict:
    b64 = base64.b64encode(png_path.read_bytes()).decode("utf-8")
    messages = [
        {"role": "system", "content": VALIDATION_SYSTEM},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": VALIDATION_USER.format(
                slide_number=slide["slide_number"],
                title=slide["title"],
                design_notes=design.get("design_notes", ""),
                html_summary=f"label={design.get('label', '')}\ncss={design.get('custom_css', '')[:900]}\nhtml={design.get('body_html', '')[:1500]}",
            )},
        ]},
    ]
    result = call_llm_json(messages, temperature=0.1, response_schema=VALIDATION_JSON_SCHEMA)
    result["slide_number"] = slide["slide_number"]
    result.setdefault("summary", "")
    result.setdefault("issues", [])
    return result


def _repair_slide(theme: dict, slide: dict, assets: list[dict], design: dict, validation: dict) -> dict:
    issues = validation.get("issues", []) or []
    issues_text = "\n".join(
        f"- [{item.get('severity', 'medium')}] {item.get('description', '')} | hint: {item.get('fix_hint', '')}"
        for item in issues
    ) or "- General visual improvement needed."
    messages = [
        {"role": "system", "content": REPAIR_SYSTEM},
        {"role": "user", "content": REPAIR_USER.format(
            theme_json=json.dumps(theme, indent=2, ensure_ascii=False),
            slide_number=slide["slide_number"],
            title=slide["title"],
            content_role=slide["content_role"],
            content_json=json.dumps(slide["full_content"], indent=2, ensure_ascii=False),
            assets_text=_assets_text(assets),
            validation_summary=validation.get("summary", ""),
            issues_text=issues_text,
            current_design_json=json.dumps(design, indent=2, ensure_ascii=False),
        )},
    ]
    repaired = call_llm_json(messages, temperature=0.2, response_schema=SLIDE_JSON_SCHEMA)
    repaired.setdefault("label", design.get("label", slide["title"]))
    repaired.setdefault("design_notes", design.get("design_notes", ""))
    repaired.setdefault("custom_css", design.get("custom_css", ""))
    repaired.setdefault("body_html", design.get("body_html", ""))
    repaired.setdefault("speaker_notes", design.get("speaker_notes", slide.get("speaker_notes", "")))
    return repaired


def _launch_browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    errors: list[str] = []
    for kwargs in (
        {"headless": True},
        {"headless": True, "channel": "msedge"},
        {"headless": True, "channel": "chrome"},
    ):
        try:
            browser = pw.chromium.launch(**kwargs)
            return pw, browser
        except Exception as exc:
            errors.append(str(exc))
    pw.stop()
    raise RuntimeError("Could not launch a Chromium browser via Playwright: " + " | ".join(errors))


def render_slide_html_files(
    manifest: list[dict],
    rendered_dir: Path,
    *,
    canvas_width: int | None = None,
    canvas_height: int | None = None,
) -> list[Path]:
    """Render each slide HTML to PNG using Playwright.

    canvas_width/canvas_height override the browser viewport for decks whose
    design canvas differs from the default (e.g. 4:3 templates in the native
    workflow); the screenshot still targets the .slide element itself.
    """
    viewport_w = max(HTML_VIEWPORT_WIDTH, int(canvas_width or 0))
    viewport_h = max(HTML_VIEWPORT_HEIGHT, int(canvas_height or 0))
    rendered_dir.mkdir(parents=True, exist_ok=True)
    pw, browser = _launch_browser()
    slide_pngs: list[Path] = []
    try:
        page = browser.new_page(
            viewport={"width": viewport_w, "height": viewport_h},
            device_scale_factor=HTML_DEVICE_SCALE,
        )
        for item in manifest:
            html_path = Path(item["html_path"])
            png_path = rendered_dir / f"slide-{item['slide_number']:02d}.png"
            page.goto(html_path.as_uri(), wait_until="load", timeout=HTML_RENDER_TIMEOUT_MS)
            page.locator(".slide").first.screenshot(path=str(png_path), animations="disabled")
            item["png_path"] = str(png_path)
            slide_pngs.append(png_path)
    finally:
        browser.close()
        pw.stop()
    return slide_pngs


def _export_pdf(pptx_path: Path, out_dir: Path) -> str:
    try:
        result = _run([
            LIBREOFFICE_PATH, "--headless",
            "--convert-to", "pdf:impress_pdf_Export",
            "--outdir", str(out_dir),
            str(pptx_path),
        ])
        if result.returncode != 0:
            return ""
        candidate = out_dir / f"{pptx_path.stem}.pdf"
        return str(candidate) if candidate.exists() else ""
    except Exception:
        return ""


def plan_html_theme(plan: dict, description: str) -> dict:
    messages = [
        {"role": "system", "content": THEME_SYSTEM},
        {"role": "user", "content": THEME_USER.format(
            deck_title=plan.get("deck_title", ""),
            deck_theme=plan.get("deck_theme", ""),
            description=description,
            slides_text=_plan_slides_text(plan),
        )},
    ]
    return call_llm_json(messages, temperature=0.2, response_schema=THEME_JSON_SCHEMA)


def run_html_pipeline(
    description: str,
    *,
    output_dir: Path,
    max_slides: int | None = None,
    validate: bool = True,
    content_plan: dict | None = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    rendered_dir = output_dir / "rendered"
    html_dir.mkdir(parents=True, exist_ok=True)

    plan = content_plan or plan_content(description)
    slide_cap = max_slides if max_slides is not None else MAX_SLIDES_DEFAULT
    if slide_cap is not None and slide_cap > 0:
        plan["slides"] = plan.get("slides", [])[:slide_cap]
    plan_path = output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    theme = plan_html_theme(plan, description)
    theme_path = output_dir / "theme.json"
    theme_path.write_text(json.dumps(theme, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest: list[dict] = []
    for slide in plan.get("slides", []):
        slide_dir = html_dir / f"slide-{slide['slide_number']:02d}-{_safe_name(slide['title'])}"
        slide_dir.mkdir(parents=True, exist_ok=True)
        assets = _prepare_slide_assets(slide, slide_dir)
        design = _design_slide(theme, slide, assets, _deck_previous_labels(manifest))
        html_path = _write_slide_html(slide_dir, theme, design)
        manifest.append({
            "slide_number": slide["slide_number"],
            "title": slide["title"],
            "speaker_notes": design.get("speaker_notes") or slide.get("speaker_notes", ""),
            "assets": assets,
            "design": design,
            "html_path": str(html_path),
        })

    slide_pngs = render_slide_html_files(manifest, rendered_dir)

    validation_results: list[dict] = []
    if validate:
        for repair_round in range(HTML_MAX_REPAIR_ROUNDS + 1):
            validation_results = []
            failed: list[int] = []
            for item, slide in zip(manifest, plan.get("slides", []), strict=False):
                result = _validate_slide_render(Path(item["png_path"]), slide, item["design"])
                validation_results.append(result)
                if result.get("verdict") != "pass":
                    failed.append(slide["slide_number"])

            if not failed or repair_round >= HTML_MAX_REPAIR_ROUNDS:
                break

            for item, slide in zip(manifest, plan.get("slides", []), strict=False):
                if slide["slide_number"] not in failed:
                    continue
                validation = next(v for v in validation_results if v["slide_number"] == slide["slide_number"])
                repaired = _repair_slide(theme, slide, item["assets"], item["design"], validation)
                item["design"] = repaired
                item["speaker_notes"] = repaired.get("speaker_notes") or item["speaker_notes"]
                _write_slide_html(Path(item["html_path"]).parent, theme, repaired)
            slide_pngs = render_slide_html_files(manifest, rendered_dir)

    collage_path: Path | None = output_dir / "contact-sheet.png"
    try:
        create_numbered_collage(slide_pngs, collage_path)
    except Exception:
        collage_path = None

    pptx_path = build_pptx_from_images(
        slide_pngs,
        output_dir / "deck.pptx",
        deck_title=plan.get("deck_title", ""),
        speaker_notes=[item["speaker_notes"] for item in manifest],
    )
    ooxml_report = validate_pptx(pptx_path)
    pdf_path = _export_pdf(pptx_path, output_dir)

    report = {
        "workflow": "html",
        "description": description,
        "plan_path": str(plan_path),
        "theme_path": str(theme_path),
        "html_manifest": manifest,
        "slide_pngs": [str(p) for p in slide_pngs],
        "contact_sheet": str(collage_path) if collage_path is not None else "",
        "pptx_path": str(pptx_path),
        "pdf_path": pdf_path,
        "validation_results": validation_results,
        "ooxml_validation": ooxml_report,
    }
    (output_dir / "build_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report
