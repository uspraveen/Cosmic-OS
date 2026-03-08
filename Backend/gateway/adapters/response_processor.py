from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


AWAITING_REPLY_TAG = "<awaiting_reply/>"
TAG_LEN = len(AWAITING_REPLY_TAG)

SendCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class StreamProcessingResult:
    content: str
    awaiting_reply: bool
    metrics: dict[str, int]


class LLMStreamProcessor:
    """Shared streaming response processor for direct Gateway LLM routes."""

    async def process_stream(
        self,
        stream: AsyncIterator[str],
        *,
        request_id: str,
        session_id: str,
        send: SendCallback,
    ) -> StreamProcessingResult:
        started_at = time.perf_counter()
        full_response = ""
        tail_buffer = ""

        async for chunk in stream:
            if not chunk:
                continue
            full_response += chunk
            tail_buffer += chunk
            if len(tail_buffer) <= TAG_LEN:
                continue

            safe_prefix = tail_buffer[:-TAG_LEN]
            tail_buffer = tail_buffer[-TAG_LEN:]
            if safe_prefix:
                await send(
                    {
                        "type": "response.chunk",
                        "request_id": request_id,
                        "session_id": session_id,
                        "content": safe_prefix,
                        "done": False,
                    }
                )

        stripped_tail = tail_buffer.rstrip()
        awaiting_reply = stripped_tail.endswith(AWAITING_REPLY_TAG)
        if awaiting_reply:
            remainder = stripped_tail.removesuffix(AWAITING_REPLY_TAG)
        else:
            remainder = tail_buffer

        if remainder:
            await send(
                {
                    "type": "response.chunk",
                    "request_id": request_id,
                    "session_id": session_id,
                    "content": remainder,
                    "done": False,
                }
            )

        display_text = full_response.rstrip()
        if display_text.endswith(AWAITING_REPLY_TAG):
            display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()

        return StreamProcessingResult(
            content=display_text,
            awaiting_reply=awaiting_reply,
            metrics={
                "rtt_ms": max(1, int((time.perf_counter() - started_at) * 1000)),
            },
        )


def normalize_conversation_history(history: list[dict[str, object]]) -> list[dict[str, str]]:
    """Collapse same-role runs and trim orphaned assistant turns.

    History is pruned by a character budget before it reaches direct adapters.
    That means the retained suffix can begin midway through a conversation on an
    assistant turn. Providers like Perplexity reject `system -> assistant`
    openings, so we normalize to a valid alternating chat transcript that starts
    with `user` and, for query-time history, also ends with `user`.
    """
    normalized: list[dict[str, str]] = []
    for message in history:
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] += "\n\n" + content
            continue
        normalized.append({"role": role, "content": content})

    while normalized and normalized[0]["role"] != "user":
        normalized.pop(0)

    while normalized and normalized[-1]["role"] != "user":
        normalized.pop()

    return normalized
