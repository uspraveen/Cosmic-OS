"""Layout engine — deterministic bounds, overlap detection, auto-spacing.

Acts as a "physics engine" for slides:
- BoundingBox tracks every element's position and size
- Overlap detection (AABB intersection)
- Bounds checking (nothing overflows slide edges)
- Auto-spacing (minimum gaps between elements)
- Density analysis (overcrowded slide detection)
- Auto-layout (grid, stack, flow)
- Alignment helpers (center, distribute, justify)

Runs as a pre-build validation step BEFORE rendering and vision checks.
Catches layout issues deterministically without needing vision LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default slide dimensions (16:9 widescreen, inches)
DEFAULT_WIDTH = 13.333
DEFAULT_HEIGHT = 7.5

# Safe margins (inches) — content should stay within these
DEFAULT_MARGIN = 0.5
TITLE_SAFE_TOP = 0.4
FOOTER_SAFE_BOTTOM = 0.4

# Minimum gaps between elements (inches)
MIN_GAP_HORIZONTAL = 0.2
MIN_GAP_VERTICAL = 0.15


@dataclass
class BoundingBox:
    """Axis-aligned bounding box for a slide element."""

    x: float  # left edge, inches
    y: float  # top edge, inches
    width: float  # inches
    height: float  # inches
    label: str = ""  # human-readable label for diagnostics
    element_type: str = ""  # "text", "image", "chart", "table", "shape"

    @property
    def left(self) -> float:
        return self.x

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    @property
    def area(self) -> float:
        return self.width * self.height

    def overlaps(self, other: BoundingBox) -> bool:
        """Check if two bounding boxes overlap (AABB intersection)."""
        return (
            self.left < other.right
            and self.right > other.left
            and self.top < other.bottom
            and self.bottom > other.top
        )

    def gap_horizontal(self, other: BoundingBox) -> float:
        """Horizontal gap between two boxes. Negative = overlap."""
        if self.right <= other.left:
            return other.left - self.right
        elif other.right <= self.left:
            return self.left - other.right
        else:
            return -(min(self.right, other.right) - max(self.left, other.left))

    def gap_vertical(self, other: BoundingBox) -> float:
        """Vertical gap between two boxes. Negative = overlap."""
        if self.bottom <= other.top:
            return other.top - self.bottom
        elif other.bottom <= self.top:
            return self.top - other.bottom
        else:
            return -(min(self.bottom, other.bottom) - max(self.top, other.top))

    def contains(self, other: BoundingBox) -> bool:
        """Check if self fully contains other."""
        return (
            self.left <= other.left
            and self.right >= other.right
            and self.top <= other.top
            and self.bottom >= other.bottom
        )

    def intersection_area(self, other: BoundingBox) -> float:
        """Area of overlap between two boxes. 0 = no overlap."""
        if not self.overlaps(other):
            return 0.0
        overlap_w = min(self.right, other.right) - max(self.left, other.left)
        overlap_h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return max(0.0, overlap_w) * max(0.0, overlap_h)

    def intersection_ratio(self, other: BoundingBox) -> float:
        """Overlap as a fraction of the smaller box's area. 0 = no overlap."""
        overlap = self.intersection_area(other)
        smaller = min(self.area, other.area)
        if smaller <= 0:
            return 0.0
        return overlap / smaller


@dataclass
class SlideBounds:
    """Slide dimensions, margins, and safe zones."""

    width: float = DEFAULT_WIDTH
    height: float = DEFAULT_HEIGHT
    margin_left: float = DEFAULT_MARGIN
    margin_right: float = DEFAULT_MARGIN
    margin_top: float = TITLE_SAFE_TOP
    margin_bottom: float = FOOTER_SAFE_BOTTOM

    @property
    def safe_left(self) -> float:
        return self.margin_left

    @property
    def safe_right(self) -> float:
        return self.width - self.margin_right

    @property
    def safe_top(self) -> float:
        return self.margin_top

    @property
    def safe_bottom(self) -> float:
        return self.height - self.margin_bottom

    @property
    def safe_width(self) -> float:
        return self.safe_right - self.safe_left

    @property
    def safe_height(self) -> float:
        return self.safe_bottom - self.safe_top

    def to_bbox(self) -> BoundingBox:
        """Full slide bounds."""
        return BoundingBox(0, 0, self.width, self.height, label="slide")

    def safe_zone_bbox(self) -> BoundingBox:
        """Safe content area (inside margins)."""
        return BoundingBox(
            self.safe_left,
            self.safe_top,
            self.safe_width,
            self.safe_height,
            label="safe_zone",
        )


