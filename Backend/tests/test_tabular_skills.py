from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest

from agents.tabular_agent.skills import (
    SkillMetadata,
    build_skills_context,
    discover_skills,
    format_skills_index,
    load_skill_content,
)

_LOCAL_TMP_ROOT = Path(r"C:\Users\Praveen Raj U S\.codex\memories\tabular-test-tmp")
_LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def _temporary_skills_dir():
    tmpdir = _LOCAL_TMP_ROOT / f"tabular-skills-{uuid4().hex}"
    tmpdir.mkdir(parents=True, exist_ok=False)
    try:
        yield str(tmpdir)
    finally:
        rmtree(tmpdir, ignore_errors=True)


def test_discover_skills_empty_dir() -> None:
    with _temporary_skills_dir() as tmpdir:
        skills = discover_skills(Path(tmpdir))
        assert skills == []


def test_discover_skills_with_valid_skill() -> None:
    with _temporary_skills_dir() as tmpdir:
        skill_dir = Path(tmpdir) / "financial_variance"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\nname: financial-variance\ndescription: Variance analysis - actual vs budget.\ntags: [finance, variance]\n---\n# Full content here\n"
        )
        skills = discover_skills(Path(tmpdir))
        assert len(skills) == 1
        assert skills[0]["name"] == "financial-variance"
        assert skills[0]["description"] == "Variance analysis - actual vs budget."
        assert "finance" in skills[0]["tags"]
        assert "variance" in skills[0]["tags"]


def test_discover_skills_ignores_missing_skill_file() -> None:
    with _temporary_skills_dir() as tmpdir:
        skill_dir = Path(tmpdir) / "some_skill"
        skill_dir.mkdir()
        # No SKILL.md file
        skills = discover_skills(Path(tmpdir))
        assert skills == []


def test_discover_skills_invalid_frontmatter() -> None:
    with _temporary_skills_dir() as tmpdir:
        skill_dir = Path(tmpdir) / "bad_skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        # Invalid YAML (plain text, no frontmatter markers)
        skill_file.write_text("This is not a skill")
        skills = discover_skills(Path(tmpdir))
        assert skills == []


def test_load_skill_content() -> None:
    with _temporary_skills_dir() as tmpdir:
        skill_dir = Path(tmpdir) / "test_skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """---
name: test
description: A test skill
tags: []
---
# Full Skill Content
This is the body of the skill.
"""
        )
        content = load_skill_content(str(skill_dir))
        assert content == "# Full Skill Content\nThis is the body of the skill."


def test_load_skill_content_missing_file() -> None:
    content = load_skill_content("/nonexistent/path")
    assert content is None


def test_format_skills_index_empty() -> None:
    result = format_skills_index([])
    assert result == ""


def test_format_skills_index_with_skills() -> None:
    skills: list[SkillMetadata] = [
        {
            "name": "finance",
            "description": "Finance analysis",
            "tags": ["fpna"],
            "path": "/a",
        },
        {
            "name": "planning",
            "description": "Planning skill",
            "tags": ["budget"],
            "path": "/b",
        },
    ]
    result = format_skills_index(skills)
    assert "## Available Skills" in result
    assert "### finance" in result
    assert "### planning" in result
    assert "Use when:" in result


def test_build_skills_context_empty() -> None:
    result = build_skills_context([])
    assert result == ""


def test_build_skills_context_with_index() -> None:
    skills: list[SkillMetadata] = [
        {"name": "variance", "description": "Variance", "tags": ["a"], "path": "/x"},
    ]
    result = build_skills_context(skills)
    assert "## Available Skills" in result
    assert "### variance" in result
    assert "Use when:" in result


def test_skills_survives_non_skill_subdirs() -> None:
    with _temporary_skills_dir() as tmpdir:
        # Add some non-directory files that should be ignored
        (Path(tmpdir) / "README.md").write_text("not a skill")
        (Path(tmpdir) / "notes.txt").write_text("notes")
        # Add a valid skill
        skill_dir = Path(tmpdir) / "real_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: real
description: Real skill
tags: []
---
content
"""
        )
        skills = discover_skills(Path(tmpdir))
        assert len(skills) == 1
        assert skills[0]["name"] == "real"
