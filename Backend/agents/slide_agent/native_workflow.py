"""Advanced slide workflow: HTML-designed, natively-editable PPTX.

The third slide path. It reuses everything good about the HTML workflow
(content planning, locked theme system, real assets, render-validate-repair)
but replaces the screenshot endpoint with an HTML → native PowerPoint
conversion (html2pptx), so the deck lands fully editable — real text boxes,
shapes, and images — while still looking designed.

Template support: given ANY user-provided PPTX, the pipeline extracts the
template's own theme (brand colors + fonts), maps each planned slide to the
template layout that best fits its content (reusing the cataloger and layout
selector), and builds the new slides ON the template file so its masters and
chrome show through. Without a template, the planning step picks a locked
theme like the HTML workflow does.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import html_workflow
from content_planner import plan_content
from html2pptx import (
    CANVAS_HEIGHT_PX,
    CANVAS_WIDTH_PX,
    convert_html_deck_to_native_pptx,
    parse_css_color,
)
from html_workflow import (
    BASE_HTML_CSS,
    SLIDE_JSON_SCHEMA,
    SLIDE_USER,
    _assets_text,
    _export_pdf,
    _prepare_slide_assets,
    _repair_slide,
    _safe_name,
    _theme_css,
    _validate_slide_render,
    plan_html_theme,
    render_slide_html_files,
)
from llm_client import call_llm_json, env_int
from ooxml_validator import validate_pptx
from template_cataloger import (
    create_numbered_collage,
    load_catalog,
    catalog_template,
    render_template_to_pngs,
)

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

NATIVE_MAX_REPAIR_ROUNDS = env_int("NATIVE_MAX_REPAIR_ROUNDS", 1)
NATIVE_PPTX_QA = os.getenv("NATIVE_PPTX_QA", "1") not in {"0", "false", "no"}
MAX_SLIDES_DEFAULT = env_int("MAX_SLIDES", 50)

logger = logging.getLogger(__name__)

NATIVE_DESIGN_ADDENDUM = """

NATIVE POWERPOINT FIDELITY  (this deck is converted into real PowerPoint objects)
- Your rendered geometry becomes native, editable PowerPoint shapes and text
  boxes. Any CSS layout works — the browser resolves it — but the visual
  vocabulary must stay convertible:
- Flat solid fills and thin borders are perfect. Linear gradients with exactly
  two colors are supported. border-radius becomes a rounded rectangle.
- Do NOT use: box-shadows, backdrop-filter/blur, CSS transforms, clip-path,
  filters, ::before/::after decorative content, border images, multiple
  background layers. These are dropped in conversion.
- Text must be a flat color: no text-shadow, no gradient text, no outlined
  text, no heavy letter-spacing as a visual device.
- Images are placed as rectangles (object-fit cover/contain is respected).
- Everything else — hierarchy, contrast, restraint, the locked theme —
  applies exactly as written above.
"""

TEMPLATE_MODE_ADDENDUM = """

TEMPLATE-BACKED SLIDE  (built on top of a user-provided PowerPoint template)
- The template's own background, master design, and decorative elements show
  through behind your content. This slide's canvas is TRANSPARENT: never paint
  an opaque background on .slide and never use a full-slide color fill.
- The canvas for this deck is {canvas_w}×{canvas_h}px — design within it.
- Use ONLY the theme tokens provided: they were extracted from the template's
  own theme (its brand colors and fonts). The result must read as if the
  template's original author designed this slide.
- Keep content inside comfortable margins so the template's chrome (headers,
  footers, side bars, logos) stays visible and unclipped.
- Prefer content-driven composition (type, panels, images) over decoration;
  the template already supplies the ornament.
