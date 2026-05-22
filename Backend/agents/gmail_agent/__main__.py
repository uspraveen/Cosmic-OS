from __future__ import annotations

import asyncio

from shared.redis_client import create_redis_client

from .agent import GmailAgent
from .config import GmailAgentConfig


async def main() -> None:
    config = GmailAgentConfig.from_env()
    if not config.redis_url:
        raise RuntimeError("REDIS_URL is not configured for gmail_agent.")
    redis_client = create_redis_client(config.redis_url)
    agent = GmailAgent(redis_client=redis_client, config=config)
    try:
        await agent.register()
        await agent.run()
    finally:
        await agent.stop()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
