from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


async def iter_stream_lines(
    stream: Any,
    *,
    max_line_bytes: int = 8 * 1024 * 1024,
    chunk_size: int = 64 * 1024,
    omitted_event_type: str = "cosmic.large_cli_event_omitted",
) -> AsyncIterator[str]:
    """Yield newline-delimited CLI output without asyncio readline limits.

    Cursor and Codex can emit JSONL events containing large file edits. Python's
    StreamReader.readline() uses readuntil() internally and raises
    LimitOverrunError when a single event exceeds the stream limit. This reader
    chunks manually, emits normal lines unchanged, and replaces oversized lines
    with a small synthetic JSON event so terminal streaming keeps moving.
    """

    buffer = bytearray()
    discarding_large_line = False
    omitted_bytes = 0

    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            if buffer and not discarding_large_line:
                yield buffer.decode("utf-8", errors="replace")
            elif discarding_large_line:
                yield _omitted_line(omitted_event_type, omitted_bytes)
            return

        start = 0
        while start < len(chunk):
            newline_index = chunk.find(b"\n", start)
            if newline_index == -1:
                piece = chunk[start:]
                start = len(chunk)
                line_complete = False
            else:
                piece = chunk[start : newline_index + 1]
                start = newline_index + 1
                line_complete = True

            if discarding_large_line:
                omitted_bytes += len(piece)
                if line_complete:
                    yield _omitted_line(omitted_event_type, omitted_bytes)
                    discarding_large_line = False
                    omitted_bytes = 0
                continue

            if len(buffer) + len(piece) > max_line_bytes:
                omitted_bytes = len(buffer) + len(piece)
                buffer.clear()
                if line_complete:
                    yield _omitted_line(omitted_event_type, omitted_bytes)
                    omitted_bytes = 0
                else:
                    discarding_large_line = True
                continue

            buffer.extend(piece)
            if line_complete:
                yield buffer.decode("utf-8", errors="replace")
                buffer.clear()


def compact_for_memory(line: str, *, max_line_chars: int = 1_000_000) -> str:
    value = str(line or "")
    if len(value) <= max_line_chars:
        return value
    return (
        json.dumps(
            {
                "type": "cosmic.large_cli_event_compacted",
                "message": f"Large CLI JSON event compacted from {len(value)} characters.",
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _omitted_line(event_type: str, omitted_bytes: int) -> str:
    return (
        json.dumps(
            {
                "type": event_type,
                "message": f"Large CLI stream event omitted after {omitted_bytes} bytes.",
                "omitted_bytes": omitted_bytes,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
