"""Gateway prompt assets."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _prompt_text(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def session_compaction_system_prompt() -> str:
    return _prompt_text("session_compaction_system.md")


def session_rollover_summary_system_prompt() -> str:
    return _prompt_text("session_rollover_summary_system.md")


def capability_wishlist_adjudicator_system_prompt() -> str:
    return _prompt_text("capability_wishlist_adjudicator_system.md")
