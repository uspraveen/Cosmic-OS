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


def build_session_compaction_user_prompt(
    *,
    session_id: str,
    existing_summary: str | None,
    turn_lines: list[str],
    older_lines: list[str],
    recent_window_count: int,
    current_tasks: list[str],
) -> str:
    return (
        f"Session ID: {session_id}\n\n"
        f"Existing compacted summary:\n{existing_summary or '[none]'}\n\n"
        f"Compactable turn ledger:\n{chr(10).join(turn_lines) or '[none]'}\n\n"
        f"Older raw conversation slice:\n{chr(10).join(older_lines) or '[none]'}\n\n"
        f"Recent window retained uncompressed count: {recent_window_count}\n"
        f"Active task refs: {', '.join(current_tasks) or '[none]'}\n"
    )


def build_session_rollover_summary_user_prompt(
    *,
    session_id: str,
    message_count: int,
    transcript_source: str,
) -> str:
    return (
        f"Session ID: {session_id}\n"
        f"Message count: {message_count}\n\n"
        "Transcript:\n\n"
        f"{transcript_source}"
    )
