"""Template registry — descriptions of available templates for LLM selection.

Each template has metadata that the LLM uses to choose the right one:
- name: template identifier
- description: when to use this template
- tone: business, minimal, startup, etc.
- color scheme: dark/light
- best_for: specific use cases
"""

TEMPLATES = [
    {
        "name": "corporate-dark",
        "description": "Dark corporate template with navy background, white text, and blue accent. Professional and modern.",
        "tone": "business",
        "color_scheme": "dark",
        "background": "#1a1a2e",
        "text_color": "#ffffff",
        "accent_colors": ["#007bff", "#e94560"],
        "best_for": [
            "business presentations",
            "quarterly reports",
            "team updates",
            "strategy decks",
            "executive summaries",
        ],
    },
    {
        "name": "corporate-light",
        "description": "Light corporate template with white background, dark text, and blue accent. Clean and professional.",
        "tone": "business",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#212529",
        "accent_colors": ["#007bff", "#28a745"],
        "best_for": [
            "client proposals",
            "project updates",
            "training materials",
            "documentation",
            "formal reports",
        ],
    },
    {
        "name": "minimal",
        "description": "Ultra-clean minimal template with generous whitespace. Lets content speak for itself.",
        "tone": "minimal",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#333333"],
        "best_for": [
            "design presentations",
            "portfolios",
            "creative pitches",
            "thought leadership",
            "simple explanations",
        ],
    },
    {
        "name": "pitch-deck",
        "description": "Bold startup pitch deck with dark background and vibrant accent colors. High impact.",
        "tone": "startup",
        "color_scheme": "dark",
        "background": "#0f0f23",
        "text_color": "#ffffff",
        "accent_colors": ["#6633ff", "#00ff88"],
        "best_for": [
            "startup pitches",
            "investor decks",
            "product launches",
            "fundraising",
            "demo presentations",
        ],
    },
    {
        "name": "business-meeting",
        "description": "Green and gray minimal geometric template for business meetings. Agenda-focused with clean lines and geometric shapes. Google Slides compatible.",
        "tone": "business",
        "color_scheme": "light",
        "background": "#ffffff",
        "text_color": "#333333",
        "accent_colors": ["#4CAF50", "#9E9E9E"],
        "best_for": [
            "meeting agendas",
            "team syncs",
            "project kickoffs",
            "status updates",
            "sprint planning",
        ],
        "source": "Slidesgo",
    },
    {
        "name": "science-lesson",
        "description": "Educational template for science lessons and teaching. Colorful with illustration-friendly layouts.",
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
            "workshops",
        ],
        "source": "Slidesgo",
    },
    {
        "name": "tech-trends",
        "description": "Tech-focused template for trends, data, and technology presentations. Modern with data visualization layouts.",
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
        ],
        "source": "Slidesgo",
    },
]


def get_template_descriptions() -> str:
    """Get a formatted string of template descriptions for the LLM prompt."""
    lines = ["## Available Templates\n"]
    for t in TEMPLATES:
        best = ", ".join(t["best_for"][:3])
        lines.append(f"- **{t['name']}**: {t['description']}")
        lines.append(f"  Tone: {t['tone']} | Best for: {best}")
    return "\n".join(lines)


def get_template(name: str) -> dict | None:
    """Get a template by name."""
    for t in TEMPLATES:
        if t["name"] == name:
            return t
    return None
