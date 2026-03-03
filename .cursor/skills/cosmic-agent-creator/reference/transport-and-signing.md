# Transport & Signing

## Redis Streams: Queue Design

Redis Streams provide consumer groups, acknowledgement, replay, and crash recovery.
Priority is implemented via stream tiers.

### Stream Naming

```
# Task streams: PER-AGENT (shared across all instances via consumer group)
streams:cosmic/research-agent:1.0.0:high
streams:cosmic/research-agent:1.0.0:normal
streams:cosmic/research-agent:1.0.0:low

# Liveness keys: PER-INSTANCE (each worker writes independently)
registry:cosmic/research-agent:1.0.0:research-1
registry:cosmic/research-agent:1.0.0:research-2

# Intent index: Redis Set mapping intent -> agent_ids
intent:research.topic            -> {cosmic/research-agent:1.0.0}

# Shared event stream — orchestrator AND gateway listen
streams:events

# Orchestrator's own input streams (reverse tasks from agents)
streams:cosmic/orchestrator:1.0.0:high
streams:cosmic/orchestrator:1.0.0:normal

# Dead letter queue
streams:dlq

# Idempotency keys
idempotency:{idempotency_key}           # execution lock
idempotency:result:{idempotency_key}    # stored terminal result

# Seq counters
event_seq:{task_id}                     # atomic per-task sequence allocation
```

**Critical distinction:** Liveness keys are per-instance. Stream keys are per-agent.
All workers consume from the same stream via a shared consumer group.

### Consumer Group Setup

```python
async def join_consumer_group(agent_id: str, instance_id: str, redis):
    for priority in ['high', 'normal', 'low']:
        stream = f'streams:{agent_id}:{priority}'
        try:
            await redis.xgroup_create(stream, 'workers', id='0', mkstream=True)
        except ResponseError as e:
            if 'BUSYGROUP' not in str(e):
                raise  # group already exists — expected
```

### Worker Loop (How Agents Consume Tasks)

```python
STREAMS = {
    'high':   f'streams:{AGENT_ID}:high',
    'normal': f'streams:{AGENT_ID}:normal',
    'low':    f'streams:{AGENT_ID}:low',
}

# XAUTOCLAIM min_idle_time MUST exceed max_task_duration_sec * 2
CLAIM_MIN_IDLE_MS = self.max_task_duration_sec * 2 * 1000

async def run(self):
    while True:
        # Crash recovery: reclaim messages from dead consumers
        for priority in ['high', 'normal', 'low']:
            claimed = await redis.xautoclaim(
                STREAMS[priority], 'workers', self.instance_id,
                min_idle_time=CLAIM_MIN_IDLE_MS, start_id='0-0'
            )
            if claimed[1]:
                for msg in claimed[1]:
                    await self._process_message(msg, STREAMS[priority])

        # Normal consumption: priority ordering with aging
        consume_order = await self._priority_order_with_aging()
        for priority in consume_order:
            messages = await redis.xreadgroup(
                groupname='workers',
                consumername=self.instance_id,
                streams={STREAMS[priority]: '>'},
                count=1,
                block=100,  # ms — fall through to next tier
            )
            if messages:
                task = TaskEnvelope.model_validate_json(messages[0]['envelope'])
                await self.handle(task, messages[0].id, STREAMS[priority])
                break  # restart priority loop after handling
```

### Dispatch (Orchestrator -> Redis)

```python
async def dispatch(task: TaskEnvelope, redis: Redis):
    validate_outbound_version(task)
    stream = f'streams:{task.recipient}:{task.priority}'

    # Backpressure check
    length = await redis.xlen(stream)
    if length > STREAM_MAXLEN:
        raise BackpressureError(f'Stream {stream} at capacity ({length}/{STREAM_MAXLEN})')

    await redis.xadd(
        stream,
        {'envelope': task.model_dump_json()},
        maxlen=STREAM_MAXLEN_APPROX,
    )
```

### XAUTOCLAIM Tuning

`min_idle_time` must be **at least 2x** the agent's `max_task_duration_sec`.

