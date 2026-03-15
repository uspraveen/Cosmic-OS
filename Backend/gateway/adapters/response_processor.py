from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


AWAITING_REPLY_TAG = "<awaiting_reply/>"
HANDOFF_OPUS_TAG = "<handoff_opus/>"
_MAX_CONTROL_TAG_LEN = max(len(AWAITING_REPLY_TAG), len(HANDOFF_OPUS_TAG))

SendCallback = Callable[[dict], Awaitable[None]]
VisibleChunkCallback = Callable[[], Awaitable[None]]


class DirectRouteHandoff(RuntimeError):
    def __init__(self, route: str) -> None:
        self.route = route
        super().__init__(f"Direct route requested handoff to {route}")


@dataclass(slots=True)
class StreamProcessingResult:
    content: str
    awaiting_reply: bool
    handoff_route: str | None
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
        on_first_visible_chunk: VisibleChunkCallback | None = None,
    ) -> StreamProcessingResult:
        started_at = time.perf_counter()
        full_response = ""
        tail_buffer = ""
        pending_leading = ""
        visible_text_emitted = False
        first_visible_chunk_emitted = False

        async def emit_response_chunk(content: str) -> None:
            nonlocal first_visible_chunk_emitted
            if not first_visible_chunk_emitted and on_first_visible_chunk is not None:
                first_visible_chunk_emitted = True
                await on_first_visible_chunk()
            await send(
                {
                    "type": "response.chunk",
                    "request_id": request_id,
                    "session_id": session_id,
                    "content": content,
                    "done": False,
                }
            )

        async def flush_safe_prefix(content: str) -> None:
            nonlocal pending_leading, visible_text_emitted
            if not content:
                return
            if visible_text_emitted:
                await emit_response_chunk(content)
                return
            pending_leading += content
            if pending_leading.strip():
                visible_text_emitted = True
                await emit_response_chunk(pending_leading)
                pending_leading = ""

        async for chunk in stream:
            if not chunk:
                continue
            full_response += chunk
            tail_buffer += chunk
            if len(tail_buffer) <= _MAX_CONTROL_TAG_LEN:
                continue

            safe_prefix = tail_buffer[:-_MAX_CONTROL_TAG_LEN]
            tail_buffer = tail_buffer[-_MAX_CONTROL_TAG_LEN:]
            await flush_safe_prefix(safe_prefix)

        stripped_tail = tail_buffer.rstrip()
        handoff_route: str | None = None
        awaiting_reply = stripped_tail.endswith(AWAITING_REPLY_TAG)
        handoff_requested = stripped_tail.endswith(HANDOFF_OPUS_TAG)
        if handoff_requested:
            remainder_without_tag = stripped_tail.removesuffix(HANDOFF_OPUS_TAG)
            if not visible_text_emitted and not (pending_leading + remainder_without_tag).strip():
                handoff_route = "opus"
                remainder = ""
                pending_leading = ""
            else:
                remainder = pending_leading + remainder_without_tag
                pending_leading = ""
        elif awaiting_reply:
            remainder = pending_leading + stripped_tail.removesuffix(AWAITING_REPLY_TAG)
            pending_leading = ""
        else:
            remainder = pending_leading + tail_buffer
            pending_leading = ""

        if remainder:
            await emit_response_chunk(remainder)

        display_text = full_response.rstrip()
        if display_text.endswith(HANDOFF_OPUS_TAG):
            display_text = display_text.removesuffix(HANDOFF_OPUS_TAG).rstrip()
        if display_text.endswith(AWAITING_REPLY_TAG):
            display_text = display_text.removesuffix(AWAITING_REPLY_TAG).rstrip()

        return StreamProcessingResult(
            content=display_text,
            awaiting_reply=awaiting_reply,
            handoff_route=handoff_route,
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
