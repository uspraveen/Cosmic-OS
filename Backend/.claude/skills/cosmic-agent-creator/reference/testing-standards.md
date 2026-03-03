# Testing Standards for COSMIC Agents

Every agent MUST have comprehensive tests. Agents are the execution layer — bugs here
corrupt user data, leak credentials, or silently drop tasks.

## Test Directory Structure

```
agents/<agent_name>/tests/
├── conftest.py             # Shared fixtures: mock redis, mock tasks, mock DB
├── test_agent.py           # Unit tests for execute() and intent handlers
├── test_intents.py         # Intent-specific tests with mocked dependencies
├── test_schemas.py         # JSON Schema validation tests
├── test_integration.py     # Full handle->execute->result flow with mock Redis+SQLite
├── test_errors.py          # Error path and retry classification tests
├── test_idempotency.py     # Idempotency enforcement tests
└── test_auth_isolation.py  # Credential isolation verification
```

## Required Test Categories

### 1. Unit Tests (test_agent.py)

Test each intent handler in isolation. Mock all external calls.

```python
import pytest
from unittest.mock import AsyncMock, patch
from shared.contracts import TaskEnvelope, AgentResult, AgentError
from agents.<agent_name>.agent import <AgentClass>

@pytest.fixture
def agent(mock_redis):
    agent = <AgentClass>(redis=mock_redis)
    agent.emit_event = AsyncMock()
    agent.step_plan = AsyncMock()
    agent.memory_read = AsyncMock()
    agent.memory_write = AsyncMock()
    return agent

@pytest.fixture
def sample_task():
    return TaskEnvelope(
        task_id='tsk_test_001',
        task_list_id='test:list',
        parent_task_id=None,
        session_id='sess_20250115',
        sender='cosmic/orchestrator:1.0.0',
        recipient='cosmic/<agent_name>-agent:1.0.0',
        intent='<domain>.<action>',
        input={'query': 'test query'},
        idempotency_key='test-uuid-001',
        priority='normal',
        signature='test-sig',
    )

class TestIntentHandlers:
    async def test_<domain>_<action>_success(self, agent, sample_task):
        """Test successful execution of <domain>.<action> intent."""
        result = await agent.handle_<domain>_<action>(sample_task)
        assert result.status == 'completed'
        assert 'response' in result.output
        assert result.error is None

    async def test_<domain>_<action>_empty_query(self, agent, sample_task):
        """Test that empty query returns appropriate error."""
        sample_task.input = {'query': ''}
        result = await agent.handle_<domain>_<action>(sample_task)
        assert result.status == 'failed'
        assert result.error.code == 'INVALID_INPUT'
        assert result.error.retryable is False

    async def test_unknown_intent(self, agent, sample_task):
        """Test that unknown intent returns INVALID_INPUT error."""
        sample_task.intent = 'unknown.intent'
        result = await agent.execute(sample_task)
        assert result.status == 'failed'
        assert result.error.code == 'INVALID_INPUT'
```

### 2. Schema Validation Tests (test_schemas.py)

Validate that sample inputs/outputs conform to the declared JSON Schemas.

```python
import json
import jsonschema
from pathlib import Path

SCHEMAS_DIR = Path('agents/<agent_name>/schemas/intents')

class TestSchemas:
    def test_input_schema_valid(self):
        """Verify input schema is valid JSON Schema."""
        schema = json.loads((SCHEMAS_DIR / '<domain>.<action>.input.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_output_schema_valid(self):
        """Verify output schema is valid JSON Schema."""
        schema = json.loads((SCHEMAS_DIR / '<domain>.<action>.output.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_sample_input_validates(self):
        """Verify sample input passes schema validation."""
        schema = json.loads((SCHEMAS_DIR / '<domain>.<action>.input.json').read_text())
        sample = {'query': 'test query'}
        jsonschema.validate(sample, schema)

    def test_sample_output_validates(self):
        """Verify sample output passes schema validation."""
        schema = json.loads((SCHEMAS_DIR / '<domain>.<action>.output.json').read_text())
        sample = {'response': 'test response', 'data': {}}
        jsonschema.validate(sample, schema)

    def test_all_intents_have_schemas(self):
        """Every intent in agent_card.yaml has input + output schemas."""
        import yaml
        card = yaml.safe_load(open('agents/<agent_name>/agent_card.yaml'))
        for intent in card['intents']:
            input_path = Path(intent['input_schema'])
            output_path = Path(intent['output_schema'])
            assert input_path.exists(), f'Missing input schema: {input_path}'
            assert output_path.exists(), f'Missing output schema: {output_path}'
```

### 3. Integration Tests (test_integration.py)

