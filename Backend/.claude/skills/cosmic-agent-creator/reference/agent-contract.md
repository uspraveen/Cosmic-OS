# Agent Runtime Contract

Every agent extends `AgentRuntime` — the base class that handles the full lifecycle.
This document specifies what the base class does and what your agent must implement.

## Startup Sequence (Handled by Base Class)

```
SQLite write (capability declaration)
  → Redis 'starting' (liveness entry)
    → on_startup() (YOUR initialization code)
      → Redis 'healthy' (ready for dispatch)
        → heartbeat_loop() started
          → begin consuming from Redis Streams
```

An agent stuck in `starting` beyond `startup_timeout_sec` is marked dead.
Orchestrator routing requires `status == 'healthy'`.

### Registration (Base Class)

```python
async def register(self):
    card = yaml.safe_load(open('agent_card.yaml'))

    # Step 1: Write SQLite (capability declaration)
    db.execute('''
        INSERT OR REPLACE INTO agents
        (agent_id, display_name, max_concurrency, heartbeat_ttl,
         max_task_duration_sec, card_json, registered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', [card['agent_id'], card['display_name'],
          card['sla']['max_concurrency'],
          card['sla']['heartbeat_ttl_sec'],
          card['sla']['max_task_duration_sec'],
          json.dumps(card), utcnow()])

    for intent in card['intents']:
        db.execute('''
            INSERT OR REPLACE INTO agent_intents
            (agent_id, intent, timeout_sec)
            VALUES (?, ?, ?)
        ''', [card['agent_id'], intent['name'], intent['timeout_sec']])

    # Step 2: Register intent index in Redis
    await register_intent_index(card['agent_id'], card, redis)

    # Step 3: Write initial Redis liveness entry BEFORE consuming tasks
    instance_key = f'registry:{self.agent_id}:{self.instance_id}'
    await redis.hset(instance_key, mapping={
        'status': 'starting',
        'current_load': '0',
        'max_conc': str(self.max_concurrency),
        'heartbeat_ttl': str(self.heartbeat_ttl_sec),
        'last_seen': utcnow().isoformat(),
    })

    # Step 4: Run agent-specific startup checks
    await self.on_startup()

    # Step 5: Mark healthy
    await redis.hset(instance_key, 'status', 'healthy')

    # Step 6: Start heartbeat loop
    asyncio.create_task(self.heartbeat_loop())

    # Step 7: Start consuming
    await self.run()
```

**Important implementation note:** the architecture doc also shows a `__main__.py` example that calls `await agent.run()` after `await agent.register()`. Treat the invariant as "start consuming exactly once" and inspect the local `AgentRuntime.register()` implementation before generating the entrypoint. Do not start the worker loop twice.

### Heartbeat Loop (Base Class)

```python
async def heartbeat_loop(self):
    instance_key = f'registry:{self.agent_id}:{self.instance_id}'
    while True:
        await redis.hset(instance_key, mapping={
            'status': 'healthy',
            'current_load': str(self.active_task_count),
            'max_conc': str(self.max_concurrency),
            'heartbeat_ttl': str(self.heartbeat_ttl_sec),
            'last_seen': utcnow().isoformat(),
        })
        await redis.expire(instance_key, self.heartbeat_ttl_sec + 5)
        await asyncio.sleep(self.heartbeat_interval_sec)
```

## Task Handling (Base Class + Your Code)

