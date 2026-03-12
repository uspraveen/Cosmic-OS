from __future__ import annotations

from typing import Any

import redis.asyncio as redis
from redis.exceptions import ResponseError


def create_redis_client(url: str) -> redis.Redis:
    return redis.from_url(
        url,
        decode_responses=True,
        health_check_interval=30,
    )


async def ensure_stream_group(
    client: redis.Redis,
    *,
    stream: str,
    group: str,
) -> None:
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def parse_stream_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    if isinstance(payload, str):
        import json

        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Redis stream payload is missing or malformed.")