Test the full handle -> execute -> result flow with mock Redis and SQLite.

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_redis():
    """Mock Redis client with decode_responses=True behavior."""
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)  # event seq
    redis.xadd = AsyncMock(return_value='msg-id-001')
    redis.rpush = AsyncMock()
    redis.xack = AsyncMock()
    redis.set = AsyncMock(return_value=True)  # idempotency lock acquired
    redis.get = AsyncMock(return_value=None)  # no cached result
    redis.exists = AsyncMock(return_value=False)
    redis.delete = AsyncMock()
    redis.expire = AsyncMock()
    redis.hset = AsyncMock()
    return redis

class TestIntegration:
    async def test_full_task_lifecycle(self, mock_redis, sample_task):
        """Test complete: receive task -> verify -> execute -> emit result."""
        agent = <AgentClass>(redis=mock_redis)
        # Mock signature verification
        with patch('shared.auth.verify_incoming_agent', return_value=True):
            await agent.handle(sample_task, 'msg-001', 'streams:test:normal')

        # Verify XACK was called (message acknowledged)
        mock_redis.xack.assert_called_once()

        # Verify terminal event was emitted
        assert any(
            call.kwargs.get('event_type') == 'task.completed'
            or (len(call.args) > 1 and call.args[1] == 'task.completed')
            for call in agent.emit_event.call_args_list
        ) or agent.emit_terminal_event.called

    async def test_task_failure_emits_failed_event(self, mock_redis, sample_task):
        """Test that a failing task emits task.failed."""
        sample_task.input = {}  # trigger failure
        agent = <AgentClass>(redis=mock_redis)
        with patch('shared.auth.verify_incoming_agent', return_value=True):
            await agent.handle(sample_task, 'msg-002', 'streams:test:normal')
        # Verify failure event was emitted
```

### 4. Error Path Tests (test_errors.py)

Verify error classification is correct — retryable vs non-retryable.

```python
class TestErrorClassification:
    async def test_network_error_is_retryable(self, agent, sample_task):
        """Network errors should be retryable."""
        with patch.object(agent, '_make_api_call', side_effect=ConnectionError):
            result = await agent.execute(sample_task)
        assert result.status == 'failed'
        assert result.error.retryable is True
        assert result.error.code == 'NETWORK_ERROR'

    async def test_invalid_input_is_not_retryable(self, agent, sample_task):
        """Invalid input should NOT be retryable."""
        sample_task.input = {'invalid_field': True}
        result = await agent.execute(sample_task)
        assert result.status == 'failed'
        assert result.error.retryable is False
        assert result.error.code == 'INVALID_INPUT'

    async def test_timeout_is_retryable(self, agent, sample_task):
        """Timeouts should be retryable."""
        with patch.object(agent, '_make_api_call', side_effect=TimeoutError):
            result = await agent.execute(sample_task)
        assert result.error.retryable is True
        assert result.error.code == 'TIMEOUT'

    async def test_auth_error_is_not_retryable(self, agent, sample_task):
        """Auth errors should NOT be retryable."""
        agent.auth = {'access_token': 'expired_token'}
        with patch.object(agent, '_make_api_call', side_effect=AuthorizationError):
            result = await agent.execute(sample_task)
        assert result.error.retryable is False
        assert result.error.code == 'AUTH_ERROR'

    async def test_internal_error_is_not_retryable(self, agent, sample_task):
        """Unexpected internal errors should NOT be retryable."""
        with patch.object(agent, 'handle_<domain>_<action>', side_effect=RuntimeError('unexpected')):
            result = await agent.execute(sample_task)
        assert result.error.retryable is False
        assert result.error.code == 'INTERNAL_ERROR'
        assert result.error.next_action == 'escalate'

    async def test_rate_limited_is_retryable(self, agent, sample_task):
        """Rate limit errors should be retryable."""
        with patch.object(agent, '_make_api_call', side_effect=RateLimitError):
            result = await agent.execute(sample_task)
        assert result.error.retryable is True
        assert result.error.code == 'RATE_LIMITED'
```

### 5. Idempotency Tests (test_idempotency.py)

```python
class TestIdempotency:
    async def test_same_key_returns_cached_result(self, mock_redis, agent, sample_task):
        """Second execution with same idempotency_key returns cached result."""
        cached_result = AgentResult(status='completed', output={'cached': True}, artifacts=[], error=None)
        mock_redis.get = AsyncMock(return_value=cached_result.model_dump_json())

        result = await execute_with_idempotency(
            sample_task, agent.execute, mock_redis,
            agent_max_duration_sec=180,
        )
        assert result.output.get('cached') is True

    async def test_concurrent_execution_returns_in_progress(self, mock_redis, agent, sample_task):
        """If another instance holds the lock, return TaskInProgress."""
        mock_redis.get = AsyncMock(return_value=None)  # no cached result
        mock_redis.set = AsyncMock(return_value=False)  # lock not acquired

        result = await execute_with_idempotency(
            sample_task, agent.execute, mock_redis,
            agent_max_duration_sec=180,
        )
        assert isinstance(result, TaskInProgress)

    async def test_expired_deadline_returns_deadline_exceeded(self, mock_redis, agent, sample_task):
        """No cached result + expired deadline should fail before execution."""
        from datetime import timedelta
        from shared.time_utils import utcnow

        mock_redis.get = AsyncMock(return_value=None)
        sample_task.deadline_ts = utcnow() - timedelta(seconds=1)

        result = await execute_with_idempotency(
            sample_task, agent.execute, mock_redis,
            agent_max_duration_sec=180,
        )
        assert result.status == 'failed'
        assert result.error.code == 'DEADLINE_EXCEEDED'

    async def test_cached_result_replays_even_if_deadline_passed(self, mock_redis, agent, sample_task):
        """Stored result must replay even when the deadline is already in the past."""
        from datetime import timedelta
        from shared.time_utils import utcnow

        cached_result = AgentResult(status='completed', output={'cached': True}, artifacts=[], error=None)
        mock_redis.get = AsyncMock(return_value=cached_result.model_dump_json())
        sample_task.deadline_ts = utcnow() - timedelta(seconds=1)

        result = await execute_with_idempotency(
            sample_task, agent.execute, mock_redis,
            agent_max_duration_sec=180,
        )
        assert result.status == 'completed'
        assert result.output.get('cached') is True
