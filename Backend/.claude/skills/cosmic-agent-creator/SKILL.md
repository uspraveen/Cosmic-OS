---
name: cosmic-agent-creator
description: >
  Build a new COSMIC agent or subagent from scratch. Use when creating research_agent, docs_agent,
  browser_agent, system_agent, cli_agent, diagram_agent, or any new specialist agent for the
  COSMIC multi-agent personal assistant system. Generates all required files, schemas, tests,
  and configuration following the exact architecture spec (cosmic_architecture.md v1.6).
argument-hint: <agent-name>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# COSMIC Agent Creator

You are building a specialist agent for **COSMIC** — a single-user, multi-agent personal assistant.
Every mistake here propagates to every agent built with this skill. Follow these instructions exactly.

## Before You Start

1. Read the full architecture spec for the section relevant to the agent you're building:
   `cosmic_architecture.md` (in the same directory as this skill's project)
2. Read the reference files in this skill's `reference/` directory for contracts and standards
3. Read the templates in this skill's `templates/` directory for boilerplate

## System Overview (The Crux)

COSMIC is a **Gateway → Orchestrator → Agents** pipeline:

```
User → Channel Adapter → Gateway (FastAPI :8080) → Model Router (FastAPI :8742)
                              ↓ (if route=opus)
                         Orchestrator (Claude Opus, consumes from Redis Streams)
                              ↓ (dispatches TaskEnvelopes)
                         Specialist Agents (consume from per-agent Redis Streams)
                              ↓ (emit EventEnvelopes)
                         Orchestrator (consumes events, routes responses)
                              ↓
                         Gateway → Channel Adapter → User
```

**Key facts:**
- **Transport:** Redis Streams with consumer groups. Three priority tiers per agent: high/normal/low.
- **Storage:** SQLite (WAL mode, busy_timeout=5000ms) for persistence. Redis for ephemeral state.
- **Contracts:** `shared/contracts.py` — Pydantic models (TaskEnvelope, EventEnvelope, AgentResult, AgentError).
- **Auth:** HMAC-SHA256 per agent-orchestrator channel. Each agent has ONE secret (`AGENT_SECRET` env var).
- **Idempotency:** Replay stored result first, then enforce deadline, then acquire the Redis lock. This ordering is mandatory.
- **All communication is orchestrator-mediated.** Agents NEVER talk to each other directly.
- **Single-user-per-VM.** No multi-tenancy. No user isolation logic needed in agents.

## Step-by-Step Agent Creation Workflow

### Step 1: Define the Agent's Identity

Determine these values:
- `agent_id`: `cosmic/<name>-agent:<semver>` (e.g., `cosmic/research-agent:1.0.0`)
- `display_name`: Human-readable (e.g., `Research Agent`)
- `description`: One-paragraph capability summary
- `intents`: What this agent can do (e.g., `research.topic`, `docs.edit`)
- `tools`: What tools this agent needs (e.g., `web_search`, `playwright_navigate`)
- `auth_requirements`: Which intents need user OAuth credentials (if any)
- `resource_resolution`: whether users may name remote provider resources by human label instead of stable IDs (docs, files, repos, issues, calendars, etc.). If yes, add a `<domain>.resolve_resource` intent and schemas.

### Step 2: Create the Directory Structure

```
agents/<agent_name>/
├── agent_card.yaml             # Capability declaration (see template)
├── __main__.py                 # Entry point: python -m agents.<agent_name>
├── agent.py                    # Core agent logic — execute() method
│
├── prompts/                    # READ-ONLY at runtime
│   ├── system.md               # System prompt — who the agent is
│   └── policies.md             # Rules, constraints, tool usage policies
│
├── skills/
│   └── SKILLS.md               # Domain knowledge, techniques, reference material
│
├── schemas/                    # Machine-readable API contracts
│   ├── intents/                # JSON Schema per intent (input + output)
│   │   ├── <intent>.input.json
│   │   └── <intent>.output.json
│   └── events/                 # Event payload schemas (optional)
│
├── store/                      # PERSISTENT — survives restarts, must be on persistent volume
│   ├── learnings.md            # Accumulated agent knowledge (updated after tasks)
│   └── data/                   # Agent-managed storage — YOUR schema, YOUR tables
│       └── sessions.db         # (or whatever the agent needs)
│
├── runtime/                    # EPHEMERAL — gitignored, recreated on restart
│   ├── state.db                # In-flight task state (suspension serialization)
│   ├── cache/                  # Cached fetches, embeddings
│   └── logs/                   # Agent-level structured logs
│
└── tests/                      # Agent tests
    ├── test_agent.py           # Unit tests for execute() logic
    ├── test_intents.py         # Intent handler tests with mocked dependencies
    ├── test_schemas.py         # Schema validation tests
    └── conftest.py             # Shared fixtures (mock redis, mock task envelopes)
```

### Step 3: Write agent_card.yaml

Use the template at `templates/agent_card.yaml.md`. Key rules:
- Every intent MUST have `input_schema` and `output_schema` pointing to JSON Schema files
- Every intent MUST have a `timeout_sec` matching the SLA
- `auth_requirements` ONLY for intents calling external provider APIs on behalf of the user
- If users may refer to provider-owned resources by name, add a `<domain>.resolve_resource` intent. The orchestrator selects ONE account at a time and passes a single `input.auth`; the agent searches only that account and returns matches. Agents NEVER guess accounts or own resource-binding storage.
- `allowed_senders` must include `cosmic/orchestrator:1.0.0`
- `stream_key` must be `streams:<agent_id>` (no priority suffix — that's added by transport)
- `retry_policy` must specify retryable vs non-retryable error codes
- Universal tools (StepPlan, MemoryRead, MemoryWrite) are NOT listed in `policies.tool_access`
- Browser, system, and CLI agents MUST add the specialized `sandbox` / `safety` blocks from architecture §§29-30
- Artifact writes are per-task under `runs/artifacts/<task_id>/`; `writable_paths` should cover the root used for those task directories

### Step 4: Write __main__.py

Match this to the local `AgentRuntime.register()` implementation. The invariant is simple: start consumption exactly once.

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
    # does not already start the worker loop.
    # await agent.run()

if __name__ == '__main__':
    asyncio.run(main())
```

### Step 5: Write agent.py (Core Logic)

Your agent MUST extend `AgentRuntime` (the base class that handles registration, heartbeat,
message consumption, HMAC verification, idempotency, universal tool injection, and event emission).

```python
# agents/<agent_name>/agent.py
from shared.contracts import TaskEnvelope, AgentResult, AgentError, ArtifactManifest
from shared.agent_runtime import AgentRuntime

class <AgentClass>(AgentRuntime):
    """<description>"""

    def __init__(self, redis):
        super().__init__(
            agent_card_path='agents/<agent_name>/agent_card.yaml',
            redis=redis,
        )
        # Agent-specific initialization here

    async def on_startup(self):
        """Called during registration, before consuming tasks.
        Initialize databases, load models, warm caches."""
        # Create store/data/ tables if needed
        # Ensure learnings.md exists
        # Warm caches / load heavyweight models
        pass

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        """Core execution logic. Called after HMAC verification,
        epoch check, auth extraction, and idempotency guard.

        Available via self:
          self.auth          — credentials (runtime-only, never serialized)
          self.step_plan     — StepPlan universal tool
          self.memory_read   — MemoryRead universal tool
          self.memory_write  — MemoryWrite universal tool

        MUST return AgentResult (completed or failed). Never raise.
        """
        # Load prompts/system.md, prompts/policies.md, and store/learnings.md
        # at task start so long-lived workers see updated prompt content
        # and accumulated learnings.
        intent = task.intent

        # Dispatch to intent handler
        handler = getattr(self, f'handle_{intent.replace(".", "_")}', None)
        if not handler:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='INVALID_INPUT',
                    retryable=False,
                    message=f'Unknown intent: {intent}',
                    next_action='escalate',
                ),
            )

        return await handler(task)

    async def handle_<domain>_<action>(self, task: TaskEnvelope) -> AgentResult:
        """Handler for <domain>.<action> intent."""
        # 1. Create a step plan for complex tasks
        # await self.step_plan.create(['Step 1', 'Step 2', 'Step 3'])

        # 2. Extract input
        # query = task.input.get('query', '')

        # 3. Use self.auth for provider API calls (if auth_requirements declared)
        # if self.auth:
        #     access_token = self.auth['access_token']
        #     # If the provider rejects with an expired-token auth error,
        #     # suspend and request orchestrator.refresh_credential.

        # 4. Do work, update step plan progress
        # await self.step_plan.update(1, 'completed', 'Found 3 results')

        # 5. Return result
        return AgentResult(
            status='completed',
            output={'response': '...', 'data': {}},
            artifacts=[],
            error=None,
        )
