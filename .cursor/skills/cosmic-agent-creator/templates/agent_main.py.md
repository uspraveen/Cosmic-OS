# Template: __main__.py

Match this to the local `AgentRuntime.register()` implementation.
The invariant is that the worker loop starts exactly once.

```python
# agents/<agent_name>/__main__.py
import asyncio
from .agent import <AgentClass>
from shared.redis_client import get_redis


async def main():
    redis = await get_redis()
    agent = <AgentClass>(redis=redis)
    await agent.register()
    # Call run() here ONLY if your local AgentRuntime.register()
    # does not already start consuming.
    # await agent.run()


if __name__ == '__main__':
    asyncio.run(main())
```

## Placeholder Reference

| Placeholder | Example |
|---|---|
| `<agent_name>` | `research_agent` (snake_case, matches directory) |
| `<AgentClass>` | `ResearchAgent` (PascalCase) |
