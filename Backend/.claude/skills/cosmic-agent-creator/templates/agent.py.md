# Template: agent.py

Core agent logic. Extends `AgentRuntime` — the base class that handles registration,
heartbeat, message consumption, HMAC verification, idempotency, universal tool injection,
and event emission.

```python
# agents/<agent_name>/agent.py
import json
import hashlib
from pathlib import Path
from uuid import uuid4

from shared.contracts import (
    TaskEnvelope, AgentResult, AgentError, ArtifactManifest,
)
from shared.agent_runtime import AgentRuntime
from shared.sqlite_client import connect_sync
from shared.time_utils import utcnow


class <AgentClass>(AgentRuntime):
    """<DESCRIPTION>"""

    def __init__(self, redis):
        super().__init__(
            agent_card_path='agents/<agent_name>/agent_card.yaml',
            redis=redis,
        )
        # ── Agent-specific initialization ──────────────────────────
        self.prompts_dir = Path('agents/<agent_name>/prompts')
        self.learnings_path = Path('agents/<agent_name>/store/learnings.md')
        self.system_prompt = None
        self.policies = None
        self.learnings = None
        self.db = None

    async def on_startup(self):
        """Called during registration, BEFORE consuming tasks.
        Initialize databases, ensure files exist, warm caches."""

        # Ensure learnings file exists. Prompts + learnings are reloaded at
        # task start so long-lived workers pick up updates.
        self.learnings_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text('# <DISPLAY_NAME> — Learnings\n')

        # Initialize agent-managed database
        data_dir = Path('agents/<agent_name>/store/data')
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect_sync(str(data_dir / 'sessions.db'))
        self._init_schema()

        # Ensure runtime directories exist
        runtime_dir = Path('agents/<agent_name>/runtime')
        (runtime_dir / 'cache').mkdir(parents=True, exist_ok=True)
        (runtime_dir / 'logs').mkdir(parents=True, exist_ok=True)

    def _load_task_context(self):
        """Reload prompt and learnings files at task start."""
        self.system_prompt = (self.prompts_dir / 'system.md').read_text()
        self.policies = (self.prompts_dir / 'policies.md').read_text()
        self.learnings = self.learnings_path.read_text()

    def _init_schema(self):
        """Create agent-specific tables if they don't exist."""
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS <agent_name>_sessions (
                session_id TEXT,
                task_id TEXT,
                intent TEXT,
                query TEXT,
                result_summary TEXT,
                created_at TIMESTAMP,
                PRIMARY KEY (session_id, task_id)
            )
        ''')
        self.db.commit()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        """Core execution logic. Dispatches to intent handlers.

        Available via self at this point:
          self.auth          — credentials (runtime-only, NEVER serialize)
          self.step_plan     — StepPlan universal tool
          self.memory_read   — MemoryRead universal tool
          self.memory_write  — MemoryWrite universal tool
        """
        self._load_task_context()
        intent = task.intent

        # Dispatch to handler
        handler_name = f'handle_{intent.replace(".", "_")}'
        handler = getattr(self, handler_name, None)
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

        try:
            return await handler(task)
        except TimeoutError:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='TIMEOUT',
                    retryable=True,
                    message=f'Timeout executing {intent}',
                    next_action='retry',
                ),
            )
        except ConnectionError as e:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='NETWORK_ERROR',
                    retryable=True,
                    message=str(e),
                    next_action='retry',
                ),
            )
        except Exception as e:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='INTERNAL_ERROR',
                    retryable=False,
                    message=str(e),
                    next_action='escalate',
                ),
            )

    # ── Intent Handlers ────────────────────────────────────────────

    async def handle_<domain>_<action>(self, task: TaskEnvelope) -> AgentResult:
        """Handler for <domain>.<action> intent."""
        query = task.input.get('query', '')

        if not query:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='INVALID_INPUT',
                    retryable=False,
                    message='Missing required field: query',
                    next_action='escalate',
                ),
            )

        # ── Step plan for complex tasks ────────────────────────
        # await self.step_plan.create([
        #     'Step 1 description',
        #     'Step 2 description',
        #     'Step 3 description',
        # ])

        # ── Check memory for relevant context ──────────────────
        # memories = await self.memory_read.search(query, max_results=5)

        # ── Use credentials if needed (auth_requirements) ──────
        # if self.auth:
        #     access_token = self.auth['access_token']
        #     # Make provider API calls with access_token.
        #     # If the provider returns an expired-token auth error,
        #     # suspend and request orchestrator.refresh_credential.

        # ── Do the actual work ─────────────────────────────────
        # await self.step_plan.update(1, 'in_progress')
        # ... your logic ...
        # await self.step_plan.update(1, 'completed', 'Done with step 1')
        #
        # If this handler makes a metered LLM or embedding API call, emit one
        # usage event to POST /internal/usage/log using the COSMIC usage
        # contract. Generate llm_call_id in the outbound call path and record
        # llm_call_placed_at when the call is initiated.
        # If you need SDK/base_url/context/pricing metadata for that call or
        # for estimated_cost_usd/headroom calculations, resolve it from
        # shared/model_specs.json via a shared helper — do not hardcode it in
        # this agent and do not put it in agent_card.yaml.
        #
        # Optional LangChain/LangGraph pattern:
        #   - inspect AIMessage.usage_metadata first
        #   - then fall back to AIMessage.response_metadata['token_usage']
        #     or ['usage']
        #   - normalize prompt/input, completion/output, total, cached-input,
        #     and reasoning tokens before posting the usage event

        result_data = {
            'response': f'Processed: {query}',
            'data': {},
        }

        # ── Persist learnings if new knowledge discovered ──────
        # await self.memory_write.write(
        #     content='Discovered that...',
        #     memory_type='agent_note',
        #     tags=['<domain>', 'learning'],
        # )

        # ── Save to agent session data ─────────────────────────
        self.db.execute('''
            INSERT OR REPLACE INTO <agent_name>_sessions
            (session_id, task_id, intent, query, result_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', [
            task.session_id, task.task_id, task.intent,
            query, result_data['response'], utcnow().isoformat(),
        ])
        self.db.commit()

        return AgentResult(
            status='completed',
            output=result_data,
            artifacts=[],
            error=None,
        )

    async def handle_<domain>_resolve_resource(self, task: TaskEnvelope) -> AgentResult:
        """Optional: search provider-owned resources by name within ONE account.
        Only add this intent when users may name remote resources instead of
        passing stable IDs directly.

        The orchestrator chooses the account and passes a single credential in
        self.auth. Never fan out across accounts here and never persist resource
        bindings locally.
        """
        query = task.input.get('query', '')
        resource_type = task.input.get('resource_type')
        account_id = task.input.get('account_id')

        if not self.auth:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='AUTH_ERROR',
                    retryable=False,
                    message='resolve_resource requires input.auth for the selected account',
                    next_action='escalate',
                ),
            )

        # Use self.auth to query the provider API for this ONE account.
        matches = []

        return AgentResult(
            status='completed',
            output={
                'query': query,
                'resource_type': resource_type,
                'account_id': account_id,
                'matches': matches,
            },
            artifacts=[],
            error=None,
        )

    async def handle_<domain>_recall_session(self, task: TaskEnvelope) -> AgentResult:
        """Recall what happened in a previous session for this agent.
        Called by orchestrator when user asks about past work."""
        session_id = task.input.get('session_id')
        query = task.input.get('query', '')
        limit = task.input.get('limit', 10)

        rows = self.db.execute('''
            SELECT * FROM <agent_name>_sessions
            WHERE session_id = ?
            ORDER BY created_at DESC LIMIT ?
        ''', [session_id, limit]).fetchall()

        return AgentResult(
            status='completed',
            output={
                'history': [dict(row) for row in rows],
                'count': len(rows),
            },
            artifacts=[],
            error=None,
        )

    # ── Session Data Management ────────────────────────────────────

    def save_session_data(self, session_id: str, task: TaskEnvelope, result: AgentResult):
        """Called by base class after successful execution.
        Override if you need custom session data persistence."""
        pass  # Already handled in intent handlers

    def maybe_update_learnings(self, task: TaskEnvelope, result: AgentResult):
        """Called by base class after successful execution.
        Override to persist new knowledge to store/learnings.md."""
        # Example:
        # if self._has_new_insight(result):
        #     with self.learnings_path.open('a') as f:
        #         f.write(f'\n## {utcnow().date()}\n')
        #         f.write(f'- {self._extract_insight(result)}\n')
        pass
```

## Placeholder Reference

| Placeholder | Example | Rule |
|---|---|---|
| `<agent_name>` | `research_agent` | Snake case, matches directory |
| `<AgentClass>` | `ResearchAgent` | PascalCase |
| `<DESCRIPTION>` | `Specialist for web research and citations` | One line |
| `<DISPLAY_NAME>` | `Research Agent` | Title case |
| `<domain>` | `research` | Lowercase, same as agent_card intents |
| `<action>` | `topic`, `find_image` | Lowercase with underscores |
