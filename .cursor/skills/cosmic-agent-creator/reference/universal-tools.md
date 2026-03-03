# Universal Agent Tools

Every agent receives these tools at runtime. They are injected by the `AgentRuntime` base class
during task handling — NOT declared in `agent_card.yaml`. Agents cannot opt out.

## Tool Tiers

| Tier | Tools | Injection | Opt-out |
|---|---|---|---|
| **Universal** | StepPlan, MemoryRead, MemoryWrite | Injected by agent runtime at task start | **Not allowed** |
| **Declared** | All other tools | Declared in `agent_card.yaml` `policies.tool_access` | Per agent |

## StepPlan: Agent-Level Execution Planning

Lightweight, single-task planning tool that prevents LLM drift during complex agent execution.

**This is NOT the orchestrator's Task Planner (which manages a DAG across agents).**
StepPlan manages a flat checklist within one agent's single task.

| Aspect | Orchestrator Task Planner | Agent StepPlan |
|---|---|---|
| Scope | Multiple tasks across agents | One task, internal steps |
| Dependencies | DAG | Flat ordered list |
| Lifetime | Lives until user request done | Ephemeral per-task |
| Persistence | SQLite ledger | In-memory, emitted via events |

### Operations

#### create(steps: list[str]) -> dict

Create a new plan before starting complex work. Auto-emits `task.progress` event.

```python
await self.step_plan.create([
    'Search for recent papers on quantum error correction',
    'Analyze key findings and extract citations',
    'Synthesize summary with APA formatting',
])
# Returns: { plan_active: True, total_steps: 3, steps: [...] }
```

#### update(step: int, status: str, note: str | None) -> dict

Mark a step's status. Auto-emits `task.progress` event.
Status values: `'in_progress'`, `'completed'`, `'skipped'`.

```python
await self.step_plan.update(1, 'completed', 'Found 5 relevant papers')
# Returns: { step: 1, status: 'completed', completed: 1, total: 3, percent: 33 }
```

#### list() -> dict

Return current plan state. Used by the agent to re-ground itself.

```python
state = await self.step_plan.list()
# Returns: { plan_active: True, steps: [...], completed: 1, total: 3 }
```

### Plan Enforcement

If the agent creates a StepPlan and returns `AgentResult(status='completed')` while steps
are still pending or in_progress, the runtime **REJECTS** the result:

```python
# Base class enforcement (automatic):
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
```

### When to Use StepPlan

- Tasks with 3+ distinct phases of work
- Tasks where intermediate progress is valuable to the user
- Complex research, multi-document editing, multi-step automation
- NOT needed for simple, single-action tasks

### Full Implementation

```python
class StepPlan:
    def __init__(self, agent_id: str, task_id: str, emit_fn: Callable):
        self.agent_id = agent_id
        self.task_id = task_id
        self.emit_fn = emit_fn
        self.steps: list[dict] = []
        self.active = False

    async def create(self, steps: list[str]) -> dict:
        self.steps = [
            {'step': i + 1, 'text': text, 'status': 'pending', 'note': None}
            for i, text in enumerate(steps)
        ]
        self.active = True
        await self.emit_fn(
            task_id=self.task_id,
            event_type='task.progress',
            payload={
                'type': 'agent_plan_created',
                'total_steps': len(self.steps),
                'steps': [{'step': s['step'], 'text': s['text']} for s in self.steps],
            },
        )
        return {'plan_active': True, 'total_steps': len(self.steps), 'steps': self.steps}

    async def update(self, step: int, status: str, note: str | None = None) -> dict:
        if not self.active:
            return {'error': 'No active plan. Call create() first.'}
        if step < 1 or step > len(self.steps):
            return {'error': f'Invalid step {step}. Valid: 1-{len(self.steps)}'}
        self.steps[step - 1]['status'] = status
        if note:
            self.steps[step - 1]['note'] = note
        completed = sum(1 for s in self.steps if s['status'] in ('completed', 'skipped'))
        total = len(self.steps)
        percent = round(completed / total * 100)
        await self.emit_fn(
            task_id=self.task_id,
            event_type='task.progress',
            payload={
                'type': 'agent_step_update',
                'step': step, 'text': self.steps[step - 1]['text'],
                'status': status, 'note': note,
                'completed': completed, 'total': total, 'percent': percent,
            },
        )
        return {'step': step, 'status': status, 'completed': completed, 'total': total, 'percent': percent}

    async def list(self) -> dict:
        if not self.active:
            return {'plan_active': False, 'steps': [], 'completed': 0, 'total': 0}
        completed = sum(1 for s in self.steps if s['status'] in ('completed', 'skipped'))
        return {'plan_active': True, 'steps': self.steps, 'completed': completed, 'total': len(self.steps)}

    def has_pending_steps(self) -> bool:
        if not self.active:
            return False
        return any(s['status'] in ('pending', 'in_progress') for s in self.steps)
```

