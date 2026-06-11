from __future__ import annotations

from shared import render_markdown_email_bodies


def test_render_markdown_email_bodies_preserves_tables_in_html_and_flattens_text() -> None:
    markdown = """# AI Hackathons

| Hackathon | Dates | Organizer | Notes |
|---|---|---|---|
| **MLH Global Hack Week** | June 12-18, 2026 | Major League Hacking | Fully online |
| Devpost Challenge | July 10-16, 2026 | Devpost | AI/ML focus |
"""

    rendered = render_markdown_email_bodies(markdown)

    assert "AI Hackathons" in rendered.text_body
    assert "|---|---|---|---|" not in rendered.text_body
    assert "Hackathon: MLH Global Hack Week" in rendered.text_body
    assert "<h1>AI Hackathons</h1>" in rendered.html_body
    assert "<table" in rendered.html_body
    assert "<thead><tr>" in rendered.html_body
    assert "Hackathon" in rendered.html_body
    assert "<strong>MLH Global Hack Week</strong>" in rendered.html_body