```python
async def handle(self, task: TaskEnvelope, msg_id: str, stream: str):
    # ── Base class handles these automatically: ─────────────
    # 1. HMAC signature verification
    if not verify_incoming(task):
        raise AuthError(f'Invalid signature on task {task.task_id}')

    # 2. Epoch check (fencing token)
    if task.leader_epoch is not None:
        current_epoch = int(await redis.get(EPOCH_KEY) or 0)
        if task.leader_epoch < current_epoch:
            await self._reject_stale_epoch(task, msg_id, stream, current_epoch)
            return

    # 3. Auth extraction (stripped BEFORE execute)
    self.auth = task.input.pop('auth', None)

    # 4. Artifact verification
    for artifact in task.input_artifacts:
        verify_artifact(artifact)

    # 5. Universal tool injection
    self.step_plan = StepPlan(agent_id=self.agent_id, task_id=task.task_id, emit_fn=self.emit_event)
    self.memory_read = MemoryRead(gateway_url=GATEWAY_INTERNAL_URL, agent_id=self.agent_id, ...)
    self.memory_write = MemoryWrite(gateway_url=GATEWAY_INTERNAL_URL, agent_id=self.agent_id, ...)

    # 6. Idempotency-guarded execution
    result = await execute_with_idempotency(
        task, self.execute, redis,
        agent_max_duration_sec=self.max_task_duration_sec,
    )

    # 7. Auth cleanup
    self.auth = None

    # 8. StepPlan enforcement
    if isinstance(result, AgentResult) and result.status == 'completed':
        if self.step_plan.has_pending_steps():
            result = AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='PLAN_INCOMPLETE',
                    retryable=False,
                    message='Agent returned completed but StepPlan has pending steps',
                    next_action='escalate',
                ),
            )

    # ── YOUR code runs inside execute() ─────────────────────
    # 9. Result handling
    if isinstance(result, AgentResult):
        if task.session_id and result.status == 'completed':
            self.save_session_data(task.session_id, task, result)
            self.maybe_update_learnings(task, result)
        await redis.xack(stream, 'workers', msg_id)
        await self.emit_terminal_event(task.task_id, result)

    elif isinstance(result, TaskInProgress):
        # Emit deferred BEFORE XACK (crash safety ordering)
        await self.emit_event(
            task_id=task.task_id,
            event_type='task.deferred',
            payload={...},
        )
        await redis.xack(stream, 'workers', msg_id)
```

## What Your Agent Must Implement

### Required Methods

| Method | Purpose |
|---|---|
| `__init__(self, redis)` | Call `super().__init__()` with agent_card_path. Agent-specific init. |
| `on_startup(self)` | DB migrations, model loading, cache warming. Called during registration. |
| `execute(self, task: TaskEnvelope) -> AgentResult` | Core logic. Dispatch to intent handlers. |

### Required Intent Handlers

For each intent declared in `agent_card.yaml`, implement a handler method:

```python
async def handle_<domain>_<action>(self, task: TaskEnvelope) -> AgentResult:
    ...
```

The intent `research.topic` maps to `handle_research_topic`.
The intent `docs.recall_session` maps to `handle_docs_recall_session`.

### Recall Intent (Strongly Recommended)

Every agent SHOULD implement `<domain>.recall_session`:

```python
async def handle_<domain>_recall_session(self, task: TaskEnvelope) -> AgentResult:
    """Return structured history from store/data/ for the given session."""
    session_id = task.input.get('session_id')
    query = task.input.get('query', '')
    limit = task.input.get('limit', 10)

    rows = db.execute('''
        SELECT * FROM <agent_sessions_table>
        WHERE session_id = ? ORDER BY created_at DESC LIMIT ?
    ''', [session_id, limit]).fetchall()

    return AgentResult(
        status='completed',
        output={'history': [dict(row) for row in rows]},
        artifacts=[],
        error=None,
    )
```

## Event Emission

### Progress Events

```python
# Use self.emit_event() for non-terminal events
await self.emit_event(
    task_id=task.task_id,
    event_type='task.progress',
    payload={
        'type': 'search_results',
        'count': 5,
        'message': 'Found 5 relevant papers',
    },
)
```

### Artifact Events

```python
await self.emit_event(
    task_id=task.task_id,
    event_type='artifact.added',
    payload={
        'artifact': ArtifactManifest(
            artifact_id=f'art_{uuid4().hex[:8]}',
            task_id=task.task_id,
            mime='application/pdf',
            sha256=hashlib.sha256(content).hexdigest(),
            path=f'runs/artifacts/{task.task_id}/paper.pdf',
            source_url='https://arxiv.org/...',
            created_by_agent=self.agent_id,
            created_at=utcnow(),
        ).model_dump(),
    },
)
```

