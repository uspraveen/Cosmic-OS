"""Deterministic PPTX builder using python-pptx.

Converts DeckPlan JSON into real .pptx files. Handles:
- Template loading (built-in and custom)
- All layout types (title, content, two-column, chart, table, image, blank)
- Bullets/paragraphs, charts, tables, images
- Background colors, font styling
- Speaker notes
- Edit operations (add/remove/update/reorder slides)
- PDF export via LibreOffice
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

from .templates_registry import get_template

logger = logging.getLogger(__name__)

# Layout name → index mapping (for built-in template)
_LAYOUT_MAP = {
    "title_slide": 0,
    "content": 1,
    "section_divider": 2,
    "two_content": 3,
    "content_with_image": 3,  # Use two_content, fill one side with image
    "content_with_chart": 1,  # Use content, replace body with chart
    "table_slide": 1,  # Use content, replace body with table
    "blank": 6,
}

# System fonts that are safe across Windows/Mac/Office
_SYSTEM_FONTS = {
    "calibri", "arial", "segoe ui", "helvetica", "verdana",
    "cambria", "times new roman", "georgia",
    "consolas", "courier new",
}

def _enforce_system_font(font_family: str) -> str:
    """Return the font if it's a known system font, otherwise fall back to Calibri."""
    if not font_family:
        return "Calibri"
    if font_family.strip().lower() in _SYSTEM_FONTS:
        return font_family.strip()
    # Check partial matches (e.g., "Segoe UI Semibold" → allow)
    lower = font_family.strip().lower()
    for sf in _SYSTEM_FONTS:
        if lower.startswith(sf):
            return font_family.strip()
    logger.warning("Non-system font '%s' replaced with 'Calibri'", font_family)
    return "Calibri"


# Placeholder indices used for footer/meta — never assign content to these
_FOOTER_PLACEHOLDER_INDICES = {10, 11, 12}
_FOOTER_ROLES = {"footer", "date", "slide_number"}


