"""Template registry — metadata for available slide templates.

All templates are professionally designed Slidesgo templates with rich backgrounds,
decorative elements, and multiple purpose-built layouts.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = [
    {
        "name": "business-meeting",
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

TEMPLATE_NAMES: set[str] = {t["name"] for t in TEMPLATES}

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


def get_template_descriptions(templates_dir: Path | None = None, **_kwargs) -> str:
    """Get a formatted string of template descriptions for the LLM prompt."""
    available = _available_templates(templates_dir)

    lines = ["## Available Templates\n"]
    for t in available:
        best = ", ".join(t["best_for"][:4])
        lines.append(f"- **{t['name']}**: {t['description']}")
        lines.append(f"  Tone: {t['tone']} | Best for: {best}")
    if available:
        available_names = {t["name"] for t in available}
        default_hints = []
        if "business-meeting" in available_names:
            default_hints.append("`business-meeting` for general requests")
        if "tech-trends" in available_names:
            default_hints.append("`tech-trends` for data/tech content")
        if "science-lesson" in available_names:
            default_hints.append("`science-lesson` for educational content")
        if "tech-infographics" in available_names:
            default_hints.append("`tech-infographics` for infographic-style slides")
        lines.append(
            "\n**IMPORTANT**: You MUST choose one of the templates listed above. "
            "Do NOT use any other template name. "
            + ("Default: " + ", ".join(default_hints) + "." if default_hints else "")
        )
    else:
        lines.append("\n**Note**: No templates are currently installed.")
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
