from __future__ import annotations

from ..prompts.session import (
    build_session_rollover_summary_user_prompt,
    session_rollover_summary_system_prompt,
)


def session_summary_source_text(transcript_markdown: str, *, char_limit: int) -> str:
    normalized = transcript_markdown.strip()
    if len(normalized) <= char_limit:
        return normalized

    half_limit = char_limit // 2
    head = normalized[:half_limit].rstrip()
    tail = normalized[-half_limit:].lstrip()
    return head + "\n\n[... transcript truncated for summarization ...]\n\n" + tail


def build_rollover_summary_prompts(
    *,
    session_id: str,
    message_count: int,
    transcript_source: str,
) -> tuple[str, str]:
    return (
        session_rollover_summary_system_prompt(),
        build_session_rollover_summary_user_prompt(
            session_id=session_id,
            message_count=message_count,
            transcript_source=transcript_source,
        ),
    )
