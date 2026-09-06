"""Curated font catalog for the slides agent.

Deliberate, scenario-driven font pairing — the thing that separates a
designed deck from a default one — needs a catalog the planner can actually
choose from, with honest metadata about how each face behaves:

- Every family here ships with Microsoft Office / Windows, so the NATIVE
  pptx renders true on the user's machine with zero font embedding.
- `preview` records how truthfully the BUILD VM renders the face:
    full       — a metric-compatible substitute is installed on the VM, so
                 Chromium/LibreOffice previews have (near-)identical widths
                 to the real face on Windows.
    approximate — the preview substitutes a different-width face; allow
                 ~10% layout slack and don't pick these for ultra-tight
                 layouts unless the brief demands the look.

Never add faces that don't ship with Office: without font embedding they
silently substitute on the user's machine and the deck breaks.
"""

from __future__ import annotations

from typing import Any

# family → metadata. `fallback` is the CSS stack tail; `substitute` is the
# metric-compatible (or closest) face installed on the build VM.
FONT_CATALOG: dict[str, dict[str, Any]] = {
    # ── Sans ──────────────────────────────────────────────────────────────
    "Segoe UI": {
        "category": "sans", "moods": ["corporate", "tech", "data", "minimal"],
        "weights": ["Light", "Semilight", "Regular", "Semibold", "Bold"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "Carlito",
        "note": "Windows' own UI voice; modern, neutral, extremely versatile.",
    },
    "Calibri": {
        "category": "sans", "moods": ["corporate", "tech", "friendly", "data"],
        "weights": ["Light", "Regular", "Bold"],
        "fallback": "Arial, sans-serif", "preview": "full",
        "substitute": "Carlito",
        "note": "Warm humanist sans; the safest modern corporate body face.",
    },
    "Candara": {
        "category": "sans", "moods": ["friendly", "casual"],
        "weights": ["Regular", "Bold"],
        "fallback": "Verdana, sans-serif", "preview": "approximate",
        "substitute": "Carlito",
        "note": "Open, airy sans with organic curves; softer decks.",
    },
    "Corbel": {
        "category": "sans", "moods": ["corporate", "editorial", "minimal"],
        "weights": ["Regular", "Bold"],
        "fallback": "Verdana, sans-serif", "preview": "approximate",
        "substitute": "Carlito",
        "note": "Screen-tuned humanist sans; clean and unexaggerated.",
    },
    "Franklin Gothic Medium": {
        "category": "sans", "moods": ["news", "editorial", "bold"],
        "weights": ["Medium"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "Liberation Sans",
        "note": "Classic newspaper display sans; headlines with authority. Single weight — do not request bold.",
        "avoid_bold": True,
    },
    "Tahoma": {
        "category": "sans", "moods": ["data", "compact", "tech"],
        "weights": ["Regular", "Bold"],
        "fallback": "Verdana, sans-serif", "preview": "approximate",
        "substitute": "DejaVu Sans",
        "note": "Tight, dense, highly legible at small sizes; good for dense tables.",
    },
    "Trebuchet MS": {
        "category": "sans", "moods": ["friendly", "web", "playful"],
        "weights": ["Regular", "Bold"],
        "fallback": "Verdana, sans-serif", "preview": "approximate",
        "substitute": "DejaVu Sans",
        "note": "Rounded web-era sans; approachable without being childish.",
    },
    "Verdana": {
        "category": "sans", "moods": ["data", "legibility", "tech"],
        "weights": ["Regular", "Bold"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "DejaVu Sans",
        "note": "Very wide and airy; unbeatable small-size legibility, hungry for space.",
    },
    "Arial": {
        "category": "sans", "moods": ["corporate", "neutral"],
        "weights": ["Regular", "Bold"],
        "fallback": "Helvetica, sans-serif", "preview": "full",
        "substitute": "Liberation Sans",
        "note": "The universal neutral. Acceptable; rarely memorable — prefer a more deliberate face.",
    },
    "Arial Black": {
        "category": "display", "moods": ["impact", "poster", "sport"],
        "weights": ["Black"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "Liberation Sans",
        "note": "Heavy poster display face for cover statements. Single weight — never request bold.",
        "avoid_bold": True,
    },
    "Impact": {
        "category": "display", "moods": ["poster", "shout", "sport"],
        "weights": ["Regular"],
        "fallback": "Arial Black, Arial, sans-serif", "preview": "approximate",
        "substitute": "Liberation Sans",
        "note": "Loud condensed poster face; use for single words, never body. Single weight.",
        "avoid_bold": True,
    },
    "Bahnschrift": {
        "category": "sans", "moods": ["tech", "cinematic", "industrial", "transport"],
        "weights": ["Light", "Regular", "Semibold", "Bold"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "Carlito",
        "note": "DIN-style technical face; cinematic labels, engineering, motion.",
    },
    "Century Gothic": {
        "category": "sans", "moods": ["geometric", "friendly", "design", "fashion"],
        "weights": ["Regular", "Bold"],
        "fallback": "Arial, sans-serif", "preview": "approximate",
        "substitute": "URW Gothic",
        "note": "Wide geometric sans; design-forward, elegant and airy.",
    },
    "Consolas": {
        "category": "mono", "moods": ["code", "data", "technical"],
        "weights": ["Regular", "Bold"],
        "fallback": "'Courier New', monospace", "preview": "approximate",
        "substitute": "DejaVu Sans Mono",
        "note": "The code face — use for code blocks and technical tokens only.",
    },
    # ── Serif ─────────────────────────────────────────────────────────────
    "Georgia": {
        "category": "serif", "moods": ["editorial", "story", "web", "heritage"],
        "weights": ["Regular", "Bold"],
        "fallback": "'Times New Roman', serif", "preview": "full",
        "substitute": "Gelasio",
        "note": "Screen-first serif with large x-height; warm editorial body or headings.",
    },
    "Cambria": {
        "category": "serif", "moods": ["academic", "corporate", "data"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "full",
        "substitute": "Caladea",
        "note": "Sturdy slab-ish serif designed for screen; pairs naturally with Calibri.",
    },
    "Constantia": {
        "category": "serif", "moods": ["luxury", "editorial", "heritage"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "approximate",
        "substitute": "Caladea",
        "note": "Refined serif with old-style figures; elegant, restrained luxury.",
    },
    "Garamond": {
        "category": "serif", "moods": ["academic", "literary", "heritage", "luxury"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "approximate",
        "substitute": "EB Garamond",
        "note": "Renaissance book serif; light on screen — use at larger sizes.",
    },
    "Palatino Linotype": {
        "category": "serif", "moods": ["heritage", "academic", "classic"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "full",
        "substitute": "P052",
        "note": "Renaissance-inspired calligraphic serif; stately and readable.",
    },
    "Book Antiqua": {
        "category": "serif", "moods": ["heritage", "editorial", "classic"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "full",
        "substitute": "P052",
        "note": "Palatino sibling; bookish and calm.",
    },
    "Bookman Old Style": {
        "category": "serif", "moods": ["retro", "friendly", "editorial"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "approximate",
        "substitute": "URW Bookman",
        "note": "Wide, warm, mid-century book serif; distinctive retro voice.",
    },
    "Rockwell": {
        "category": "serif", "moods": ["industrial", "retro", "bold"],
        "weights": ["Regular", "Bold"],
        "fallback": "Georgia, serif", "preview": "approximate",
        "substitute": "DejaVu Serif",
        "note": "Geometric slab serif; industrial confidence, great for headers.",
    },
    "Times New Roman": {
        "category": "serif", "moods": ["academic"],
        "weights": ["Regular", "Bold"],
        "fallback": "serif", "preview": "full",
        "substitute": "Liberation Serif",
        "note": "The default-looking serif. Legitimate for academic decks only — choosing it as an identity face reads as 'never left the default'.",
    },
}


def format_catalog_for_prompt() -> str:
    """Compact catalog text injected into the theme planner's prompt."""
    lines: list[str] = []
    by_category: dict[str, list[str]] = {}
    for family, meta in FONT_CATALOG.items():
        by_category.setdefault(meta["category"], []).append(family)
    order = ["sans", "serif", "display", "mono"]
    for category in order:
        families = by_category.get(category)
        if not families:
            continue
        lines.append(f"{category.upper()} FACES (ships with Windows/Office):")
        for family in families:
            meta = FONT_CATALOG[family]
            moods = ", ".join(meta["moods"])
            preview = "preview-true" if meta["preview"] == "full" else "preview-approx"
            weights = "/".join(meta["weights"])
            lines.append(f"- {family} [{weights}] ({moods}) {{{preview}}} — {meta['note']}")
        lines.append("")
    return "\n".join(lines).strip()


def font_stack(family: str) -> str:
    """CSS font stack for a catalog family (or passthrough for unknowns)."""
    meta = FONT_CATALOG.get(family)
    if meta is None:
        return family
    return f"'{family}', {meta['fallback']}"


def resolve_preview_substitute(family: str) -> str:
    """The face the build VM actually renders for this family (fontconfig alias)."""
    meta = FONT_CATALOG.get(family)
    if meta is None:
        return family
    return meta["substitute"]
