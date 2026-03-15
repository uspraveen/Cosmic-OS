"""Prompt asset loaders for the COSMIC orchestrator."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from ..tools.registry import build_tool_prompt_catalog


PROMPT_DIR = Path(__file__).resolve().parent
_PROMPT_ASSETS = (
    "thin_system.md",
    "system.md",
    "policies.md",
    "memory_authority.md",
)


@lru_cache(maxsize=None)
def _load_prompt_asset(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


THIN_ORCHESTRATOR_SYSTEM_PROMPT = _load_prompt_asset("thin_system.md")
AGENTIC_ORCHESTRATOR_SYSTEM_PROMPT = _load_prompt_asset("system.md")
ORCHESTRATOR_POLICIES_PROMPT = _load_prompt_asset("policies.md")
ORCHESTRATOR_MEMORY_AUTHORITY_INSTRUCTION = _load_prompt_asset("memory_authority.md")


def get_prompt_asset_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(_load_prompt_asset(name).encode("utf-8")).hexdigest()
        for name in _PROMPT_ASSETS
    }


def build_thin_orchestrator_system_prompt(memory_context: str | None = None) -> str:
    prompt = THIN_ORCHESTRATOR_SYSTEM_PROMPT
    context = str(memory_context or "").strip()
    if not context:
        return prompt
    return f"{prompt}\n\n{ORCHESTRATOR_MEMORY_AUTHORITY_INSTRUCTION}\n\n{context}"


def build_agentic_system_prompt(
    memory_context: str | None = None,
    *,
    user_timezone: str | None = None,
) -> str:
    now_utc = datetime.now(timezone.utc)
    date_line = f"Current date and time (UTC): {now_utc.strftime('%A, %B %d, %Y at %H:%M UTC')}."

    tz_name = (user_timezone or "").strip()
    if tz_name:
        try:
            import zoneinfo

            local_now = now_utc.astimezone(zoneinfo.ZoneInfo(tz_name))
            date_line += f"\nUser's local time: {local_now.strftime('%A, %B %d, %Y at %I:%M %p %Z')}."
        except Exception:
            pass

    sections = [
        AGENTIC_ORCHESTRATOR_SYSTEM_PROMPT,
        build_tool_prompt_catalog(),
        ORCHESTRATOR_POLICIES_PROMPT,
        date_line,
    ]
    prompt = "\n\n".join(section.strip() for section in sections if section.strip())
    context = str(memory_context or "").strip()
    if not context:
        return prompt
    return f"{prompt}\n\n{ORCHESTRATOR_MEMORY_AUTHORITY_INSTRUCTION}\n\n{context}"