## MemoryRead: Shared Memory Access

Queries the Gateway's memory retrieval system (Qdrant hybrid search) via internal HTTP.

```python
class MemoryRead:
    def __init__(self, gateway_url: str, agent_id: str, service_token: str):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.service_token = service_token

    async def search(self, query: str, max_results: int = 5,
                     memory_types: list[str] | None = None) -> list[dict]:
        """Search shared memory store.

        Args:
            query: natural language search query
            max_results: max results
            memory_types: filter by type (e.g., ['agent_note', 'session_summary',
                          'task_summary', 'user_data'])

        Returns:
            list of { memory_id, type, content, date, relevance_score }
        """
        response = await http_client.post(
            f'{self.gateway_url}/internal/memory/search',
            json={
                'query': query,
                'memory_types': memory_types,
                'max_results': max_results,
                'agent_id': self.agent_id,
            },
            headers={'X-Internal-Token': self.service_token},
        )
        return response.json()['results']
```

### When to Use MemoryRead

- Before starting research: check if relevant knowledge already exists
- When the user references something from the past
- When context from other agents' learnings would be useful
- To avoid redundant work

## MemoryWrite: Persist to Shared Memory

Writes a new memory entry to the shared store (`.md` file + Qdrant vector index) via Gateway.

```python
class MemoryWrite:
    def __init__(self, gateway_url: str, agent_id: str, service_token: str):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.service_token = service_token

    async def write(self, content: str, tags: list[str] | None = None,
                    memory_type: str = 'agent_note') -> dict:
        """Write a memory to the shared store.

        Args:
            content: the memory content (markdown text)
            tags: optional categorization tags
            memory_type: 'agent_note' (default) or 'task_summary'

        Returns:
            { memory_id: str, indexed: bool }
        """
        response = await http_client.post(
            f'{self.gateway_url}/internal/memory/write',
            json={
                'agent_id': self.agent_id,
                'content': content,
                'memory_type': memory_type,
                'tags': tags or [],
            },
            headers={'X-Internal-Token': self.service_token},
        )
        return response.json()
```

### When to Use MemoryWrite

- After discovering important facts the user would want remembered
- After completing a significant task (persist the summary)
- When learning user preferences or patterns
- Rate limited: max 50 writes per agent per hour

### Deduplication

MemoryWrite uses content-hash-based deduplication (SHA256 prefix + Redis SETNX).
Writing the same content twice returns the original memory_id with `deduplicated: True`.
No duplicate entries are created.

## Tool Injection (How It Works)

```python
# AgentRuntime.handle() injects these before calling execute():
self.step_plan = StepPlan(
    agent_id=self.agent_id,
    task_id=task.task_id,
    emit_fn=self.emit_event,
)
self.memory_read = MemoryRead(
    gateway_url=GATEWAY_INTERNAL_URL,
    agent_id=self.agent_id,
    service_token=self.service_token,
)
self.memory_write = MemoryWrite(
    gateway_url=GATEWAY_INTERNAL_URL,
    agent_id=self.agent_id,
    service_token=self.service_token,
)
```

Inside your `execute()` method, access them via `self.step_plan`, `self.memory_read`, `self.memory_write`.

## Tool Summary

| Tool | Operations | Purpose | Auto-emits |
|---|---|---|---|
| **StepPlan** | `create`, `update`, `list` | Prevent drift. Externalize thinking. | Yes — every create/update emits task.progress |
| **MemoryRead** | `search` | Access shared memory (all types). | No |
| **MemoryWrite** | `write` | Persist learnings to shared store. | No |