If `research.topic` has `timeout_sec: 180`, set `min_idle_time` to at least `360000` ms.
Otherwise a healthy worker running a long task gets its message reclaimed = duplicate processing.

### Backpressure Constants

```python
# shared/config.py
STREAM_MAXLEN = 10000           # hard cap — dispatch rejects above this
STREAM_MAXLEN_APPROX = 12000    # approximate trim on XADD
EVENTS_STREAM_MAXLEN = 50000    # trim streams:events
MEMORY_WRITE_MAX_PER_HOUR = 50  # per-agent memory write rate limit
```

## HMAC Signing

### Per-Channel Shared Secret Model

Each agent has ONE secret (`AGENT_SECRET` env var). The orchestrator holds all secrets
(`AGENT_SECRETS` env var — JSON map of agent_id -> secret). Both directions use the
agent's secret. The `sender` field determines which secret the orchestrator uses to verify.

```python
# shared/auth.py
import hmac, hashlib, json, os

ORCHESTRATOR_ID = 'cosmic/orchestrator:1.0.0'

def canonical_payload(task: TaskEnvelope) -> bytes:
    data = task.model_dump(mode='json', exclude={'signature'})
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

def sign_task(task: TaskEnvelope, secret: str) -> str:
    return hmac.new(secret.encode(), canonical_payload(task), hashlib.sha256).hexdigest()

def verify_task(task: TaskEnvelope, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_task(task, secret), signature)

# --- Agent side ---

def sign_reverse_task(task: TaskEnvelope) -> TaskEnvelope:
    """Agent signs outgoing reverse-task with its own secret."""
    secret = os.environ['AGENT_SECRET']
    task.signature = sign_task(task, secret)
    return task

def verify_incoming_agent(task: TaskEnvelope) -> bool:
    """Agent verifies incoming task from orchestrator with its own secret."""
    secret = os.environ['AGENT_SECRET']
    return verify_task(task, task.signature, secret)
```

### What Is Covered by the Signature

The HMAC covers the ENTIRE TaskEnvelope (excluding the `signature` field itself):
- task_id, sender, recipient, intent, input (including input.auth), idempotency_key
- priority, leader_epoch, contract_version, created_at, source, source_id, channel
- input_artifacts, deadline_ts, parent_task_id, session_id, task_list_id

Mutating ANY field after signing invalidates the signature.

## Idempotency

**Order is mandatory:** replay stored result first, then apply the deadline guard, then acquire the execution lock.

```python
# shared/idempotency.py

async def execute_with_idempotency(
    task: TaskEnvelope,
    handler: Callable,
    redis: Redis,
    agent_max_duration_sec: int = 0,
) -> ExecutionResult:
    dedupe_key = f'idempotency:{task.idempotency_key}'
    result_key = f'idempotency:result:{task.idempotency_key}'

    # Step 1: replay terminal result if one is already stored
    stored = await redis.get(result_key)
    if stored:
        return AgentResult.model_validate_json(stored)

    # Step 2: apply deadline guard only when no stored result exists
    if task.deadline_ts is not None:
        remaining_sec = deadline_remaining_sec(task.deadline_ts)
        if remaining_sec <= 0:
            return AgentResult(
                status='failed',
                output={},
                artifacts=[],
                error=AgentError(
                    code='DEADLINE_EXCEEDED',
                    retryable=False,
                    message='Task deadline already exceeded before execution',
                    next_action='skip',
                ),
            )
        base_ttl = int(remaining_sec) * 2
    elif agent_max_duration_sec > 0:
        base_ttl = agent_max_duration_sec * 2
    else:
        base_ttl = DEDUPE_TTL_FALLBACK_SEC

    dedupe_ttl = max(DEDUPE_TTL_MIN_SEC, base_ttl)

    # Step 3: acquire execution lock
    exec_since = utcnow().isoformat()
    acquired = await redis.set(dedupe_key, exec_since, nx=True, ex=dedupe_ttl)
    if not acquired:
        raw = await redis.get(dedupe_key)
        since_str = raw if raw else exec_since
        return TaskInProgress(
            task_id=task.task_id,
            idempotency_key=task.idempotency_key,
            executing_since=datetime.fromisoformat(since_str),
            check_after_sec=30,
        )

    # Step 4: execute and store the terminal result
    try:
        result = await handler(task)
        await redis.set(result_key, result.model_dump_json(), ex=RESULT_TTL_SEC)
        return result
    except Exception:
        await redis.delete(dedupe_key)
        raise
```

