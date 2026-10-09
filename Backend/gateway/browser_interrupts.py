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
    page_url: str = ""
    commit: dict[str, Any] = field(default_factory=dict)
    # Corrected values from a commit card: the user edited a field's value
    # before approving. Only ever populated on an approving answer, and only
    # for fields the card actually showed — the engine writes them into the
    # form just before the authorized commit fires.
    field_edits: list[dict[str, str]] = field(default_factory=list)
    # "Say something in addition": free-text instruction the user attached to
    # whatever action they took (approve, deny, an option chip). Distinct from
    # the answer itself — it rides alongside, for the agent to act on.
    note: str = ""
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
        page_url: str | None = None,
        commit: dict[str, Any] | None = None,
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
            page_url=str(page_url or "").strip(),
            commit=dict(commit) if isinstance(commit, dict) else {},
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
                "field_edits": list(interrupt.field_edits),
                "note": interrupt.note,
            }
        finally:
            self._pending.pop(request_id, None)

    def resolve(
        self,
        request_id: str,
        answer: str,
        field_edits: list[dict[str, str]] | None = None,
        note: str = "",
    ) -> BrowserInterrupt | None:
        interrupt = self._pending.get((request_id or "").strip())
        if interrupt is None or interrupt.status != "pending":
            return None
        interrupt.answer = answer
        interrupt.field_edits = [
            {"label": label, "value": value}
            for edit in (field_edits or [])
            if isinstance(edit, dict)
            for label in [str(edit.get("label") or "").strip()[:80]]
            for value in [str(edit.get("value") or "").strip()[:200]]
            if label and value
        ]
        interrupt.note = str(note or "").strip()[:500]
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

    # Rides back to the engine as the deny reason the model reads.
    TAKEOVER_NOTE = (
        "The user took control of the browser instead of answering. Do not retry this; "
        "after they hand control back, re-read the page — they may have done it themselves."
    )

    def release_for_takeover(self, task_id: str | None) -> list[BrowserInterrupt]:
        """Free a run that is blocked on a card so a takeover can actually start.

        A run only parks between steps, and a step waiting on a card never
        ends — so "Take control" did nothing until the user answered the card
        (2026-10-08: pause at 20:56:44, card answered 20:57:05, control 21s
        later). Taking control IS the answer: a pending commit is denied with
        a note saying why, and any other question is skipped (never answered:
        a password card must not receive text as its secret).
        """
        wanted = str(task_id or "").strip()
        if not wanted:
            return []
        released: list[BrowserInterrupt] = []
        for interrupt in list(self._pending.values()):
            if interrupt.status != "pending" or str(interrupt.task_id or "").strip() != wanted:
                continue
            if interrupt.kind == "commit":
                interrupt.answer = "deny"
                interrupt.note = self.TAKEOVER_NOTE
                interrupt.status = "answered"
            else:
                interrupt.status = "skipped"
            interrupt.event.set()
            released.append(interrupt)
        return released
