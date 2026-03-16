from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import redis.asyncio as redis

from .contracts import AgentError, AgentResult, TaskEnvelope, TaskInProgress, utcnow

RESULT_TTL_SEC = 86_400
DEDUPE_TTL_FALLBACK_SEC = 300
DEDUPE_TTL_MIN_SEC = 30


def to_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def deadline_remaining_sec(deadline_ts: datetime) -> float:
    return (to_utc_aware(deadline_ts) - utcnow()).total_seconds()


async def execute_with_idempotency(
    task: TaskEnvelope,
    handler: Callable[[TaskEnvelope], Awaitable[AgentResult | TaskInProgress]],
    client: redis.Redis,
    *,
    agent_max_duration_sec: int = 0,
) -> AgentResult | TaskInProgress:
    dedupe_key = f"idempotency:{task.idempotency_key}"
    result_key = f"idempotency:result:{task.idempotency_key}"

    stored_result = await client.get(result_key)
    if isinstance(stored_result, str) and stored_result.strip():
        return AgentResult.model_validate_json(stored_result)

    if task.deadline_ts is not None:
        remaining_sec = deadline_remaining_sec(task.deadline_ts)
        if remaining_sec <= 0:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="DEADLINE_EXCEEDED",
                    retryable=False,
                    message="Task deadline already exceeded before execution.",
                    next_action="skip",
                ),
            )
        base_ttl = int(remaining_sec) * 2
    elif agent_max_duration_sec > 0:
        base_ttl = int(agent_max_duration_sec) * 2
    else:
        base_ttl = DEDUPE_TTL_FALLBACK_SEC

    dedupe_ttl = max(DEDUPE_TTL_MIN_SEC, base_ttl)
    executing_since = utcnow()
    acquired = await client.set(
        dedupe_key,
        executing_since.isoformat().replace("+00:00", "Z"),
        nx=True,
        ex=dedupe_ttl,
    )
    if not acquired:
        raw = await client.get(dedupe_key)
        if isinstance(raw, str) and raw.strip():
            try:
                executing_since = to_utc_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                executing_since = utcnow()
        return TaskInProgress(
            task_id=task.task_id,
            idempotency_key=task.idempotency_key,
            executing_since=executing_since,
            check_after_sec=min(30, max(5, dedupe_ttl // 4)),
        )

    try:
        result = await handler(task)
        if isinstance(result, AgentResult):
            await client.set(
                result_key,
                result.model_dump_json(),
                ex=RESULT_TTL_SEC,
            )
        return result
    except Exception:
        await client.delete(dedupe_key)
        raise
