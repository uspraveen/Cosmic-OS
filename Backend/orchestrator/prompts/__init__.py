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
    "soul.md",
    "policies.md",
    "visual_response_policy.md",
    "memory_authority.md",
)


@lru_cache(maxsize=None)
def _load_prompt_asset(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


THIN_ORCHESTRATOR_SYSTEM_PROMPT = _load_prompt_asset("thin_system.md")
AGENTIC_ORCHESTRATOR_SYSTEM_PROMPT = _load_prompt_asset("system.md")
ORCHESTRATOR_SOUL_PROMPT = _load_prompt_asset("soul.md")
ORCHESTRATOR_POLICIES_PROMPT = _load_prompt_asset("policies.md")
ORCHESTRATOR_VISUAL_RESPONSE_POLICY_PROMPT = _load_prompt_asset(
    "visual_response_policy.md"
)
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
    featured_specialists: list[dict[str, object]] | None = None,
    visual_response_enhancement_enabled: bool = False,
    visual_supported_slot_kinds: list[str] | None = None,
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

    featured_agent_ids = {
        str(item.get("agent_id") or "").strip()
        for item in (featured_specialists or [])
        if isinstance(item, dict) and str(item.get("agent_id") or "").strip()
    }
    sections = [
        AGENTIC_ORCHESTRATOR_SYSTEM_PROMPT,
        ORCHESTRATOR_SOUL_PROMPT,
        build_featured_specialists_prompt(featured_specialists),
        build_tool_prompt_catalog(featured_agent_ids),
        ORCHESTRATOR_POLICIES_PROMPT,
        build_visual_response_policy_prompt(
            enabled=visual_response_enhancement_enabled,
            supported_slot_kinds=visual_supported_slot_kinds,
        ),
        date_line,
    ]
    prompt = "\n\n".join(section.strip() for section in sections if section.strip())
    context = str(memory_context or "").strip()
    if not context:
        return prompt
    return f"{prompt}\n\n{ORCHESTRATOR_MEMORY_AUTHORITY_INSTRUCTION}\n\n{context}"


def build_visual_response_policy_prompt(
    *,
    enabled: bool = False,
    supported_slot_kinds: list[str] | None = None,
) -> str:
    if not enabled:
        return ""
    supported = [
        str(item).strip().lower()
        for item in (supported_slot_kinds or [])
        if str(item).strip()
    ]
    if not supported:
        return ORCHESTRATOR_VISUAL_RESPONSE_POLICY_PROMPT
    capability_line = "Supported runtime slot kinds for this turn: {0}.".format(
        ", ".join(f"`{item}`" for item in supported)
    )
    return "{0}\n\n{1}".format(
        ORCHESTRATOR_VISUAL_RESPONSE_POLICY_PROMPT,
        capability_line,
    )


def build_featured_specialists_prompt(featured_specialists: list[dict[str, object]] | None = None) -> str:
    items = featured_specialists if isinstance(featured_specialists, list) else []
    normalized: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("display_name") or "").strip()
        agent_id = str(item.get("agent_id") or "").strip()
        if not display_name and not agent_id:
            continue
        normalized.append(item)
    if not normalized:
        return ""

    lines = [
        "## Current Specialist Shortlist",
        "",
        "This is a small dynamically promoted subset of specialists based on recent successful usage. It is not the full registry.",
        "Use it as a fast hint, but rely on `agent_catalog_search` when you need capabilities outside this shortlist or need the live exact intent.",
        "",
    ]
    for item in normalized:
        display_name = str(item.get("display_name") or item.get("agent_id") or "").strip()
        summary = str(item.get("agent_summary") or "").strip()
        common_intents = item.get("common_intents") if isinstance(item.get("common_intents"), list) else []
        intent_suffix = ""
        trimmed_intents = [str(intent).strip() for intent in common_intents if str(intent).strip()][:2]
        if trimmed_intents:
            intent_suffix = f" Common intents: {', '.join(f'`{intent}`' for intent in trimmed_intents)}."
        body = summary.rstrip(".")
        if body:
            lines.append(f"- `{display_name}`: {body}.{intent_suffix}")
        else:
            fallback = intent_suffix.strip() or "Specialist agent available via live catalog lookup."
            lines.append(f"- `{display_name}`: {fallback}")
    return "\n".join(lines).strip()
