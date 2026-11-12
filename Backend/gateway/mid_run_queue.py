"""Accept user messages at any time, including while Cosmic is working.

Before this module the desktop composer was locked for the whole life of a
foreground request, and the gateway refused a second query on a busy channel
("A foreground task is already active on this channel"). The only way to say
anything mid-run was to push the current run to the background first.

Now every desktop/mobile ``query`` is accepted. When the channel is idle it
runs exactly as before. When the channel is busy the message is held here and
handled with the same semantics mature agent products use (Codex CLI's steer
mode, Claude Code's queued messages, Cursor's queue):

``steer`` (Enter, the default)
    Delivered *into* the running orchestrator task at its next safe step
    boundary: after the current model turn and tool calls finish and before
    the next model call. The model reads it before it picks its next step.
    The run reports delivery with ``task.user_message_delivered``; only then
    is the message written to session history (so history order matches what
    the model saw) and the UI told it was delivered.

``queue`` (Tab)
    Held until the current run ends, then dispatched as its own normal turn.

A steer that cannot be delivered - the run is a direct-model answer with no
step boundaries, the run already passed its last boundary, it was stopped or
failed, or the message carries attachments - silently falls back to a queued
follow-up, so nothing the user typed is ever dropped.

Stop keeps the queue: the current run is cancelled and whatever the user had
queued is sent next (Claude Code semantics). Each queued item can be
withdrawn (cancel, or edit = withdraw + put the text back in the composer)
until it has reached the model.

Exactly-once bookkeeping
------------------------
Every held message has one of two states:

* ``waiting``   - only the gateway has it.
* ``injecting`` - also handed to the target run's steer inbox.

A message leaves the queue in exactly one of three ways: delivered (the run
reported it), dispatched (it became its own turn), or withdrawn. When a target
run finalizes, its ``injecting`` messages fall back to ``waiting``: the
orchestrator closes its inbox before any terminal event and reports every
delivery on the same ordered stream as that terminal event, so "not reported
delivered by the end of the run" reliably means "the model never saw it".
All bookkeeping happens on the gateway's single event loop; the only awaits
are the orchestrator HTTP calls, and every post-await step re-checks that the
item is still in the state it left it in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runtime import ActiveRequest, GatewayRuntime

logger = logging.getLogger(__name__)

DeliveryMode = Literal["steer", "queue"]
ItemState = Literal["waiting", "injecting"]

# A user cannot reasonably queue more than this behind one run; past it we
# refuse loudly rather than grow memory without bound.
MAX_HELD_MESSAGES_PER_CHANNEL = 50


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class HeldMessage:
    request_id: str
    channel: str
    content: str
    mode: DeliveryMode
    payload: dict[str, Any]
    platform: str
    created_at: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    state: ItemState = "waiting"
    target_request_id: str | None = None
    target_task_id: str | None = None
    # Runs that refused this steer (inbox closed). Never retried into them.
    refused_by: set[str] = field(default_factory=set)
    withdrawing: bool = False

    @property
    def can_steer(self) -> bool:
        return self.mode == "steer" and not self.attachments

    def to_public(self) -> dict[str, Any]:
        if self.state == "injecting":
            public_state = "steering"
        elif self.can_steer:
            public_state = "pending_steer"
        else:
            public_state = "queued"
        return {
            "request_id": self.request_id,
            "content": self.content,
            "mode": self.mode,
            "state": public_state,
            "target_request_id": self.target_request_id,
            "created_at": self.created_at,
            "attachment_count": len(self.attachments),
            "withdrawing": self.withdrawing,
        }


class HeldMessageQueue:
    """Ordered per-channel store. Pure data structure; no I/O."""

    def __init__(self) -> None:
        self._by_channel: dict[str, list[HeldMessage]] = {}

    def items(self, channel: str) -> list[HeldMessage]:
        return list(self._by_channel.get(channel, ()))

    def get(self, channel: str, request_id: str) -> HeldMessage | None:
        for item in self._by_channel.get(channel, ()):
            if item.request_id == request_id:
                return item
        return None

    def add(self, item: HeldMessage) -> bool:
        bucket = self._by_channel.setdefault(item.channel, [])
        if any(existing.request_id == item.request_id for existing in bucket):
            return False
        if len(bucket) >= MAX_HELD_MESSAGES_PER_CHANNEL:
            raise ValueError(
                "Too many messages are waiting for Cosmic. Remove a few queued messages and try again."
            )
        bucket.append(item)
        return True

    def remove(self, item: HeldMessage) -> bool:
        bucket = self._by_channel.get(item.channel)
        if not bucket:
            return False
        for index, existing in enumerate(bucket):
            if existing is item:
                del bucket[index]
                if not bucket:
                    self._by_channel.pop(item.channel, None)
                return True
        return False

    def waiting(self, channel: str) -> list[HeldMessage]:
        return [
            item
            for item in self._by_channel.get(channel, ())
            if item.state == "waiting" and not item.withdrawing
        ]

    def release_target(self, request_id: str) -> list[HeldMessage]:
        """A run ended: everything it never reported delivered is waiting again."""
        released: list[HeldMessage] = []
        for bucket in self._by_channel.values():
            for item in bucket:
                if item.state == "injecting" and item.target_request_id == request_id:
                    item.state = "waiting"
                    item.target_request_id = None
                    item.target_task_id = None
                    released.append(item)
        return released

    def channels(self) -> list[str]:
        return list(self._by_channel)


class MidRunCoordinator:
    """Gateway glue between the realtime socket, active requests and the queue."""

    def __init__(self, runtime: "GatewayRuntime") -> None:
        self.runtime = runtime
        self.queue = HeldMessageQueue()
        self._adapters: dict[str, Any] = {}
        # Channels with a turn between "accepted" and "worker started".
        self._dispatching: dict[str, str] = {}
        # Requests whose orchestrator run has started (task.created seen), so
        # its steer inbox exists. Injecting earlier would be refused.
        self._steerable_requests: set[str] = set()
        self._pumping: set[str] = set()
        self._repump: set[str] = set()

    # ── Channel state ─────────────────────────────────────────────
    def foreground_request(self, channel: str) -> "ActiveRequest | None":
        """The user-visible run that owns the channel's composer, if any."""
        runtime = self.runtime
        runtime._background_autonomous_foreground_requests_for_channel(channel)
        for state in runtime.active_requests.values():
            if state.channel != channel or not state.foreground:
                continue
            if not state.completed:
                return state
            # Completed but still finalizing (assistant message being stored):
            # still busy, finalize will pump the queue.
            worker = state.worker
            if worker is not None and not worker.done():
                return state
        return None

    def is_busy(self, channel: str) -> bool:
        return (
            self.foreground_request(channel) is not None
            or channel in self._dispatching
            or bool(self.queue.waiting(channel))
        )

    def begin_dispatch(self, channel: str, request_id: str) -> None:
        self._dispatching[channel] = request_id

    def end_dispatch(self, channel: str, request_id: str) -> None:
        if self._dispatching.get(channel) == request_id:
            self._dispatching.pop(channel, None)
        self.schedule_pump(channel)

    def snapshot(self, channel: str) -> list[dict[str, Any]]:
        return [item.to_public() for item in self.queue.items(channel)]

    # ── Intake ────────────────────────────────────────────────────
    async def accept(
        self,
        payload: dict[str, Any],
        *,
        adapter: Any,
        channel: str,
        platform: str,
    ) -> bool:
        """Hold a query if the channel is busy. Returns True when held.

        Raises ValueError/TypeError for a malformed query, exactly like the
        normal path does, so the caller's error handling is unchanged.
        """
        if not self.is_busy(channel):
            return False
        # Validate now (the same normalizer the normal path uses) so a bad
        # message is rejected immediately instead of failing minutes later.
        normalized = adapter.normalize_message({**payload, "_channel": channel})
        request_id = str(payload.get("request_id") or "").strip() or str(
            normalized.get("request_id") or ""
        ).strip()
        if not request_id:
            raise ValueError("A queued message needs a request_id")
        content = str(payload.get("content") or "").strip() or str(
            normalized.get("content") or ""
        ).strip()
        attachments = payload.get("attachments")
        attachments = [item for item in attachments if isinstance(item, dict)] if isinstance(attachments, list) else []
        mode: DeliveryMode = "queue" if str(payload.get("delivery") or "").strip().lower() == "queue" else "steer"
        self._adapters[channel] = adapter
        item = HeldMessage(
            request_id=request_id,
            channel=channel,
            content=content,
            mode=mode,
            payload={**payload, "request_id": request_id},
            platform=platform,
            created_at=_utcnow_iso(),
            attachments=attachments,
        )
        added = self.queue.add(item)
        foreground = self.foreground_request(channel)
        logger.info(
            "gateway.mid_run.accepted channel=%s request_id=%s mode=%s duplicate=%s target=%s",
            channel,
            request_id,
            mode,
            not added,
            foreground.request_id if foreground else None,
        )
        existing = self.queue.get(channel, request_id)
        await self._send(
            channel,
            {
                "type": "user_message.queued",
                "request_id": request_id,
                "channel": channel,
                "mode": mode,
                "target_request_id": foreground.request_id if foreground else None,
                "item": existing.to_public() if existing else item.to_public(),
            },
        )
        await self._publish(channel)
        self.schedule_pump(channel)
        return True

    # ── Pump: inject steers / dispatch the next turn ──────────────
    def schedule_pump(self, channel: str) -> None:
        if not channel or not self.queue.items(channel):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.runtime._schedule_background_task(
            self._pump(channel), name=f"gateway-mid-run-pump:{channel}"
        )

    async def _pump(self, channel: str) -> None:
        if channel in self._pumping:
            self._repump.add(channel)
            return
        self._pumping.add(channel)
        try:
            while True:
                self._repump.discard(channel)
                await self._pump_once(channel)
                if channel not in self._repump:
                    break
        except Exception:
            logger.exception("gateway.mid_run.pump_failed channel=%s", channel)
        finally:
            self._pumping.discard(channel)

    async def _pump_once(self, channel: str) -> None:
        # Bounded: each pass either injects, dispatches one item, or stops.
        for _ in range(MAX_HELD_MESSAGES_PER_CHANNEL + 1):
            waiting = self.queue.waiting(channel)
            if not waiting:
                return
            foreground = self.foreground_request(channel)
            if foreground is not None:
                for item in waiting:
                    if self._can_inject(item, foreground):
                        await self._inject(item, foreground)
                # Follow-ups wait for this run to end.
                return
            if channel in self._dispatching:
                return
            await self._dispatch(waiting[0])

    def _can_inject(self, item: HeldMessage, foreground: "ActiveRequest") -> bool:
        runtime = self.runtime
        return (
            item.can_steer
            and item.state == "waiting"
            and not item.withdrawing
            and foreground.request_id not in item.refused_by
            and foreground.request_id in self._steerable_requests
            and not foreground.cancel_requested
            and not foreground.completed
            and bool(foreground.task_id)
            and runtime._is_orchestrator_route(foreground.route)
            and callable(getattr(runtime.orchestrator, "submit_task_message", None))
        )

    async def _inject(self, item: HeldMessage, foreground: "ActiveRequest") -> None:
        target_request_id = foreground.request_id
        task_id = str(foreground.task_id or "")
        item.state = "injecting"
        item.target_request_id = target_request_id
        item.target_task_id = task_id
        try:
            accepted = await self.runtime.orchestrator.submit_task_message(
                task_id, message_id=item.request_id, content=item.content
            )
        except Exception:
            logger.warning(
                "gateway.mid_run.inject_failed request_id=%s target=%s",
                item.request_id,
                target_request_id,
                exc_info=True,
            )
            accepted = False
        # Re-check after the await: the run may have ended (release_target),
        # or the item may have been withdrawn or delivered meanwhile.
        current = self.queue.get(item.channel, item.request_id)
        if current is not item or item.state != "injecting" or item.target_request_id != target_request_id:
            return
        if not accepted:
            item.state = "waiting"
            item.target_request_id = None
            item.target_task_id = None
            item.refused_by.add(target_request_id)
        logger.info(
            "gateway.mid_run.inject request_id=%s target=%s accepted=%s",
            item.request_id,
            target_request_id,
            accepted,
        )
        await self._publish(item.channel)

    async def _dispatch(self, item: HeldMessage) -> None:
        """Run a held message as its own turn, through the normal intake path."""
        channel = item.channel
        if not self.queue.remove(item):
            return
        adapter = self._adapters.get(channel)
        self.begin_dispatch(channel, item.request_id)
        try:
            await self._send(
                channel,
                {
                    "type": "user_message.dispatched",
                    "request_id": item.request_id,
                    "channel": channel,
                    "content": item.content,
                    "mode": item.mode,
                    "attachments": item.attachments or None,
                },
            )
            await self._publish(channel)
            if adapter is None:
                raise RuntimeError("The desktop connection for this message is gone.")
            await self.runtime.run_realtime_query(item.payload, adapter=adapter, channel=channel)
        except Exception as exc:
            logger.exception("gateway.mid_run.dispatch_failed request_id=%s", item.request_id)
            await self._send(
                channel,
                {
                    "type": "error",
                    "request_id": item.request_id,
                    "session_id": None,
                    "channel": channel,
                    "code": "UPSTREAM_ERROR",
                    "message": str(exc) or "Unable to send the queued message.",
                },
            )
        finally:
            if self._dispatching.get(channel) == item.request_id:
                self._dispatching.pop(channel, None)
            # No schedule_pump here: _pump_once's loop continues on its own.

    # ── Hooks from the request lifecycle ──────────────────────────
    def on_run_started(self, state: "ActiveRequest") -> None:
        """The orchestrator run exists (task.created): steers may go in."""
        self._steerable_requests.add(state.request_id)
        self.schedule_pump(state.channel)

    async def on_delivered(self, state: "ActiveRequest", message_ids: list[Any]) -> None:
        delivered: list[HeldMessage] = []
        for raw_id in message_ids:
            message_id = str(raw_id or "").strip()
            item = self.queue.get(state.channel, message_id)
            if item is None or item.state != "injecting" or item.target_request_id != state.request_id:
                logger.warning(
                    "gateway.mid_run.unexpected_delivery request_id=%s message_id=%s",
                    state.request_id,
                    message_id,
                )
                continue
            self.queue.remove(item)
            delivered.append(item)
        for item in delivered:
            self.runtime._record_steered_user_message(item, into=state)
            await self._send(
                item.channel,
                {
                    "type": "user_message.delivered",
                    "request_id": item.request_id,
                    "into_request_id": state.request_id,
                    "task_id": state.task_id,
                    "session_id": state.session_id,
                    "channel": item.channel,
                    "content": item.content,
                },
            )
        if delivered:
            await self._publish(state.channel)

    def on_request_finalized(self, state: "ActiveRequest") -> None:
        self._steerable_requests.discard(state.request_id)
        released = self.queue.release_target(state.request_id)
        if released:
            logger.info(
                "gateway.mid_run.released request_id=%s count=%s",
                state.request_id,
                len(released),
            )
        channels = {state.channel, *(item.channel for item in released)}
        for channel in channels:
            if self.queue.items(channel):
                self.runtime._schedule_background_task(
                    self._publish(channel), name=f"gateway-mid-run-publish:{channel}"
                )
                self.schedule_pump(channel)

    def on_request_backgrounded(self, state: "ActiveRequest") -> None:
        # The composer slot is free again: queued follow-ups go now. Steers
        # already handed to the backgrounded run stay with it.
        self.schedule_pump(state.channel)

    # ── Withdraw (cancel / edit a queued message) ─────────────────
    async def withdraw(
        self, channel: str, request_id: str, *, adapter: Any | None = None
    ) -> dict[str, Any]:
        if adapter is not None:
            self._adapters[channel] = adapter
        item = self.queue.get(channel, request_id)
        result: dict[str, Any] = {
            "type": "user_message.withdrawn",
            "request_id": request_id,
            "channel": channel,
            "withdrawn": False,
            "content": item.content if item else None,
        }
        if item is None or item.withdrawing:
            result["reason"] = "not_found" if item is None else "in_progress"
            await self._send(channel, result)
            return result
        withdrawn = False
        if item.state == "waiting":
            withdrawn = self.queue.remove(item)
        else:
            item.withdrawing = True
            ok = False
            try:
                withdraw = getattr(self.runtime.orchestrator, "withdraw_task_message", None)
                if callable(withdraw) and item.target_task_id:
                    ok = bool(await withdraw(item.target_task_id, message_id=item.request_id))
            except Exception:
                logger.warning("gateway.mid_run.withdraw_failed request_id=%s", request_id, exc_info=True)
            item.withdrawing = False
            still_held = self.queue.get(channel, request_id) is item
            if still_held and (ok or item.state == "waiting"):
                # Either the run gave it back, or the run ended without ever
                # reading it (released to waiting during the await).
                withdrawn = self.queue.remove(item)
            else:
                # Drained into the run during the await: the model has it.
                result["reason"] = "already_delivered"
        result["withdrawn"] = withdrawn
        logger.info("gateway.mid_run.withdraw request_id=%s withdrawn=%s", request_id, withdrawn)
        await self._send(channel, result)
        await self._publish(channel)
        self.schedule_pump(channel)
        return result

    # ── Transport ─────────────────────────────────────────────────
    async def _publish(self, channel: str) -> None:
        await self._send(
            channel,
            {
                "type": "user_message.queue",
                "channel": channel,
                "items": self.snapshot(channel),
            },
        )

    async def _send(self, channel: str, event: dict[str, Any]) -> None:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return
        try:
            await adapter.send(event, channel=channel)
        except Exception:
            # The socket may be gone; the resume payload carries the queue.
            logger.info(
                "gateway.mid_run.send_skipped channel=%s type=%s", channel, event.get("type")
            )
