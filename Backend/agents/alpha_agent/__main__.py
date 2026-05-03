"""Run the COSMIC Alpha Agent."""

from __future__ import annotations

import asyncio

from shared.redis_client import create_redis_client

from .agent import AlphaAgent
from .config import AlphaAgentConfig


async def main() -> None:
    config = AlphaAgentConfig.from_env()
    if not config.enabled:
        print("alpha_agent disabled; set ALPHA_AGENT_ENABLED=true to run.")
        return

    if not config.redis_url:
        raise RuntimeError("REDIS_URL is not configured for alpha_agent.")

    redis_client = create_redis_client(config.redis_url)
    agent = AlphaAgent(redis_client=redis_client, config=config)
    try:
        await agent.register()
        await agent.run()
    finally:
        await agent.stop()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