```

### Step 6: Write Intent JSON Schemas

Every intent needs input and output JSON Schema files in `schemas/intents/`.

Provider-backed agents that support named remote resources also need:
- `schemas/intents/<domain>.resolve_resource.input.json`
- `schemas/intents/<domain>.resolve_resource.output.json`

```json
// schemas/intents/<domain>.<action>.input.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "query": { "type": "string", "description": "The user's request" }
  },
  "required": ["query"]
}
```

```json
// schemas/intents/<domain>.<action>.output.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "response": { "type": "string", "description": "Human-readable response" },
    "data": { "type": "object", "description": "Structured result data" }
  },
  "required": ["response"]
}
```

### Step 7: Write System Prompt and Policies

**prompts/system.md** — WHO the agent is:
```markdown
# <Agent Name>

You are the <Agent Name> for COSMIC, a personal assistant system.

## Your Role
<what this agent does>

## Your Capabilities
<list of tools and abilities>

## Important Rules
- You are a specialist. Only handle tasks within your domain.
- Use StepPlan for any task with 3+ steps.
- Use MemoryRead to check for relevant past knowledge before starting work.
- Use MemoryWrite to persist important learnings after completing tasks.
- If you hit ambiguity you cannot resolve, use orchestrator.clarify or orchestrator.decide.
- If a side effect needs explicit permission before continuing, use orchestrator.approve.
- If credentials expire mid-task, use orchestrator.refresh_credential.
- NEVER log, serialize, or persist credential data (self.auth).
```

**prompts/policies.md** — constraints and tool usage:
```markdown
# Policies

