from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import ProphetStore


PROPHET_MORNING_CRON_ID = "prophet.morning"
PROPHET_EVENING_CRON_ID = "prophet.evening"
PROPHET_CRON_IDS = frozenset({PROPHET_MORNING_CRON_ID, PROPHET_EVENING_CRON_ID})

PROPHET_MORNING_PROMPT = """Compose today's morning edition of The Daily Prophet — COSMIC's personalized newspaper for this user.

This is not a headline dump. Write the user's own edition: choose the stories that matter to them, rewrite headlines and body copy in COSMIC's editorial voice, add framing that connects each story to their world (their projects, interests, and goals), and order everything front-page-first.

Work rules:
- Do not ask the user anything and do not narrate your process.
- Keep research bounded and prioritize quality over quantity; do not chase every thread or start long delegations late.
- Images: a couple of visuals carry a paper — aim for the lead and up to two more, using each article's own main image when you have a URL you trust. Skip icons, logos, avatars, and anything you cannot verify.
- You MUST end this run by calling publish_prophet_edition with the complete edition (lead plus sections). Aim for a full paper — roughly eight to {max_stories} stories — and go smaller only on a genuinely quiet day.
- If you are running low on tool budget, publish the strongest stories you already have instead of doing more research.
- If publish_prophet_edition is rejected, correct the specific problem and republish the real edition. Never publish placeholder, sample, or test stories.
- After the publish succeeds, reply with one short line confirming the edition is ready."""

PROPHET_EVENING_PROMPT = """Compose today's evening edition of The Daily Prophet — COSMIC's personalized newspaper for this user.

This is the evening wrap: what developed during the day, what the user will want to know before tomorrow, and any late-breaking items — written as their own edition, not a raw feed. Choose the stories that matter to them, rewrite headlines and body copy in COSMIC's editorial voice, and connect each story to their world.

Work rules:
- Do not ask the user anything and do not narrate your process.
- Keep research bounded and prioritize quality over quantity; do not chase every thread or start long delegations late.
- Images: a couple of visuals carry a paper — aim for the lead and up to two more, using each article's own main image when you have a URL you trust. Skip icons, logos, avatars, and anything you cannot verify.
- You MUST end this run by calling publish_prophet_edition with the complete edition (lead plus sections). Aim for a full paper — roughly eight to {max_stories} stories — and go smaller only on a genuinely quiet day.
- If you are running low on tool budget, publish the strongest stories you already have instead of doing more research.
- If publish_prophet_edition is rejected, correct the specific problem and republish the real edition. Never publish placeholder, sample, or test stories.
- After the publish succeeds, reply with one short line confirming the edition is ready."""


def prophet_prompt(slot: str, *, max_stories: int) -> str:
    template = PROPHET_EVENING_PROMPT if slot == "evening" else PROPHET_MORNING_PROMPT
    return template.format(max_stories=max(1, int(max_stories or 15)))


def prophet_cron_specs(settings: dict[str, Any]) -> list[dict[str, Any]]:
    max_stories = int(settings.get("max_stories") or 15)
    enabled = bool(settings.get("enabled"))
    return [
        {
            "cron_id": PROPHET_MORNING_CRON_ID,
            "slot": "morning",
            "name": "Daily Prophet morning edition",
            "time_key": "morning_time",
            "time_value": str(settings.get("morning_time") or "05:00"),
            "active": enabled,
            "prompt": prophet_prompt("morning", max_stories=max_stories),
            "description": "Compose and publish the morning edition of the user's Daily Prophet.",
        },
        {
            "cron_id": PROPHET_EVENING_CRON_ID,
            "slot": "evening",
            "name": "Daily Prophet evening edition",
            "time_key": "evening_time",
            "time_value": str(settings.get("evening_time") or "19:00"),
            "active": enabled and bool(settings.get("evening_enabled")),
            "prompt": prophet_prompt("evening", max_stories=max_stories),
            "description": "Compose and publish the evening wrap edition of the user's Daily Prophet.",
        },
    ]


def cron_expression_for_time(time_value: str) -> str:
    hour_text, _, minute_text = str(time_value or "05:00").partition(":")
    try:
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
    except ValueError:
        hour, minute = 5, 0
    return f"{minute} {hour} * * *"


def render_prophet_context_block(
    store: "ProphetStore",
    *,
    slot: str,
    max_stories: int,
) -> str:
    settings = store.get_settings()
    interests = store.list_interests(include_muted=False)
    muted = [item["topic"] for item in store.list_interests() if item.get("muted")]
    sources = store.list_sources()
    recent = store.recent_story_summaries(days=3, limit=80)
    sections = [
        section
        for section in settings.get("sections") or []
        if isinstance(section, dict) and section.get("enabled", True)
    ]

    lines = [
        "## Daily Prophet Briefing",
        f"- Slot: {slot} edition",
        f"- Story cap: {max_stories} stories total, including the lead",
    ]
    if sections:
        lines.append(
            "- Enabled sections: "
            + ", ".join(
                f"{section.get('label') or section.get('id')}"
                for section in sections
            )
        )
    if interests:
        lines.append(
            "- User interests: "
            + ", ".join(
                f"{item['topic']} ({item.get('origin') or 'inferred'})" for item in interests[:24]
            )
        )
    else:
        lines.append("- User interests: none recorded yet; infer from memory and recent context.")
    if muted:
        lines.append("- Muted topics (never surface): " + ", ".join(muted[:16]))
    if sources:
        lines.append(
            "- Preferred sources: "
            + ", ".join(
                f"{item['kind']}:{item['value']}" for item in sources[:16]
            )
        )
    if recent:
        lines.extend(["", "Recently shown stories (avoid unless there is a major update):"])
        for item in recent[:24]:
            section = item.get("section_id") or "general"
            lines.append(
                f"- {item['edition_date']} {item['slot']} [{section}] {item['headline']}"
            )
    return "\n".join(lines)