def auto_map_legacy_to_assignments(
    slide_def: dict[str, Any],
    layout_placeholders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert legacy content/image/chart/table fields to template-guided assignments.

    If the slide already has assignments, returns it unchanged.
    For BLANK layouts, returns unchanged (free-flow is expected).
    For code_chart and flow_diagram content types, returns unchanged (need custom positioning).
    """
    if slide_def.get("assignments"):
        return slide_def  # Already template-guided

    layout_name = str(slide_def.get("layout") or "").strip().upper()
    if "BLANK" in layout_name:
        return slide_def  # Blank layout: free-flow is correct

    # Check for content types that require free-flow
    for slot in ("content", "left_content", "right_content"):
        slot_content = slide_def.get(slot, {})
        if isinstance(slot_content, dict) and slot_content.get("type") in (
            "code_chart",
            "flow_diagram",
        ):
            return slide_def  # These need custom positioning

    # Filter out footer/meta placeholders
    content_phs = [
        ph
        for ph in layout_placeholders
        if ph.get("idx") not in _FOOTER_PLACEHOLDER_INDICES
        and ph.get("role") not in _FOOTER_ROLES
    ]
    if not content_phs:
        return slide_def  # No content placeholders available

    assignments: dict[str, Any] = {}

    # Map title → first title placeholder
    title_ph = next((ph for ph in content_phs if ph.get("role") == "title"), None)
    if title_ph and slide_def.get("title"):
        assignments[str(title_ph["idx"])] = {
            "type": "title",
            "text": str(slide_def["title"]),
        }

    # Map subtitle → first subtitle placeholder
    subtitle_ph = next(
        (ph for ph in content_phs if ph.get("role") == "subtitle"), None
    )
    if subtitle_ph and slide_def.get("subtitle"):
        assignments[str(subtitle_ph["idx"])] = {
            "type": "subtitle",
            "text": str(slide_def["subtitle"]),
        }

    # Collect body/content placeholders (not yet assigned)
    body_phs = [
        ph
        for ph in content_phs
        if ph.get("role") in ("body", "content", "object")
        and str(ph.get("idx")) not in assignments
    ]

    # Map content (bullets/paragraph)
    content = slide_def.get("content")
    if isinstance(content, dict) and body_phs:
        assignments[str(body_phs[0]["idx"])] = content
        body_phs = body_phs[1:]

    # Map two-column content
    left = slide_def.get("left_content")
    if isinstance(left, dict) and body_phs:
        assignments[str(body_phs[0]["idx"])] = left
        body_phs = body_phs[1:]
    right = slide_def.get("right_content")
    if isinstance(right, dict) and body_phs:
        assignments[str(body_phs[0]["idx"])] = right
        body_phs = body_phs[1:]

    # Map chart → next available body placeholder
    chart = slide_def.get("chart")
    if isinstance(chart, dict) and body_phs:
        chart_assignment = dict(chart)
        chart_assignment.setdefault("type", "chart")
        assignments[str(body_phs[0]["idx"])] = chart_assignment
        body_phs = body_phs[1:]

    # Map table → next available body placeholder
    table = slide_def.get("table")
    if isinstance(table, dict) and body_phs:
        table_assignment = dict(table)
        table_assignment.setdefault("type", "table")
        assignments[str(body_phs[0]["idx"])] = table_assignment
        body_phs = body_phs[1:]

    # Map image → picture placeholder first, then body placeholder
    image = slide_def.get("image")
    if isinstance(image, dict):
        image_ph = next(
            (ph for ph in content_phs if ph.get("role") == "image" and str(ph.get("idx")) not in assignments),
            None,
        )
        if image_ph:
            image_assignment = dict(image)
            image_assignment.setdefault("type", "image")
            assignments[str(image_ph["idx"])] = image_assignment
        elif body_phs:
            image_assignment = dict(image)
            image_assignment.setdefault("type", "image")
            assignments[str(body_phs[0]["idx"])] = image_assignment

    if assignments:
        slide_def["assignments"] = assignments
        logger.info(
            "Auto-mapped legacy fields to assignments for slide %s (layout=%s, indices=%s)",
            slide_def.get("slide_number", "?"),
            slide_def.get("layout", "?"),
            sorted(assignments.keys()),
        )

    return slide_def

# Chart type string → XL_CHART_TYPE mapping
_CHART_TYPE_MAP = {
    "column_clustered": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar_clustered": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "area": XL_CHART_TYPE.AREA,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
    "column_stacked": XL_CHART_TYPE.COLUMN_STACKED,
    "bar_stacked": XL_CHART_TYPE.BAR_STACKED,
    "line_markers": XL_CHART_TYPE.LINE_MARKERS,
    "pie_exploded": XL_CHART_TYPE.PIE_EXPLODED,
    "area_stacked": XL_CHART_TYPE.AREA_STACKED,
}


def _hex_to_rgb(hex_color: str) -> RGBColor:
    """Convert hex color string to RGBColor."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _safe_text(text: Any) -> str:
    """Safely convert to string."""
    return str(text or "").strip()


class SlideBuilder:
    """Builds PPTX presentations from DeckPlan JSON using python-pptx."""

    def __init__(self, templates_dir: Path) -> None:
        self._templates_dir = templates_dir

    @staticmethod
    def _strip_content_slides(prs: Presentation) -> Presentation:
        """Remove all existing content slides from a template, preserving layouts and masters.

        Downloaded templates (Slidesgo, etc.) ship with 18-33 example slides.
        Without stripping, every generated deck would start with those junk slides.
        """
        while len(prs.slides._sldIdLst) > 0:
            sld_id = prs.slides._sldIdLst[0]
            rId = sld_id.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            if rId:
                try:
                    prs.part.drop_rel(rId)
                except Exception:
                    pass
            prs.slides._sldIdLst.remove(sld_id)
        return prs

    def load_template(self, template_name: str) -> Presentation:
        """Load a template PPTX, strip example slides, return clean canvas.

        Supports:
        - Built-in: "tech-trends", "business-meeting", "science-lesson", "tech-infographics"
        - Fallback: "blank" (empty presentation with no design)
        - User-uploaded: "user:template-name" (looks in templates/user/)
        - File path: absolute or relative .pptx path
        """
        prs = None
        if template_name and template_name != "blank":
            if template_name.startswith("user:"):
                user_name = template_name[5:]
                user_path = self._templates_dir / "user" / f"{user_name}.pptx"
                if user_path.exists():
                    prs = Presentation(str(user_path))
            else:
                template_path = self._templates_dir / f"{template_name}.pptx"
                if template_path.exists():
                    prs = Presentation(str(template_path))

            if prs is None:
                p = Path(template_name)
                if p.exists() and p.suffix == ".pptx":
                    prs = Presentation(str(p))

        if prs is None:
            logger.warning(
                "Template '%s' not found in %s — falling back to blank presentation. "
                "Slides will have no background design or decorative elements.",
                template_name,
                self._templates_dir,
            )
            return Presentation()

        # Strip example/demo slides that ship with downloaded templates
        if len(prs.slides) > 0:
            self._strip_content_slides(prs)

        return prs

    def extract_layouts(self, prs: Presentation) -> list[dict[str, Any]]:
        """Extract layout structure from a presentation.

        Returns a list of layout definitions with placeholder zones,
        suitable for the LLM to plan within template constraints.
        """
        layouts = []
        for layout in prs.slide_layouts:
            layout_info: dict[str, Any] = {
                "name": layout.name,
                "index": prs.slide_layouts.index(layout),
                "placeholders": [],
            }
            for ph in layout.placeholders:
                pf = ph.placeholder_format
                # Convert EMU to inches
                left_in = round(ph.left / 914400, 3)
                top_in = round(ph.top / 914400, 3)
                width_in = round(ph.width / 914400, 3)
                height_in = round(ph.height / 914400, 3)

                # Map type to content role
                raw = str(pf.type)
                # Handle enum format like "CENTER_TITLE (3)"
                if "(" in raw:
                    type_name = raw.split("(")[0].strip().upper()
                elif "." in raw:
                    type_name = raw.split(".")[-1].strip().upper()
                else:
                    type_name = raw.strip().upper()
                content_role = {
                    "TITLE": "title",
                    "CENTER_TITLE": "title",
                    "SUBTITLE": "subtitle",
                    "BODY": "body",
                    "OBJECT": "content",
                    "PICTURE": "image",
                    "DATE": "footer",
                    "FOOTER": "footer",
                    "SLIDE_NUMBER": "footer",
                    "CHART": "chart",
                    "TABLE": "table",
                    "VERTICAL_BODY": "body",
                    "VERTICAL_TITLE": "title",
                    "VERTICAL_OBJECT": "content",
                }.get(type_name, type_name.lower())

                layout_info["placeholders"].append(
                    {
                        "idx": pf.idx,
                        "name": ph.name,
                        "type": type_name,
                        "role": content_role,
                        "zone": {
                            "x_inches": left_in,
                            "y_inches": top_in,
                            "width_inches": width_in,
                            "height_inches": height_in,
                        },
                    }
                )
            layouts.append(layout_info)
        return layouts

    def load_existing(self, pptx_path: Path) -> Presentation:
        """Load an existing PPTX for editing."""
        return Presentation(str(pptx_path))

    def build_deck(self, plan: dict[str, Any], output_path: Path) -> Path:
        """Build a complete deck from a DeckPlan JSON. Returns output path."""
        deck_def = plan.get("deck", {})
        slides_def = plan.get("slides", [])

        template_name = deck_def.get("template", "blank")
        prs = self.load_template(template_name)

        # Set slide dimensions — preserve template's own if loading a real template
        plan_dims = deck_def.get("dimensions", {})
        if plan_dims and plan_dims.get("width"):
            prs.slide_width = Inches(plan_dims["width"])
            prs.slide_height = Inches(plan_dims["height"])
        # else: keep template's native dimensions

        theme = self._effective_theme(template_name, deck_def.get("theme", {}))

        for slide_def in slides_def:
            self._add_slide(prs, slide_def, theme)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(str(output_path))
        return output_path

    def _effective_theme(
        self, template_name: str, theme: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Merge template defaults with LLM-provided theme overrides."""
        merged = dict(theme or {})
        template_meta = get_template(template_name, self._templates_dir) or {}
        if template_meta:
            merged.setdefault("background_color", template_meta.get("background"))
            merged.setdefault("text_color", template_meta.get("text_color"))
            accents = template_meta.get("accent_colors") or []
            if accents:
                merged.setdefault("accent_color", accents[0])
        return merged

    def apply_edits(
        self, prs: Presentation, operations: list[dict[str, Any]]
    ) -> Presentation:
        """Apply edit operations to an existing presentation."""
        for op in operations:
            action = op.get("action", "")
            try:
                if action == "add_slide":
                    self._edit_add_slide(prs, op)
                elif action == "remove_slide":
                    self._edit_remove_slide(prs, op)
                elif action == "move_slide":
                    self._edit_move_slide(prs, op)
                elif action == "update_slide":
                    self._edit_update_slide(prs, op)
                elif action == "update_text":
                    self._edit_update_text(prs, op)
                elif action == "replace_image":
                    self._edit_replace_image(prs, op)
                elif action == "update_chart":
                    self._edit_update_chart(prs, op)
                elif action == "update_table":
                    self._edit_update_table(prs, op)
                elif action == "restyle_deck":
                    self._edit_restyle_deck(prs, op)
                else:
                    logger.warning("Unknown edit action: %s", action)
            except Exception as exc:
                logger.warning("Edit operation %s failed: %s", action, exc)

        return prs

    def extract_structure(self, prs: Presentation) -> dict[str, Any]:
        """Extract deck structure for edit planning."""
        slides = []
        for idx, slide in enumerate(prs.slides, 1):
            shapes_info = []
            for shape in slide.shapes:
                info: dict[str, Any] = {
                    "name": shape.name,
                    "type": str(shape.shape_type),
                }
                if shape.has_text_frame:
                    info["text"] = shape.text_frame.text[:500]
                if shape.has_chart:
                    info["chart_type"] = str(shape.chart.chart_type)
                if shape.has_table:
                    tbl = shape.table
                    info["table_rows"] = tbl.rows.__len__()
                    info["table_cols"] = tbl.columns.__len__()
                shapes_info.append(info)

            notes_text = ""
            if slide.has_notes_slide:
                notes_text = slide.notes_slide.notes_text_frame.text[:500]

            layout_name = "unknown"
            if slide.slide_layout:
                layout_name = slide.slide_layout.name

            slides.append(
                {
                    "slide_number": idx,
                    "layout": layout_name,
                    "title": slide.shapes.title.text if slide.shapes.title else "",
                    "shapes": shapes_info,
                    "speaker_notes": notes_text,
                }
            )

        return {
            "slide_count": len(slides),
            "slides": slides,
        }

    # ── Private: Add slide ────────────────────────────────────────────

    def _add_slide(
        self, prs: Presentation, slide_def: dict[str, Any], theme: dict[str, Any]
    ) -> None:
        """Add a single slide from a slide definition."""
        layout_name = slide_def.get("layout", "content")

        # First try: exact match by template layout name (LLM should use these)
        matched_layout = None
        for layout in prs.slide_layouts:
            if layout.name == layout_name:
                matched_layout = layout
                break

        # Second try: case-insensitive match
        if matched_layout is None:
            layout_lower = layout_name.lower().strip()
            for layout in prs.slide_layouts:
                if layout.name.lower().strip() == layout_lower:
                    matched_layout = layout
                    break

        # Third try: legacy _LAYOUT_MAP index
        if matched_layout is None:
            layout_idx = _LAYOUT_MAP.get(layout_name, 1)
            if layout_idx >= len(prs.slide_layouts):
                layout_idx = 1
            matched_layout = prs.slide_layouts[layout_idx]

        slide = prs.slides.add_slide(matched_layout)

        # Title
        title_text = _safe_text(slide_def.get("title"))
        if title_text and slide.shapes.title:
            slide.shapes.title.text = title_text
            self._style_text_frame(
                slide.shapes.title.text_frame,
                font_size=theme.get("font_size_title", 28),
                font_family=theme.get("font_family", "Calibri"),
                color_hex=theme.get("text_color"),
            )

        # Subtitle (title_slide layout)
        subtitle = _safe_text(slide_def.get("subtitle"))
        if subtitle and layout_name == "title_slide":
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 1:
                    ph.text = subtitle
                    self._style_text_frame(
                        ph.text_frame,
                        font_size=20,
                        font_family=theme.get("font_family", "Calibri"),
                        color_hex=theme.get("text_color"),
                    )
                    break

        # ── Template-guided assignments ────────────────────────────────
        assignments = slide_def.get("assignments", {})
        if assignments:
            self._apply_assignments(slide, assignments, theme)
        else:
            # ── Free-flow content (legacy) ─────────────────────────────
            # Check for code_chart or flow_diagram in any content slot
            for slot in ("content", "left_content", "right_content", "image"):
                slot_content = slide_def.get(slot, {})
                if isinstance(slot_content, dict):
                    content_type = slot_content.get("type", "")
                    if content_type == "code_chart" and slot_content.get("chart_bytes"):
                        self._embed_prebuilt_chart(slide, slot_content, theme)
                    elif content_type == "flow_diagram":
                        self._add_flow_diagram_content(slide, slot_content, theme)

            if layout_name == "two_content":
                self._add_two_content(slide, slide_def, theme)
            elif layout_name == "content_with_image":
                self._add_content_with_image(slide, slide_def, theme)
            elif layout_name == "content_with_chart":
                self._add_content_with_chart(slide, slide_def, theme)
            elif layout_name == "table_slide":
                self._add_table_slide(slide, slide_def, theme)
            elif layout_name == "content":
                # If slide has a table, use table handler instead of body content
                if slide_def.get("table"):
                    self._add_table_slide(slide, slide_def, theme)
                elif slide_def.get("chart"):
                    self._add_content_with_chart(slide, slide_def, theme)
                else:
                    self._add_content(slide, slide_def, theme)
            elif layout_name == "blank":
                self._add_blank_content(slide, slide_def, theme)

        # Background
        bg = slide_def.get("background") or {}
        bg_color = bg.get("color") or theme.get("background_color") or theme.get(
            "background"
        )
        if bg_color:
            try:
                slide.background.fill.solid()
                slide.background.fill.fore_color.rgb = _hex_to_rgb(bg_color)
            except Exception as exc:
                logger.warning("Failed to apply slide background color %s: %s", bg_color, exc)

        # Speaker notes
        notes = _safe_text(slide_def.get("speaker_notes"))
        if notes:
            notes_slide = slide.notes_slide
            notes_slide.notes_text_frame.text = notes

    # ── Private: Content types ────────────────────────────────────────

    def _add_content(self, slide, slide_def: dict, theme: dict) -> None:
        """Add bullets/paragraph content to a standard content slide."""
        content = slide_def.get("content", {})
        body_ph = None
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                body_ph = ph
                break
        if not body_ph:
            return

        self._populate_text_content(body_ph, content, theme)

    def _add_two_content(self, slide, slide_def: dict, theme: dict) -> None:
        """Add content to both sides of a two-column layout."""
        left = slide_def.get("left_content", {})
        right = slide_def.get("right_content", {})

        placeholders = list(slide.placeholders)
        left_ph = None
        right_ph = None
        for ph in placeholders:
            idx = ph.placeholder_format.idx
            if idx == 1:
                left_ph = ph
            elif idx == 2:
                right_ph = ph

        if left_ph and left:
            if left.get("type") == "image":
                self._add_image_to_placeholder(slide, left_ph, left, theme)
            else:
                self._populate_text_content(left_ph, left, theme)

        if right_ph and right:
            if right.get("type") == "image":
                self._add_image_to_placeholder(slide, right_ph, right, theme)
            else:
                self._populate_text_content(right_ph, right, theme)

    def _add_content_with_image(self, slide, slide_def: dict, theme: dict) -> None:
        """Add content on left, image on right."""
        content = slide_def.get("content", {})
        image_def = slide_def.get("image", {})

        # Use body placeholder for content
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                self._populate_text_content(ph, content, theme)
                break

        # Add image at specified position
        if image_def:
            source = image_def.get("source", {})
            placement = image_def.get("placement", {})
            if source.get("image_bytes"):
                try:
                    slide.shapes.add_picture(
                        __import__("io").BytesIO(source["image_bytes"]),
                        left=Inches(placement.get("x_inches", 6)),
                        top=Inches(placement.get("y_inches", 1.5)),
                        width=Inches(placement.get("width_inches", 6)),
                        height=Inches(placement.get("height_inches", 4)),
                    )
                except Exception as exc:
                    logger.warning("Failed to add image: %s", exc)

    def _add_content_with_chart(self, slide, slide_def: dict, theme: dict) -> None:
        """Add a chart to the slide."""
        chart_def = slide_def.get("chart", slide_def.get("content", {}))
        if not chart_def or chart_def.get("type") != "chart":
            chart_def = slide_def.get("content", {})
        if chart_def.get("type") == "chart":
            chart_def = chart_def

        # For content_with_chart, remove the body placeholder and add chart
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                sp = ph._element
                sp.getparent().remove(sp)
                break

        chart_type_str = chart_def.get("chart_type", "column_clustered")
        chart_type = _CHART_TYPE_MAP.get(chart_type_str, XL_CHART_TYPE.COLUMN_CLUSTERED)
        data = chart_def.get("data", {})

        chart_data = ChartData()
        chart_data.categories = data.get("categories", [])
        for series in data.get("series", []):
            chart_data.add_series(series.get("name", ""), series.get("values", []))

        chart_frame = slide.shapes.add_chart(
            chart_type,
            Inches(1),
            Inches(1.5),
            Inches(11),
            Inches(5),
            chart_data,
        )
        chart = chart_frame.chart
        chart.has_legend = chart_def.get("style", {}).get("has_legend", True)

        if chart_def.get("style", {}).get("data_labels"):
            plot = chart.plots[0]
            plot.has_data_labels = True

    def _add_table_slide(self, slide, slide_def: dict, theme: dict) -> None:
        """Add a styled table to the slide."""
        table_def = slide_def.get("table", {})
        if not table_def:
            return

        # Remove body placeholder
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                sp = ph._element
                sp.getparent().remove(sp)
                break

        headers = table_def.get("headers", [])
        rows = table_def.get("rows", [])
        style = table_def.get("style", {})

        if not headers:
            return

        cols = len(headers)
        data_rows = len(rows)

        tbl_shape = slide.shapes.add_table(
            data_rows + 1,
            cols,
            Inches(0.8),
            Inches(1.5),
            Inches(11.5),
            Inches(0.4 * (data_rows + 1)),
        )
        table = tbl_shape.table

        # Header row
        for col_idx, header in enumerate(headers):
            cell = table.cell(0, col_idx)
            cell.text = str(header)
            self._style_cell(
                cell,
                bold=True,
                font_size=12,
                fill_hex=style.get("header_bg", "#1a1a2e"),
                text_hex=style.get("header_text", "#ffffff"),
            )

        # Data rows
        for row_idx, row_data in enumerate(rows):
            for col_idx, value in enumerate(row_data):
                if col_idx < cols:
                    cell = table.cell(row_idx + 1, col_idx)
                    cell.text = str(value)
                    fill = style.get("row_alt_bg") if row_idx % 2 == 1 else None
                    self._style_cell(cell, font_size=11, fill_hex=fill)

        # Set column widths evenly
        col_width = Inches(11.5 / cols)
        for col in table.columns:
            col.width = col_width

    def _add_blank_content(self, slide, slide_def: dict, theme: dict) -> None:
        """Add content to a blank slide (custom positioning)."""
        content = slide_def.get("content", {})
        if content.get("items"):
            from pptx.util import Inches, Pt

            txBox = slide.shapes.add_textbox(
                Inches(1), Inches(1.5), Inches(11), Inches(5)
            )
            tf = txBox.text_frame
            tf.word_wrap = True
            for i, item in enumerate(content.get("items", [])):
                if i == 0:
                    tf.text = str(item)
                else:
                    p = tf.add_paragraph()
                    p.text = str(item)
                    p.level = content.get("level", 0)
            self._style_text_frame(
                tf,
                font_size=theme.get("font_size_body", 16),
                font_family=theme.get("font_family", "Calibri"),
                color_hex=theme.get("text_color"),
            )

    # ── Private: Edit operations ──────────────────────────────────────

    def _edit_add_slide(self, prs: Presentation, op: dict) -> None:
        after = op.get("after_slide", len(prs.slides))
        layout_name = op.get("layout", "content")

        slide_def: dict[str, Any] = {
            "layout": layout_name,
            "title": op.get("title", ""),
            "subtitle": op.get("subtitle", ""),
            "speaker_notes": op.get("speaker_notes", ""),
        }
        # Forward template-guided assignments (preferred) or legacy content
        if op.get("assignments"):
            slide_def["assignments"] = op["assignments"]
        if op.get("content"):
            slide_def["content"] = op["content"]
        # Forward other legacy fields for backward compatibility
        for legacy_key in ("left_content", "right_content", "image", "chart", "table"):
            if op.get(legacy_key):
                slide_def[legacy_key] = op[legacy_key]

        # python-pptx doesn't support insert at position natively.
        # We add at the end and reorder.
        self._add_slide(prs, slide_def, op.get("theme", {}))

        # Move to correct position
        if after < len(prs.slides) - 1:
            slides = list(prs.slides._sldIdLst)
            new_slide = slides[-1]
            slides.pop()
            slides.insert(after, new_slide)
            prs.slides._sldIdLst[:] = slides

    def _edit_remove_slide(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        if 1 <= slide_num <= len(prs.slides):
            slide = prs.slides[slide_num - 1]
            sp = slide._element
            sp.getparent().remove(sp)

    def _edit_move_slide(self, prs: Presentation, op: dict) -> None:
        from_pos = op.get("from", 1) - 1
        to_pos = op.get("to", 1) - 1
        slides = list(prs.slides._sldIdLst)
        if 0 <= from_pos < len(slides) and 0 <= to_pos < len(slides):
            slide = slides.pop(from_pos)
            slides.insert(to_pos, slide)
            prs.slides._sldIdLst[:] = slides

    def _edit_update_slide(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        changes = op.get("changes", {})
        if not (1 <= slide_num <= len(prs.slides)):
            return
        slide = prs.slides[slide_num - 1]

        # Update title
        if "title" in changes and slide.shapes.title:
            slide.shapes.title.text = str(changes["title"])
            self._style_text_frame(
                slide.shapes.title.text_frame,
                font_size=28,
                font_family="Calibri",
            )

        # Update subtitle (placeholder idx 1 on title layouts)
        if "subtitle" in changes:
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 1:
                    ph.text = _safe_text(changes["subtitle"])
                    self._style_text_frame(ph.text_frame, font_size=20, font_family="Calibri")
                    break

        # Template-guided assignment updates
        if "assignments" in changes:
            self._apply_assignments(slide, changes["assignments"], changes.get("theme", {}))

        # Speaker notes
        if "speaker_notes" in changes:
            notes_text = _safe_text(changes["speaker_notes"])
            if notes_text:
                notes_slide = slide.notes_slide
                notes_slide.notes_text_frame.text = notes_text

    def _edit_update_text(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        shape_name = op.get("shape_name", "")
        new_text = op.get("text", "")
        if not (1 <= slide_num <= len(prs.slides)):
            return
        slide = prs.slides[slide_num - 1]
        for shape in slide.shapes:
            if shape.name == shape_name and shape.has_text_frame:
                shape.text_frame.text = str(new_text)
                return

    def _edit_replace_image(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        shape_name = op.get("shape_name", "")
        new_image = op.get("new_image", {})
        if not (1 <= slide_num <= len(prs.slides)):
            return
        if not new_image.get("image_bytes"):
            return
        slide = prs.slides[slide_num - 1]
        for shape in slide.shapes:
            if shape.name == shape_name:
                # Remove old shape, add new image at same position
                left, top, width, height = (
                    shape.left,
                    shape.top,
                    shape.width,
                    shape.height,
                )
                sp = shape._element
                sp.getparent().remove(sp)
                import io

                slide.shapes.add_picture(
                    io.BytesIO(new_image["image_bytes"]),
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                )
                return

    def _edit_update_chart(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        shape_name = op.get("shape_name", "")
        new_data = op.get("new_data", {})
        if not (1 <= slide_num <= len(prs.slides)):
            return
        slide = prs.slides[slide_num - 1]
        for shape in slide.shapes:
            if shape.name == shape_name and shape.has_chart:
                chart_data = ChartData()
                chart_data.categories = new_data.get("categories", [])
                for series in new_data.get("series", []):
                    chart_data.add_series(
                        series.get("name", ""), series.get("values", [])
                    )
                shape.chart.replace_data(chart_data)
                return

    def _edit_update_table(self, prs: Presentation, op: dict) -> None:
        slide_num = op.get("slide_number", 1)
        shape_name = op.get("shape_name", "")
        new_rows = op.get("new_rows", [])
        if not (1 <= slide_num <= len(prs.slides)):
            return
        slide = prs.slides[slide_num - 1]
        for shape in slide.shapes:
            if shape.name == shape_name and shape.has_table:
                table = shape.table
                # Clear existing data rows (skip header)
                for row_idx in range(1, len(table.rows)):
                    for cell in table.rows[row_idx].cells:
                        cell.text = ""
                # Fill new data
                for row_idx, row_data in enumerate(new_rows):
                    actual_row = row_idx + 1
                    if actual_row < len(table.rows):
                        for col_idx, value in enumerate(row_data):
                            if col_idx < len(table.columns):
                                table.cell(actual_row, col_idx).text = str(value)
                return

    def _edit_restyle_deck(self, prs: Presentation, op: dict) -> None:
        """Restyle deck by applying theme changes to all slides.

        Changes background colors, font colors, and accent colors across all slides.
        For full template swap, use build_deck() with extracted content instead.
        """
        theme = op.get("theme", {})
        if not theme:
            return

        primary_color = theme.get("primary_color")
        text_color = theme.get("text_color")
        font_family = theme.get("font_family")

        for slide in prs.slides:
            # Apply background
            if primary_color:
                try:
                    slide.background.fill.solid()
                    slide.background.fill.fore_color.rgb = _hex_to_rgb(primary_color)
                except Exception as exc:
                    logger.warning(
                        "Failed to restyle slide background color %s: %s",
                        primary_color,
                        exc,
                    )

            # Apply text color to all text shapes
            if text_color or font_family:
                safe_font = _enforce_system_font(font_family) if font_family else None
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for p in shape.text_frame.paragraphs:
                            for run in p.runs:
                                if text_color:
                                    try:
                                        run.font.color.rgb = _hex_to_rgb(text_color)
                                    except Exception:
                                        pass
                                if safe_font:
                                    run.font.name = safe_font

    def apply_transition(
        self, slide, *, transition_type: str = "fade", speed: str = "med"
    ) -> None:
        """Apply a slide transition via XML workaround.

        Supported types: fade, push, wipe, split, zoom
        Supported speed: slow, med, fast
        """
        from pptx.oxml import parse_xml

        # Build the transition XML
        speed_map = {"slow": "slow", "med": "med", "fast": "fast"}
        spd = speed_map.get(speed, "med")

        transition_xml = f'''<p:transition xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" spd="{spd}"><p:{transition_type} /></p:transition>'''

        try:
            transition_element = parse_xml(transition_xml)
            # Insert before the slide's timing element or at end
            slide.element.append(transition_element)
        except Exception as exc:
            logger.debug("Failed to apply transition: %s", exc)

    def embed_chart_image(
        self,
        slide,
        image_bytes: bytes,
        *,
        x_inches: float = 1,
        y_inches: float = 1.5,
        width_inches: float = 8,
        height_inches: float = 4.5,
    ) -> None:
        """Embed a matplotlib-generated chart image into a slide."""
        import io

        try:
            slide.shapes.add_picture(
                io.BytesIO(image_bytes),
                left=Inches(x_inches),
                top=Inches(y_inches),
                width=Inches(width_inches),
                height=Inches(height_inches),
            )
        except Exception as exc:
            logger.warning("Failed to embed chart image: %s", exc)

    # ── Private: Text content population ──────────────────────────────

    def _populate_text_content(self, placeholder, content: dict, theme: dict) -> None:
        """Populate a placeholder with bullets, numbered, or paragraph content."""
        content_type = content.get("type", "bullets")
        items = content.get("items", [])

        if not items and content.get("text"):
            items = [content["text"]]

        tf = placeholder.text_frame
        tf.clear()
        tf.word_wrap = True

        for i, item in enumerate(items):
            if i == 0:
                p = tf.paragraphs[0]
            else:
                p = tf.add_paragraph()
            p.text = str(item)

            if content_type == "numbered":
                p.level = 0
            elif content_type == "bullets":
                p.level = content.get("level", 0)

        self._style_text_frame(
            tf,
            font_size=theme.get("font_size_body", 16),
            font_family=theme.get("font_family", "Calibri"),
            color_hex=theme.get("text_color"),
        )

    def _apply_assignments(self, slide, assignments: dict, theme: dict) -> None:
        """Populate placeholders by idx based on template-guided assignments.

        assignments format: {"0": {"type": "title", "text": "..."}, "1": {"type": "body", "items": [...]}, ...}
        The key is the placeholder idx as a string.
        """
        for ph in slide.placeholders:
            idx_str = str(ph.placeholder_format.idx)
            if idx_str not in assignments:
                continue

            assignment = assignments[idx_str]
            a_type = assignment.get("type", "body")

            try:
                if a_type == "title":
                    ph.text = _safe_text(assignment.get("text", ""))
                    self._style_text_frame(
                        ph.text_frame,
                        font_size=theme.get("font_size_title", 28),
                        font_family=theme.get("font_family", "Calibri"),
                        color_hex=theme.get("text_color"),
                    )

                elif a_type == "subtitle":
                    ph.text = _safe_text(assignment.get("text", ""))
                    self._style_text_frame(
                        ph.text_frame,
                        font_size=20,
                        font_family=theme.get("font_family", "Calibri"),
                        color_hex=theme.get("text_color"),
                    )

                elif a_type in ("body", "bullets", "numbered", "paragraph"):
                    self._populate_text_content(ph, assignment, theme)

                elif a_type == "image":
                    source = assignment.get("source", {})
                    if source.get("image_bytes"):
                        import io

                        ph.insert_picture(io.BytesIO(source["image_bytes"]))

                elif a_type == "chart":
                    from pptx.chart.data import ChartData

                    chart_type_str = assignment.get("chart_type", "column_clustered")
                    chart_type = _CHART_TYPE_MAP.get(
                        chart_type_str, XL_CHART_TYPE.COLUMN_CLUSTERED
                    )
                    data = assignment.get("data", {})
                    chart_data = ChartData()
                    chart_data.categories = data.get("categories", [])
                    for s in data.get("series", []):
                        chart_data.add_series(s.get("name", ""), s.get("values", []))
                    ph.insert_chart(chart_type, chart_data)

                elif a_type == "table":
                    headers = assignment.get("headers", [])
                    rows = assignment.get("rows", [])
                    if headers:
                        n_cols = len(headers)
                        n_rows = len(rows) + 1
                        gf = ph.insert_table(n_rows, n_cols)
                        tbl = gf.table
                        for ci, h in enumerate(headers):
                            tbl.cell(0, ci).text = str(h)
                        for ri, row_data in enumerate(rows):
                            for ci, val in enumerate(row_data):
                                if ci < n_cols:
                                    tbl.cell(ri + 1, ci).text = str(val)

                elif a_type == "code_chart" and assignment.get("chart_bytes"):
                    import io

                    ph.insert_picture(io.BytesIO(assignment["chart_bytes"]))

            except Exception as exc:
                logger.warning(
                    "Failed to populate placeholder idx=%s type=%s: %s",
                    idx_str,
                    a_type,
                    exc,
                )

    def _embed_prebuilt_chart(self, slide, content: dict, theme: dict) -> None:
        """Embed a prebuilt chart image (from code sandbox) into a slide."""
        chart_bytes = content.get("chart_bytes", b"")
        if not chart_bytes:
            return
        placement = content.get("placement", {})
        self.embed_chart_image(
            slide,
            chart_bytes,
            x_inches=placement.get("x_inches", 1),
            y_inches=placement.get("y_inches", 1.5),
            width_inches=placement.get("width_inches", 10),
            height_inches=placement.get("height_inches", 5.5),
        )

    def _add_flow_diagram_content(self, slide, content: dict, theme: dict) -> None:
        """Add a flow diagram to the slide."""
        boxes = content.get("boxes", [])
        if not boxes:
            return

        position = content.get("position", {})
        box_size = content.get("box_size", {})

        # Remove body placeholder if present
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                sp = ph._element
                sp.getparent().remove(sp)
                break

        self.add_flow_diagram(
            slide,
            boxes=boxes,
            direction=content.get("direction", "horizontal"),
            start_x_inches=position.get("x_inches", 1),
            start_y_inches=position.get("y_inches", 2.5),
            box_width_inches=box_size.get("width", 2.5),
            box_height_inches=box_size.get("height", 1),
            gap_inches=content.get("gap", 0.8),
            fill_hex=content.get("fill", "#4472C4"),
            text_color_hex=content.get("text_color", "#ffffff"),
            arrow_color_hex=content.get("arrow_color", "#333333"),
            font_size=content.get("font_size", 11),
            connector_type=content.get("connector_type", "straight"),
        )

    def _add_image_to_placeholder(
        self, slide, placeholder, image_def: dict, theme: dict
    ) -> None:
        """Add an image to a placeholder position."""
        source = image_def.get("source", {})
        if source.get("image_bytes"):
            try:
                import io

                placement = image_def.get("placement", {})
                # Remove placeholder, add image at same position
                left, top = placeholder.left, placeholder.top
                width = Inches(placement.get("width_inches", 5))
                height = Inches(placement.get("height_inches", 4))
                sp = placeholder._element
                sp.getparent().remove(sp)
                slide.shapes.add_picture(
                    io.BytesIO(source["image_bytes"]),
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                )
            except Exception as exc:
                logger.warning("Failed to add image to placeholder: %s", exc)

    # ── Private: Styling helpers ──────────────────────────────────────

    def _style_text_frame(
        self,
        tf,
        *,
        font_size: int = 16,
        font_family: str = "Calibri",
        color_hex: str | None = None,
    ) -> None:
        """Apply consistent styling to a text frame."""
        safe_font = _enforce_system_font(font_family)
        for p in tf.paragraphs:
            for run in p.runs:
                run.font.size = Pt(font_size)
                run.font.name = safe_font
                if color_hex:
                    try:
                        run.font.color.rgb = _hex_to_rgb(color_hex)
                    except Exception:
                        pass

    def _style_cell(
        self,
        cell,
        *,
        bold: bool = False,
        font_size: int = 11,
        fill_hex: str | None = None,
        text_hex: str | None = None,
    ) -> None:
        """Style a table cell."""
        for p in cell.text_frame.paragraphs:
            for run in p.runs:
                run.font.size = Pt(font_size)
                run.font.bold = bold
                if text_hex:
                    try:
                        run.font.color.rgb = _hex_to_rgb(text_hex)
                    except Exception:
                        pass
        if fill_hex:
            try:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _hex_to_rgb(fill_hex)
            except Exception:
                pass

    # ── Shape and connector support ────────────────────────────────────

    def add_shape(
        self,
        slide,
        *,
        shape_type: str = "rectangle",
        x_inches: float = 1,
        y_inches: float = 1,
        width_inches: float = 2,
        height_inches: float = 1,
        text: str = "",
        fill_hex: str | None = None,
        border_hex: str | None = None,
        border_width_pt: float = 1.5,
        font_size: int = 12,
        font_bold: bool = False,
        text_color_hex: str | None = None,
        text_align: str = "center",
    ) -> Any:
        """Add a shape (rectangle, rounded_rectangle, ellipse, etc.) to a slide.

        Returns the created shape object.
        """
        from pptx.enum.shapes import MSO_SHAPE

        shape_map = {
            "rectangle": MSO_SHAPE.RECTANGLE,
            "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
            "ellipse": MSO_SHAPE.OVAL,
            "circle": MSO_SHAPE.OVAL,
            "diamond": MSO_SHAPE.DIAMOND,
            "hexagon": MSO_SHAPE.HEXAGON,
            "chevron": MSO_SHAPE.CHEVRON,
            "arrow": MSO_SHAPE.RIGHT_ARROW,
            "arrow_right": MSO_SHAPE.RIGHT_ARROW,
            "arrow_left": MSO_SHAPE.LEFT_ARROW,
            "arrow_up": MSO_SHAPE.UP_ARROW,
            "arrow_down": MSO_SHAPE.DOWN_ARROW,
            "star": MSO_SHAPE.STAR_5_POINT,
            "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
        }

        mso_shape = shape_map.get(shape_type, MSO_SHAPE.RECTANGLE)

        shape = slide.shapes.add_shape(
            mso_shape,
            Inches(x_inches),
            Inches(y_inches),
            Inches(width_inches),
            Inches(height_inches),
        )

        # Fill
        if fill_hex:
            shape.fill.solid()
            shape.fill.fore_color.rgb = _hex_to_rgb(fill_hex)
        else:
            shape.fill.background()

        # Border
        if border_hex:
            shape.line.color.rgb = _hex_to_rgb(border_hex)
            shape.line.width = Pt(border_width_pt)
        else:
            shape.line.fill.background()

        # Text
        if text:
            tf = shape.text_frame
            tf.word_wrap = True
            tf.text = text

            # Vertical center
            try:
                tf.paragraphs[0].alignment = {
                    "left": PP_ALIGN.LEFT,
                    "center": PP_ALIGN.CENTER,
                    "right": PP_ALIGN.RIGHT,
                }.get(text_align, PP_ALIGN.CENTER)
            except Exception:
                pass

            tf.paragraphs[0].font.size = Pt(font_size)
            tf.paragraphs[0].font.bold = font_bold
            if text_color_hex:
                try:
                    tf.paragraphs[0].font.color.rgb = _hex_to_rgb(text_color_hex)
                except Exception:
                    pass

        return shape

    def add_connector(
        self,
        slide,
        *,
        from_shape,
        to_shape,
        connector_type: str = "straight",
        color_hex: str = "#333333",
        width_pt: float = 1.5,
        arrow_end: bool = True,
    ) -> Any:
        """Add a connector/arrow between two shapes.

        Returns the connector shape object.
        """
        from pptx.enum.shapes import MSO_CONNECTOR

        connector_map = {
            "straight": MSO_CONNECTOR.STRAIGHT,
            "elbow": MSO_CONNECTOR.ELBOW,
            "curved": MSO_CONNECTOR.CURVE,
        }

        mso_connector = connector_map.get(connector_type, MSO_CONNECTOR.STRAIGHT)

        # Calculate connection points
        from_x = from_shape.left + from_shape.width
        from_y = from_shape.top + from_shape.height // 2
        to_x = to_shape.left
        to_y = to_shape.top + to_shape.height // 2

        connector = slide.shapes.add_connector(
            mso_connector,
            from_x,
            from_y,
            to_x,
            to_y,
        )

        connector.line.color.rgb = _hex_to_rgb(color_hex)
        connector.line.width = Pt(width_pt)

        if arrow_end:
            connector.end_x = to_x
            connector.end_y = to_y

        return connector

    def add_flow_diagram(
        self,
        slide,
        *,
        boxes: list[dict[str, Any]],
        direction: str = "horizontal",
        start_x_inches: float = 1,
        start_y_inches: float = 2,
        box_width_inches: float = 2.5,
        box_height_inches: float = 1,
        gap_inches: float = 0.8,
        fill_hex: str = "#4472C4",
        text_color_hex: str = "#ffffff",
        arrow_color_hex: str = "#333333",
        font_size: int = 11,
        connector_type: str = "straight",
    ) -> list[Any]:
        """Create a flow diagram with connected boxes.

        boxes: list of {"text": "Step 1", "fill": "#hex" (optional)}
        direction: "horizontal" or "vertical"
        Returns list of created shape objects.
        """
        shapes = []
        x = start_x_inches
        y = start_y_inches

        for i, box_def in enumerate(boxes):
            box_fill = box_def.get("fill", fill_hex)
            box_text = box_def.get("text", "")

            shape = self.add_shape(
                slide,
                shape_type=box_def.get("shape", "rounded_rectangle"),
                x_inches=x,
                y_inches=y,
                width_inches=box_width_inches,
                height_inches=box_height_inches,
                text=box_text,
                fill_hex=box_fill,
                text_color_hex=text_color_hex,
                font_size=font_size,
                font_bold=True,
            )
            shapes.append(shape)

            # Add connector to previous shape
            if i > 0:
                self.add_connector(
                    slide,
                    from_shape=shapes[i - 1],
                    to_shape=shape,
                    connector_type=connector_type,
                    color_hex=arrow_color_hex,
                    arrow_end=True,
                )

            # Advance position
            if direction == "horizontal":
                x += box_width_inches + gap_inches
            else:
                y += box_height_inches + gap_inches

        return shapes


def export_to_pdf(
    pptx_path: Path,
    *,
    libreoffice_path: str = "soffice",
    output_dir: Path | None = None,
) -> Path | None:
    """Export PPTX to PDF via LibreOffice headless. Returns PDF path or None."""
    if output_dir is None:
        output_dir = pptx_path.parent

    try:
        result = subprocess.run(
            [
                libreoffice_path,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(pptx_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("LibreOffice PDF export failed: %s", result.stderr)
            return None

        pdf_path = output_dir / f"{pptx_path.stem}.pdf"
        if pdf_path.exists():
            return pdf_path
        return None
    except Exception as exc:
        logger.warning("PDF export error: %s", exc)
        return None


def render_slides_to_png(
    pptx_path: Path,
    *,
    libreoffice_path: str = "soffice",
    pdftoppm_path: str = "pdftoppm",
    output_dir: Path | None = None,
    dpi: int = 200,
) -> list[Path]:
    """Render each slide as a PNG via LibreOffice + pdftoppm. Returns list of PNG paths."""
    if output_dir is None:
        output_dir = pptx_path.parent / "slides_preview"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Convert to PDF
        subprocess.run(
            [
                libreoffice_path,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(pptx_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        pdf_path = output_dir / f"{pptx_path.stem}.pdf"
        if not pdf_path.exists():
            logger.warning("PDF not created for slide rendering")
            return []

        # Step 2: Convert PDF to PNG
        png_prefix = output_dir / "slide"
        subprocess.run(
            [
                pdftoppm_path,
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(png_prefix),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        pngs = sorted(output_dir.glob("slide-*.png"))
        return pngs

    except Exception as exc:
        logger.warning("Slide rendering error: %s", exc)
        return []
