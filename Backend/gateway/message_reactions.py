"""Quick emoji acknowledgments on the user's own message, picked by Jev.

The gateway fires this on message intake, in parallel with dispatching the
real work — a receipt that lands before the orchestrator's first token. Two
Jev questions in one call decide it: an absolute "is there a signal worth
acknowledging" (noul) and a relative "which emoji fits best" (choice). The
abstain path is the majority outcome; a reaction that fired on every message
would be noise within a day.

Jev's choice answers are relative among the offered options, so a bland
message still produces a "most likely" emoji — that is exactly why the gate
is two questions and not one. Failure is silent by design: no key, a timeout,
or a malformed answer all degrade to "no badge", never to an error on the
intake path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Set B minus the check mark, confirmed by the user (2026-10-13).
REACTION_EMOJI: tuple[str, ...] = ("👍", "🚀", "👀", "🔥", "🤔", "❤️", "⚡", "🫡", "🎉")

EMOJI_DESCRIPTIONS: dict[str, str] = {
    "👍": "Plain acknowledgment — a simple instruction or 'got it' message",
    "🚀": "Ambitious framing — launches, big goals, building something new",
    "👀": "Looking into it — the message points at media, a link, or something to examine",
    "🔥": "Impressive content — the message shares something exciting or high quality",
    "🤔": "Thoughtful or tricky — the message needs care or raises a real question",
    "❤️": "Warmth — gratitude, praise, or a kind message",
    "⚡": "Speed — a quick, efficient ask that can be handled fast",
    "🫡": "Mission received — a clear task or directive the assistant is taking on",
    "🎉": "Celebration — a milestone, a ship, good news",
}

# The noul readout is absolute; the choice readout is relative. Both must
# clear their bar before a badge shows. Calibrated live (2026-10-13): bland
# and routine messages cluster at noul <= 0.23, while the proven reactions
# ("post this" + video, "think 10 years out") scored 0.35 and 0.69 — the bar
# sits between the two clusters.
REACT_THRESHOLD = 0.30
EMOJI_CONFIDENCE_THRESHOLD = 0.35

# Tone lives at the start and the ask lives at the end; the middle rarely
# changes an emoji choice but is cheap to include.
TEXT_CAP_CHARS = 1600
_SECTION_CHARS = 600

_JEV_URL = "https://api.typesafe.ai/v1/systemone"
_JEV_MODEL = "jev-latest"


def cap_message_text(text: str, *, cap: int = TEXT_CAP_CHARS) -> str:
    """Start/middle/end projection of a message that exceeds the cap."""
    normalized = str(text or "").strip()
    if len(normalized) <= cap:
        return normalized
    section = max(1, cap // 3)
    start = normalized[:section].strip()
    middle = normalized[len(normalized) // 2 - section // 2 : len(normalized) // 2 + section // 2].strip()
    end = normalized[-section:].strip()
    return "…\n…\n".join(part for part in (start, middle, end) if part)


def summarize_attachments(attachments: list[Any] | None) -> list[str]:
    """Short per-attachment descriptors for the Jev state payload."""
    if not isinstance(attachments, list):
        return []
    summarized: list[str] = []
    for item in attachments[:20]:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime") or item.get("mime_type") or "").strip().lower()
        filename = str(item.get("filename") or item.get("name") or "").strip()
        kind = "file"
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "audio"
        elif mime or filename:
            kind = "document"
        summarized.append(f"{kind}: {filename}"[:120] if filename else kind)
    return summarized


def build_questions(attachment_summary: list[str] | None = None) -> dict[str, dict[str, Any]]:
    state_hint = ""
    if attachment_summary:
        state_hint = (
            " The message came with attachments: " + ", ".join(attachment_summary) + "."
        )
    return {
        "react": {
            "type": "noul",
            "instructions": (
                "Is there a clear tone or intent in this message that deserves a quick"
                " emoji acknowledgment from an assistant about to start working on it?"
                " Answer no for routine, bland, or purely informational messages."
                " A reaction must feel earned, not automatic." + state_hint
            ),
            "criteria": {
                "true": "There is a clear tone or intent worth acknowledging",
                "false": "No clear signal — no reaction",
            },
        },
        "emoji": {
            "type": "choice",
            "criteria": dict(EMOJI_DESCRIPTIONS),
            "instructions": {
                "role": (
                    "Pick the single emoji that best matches the sender's tone or"
                    " intent in this message, as a quick acknowledgment before work"
                    " starts." + state_hint
                ),
            },
        },
    }


def parse_reaction_response(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Gate the Jev answers; returns the outcome dict or None (abstain/failure)."""
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None

    react_answer = answers.get("react")
    react_score: float | None = None
    if isinstance(react_answer, dict):
        try:
            react_score = float(react_answer.get("noul"))
        except (TypeError, ValueError):
            react_score = None
    if react_score is None or react_score < REACT_THRESHOLD:
        return None

    emoji_answer = answers.get("emoji")
    if not isinstance(emoji_answer, dict):
        return None
    emoji = str(emoji_answer.get("choice") or "").strip()
    if emoji not in REACTION_EMOJI:
        return None
    try:
        confidence = float(emoji_answer.get("confidence"))
    except (TypeError, ValueError):
        return None
    if confidence < EMOJI_CONFIDENCE_THRESHOLD:
        return None

    return {
        "emoji": emoji,
        "confidence": confidence,
        "react_score": react_score,
    }


async def pick_reaction(
    api_key: str,
    text: str,
    attachments: list[Any] | None = None,
    *,
    timeout_sec: float = 4.0,
    model: str = _JEV_MODEL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | None:
    """Ask Jev for a reaction. Returns None on abstain or any failure — never raises."""
    if not api_key:
        return None
    attachment_summary = summarize_attachments(attachments)
    state: dict[str, Any] = {
        "message": cap_message_text(text),
        "context": "a user's chat message to their AI assistant",
    }
    if attachment_summary:
        state["attachments"] = attachment_summary
    body = {
        "model": model,
        "state": state,
        "questions": build_questions(attachment_summary),
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_sec, transport=transport) as client:
            response = await client.post(
                _JEV_URL,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info(
            "gateway.message_reaction_request_failed error=%s", type(exc).__name__
        )
        return None
    outcome = parse_reaction_response(payload)
    if outcome:
        outcome["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return outcome


def run_pick_reaction_sync(
    api_key: str,
    text: str,
    attachments: list[Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Sync wrapper for tests and one-off probes."""
    return asyncio.run(pick_reaction(api_key, text, attachments, **kwargs))
