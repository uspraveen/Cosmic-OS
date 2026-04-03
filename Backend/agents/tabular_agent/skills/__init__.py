from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
from typing import TYPE_CHECKING

import yaml

logger = logging.getLogger(__name__)


class SkillMetadata(TypedDict):
    """Skill metadata from YAML frontmatter (progressive disclosure)."""

    name: str
    description: str
    tags: list[str]
    path: str


@dataclass(frozen=True, slots=True)
class Skill:
    """Full skill with loaded content."""

    name: str
    description: str
    tags: list[str]
    content: str
    path: Path


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split YAML frontmatter from markdown body."""
    t = text.strip()
    if not t.startswith("---"):
        return "", t

    first_sep = t.find("---")
    if first_sep < 0:
        return "", t

    second_sep = t.find("---", first_sep + 3)
    if second_sep < 0:
        return "", t

    frontmatter = t[first_sep + 3 : second_sep]
    body = t[second_sep + 3 :]
    return frontmatter.strip(), body.strip()


def discover_skills(skills_dir: Path) -> list[SkillMetadata]:
    """
    Scan skills/*/SKILL.md, parse YAML frontmatter only.

    Args:
        skills_dir: Path to skills directory containing skill subdirectories.

    Returns:
        List of skill metadata (name, description, tags, path).
        Returns empty list if skills_dir doesn't exist or is empty.
    """
    if not skills_dir.is_dir():
        logger.debug("skills_dir not found: %s", skills_dir)
        return []

    discovered: list[SkillMetadata] = []

    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue
        skill_file = skill_path / "SKILL.md"
        if not skill_file.is_file():
            continue

        try:
            frontmatter, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
            meta = yaml.safe_load(frontmatter)
            if not isinstance(meta, dict):
                logger.warning("skipping %s: invalid frontmatter", skill_path)
                continue

            name = str(meta.get("name", ""))
            description = str(meta.get("description", ""))
            raw_tags = meta.get("tags", [])
            tag_list: list[str] = []
            if isinstance(raw_tags, list):
                tag_list = [str(t).strip() for t in raw_tags if str(t).strip()]

            discovered.append(
                SkillMetadata(
                    name=name,
                    description=description,
                    tags=tag_list,
                    path=str(skill_path.resolve()),
                )
            )
            logger.debug("discovered skill: %s", name)

        except Exception as e:  # noqa: BLE001
            logger.warning("failed to parse skill %s: %s", skill_path, e)

    return discovered


MAX_SKILL_SIZE_BYTES = 1024 * 1024  # 1MB max per skill


def load_skill_content(skill_path: str) -> str | None:
    """
    Load full SKILL.md body after YAML frontmatter (on-demand).

    Args:
        skill_path: Path to skill directory (from SkillMetadata.path).

    Returns:
        Full markdown content or None if file not found or too large.
    """
    p = Path(skill_path)
    if not p.is_dir():
        return None
    skill_file = p / "SKILL.md"
    if not skill_file.is_file():
        return None

    # Size guard - cap at 1MB to prevent context blowup
    if skill_file.stat().st_size > MAX_SKILL_SIZE_BYTES:
        logger.warning("skill file too large: %s", skill_path)
        return None

    try:
        _, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        return body.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to load skill content %s: %s", skill_path, e)
        return None


_SKILL_TRIGGERS = {
    "three-statement": "P&L and balance sheet and cash flow | income statement | net income | retained earnings | three statements",
    "ratio-analysis": "ratio | ROE | ROA | ROI | liquidity | leverage | current ratio | quick ratio | debt-to-equity | Altman Z",
    "revenue-analytics": "MRR | ARR | NRR | GRR | LTV | CAC | churn | cohort | recurring revenue | subscription",
    "financial-variance": "variance | actual vs budget | vs budget | vs forecast | YoY | MoM | QoQ | delta | bridge",
    "financial-planning": "forecast | budget | scenario | planning | FY | Q# projection | target | what-if",
    "working-capital": "DSO | DPO | DIO | CCC | cash conversion | working capital | aging | receivables | payables | inventory",
    "margin-bridge": "margin | gross margin | EBITDA | PVM | price-volume-mix | bridge | waterfall | segment profit",
    "consolidation": "consolidation | intercompany | IC elimination | minority interest | NCI | CTA | currency translation | FX",
    "expense-analysis": "expense | OpEx | SG&A | cost per head | run-rate | vendor | headcount |FTE",
}


def format_skills_index(skills: list[SkillMetadata]) -> str:
    """
    Render compact skills index with trigger phrases for system prompt.

    Args:
        skills: List of skill metadata from discover_skills().

    Returns:
        Formatted string with skill names, descriptions, and trigger phrases.
    """
    if not skills:
        return ""

    lines = [
        "## Available Skills",
        "When user's goal matches a trigger phrase, use activate_skill action.",
        "",
    ]
    for s in skills:
        name = s["name"]
        trigger = _SKILL_TRIGGERS.get(name, "")
        lines.append(f"### {name}")
        lines.append(
            f"*Use when: {trigger}*"
            if trigger
            else "*Use when: analysis relates to {name}*"
        )
        lines.append(f"**{s['description']}**")
        lines.append("")

    return "\n".join(lines)


def build_skills_context(
    skills: list[SkillMetadata],
    include_index: bool = True,
) -> str:
    """
    Build skills context block for system prompt.

    Args:
        skills: List of skill metadata from discover_skills().
        include_index: If True, includes the skills index.

    Returns:
        Formatted skills context block.
    """
    if not skills:
        return ""
    parts: list[str] = []

    if include_index:
        idx = format_skills_index(skills)
        if idx:
            parts.append(idx)

    if not parts:
        return ""
    return "\n\n".join(parts)


def build_active_skill_context(
    skill_name: str | None,
    skill_content: str | None,
) -> str:
    """Format the currently activated skill as isolated planner context.

    This keeps the full skill body out of the shared transcript while still
    making it available to later planner turns after ``activate_skill``.
    """
    name = str(skill_name or "").strip()
    content = str(skill_content or "").strip()
    if not name or not content:
        return ""
    return "\n".join(
        [
            "## Active Skill",
            f"Name: {name}",
            "Use this activated skill's formulas, heuristics, and SQL patterns when deciding the next action.",
            "Treat it as the currently selected domain context. Do not assume other skill bodies are active unless another activate_skill action occurs.",
            "",
            content,
        ]
    ).strip()


__all__ = [
    "SkillMetadata",
    "Skill",
    "discover_skills",
    "load_skill_content",
    "format_skills_index",
    "build_skills_context",
    "build_active_skill_context",
]