"""


# ── template theme extraction ─────────────────────────────────────────────────

_DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _parse_scheme_color(scheme_el: Any, tag: str) -> str | None:
    el = scheme_el.find(f"{{{_DRAWINGML_NS}}}{tag}")
    if el is None:
        return None
    srgb = el.find(f"{{{_DRAWINGML_NS}}}srgbClr")
    if srgb is not None and srgb.get("val"):
        return f"#{srgb.get('val').upper()}"
    sys_clr = el.find(f"{{{_DRAWINGML_NS}}}sysClr")
    if sys_clr is not None:
        return f"#{(sys_clr.get('lastClr') or ('000000' if sys_clr.get('val') == 'windowText' else 'FFFFFF')).upper()}"
    return None


def _parse_scheme_font(scheme_el: Any, tag: str) -> str | None:
    font_el = scheme_el.find(f"{{{_DRAWINGML_NS}}}{tag}")
    if font_el is None:
        return None
    latin = font_el.find(f"{{{_DRAWINGML_NS}}}latin")
    if latin is not None and latin.get("typeface"):
        return latin.get("typeface")
    return None


def extract_template_theme(template_path: Path) -> dict[str, Any] | None:
    """Extract a THEME_JSON_SCHEMA-shaped design system from a PPTX template.

    Reads the drawingml theme (brand colors + major/minor fonts) so the
    designed slides match the template's identity. Returns None when the
    theme part cannot be read — callers then fall back to a planned theme.
    """
    try:
        with zipfile.ZipFile(template_path) as bundle:
            theme_names = sorted(
                name for name in bundle.namelist()
                if re.fullmatch(r"ppt/theme/theme\d+\.xml", name)
            )
            if not theme_names:
                return None
            xml_bytes = bundle.read(theme_names[0])
    except Exception:
        logger.warning("native_workflow: could not read theme from %s", template_path, exc_info=True)
        return None

    try:
        from lxml import etree

        root = etree.fromstring(xml_bytes)
    except Exception:
        logger.warning("native_workflow: could not parse theme XML in %s", template_path, exc_info=True)
        return None

    scheme = root.find(f".//{{{_DRAWINGML_NS}}}clrScheme")
    font_scheme = root.find(f".//{{{_DRAWINGML_NS}}}fontScheme")
    if scheme is None:
        return None

    bg = _parse_scheme_color(scheme, "lt1") or "#FFFFFF"
    fg = _parse_scheme_color(scheme, "dk1") or "#111111"
    accent = _parse_scheme_color(scheme, "accent1") or "#2563EB"
    accent_2 = _parse_scheme_color(scheme, "accent2") or "#0EA5A4"

    display_font = _parse_scheme_font(font_scheme, "majorFont") if font_scheme is not None else None
    body_font = _parse_scheme_font(font_scheme, "minorFont") if font_scheme is not None else None
    display_font = display_font or "Arial"
    body_font = body_font or "Arial"

    muted = _blend_hex(fg, bg, 0.55)
    theme = {
        "theme_name": f"{template_path.stem} (template theme)",
        "design_rationale": (
            "Colors and fonts extracted from the user's PowerPoint template "
            "theme; slides are built on the template's own layouts."
        ),
        "css_variables": {
            "bg": bg,
            "fg": fg,
            "muted": muted,
            "accent": accent,
            "accent_2": accent_2,
            "panel": _rgba(fg, 0.06),
            "panel_strong": _rgba(fg, 0.12),
            "line": _rgba(fg, 0.16),
        },
        "font_stacks": {
            "display": f"{display_font}, Arial, sans-serif",
            "body": f"{body_font}, Arial, sans-serif",
            "accent": f"{display_font}, Arial, sans-serif",
        },
        "deck_guidelines": [
            "Use the template's brand colors and fonts exactly as given in the tokens.",
            "Never paint an opaque slide background — the template's own design shows through.",
            "Keep content inside generous margins so template headers and footers stay clear.",
            "When the template look is quiet, let type scale and spacing carry the hierarchy.",
        ],
    }
    return theme


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    raw = hex_color.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except (ValueError, IndexError):
        return (17, 17, 17)


def _blend_hex(fg_hex: str, bg_hex: str, alpha: float) -> str:
    fg = _hex_to_rgb(fg_hex)
    bg = _hex_to_rgb(bg_hex)
    blended = tuple(round(bg[i] * (1 - alpha) + fg[i] * alpha) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*blended)


# ── slide design (native-fidelity prompt variants) ────────────────────────────

def _design_slide_native(
    theme: dict,
    slide: dict,
    assets: list[dict],
    previous_labels: str,
    *,
    template_mode: bool,
    canvas_w: int = CANVAS_WIDTH_PX,
    canvas_h: int = CANVAS_HEIGHT_PX,
) -> dict:
    system_prompt = html_workflow.SLIDE_SYSTEM + NATIVE_DESIGN_ADDENDUM
    if template_mode:
        system_prompt += TEMPLATE_MODE_ADDENDUM.format(canvas_w=canvas_w, canvas_h=canvas_h)
    messages = [
        {"role": "system", "content": system_prompt},
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


def _write_slide_html_native(
    slide_dir: Path,
    theme: dict,
    design: dict,
    *,
    canvas_w: int = CANVAS_WIDTH_PX,
    canvas_h: int = CANVAS_HEIGHT_PX,
) -> Path:
    """Like html_workflow._write_slide_html but with an adjustable canvas."""
    canvas_override = ""
    if (canvas_w, canvas_h) != (CANVAS_WIDTH_PX, CANVAS_HEIGHT_PX):
        canvas_override = (
            f"\nhtml, body {{ width: {canvas_w}px; height: {canvas_h}px; }}\n"
            f".slide {{ width: {canvas_w}px; height: {canvas_h}px; }}\n"
        )
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width={canvas_w}, initial-scale=1.0" />
  <style>
{BASE_HTML_CSS}
{_theme_css(theme)}
{canvas_override}{design.get("custom_css", "")}
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


# ── parallel QA helpers ────────────────────────────────────────────────────────
#
# Vision validations and repairs are independent per slide, so they fan out
# across threads; on a thinking model this turns N sequential calls into
# roughly one call of wall time. Results keep slide order.


def _qa_workers(count: int) -> int:
    return max(1, min(env_int("NATIVE_QA_PARALLELISM", 4), count or 1))


def _validate_pairs_parallel(pairs: list[tuple[dict, dict, Path]]) -> list[dict]:
    """Validate (manifest_item, plan_slide, png_path) triples concurrently."""
    results: list[dict | None] = [None] * len(pairs)

    def _one(index: int) -> None:
        item, slide, png_path = pairs[index]
        results[index] = _validate_slide_render(png_path, slide, item["design"])

    if len(pairs) > 1:
        with ThreadPoolExecutor(max_workers=_qa_workers(len(pairs))) as pool:
            futures = [pool.submit(_one, index) for index in range(len(pairs))]
            for future in futures:
                future.result()
    else:
        for index in range(len(pairs)):
            _one(index)
    return [result for result in results if result is not None]


def _repair_failed_parallel(
    manifest: list[dict],
    slides: list[dict],
    validations: list[dict],
    failed_numbers: set[int],
    theme: dict,
    *,
    canvas_w: int = CANVAS_WIDTH_PX,
    canvas_h: int = CANVAS_HEIGHT_PX,
) -> None:
    """Repair the failed slides concurrently and rewrite their HTML."""

    def _one(item: dict, slide: dict, validation: dict) -> None:
        repaired = _repair_slide(theme, slide, item["assets"], item["design"], validation)
        item["design"] = repaired
        item["speaker_notes"] = repaired.get("speaker_notes") or item["speaker_notes"]
        _write_slide_html_native(
            Path(item["html_path"]).parent,
            theme,
            repaired,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )

    jobs = [
        (item, slide, next((v for v in validations if v.get("slide_number") == slide["slide_number"]), None))
        for item, slide in zip(manifest, slides, strict=False)
        if slide["slide_number"] in failed_numbers
    ]
    jobs = [(item, slide, validation) for item, slide, validation in jobs if validation is not None]
    if not jobs:
        return
    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=_qa_workers(len(jobs))) as pool:
            futures = [pool.submit(_one, item, slide, validation) for item, slide, validation in jobs]
            for future in futures:
                future.result()
    else:
        _one(*jobs[0])


# ── conversion + final QA ──────────────────────────────────────────────────────

def _backdrop_from_theme(theme: dict, template_mode: bool) -> tuple[int, int, int]:
    """Fallback backdrop for flattening translucent colors against."""
    bg = (theme.get("css_variables") or {}).get("bg") if theme else None
    if bg and not str(bg).strip().lower().startswith("transparent"):
        parsed = parse_css_color(str(bg))
        if parsed:
            return (parsed[0], parsed[1], parsed[2])
    return (255, 255, 255) if template_mode else (17, 17, 17)


def _final_pptx_qa_rounds(
    manifest: list[dict],
    plan: dict,
    theme: dict,
    output_dir: Path,
    deck_path: Path,
    *,
    validate: bool,
    template_path: Path | None,
    layout_numbers: dict[int, int] | None,
    canvas_w: int,
    canvas_h: int,
) -> tuple[list[dict], list[Path]]:
    """Render the FINAL pptx and repair slides that fail visual review.

    Returns (validation_results, final_render_pngs). When validate is off or
    LibreOffice rendering is unavailable, returns ([], []) and the HTML-stage
    validation already recorded is the QA of record.
    """
    rendered_dir = output_dir / "rendered-native"
    if not validate or not NATIVE_PPTX_QA:
        return [], []

    validation_results: list[dict] = []
    final_pngs: list[Path] = []
    for round_index in range(NATIVE_MAX_REPAIR_ROUNDS + 1):
        try:
            final_pngs = render_template_to_pngs(deck_path, rendered_dir)
        except Exception as exc:
            logger.warning("native_workflow: final pptx render unavailable, skipping pptx QA: %s", exc)
            return validation_results, []
        if len(final_pngs) != len(manifest):
            logger.warning(
                "native_workflow: expected %d rendered slides, got %d",
                len(manifest),
                len(final_pngs),
            )

        validation_results = []
        pairs = [
            (item, slide, final_pngs[slide["slide_number"] - 1])
            for item, slide in zip(manifest, plan.get("slides", []), strict=False)
            if slide["slide_number"] - 1 < len(final_pngs)
        ]
        validation_results = _validate_pairs_parallel(pairs)
        failed = {
            result["slide_number"]
            for result in validation_results
            if result.get("verdict") != "pass"
        }

        if not failed or round_index >= NATIVE_MAX_REPAIR_ROUNDS:
            break

        # Repair the HTML design, re-render, and reconvert the whole deck.
        _repair_failed_parallel(
            manifest,
            plan.get("slides", []),
            validation_results,
            failed,
            theme,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )
        _reconvert_manifest(
            manifest,
            deck_path,
            theme=theme,
            template_path=template_path,
            layout_numbers=layout_numbers,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )

    return validation_results, final_pngs


def _reconvert_manifest(
    manifest: list[dict],
    deck_path: Path,
    *,
    theme: dict,
    template_path: Path | None,
    layout_numbers: dict[int, int] | None,
    canvas_w: int,
    canvas_h: int,
) -> None:
    try:
        convert_html_deck_to_native_pptx(
            manifest,
            deck_path,
            template_path=template_path,
            template_layout_numbers=layout_numbers,
            backdrop_rgb=_backdrop_from_theme(theme, template_path is not None),
        )
    except Exception:
        logger.exception("native_workflow: reconvert failed; keeping previous deck")


# ── main pipeline ──────────────────────────────────────────────────────────────

def run_native_pipeline(
    description: str,
    *,
    output_dir: Path,
    max_slides: int | None = None,
    validate: bool = True,
    content_plan: dict | None = None,
    template_path: Path | None = None,
    catalog: dict | None = None,
    force_catalog: bool = False,
) -> dict:
    """Design in HTML, land as a native editable PPTX.

    With template_path set, the deck is built on the user's template: its
    theme drives the design tokens and each slide is placed on the template
    layout chosen for its content. Without one, the planning step picks and
    locks a theme (same system as the HTML workflow).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    rendered_dir = output_dir / "rendered"
    html_dir.mkdir(parents=True, exist_ok=True)

    plan = content_plan or plan_content(description, num_slides=max_slides)
    slide_cap = max_slides if max_slides is not None else MAX_SLIDES_DEFAULT
    if slide_cap is not None and slide_cap > 0:
        plan["slides"] = plan.get("slides", [])[:slide_cap]
    plan_path = output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    template_mode = template_path is not None
    layout_numbers: dict[int, int] = {}

    if template_mode:
        catalog = catalog or _ensure_catalog(template_path, force_catalog)
        theme = extract_template_theme(template_path)
        if theme is None:
            logger.info("native_workflow: template theme extraction failed; planning a theme instead")
            theme = plan_html_theme(plan, description)
        else:
            layout_numbers = _map_slides_to_template_layouts(plan, catalog, max_slides)
    else:
        theme = plan_html_theme(plan, description)

    theme_path = output_dir / "theme.json"
    theme_path.write_text(json.dumps(theme, indent=2, ensure_ascii=False), encoding="utf-8")

    # Canvas follows the target deck's aspect ratio (templates may be 4:3).
    prs_dims = _deck_dimensions(template_path)
    canvas_w, canvas_h = prs_dims or (CANVAS_WIDTH_PX, CANVAS_HEIGHT_PX)

    # Per-slide design runs in parallel: the calls are independent (coherence
    # comes from the locked theme + planned titles, not from call order), and
    # on a thinking model sequential design dominated the wall clock.
    slides = plan.get("slides", [])
    manifest: list[dict | None] = [None] * len(slides)
    design_workers = max(1, min(env_int("NATIVE_DESIGN_PARALLELISM", 4), len(slides) or 1))

    def _design_one(index: int, slide: dict) -> None:
        slide_dir = html_dir / f"slide-{slide['slide_number']:02d}-{_safe_name(slide['title'])}"
        slide_dir.mkdir(parents=True, exist_ok=True)
        assets = _prepare_slide_assets(slide, slide_dir)
        planned_labels = "\n".join(
            f"- Slide {s['slide_number']}: {s['title']}" for s in slides[max(0, index - 3):index]
        ) or "- none"
        design = _design_slide_native(
            theme,
            slide,
            assets,
            planned_labels,
            template_mode=template_mode,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )
        html_path = _write_slide_html_native(
            slide_dir,
            theme,
            design,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )
        manifest[index] = {
            "slide_number": slide["slide_number"],
            "title": slide["title"],
            "deck_title": plan.get("deck_title", ""),
            "speaker_notes": design.get("speaker_notes") or slide.get("speaker_notes", ""),
            "assets": assets,
            "design": design,
            "html_path": str(html_path),
        }

    if design_workers > 1 and len(slides) > 1:
        with ThreadPoolExecutor(max_workers=design_workers) as pool:
            futures = [pool.submit(_design_one, index, slide) for index, slide in enumerate(slides)]
            for future in futures:
                future.result()
    else:
        for index, slide in enumerate(slides):
            _design_one(index, slide)
    manifest = [entry for entry in manifest if entry is not None]

    # Stage 1 QA: the HTML renders (cheap, catches most issues before conversion).
    slide_pngs = render_slide_html_files(manifest, rendered_dir, canvas_width=canvas_w, canvas_height=canvas_h)

    html_validation: list[dict] = []
    if validate:
        for repair_round in range(html_workflow.HTML_MAX_REPAIR_ROUNDS + 1):
            html_validation = _validate_pairs_parallel(
                [(item, slide, Path(item["png_path"])) for item, slide in zip(manifest, slides, strict=False)]
            )
            failed = {
                result["slide_number"]
                for result in html_validation
                if result.get("verdict") != "pass"
            }
            if not failed or repair_round >= html_workflow.HTML_MAX_REPAIR_ROUNDS:
                break
            _repair_failed_parallel(
                manifest,
                slides,
                html_validation,
                failed,
                theme,
                canvas_w=canvas_w,
                canvas_h=canvas_h,
            )
            slide_pngs = render_slide_html_files(manifest, rendered_dir, canvas_width=canvas_w, canvas_height=canvas_h)

    # Conversion to native objects.
    deck_path = output_dir / "deck.pptx"
    convert_html_deck_to_native_pptx(
        manifest,
        deck_path,
        deck_title=plan.get("deck_title", ""),
        template_path=template_path,
        template_layout_numbers=layout_numbers or None,
        backdrop_rgb=_backdrop_from_theme(theme, template_mode),
    )

    # Stage 2 QA: review the FINAL pptx renders and repair + reconvert if needed.
    validation_results, final_pngs = _final_pptx_qa_rounds(
        manifest,
        plan,
        theme,
        output_dir,
        deck_path,
        validate=validate,
        template_path=template_path,
        layout_numbers=layout_numbers or None,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )

    contact_pngs = final_pngs or slide_pngs
    collage_path: Path | None = output_dir / "contact-sheet.png"
    try:
        create_numbered_collage(contact_pngs, collage_path)
    except Exception:
        collage_path = None

    ooxml_report = validate_pptx(deck_path)
    pdf_path = _export_pdf(deck_path, output_dir)

    report = {
        "workflow": "advanced",
        "description": description,
        "plan_path": str(plan_path),
        "theme_path": str(theme_path),
        "html_manifest": manifest,
        "slide_pngs": [str(p) for p in contact_pngs],
        "contact_sheet": str(collage_path) if collage_path is not None else "",
        "pptx_path": str(deck_path),
        "pdf_path": pdf_path,
        "validation_results": validation_results or html_validation,
        "ooxml_validation": ooxml_report,
        "native": True,
        "template_backed": template_mode,
        "template_path": str(template_path) if template_path else None,
        "template_layouts": layout_numbers,
        "canvas": {"width": canvas_w, "height": canvas_h},
    }
    (output_dir / "build_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def _map_slides_to_template_layouts(
    plan: dict,
    catalog: dict,
    max_slides: int | None,
) -> dict[int, int]:
    """Map deck slides → template slide numbers via the existing selector."""
    try:
        from layout_selector import select_layouts

        build_spec = select_layouts(plan, catalog, max_slides=max_slides)
        mapping = {
            int(slide.get("deck_slide_number")): int(slide.get("template_slide_number"))
            for slide in build_spec.get("slides", [])
            if isinstance(slide, dict)
            and slide.get("deck_slide_number") is not None
            and slide.get("template_slide_number") is not None
        }
        return mapping
    except Exception:
        logger.warning("native_workflow: layout selection failed; using the template's default layout", exc_info=True)
        return {}


def _ensure_catalog(template_path: Path, force: bool) -> dict:
    cached = None if force else load_catalog(template_path)
    if cached is not None:
        return cached
    return catalog_template(template_path, force=force)


def _deck_dimensions(template_path: Path | None) -> tuple[int, int] | None:
    """Canvas size matching the template's slide aspect (px at 96dpi width 1280)."""
    if template_path is None:
        return None
    try:
        from pptx import Presentation
        from pptx.util import Emu

        prs = Presentation(str(template_path))
        width_emu = int(prs.slide_width)
        height_emu = int(prs.slide_height)
        if width_emu <= 0:
            return None
        canvas_w = CANVAS_WIDTH_PX
        canvas_h = round(CANVAS_WIDTH_PX * height_emu / width_emu)
        return (canvas_w, canvas_h)
    except Exception:
        logger.warning("native_workflow: could not read template dimensions", exc_info=True)
        return None
