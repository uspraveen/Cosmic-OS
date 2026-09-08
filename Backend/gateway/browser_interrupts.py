"""In-memory ask-user bridge for the browser agent.

A running `browser.run` specialist task can hit a page it cannot get past
without a human — an OTP/verification code, a CAPTCHA or bot check, "approve
this sign-in on your phone", an ambiguous form choice. `cosmic-browser-use`
already routes all of these through one `AskUser` action; the browser agent
wraps that action's handler with one blocking call into
`POST /internal/browser/ask-user` (see `browser/routes.py`), which waits here
until the desktop resolves it or the timeout elapses.

The browser agent — not the gateway — owns the request_id: it emits the
interrupt as part of its normal `task.progress` live-progress stream (so it
renders on the already-live `BrowserRunCard` with zero extra broadcast
plumbing) before making the blocking call, so the same id has to be used on
both sides. This store just tracks the wait.

Single-process, in-memory by design: an interrupt only outlives one
still-running browser session. If the gateway restarts, the browser agent's
blocking HTTP call drops too and its own non-fatal exception handling around
`ask_user_handler` takes over — nothing here needs to survive a restart.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrowserInterrupt:
    request_id: str
    question: str
    kind: str
    task_id: str | None
    session_id: str | None
    channel: str | None
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | answered | skipped | timeout
    answer: str = ""
    event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class BrowserInterruptManager:
    """Tracks in-flight AskUser interrupts for running browser.run tasks."""

    def __init__(self) -> None:
        self._pending: dict[str, BrowserInterrupt] = {}

    async def create_and_wait(
        self,
        *,
        request_id: str,
        question: str,
        kind: str,
        task_id: str | None,
        session_id: str | None,
        channel: str | None,
        timeout_sec: float,
    ) -> dict[str, Any]:
        """Register a pending interrupt and block until it's resolved or times out."""
        request_id = (request_id or "").strip()
        if not request_id:
            return {"request_id": "", "status": "error", "answer": ""}
        interrupt = BrowserInterrupt(
            request_id=request_id,
            question=question.strip(),
            kind=kind or "generic",
            task_id=task_id,
            session_id=session_id,
            channel=channel,
        )
        self._pending[request_id] = interrupt
        try:
            try:
                await asyncio.wait_for(interrupt.event.wait(), timeout=max(1.0, float(timeout_sec)))
            except asyncio.TimeoutError:
                interrupt.status = "timeout"
            return {
                "request_id": interrupt.request_id,
                "status": interrupt.status,
                "answer": interrupt.answer,
            }
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, answer: str) -> BrowserInterrupt | None:
        interrupt = self._pending.get((request_id or "").strip())
        if interrupt is None or interrupt.status != "pending":
            return None
        interrupt.answer = answer
        interrupt.status = "answered"
        interrupt.event.set()
        return interrupt

    def skip(self, request_id: str) -> BrowserInterrupt | None:
        interrupt = self._pending.get((request_id or "").strip())
        if interrupt is None or interrupt.status != "pending":
            return None
        interrupt.status = "skipped"
        interrupt.event.set()
        return interrupt

    def get(self, request_id: str) -> BrowserInterrupt | None:
        return self._pending.get((request_id or "").strip())