### Terminal Events (Handled by Base Class)

The base class calls `self.emit_terminal_event(task.task_id, result)` automatically.
You just return an `AgentResult` from `execute()`.

## Usage Logging

If the agent makes a metered LLM or embedding API call, usage logging is mandatory:

- Generate `llm_call_id` in the code path that initiates the outbound metered call.
- Record `llm_call_placed_at` when the outbound call is initiated.
- After the provider returns or fails, emit exactly one usage event to `POST /internal/usage/log`.
- Reuse the same `llm_call_id` if the usage log write is retried, so Gateway deduplicates idempotently.
- Never write `gateway/usage.db` directly from the agent.
- If the agent computes `estimated_cost_usd`, context headroom, or other model-limit telemetry, resolve
  SDK/base-URL/limits/pricing metadata from `shared/model_specs.json` rather than agent-local
  constants.

If the agent uses LangChain or LangGraph, a common extraction pattern is:

- read `AIMessage.usage_metadata` first
- then fall back to `AIMessage.response_metadata['token_usage']` or `AIMessage.response_metadata['usage']`
- normalize `input_tokens`, `output_tokens`, `total_tokens`, and, when present, cached-input and reasoning token details

Treat this LangChain/LangGraph note as a suggestion, not a mandatory implementation detail. The
hard requirement is that the final usage event matches the COSMIC usage contract.

## Shared Model Registry

Generated agents must treat `shared/model_specs.json` as the global source of truth for metered
model metadata:

- provider/model -> SDK family
- provider/model -> default `base_url`
- context-window and max-output limits
- recommended reserve/headroom
- pricing used for cost estimation

Do not duplicate this metadata inside:

- `agent_card.yaml`
- agent-local constants
- per-agent pricing tables

If the agent needs model metadata at runtime, load it through `shared/model_specs.py` or an
equivalent shared helper.

## Reverse Tasks (Agent -> Orchestrator)

When the agent needs help, it creates a reverse task:

```python
# 1. Create reverse task
reverse = TaskEnvelope(
    task_id=generate_task_id(),
    task_list_id=current_task.task_list_id,
    parent_task_id=current_task.task_id,
    session_id=current_task.session_id,
    sender=self.agent_id,
    recipient='cosmic/orchestrator:1.0.0',
    intent='orchestrator.clarify',  # or .approve, .decide, .delegate, .escalate, .refresh_credential
    input={
        'question': 'Found conflicting sources. Which to prioritize?',
        'options': ['source_a', 'source_b'],
    },
    idempotency_key=str(uuid4()),
    priority='normal',
    source='agent',
    source_id=self.agent_id,
    channel=current_task.channel,
    ...
)

# 2. Sign with agent's own secret
reverse = sign_reverse_task(reverse)

# 3. Emit suspension event
await self.emit_event(task_id=current_task.task_id, event_type='task.suspended', payload={...})

# 4. Serialize state to runtime/state.db
await self.save_suspension_state(current_task.task_id, state_data)

# 5. Dispatch reverse task
await dispatch(reverse, self.redis)

# 6. Wait for reply on streams:<agent_id>:replies
# (base class handles the reply consumption loop)
```

### Reverse Task Intents

| Intent | When to Use |
|---|---|
| `orchestrator.clarify` | Agent needs a decision between options |
| `orchestrator.approve` | Agent needs permission before a side effect |
| `orchestrator.decide` | Agent hit a branch it cannot resolve |
| `orchestrator.delegate` | Agent needs another agent's output |
| `orchestrator.escalate` | Push to gateway for human input |
| `orchestrator.refresh_credential` | Access token expired mid-task |

### Credential Refresh Suspension Contract