```

### 6. Auth Isolation Tests (test_auth_isolation.py)

**Critical** — verify credentials never leak outside runtime context.

```python
class TestAuthIsolation:
    async def test_auth_not_in_events(self, agent, sample_task):
        """Credentials must NEVER appear in emitted events."""
        sample_task.input['auth'] = {
            'access_token': 'secret_token_123',
            'credential_ref': 'cred_abc',
        }
        events_emitted = []
        agent.emit_event = AsyncMock(side_effect=lambda **kwargs: events_emitted.append(kwargs))

        await agent.handle(sample_task, 'msg-001', 'streams:test:normal')

        for event in events_emitted:
            payload_str = json.dumps(event.get('payload', {}))
            assert 'secret_token_123' not in payload_str
            assert 'access_token' not in payload_str

    async def test_auth_not_in_result(self, agent, sample_task):
        """Credentials must NEVER appear in AgentResult output."""
        sample_task.input['auth'] = {'access_token': 'secret_token_123'}
        result = await agent.execute(sample_task)
        result_str = result.model_dump_json()
        assert 'secret_token_123' not in result_str

    async def test_auth_cleared_after_execution(self, agent, sample_task):
        """self.auth must be None after execute() returns."""
        sample_task.input['auth'] = {'access_token': 'token'}
        await agent.handle(sample_task, 'msg-001', 'streams:test:normal')
        assert agent.auth is None

    async def test_auth_not_in_learnings(self, agent, sample_task, tmp_path):
        """Credentials must not appear in learnings.md updates."""
        sample_task.input['auth'] = {'access_token': 'secret_token_123'}
        learnings_path = tmp_path / 'learnings.md'
        learnings_path.write_text('')
        agent.learnings_path = learnings_path

        await agent.execute(sample_task)

        content = learnings_path.read_text()
        assert 'secret_token_123' not in content
```

## Shared Fixtures (conftest.py)

```python
import pytest
from unittest.mock import AsyncMock
from shared.contracts import TaskEnvelope
from shared.time_utils import utcnow

@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.xadd = AsyncMock(return_value='msg-id')
    redis.rpush = AsyncMock()
    redis.xack = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.exists = AsyncMock(return_value=False)
    redis.delete = AsyncMock()
    redis.expire = AsyncMock()
    redis.hset = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    return redis

@pytest.fixture
def make_task():
    def _make(intent: str, input_data: dict = None, **kwargs):
        defaults = {
            'task_id': f'tsk_test_{id(intent)}',
            'task_list_id': 'test:list',
            'parent_task_id': None,
            'session_id': 'sess_20250115',
            'sender': 'cosmic/orchestrator:1.0.0',
            'recipient': 'cosmic/test-agent:1.0.0',
            'intent': intent,
            'input': input_data or {'query': 'test'},
            'idempotency_key': f'idem_{id(intent)}',
            'priority': 'normal',
            'signature': 'test-sig',
            'source': 'user',
            'channel': 'desktop',
        }
        defaults.update(kwargs)
        return TaskEnvelope(**defaults)
    return _make
```

## Coverage Expectations

| Category | Minimum Coverage |
|---|---|
| Intent handlers (happy path) | 100% of declared intents |
| Intent handlers (error path) | At least 2 error cases per intent |
| Schema validation | All input + output schemas |
| Auth isolation | All 4 leak vectors (events, result, learnings, post-execution) |
| Idempotency | Cache hit + concurrent execution |
| Error classification | Every error code in agent_card.yaml retry_policy |

## Running Tests

```bash
# From project root
pytest agents/<agent_name>/tests/ -v

# With coverage
pytest agents/<agent_name>/tests/ --cov=agents.<agent_name> --cov-report=term-missing

# Specific test file
pytest agents/<agent_name>/tests/test_agent.py -v
```
