"""Mid-run user messages ("steers") for a running orchestrator task.

A user can keep typing while Cosmic works. When the gateway receives a
message for a channel whose foreground request is an orchestrator run, it
hands the text to that run's :class:`SteerInbox`. The run drains the inbox at
its next *safe step boundary* - right before the next model call, after the
previous turn's tool results are in the transcript - so the model reads the
message before it decides its next step. Nothing is ever spliced into the
middle of a streaming model turn or a running tool call.

Delivery contract (shared with the gateway, see
``gateway/mid_run_queue.py``):

* ``submit`` succeeds only while the inbox is open. The run closes the inbox
  when its tool loop ends, *before* it emits any terminal event, so a submit
  that races the end of a run is refused and the gateway keeps the message
  for a follow-up turn instead.
* ``drain`` is the only way a message reaches the model. The run reports each
  drained batch with a ``task.user_message_delivered`` event, which travels on
  the same ordered stream as the run's terminal event. Anything the gateway
  handed over but never saw reported as delivered by the time the run ends is
  therefore known to be undelivered and is re-dispatched exactly once.
* ``withdraw`` removes a message that has not been drained yet. It still works
  after the inbox is closed, so cancelling a queued message can never lose the
  race against the run ending.

Everything here runs on the orchestrator's single asyncio loop and never
awaits, so each operation is atomic with respect to the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Upper bounds that keep a hostile or runaway client from growing a run's
# context without limit. A real user never gets near these.
MAX_PENDING_STEERS = 20
MAX_STEER_CHARS = 8000

STEER_PREAMBLE = (
    "[New message from the user, sent while you were working on this request. "
    "You have not seen it before. Read it now and take it into account from this "
    "step on - it may add context, change priorities, or redirect or cancel the "
    "rest of the work. Do not restart work that is already done unless it asks you to.]"
)


@dataclass(slots=True)
class PendingSteer:
    message_id: str
    content: str


@dataclass(slots=True)
class SteerInbox:
    open: bool = True
    pending: list[PendingSteer] = field(default_factory=list)
    delivered_ids: set[str] = field(default_factory=set)

    def submit(self, message_id: str, content: str) -> bool:
        """Accept a steer for the next step boundary.

        Idempotent per ``message_id``: a retried submit of a message that is
        already pending or already delivered reports success without adding a
        second copy, so a gateway retry can never duplicate a message.
        """
        normalized_id = str(message_id or "").strip()
        text = str(content or "").strip()
        if not normalized_id or not text:
            return False
        if normalized_id in self.delivered_ids:
            return True
        if any(item.message_id == normalized_id for item in self.pending):
            return True
        if not self.open or len(self.pending) >= MAX_PENDING_STEERS:
            return False
        self.pending.append(PendingSteer(message_id=normalized_id, content=text[:MAX_STEER_CHARS]))
        return True

    def withdraw(self, message_id: str) -> bool:
        """Remove a steer that has not reached the model yet."""
        normalized_id = str(message_id or "").strip()
        for index, item in enumerate(self.pending):
            if item.message_id == normalized_id:
                del self.pending[index]
                return True
        return False

    def drain(self) -> list[PendingSteer]:
        if not self.open or not self.pending:
            return []
        drained = list(self.pending)
        self.pending.clear()
        self.delivered_ids.update(item.message_id for item in drained)
        return drained

    def close(self) -> list[PendingSteer]:
        """Stop accepting steers; returns whatever was never delivered."""
        self.open = False
        return list(self.pending)


def render_steers_for_model(steers: list[PendingSteer]) -> str:
    if not steers:
        return ""
    if len(steers) == 1:
        return f"{STEER_PREAMBLE}\n\n{steers[0].content}"
    numbered = "\n\n".join(
        f"Message {index}:\n{item.content}" for index, item in enumerate(steers, 1)
    )
    return (
        f"{STEER_PREAMBLE}\n\nThe user sent {len(steers)} messages, oldest first.\n\n{numbered}"
    )


def last_message_accepts_user_text(messages: list[dict[str, Any]]) -> bool:
    """True when the transcript is at a boundary where user text may be added.

    That is the case when the most recent message is a user turn (the initial
    query, or the tool results of the previous step). After an Anthropic
    ``pause_turn`` the last message is the assistant's partial turn, which
    must be continued as-is, so steering waits for the next boundary.
    OpenAI-style ``tool`` messages are also a boundary: a user message may
    follow them.
    """
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict):
        return False
    return last.get("role") in {"user", "tool"}