@dataclass
class LayoutIssue:
    """A single layout issue found during validation."""

    severity: str  # "error", "warning", "info"
    code: str  # "OVERLAP", "OUT_OF_BOUNDS", "TIGHT_SPACING", "HIGH_DENSITY", etc.
    message: str
    elements: list[str] = field(default_factory=list)  # labels of involved elements
    suggestion: str = ""


@dataclass
class LayoutReport:
    """Result of layout validation."""

    valid: bool  # True if no errors (warnings are OK)
    issues: list[LayoutIssue] = field(default_factory=list)
    density: float = 0.0  # 0-1, fraction of safe area covered
    element_count: int = 0

    @property
    def errors(self) -> list[LayoutIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LayoutIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def summary(self) -> str:
        if not self.issues:
            return "Layout OK — no issues found."
        lines = []
        for issue in self.issues:
            prefix = {"error": "ERROR", "warning": "WARN", "info": "INFO"}[
                issue.severity
            ]
            lines.append(f"[{prefix}] {issue.code}: {issue.message}")
            if issue.suggestion:
                lines.append(f"  → {issue.suggestion}")
        return "\n".join(lines)


class LayoutEngine:
    """Validate and auto-layout slide elements."""

    def __init__(
        self,
        slide_bounds: SlideBounds | None = None,
        min_gap_h: float = MIN_GAP_HORIZONTAL,
        min_gap_v: float = MIN_GAP_VERTICAL,
    ) -> None:
        self.bounds = slide_bounds or SlideBounds()
        self.min_gap_h = min_gap_h
        self.min_gap_v = min_gap_v

    def validate(
        self,
        elements: list[BoundingBox],
        *,
        title_present: bool = False,
    ) -> LayoutReport:
        """Run all layout checks on a list of elements.

        Returns a LayoutReport with errors, warnings, and suggestions.
        """
        issues: list[LayoutIssue] = []
        safe_zone = self.bounds.safe_zone_bbox()

        # Title area check
        if title_present:
            title_area = BoundingBox(
                self.bounds.safe_left,
                0,
                self.bounds.safe_width,
                self.bounds.margin_top + 0.3,
                label="title_area",
            )
            for elem in elements:
                if elem.label and "title" in elem.label.lower():
                    if not title_area.contains(elem):
                        issues.append(
                            LayoutIssue(
                                severity="warning",
                                code="TITLE_POSITION",
                                message=f"Title '{elem.label}' may be too far from top.",
                                elements=[elem.label],
                                suggestion=f"Move title to y <= {title_area.bottom:.1f} inches.",
                            )
                        )

        # 1. Out of bounds check
        for elem in elements:
            if elem.right > self.bounds.width:
                issues.append(
                    LayoutIssue(
                        severity="error",
                        code="OUT_OF_BOUNDS",
                        message=f"'{elem.label}' overflows right edge ({elem.right:.2f} > {self.bounds.width:.2f}).",
                        elements=[elem.label],
                        suggestion=f"Reduce width or move left. Max right = {self.bounds.width:.2f}.",
                    )
                )
            if elem.bottom > self.bounds.height:
                issues.append(
                    LayoutIssue(
                        severity="error",
                        code="OUT_OF_BOUNDS",
                        message=f"'{elem.label}' overflows bottom edge ({elem.bottom:.2f} > {self.bounds.height:.2f}).",
                        elements=[elem.label],
                        suggestion=f"Reduce height or move up. Max bottom = {self.bounds.height:.2f}.",
                    )
                )
            if elem.left < 0:
                issues.append(
                    LayoutIssue(
                        severity="error",
                        code="OUT_OF_BOUNDS",
                        message=f"'{elem.label}' extends past left edge ({elem.left:.2f} < 0).",
                        elements=[elem.label],
                    )
                )
            if elem.top < 0:
                issues.append(
                    LayoutIssue(
                        severity="error",
                        code="OUT_OF_BOUNDS",
                        message=f"'{elem.label}' extends past top edge ({elem.top:.2f} < 0).",
                        elements=[elem.label],
                    )
                )

            # Margin check (warning, not error)
            if elem.left < self.bounds.safe_left:
                issues.append(
                    LayoutIssue(
                        severity="warning",
                        code="INSIDE_MARGIN",
                        message=f"'{elem.label}' is inside left margin.",
                        elements=[elem.label],
                        suggestion=f"Move to x >= {self.bounds.safe_left:.2f}.",
                    )
                )
            if elem.right > self.bounds.safe_right:
                issues.append(
                    LayoutIssue(
                        severity="warning",
                        code="INSIDE_MARGIN",
                        message=f"'{elem.label}' is inside right margin.",
                        elements=[elem.label],
                        suggestion=f"Keep right <= {self.bounds.safe_right:.2f}.",
                    )
                )

        # 2. Overlap detection
        for i, a in enumerate(elements):
            for b in elements[i + 1 :]:
                if a.overlaps(b):
                    ratio = a.intersection_ratio(b)
                    severity = "error" if ratio > 0.3 else "warning"
                    issues.append(
                        LayoutIssue(
                            severity=severity,
                            code="OVERLAP",
                            message=f"'{a.label}' overlaps '{b.label}' ({ratio:.0%} overlap).",
                            elements=[a.label, b.label],
                            suggestion=f"Move one element by {max(abs(a.gap_horizontal(b)), abs(a.gap_vertical(b))) + self.min_gap_h:.2f} inches.",
                        )
                    )

        # 3. Tight spacing (elements too close but not overlapping)
        for i, a in enumerate(elements):
            for b in elements[i + 1 :]:
                h_gap = a.gap_horizontal(b)
                v_gap = a.gap_vertical(b)
                # Check if horizontally adjacent and too close
                if (
                    0 <= h_gap < self.min_gap_h
                    and abs(v_gap) < max(a.height, b.height) * 0.8
                ):
                    issues.append(
                        LayoutIssue(
                            severity="warning",
                            code="TIGHT_SPACING",
                            message=f"'{a.label}' and '{b.label}' are only {h_gap:.2f} inches apart horizontally (min: {self.min_gap_h}).",
                            elements=[a.label, b.label],
                            suggestion=f"Add at least {self.min_gap_h - h_gap:.2f} inches of horizontal gap.",
                        )
                    )
                # Check if vertically adjacent and too close
                if (
                    0 <= v_gap < self.min_gap_v
                    and abs(h_gap) < max(a.width, b.width) * 0.8
                ):
                    issues.append(
                        LayoutIssue(
                            severity="warning",
                            code="TIGHT_SPACING",
                            message=f"'{a.label}' and '{b.label}' are only {v_gap:.2f} inches apart vertically (min: {self.min_gap_v}).",
                            elements=[a.label, b.label],
                            suggestion=f"Add at least {self.min_gap_v - v_gap:.2f} inches of vertical gap.",
                        )
                    )

        # 4. Density check
        total_element_area = sum(e.area for e in elements)
        safe_area = safe_zone.area
        density = total_element_area / safe_area if safe_area > 0 else 0

        if density > 0.75:
            issues.append(
                LayoutIssue(
                    severity="error",
                    code="HIGH_DENSITY",
                    message=f"Slide is {density:.0%} full — too crowded.",
                    suggestion="Remove elements or split into multiple slides.",
                )
            )
        elif density > 0.55:
            issues.append(
                LayoutIssue(
                    severity="warning",
                    code="HIGH_DENSITY",
                    message=f"Slide is {density:.0%} full — consider simplifying.",
                    suggestion="Reduce element sizes or count for better readability.",
                )
            )

        # 5. Element count
        if len(elements) > 12:
            issues.append(
                LayoutIssue(
                    severity="warning",
                    code="TOO_MANY_ELEMENTS",
                    message=f"Slide has {len(elements)} elements — hard to read.",
                    suggestion="Consider splitting into multiple slides.",
                )
            )

        has_errors = any(i.severity == "error" for i in issues)

        return LayoutReport(
            valid=not has_errors,
            issues=issues,
            density=density,
            element_count=len(elements),
        )

    def auto_layout_grid(
        self,
        elements: list[BoundingBox],
        *,
        columns: int = 0,
        gap_h: float = 0.4,
        gap_v: float = 0.3,
    ) -> list[BoundingBox]:
        """Auto-layout elements in a grid within the safe zone.

        If columns=0, auto-detect based on element count.
        Modifies and returns the element list with updated positions.
        """
        if not elements:
            return elements

        n = len(elements)
        if columns <= 0:
            columns = max(1, min(4, int(n**0.5 + 0.5)))

        rows = (n + columns - 1) // columns
        cell_w = (self.bounds.safe_width - (columns - 1) * gap_h) / columns
        cell_h = (self.bounds.safe_height - (rows - 1) * gap_v) / rows

        for i, elem in enumerate(elements):
            col = i % columns
            row = i // columns
            elem.x = self.bounds.safe_left + col * (cell_w + gap_h)
            elem.y = self.bounds.safe_top + row * (cell_h + gap_v)
            # Fit element into cell
            scale = min(cell_w / elem.width, cell_h / elem.height, 1.0)
            elem.width = elem.width * scale
            elem.height = elem.height * scale

        return elements

    def auto_layout_stack(
        self,
        elements: list[BoundingBox],
        *,
        direction: str = "vertical",
        gap: float = 0.3,
        align: str = "center",
    ) -> list[BoundingBox]:
        """Stack elements vertically or horizontally within the safe zone."""
        if not elements:
            return elements

        if direction == "vertical":
            total_h = sum(e.height for e in elements) + (len(elements) - 1) * gap
            start_y = self.bounds.safe_top + (self.bounds.safe_height - total_h) / 2
            for elem in elements:
                elem.y = start_y
                if align == "center":
                    elem.x = (
                        self.bounds.safe_left
                        + (self.bounds.safe_width - elem.width) / 2
                    )
                elif align == "left":
                    elem.x = self.bounds.safe_left
                start_y += elem.height + gap
        else:
            total_w = sum(e.width for e in elements) + (len(elements) - 1) * gap
            start_x = self.bounds.safe_left + (self.bounds.safe_width - total_w) / 2
            for elem in elements:
                elem.x = start_x
                if align == "center":
                    elem.y = (
                        self.bounds.safe_top
                        + (self.bounds.safe_height - elem.height) / 2
                    )
                elif align == "top":
                    elem.y = self.bounds.safe_top
                start_x += elem.width + gap

        return elements

    def center_element(self, elem: BoundingBox) -> BoundingBox:
        """Center a single element on the slide."""
        elem.x = self.bounds.safe_left + (self.bounds.safe_width - elem.width) / 2
        elem.y = self.bounds.safe_top + (self.bounds.safe_height - elem.height) / 2
        return elem

    def distribute_evenly(
        self,
        elements: list[BoundingBox],
        *,
        direction: str = "horizontal",
    ) -> list[BoundingBox]:
        """Distribute elements evenly across the safe zone."""
        if len(elements) < 2:
            return elements

        if direction == "horizontal":
            total_w = sum(e.width for e in elements)
            available = self.bounds.safe_width - total_w
            gap = available / (len(elements) - 1) if len(elements) > 1 else 0
            x = self.bounds.safe_left
            for elem in elements:
                elem.x = x
                x += elem.width + gap
        else:
            total_h = sum(e.height for e in elements)
            available = self.bounds.safe_height - total_h
            gap = available / (len(elements) - 1) if len(elements) > 1 else 0
            y = self.bounds.safe_top
            for elem in elements:
                elem.y = y
                y += elem.height + gap

        return elements
