from __future__ import annotations

import asyncio

from shared.redis_client import create_redis_client

from .agent import XTwitterSearchAgent
from .config import XTwitterSearchConfig


async def main() -> None:
    config = XTwitterSearchConfig.from_env()
    if not config.redis_url:
        raise RuntimeError("REDIS_URL is not configured for x_twitter_search.")

    redis_client = create_redis_client(config.redis_url)
    agent = XTwitterSearchAgent(redis_client=redis_client, config=config)
    try:
        await agent.register()
        await agent.run()
    finally:
        await agent.stop()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

