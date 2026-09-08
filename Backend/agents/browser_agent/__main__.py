"""Run the COSMIC Browser Agent."""

from __future__ import annotations

import asyncio

import redis.asyncio as redis

from .agent import BrowserAgent
from .config import BrowserAgentConfig


async def main() -> None:
    cfg = BrowserAgentConfig.from_env()
    client = redis.from_url(cfg.redis_url, decode_responses=True)
    agent = BrowserAgent(client, config=cfg)
    try:
        await agent.register()
        await agent.run()
    finally:
        await agent.stop()
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
