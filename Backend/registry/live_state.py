from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis

from shared import Heartbeat, heartbeat_key, intent_members_key, utcnow


def heartbeat_mapping(heartbeat: Heartbeat, *, status: str | None = None) -> dict[str, str]:
    return {
        "status": status or ("healthy" if heartbeat.healthy else "unhealthy"),
        "current_load": str(int(heartbeat.current_load)),
        "max_conc": str(int(heartbeat.max_concurrency)),
        "heartbeat_ttl": str(int(heartbeat.heartbeat_ttl_sec)),
        "last_seen": heartbeat.last_seen.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def register_intent_index(agent_id: str, card: dict[str, Any], client: redis.Redis) -> None:
    intents = card.get("intents") if isinstance(card.get("intents"), list) else []
    for item in intents:
        if not isinstance(item, dict):
            continue
        intent_name = str(item.get("name") or "").strip()
        if intent_name:
            await client.sadd(intent_members_key(intent_name), agent_id)


async def deregister_intent_index(agent_id: str, card: dict[str, Any], client: redis.Redis) -> None:
    intents = card.get("intents") if isinstance(card.get("intents"), list) else []
    for item in intents:
        if not isinstance(item, dict):
            continue
        intent_name = str(item.get("name") or "").strip()
        if intent_name:
            await client.srem(intent_members_key(intent_name), agent_id)


async def write_heartbeat(
    heartbeat: Heartbeat,
    client: redis.Redis,
    *,
    status: str | None = None,
) -> str:
    key = heartbeat_key(heartbeat.agent_id, heartbeat.instance_id)
    await client.hset(key, mapping=heartbeat_mapping(heartbeat, status=status))
    await client.expire(key, int(heartbeat.heartbeat_ttl_sec) + 5)
    return key


async def read_instance_state(agent_id: str, instance_id: str, client: redis.Redis) -> dict[str, str]:
    return await client.hgetall(heartbeat_key(agent_id, instance_id))


async def find_available_instance(
    intent: str,
    client: redis.Redis,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    current_time = (now or utcnow()).astimezone(timezone.utc)
    agent_ids = await client.smembers(intent_members_key(intent))

    for agent_id in sorted(agent_ids):
        candidate = await find_available_instance_for_agent(str(agent_id), client, now=current_time)
        if candidate != (None, None):
            return candidate

    return None, None


async def find_available_instance_for_agent(
    agent_id: str,
    client: redis.Redis,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    current_time = (now or utcnow()).astimezone(timezone.utc)
    cursor = 0
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        return None, None

    while True:
        cursor, keys = await client.scan(
            cursor=cursor,
            match=f"registry:{normalized_agent_id}:*",
            count=20,
        )
        for key in keys:
            state = await client.hgetall(key)
            if _state_is_available(state, now=current_time):
                instance_id = key.split(":")[-1]
                return normalized_agent_id, instance_id
        if cursor == 0:
            break
    return None, None


def _safe_int(value: Any, *, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _parse_utc_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _state_is_available(state: dict[str, Any], *, now: datetime) -> bool:
    if not state:
        return False
    if str(state.get("status") or "").strip() != "healthy":
        return False
    load = _safe_int(state.get("current_load"), fallback=10**9)
    max_conc = _safe_int(state.get("max_conc"), fallback=0)
    ttl = _safe_int(state.get("heartbeat_ttl"), fallback=0)
    last_seen = _parse_utc_timestamp(state.get("last_seen"))
    if last_seen is None or ttl <= 0 or load >= max_conc:
        return False
    if (now - last_seen).total_seconds() >= ttl:
        return False
    return True
