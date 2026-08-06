"""A login CLI's sign-in URL has to reach the desktop, newline or not.

`cursor-agent login` and `codex login` print the URL and then sit on a spinner,
redrawing with carriage returns. Line-based capture blocks until a newline that
never comes, so the one line the panel exists to show is the one line it never
gets. Capture splits on either terminator and surfaces an unterminated tail.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime  # noqa: E402


class _Stream:
    """Feeds `read()` a scripted sequence of chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


async def _capture(chunks: list[bytes]) -> list[str]:
    lines: list[str] = []
    await GatewayRuntime._capture_codex_stream(
        object.__new__(GatewayRuntime), _Stream(chunks), lines
    )
    return lines


URL = "https://cursor.com/loginDeepControl?challenge=abc"


def test_a_url_with_no_trailing_newline_still_reaches_the_panel() -> None:
    """The production shape: URL, then a spinner that never ends the line."""
    lines = asyncio.run(
        _capture([f"To sign in, visit:\n{URL}".encode(), b"\r  Waiting", b"\r  Waiting."])
    )
    assert any(URL in line for line in lines)


def test_a_terminated_line_replaces_its_own_provisional_copy() -> None:
    lines = asyncio.run(_capture([b"Signing in", b" now\n", b"Done\n"]))
    assert lines == ["Signing in now", "Done"]


def test_plain_newline_output_is_unchanged() -> None:
    lines = asyncio.run(_capture([b"first\nsecond\n", b"third\n"]))
    assert lines == ["first", "second", "third"]


def test_a_line_split_across_chunks_is_not_torn_apart() -> None:
    lines = asyncio.run(_capture([b"https://cursor.com/log", b"in?x=1\n"]))
    assert lines == ["https://cursor.com/login?x=1"]


def test_blank_output_produces_no_lines() -> None:
    assert asyncio.run(_capture([b"\n\n", b"   \n"])) == []
    assert asyncio.run(_capture([])) == []


def test_a_chatty_cli_cannot_grow_the_session_without_bound() -> None:
    chunks = [f"line {index}\n".encode() for index in range(200)]
    lines = asyncio.run(_capture(chunks))
    assert len(lines) == 30
    assert lines[-1] == "line 199"


def test_a_spinner_never_pushes_the_url_out_of_the_window() -> None:
    """Carriage-return redraws are frequent; the URL must survive them.

    It cannot in general - the window is bounded - so this pins the honest
    guarantee: the most recent 30 segments, with the live tail always last.
    """
    chunks = [f"{URL}\n".encode()] + [b"\r  Waiting" for _ in range(50)]
    lines = asyncio.run(_capture(chunks))
    assert lines[-1] == "Waiting"
    assert len(lines) <= 30


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\r"])
def test_every_terminator_a_cli_might_use_ends_a_line(terminator: bytes) -> None:
    lines = asyncio.run(_capture([b"alpha" + terminator + b"beta" + terminator]))
    assert lines == ["alpha", "beta"]


def test_a_missing_stream_is_not_an_error() -> None:
    lines: list[str] = []
    asyncio.run(GatewayRuntime._capture_codex_stream(object.__new__(GatewayRuntime), None, lines))
    assert lines == []