**Critical invariant:** if a terminal result is stored, replay it even if the deadline is already in the past. Only apply the deadline guard when no stored result exists.

### Idempotency Key Design by Source

| Source | Key Format | Deterministic? |
|---|---|---|
| User message | `uuid4()` | No — each message is unique |
| Cron | `cron:{cron_id}:{fire_date}` | Yes — same cron + date = same key |
| Heartbeat | `heartbeat:default:{time_bucket}` | Yes — same interval = same key |
| Webhook | `webhook:{webhook_id}:{provider_event_id}` | Yes — provider redeliveries get same key |
| Hook | `uuid4()` | No — hooks are one-shot internal events |

**Why this matters for agents:** Tasks from crons, heartbeats, and webhooks use deterministic
keys so that redeliveries are deduplicated by the idempotency layer (SETNX). Your agent only
sees each unique event once. Webhook handlers extract provider-specific event IDs (e.g.,
GitHub's `X-GitHub-Delivery`, Slack's `event_id`) to build these deterministic keys.

## Event Emission

```python
# shared/events.py

async def emit_event(self, task_id: str, event_type: str, payload: dict):
    """Emit an event to streams:events with atomic seq allocation."""
    seq = await self.redis.incr(f'event_seq:{task_id}')

    event = EventEnvelope(
        task_id=task_id,
        agent_id=self.agent_id,
        event_type=event_type,
        seq=seq,
        payload=payload,
        emitted_at=utcnow(),
    )

    msg_id = await self.redis.xadd(
        'streams:events',
        {'event': event.model_dump_json()},
        maxlen=EVENTS_STREAM_MAXLEN,
    )

    # Index: per-task event list for replay
    await self.redis.rpush(f'task_events:{task_id}', msg_id)

    # Terminal events: expire the seq key
    if event_type in TERMINAL_EVENTS:
        await self.redis.expire(f'event_seq:{task_id}', RESULT_TTL_SEC)
        await self.redis.expire(f'task_events:{task_id}', RESULT_TTL_SEC)
```

## Redis Client

```python
# shared/redis_client.py
import redis.asyncio as redis

async def get_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get('REDIS_HOST', 'localhost'),
        port=int(os.environ.get('REDIS_PORT', 6379)),
        decode_responses=True,  # all reads return str, not bytes
    )
```

**NEVER use `redis.keys()`** — it blocks the entire Redis event loop. Use `redis.scan()` instead.

## SQLite

```python
# shared/sqlite_client.py
SQLITE_PRAGMAS = [
    'PRAGMA journal_mode=WAL',
    'PRAGMA busy_timeout=5000',
    'PRAGMA synchronous=NORMAL',
    'PRAGMA wal_autocheckpoint=1000',
    'PRAGMA foreign_keys=ON',
]

def connect_sync(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for pragma in SQLITE_PRAGMAS:
        conn.execute(pragma)
    return conn

async def connect_async(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    for pragma in SQLITE_PRAGMAS:
        await conn.execute(pragma)
    return conn
```

**Every SQLite database** — sessions.db, credentials.db, scheduler.db, webhooks.db,
task_ledger.db, registry.db, agents/*/store/data/*.db — MUST use these helpers.
Direct `sqlite3.connect()` without pragmas is a bug.

## Time Utilities

```python
# shared/time_utils.py
from datetime import datetime, timezone

def utcnow() -> datetime:
    """Always returns timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)

def to_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def deadline_remaining_sec(deadline_ts: datetime) -> float:
    return (to_utc_aware(deadline_ts) - utcnow()).total_seconds()
```

**NEVER use `datetime.utcnow()`** — deprecated in Python 3.12, produces naive datetimes.
