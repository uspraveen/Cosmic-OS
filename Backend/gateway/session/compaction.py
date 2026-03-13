from __future__ import annotations

from ..prompts.session import (
    build_session_compaction_user_prompt,
    session_compaction_system_prompt,
)


def build_compaction_prompts(
    *,
    session_id: str,
    existing_summary: str | None,
    turn_lines: list[str],
    older_lines: list[str],
    recent_window_count: int,
    current_tasks: list[str],
) -> tuple[str, str]:
    return (
        session_compaction_system_prompt(),
        build_session_compaction_user_prompt(
            session_id=session_id,
            existing_summary=existing_summary,
            turn_lines=turn_lines,
            older_lines=older_lines,
            recent_window_count=recent_window_count,
            current_tasks=current_tasks,
        ),
    )
