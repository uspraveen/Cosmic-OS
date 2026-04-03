"""Template registry — metadata for available slide templates.

Templates are split into two tiers:
- **Premium**: Downloaded from professional sources (Slidesgo), with rich backgrounds,
  decorative elements, and multiple purpose-built layouts. Always prefer these.
- **Legacy**: Generated programmatically. Bare-bones Office defaults with no design.
  Only used as a fallback if no premium template fits.
"""

from __future__ import annotations

from pathlib import Path

# ── Premium templates (always prefer these) ──────────────────────────────────

PREMIUM_TEMPLATES = [
    {
        "name": "business-meeting",
        "tier": "premium",
        "description": "Green and gray minimal geometric template. Clean lines, geometric shapes, professional feel. The most versatile general-purpose template.",
        "tone": "business",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#4CAF50", "#9E9E9E"],
        "best_for": [
            "business presentations",
            "meeting agendas",
            "team syncs",
            "project updates",
            "strategy decks",
            "quarterly reports",
            "status updates",
            "sprint planning",
            "client proposals",
        ],
        "source": "Slidesgo",
    },
    {
        "name": "tech-trends",
        "tier": "premium",
        "description": "Modern tech-focused template with 21 layouts including data viz, big numbers, infographic grids, and section headers. Best for data-heavy and technical content.",
        "tone": "tech",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#1a1a2e",
        "accent_colors": ["#6366f1", "#06b6d4", "#10b981"],
        "best_for": [
            "tech trends",
            "data analysis",
            "product roadmaps",
            "engineering updates",
            "AI/ML presentations",
            "research findings",
            "analytics reports",
            "startup pitches",
            "investor decks",
        ],
        "source": "Slidesgo",
    },
    {
        "name": "science-lesson",
        "tier": "premium",
        "description": "Educational template with colorful illustration-friendly layouts. Great for teaching, workshops, and explanations.",
        "tone": "educational",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#2196F3", "#FF9800", "#4CAF50"],
        "best_for": [
            "lesson plans",
            "educational presentations",
            "training workshops",
            "tutorials",
            "onboarding decks",
            "how-to guides",
            "explainer decks",
        ],
        "source": "Slidesgo",
    },
    {
        "name": "tech-infographics",
        "tier": "premium",
        "description": "Technology infographic template with clean layouts for data visualization, comparisons, and visual storytelling. Great for infographic-style slides.",
        "tone": "tech",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#6366f1", "#06b6d4"],
        "best_for": [
            "infographics",
            "data visualization",
            "comparison slides",
            "process flows",
            "technology overviews",
            "product features",
            "visual summaries",
        ],
        "source": "Slidesgo",
    },
]

# ── Legacy templates (fallback only — bare Office defaults) ──────────────────

LEGACY_TEMPLATES = [
    {
        "name": "corporate-dark",
        "tier": "legacy",
        "description": "Basic dark template. Minimal design — use only if an explicitly dark theme is requested.",
        "tone": "business",
        "color_scheme": "dark",
        "background": "#1a1a2e",
        "text_color": "#ffffff",
        "accent_colors": ["#007bff", "#e94560"],
        "best_for": ["dark-themed presentations"],
    },
    {
        "name": "corporate-light",
        "tier": "legacy",
        "description": "Basic light template. Minimal design — prefer business-meeting instead.",
        "tone": "business",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#212529",
        "accent_colors": ["#007bff", "#28a745"],
        "best_for": [],
    },
    {
        "name": "minimal",
        "tier": "legacy",
        "description": "Bare minimal template. No design elements — prefer tech-infographics for clean looks.",
        "tone": "minimal",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#333333"],
        "best_for": [],
    },
    {
        "name": "pitch-deck",
        "tier": "legacy",
        "description": "Basic dark startup template. Minimal design — prefer tech-trends for pitches.",
        "tone": "startup",
        "color_scheme": "dark",
        "background": "#0f0f23",
        "text_color": "#ffffff",
        "accent_colors": ["#6633ff", "#00ff88"],
        "best_for": [],
    },
]

# Combined list (premium first)
TEMPLATES = PREMIUM_TEMPLATES + LEGACY_TEMPLATES


_DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _built_in_template_path(name: str, templates_dir: Path) -> Path:
    return templates_dir / f"{name}.pptx"


def _available_templates(templates_dir: Path | None = None) -> list[dict]:
    root = templates_dir or _DEFAULT_TEMPLATES_DIR
    available: list[dict] = []
    for template in TEMPLATES:
        candidate = _built_in_template_path(str(template["name"]), root)
        if candidate.exists():
            available.append(template)
    return available


def get_template_descriptions(templates_dir: Path | None = None) -> str:
    """Get a formatted string of template descriptions for the LLM prompt.

    Premium templates are listed first with a recommendation badge.
    Legacy templates are listed with a warning to prefer premium alternatives.
    """
    available = _available_templates(templates_dir)
    available_names = {str(t["name"]) for t in available}
    premium_templates = [t for t in PREMIUM_TEMPLATES if str(t["name"]) in available_names]
    legacy_templates = [t for t in LEGACY_TEMPLATES if str(t["name"]) in available_names]

    lines = ["## Available Templates\n"]
    lines.append("### Recommended (professionally designed, use these by default):\n")
    for t in premium_templates:
        best = ", ".join(t["best_for"][:4])
        lines.append(f"- **{t['name']}** ★: {t['description']}")
        lines.append(f"  Tone: {t['tone']} | Best for: {best}")
    lines.append("\n### Legacy (basic design — only use if explicitly requested):\n")
    for t in legacy_templates:
        lines.append(f"- **{t['name']}**: {t['description']}")
    if premium_templates:
        lines.append(
            "\n**Default**: Use `business-meeting` for general requests, `tech-trends` "
            "for data/tech content, `science-lesson` for educational content, "
            "`tech-infographics` for infographic-style slides."
        )
    else:
        lines.append("\n**Note**: No premium built-in templates are currently installed.")
    return "\n".join(lines)


def get_template(name: str, templates_dir: Path | None = None) -> dict | None:
    """Get a template by name."""
    root = templates_dir or _DEFAULT_TEMPLATES_DIR
    for t in TEMPLATES:
        if t["name"] == name:
            if not _built_in_template_path(str(name), root).exists():
                return None
            return t
    return None