## Error Handling
- Return AgentError with retryable=True for: TIMEOUT, NETWORK_ERROR, RATE_LIMITED
- Return AgentError with retryable=False for: INVALID_INPUT, AUTH_ERROR, SCHEMA_VIOLATION
- Always include next_action: 'retry', 'escalate', or 'skip'

## Credential Safety
- Access credentials ONLY via self.auth
- NEVER include credentials in: events, artifacts, logs, store/, learnings.md
- If access token expires mid-task: suspend and request refresh via orchestrator.refresh_credential
- Do NOT assume the base class refreshes provider tokens for you

## Tool Usage
- <tool-specific policies for this agent>
```

### Step 8: Write supervisord Entry

Add to `supervisord.conf`:
```ini
[program:<agent_name>]
command=python -m agents.<agent_name>
autostart=true
autorestart=true
environment=INSTANCE_ID='<agent_name>-1',AGENT_SECRET='<agent-specific-secret>'
stderr_logfile=/var/log/<agent_name>.err.log
stdout_logfile=/var/log/<agent_name>.out.log
```

Special cases:
- Browser agents typically also set `PLAYWRIGHT_BROWSERS_PATH`
- CLI agent does NOT use the generic block above; it runs in sleeping mode with `autostart=false`, `autorestart=false`, and `CLI_AGENT_MODE='sleeping'`

### Step 9: Add Routing Config

Add to `routing.yaml`:
```yaml
<domain>.<action>:
  agent: <agent_id>
  priority: normal  # or high/low
  fallback: null