When a provider rejects with an expired-token auth error, suspend and request a refresh:

```python
await self._send_reverse_task(
    intent='orchestrator.refresh_credential',
    input={
        'credential_ref': self.auth['credential_ref'],
        'provider': self.auth['provider'],
        'parent_task_id': current_task.task_id,
    },
)
```

Rules:
- Use the existing credential reference. The orchestrator refreshes via the Gateway's dedicated `/internal/credentials/refresh` endpoint.
- Persist enough execution state to `runtime/state.db` before suspension so the task can continue after resume.
- Expect the continuation as a resume envelope: `intent='agent.resume'`, with `input={'task_id': <suspended_task_id>, 'auth': {...fresh token...}}`.
- Treat the resumed work as the same suspended task, not as a fresh multi-account dispatch.

### Human-Input Resume Contract

If the agent asks the orchestrator for clarification/approval/decision and the orchestrator cannot answer itself:
- The orchestrator publishes a task input request to `user_input:requests`
- The Gateway/UI collects the reply and publishes it to `user_input:replies`
- The orchestrator resumes the suspended agent with `intent='agent.resume'`

Agent responsibilities:
- Emit `task.suspended`
- Serialize resumable state to `runtime/state.db`
- Wait for resume via the runtime reply loop

Agents do NOT publish directly to `user_input:requests` or consume `user_input:replies`.

## Learnings Management

```python
# Read at task start
learnings = Path('agents/<agent_name>/store/learnings.md').read_text()

# Append after task completion (when new knowledge discovered)
def maybe_update_learnings(self, task, result):
    if result.status == 'completed' and self._has_new_insight(result):
        with open('agents/<agent_name>/store/learnings.md', 'a') as f:
            f.write(f'\n## {utcnow().date()}\n')
            f.write(f'- {self._extract_insight(result)}\n')
```

The Session Manager syncs `store/learnings.md` to `memory/agent_notes/<agent_name>/learnings.md`
and indexes it in Qdrant as a high-priority memory. All LLM backends benefit from agent learnings
during context assembly.

## Orchestrator-Side Guarantees (What Agents Can Rely On)

These are NOT implemented by agents but are important to understand:

### Credential Resolution Guarantee

If your intent declares `auth_requirements` in `agent_card.yaml`, the orchestrator resolves
credentials BEFORE dispatching to your agent. If resolution fails (no connected account, revoked
access, insufficient scopes), the orchestrator **escalates to the user** instead of dispatching
without auth. Your agent will NEVER receive a task for an auth-required intent without `input.auth`
populated. You can trust that `self.auth` is valid when it's present.

### Retry and Superseded Tasks

When your agent returns a retryable error and the orchestrator decides to retry:
- The original task is marked `superseded` (a terminal state — not a zombie)
- A fresh task row with a new `task_id` is created for the retry
- The plan step's `attempt` counter (tracked in `plan_steps.attempt` and `plan_steps.max_attempts`)
  is incremented
- Your agent receives the retry as a brand-new task — treat it normally

The `superseded` status prevents zombie task rows in the ledger. Agents don't set this status —
the orchestrator handles it automatically.

### Plan Step Retry Tracking

The orchestrator's `plan_steps` table tracks retries at the step level:
- `attempt INTEGER DEFAULT 0` — current retry count for this step
- `max_attempts INTEGER DEFAULT 3` — sourced from your `agent_card.yaml` SLA `retry_policy.max_attempts`

This means your `retry_policy.max_attempts` value directly controls how many times a plan step
can be retried before the orchestrator gives up or asks the LLM whether to skip/fail the plan.

### Unified Sessions

Sessions are channel-agnostic — all channels (Desktop, WhatsApp, Telegram, etc.) share one session
per day (format: `sess_20250115`). The `channel` field on TaskEnvelope tells you which platform the
task originated from, but the `session_id` is the same regardless of channel. When querying
`store/data/` by session_id, you get results from all channels.
