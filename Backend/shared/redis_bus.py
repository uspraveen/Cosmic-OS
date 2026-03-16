from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from .contracts import CURRENT_WRITE_VERSION, EventEnvelope, TaskEnvelope, sign_task_envelope, validate_outbound_version

STREAM_MAXLEN = 10_000
STREAM_MAXLEN_APPROX = 12_000
EVENTS_STREAM_MAXLEN = 50_000
EVENTS_STREAM = "streams:events"


class BackpressureError(RuntimeError):
    """Raised when a task stream is over capacity and should not accept more work."""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    stream: str
    message_id: str


def task_stream_name(recipient: str, priority: str) -> str:
    normalized_recipient = str(recipient or "").strip()
    normalized_priority = str(priority or "").strip() or "normal"
    if not normalized_recipient:
        raise ValueError("recipient is required")
    if normalized_priority not in {"high", "normal", "low"}:
        raise ValueError(f"Unsupported priority: {priority!r}")
    return f"streams:{normalized_recipient}:{normalized_priority}"


def heartbeat_key(agent_id: str, instance_id: str) -> str:
    normalized_agent_id = str(agent_id or "").strip()
    normalized_instance_id = str(instance_id or "").strip()
    if not normalized_agent_id or not normalized_instance_id:
        raise ValueError("agent_id and instance_id are required")
    return f"registry:{normalized_agent_id}:{normalized_instance_id}"


def intent_members_key(intent: str) -> str:
    normalized_intent = str(intent or "").strip()
    if not normalized_intent:
        raise ValueError("intent is required")
    return f"intent:{normalized_intent}"


def prepare_for_redispatch(task: TaskEnvelope, *, new_epoch: int, signing_secret: str) -> TaskEnvelope:
    """Re-stamp a stored task for redispatch without changing its idempotency key."""
    updated = task.model_copy(
        update={
            "leader_epoch": new_epoch,
            "contract_version": CURRENT_WRITE_VERSION,
            "signature": "",
        }
    )
    signature = sign_task_envelope(updated, signing_secret)
    return updated.model_copy(update={"signature": signature})


async def dispatch_task(
    task: TaskEnvelope,
    client: redis.Redis,
    *,
    stream_maxlen: int = STREAM_MAXLEN,
    stream_maxlen_approx: int = STREAM_MAXLEN_APPROX,
) -> DispatchResult:
    validate_outbound_version(task.contract_version)
    stream = task_stream_name(task.recipient, task.priority)
    length = int(await client.xlen(stream))
    if length > stream_maxlen:
        raise BackpressureError(f"Stream {stream} is over capacity ({length}/{stream_maxlen})")
    message_id = await client.xadd(
        stream,
        {"envelope": task.model_dump_json()},
        maxlen=stream_maxlen_approx,
        approximate=True,
    )
    return DispatchResult(stream=stream, message_id=message_id)


async def emit_event(
    event: EventEnvelope,
    client: redis.Redis,
    *,
    stream: str = EVENTS_STREAM,
    maxlen: int = EVENTS_STREAM_MAXLEN,
) -> DispatchResult:
    validate_outbound_version(event.contract_version)
    message_id = await client.xadd(
        stream,
        {"event": event.model_dump_json()},
        maxlen=maxlen,
        approximate=True,
    )
    return DispatchResult(stream=stream, message_id=message_id)


def parse_task_envelope(fields: dict[str, Any]) -> TaskEnvelope:
    raw = fields.get("envelope")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Redis task stream entry is missing the envelope field.")
    return TaskEnvelope.model_validate_json(raw)


def parse_event_envelope(fields: dict[str, Any]) -> EventEnvelope:
    raw = fields.get("event")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Redis event stream entry is missing the event field.")
    return EventEnvelope.model_validate_json(raw)


def parse_json_fields(fields: dict[str, Any], *, field: str) -> dict[str, Any]:
    raw = fields.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Redis entry is missing the {field!r} field.")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"Redis field {field!r} did not decode to an object.")
    return parsed
