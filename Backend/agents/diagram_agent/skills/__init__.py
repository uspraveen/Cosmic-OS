"""Diagram Agent skills — renderer-specific syntax references and patterns.

Follows the tabular agent skill discovery pattern:
- skills/<name>/SKILL.md with YAML frontmatter
- discover_skills() loads metadata, load_skill_content() loads full body
- trigger phrases map renderer selection to user intent
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

import yaml

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent


class SkillMetadata(TypedDict):
    name: str
    description: str
    tags: list[str]
    path: str


def _split_frontmatter(text: str) -> tuple[str, str]:
    t = text.strip()
    if not t.startswith("---"):
        return "", t
    first_sep = t.find("---")
    if first_sep < 0:
        return "", t
    second_sep = t.find("---", first_sep + 3)
    if second_sep < 0:
        return "", t
    return t[first_sep + 3 : second_sep].strip(), t[second_sep + 3 :].strip()


def discover_skills() -> list[SkillMetadata]:
    """Scan skills/*/SKILL.md, parse YAML frontmatter only."""
    if not SKILLS_DIR.is_dir():
        return []

    discovered: list[SkillMetadata] = []
    for skill_path in sorted(SKILLS_DIR.iterdir()):
        if not skill_path.is_dir():
            continue
        skill_file = skill_path / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            frontmatter, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
            meta = yaml.safe_load(frontmatter)
            if not isinstance(meta, dict):
                continue
            discovered.append(
                SkillMetadata(
                    name=str(meta.get("name", "")),
                    description=str(meta.get("description", "")),
                    tags=[str(t) for t in meta.get("tags", []) if t],
                    path=str(skill_path.resolve()),
                )
            )
        except Exception:
            continue
    return discovered


def load_skill_content(skill_path: str) -> str | None:
    """Load full SKILL.md body (after frontmatter)."""
    p = Path(skill_path)
    if not p.is_dir():
        return None
    skill_file = p / "SKILL.md"
    if not skill_file.is_file():
        return None
    if skill_file.stat().st_size > 1024 * 1024:
        return None
    try:
        _, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        return body.strip()
    except Exception:
        return None


_SKILL_TRIGGERS = {
    "mermaid": "sequence | flowchart | ERD | gantt | state | class | git graph | pie | timeline | quadrant | inline diagram | markdown diagram | GitHub diagram",
    "d2": "architecture | system design | infrastructure | network | topology | database schema | API diagram | layered diagram | nested containers | VPC | subnet | microservice",
    "excalidraw": "whiteboard | sketch | hand-drawn | brainstorm | rough | informal | wireframe | doodle | canvas",
}


def format_skills_index(skills: list[SkillMetadata]) -> str:
    """Render compact skills index with trigger phrases for system prompt."""
    if not skills:
        return ""
    lines = ["## Available Renderers", ""]
    for s in skills:
        name = s["name"]
        trigger = _SKILL_TRIGGERS.get(name, "")
        lines.append(f"### {name}")
        if trigger:
            lines.append(f"*Use when: {trigger}*")
        lines.append(f"**{s['description']}**")
        lines.append("")
    return "\n".join(lines)


def build_skills_context(skills: list[SkillMetadata]) -> str:
    """Build skills context block for system prompt."""
    idx = format_skills_index(skills)
    return idx if idx else ""