```

### Step 10: Write Tests

See `reference/testing-standards.md` for the full testing contract. Minimum requirements:

1. **Unit tests** for each intent handler (mock external calls, verify output shape)
2. **Schema validation tests** (validate sample inputs/outputs against JSON Schemas)
3. **Integration tests** (mock Redis + SQLite, test full handle→execute→result flow)
4. **Error path tests** (verify retryable vs non-retryable errors are correctly classified)
5. **Idempotency tests** (verify same idempotency_key returns cached result)
6. **Auth isolation tests** (verify credentials never appear in events, artifacts, or store)

## Critical Rules — Violations Are Bugs

1. **All imports from `shared/`** — contracts, redis_client, sqlite_client, auth, events, idempotency, step_plan, memory_tools. NEVER duplicate these in agent code.

2. **SQLite connections MUST use `shared/sqlite_client.py`** — WAL mode, busy_timeout=5000, foreign_keys=ON. Direct `sqlite3.connect()` is a bug.

3. **Redis client MUST use `shared/redis_client.py`** — `decode_responses=True`. No manual `.decode()` anywhere.

4. **All datetimes via `shared/time_utils.py`** — `utcnow()` returns timezone-aware UTC. Never use `datetime.utcnow()` (deprecated Python 3.12, produces naive datetimes).

5. **Credential isolation** — `self.auth` is runtime-only. Never in events, artifacts, logs, store, learnings.md. The base class extracts `input.auth` before execute() and clears it after.

6. **Event emission contract** — use `self.emit_event()` for non-terminal events (`task.accepted`, `task.progress`, `task.suspended`, `task.resumed`, `task.deferred`, `artifact.added`, `task.rejected`). The runtime emits terminal events after you return `AgentResult`. Seq is per-task monotonic via `redis.incr(f'event_seq:{task_id}')`.

7. **Reverse tasks** — to ask the orchestrator anything, create a TaskEnvelope addressed to `cosmic/orchestrator:1.0.0` with the appropriate intent (`orchestrator.clarify`, `orchestrator.approve`, `orchestrator.decide`, `orchestrator.delegate`, `orchestrator.escalate`, `orchestrator.refresh_credential`). Stamp `source='agent'`, `source_id=self.agent_id`, and inherit `task_list_id`, `session_id`, and `channel` from the parent task. Sign with `sign_reverse_task()`. Emit `task.suspended`. Wait on `streams:<agent_id>:replies`.

8. **Token refresh ownership** — if a provider access token expires mid-task, the agent suspends and sends `orchestrator.refresh_credential`. Do not assume the runtime auto-refreshes tokens.

9. **Specialized agent cards** — browser, system, and CLI agents require the exact specialized `sandbox` / `safety` blocks from architecture §§29-30. The generic policy template is insufficient for those classes.

10. **Artifacts** — write to `runs/artifacts/<task_id>/`. `agent_card.yaml` writable paths must cover the root used for those per-task directories. Include ArtifactManifest in AgentResult. SHA256 integrity hash is mandatory.

11. **Prompts and learnings load at task start** — read `prompts/system.md`, `prompts/policies.md`, and `store/learnings.md` when a task begins so long-lived workers pick up prompt and memory updates.

12. **StepPlan enforcement** — if the agent creates a StepPlan and returns `completed` with pending steps, the runtime REJECTS the result and returns `failed` with code `PLAN_INCOMPLETE`.

13. **Contract version** — never hardcode. Use `CURRENT_WRITE_VERSION` from config. The `contract_version` field on TaskEnvelope and EventEnvelope is dynamic.

14. **Startup lifecycle** — inspect the local `AgentRuntime.register()` implementation before writing `__main__.py`. Start the worker loop exactly once; do not call `run()` twice.

15. **XAUTOCLAIM tuning** — `min_idle_time` must be at least 2x `max_task_duration_sec` from agent_card.yaml. Set via `CLAIM_MIN_IDLE_MS = self.max_task_duration_sec * 2 * 1000`.

16. **Session data** — each agent owns its own `store/data/` schema. No cross-agent reads. The orchestrator queries past work via recall intents (e.g., `<domain>.recall_session`).

17. **recall intent** — every agent SHOULD implement a `<domain>.recall_session` intent that queries `store/data/` and returns structured history. The orchestrator uses this when the user asks about past work.

18. **No `datetime.utcnow()`** — use `utcnow()` from `shared/time_utils.py`.

19. **No `redis.keys()`** — use `redis.scan()` instead. `keys()` blocks the Redis event loop.

20. **No direct `sqlite3.connect()`** — use `connect_sync()` or `connect_async()` from `shared/sqlite_client.py`.

21. **Resource resolution ownership** — if users may name provider-owned resources instead of giving stable IDs, implement `<domain>.resolve_resource`. The orchestrator chooses the account and dispatches one account at a time with a single `input.auth` dict. Agents never guess accounts, never fan out across accounts internally, and never own resource-binding storage.

22. **Suspend/resume contract** — for `orchestrator.refresh_credential`, send `credential_ref`, `provider`, and `parent_task_id`. Persist enough state to `runtime/state.db` before suspension. Suspended work resumes via `intent='agent.resume'`; refresh resumes include fresh `input.auth`, and human-input resumes arrive after the orchestrator round-trips through `user_input:requests` and `user_input:replies`.

23. **Usage logging** — any agent that makes a metered LLM or embedding API call must generate `llm_call_id` in the outbound call path, record `llm_call_placed_at`, and emit one usage event to `POST /internal/usage/log`. Do not write the Gateway Usage Ledger directly. If the agent uses LangChain/LangGraph, it is acceptable to normalize token usage from `AIMessage.usage_metadata` plus `AIMessage.response_metadata['token_usage'|'usage']` before posting the COSMIC usage event.

24. **Shared model registry** — do not hardcode provider SDK choice, `base_url`, context-window limits, output limits, or token pricing inside generated agents. Resolve those from `shared/model_specs.json` / `shared/model_specs.py`. This metadata does not belong in `agent_card.yaml`.

## Reference Files

For detailed specifications, read these files in the `reference/` directory:
- `agent-contract.md` — Full agent runtime contract, event emission, error handling
- `message-contracts.md` — TaskEnvelope, EventEnvelope, AgentResult, AgentError field specs
- `transport-and-signing.md` — Redis Streams, consumer groups, HMAC signing, dispatch
- `agent-card-schema.md` — Complete agent_card.yaml specification with all fields
- `universal-tools.md` — StepPlan, MemoryRead, MemoryWrite specs and usage
- `testing-standards.md` — Test requirements, patterns, fixtures, coverage expectations
