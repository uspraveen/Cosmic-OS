# COSMIC Multi-Agent System — Architecture & Implementation Spec

**Agent Runtime Contract v1.6**

| Layer | Technology |
|---|---|
| Deployment Model | **Single-user-per-instance.** Each user gets their own VM/VPC with a dedicated backend. User isolation is infrastructure-level (VM boundary), not application-level. |
| Transport | Redis Streams |
| Routing | Model Router (Groq classifier) — three routes: opus, haiku, perplexity |
| Session & Memory | Session Manager (Gateway) + Qdrant local (hybrid: dense + sparse vectors) + .md file store |
| Embeddings | Dense: Qwen3-embedding-8b via OpenRouter. Sparse: FastEmbed BM25 (local). |
| Credentials | Gateway Credential Manager (OAuth PKCE, encrypted token store) |
| User Auth & Provisioning | Supabase (user accounts, VM provisioning, API key auth — §3.5a) |
| Process Management | supervisord (containers) / systemd (bare-metal) |
| Protocol | Agent Runtime Contract v1.6 |
| Tool Access | Declared per agent in `agent_card.yaml` |
| Agent IDs | `{org}/{name}:{version}` |
| Scheduling | Gateway Scheduler / Cron Manager (Crons + Heartbeats) — SQLite-backed, timezone-aware, observable, internal + desktop control surfaces |
| Webhooks | Gateway Webhook Handler — provider signature verification, event conversion |
| Channel Adapters | Multi-platform messaging — Desktop (WebSocket), WhatsApp, Telegram, Slack, Discord, CLI |
| Hooks | Gateway Hooks Engine — internal state change triggers |
| Input Tagging | `source`, `source_id`, `channel` fields on every TaskEnvelope for observability |
| Task Planning | Orchestrator Task Planner (§31) — LLM-driven decomposition, SQLite plan ledger, concurrent multi-plan execution |
| Universal Agent Tools | StepPlan + MemoryRead + MemoryWrite — injected into every agent runtime, not declared in agent cards (§32) |

> **This is the single implementation-ready spec.** All code in this document reflects the final v1.6 contract. There are no superseded sections, no version history, no delta patches. Build from this document only.
>
> **Deployment model: single-user-per-instance.** Every COSMIC deployment is a dedicated backend for one user — their own VM/VPC with its own Redis, SQLite, Qdrant, agents, and memory store. There is no multi-tenant user isolation in the application layer because the VM boundary IS the isolation. All `user_id` fields in schemas exist for future extensibility (e.g., shared household instance) but within a single deployment, `user_id` is always the same value. Do not build application-level user isolation — it is unnecessary and adds complexity for no benefit in this deployment model.

---

## 1. System Architecture: Three Layers

Every design decision flows from one mental model. Keep these three layers strictly separated — confusion comes from mixing them.

| Layer | Question | Examples |
|---|---|---|
| **Contract** (WHAT moves) | Message schemas, envelope shapes | TaskEnvelope, EventEnvelope, Heartbeat |
| **Transport** (HOW it moves) | Wire protocol, queue technology | Redis Streams |
| **Topology** (WHERE it runs) | Process layout, scaling model | supervisord workers, containers, K8s |

**The contract never changes across deployment targets.** Only transport and topology evolve.

### 1.1 Layer Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    ONE VM / VPC PER USER                          │
│  Everything below runs as a dedicated instance.                  │
│  User isolation = VM boundary. No app-level tenancy.             │
├──────────────────────────────────────────────────────────────────┤
│                     FIVE INPUT SOURCES                            │
│                                                                   │
│  ① MESSAGES (human, any channel)                                 │
│  ┌────────┐ ┌─────────┐ ┌─────────┐ ┌───────┐ ┌─────┐          │
│  │Desktop │ │WhatsApp │ │Telegram │ │ Slack │ │ CLI │ ...       │
│  │  App   │ │ Adapter │ │ Adapter │ │Adapter│ │Agent│           │
│  └───┬────┘ └────┬────┘ └────┬────┘ └──┬────┘ └──┬──┘          │
│      └───────────┴───────────┴─────────┴─────────┘              │
│                    Channel Adapter Layer                          │
│                                                                   │
│  ② HEARTBEATS (timer, default 30m)   ③ CRONS (scheduled jobs)   │
│  ④ HOOKS (internal state changes)    ⑤ WEBHOOKS (ext. systems)  │
│                                                                   │
│  All five produce TaskEnvelopes tagged with source + channel.    │
├──────────────────────────────────────────────────────────────────┤
│                         GATEWAY                                   │
│  Single FastAPI door. Authenticates, validates, rate-limits,     │
│  manages sessions, routes queries, streams responses.            │
│  Tags every input with source, source_id, and channel.           │
│  Strips <awaiting_reply/> tags from LLM responses.               │
│  Sticky routing (reply → last_route) + task input relay.         │
│  Usage Ledger (SQLite): append-only token/cost telemetry for     │
│  direct LLM routes, orchestrator, model router, and agents.      │
│                                                                   │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ │
│  │  SESSION MANAGER │ │CREDENTIAL MANAGER│ │    SCHEDULER     │ │
│  │ Context assembly,│ │ OAuth flows,     │ │ Crons + Heart-   │ │
│  │ memory retrieval │ │ token storage,   │ │ beats. SQLite-   │ │
│  │ (Qdrant hybrid), │ │ refresh, scope   │ │ backed. Internal │ │
│  │ pruning,         │ │ validation,      │ │ API. Fires       │ │
│  │ compaction,      │ │ credential audit.│ │ TaskEnvelopes on │ │
│  │ daily reset.     │ │ Internal-only.   │ │ schedule.        │ │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘ │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ │
│  │ WEBHOOK HANDLER  │ │  HOOKS ENGINE    │ │ CHANNEL ADAPTER  │ │
│  │ Provider sig     │ │ Internal state   │ │ REGISTRY         │ │
│  │ verification,    │ │ change triggers, │ │ Platform adapters│ │
│  │ event conversion │ │ lifecycle events │ │ channel-tagged   │ │
│  │ to TaskEnvelopes │ │ to TaskEnvelopes │ │ session routing  │ │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘ │
├──────────────────────────────────────────────────────────────────┤
│                       MODEL ROUTER                                │
│  Lightweight classifier (Groq). Decides which backend handles    │
│  each query. Called by Gateway after context assembly.            │
├──────────────┬──────────────────┬────────────────────────────────┤
│ opus         │ haiku            │ perplexity                     │
│ (task/cont.) │ (GK)             │ (search)                       │
├──────────────┼──────────────────┴────────────────────────────────┤
│              │                                                    │
│  ORCHESTRATOR              Direct LLM APIs                       │
│  Brain. Grounds            Gateway calls Haiku / Perplexity      │
│  context, decomposes       directly — no orchestrator overhead.  │
│  goals, routes by                                                │
│  capability, retries,      Propagates source, source_id, and    │
│  merges outputs.           channel to all child TaskEnvelopes.   │
│  Resolves credentials                                            │
│  via Gateway internal                                            │
│  API before dispatch.                                            │
│  Creates/manages crons                                           │
│  via Gateway Scheduler                                           │
│  internal API.                                                   │
├────────┬────────┬────────┬────────┬────────┬─────────────────────┤
│research│ docs   │diagram │browser │ system │                     │
│_agent  │ _agent │_agent  │_agent  │ _agent │ ... future agents   │
│(worker)│(worker)│(worker)│(worker)│(worker)│                     │
├────────┴────────┴────────┴────────┴────────┴─────────────────────┤
│  CLI AGENT (alpha · sleeping · wakes on demand)                   │
│  Full system access including agent code, prompts, configs.      │
│  Operates as an embedded terminal assistant with root-level      │
│  visibility into the entire COSMIC runtime.                       │
├──────────────────────────────────────────────────────────────────┤
│         ↑ All agents identical in shape ↑                         │
│         Each follows Agent Runtime Contract v1.6                  │
└──────────────────────────────────────────────────────────────────┘
```

| Component | Responsibility |
|---|---|
| **Desktop App** | Electron + React UI. Always-on background process registered at startup. Authenticates users via Cosmic API key against Supabase, auto-provisions Gateway URL and API token from the user's VM config (§3.5a). Maintains conversation history locally. Connects to Gateway via WebSocket. Reports the user's current IANA timezone to the Gateway on login/startup/resume and whenever the OS timezone changes; that timezone becomes the authoritative basis for daily session rollover and default cron scheduling. Settings panel triggers OAuth account connections via Gateway. One of several channel adapters — see §27. |
| **Channel Adapters** | Normalize platform-specific messages into the unified TaskEnvelope format. Each adapter handles authentication, message parsing, and response delivery for its platform. Available adapters: Desktop (WebSocket), WhatsApp, Telegram, Slack, Discord, CLI. New platforms are added by implementing the adapter interface (§27). |
| **Gateway** | Receives inputs from all five sources (messages, heartbeats, crons, hooks, webhooks), validates auth + schema, tags every input with `source`, `source_id`, and `channel`, assembles session context via the Session Manager (today's conversation + retrieved memories). Checks `awaiting_reply` for sticky routing (§3.7); otherwise calls Model Router for classification. Routes to appropriate backend, strips `<awaiting_reply/>` control tags from responses (§3.8), streams responses and task events via the originating channel adapter. Relays task input requests from `user_input:requests` to UI and user replies to `user_input:replies` (§3.12). Owns the Credential Manager (§22), Session Manager (§23), Scheduler / Cron Manager (§25), Webhook Handler (§26), Hooks Engine (§28), Channel Adapter Registry (§27), the Usage Ledger (`gateway/usage.db`) for append-only token/cost telemetry, and the Routing Audit store (`gateway/routing_audit.db`) for durable inspection of final route decisions. Persists the user-local timezone last reported by the desktop and uses it as the authoritative basis for 4 AM rollover and default cron scheduling. |
| **Scheduler / Cron Manager** | Gateway module that manages crons and heartbeats. Stores cron definitions, execution history, pause state, and the persisted user timezone snapshot in SQLite. Runs a polling loop that fires TaskEnvelopes to the orchestrator when jobs are due. Exposes an internal API for orchestrator CRUD plus a desktop-facing management surface for future observability/UI control (list, inspect, pause, resume, edit, delete). See §25. |
| **Webhook Handler** | Gateway module that receives HTTP POST callbacks from external systems (Gmail, GitHub, Jira, Slack). Verifies provider-specific signatures, converts payloads into TaskEnvelopes tagged with `source='webhook'`, and dispatches to the orchestrator. See §26. |
| **Hooks Engine** | Gateway module that fires TaskEnvelopes in response to internal state changes: gateway startup/shutdown, session reset, compaction, agent registration/deregistration. Configurable hook definitions stored alongside the Gateway. See §28. |
| **Model Router** | Lightweight stateless classifier. Determines which backend handles a query: `opus` (orchestrator — tasks, continuations, ambiguous input), `haiku` (direct API), or `perplexity` (direct API). Called by Gateway after context assembly — unless `awaiting_reply` sticky routing triggers first (§3.7), which skips the classifier entirely. No `unknown` route — `opus` is the fallback. |
| **Orchestrator** | Reads context, decomposes goals into subtasks via the Task Planner (§31), queries registry for capable healthy agents, resolves credentials via Gateway internal API when intents require provider access, dispatches via Redis, merges results. Classifies requests as simple (direct dispatch) or complex (structured plan with steps, dependencies, synthesis). Manages multiple concurrent plans. Propagates `source`, `source_id`, and `channel` from parent TaskEnvelope to all child tasks. Maintains a compact prompt-visible specialist shortlist derived from recent successful specialist usage in the registry; this shortlist is only a hint layer, while live agent discovery still flows through `agent_catalog_search` (§11, §32.6). Creates and manages cron jobs via the Gateway Scheduler internal API. Powered by Claude Opus. |
| **Sub-Agent Worker** | Single-domain specialist. Consumes Task Envelopes from its Redis stream, emits Event Envelopes, writes artifacts, sends heartbeats. Has access to universal tools (StepPlan, MemoryRead, MemoryWrite — see §32) injected by the agent runtime. |
| **Browser Agent** | Specialist agent for browser automation via Playwright. Navigates pages, fills forms, clicks elements, extracts content, takes screenshots. Runs in a sandboxed browser context. See §29. |
| **System Agent** | Specialist agent for OS-level automation. File system operations, process management, clipboard access, app control, shell command execution. Sandboxed by declared tool policies. See §29. |
| **CLI Agent** | Alpha-stage embedded terminal assistant with full system access. Sleeps by default, wakes on demand. Can read and modify agent code, prompts, configurations, and system state. Operates like a root-level maintenance console for the COSMIC runtime. See §30. |

### 1.2 Request Flow

Every input — regardless of source — follows the same processing pipeline through the Gateway. The five input sources converge into a single flow:

```
FIVE INPUT SOURCES
    │
    ├── ① Messages ────── Channel Adapters ──┐
    │   (Desktop, WhatsApp, Telegram,        │
    │    Slack, Discord, CLI)                 │
    │                                         │
    ├── ② Heartbeats ─── Scheduler ──────────┤
    │   (timer, default 30m)                  │
    │                                         │
    ├── ③ Crons ──────── Scheduler ──────────┤
    │   (scheduled jobs)                      │
    │                                         │
    ├── ④ Hooks ──────── Hooks Engine ───────┤
    │   (internal state changes)              │
    │                                         │
    ├── ⑤ Webhooks ───── Webhook Handler ────┤
    │   (external systems)                    │
    │                                         ▼
    │                    Gateway (auth, validate, rate-limit, session)
    │                    Tags: source, source_id, channel
    │                         │
    │                         ▼
    │                    Session Manager (assemble context + memories)
    │                         │
    │                         ▼
    │                    awaiting_reply check (§3.7)
    │                    ┌─ YES → route = last_route (skip classifier)
    │                    └─ NO ──► Model Router (/classify)
    │                         │
    │         ┌───────────────┼───────────────┐
    │         ▼               ▼               ▼
    │     route=opus      route=haiku    route=perplexity
    │         │               │               │
    │         ▼               ▼               ▼
    │    Orchestrator     Haiku API      Perplexity API
    │    → Agents         (direct)       (direct)
    │    → Redis
    │         │               │               │
    │         ▼               ▼               ▼
    │◄── task events ── stream tokens ── stream tokens + citations
         via originating   via originating   via originating
         channel adapter   channel adapter   channel adapter
                         (strip <awaiting_reply/> tags from all responses)
```

**Source-to-priority mapping:** User messages dispatch at `high` priority. Webhooks dispatch at `normal`. Crons and heartbeats dispatch at `low` (configurable per-cron). This ensures user queries always process first under full-bandwidth operation, while the aging mechanism (§18) prevents background tasks from starving.

**Why three paths?** A simple general knowledge question ("What is a knowledge graph?") does not need Claude Opus, agent decomposition, or Redis task envelopes. Routing it directly to Claude Haiku 4.5 saves cost and latency. Only genuine tasks ("Draft an email to my team"), continuations, and ambiguous inputs go through the orchestrator. There is no `unknown` route — the orchestrator handles anything the classifier can't confidently categorize. All three paths receive the same assembled session context (today's conversation + retrieved memories) from the Session Manager (see §23).

**Why sticky routing before the classifier?** When a model asks the user a question (and emits `<awaiting_reply/>`), the user's response must go back to that same model — regardless of what the classifier would have chosen. A short reply like "B" has no context for the classifier to work with, and would be misrouted. Checking the `awaiting_reply` flag first is a zero-cost Gateway-level check that avoids an unnecessary Groq API call. See §3.7 and §3.8 for the full mechanism.

**Channel-aware response delivery:** Responses route back through the originating channel adapter. A query from WhatsApp gets its response delivered to WhatsApp. A cron result gets delivered to the channel specified in the cron definition's `delivery_channel` field. The `channel` field on the TaskEnvelope drives this routing.

---

## 2. Model Router

The Model Router is a lightweight classification service that determines which backend should handle a user query. Its purpose is cost optimization — avoiding the expensive Opus orchestrator for queries that don't need it.

### 2.1 Classification Routes

| Route | Backend | When | Cost |
|---|---|---|---|
| `opus` | Orchestrator (Claude Opus) | Tasks, continuations, ambiguous input, progress queries, anything referencing prior work | High |
| `perplexity` | Perplexity API (direct) | Real-time info, citations, current events, verification | Medium |
| `haiku` | Claude Haiku 4.5 API (direct) | General knowledge, explanations, theory, brainstorming | Low |

### 2.2 Hard Routing Rules

Routing has two layers: a **Gateway-level pre-check** (before the classifier is called) and **post-classification enforcement** (after the classifier returns).

**Layer 1 — Gateway pre-check (before classifier):**

If the last stored assistant message has `awaiting_reply = true`, the Gateway skips the classifier entirely and routes directly to the model that sent that message (`last_route`). This is the sticky routing mechanism — see §3.7 for the full logic and §3.8 for how `awaiting_reply` is detected.

**Layer 2 — Post-classification enforcement (non-negotiable):**

1. If `is_task` is true → `opus` (always, regardless of other flags)
2. If input is a continuation or ambiguous (`go on`, `ok`, `why?`, progress queries) → `opus` (the orchestrator is smart enough to handle these — it has full session context and task state)
3. Else if `needs_latest` OR `needs_citations` → `perplexity`
4. Else if `confidence < 0.5` → `opus` (low confidence fallback)
5. Else → `haiku`

**Effective priority order (both layers combined):**

```
1. awaiting_reply flag set    → last_route  (Gateway pre-check, skips classifier)
2. is_task                    → opus        (post-classification)
3. is_continuation            → opus        (post-classification)
4. needs_latest/citations     → perplexity  (post-classification)
5. confidence < 0.5           → opus        (post-classification)
6. default                    → haiku       (post-classification)
```

**There is no `unknown` route.** A personal assistant should never bounce back "please clarify." The orchestrator (Opus) handles ambiguous inputs — it can respond conversationally, ask a specific clarifying question, or take action based on context. This is a UX decision: the smartest model handles the hardest-to-classify inputs.

```python
def enforce_rules(parsed: dict) -> dict:
    """Post-classification rule enforcement. Called only when Gateway
    pre-check did NOT trigger sticky routing (no awaiting_reply flag)."""
    out = {
        'route': parsed.get('route', 'haiku'),
        'needs_latest': bool(parsed.get('needs_latest')),
        'needs_citations': bool(parsed.get('needs_citations')),
        'is_task': bool(parsed.get('is_task')),
        'is_continuation': bool(parsed.get('is_continuation')),
        'confidence': parsed.get('confidence', 0.5),
        'signals': parsed.get('signals', []),
    }

    if out['is_task']:
        out['route'] = 'opus'
    elif out['is_continuation']:
        out['route'] = 'opus'
    elif out['needs_latest'] or out['needs_citations']:
        out['route'] = 'perplexity'
    elif out['confidence'] < 0.5:
        out['route'] = 'opus'       # low confidence → let Opus decide
    else:
        out['route'] = 'haiku'

    return out
```

### 2.3 Architecture

The Model Router runs as a standalone FastAPI microservice, called by the Gateway via internal HTTP after authentication.

| Property | Value |
|---|---|
| Server | FastAPI + uvicorn |
| Port | `8742` (internal, not exposed to desktop app) |
| Classifier model | `openai/gpt-oss-20b` via Groq API |
| Protocol | HTTP/2 with pre-warmed connection pool |
| Typical RTT | < 100ms |

**Why behind the Gateway, not before it?** Placing the Model Router after the Gateway ensures:
- Unauthenticated requests never reach the classifier (no wasted Groq API calls)
- The Gateway owns all cross-cutting concerns (auth, rate limiting, sessions)
- The Model Router stays a pure stateless classifier with no auth/session logic
- The desktop app has a single endpoint to talk to (the Gateway)

### 2.4 Classification Endpoint

```python
# POST http://localhost:8742/classify
# Request
{
    "query": "What are today's headlines about Nvidia?",
    "conversation_context": [                       # optional
        {"role": "user", "content": "Tell me about AI stocks"},
        {"role": "assistant", "content": "Here are the top AI stocks..."}
    ],
    "max_completion_tokens": 430
}

# Response
{
    "classification": {
        "route": "perplexity",
        "needs_latest": true,
        "needs_citations": true,
        "is_task": false,
        "confidence": 0.92,
        "signals": ["current_events", "news_query", "time_sensitive"]
    },
    "metrics": {
        "rtt_ms": 67.3,
        "connection_warmed": true,
        "http2_enabled": true
    },
    "classifier_model": "openai/gpt-oss-20b",
    "raw_classifier_output": "{...}",
    "timestamp_unix_ms": 1706000000000
}
```

### 2.5 Conversation Context

The Model Router accepts optional conversation history for better classification. The Gateway attaches the last N messages from the session to the classification request, along with the route used for each message. The Model Router does not maintain any session state itself.

With context, the classifier can distinguish between a continuation of a task discussion (`opus`) vs a follow-up to a knowledge question (`haiku`). Without context, ambiguous inputs like `"go on"` default to `opus` — the orchestrator has session state and task context to handle them intelligently.

**Note:** The `awaiting_reply` sticky routing check happens at the Gateway level BEFORE this classification call. If the last assistant message has `awaiting_reply = true`, the classifier is never called — the Gateway routes directly to `last_route`. The Model Router only sees queries that passed through the pre-check without triggering sticky routing. See §3.7.

**Routing audit note:** The Gateway persists the full Model Router response payload for each classified request into `gateway/routing_audit.db`, alongside the final effective route chosen after overrides, sticky routing, and fallback handling. This includes `classification`, `metrics`, `classifier_model`, `raw_classifier_output`, and any future classifier-side debug/reasoning fields returned by the Model Router. The audit store is Gateway-owned because the final truth may differ from the raw classifier route.

### 2.6 Fallback & Error Handling

If the Model Router is unreachable or returns an error, the Gateway falls back to `opus`. This is the safest default — the orchestrator can handle any query type, even if it's more expensive. The system degrades gracefully rather than failing.

```python
# Gateway-side fallback
async def classify_query(query: str, context: list) -> dict:
    try:
        result = await http_client.post(
            f'{MODEL_ROUTER_URL}/classify',
            json={'query': query, 'conversation_context': context},
            timeout=MODEL_ROUTER_TIMEOUT_SEC,
        )
        return result.json()['classification']
    except Exception:
        return {'route': 'opus', 'confidence': 0.0, 'signals': ['router_fallback']}
```

### 2.6a Circuit Breaker: External LLM APIs

The Gateway calls three external LLM APIs (Groq for Model Router, Claude Haiku 4.5, Perplexity). Without a circuit breaker, if an API goes down, every request to that route hangs for the full timeout before failing — then the next request does the same. Under sustained failure, the system appears frozen for all queries routed to the failing API.

**Pattern:** Per-API circuit breaker with three states: `closed` (healthy — requests flow normally), `open` (unhealthy — requests fail immediately with fallback), `half_open` (probe — one request passes through to test recovery).

```python
# gateway/circuit_breaker.py
import time

class CircuitBreaker:
    """Per-API circuit breaker. Prevents cascading hangs when an
    external LLM API is down or degraded."""

    def __init__(self, name: str,
                 failure_threshold: int = 3,
                 recovery_timeout_sec: int = 30):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self.failures = 0
        self.state = 'closed'             # closed | open | half_open
        self.opened_at: float = 0

    def should_allow(self) -> bool:
        if self.state == 'closed':
            return True
        if self.state == 'open':
            if time.time() - self.opened_at >= self.recovery_timeout_sec:
                self.state = 'half_open'
                return True               # allow one probe request
            return False
        if self.state == 'half_open':
            return False                   # only one probe at a time
        return True

    def record_success(self):
        self.failures = 0
        self.state = 'closed'

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = 'open'
            self.opened_at = time.time()

# One breaker per external API
haiku_breaker = CircuitBreaker('haiku', failure_threshold=3, recovery_timeout_sec=30)
perplexity_breaker = CircuitBreaker('perplexity', failure_threshold=3, recovery_timeout_sec=30)
model_router_breaker = CircuitBreaker('model_router', failure_threshold=3, recovery_timeout_sec=15)
```

**Gateway integration (extends §3.7 route handling):**

```python
async def stream_from_haiku(session_id, content, context, channel_adapter, **kwargs):
    if not haiku_breaker.should_allow():
        # Haiku is down — reroute to opus (safe fallback, same as §2.6 logic)
        logger.warning('Haiku circuit open — rerouting to opus')
        await dispatch_to_orchestrator(session_id, content, context, channel_adapter, **kwargs)
        return
    try:
        await _stream_haiku_impl(session_id, content, context, channel_adapter, **kwargs)
        haiku_breaker.record_success()
    except Exception:
        haiku_breaker.record_failure()
        # Fallback to opus for this request
        await dispatch_to_orchestrator(session_id, content, context, channel_adapter, **kwargs)
```

**Fallback behavior:**

| API Down | Fallback Route | Rationale |
|---|---|---|
| Haiku | `opus` | Orchestrator (Opus) can handle any query, including general knowledge |
| Perplexity | `opus` | Orchestrator can delegate to research agent for web search |
| Model Router (Groq) | `opus` (already in §2.6) | Same existing fallback — orchestrator handles everything |

**Why `opus` is always the fallback:** The orchestrator is the most capable path — it can handle any query type, even if it's more expensive. When an API recovers (`half_open` → probe succeeds → `closed`), routing returns to normal automatically. The user never sees an error — they get a slightly more expensive but correct response.

### 2.7 Configuration

All secrets and configuration are loaded from environment variables.

```ini
# Model Router environment
GROQ_API_KEY=<secret>
CLASSIFIER_MODEL=openai/gpt-oss-20b
MODEL_ROUTER_HOST=0.0.0.0
MODEL_ROUTER_PORT=8742
HTTP2_ENABLED=true
CONNECTION_POOL_SIZE=10
KEEPALIVE_EXPIRY=30

# Gateway references to Model Router
MODEL_ROUTER_URL=http://localhost:8742
MODEL_ROUTER_TIMEOUT_SEC=3
MODEL_ROUTER_FALLBACK_ROUTE=opus           # also used when confidence < 0.5

# Circuit breaker configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD=3        # failures before opening circuit
CIRCUIT_BREAKER_RECOVERY_SEC=30            # seconds before half-open probe
```

**Process-scoped environment rule:** the architecture uses environment variables as the configuration interface, but in real deployments those variables are injected **per service/process**, not through one giant backend-wide env file. A Gateway process, Model Router process, WhatsApp Bridge process, and Orchestrator process each receive only the variables they need.

**Canonical VM pattern:**

- `/etc/cosmic/gateway.env`
- `/etc/cosmic/model-router.env`
- `/etc/cosmic/whatsapp-bridge.env`
- `/etc/cosmic/orchestrator.env`
- optional agent-specific env files when an agent needs unique runtime settings

Shared values such as `GATEWAY_INTERNAL_TOKEN` may intentionally appear in more than one env file when two internal services must trust each other. This is expected and preferable to leaking every secret into every process.

`CLASSIFIER_MODEL` is only the model identifier. The Model Router resolves its SDK family,
provider `base_url`, context limits, and pricing metadata from `shared/model_specs.json` (see
§7.2c) rather than hardcoding them in `model_router/config.py`.

---

## 3. Gateway

The Gateway is the single entry point for all input sources. It handles authentication, request validation, input tagging, model routing, session management, credential management, usage monitoring, scheduling, webhook ingestion, internal hooks, channel adapter management, and response delivery. No external client communicates with the orchestrator or agents directly.

### 3.1 Responsibilities

| Responsibility | Description |
|---|---|
| Authentication | Validates local API token on every request, WebSocket connection, and channel adapter connection. Webhook endpoints use provider-specific signature verification. |
| Input tagging | Tags every incoming TaskEnvelope with `source` (user/cron/webhook/heartbeat/hook/agent), `source_id` (specific origin identifier), and `channel` (platform identifier). These tags propagate through the entire task tree for observability. |
| Session & memory management | Owns the Session Manager: assembles context (today's conversation + retrieved memories) for every query before classification. Manages daily sessions, context window pruning, compaction, and memory retrieval via Qdrant hybrid search (dense + sparse vectors — see §23) |
| Model routing | Checks `awaiting_reply` flag for sticky routing (skips classifier), otherwise calls Model Router to classify each query |
| Reply routing | Sticky routing: if last assistant message has `awaiting_reply` flag, routes user's reply directly to the model that asked — no classifier call |
| Task dispatch | For `opus` route: creates TaskEnvelope, dispatches to orchestrator via Redis |
| Direct LLM proxy | For `haiku`/`perplexity` routes: calls LLM API directly, streams response, strips control tags |
| Event streaming | Streams task progress events and task input requests to the originating channel adapter |
| Task input relay | Consumes `user_input:requests` from orchestrator, surfaces to UI via the appropriate channel; collects user replies, publishes to `user_input:replies` |
| Rate limiting | Token-bucket rate limiting per session |
| Credential management | Owns the Credential Manager: handles OAuth PKCE flows, stores encrypted refresh tokens, manages connected accounts, validates scopes, refreshes access tokens, and exposes internal-only endpoints for the orchestrator to resolve credentials at dispatch time (see §22) |
| Usage monitoring | Owns the Usage Ledger: append-only token/cost telemetry in `gateway/usage.db`. Logs direct LLM route usage locally and accepts internal usage events from the orchestrator, agents, Session Manager, and Model Router. Stores `llm_call_placed_at` as the UTC timestamp of each metered call. |
| Scheduling | Owns the Scheduler / Cron Manager module: manages cron job definitions, pause/resume state, heartbeat configuration, and the persisted user timezone snapshot in SQLite. Runs a polling loop that fires TaskEnvelopes when jobs are due. Exposes internal API for orchestrator CRUD plus a desktop-facing management surface for observability and future UI controls (see §25) |
| Webhook ingestion | Owns the Webhook Handler: receives HTTP POST callbacks from external systems, verifies provider signatures, converts payloads to TaskEnvelopes tagged with `source='webhook'` (see §26) |
| Channel management | Owns the Channel Adapter Registry: manages platform adapters (Desktop/WebSocket, WhatsApp, Telegram, Slack, Discord, CLI), normalizes incoming messages, routes responses back to originating channels (see §27) |
| Internal hooks | Owns the Hooks Engine: fires TaskEnvelopes on internal state changes (startup, shutdown, session reset, compaction, agent registration) (see §28) |

### 3.2 Connection Model: WebSocket + REST

The desktop app maintains one persistent WebSocket connection per desktop installation for real-time bidirectional communication. REST endpoints are available for stateless operations and control-plane actions such as authentication, health checks, channel status, and WhatsApp pairing.

**Why WebSocket as primary?** The desktop app is always-on (background process, registered as startup app). A persistent WebSocket connection eliminates connection setup overhead on every query and enables the server to push task events, progress updates, and clarification requests without polling.

**Desktop ownership rule:** The long-lived Gateway connection is owned by the Electron **main process**, not by the renderer. UI reloads, route changes, or renderer crashes must not drop the VM connection. The renderer communicates with the main-process connection manager over IPC.

**Single Gateway server:** The Gateway is one FastAPI service with multiple route surfaces. Desktop chat uses WebSocket. Webhooks, health checks, scheduler/credential APIs, and sidecar-bridge callbacks use REST endpoints on the same Gateway process. New channel adapters do **not** create separate FastAPI servers; they extend the existing Gateway with additional adapter logic and, when needed, internal callback routes.

**Production edge recommendation:** The desktop-facing endpoint should be exposed over HTTPS/WSS on a public hostname. Recommended VM topology: Caddy terminates TLS on `:443` and reverse-proxies to the Gateway on `127.0.0.1:8080`. Simpler deployments may expose the Gateway directly or place it behind a cloud load balancer, but the client-visible endpoint must still be HTTPS/WSS.

### 3.3 WebSocket Protocol

**Production connection:** `wss://gateway.example.com/ws`

**Local development connection:** `ws://127.0.0.1:8080/ws?token=<local_api_token>&device_id=<device_id>`

**Authentication and identity:**

- Preferred production pattern: the Electron main process opens the socket with `Authorization: Bearer <GATEWAY_LOCAL_API_TOKEN>` and `X-Device-Id: <device_id>` headers.
- Compatibility / local-dev fallback: `token` and `device_id` query params on the WebSocket URL.
- The Gateway derives the concrete desktop channel from the authenticated connection context as `desktop:<device_id>`. Desktop clients do **not** send arbitrary desktop channel strings inside message bodies.

**Client → Server messages:**

```python
# Submit a new query
{
    "type": "query",
    "session_id": "sess_abc123",        # null for first message of the day
    "content": "Write a Python script to parse logs",
    "conversation_context": [           # last N messages for model router
        {"role": "user", "content": "I need to analyze server logs"},
        {"role": "assistant", "content": "What format are the logs in?"}
    ],
    "request_id": "req_001"             # client-generated, for correlation
}

# Reply to a task input request (from user_input:requests queue)
{
    "type": "task.input_reply",
    "input_request_id": "uir_001",          # matches the request
    "task_id": "tsk_abc123",
    "content": "Use source A",
    "request_id": "req_002"                 # client-generated correlation
}

# Cancel an in-progress task
{
    "type": "cancel",
    "task_id": "tsk_abc123",
    "request_id": "req_003"
}

# Resume after reconnect
{
    "type": "resume",
    "session_id": "sess_20260307",
    "known_task_ids": ["tsk_abc123", "tsk_def456"],
    "timezone": "America/Chicago",      # current desktop IANA timezone
    "request_id": "req_resume_001"
}

# Keepalive
{
    "type": "ping",
    "ts_unix_ms": 1772899200000
}
```

**Server → Client messages:**

```python
# Resume acknowledgment + state re-sync after reconnect
{
    "type": "resume.ok",
    "request_id": "req_resume_001",
    "session_id": "sess_20260307",
    "channel": "desktop:desk_a1b2c3",
    "history_tail": [ ... ],
    "active_tasks": [ ... ],
    "pending_inputs": [ ... ]
}

# Keepalive response
{
    "type": "pong",
    "ts_unix_ms": 1772899200000,
    "server_time": "2026-03-07T12:00:00Z"
}

# Classification result (sent immediately after model router returns)
{
    "type": "route_result",
    "request_id": "req_001",
    "route": "opus",
    "classification": { ... }
}

# Streaming tokens (haiku/perplexity direct response)
{
    "type": "response.chunk",
    "request_id": "req_001",
    "session_id": "sess_abc123",
    "content": "Here is a script that...",
    "done": false
}

# Final response (haiku/perplexity/opus conversational complete)
{
    "type": "response.complete",
    "request_id": "req_001",
    "session_id": "sess_abc123",
    "content": "Full response text...",
    "route": "haiku",
    "awaiting_reply": true,             # set if model emitted <awaiting_reply/> tag
    "metrics": { "rtt_ms": 450 }
}

# Task created (opus route — orchestrator pipeline started)
{
    "type": "task.created",
    "request_id": "req_001",
    "task_id": "tsk_abc123",
    "session_id": "sess_abc123"
}

# Task progress event (forwarded from orchestrator/agents)
{
    "type": "task.progress",
    "task_id": "tsk_abc123",
    "agent_id": "cosmic/research-agent:1.0.0",
    "payload": { ... },
    "seq": 3
}

# Task completed
{
    "type": "task.completed",
    "task_id": "tsk_abc123",
    "result": { ... },
    "artifacts": [ ... ]
}

# Task failed
{
    "type": "task.failed",
    "task_id": "tsk_abc123",
    "error": { "code": "TIMEOUT", "message": "..." }
}

# Task needs user input (from user_input:requests queue — see §13.2)
{
    "type": "task.input_required",
    "input_request_id": "uir_001",
    "task_id": "tsk_abc123",
    "agent": "cosmic/research-agent:1.0.0",
    "question": "Found conflicting sources. Which to prioritize?",
    "options": ["source_a", "source_b"],
    "status": "pending"
}

# Error
{
    "type": "error",
    "request_id": "req_001",
    "code": "RATE_LIMITED",
    "message": "Too many requests. Try again in 5 seconds."
}
```

### 3.3a Desktop Connection Lifecycle

The WebSocket is a **transport**, not the session. Reconnects do not create new conversation sessions.

**Rules:**

1. One live Gateway WebSocket per desktop installation (`device_id`) is owned by the Electron main process.
2. `device_id` is a stable per-installation identifier generated once and stored locally. The Gateway derives the concrete desktop channel `desktop:<device_id>` from it.
3. The active conversation session is the current **shared daily session** (`sess_YYYYMMDD`). WebSocket reconnects reattach to that session; they do not create a new one.
4. The desktop connection manager sends an application-level keepalive (`ping` / `pong`) every 25-30 seconds.
5. On disconnect, the desktop reconnects with exponential backoff + jitter (for example: `1s, 2s, 4s, 8s, 16s, 30s max`).
6. Immediately after reconnect, the desktop sends `resume` so the Gateway can re-sync:
   - tail of the current daily session
   - active task summaries
   - pending `task.input_required` items
7. The desktop includes its current IANA timezone in `resume` and on any later timezone-change event so the Gateway keeps scheduler/session boundaries aligned to the user's local time.
8. REST remains the control plane for stateless operations such as `/auth/*`, `/health*`, `/channels/*`, and other non-chat actions.

**Why explicit resume matters:** a live desktop connection can drop because of laptop sleep, Wi-Fi transitions, renderer reloads, Gateway restarts, reverse-proxy idle timeouts, or VM redeploys. The system must recover the user's current shared daily session and in-flight tasks after reconnect rather than pretending this is a brand-new conversation.

### 3.4 REST Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/query` | Submit query (returns task_id for opus, or streams response for haiku/perplexity) |
| `GET` | `/tasks/{task_id}` | Get task status and result |
| `GET` | `/tasks/{task_id}/events` | SSE stream of task events |
| `POST` | `/tasks/{task_id}/input-reply/{input_request_id}` | Reply to a task input request (alternative to WebSocket `task.input_reply`) |
| `GET` | `/sessions/{session_id}` | Get session conversation history |
| `GET` | `/routing-audit?limit=N` | Inspect recent Gateway routing decisions from `gateway/routing_audit.db`. Protected by the desktop/local API token. Operational visibility only — not part of the message pipeline. |
| `DELETE` | `/tasks/{task_id}` | Cancel a task |
| `GET` | `/health` | Health check |
| `GET` | `/health/ready` | Readiness check (all dependencies up) |
| | | |
| | **Scheduler / Cron Manager (desktop app / local API token)** | |
| `GET` | `/scheduler/overview` | Return heartbeat state plus all cron jobs with computed state (`active`, `paused`, `error`), next fire time, last fire time, last outcome, timezone, and recent execution summary for observability UI |
| `GET` | `/scheduler/crons` | List cron jobs for the desktop Cron Manager UI |
| `GET` | `/scheduler/crons/{cron_id}` | Get cron details, execution history, timezone, pause state, and next fire time |
| `POST` | `/scheduler/crons/{cron_id}/pause` | Pause a cron without deleting it; records `paused_at` and `pause_reason` |
| `POST` | `/scheduler/crons/{cron_id}/resume` | Resume a paused cron and recompute `next_fire_at` |
| `GET` | `/scheduler/heartbeat` | Get heartbeat status, delivery channel, active hours, timezone behavior, and current pause/enabled state |
| `POST` | `/scheduler/heartbeat/pause` | Pause heartbeat delivery without deleting config |
| `POST` | `/scheduler/heartbeat/resume` | Resume heartbeat delivery |
| | | |
| | **Scheduler (internal — orchestrator only)** | |
| `POST` | `/internal/scheduler/crons` | Create a cron job — returns `cron_id` |
| `GET` | `/internal/scheduler/crons` | List all cron jobs (active, paused, all) |
| `GET` | `/internal/scheduler/crons/{cron_id}` | Get cron job details |
| `PATCH` | `/internal/scheduler/crons/{cron_id}` | Update cron schedule, timezone, prompt, priority, enabled/pause metadata |
| `DELETE` | `/internal/scheduler/crons/{cron_id}` | Delete a cron job |
| `POST` | `/internal/scheduler/heartbeat/config` | Update heartbeat configuration |
| `GET` | `/internal/scheduler/heartbeat/config` | Get current heartbeat configuration |
| | | |
| | **Webhooks (external systems)** | |
| `POST` | `/webhooks/{webhook_id}` | Receive webhook callback from external system — signature verified per provider |
| | | |
| | **Webhooks (internal — orchestrator only)** | |
| `POST` | `/internal/webhooks` | Register a new webhook endpoint — returns `webhook_id` + secret |
| `GET` | `/internal/webhooks` | List registered webhooks |
| `DELETE` | `/internal/webhooks/{webhook_id}` | Deregister a webhook |
| | | |
| | **Credential Management (desktop app)** | |
| `GET` | `/auth/connect/{provider}` | Initiate OAuth PKCE flow — redirects user to provider consent screen |
| `GET` | `/auth/callback/{provider}` | OAuth callback — exchanges code for tokens, stores encrypted refresh token |
| `GET` | `/auth/accounts` | List all connected provider accounts for the current user |
| `DELETE` | `/auth/accounts/{account_id}` | Disconnect a provider account — revokes tokens, marks credential revoked |
| | | |
| | **Channel Management (desktop app / local API token)** | |
| `GET` | `/channels` | List configured channels, enabled state, and high-level connection status for the desktop settings UI |
| `GET` | `/channels/{platform}/status` | Get detailed status for one channel (connected, disconnected, pairing_required, bridge_unreachable, last_error) |
| `POST` | `/channels/whatsapp/pairing/qr` | Request a fresh WhatsApp pairing QR from the running WhatsApp Bridge and return renderable QR payload to the desktop app |
| `DELETE` | `/channels/whatsapp/session` | Disconnect the active WhatsApp session and clear bridge-owned device auth state |
| `GET` | `/channels/whatsapp/config` | Retrieve persisted WhatsApp bridge configuration (allowed phone, self-chat-only flag) from the running bridge |
| `POST` | `/channels/whatsapp/config` | Update WhatsApp bridge configuration (allowed phone, self-chat-only flag). Persisted by the bridge in `store/bridge-config.json` |
| | | |
| | **Credential Management (internal — orchestrator only)** | |
| `POST` | `/internal/credentials/resolve` | Resolve provider + scopes + user → short-lived access token + credential_ref. Authenticated via internal service token (see §22.3) |
| `POST` | `/internal/credentials/refresh` | Refresh an existing credential by credential_ref → new access token. Used for mid-task token refresh (see §22.5) |
| `GET` | `/internal/credentials/accounts/{provider}` | List connected accounts for a provider. Used by orchestrator for account resolution. |
| `POST` | `/internal/credentials/lookup-resource` | Look up resource bindings (e.g., doc_id → account) for orchestrator account resolution |
| | | |
| | **Channel Intake (internal — sidecar bridges)** | |
| `POST` | `/internal/channels/whatsapp/incoming` | Receive authenticated inbound events from the WhatsApp Bridge, validate payload, normalize via the adapter, then enter the Gateway user-message processing pipeline |
| | | |
| | **Memory (internal — agents via service token)** | |
| `POST` | `/internal/memory/search` | Semantic search across shared memory store. Used by MemoryRead universal tool (§32.5) |
| `POST` | `/internal/memory/write` | Write a new memory to the shared store. Used by MemoryWrite universal tool (§32.5) |
| | | |
| | **Usage Monitoring (internal — gateway, orchestrator, agents, model router)** | |
| `POST` | `/internal/usage/log` | Append one token/cost usage event to `gateway/usage.db`. |

### 3.4a Usage Ledger

The Gateway owns a dedicated append-only SQLite ledger for model/API usage. This is separate from
`sessions.db` and `credentials.db` because usage tracking is observability/billing telemetry, not
conversation state or secret storage.

**Database:** `gateway/usage.db`

```sql
-- gateway/usage.db

CREATE TABLE usage_events (
    llm_call_id TEXT PRIMARY KEY,           -- unique ID for one metered LLM/API call
    user_id TEXT NOT NULL,                  -- always same value per VM today; future-proof

    source_component TEXT NOT NULL,         -- 'gateway', 'orchestrator', 'agent', 'session_manager', 'model_router'
    source_id TEXT,                         -- 'cosmic/orchestrator:1.0.0', 'cosmic/research-agent:1.0.0', 'gateway:haiku'

    task_id TEXT,                           -- NULL for non-task calls
    plan_id TEXT,
    parent_task_id TEXT,
    session_id TEXT,

    route TEXT,                             -- 'opus', 'haiku', 'perplexity', nullable
    operation TEXT NOT NULL,                -- 'orchestrator.process', 'research.topic', 'model_router.classify'
    usage_kind TEXT NOT NULL,               -- 'chat_completion', 'classifier', 'embedding', 'rerank', 'other'

    provider TEXT NOT NULL,                 -- 'anthropic', 'google', 'perplexity', 'groq', 'openrouter'
    model TEXT NOT NULL,                    -- actual provider model id

    request_id TEXT,                        -- local correlation id
    provider_request_id TEXT,               -- provider-side request id if available

    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,

    estimated_cost_usd REAL,
    latency_ms INTEGER,

    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_code TEXT,

    metadata_json TEXT,                     -- raw provider usage blob / extra fields
    llm_call_placed_at TIMESTAMP NOT NULL   -- UTC timestamp when the outbound LLM/API call was initiated
);

CREATE INDEX idx_usage_task_id
    ON usage_events(task_id);

CREATE INDEX idx_usage_plan_id
    ON usage_events(plan_id);

CREATE INDEX idx_usage_session_id
    ON usage_events(session_id);

CREATE INDEX idx_usage_source
    ON usage_events(source_component, source_id, llm_call_placed_at);

CREATE INDEX idx_usage_provider_model
    ON usage_events(provider, model, llm_call_placed_at);
```

**Logging rules:**

- The table is append-only. Never update token counts after insert; corrections are separate admin events.
- `llm_call_placed_at` is the UTC timestamp when the outbound LLM/API call was initiated.
- The Gateway logs direct Haiku/Perplexity usage itself.
- The orchestrator logs its own Opus/task-planning usage via `/internal/usage/log`.
- Agents log their own model usage via `/internal/usage/log` when they call LLMs or embedding APIs.
- The Model Router logs classifier usage via `/internal/usage/log`.
- Session Manager memory/embed operations may also log usage rows when they consume metered APIs.

#### Internal Usage Event Contract

`POST /internal/usage/log` accepts one JSON object per metered LLM/API call. The request body maps
directly to one row in `usage_events`.

**Required fields:**

- `llm_call_id`: unique ID for this specific metered call. This is generated by the caller in the
  code path that initiates the outbound LLM/API request, not by the Gateway.
- `source_component`: one of `gateway`, `orchestrator`, `agent`, `session_manager`, `model_router`
- `operation`: logical operation name such as `orchestrator.process`, `research.topic`,
  `model_router.classify`
- `usage_kind`: one of `chat_completion`, `classifier`, `embedding`, `rerank`, `other`
- `provider`: provider name such as `anthropic`, `google`, `perplexity`, `groq`, `openrouter`
- `model`: concrete provider model ID
- `llm_call_placed_at`: UTC timestamp when the outbound call was initiated

**Optional fields:**

- `source_id`
- `task_id`
- `plan_id`
- `parent_task_id`
- `session_id`
- `route`
- `request_id`
- `provider_request_id`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `cached_tokens`
- `reasoning_tokens`
- `estimated_cost_usd`
- `latency_ms`
- `success`
- `error_code`
- `metadata_json`
- `user_id`

**Validation rules:**

- `llm_call_placed_at` must be an ISO 8601 / RFC 3339 UTC timestamp, for example
  `2026-03-02T15:04:05.123Z`.
- `llm_call_id` must be generated once per outbound metered call and reused on retries of the same
  log event.
- Omitted token fields default to `0`.
- If `estimated_cost_usd` is computed locally rather than returned by the provider, it must be
  derived from the matching entry in `shared/model_specs.json`.
- `success=false` may include `error_code`; successful rows should leave `error_code=NULL`.
- `metadata_json` is for provider-specific usage blobs and extra structured telemetry that does not
  deserve a first-class column.

**Idempotency and write behavior:**

- The Gateway treats `llm_call_id` as the idempotency key for `/internal/usage/log`.
- Retrying the same event with the same `llm_call_id` must not create a second row.
- The endpoint writes append-only into `gateway/usage.db`; it never mutates an existing usage row.

**Response shape:**

- `201 Created` when a new usage row is inserted
- `200 OK` when the same `llm_call_id` is replayed and treated as an idempotent duplicate
- response body:

```json
{
  "ok": true,
  "llm_call_id": "call_01HXYZ...",
  "deduplicated": false
}
```

### 3.4b Routing Audit

The Gateway owns a separate SQLite inspection store for routing decisions. This is distinct from `usage.db`: usage is billing/telemetry for outbound metered API calls, while routing audit records *why* a given inbound request was sent to `haiku`, `perplexity`, or `opus`.

**Database:** `gateway/routing_audit.db`

Each inbound request appends one row containing the Gateway's final decision context, including:

- request identity: `request_id`, `session_id`, `channel`, `source`, `source_id`
- input: `query_text`, bounded conversation-context snapshot
- decision controls: `route_override`, `sticky_hit`, `decision_source`
- routing outcome: `classifier_route`, `final_route`, `dispatch_target`, `confidence`, `signals`
- classifier payload: an allowlisted subset of the Model Router response payload (`classification`, `metrics`, `classifier_model`, `raw_classifier_output`, `timestamp_unix_ms`). Future debug/reasoning fields are **not** persisted automatically; adding them requires an explicit schema/privacy review.
- timing: classifier latency and total Gateway routing-decision latency
- error details when the classifier is unavailable and the Gateway falls back to `opus`

**Design rule:** this store is written by the Gateway, not the Model Router, because the final route may be changed by Gateway-local logic such as:

- manual route override from the desktop model selector
- sticky routing via `awaiting_reply`
- non-text inbound coercion to `opus`
- classifier failure fallback to `opus`

**Operational inspection route:** `GET /routing-audit?limit=N` returns the most recent rows from this store. It is protected by the desktop/local API token and is for debugging/inspection only; it is not part of the user message pipeline.

### 3.5 Authentication

For the desktop app, authentication uses a local API token (`GATEWAY_LOCAL_API_TOKEN`) that is auto-provisioned from Supabase during user login and stored in the desktop app's local SQLite database (`resources/user_data.db`) via the settings bridge. See §3.5a for the full user authentication and VM provisioning flow.

```python
import hmac
from fastapi import Request, HTTPException, WebSocket

LOCAL_API_TOKEN = os.environ['GATEWAY_LOCAL_API_TOKEN']

async def verify_auth(request: Request):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not hmac.compare_digest(token, LOCAL_API_TOKEN):
        raise HTTPException(status_code=401, detail='Invalid token')

async def verify_ws_auth(websocket: WebSocket):
    auth_header = websocket.headers.get('authorization', '')
    token = auth_header.replace('Bearer ', '').strip()
    if not token:
        token = websocket.query_params.get('token', '')
    device_id = (
        websocket.headers.get('x-device-id', '')
        or websocket.query_params.get('device_id', '')
    )
    if not hmac.compare_digest(token, LOCAL_API_TOKEN):
        await websocket.close(code=4001, reason='Invalid token')
        return False
    if not device_id:
        await websocket.close(code=4002, reason='Missing device_id')
        return False
    websocket.state.device_id = device_id
    websocket.state.channel = f'desktop:{device_id}'
    return True
```

**Desktop → Gateway connectivity:** The desktop app connects directly to the Gateway's public HTTPS/WSS endpoint using the provisioned `gateway_url` (for example, `https://gateway.user.example.com`). Recommended VM deployment: Caddy listens on `:443`, terminates TLS, and reverse-proxies to the Gateway on `127.0.0.1:8080`. Simpler deployments may terminate TLS at a cloud load balancer or expose the Gateway directly, but the desktop-facing endpoint must still be HTTPS/WSS. There is no SSH tunnel or VPN layer between the desktop and Gateway — the local API token provides the authentication boundary.

**Production auth model:** In production, each user gets a dedicated VM/VPC. The Gateway URL and local API token are auto-provisioned from Supabase during login — the user enters their Cosmic API key once and the desktop app resolves their VM config automatically (see §3.5a for the full Supabase schema, RPC function, and desktop auth flow). The desktop settings UI for WhatsApp reduces to just the user's phone number and a "Connect" button. The token and Gateway URL are injected by the auth system, not entered manually.

**Future mobile app:** When a mobile client is added, the auth layer can be extended with JWT or OAuth2 without changing the Gateway's internal routing logic. The auth middleware is a pluggable concern.

**Internal service authentication:** The `/internal/*` credential, memory, and usage endpoints are protected by a separate internal service token (`GATEWAY_INTERNAL_TOKEN`), not the desktop app's local API token. The orchestrator and any agent/runtime component that needs sanctioned internal APIs hold this token. The desktop app cannot access internal endpoints.

```python
INTERNAL_SERVICE_TOKEN = os.environ['GATEWAY_INTERNAL_TOKEN']

async def verify_internal_auth(request: Request):
    token = request.headers.get('X-Internal-Token', '')
    if not hmac.compare_digest(token, INTERNAL_SERVICE_TOKEN):
        raise HTTPException(status_code=403, detail='Internal access only')
```

This same internal service token protects sidecar bridge callbacks such as `/internal/channels/whatsapp/incoming`. The WhatsApp Bridge authenticates to the existing Gateway service over an internal route; the Gateway does **not** open a second server or a WhatsApp-specific FastAPI process.

### 3.5a User Authentication & VM Provisioning (Supabase)

The desktop app authenticates users against a centralized Supabase project before connecting to any per-user Gateway. This is the **platform-level** authentication layer — it proves the user is a valid COSMIC subscriber and resolves which VM/Gateway they should connect to. It is separate from the **Gateway-level** `GATEWAY_LOCAL_API_TOKEN` authentication described in §3.5, which secures traffic between the desktop app and the Gateway process running on the user's VM.

**Architecture:**

```text
Desktop App (Electron)
  └── Supabase RPC (authenticate_with_api_key)
        └── Returns: user profile + VM config (gateway_url, api_token, vm_ip, vm_dns)
              └── Desktop stores auth data in local SQLite (resources/user_data.db)
              └── Auto-configures Gateway URL + API token for all downstream connections
              └── Desktop reports current IANA timezone to Gateway after auth so
                    rollover and default cron scheduling use user-local time, not VM time

VM Bootstrap (Linux VM)
  └── Supabase RPC (consume_bootstrap_token)
        └── Returns: env payload for gateway/model-router/orchestrator
              └── bootstrap.py materializes repo env files
              └── bootstrap.py syncs /etc/cosmic/*.env
```

**Supabase project constants** (public — safe to commit, RLS-protected):

```
URL:      https://hluenippcdiejenmteen.supabase.co
Anon Key: eyJhbGciOi...  (standard Supabase anon key — only allows RLS-protected queries)
```

#### 3.5a.1 Supabase Schema: `user_vms` Table

Each COSMIC user gets one dedicated VM. The `user_vms` table maps users to their VM infrastructure.

**Source-of-truth rule:** `public.user_vms.api_token` is the canonical desktop-facing `GATEWAY_LOCAL_API_TOKEN` for that VM. Desktop login reads this value through `authenticate_with_api_key(...)`, and VM bootstrap installs this exact same value into `gateway.env`. `bootstrap.py` must not invent a different desktop/local token.

**Public-host rule:** `public.user_vms.vm_dns` must be the final public COSMIC hostname for that VM (for example, `<user_id>.thelearnchain.com`), not the raw cloud-provider hostname. The current bootstrap/Caddy flow maps `vm_dns` into `GATEWAY_PUBLIC_HOST`, and ACME/TLS issuance depends on that hostname being cert-eligible and publicly resolvable.

```sql
-- Create user_vms table
CREATE TABLE public.user_vms (
  id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  gateway_url text NOT NULL,          -- e.g. 'https://gateway.user.example.com'
  api_token   text NOT NULL,          -- the GATEWAY_LOCAL_API_TOKEN value for this user's VM
  vm_ip       text,                   -- e.g. '3.137.194.119'
  vm_dns      text,                   -- e.g. 'c2ece0ad-....thelearnchain.com'
  vm_region   text DEFAULT 'us-east-2',
  status      text DEFAULT 'active' CHECK (status IN ('active','suspended','terminated')),
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now(),
  CONSTRAINT  one_vm_per_user UNIQUE (user_id)
);

-- Index for fast lookup by user_id
CREATE INDEX idx_user_vms_user_id ON public.user_vms(user_id);

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_user_vms_updated_at
  BEFORE UPDATE ON public.user_vms
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- RLS: users can only read their own VM row
ALTER TABLE public.user_vms ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own VM"
  ON public.user_vms
  FOR SELECT
  USING (auth.uid() = user_id);
```

#### 3.5a.2 Supabase RPC: `authenticate_with_api_key`

A `SECURITY DEFINER` function that bypasses RLS to validate a Cosmic API key and return the user's profile and VM config in a single call. Callable with the Supabase anon key — no prior session required.

```sql
CREATE OR REPLACE FUNCTION public.authenticate_with_api_key(p_api_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user  record;
  v_vm    record;
BEGIN
  -- Look up user by API key
  SELECT id, full_name, email, is_privileged
    INTO v_user
    FROM public.users
   WHERE api_key = p_api_key;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'success', false,
      'error',   'invalid_api_key',
      'message', 'No user found with this API key.'
    );
  END IF;

  -- Check privilege flag
  IF NOT v_user.is_privileged THEN
    RETURN jsonb_build_object(
      'success', false,
      'error',   'not_privileged',
      'message', 'This account does not have desktop access.'
    );
  END IF;

  -- Look up active VM
  SELECT gateway_url, api_token, vm_ip, vm_dns, vm_region
    INTO v_vm
    FROM public.user_vms
   WHERE user_id = v_user.id
     AND status = 'active';

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'success', false,
      'error',   'no_active_vm',
      'message', 'No active VM found for this account.'
    );
  END IF;

  -- Return full auth payload
  RETURN jsonb_build_object(
    'success', true,
    'user', jsonb_build_object(
      'id',            v_user.id,
      'full_name',     v_user.full_name,
      'email',         v_user.email,
      'is_privileged', v_user.is_privileged
    ),
    'vm', jsonb_build_object(
      'gateway_url', v_vm.gateway_url,
      'api_token',   v_vm.api_token,
      'vm_ip',       v_vm.vm_ip,
      'vm_dns',      v_vm.vm_dns,
      'vm_region',   v_vm.vm_region
    )
  );
END;
$$;
```

**Example: provisioning a user's VM row:**

```sql
CREATE OR REPLACE FUNCTION public.provision_user_vm(
  p_user_email text,
  p_gateway_url text,
  p_vm_ip text,
  p_vm_dns text,
  p_vm_region text default 'us-east-2'
)
RETURNS TABLE (
  out_user_id uuid,
  out_vm_id uuid,
  out_gateway_api_token text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id uuid;
  v_vm_id uuid;
  v_existing_token text;
  v_new_token text;
BEGIN
  SELECT u.id
    INTO v_user_id
    FROM public.users AS u
   WHERE u.email = p_user_email;

  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'No user found for %', p_user_email;
  END IF;

  SELECT uv.id, uv.api_token
    INTO v_vm_id, v_existing_token
    FROM public.user_vms AS uv
   WHERE uv.user_id = v_user_id
   LIMIT 1;

  v_new_token := coalesce(
    nullif(v_existing_token, ''),
    'glt_' || encode(extensions.gen_random_bytes(24), 'hex')
  );

  INSERT INTO public.user_vms (
    user_id,
    gateway_url,
    api_token,
    vm_ip,
    vm_dns,
    vm_region,
    status
  )
  VALUES (
    v_user_id,
    p_gateway_url,
    v_new_token,
    p_vm_ip,
    p_vm_dns,
    p_vm_region,
    'active'
  )
  ON CONFLICT (user_id)
  DO UPDATE SET
    gateway_url = excluded.gateway_url,
    api_token = public.user_vms.api_token,
    vm_ip = excluded.vm_ip,
    vm_dns = excluded.vm_dns,
    vm_region = excluded.vm_region,
    status = 'active',
    updated_at = now()
  RETURNING public.user_vms.id, public.user_vms.api_token
    INTO v_vm_id, v_new_token;

  RETURN QUERY
  SELECT v_user_id, v_vm_id, v_new_token;
END;
$$;
```

**Operational rule:** re-running `provision_user_vm(...)` for an existing VM preserves the current `api_token`. This keeps desktop login stable while allowing `gateway_url`, IP, and DNS metadata to change.

#### 3.5a.3 Supabase Bootstrap Token Table

VM bootstrap uses one-time tokens stored as hashes in a private schema. This is separate from desktop login.

```sql
CREATE SCHEMA IF NOT EXISTS app_private;

CREATE TABLE IF NOT EXISTS app_private.vm_bootstrap_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  vm_id uuid NOT NULL REFERENCES public.user_vms(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  note text
);

REVOKE ALL ON SCHEMA app_private FROM public;
REVOKE ALL ON ALL TABLES IN SCHEMA app_private FROM public;
```

#### 3.5a.4 Supabase RPC: `issue_vm_bootstrap_token`

Operators mint a one-time bootstrap token immediately before provisioning or syncing a VM. The raw token is returned once; the database stores only its SHA-256 hash.

```sql
CREATE OR REPLACE FUNCTION app_private.issue_vm_bootstrap_token(
  p_user_email text,
  p_ttl_minutes integer default 20
)
RETURNS TABLE (
  raw_token text,
  expires_at timestamptz,
  vm_id uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app_private
AS $$
DECLARE
  v_vm_id uuid;
  v_raw_token text;
  v_expiry timestamptz;
BEGIN
  SELECT uv.id
    INTO v_vm_id
    FROM public.user_vms uv
    JOIN public.users u ON u.id = uv.user_id
   WHERE u.email = p_user_email
     AND uv.status = 'active'
   LIMIT 1;

  IF v_vm_id IS NULL THEN
    RAISE EXCEPTION 'No active VM found for %', p_user_email;
  END IF;

  v_raw_token := 'bs_' || encode(extensions.gen_random_bytes(24), 'hex');
  v_expiry := now() + make_interval(mins => p_ttl_minutes);

  INSERT INTO app_private.vm_bootstrap_tokens (vm_id, token_hash, expires_at)
  VALUES (
    v_vm_id,
    encode(extensions.digest(v_raw_token, 'sha256'), 'hex'),
    v_expiry
  );

  RETURN QUERY
  SELECT v_raw_token, v_expiry, v_vm_id;
END;
$$;
```

#### 3.5a.5 Supabase RPC: `consume_bootstrap_token`

`bootstrap.py` calls this RPC over Supabase REST using the public anon key plus the one-time bootstrap token. The RPC validates the token, reads the VM metadata from `public.user_vms`, reads shared provider secrets from Supabase Vault, marks the token as used, and returns the env bundle required by the VM.

```sql
CREATE OR REPLACE FUNCTION public.consume_bootstrap_token(p_token text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app_private, vault
AS $$
DECLARE
  v_token_hash text;
  v_token_row record;
  v_anthropic text;
  v_perplexity text;
  v_deepgram text;
  v_groq text;
  v_firecrawl text;
  v_xai text;
BEGIN
  IF p_token IS NULL OR length(trim(p_token)) < 20 THEN
    RETURN jsonb_build_object(
      'success', false,
      'error', 'invalid_token',
      'message', 'Bootstrap token missing or malformed.'
    );
  END IF;

  v_token_hash := encode(extensions.digest(trim(p_token), 'sha256'), 'hex');

  SELECT
    t.id AS token_id,
    uv.id AS vm_id,
    uv.user_id,
    uv.gateway_url,
    uv.api_token,
    uv.vm_ip,
    uv.vm_dns,
    uv.vm_region
  INTO v_token_row
  FROM app_private.vm_bootstrap_tokens t
  JOIN public.user_vms uv ON uv.id = t.vm_id
  WHERE t.token_hash = v_token_hash
    AND t.used_at IS NULL
    AND t.expires_at > now()
    AND uv.status = 'active'
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'success', false,
      'error', 'invalid_or_expired_token',
      'message', 'Bootstrap token is invalid, expired, or already used.'
    );
  END IF;

  SELECT decrypted_secret
    INTO v_anthropic
    FROM vault.decrypted_secrets
   WHERE name = 'platform_anthropic_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  SELECT decrypted_secret
    INTO v_perplexity
    FROM vault.decrypted_secrets
   WHERE name = 'platform_perplexity_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  SELECT decrypted_secret
    INTO v_deepgram
    FROM vault.decrypted_secrets
   WHERE name = 'platform_deepgram_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  SELECT decrypted_secret
    INTO v_groq
    FROM vault.decrypted_secrets
   WHERE name = 'platform_groq_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  SELECT decrypted_secret
    INTO v_firecrawl
    FROM vault.decrypted_secrets
   WHERE name = 'platform_firecrawl_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  SELECT decrypted_secret
    INTO v_xai
    FROM vault.decrypted_secrets
   WHERE name = 'platform_xai_api_key'
   ORDER BY created_at DESC
   LIMIT 1;

  IF v_anthropic IS NULL OR v_perplexity IS NULL THEN
    RETURN jsonb_build_object(
      'success', false,
      'error', 'missing_platform_secrets',
      'message', 'Required platform secrets are missing from Vault.'
    );
  END IF;

  UPDATE app_private.vm_bootstrap_tokens
     SET used_at = now()
   WHERE id = v_token_row.token_id
     AND used_at IS NULL;

  RETURN jsonb_build_object(
    'success', true,
    'vm', jsonb_build_object(
      'vm_id', v_token_row.vm_id,
      'user_id', v_token_row.user_id,
      'gateway_url', v_token_row.gateway_url,
      'vm_ip', v_token_row.vm_ip,
      'vm_dns', v_token_row.vm_dns,
      'vm_region', v_token_row.vm_region
    ),
    'gateway_env', jsonb_build_object(
      'GATEWAY_LOCAL_API_TOKEN', v_token_row.api_token,
      'GATEWAY_PUBLIC_HOST', v_token_row.vm_dns,
      'ANTHROPIC_API_KEY', v_anthropic,
      'PERPLEXITY_API_KEY', v_perplexity,
      'HAIKU_MODEL', 'claude-haiku-4-5'
    ),
    'orchestrator_env', jsonb_build_object(
      'ANTHROPIC_API_KEY', v_anthropic,
      'OPUS_MODEL', 'claude-opus-4-6'
    ),
    'meeting_env', jsonb_build_object(
      'DEEPGRAM_API_KEY', coalesce(v_deepgram, ''),
      'GROQ_API_KEY', coalesce(v_groq, '')
    ),
    'firecrawl_agent_env', jsonb_build_object(
      'FIRECRAWL_API_KEY', coalesce(v_firecrawl, '')
    ),
    'memory_env', jsonb_build_object(
      'XAI_API_KEY', coalesce(v_xai, '')
    )
  );
END;
$$;
```

**Current implementation note:** the current RPC shape returns `meeting_env.GROQ_API_KEY`, `orchestrator_env.OPUS_MODEL`, `vm.user_id`, `firecrawl_agent_env`, and `memory_env`. `bootstrap.py` normalizes these into the actual backend env files:

- `meeting_env.GROQ_API_KEY` -> `model_router.env:GROQ_API_KEY`
- `orchestrator_env.OPUS_MODEL` -> `orchestrator.env:ANTHROPIC_MODEL`
- `vm.user_id` -> `gateway.env:COSMIC_USER_ID`
- `firecrawl_agent_env.FIRECRAWL_API_KEY` -> `/etc/cosmic/agents/firecrawl-web-scrape-agent.env:FIRECRAWL_API_KEY`
- `memory_env.XAI_API_KEY` -> `memory.env:XAI_API_KEY`

This is acceptable for the current system, though a future cleanup may return `model_router_env` and `orchestrator_env.ANTHROPIC_MODEL` directly.

**Secret storage:** shared provider keys live once in Supabase Vault under names such as `platform_anthropic_api_key`, `platform_perplexity_api_key`, `platform_deepgram_api_key`, `platform_groq_api_key`, `platform_firecrawl_api_key`, and `platform_xai_api_key`. They are not duplicated into `public.user_vms`.

#### 3.5a.5a Production Bare-VM Provisioning Sequence

For the current production-ready bare-VM flow, the operator sequence is:

1. Create the VM and open inbound `80/tcp` and `443/tcp` in the attached security group before running bootstrap. Keep `8080/tcp` only if you want a temporary rollback/debug path during rollout.
2. Add a public DNS record for the VM under the COSMIC-owned base domain (for example, Squarespace-managed `thelearnchain.com`):
   - `A <user_id>.thelearnchain.com -> <vm_public_ip>`
3. Create/update the Supabase VM row with the final public HTTPS hostname and the same hostname in `vm_dns`:

```sql
SELECT *
FROM public.provision_user_vm(
  'user@example.com',
  'https://<user_id>.thelearnchain.com',
  '3.137.194.119',
  '<user_id>.thelearnchain.com',
  'us-east-2'
);
```

4. Mint a one-time bootstrap token:

```sql
SELECT *
FROM app_private.issue_vm_bootstrap_token('user@example.com', 20);
```

5. Copy `Backend/` to the VM and run:

```bash
cd ~/Cosmic-OS/Backend
export COSMIC_BOOTSTRAP_TOKEN='<one-time bootstrap token>'
python3 bootstrap.py --memory-repo-dir ~/cosmic-memory provision-vm
```

That flow fetches the env payload from Supabase, installs/syncs the backend env files, installs dependencies, installs systemd units, starts the backend services, installs/configures Caddy, and lets Caddy obtain the TLS certificate automatically.

**Memory provisioning rule:** if the VM should run the internal `cosmic-memory` service, operators must clone the `cosmic-memory` repo onto the VM first and pass its local path to bootstrap via `--memory-repo-dir` (for example, `~/cosmic-memory`). In the current implementation, bootstrap only materializes `memory.env`, installs `cosmic-memory.service`, enables the service, and injects `COSMIC_MEMORY_URL` into `gateway.env` when `--memory-repo-dir` is present. If the flag is omitted, the Gateway intentionally leaves long-term memory integration disabled for that VM.

6. Verify the public edge:

```bash
curl -fsS https://<user_id>.thelearnchain.com/health
```

If DNS or security-group ingress was fixed only after Caddy had already entered ACME retry backoff, force an immediate retry with:

```bash
sudo systemctl restart caddy
```

**Operational note:** the same base domain may be reused for all users, but each VM still needs a unique hostname (for example, `<user_id>.thelearnchain.com`) because the current architecture connects the desktop directly to that user's VM edge.

#### 3.5a.5b Cosmic Mail Auto-Provisioning

After `bootstrap.py` materializes `gateway.env` (and therefore knows the VM's `GATEWAY_LOCAL_API_TOKEN`), it provisions a Cosmic Mail organization, default mailbox, default agent, and a fresh org-scoped API key for the user — without ever exposing the cosmic-mail admin key to the VM. The trusted boundary is a Supabase Edge Function that holds the admin key in Supabase Vault.

##### Naming convention (one org per user)

| Thing | Convention | Example for `full_name="Praveen Raj U S", email="uspraveenraj@gmail.com", id="c2ece0ad-…"` |
| --- | --- | --- |
| Cosmic Mail org `name` (display) | `users.full_name` if present, else `email` local-part, else `"Cosmic User"` | `Praveen Raj U S` |
| Cosmic Mail org `slug` (globally unique) | `cosmic-<short_id>` where `<short_id>` = first 8 hex chars of `users.id` | `cosmic-c2ece0ad` |
| Cosmic Mail `organizations.cosmic_user_id` | `users.id` verbatim — used by `provision-cosmic-mail-org` to find existing orgs and skip re-creation | `c2ece0ad-4b2d-4af4-ae65-1b07660550dc` |
| Default mailbox local-part (memorable) | First non-empty of: `cosmic_<first_name>` → `cosmic_<last_name>` → `cosmic_<email_local_part>` → `cosmic_<short_id>`. Each candidate must be `>= 2` chars after slugify. Function tries them in order, falls through on `409 Conflict`. | `cosmic_praveen@mail.thelearnchain.com` |
| Default agent (`name`/`slug`) | `Cosmic` / `cosmic` (per-org slug uniqueness only) | `Cosmic` |
| Default agent `default_domain_id` | The platform-shared `mail.thelearnchain.com` domain row (see §3.5a.5c) | shared domain id |
| Default agent `approval_required` | `true` — bypassed only by the trusted-recipients allowlist (see §3.5a.5d) | `true` |
| Mailbox `display_name` (visible in From line) | `Cosmic` | `Cosmic <cosmic_praveen@mail.thelearnchain.com>` |

##### Trusted side: Supabase Edge Function `provision-cosmic-mail-org`

The Edge Function is the only component that holds the cosmic-mail admin key. It:

1. Authenticates the caller using the VM's own `GATEWAY_LOCAL_API_TOKEN` (matched against `public.user_vms.api_token, status='active'`). No JWT.
2. Reads `users.{id, email, full_name}` for naming.
3. Reads cosmic-mail platform secrets via the SECURITY DEFINER helper RPC `public._get_platform_cosmic_mail_secrets()` (PostgREST does not expose `vault.decrypted_secrets` directly).
4. Lists cosmic-mail domains via the admin key, finds the `is_shared=true, status=active` platform domain.
5. Lists cosmic-mail orgs and finds one with `cosmic_user_id = users.id`. If absent, creates it with the naming above.
6. Mints a fresh org-scoped API key.
7. Reuses the org's existing mailbox on the shared domain if any (covers legacy local-parts like `iamcosmic001`); otherwise creates one using the candidate cascade.
8. Reuses the org's existing `cosmic` agent if any; otherwise creates one with `default_domain_id = shared_domain.id, approval_required=true`. Links the agent to the mailbox as primary.
9. Revokes any prior org-scoped keys for this org (now that the new key is verified by the steps above).
10. Returns:
    ```json
    {
      "success": true,
      "base_url": "https://console.thelearnchain.com",
      "organization": {"id": "...", "name": "...", "slug": "...", "cosmic_user_id": "..."},
      "api_key":      {"id": "...", "plaintext": "cm_org_...", "name": "..."},
      "mailbox":      {"id": "...", "address": "cosmic_praveen@mail.thelearnchain.com", "domain": "mail.thelearnchain.com"},
      "agent":        {"id": "...", "slug": "cosmic", "name": "Cosmic"}
    }
    ```

The function is **idempotent**: re-running bootstrap on the same VM rotates only the org key (fresh mint + revoke prior), and reuses the existing org/mailbox/agent. Failure modes return `{"success": false, "error": "..."}` with HTTP status 4xx/5xx.

The cosmic-mail admin key never leaves Supabase. Only the org-scoped key is returned to the VM, where it is stored in `gateway/agent_email_integrations.db` (via `AgentEmailIntegrationStore.save_primary`).

##### Vault secrets

Stored once in `vault.secrets`:

- `platform_cosmic_mail_admin_api_key` — the cosmic-mail admin key. Treat as root credential.
- `platform_cosmic_mail_base_url` — the cosmic-mail base URL (no trailing slash), e.g. `https://console.thelearnchain.com`.

Read via SECURITY DEFINER RPC `public._get_platform_cosmic_mail_secrets()` (granted `service_role` only).

##### Bootstrap integration

In `bootstrap.py.materialize_bootstrap_env_files()`:

1. `consume_bootstrap_token` returns the env bundle (gateway, orchestrator, meeting, etc.).
2. **(new)** Extract `gateway.env.GATEWAY_LOCAL_API_TOKEN` and call `provision_cosmic_mail_org_via_edge_function(vm_api_token=...)`. On success, `persist_cosmic_mail_provisioning(payload)` writes the result into `gateway/agent_email_integrations.db` via `AgentEmailIntegrationStore.save_primary`.
3. The existing `build_email_agent_env_rendered()` reads `agent_email_integration_store` to populate `email-agent.env:COSMIC_MAIL_BASE_URL`/`COSMIC_MAIL_API_TOKEN`/`COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS` — it now picks up the just-provisioned values automatically.

Failures during step 2 are logged and **do not block** the rest of bootstrap; the VM boots without an Agent Email integration and a future re-run of bootstrap retries idempotently.

#### 3.5a.5c Cosmic Mail: Shared Platform Domain

Because `Domain.name` is globally unique in cosmic-mail, only one org can own `mail.thelearnchain.com`. To avoid per-user MX/DKIM provisioning for a single platform domain, cosmic-mail's `Domain` table carries an `is_shared boolean` flag. Mailbox and agent creation accept any domain owned by the caller's org **or** any `is_shared=true` domain, regardless of ownership. Strict ownership is preserved for DKIM rotation, deactivation, and deliverability checks (these still go through `authorize_domain`, not `authorize_domain_for_mailbox`).

The `mail.thelearnchain.com` row is owned by the platform's `cosmic` org and has `is_shared=true`. User-org mailboxes reference it via `domain_id`; the mailbox row's `organization_id` is the user's org, not the platform org's. DKIM, MX, and DMARC are configured once on the platform side.

#### 3.5a.5d Cosmic Mail: Trusted Recipients Bypass

The desktop app maintains a "Trusted senders" list in Spaces > Agent Email > Settings. Cosmic-OS is the source of truth and PUTs the full list to cosmic-mail's `PUT /v1/organizations/{org_id}/trusted-recipients` whenever it changes (and on every gateway adapter reconnect, for drift recovery). When `agent.approval_required = true` and **every** recipient on an outbound draft (to + cc + bcc) is in this allowlist, cosmic-mail bypasses the approval gate and sends directly. Email comparison is case-insensitive. Any single untrusted recipient falls back to the approval queue.

The orchestrator's email-agent card explains this contract so it correctly interprets `delivery_status="queued_for_approval"` / `queued_for_approval=true` as a successful handoff rather than a stuck task.

#### 3.5a.5e Desktop One-Click Connect

After bootstrap completes, the gateway holds the cosmic-mail base URL, org-scoped API token, and primary mailbox address in its local `AgentEmailIntegrationStore`. The desktop adopts them without typing via:

- `GET /channels/agent-email/desktop-config` (gateway, auth: `GATEWAY_LOCAL_API_TOKEN`) → `{available, base_url, api_token, primary_mailbox_address, organization_id}`. Returns `{available: false}` when the gateway has no integration configured.
- IPC bridge: `gateway:get-agent-email-desktop-config` → renderer.
- UI: Spaces > Agent Email > Settings > Connection has **two** buttons:
  1. *Use VM-provisioned config* (primary, recommended): calls the desktop-config endpoint, then immediately persists via the existing `gateway:save-agent-email-config` IPC.
  2. *Save connection* (secondary): the original manual base URL + API key form, kept for admin overrides.

#### 3.5a.6 Desktop App Auth Flow

The desktop app (Electron) implements a login gate that runs before the main UI loads:

```text
App startup
  → Settings bridge loads all settings from SQLite (resources/user_data.db)
  → Renderer checks for 'cosmicAuth' in settings
    ├── Found & valid → authState = 'authenticated' → show main UI
    └── Not found     → authState = 'unauthenticated' → show Login Modal

Login:
  → User enters Cosmic API key
  → Renderer calls window.cosmic.login(apiKey)
  → Main process (IPC 'auth:login'):
    1. POST to Supabase RPC: authenticate_with_api_key({ p_api_key: apiKey })
    2. On success: write to SQLite via settings bridge:
       - SAVE_SETTING:cosmicAuth:{full auth JSON}
       - SAVE_SETTING:gatewayBaseUrl:{vm.gateway_url}
       - SAVE_SETTING:gatewayApiToken:{vm.api_token}
       - ENSURE_SETTING:desktopDeviceId:{stable per-installation ID}
    3. Settings bridge persists to SQLite, emits updated settings
    4. Renderer receives updated settings → authState = 'authenticated'
    5. Main process starts or refreshes the GatewayConnectionManager
       using (gatewayBaseUrl, gatewayApiToken, desktopDeviceId)
  → Gateway URL, API token, and desktop device identity are now configured
    for the persistent VM connection and all channel operations

Logout:
  → window.cosmic.logout()
  → Main process (IPC 'auth:logout'):
    1. SAVE_SETTING:cosmicAuth:    (empty value — clears the key)
    2. SAVE_SETTING:gatewayBaseUrl:
    3. SAVE_SETTING:gatewayApiToken:
    4. Close GatewayConnectionManager socket
  → Settings cleared → renderer → authState = 'unauthenticated' → show Login Modal
```

**Local storage:** Auth data is stored in the existing `app_settings` table in `resources/user_data.db` (SQLite) under the key `cosmicAuth`. This is the same database used by the settings bridge for all desktop app settings. The database file and its encryption key (`resources/secret.key`) are both gitignored (`*.db`, `*.key`). The Fernet encryption infrastructure in `resources/database.py` is available for future use. The desktop also stores a stable `desktopDeviceId` setting in the same SQLite database. It is generated once per installation, reused across logins, and not cleared on logout.

**Auth data shape** (stored as JSON string in `app_settings.value`):

```json
{
  "apiKey": "cosmic_...",
  "userId": "uuid",
  "fullName": "User Name",
  "isPrivileged": true,
  "gatewayUrl": "https://gateway.user.example.com",
  "gatewayApiToken": "the-gateway-token",
  "vmIp": "3.137.194.119",
  "vmDns": "c2ece0ad-4b2d-4af4-ae65-1b07660550dc.thelearnchain.com",
  "authenticatedAt": 1709500000000
}
```

**Desktop app files involved:**

| File | Role |
|---|---|
| `electron/main.ts` | Supabase constants, `auth:login` / `auth:logout` IPC handlers |
| `electron/preload.ts` | Exposes `window.cosmic.login()` and `window.cosmic.logout()` to renderer |
| `src/vite-env.d.ts` | TypeScript declarations for `login` and `logout` on `Window.cosmic` |
| `src/CosmicLoginModal.tsx` | Login screen component (API key input, error display, LiquidGlass styling) |
| `src/App.tsx` | Auth gate: `authState` state machine, renders `CosmicLoginModal` when unauthenticated, passes `authData` / `onLogout` through component tree |
| `src/DynamicIsland.tsx` | Threads `authData` and `onLogout` props to Settings |
| `src/Settings.tsx` | User info card (name + "Connected to VM" + logout button), passes `cosmicAuth` to WhatsApp settings |
| `src/WhatsAppIntegrationSettings.tsx` | When `cosmicAuth` prop is present: hides Gateway URL / API Token fields, shows simplified phone-number-only UI |

**UI behavior when authenticated:** The WhatsApp integration settings page hides the Gateway URL and API Token input fields (they are auto-configured from the Supabase response). The card header changes from "Gateway connection" to "Phone number", and the user only needs to enter their allowed WhatsApp number and click "Save number". The "Save connection" button is hidden since the connection is managed by the auth system.

**Hard rules:**

1. Supabase URL and anon key are public constants — safe to commit. They only enable RLS-protected queries.
2. The user's Cosmic API key, Gateway token, and VM details are stored only in the local SQLite database — never committed to source control.
3. The `authenticate_with_api_key` function is `SECURITY DEFINER` — it runs with elevated privileges to read across tables regardless of RLS. The function itself validates the API key and checks privileges.
4. The desktop app never stores auth data in `electron-store` / APPDATA / `localStorage`. All auth persistence goes through the SQLite settings bridge pipeline.
5. On logout, all three settings keys (`cosmicAuth`, `gatewayBaseUrl`, `gatewayApiToken`) are cleared. The app returns to the login modal.
6. VM bootstrap uses `consume_bootstrap_token(...)`, not `authenticate_with_api_key(...)`. Desktop login and VM bootstrap are separate flows.
7. Bootstrap tokens are one-time, expire quickly, and must not be manually consumed before the VM uses them.
8. `public.user_vms.api_token` and the VM's `GATEWAY_LOCAL_API_TOKEN` must stay aligned. If they drift apart, desktop auth breaks.

### 3.6 Rate Limiting

Token-bucket rate limiting per session. Prevents runaway loops in the desktop app from overwhelming the backend.

```python
RATE_LIMIT_TOKENS = 60        # requests per window
RATE_LIMIT_WINDOW_SEC = 60    # window size
RATE_LIMIT_BURST = 10         # max burst above steady rate
```

### 3.7 Request Processing Flow

```python
async def handle_query(session_id: str, content: str, context: list,
                       channel_adapter: ChannelAdapter,
                       source: str = 'user', source_id: str | None = None,
                       channel: str | None = None):
    # 1. Assign session (unified across channels — see §27.2)
    if not session_id:
        session_id = generate_session_id()

    # 2. Rate limit check
    if not rate_limiter.allow(session_id):
        await channel_adapter.send({'type': 'error', 'code': 'RATE_LIMITED', ...})
        return

    # 3. Assemble context via Session Manager (see §23)
    assembled_context = await session_manager.assemble_context(
        session_id, content, context
    )
    # assembled_context = {
    #     'memories': [...],           # retrieved + ranked memories (10-12k budget)
    #     'conversation': [...],       # today's messages (pruned to fit window)
    #     'compacted_summary': '...',  # if mid-day compaction has occurred
    # }

    # 4. Sticky routing check: awaiting_reply? (see §2.2 Layer 1)
    #    Channel-scoped: only match awaiting_reply from the SAME channel.
    #    A desktop:macbook-001 awaiting_reply must not capture
    #    desktop:workstation-002 or WhatsApp messages.
    last_msg = await get_last_assistant_message(session_id, channel=channel)
    used_sticky = last_msg and last_msg.get('awaiting_reply')
    if used_sticky:
        # Skip classifier entirely — route to the model that asked
        route = last_msg['route']
    else:
        # 5. Normal classification via Model Router (see §2.2 Layer 2)
        classification = await classify_query(content, context)
        route = classification['route']

    await channel_adapter.send({
        'type': 'route_result',
        'route': route,
        'source': source,
        'source_id': source_id,
        'channel': channel,
        'classification': {'route': route, 'confidence': 1.0, 'signals': ['sticky_routing']}
            if used_sticky else classification,
    })

    # 6. Store user message BEFORE dispatch (crash safety).
    #    If the Gateway crashes between dispatch and store, the session
    #    permanently loses the user's message — the LLM dispatched a task
    #    it will never see in future context assembly. Storing first
    #    ensures the session is always consistent. If dispatch subsequently
    #    fails, the message is still in the session (correct — the user
    #    did send it), and the user will see an error and can retry.
    await store_message(session_id, 'user', content, route=route,
                        source=source, source_id=source_id, channel=channel)

    # 7. Route to appropriate backend (all receive assembled_context)
    #    source, source_id, and channel are passed through to TaskEnvelopes
    #    so the full provenance chain is preserved in all child tasks.
    #    Flag is cleared AFTER successful dispatch — if dispatch fails,
    #    the flag stays and the next message retries sticky routing.
    if route == 'opus':
        await dispatch_to_orchestrator(
            session_id, content, assembled_context, channel_adapter,
            source=source, source_id=source_id, channel=channel,
        )
    elif route == 'haiku':
        await stream_from_haiku(
            session_id, content, assembled_context, channel_adapter,
            source=source, source_id=source_id, channel=channel,
        )
    elif route == 'perplexity':
        await stream_from_perplexity(
            session_id, content, assembled_context, channel_adapter,
            source=source, source_id=source_id, channel=channel,
        )

    # 8. Clear sticky flag only after successful delivery
    if used_sticky:
        await clear_awaiting_reply(session_id, last_msg['message_id'])
```

**Message storage ordering rationale:** The user's message is stored in `sessions.db` (step 6) **before** dispatch (step 7). This is a deliberate crash-safety choice. If the Gateway crashes between dispatch and storage, the session would permanently lose the message — the LLM received a task referencing context it can never reconstruct. Storing first makes the session always-consistent: if dispatch fails after storage, the user sees an error and retries; if the Gateway crashes after storage but before dispatch, the message is preserved for the next attempt. The cost is that a message might be stored for a dispatch that immediately fails — this is acceptable because the message reflects what the user actually said, regardless of dispatch success.

**Sticky routing design rationale:** When a model (Opus, Haiku, or Perplexity) is in a conversational exchange and asks the user a question, the user's reply must go back to that same model. Without this, a short reply like "B" would be classified as low-confidence or continuation and routed to Opus, even if Haiku asked the question. The `awaiting_reply` flag is the model's own declaration that it expects a direct response — no heuristics, no guessing. See §3.8 for how the flag is detected from model output.

**Channel entry-point rule:** `handle_query(...)` is the common post-normalization path for user-originated messages regardless of channel. The Desktop adapter reaches it from the WebSocket handler. A sidecar-backed adapter such as WhatsApp reaches it from an internal REST route after the Bridge payload has been authenticated and normalized by `gateway/channels/whatsapp.py`.

**Routing nuance:** not every incoming user message becomes a Redis-dispatched orchestrator task. Only `route='opus'` results in TaskEnvelope creation and Redis dispatch to the orchestrator. `haiku` and `perplexity` remain on the Gateway's direct LLM proxy path.

### 3.8 Direct LLM Routing & Response Control Tags

For `haiku` and `perplexity` routes, the Gateway acts as a streaming proxy. For conversational `opus` responses (non-task), the orchestrator streams through the same path. This avoids orchestrator overhead for simple questions while maintaining consistent response processing.

**The `<awaiting_reply/>` control tag:** All three LLM backends (Opus, Haiku, Perplexity) receive a system prompt instruction to emit `<awaiting_reply/>` at the end of their response when they genuinely expect a direct user reply (e.g., they asked a question, presented options, or need confirmation before proceeding). The Gateway strips this tag before forwarding to the UI and sets a flag on the stored message.

**Current implementation note:** the current direct-model runtime also supports a best-effort `<handoff_opus/>` control tag for Haiku and Perplexity. If a direct model determines that a request was misrouted and actually needs orchestrator-level handling, it may emit only that tag. The Gateway intercepts it before any user-visible text, redispatches the request to Opus, and emits a desktop `task.progress` status while the escalation happens. This is a safety net, not the primary routing decision.

**System prompt instruction (included in all three model prompts):**

```
When you need the user to choose, confirm, or answer something before
you can meaningfully continue, place this tag as the very last thing
in your response — nothing after it, no trailing text, no whitespace:
<awaiting_reply/>
Do not use this for rhetorical questions or open-ended suggestions.
Only use it when you are genuinely blocked without the user's response.
```

**Why "very last thing, nothing after it"?** The Gateway strips the tag by checking the tail of the response. If the model emits text after the tag, that text would be lost or the tag detection would fail. Placing it at the absolute end makes detection a simple suffix check — no scanning needed.

**Why a control tag instead of heuristics?** Detecting questions via `?` at the end is fragile — models ask questions without question marks and use question marks rhetorically. The control tag is the model's own declaration of intent. No NLP, no regex, no false positives. The Gateway just checks for one string.

**Why a control tag instead of tool calls?** Tool call interception adds schema definition, parsing logic, and API-specific handling. A text tag is universal across all three APIs, keeps models in pure text generation mode, and requires exactly one string check in the Gateway's streaming pipeline.

```python
AWAITING_REPLY_TAG = '<awaiting_reply/>'
TAG_LEN = len(AWAITING_REPLY_TAG)          # 18 chars

class LLMStreamProcessor:
    """Base class for all LLM streaming adapters. Handles control tag
    stripping and awaiting_reply detection.

    Uses a tail buffer to prevent the control tag from leaking to the UI.
    The last TAG_LEN characters of the stream are held back from the
    client. When the stream ends, the buffer is checked for the tag.
    If present, it's stripped silently. If not, it's flushed as a
    final chunk. The user never sees the tag — not even briefly."""

    async def process_stream(self, stream, session_id: str, route: str,
                              ws: WebSocket):
        full_response = ''
        tail_buf = ''                      # holds back last TAG_LEN chars

        async for chunk in stream:
            full_response += chunk

            # Accumulate in buffer, only flush the safe prefix
            tail_buf += chunk
            if len(tail_buf) > TAG_LEN:
                safe = tail_buf[:-TAG_LEN] # guaranteed tag-free
                tail_buf = tail_buf[-TAG_LEN:]
                await ws.send_json({
                    'type': 'response.chunk',
                    'content': safe,
                    'done': False,
                })

        # Stream ended. Check if tail buffer contains the tag.
        awaiting_reply = tail_buf.rstrip().endswith(AWAITING_REPLY_TAG)
        if awaiting_reply:
            # Strip tag, flush any remaining clean text
            remainder = tail_buf.rstrip().removesuffix(AWAITING_REPLY_TAG)
        else:
            remainder = tail_buf

        if remainder:
            await ws.send_json({
                'type': 'response.chunk',
                'content': remainder,
                'done': False,
            })

        display_text = full_response.rstrip().removesuffix(AWAITING_REPLY_TAG).rstrip()

        # Send complete response
        await ws.send_json({
            'type': 'response.complete',
            'route': route,
            'content': display_text,
            'awaiting_reply': awaiting_reply,
        })

        # Store in session history with metadata
        await store_message(
            session_id, 'assistant', display_text,
            route=route, awaiting_reply=awaiting_reply,
        )

class HaikuAdapter(LLMStreamProcessor):
    """Streams responses from Anthropic Claude Haiku 4.5 API."""
    async def stream(self, query: str, context: list, session_id: str,
                      ws: WebSocket):
        stream = self._call_haiku_streaming(query, context)
        await self.process_stream(stream, session_id, 'haiku', ws)

class PerplexityAdapter(LLMStreamProcessor):
    """Streams responses from Perplexity API with citations."""
    async def stream(self, query: str, context: list, session_id: str,
                      ws: WebSocket):
        stream = self._call_perplexity_streaming(query, context)
        await self.process_stream(stream, session_id, 'perplexity', ws)
```

Each adapter inherits from `LLMStreamProcessor` which handles:
- Streaming response tokens to the desktop app via WebSocket
- Stripping the `<awaiting_reply/>` control tag from display text
- Setting the `awaiting_reply` flag on the stored message for sticky routing (§3.7)
- Storing the complete response in session history after completion
- API errors are handled gracefully (retry with exponential backoff, then error to client)

### 3.9 Orchestrator Dispatch Path

For `opus` routes, the Gateway creates a TaskEnvelope and dispatches it to the orchestrator via Redis. It then consumes events from `streams:events` and forwards them to the originating channel in real time.

```python
async def dispatch_to_orchestrator(session_id, content, context,
                                    channel_adapter: ChannelAdapter,
                                    source: str = 'user',
                                    source_id: str | None = None,
                                    channel: str | None = None):
    task_id = generate_task_id()

    # Source-to-priority mapping: user=high, webhook=normal, cron/heartbeat=low
    priority = SOURCE_PRIORITY_MAP.get(source, 'normal')

    task = TaskEnvelope(
        task_id=task_id,
        task_list_id=session_id,
        session_id=session_id,
        sender='cosmic/gateway:1.0.0',
        recipient='cosmic/orchestrator:1.0.0',
        intent='orchestrator.process',
        input={'query': content, 'conversation_context': context},
        idempotency_key=str(uuid4()),
        priority=priority,
        source=source,
        source_id=source_id,
        channel=channel,
        ...
    )
    await dispatch(task, redis)

    # Register the concrete channel for this task (fast path for response routing)
    active_task_channels[task_id] = channel

    await channel_adapter.send({'type': 'task.created', 'task_id': task_id, ...},
                               channel=channel)
    # Events are forwarded by the gateway event consumer (see §3.10)
```

**Note:** The Gateway dispatches to the orchestrator here. Credential resolution happens later — when the orchestrator decomposes the task into sub-tasks and dispatches to agents, it calls the Gateway's internal credential endpoint to resolve short-lived access tokens (see §22.3).

### 3.10 Gateway Event Consumer

The Gateway runs its own consumer group on `streams:events`, independent of the orchestrator's consumer group. This means both the Gateway and the orchestrator receive all events independently.

```python
# On startup: create gateway consumer group
await redis.xgroup_create('streams:events', 'gateway', id='0', mkstream=True)

async def gateway_event_consumer():
    """Forwards task events to the correct channel adapter."""
    while True:
        events = await redis.xreadgroup(
            groupname='gateway',
            consumername=GATEWAY_INSTANCE_ID,
            streams={'streams:events': '>'},
            count=10,
            block=500,
        )
        for stream, messages in events:
            for msg_id, data in messages:
                event = EventEnvelope.model_validate_json(data['event'])
                # Route to the concrete channel that originated this task.
                # active_task_channels is a fast-path cache. On reconnect or
                # Gateway restart, the authoritative route is the persisted
                # channel string associated with the task/session state.
                channel = active_task_channels.get(event.task_id) or lookup_task_channel(event.task_id)
                adapter = channel_registry.get_adapter(channel)
                if adapter:
                    await adapter.send(format_event_for_client(event), channel=channel)
                await redis.xack('streams:events', 'gateway', msg_id)
```

**Two consumer groups on `streams:events`:**

| Consumer Group | Purpose |
|---|---|
| `orchestrator` | Routing logic, retry decisions, DLQ handling, deferred recovery |
| `gateway` | Forward events to desktop app via WebSocket |

Both groups receive every event independently. Neither blocks the other.

### 3.11 Session Management

The Gateway maintains session state in SQLite. The user experiences one persistent assistant, implemented as one shared **daily** session across all channels. WebSocket reconnects do not create new sessions. Daily sessions reset at 4AM with forced compaction. In addition to the canonical SQLite store, the Gateway maintains a **derived append-only daily transcript** in Markdown under `logs/sessions/` for human-readable archival and export. SQLite remains the source of truth for live session state, routing, and replay. See §23 for the full Session & Memory Management specification.

```sql
-- gateway/sessions.db
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,                 -- always same value in single-user-per-VM deployment; exists for future extensibility
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    compaction_count INTEGER DEFAULT 0,    -- how many times compaction ran
    compacted_summary TEXT,                -- mid-day compaction summary (if any)
    metadata_json TEXT
);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,               -- 'user', 'assistant', 'system'
    content TEXT,
    route TEXT,              -- 'opus', 'haiku', 'perplexity'
    channel TEXT,            -- originating channel: 'desktop:desk_a1b2c3', 'whatsapp:+1234567890', 'telegram:chat_123', etc.
    task_id TEXT,            -- null for non-opus messages
    awaiting_reply BOOLEAN DEFAULT FALSE,  -- model expects direct reply (sticky routing)
    created_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX idx_messages_session ON messages(session_id, created_at);
CREATE INDEX idx_messages_channel ON messages(session_id, channel, created_at);
```

### 3.12 Task User Input Relay

When a background task needs user input, the orchestrator publishes a request to the `user_input:requests` Redis stream. The Gateway consumes these and surfaces them to the UI. When the user replies, the Gateway publishes to `user_input:replies` for the orchestrator to pick up.

This is a separate mechanism from conversational `awaiting_reply` sticky routing (§3.7). Sticky routing handles synchronous conversation flow (model asks, user replies inline). The user input relay handles asynchronous task input — the user might be mid-conversation about something else entirely when a background task needs their attention.

```python
# On startup: create consumer group (idempotent — ignores if already exists)
try:
    await redis.xgroup_create('user_input:requests', 'gateway', id='0', mkstream=True)
except ResponseError as e:
    if 'BUSYGROUP' not in str(e):
        raise

async def user_input_consumer():
    """Consumes task input requests from orchestrator, surfaces to UI.

    Only acks after successful channel delivery. If the target
    desktop device is offline, the message stays in Redis's Pending
    Entries List (PEL). On reconnect, deliver_pending_inputs(channel)
    drains the PEL for that concrete channel first."""
    while True:
        entries = await redis.xreadgroup(
            groupname='gateway',
            consumername=GATEWAY_INSTANCE_ID,
            streams={'user_input:requests': '>'},
            count=5,
            block=1000,
        )
        for stream, messages in entries:
            for msg_id, data in messages:
                request = json.loads(data['payload'])
                channel = request.get('channel') or lookup_task_channel(request['task_id'])
                adapter = channel_registry.get_adapter(channel)
                if adapter:
                    await adapter.send({
                        'type': 'task.input_required',
                        **request,
                    }, channel=channel)
                    # Only ack AFTER successful delivery
                    await redis.xack('user_input:requests', 'gateway', msg_id)
                # else: no ack — stays in PEL, redelivered on reconnect

async def deliver_pending_inputs(channel: str):
    """Called when a concrete channel reconnects (for example,
    desktop:desk_a1b2c3). Drains the Pending Entries List so the user
    sees any task input requests they missed while offline."""
    pending = await redis.xreadgroup(
        groupname='gateway',
        consumername=GATEWAY_INSTANCE_ID,
        streams={'user_input:requests': '0'},  # '0' = read pending entries
        count=50,
    )
    adapter = channel_registry.get_adapter(channel)
    if not adapter:
        return
    for stream, messages in pending:
        for msg_id, data in messages:
            if not data:      # already acked between check and read
                continue
            request = json.loads(data['payload'])
            request_channel = request.get('channel') or lookup_task_channel(request['task_id'])
            if request_channel != channel:
                continue
            await adapter.send({
                'type': 'task.input_required',
                **request,
            }, channel=channel)
            await redis.xack('user_input:requests', 'gateway', msg_id)

async def handle_user_input_reply(input_request_id: str, task_id: str,
                                    content: str):
    """User replied to a task input request. Publish to replies stream."""
    await redis.xadd('user_input:replies', {
        'payload': json.dumps({
            'input_request_id': input_request_id,
            'task_id': task_id,
            'content': content,
            'timestamp': utcnow().isoformat(),
        }),
    })
```

**Two separate user-facing interaction patterns:**

| Pattern | Trigger | Mechanism | UX |
|---|---|---|---|
| **Conversational reply** | Model emits `<awaiting_reply/>` tag | `awaiting_reply` flag on message → sticky routing (§3.7) | Inline in conversation — user just types normally |
| **Task input request** | Orchestrator publishes to `user_input:requests` | Redis stream → Gateway → WebSocket `task.input_required` | Separate UI element (notification/modal/sidebar) — not inline chat |

**Design rationale:** Conversational replies are part of the chat flow — the user sees a question and answers it naturally. Task input requests are interruptions from background work — they should appear differently in the UI (e.g., a notification badge, a separate panel) so the user knows this is a background task asking, not the current conversation.

### 3.13 Health & Readiness

```python
@app.get('/health')
async def health():
    return {'status': 'healthy'}

@app.get('/health/ready')
async def readiness():
    checks = {
        'redis': await check_redis(),
        'model_router': await check_model_router(),
        'sqlite': check_sqlite(),
    }
    all_ok = all(v == 'ok' for v in checks.values())
    return {'ready': all_ok, 'checks': checks}
```

---

## 4. Agent ID Design

Agent IDs follow the `{org}/{name}:{version}` pattern (same as HuggingFace, Docker Hub, model registries).

### 4.1 Format

```
cosmic/research-agent:1.0.0
cosmic/docs-agent:2.1.0
cosmic/orchestrator:1.0.0

# External LLM models referenced inside agents
anthropic/claude-sonnet-4-6
ollama/qwen2.5:7b
```

### 4.2 Agent ID vs Display Name

| Field | Value / Rule |
|---|---|
| `agent_id` | `cosmic/research-agent:1.0.0` |
| `display_name` | `Research Agent` |
| Purpose of ID | Machine identifier — used in code, DB, Redis streams, logs, artifact manifests. Never changes after creation. |
| Purpose of Name | Human-readable label for UI and docs. Can be rebranded freely without touching code. |
| Foreign key rule | `agent_id` is the FK everywhere. Task envelopes, event envelopes, heartbeats, artifacts — all reference `agent_id` string only. |

**Why version in the ID?** Lets you run `research-agent:1.0.0` and `research-agent:2.0.0` side-by-side during canary rollout. Orchestrator routing config pins exact versions — no silent breaking changes from `latest`. Audit logs are unambiguous: you always know exactly which version produced an artifact.

### 4.3 Pydantic Definition

```python
class AgentID(BaseModel):
    org: str        # 'cosmic', 'anthropic'
    name: str       # 'research-agent', 'docs-agent'
    version: str    # semver '1.0.0' or date '2024-11-20'

    def __str__(self):
        return f'{self.org}/{self.name}:{self.version}'
```

---

## 5. Folder Structure

Every agent has the same shape on disk. This makes orchestrator tooling fully generic — it never needs custom logic per agent.

### 5.1 Top-Level Project Layout

```
cosmic-agents/
├── shared/                     # Shared contracts (single source of truth)
│   ├── contracts.py            # All Pydantic envelope models
│   ├── registry.py             # Agent registry client
│   ├── redis_bus.py            # Redis Streams transport layer
│   ├── redis_client.py         # Redis client factory (decode_responses=True)
│   ├── sqlite_client.py        # SQLite connection factory (WAL mode, busy_timeout — §7.2a)
│   ├── auth.py                 # HMAC signing + verification
│   ├── time_utils.py           # UTC datetime normalization
│   ├── events.py               # Atomic seq allocation
│   ├── idempotency.py          # Idempotency enforcement
│   ├── step_plan.py            # StepPlan universal tool (§32.2)
│   ├── memory_tools.py         # MemoryRead + MemoryWrite universal tools (§32.5)
│   ├── leader.py               # Leader election + fencing
│   ├── artifact_security.py    # Path allowlist + integrity checks
│   ├── model_specs.py          # Loader/lookup helpers for shared/model_specs.json
│   ├── model_specs.json        # Global metered-model registry: SDK, base URL, limits, pricing
│   ├── usage.py                # Shared metered-call helpers: ids, normalization, cost, events
│   └── config.py               # TTL tunables, constants
│
├── agents/                     # One folder per agent
│   ├── orchestrator/
│   │   ├── planner.py          # Task Planner — plan creation, execution, synthesis (§31)
│   │   ├── suspension.py       # Suspension lifecycle & GC — expire stale suspensions (§16.3a)
│   │   └── store/data/
│   │       └── task_ledger.db  # Plans, plan_steps, tasks tables (§31.1)
│   ├── research_agent/
│   ├── docs_agent/
│   ├── diagram_agent/
│   ├── browser_agent/          # Browser automation via Playwright (see §29)
│   ├── system_agent/           # OS-level automation — files, processes, clipboard (see §29)
│   ├── cli_agent/              # Alpha CLI agent — full system access, sleeping (see §30)
│   └── _template/              # Copy this to create a new agent
│
├── gateway/                    # FastAPI gateway service
│   ├── main.py                 # Gateway entry point + WebSocket handler
│   ├── circuit_breaker.py      # Per-API circuit breaker for external LLM APIs (§2.6a)
│   ├── adapters/               # Direct LLM adapters (Haiku, Perplexity)
│   │   ├── haiku.py
│   │   └── perplexity.py
│   ├── artifacts/              # Attachment/artifact persistence helpers
│   │   ├── __init__.py
│   │   └── store.py            # Canonical ArtifactStore implementation
│   ├── channels/               # Channel Adapter Registry (see §27)
│   │   ├── base.py             # ChannelAdapter base class (interface contract)
│   │   ├── registry.py         # Adapter registry + channel → session routing
│   │   ├── routes.py           # Channel management + bridge intake routes (/channels/*, /internal/channels/*)
│   │   ├── desktop.py          # Desktop app adapter (WebSocket)
│   │   ├── whatsapp.py         # WhatsApp adapter (Baileys via bridge / WhatsApp Business API)
│   │   ├── telegram.py         # Telegram adapter (Bot API / grammY)
│   │   ├── slack.py            # Slack adapter (Bolt / Events API)
│   │   ├── discord.py          # Discord adapter (discord.py)
│   │   └── cli.py              # CLI agent channel adapter (internal pipe)
│   ├── delivery/               # Durable channel-delivery queue helpers
│   │   ├── __init__.py
│   │   └── queue_store.py      # Canonical DeliveryQueueStore implementation
│   ├── credentials/            # Credential Manager module (see §22)
│   │   ├── manager.py          # Core credential logic (resolve, refresh, revoke)
│   │   ├── oauth.py            # OAuth PKCE flow handlers per provider
│   │   ├── providers.py        # Provider adapter registry (Google, GitHub, etc.)
│   │   ├── encryption.py       # Token encryption/decryption (Fernet envelope)
│   │   └── routes.py           # OAuth + internal credential API routes
│   ├── memory/                 # Internal cosmic-memory integration surface
│   │   ├── __init__.py
│   │   ├── client.py           # Canonical CosmicMemoryClient implementation
│   │   └── routes.py           # Internal memory/session proxy routes
│   ├── prompts/                # READ-ONLY runtime prompt assets
│   │   ├── session.py
│   │   ├── session_compaction_system.md
│   │   └── session_rollover_summary_system.md
│   ├── routing/                # Routing helpers / audit storage
│   │   ├── __init__.py
│   │   ├── audit_store.py      # Durable inspection log for final routing decisions (§3.4b)
│   │   └── router_client.py    # Internal model-router client
│   ├── scheduler/              # Scheduler module — Crons + Heartbeats (see §25)
│   │   ├── __init__.py
│   │   ├── store.py            # Canonical SchedulerStore implementation
│   │   └── scheduler.db        # Cron definitions + heartbeat config (SQLite)
│   ├── webhooks/               # Webhook Handler module (see §26)
│   │   ├── handler.py          # Receive, verify, convert to TaskEnvelope
│   │   ├── providers.py        # Per-provider signature verification (Gmail, GitHub, Slack, Jira)
│   │   ├── routes.py           # Webhook endpoints (/webhooks/{webhook_id})
│   │   └── webhooks.db         # Webhook registrations (SQLite)
│   ├── hooks/                  # Hooks Engine module (see §28)
│   │   ├── engine.py           # Hook registry, lifecycle event dispatcher
│   │   └── definitions.py     # Built-in hook definitions (startup, shutdown, session_reset, etc.)
│   ├── session/                # Session Manager module (see §23)
│   │   ├── compaction.py       # Conversation compaction logic (summarization)
│   │   ├── summary.py          # Daily rollover summarization helpers
│   │   └── __init__.py
│   ├── user_input.py           # Task input relay: consume user_input:requests, publish replies (§3.12)
│   ├── artifact_store.py       # Compatibility shim -> gateway/artifacts/store.py
│   ├── delivery_queue_store.py # Compatibility shim -> gateway/delivery/queue_store.py
│   ├── memory_client.py        # Compatibility shim -> gateway/memory/client.py
│   ├── memory_routes.py        # Compatibility shim -> gateway/memory/routes.py
│   ├── router_client.py        # Compatibility shim -> gateway/routing/router_client.py
│   ├── routing_audit_store.py  # Compatibility shim -> gateway/routing/audit_store.py
│   ├── scheduler_store.py      # Compatibility shim -> gateway/scheduler/store.py
│   ├── sessions.db             # Session + message storage
│   ├── routing_audit.db        # Route decision audit store (SQLite)
│   ├── artifacts.db            # Inbound attachment metadata store (SQLite)
│   ├── delivery_queue.db       # Durable user-visible delivery queue (SQLite)
│   ├── credentials.db          # Credential store (accounts, tokens, audit — see §22)
│   └── usage.db                # Token/cost telemetry ledger (SQLite)
│
├── bridges/                    # Sidecar-backed adapter/integration services
│   └── whatsapp_bridge/        # WhatsApp bridge process for Baileys-based adapter (§27.6, §27.7)
│       ├── package.json        # Bridge runtime/dependencies
│       ├── src/                # Bridge code: socket lifecycle, reconnects, internal API/callbacks
│       ├── store/              # PERSISTENT. Channel auth/session/device state
│       │   └── auth/           # e.g. Baileys multi-file auth state
│       └── runtime/            # EPHEMERAL. Logs, cache, pidfiles, temp files
│
├── memory/                     # Logical canonical memory tree (.md source of truth; owned by internal cosmic-memory service when enabled)
│   ├── sessions/               # Compacted daily session summaries
│   │   ├── 2025-01-15.md
│   │   └── 2025-01-16.md
│   ├── agent_notes/            # Agent-saved memories (synced from agents/*/store/)
│   │   ├── research_agent/
│   │   │   └── learnings.md
│   │   └── docs_agent/
│   │       └── learnings.md
│   ├── user_data/              # Indexed user data (emails, files, etc.)
│   │   ├── emails/
│   │   └── files/
│   └── tasks/                  # Task result summaries (retrievable, not in main session)
│       └── <task_id>.md
│
├── qdrant_data/                # Qdrant local storage (vector index — rebuilt from memory/)
│
├── model_router/               # Model Router classifier service
│   ├── main.py                 # FastAPI server (port 8742)
│   ├── classifier.py           # Classification logic + rule enforcement
│   └── config.py               # Groq API config, thresholds
│
├── registry/                   # SQLite agent registry
│   └── registry.db
│
├── logs/                       # Persistent derived/archival logs
│   ├── sessions/               # Derived append-only daily session transcripts (.md)
│   │   ├── 2025-01-15.md
│   │   └── 2025-01-16.md
│   └── events/                 # Archived task events (.jsonl per task — see §12.8)
│       └── <task_id>.jsonl
│
├── runs/                       # Runtime artifacts (gitignored)
│   └── artifacts/
│       └── <task_id>/
│
├── bootstrap.py                # First-run Linux VM bootstrap helper (deps, venv, bridge setup)
├── requirements.txt            # Python runtime dependencies for Gateway, Router, Orchestrator, Agents
├── requirements-dev.txt        # Dev/test/lint dependencies (-r requirements.txt + extras)
│
├── supervisord.conf            # Process management
└── docker-compose.yml          # Container deployment
    # NOTE: agents/*/store/ and bridges/*/store/ directories MUST be
    # mapped to persistent volumes in docker-compose.yml. runtime/
    # directories are ephemeral.
    # gateway/credentials.db MUST also be on a persistent volume —
    # it contains encrypted refresh tokens and account bindings.
    # gateway/scheduler/scheduler.db MUST be on a persistent volume —
    # it contains cron definitions and heartbeat configuration.
    # gateway/webhooks/webhooks.db MUST be on a persistent volume —
    # it contains webhook registrations.
    # memory/ and qdrant_data/ MUST be on persistent volumes —
    # they contain the system's long-term memory.
    # logs/sessions/ MUST be on a persistent volume —
    # it contains derived daily session transcript archives.
    # logs/events/ MUST be on a persistent volume —
    # it contains archived task event history (see §12.8).
```

### 5.1a Sidecar Bridge Layout

Most channel adapters are simple in-process modules inside `gateway/channels/`. A **bridge** is only introduced when the platform SDK, transport, or auth model does not fit cleanly inside the Python Gateway process.

**Use a bridge when one or more of these are true:**

- The required platform library is not Python-native or is operationally better in another runtime (for example, Baileys in Node.js).
- The platform needs a long-lived socket/session process with its own reconnect and device-state lifecycle.
- The platform persists non-OAuth runtime state (session blobs, device registrations, encrypted local auth files) that should be isolated from Gateway code.

**Canonical bridge shape:**

```text
bridges/<name>_bridge/
├── src/                        # Bridge implementation
├── store/                      # PERSISTENT. Runtime auth/session/device state
│   └── auth/
└── runtime/                    # EPHEMERAL. Logs, cache, temp files
```

**Hard rules:**

1. The Gateway-facing adapter still lives in `gateway/channels/<platform>.py`. The bridge is an implementation detail behind that adapter, not a replacement for it.
2. `store/` is for channel/runtime auth state only. It survives restarts and deploys. It must be on a persistent volume in containers, or on persistent disk in VM deployments.
3. `runtime/` is ephemeral. It may be deleted on restart without data loss.
4. Bridges stay thin: connection lifecycle, provider-specific auth/session handling, inbound event capture, outbound send. They do **not** own sessions, model routing, memory, credentials, orchestration, or response policy.
5. Bridge traffic is internal-only. Expose it on localhost or a Unix socket, authenticate it, and never treat it like a public webhook surface.

### 5.1b Bootstrap + Dependency Manifests

COSMIC uses **one Python runtime environment for the entire Python backend** and **one dependency environment per non-Python bridge runtime**.

**Top-level Python dependency files:**

- `bootstrap.py`: First-run provisioning helper for Linux VM deployments. It prepares Python, `pip`, `venv`, installs backend Python dependencies, can fetch a per-VM env bundle from Supabase using a one-time bootstrap token, and can provision sidecar runtimes such as the WhatsApp bridge.
- `requirements.txt`: Runtime dependency manifest for all Python services in this repository: Gateway, Model Router, Orchestrator, and agents.
- `requirements-dev.txt`: Development-only extension of `requirements.txt` for linting, tests, and local verification. Production process managers do **not** install or depend on it.

**Runtime separation rules:**

1. Use a single top-level Python virtual environment for the backend, for example `./.venv`. Do **not** create separate Python virtual environments for `gateway/`, `scheduler/`, `credentials/`, or individual adapters.
2. Each bridge owns its own non-Python runtime manifest inside the bridge directory. For Node.js bridges, this means `package.json`, `package-lock.json`, and bridge-local `node_modules/`.
3. Separate environments are created by **runtime/ecosystem boundary**, not by feature folder. Today that means:
   - Python backend → one shared `.venv`
   - `bridges/whatsapp_bridge/` → its own Node.js dependencies
4. Introduce an additional Python virtual environment only if a real incompatibility forces it (for example, conflicting interpreter or binary dependency constraints across separately deployed Python services). Do not pre-optimize for this.

**Bootstrap script role:**

`bootstrap.py` is a deployment helper, not a long-running service. It may execute package-manager and shell commands via Python `subprocess` to prepare the machine. It should remain:

- idempotent where practical
- subcommand-based rather than one monolithic setup function
- limited to provisioning/setup concerns, not application runtime behavior
- capable of reconciling placeholders in existing env files without clobbering live non-placeholder secrets

**Canonical commands:**

```bash
python bootstrap.py doctor
python bootstrap.py fetch-bootstrap-env
python bootstrap.py setup-python
python bootstrap.py setup-whatsapp-bridge
python bootstrap.py sync-env
python bootstrap.py bootstrap
python bootstrap.py provision-vm
python bootstrap.py provision-vm --skip-edge
```

**Current command meanings:**

- `doctor`: Read-only prerequisite check. Reports Python, `pip`, `venv`, requirements-file, Node/npm, and bridge-manifest state.
- `fetch-bootstrap-env`: Requires `COSMIC_BOOTSTRAP_TOKEN` or `--bootstrap-token`; fetches the one-time Supabase bootstrap payload and materializes the repo env files.
- `setup-python`: Ensures Python `3.10+`, `pip`, and `venv`; creates/updates `.venv`; installs `requirements.txt`.
- `setup-whatsapp-bridge`: Installs the Node.js dependency set for `bridges/whatsapp_bridge/`.
- `sync-env`: Appends missing keys from committed env templates and, on Linux VMs, also updates existing `/etc/cosmic` env files. If a bootstrap token is present, it first refreshes the repo env files from Supabase and then reconciles placeholder values in `/etc/cosmic`.
- `bootstrap`: Runs the first-pass provisioning flow for the backend VM by materializing env files (optionally from Supabase), then executing both Python and bridge setup.
- `provision-vm`: Full production bare-VM flow for a host whose DNS and ingress are already ready. Materializes envs, installs Python and bridge deps, installs systemd units, starts the backend target, and invokes Caddy/TLS edge setup.
- `provision-vm --skip-edge`: Full bare-VM flow for an already-networked host. Materializes envs, installs Python and bridge deps, installs systemd units, and starts the backend target without forcing Caddy/TLS edge setup.

**Memory-specific invocation:** long-term memory is opt-in at bootstrap time. To provision the internal memory service on a VM, the operator must already have a local clone of the `cosmic-memory` repo on that machine and pass `--memory-repo-dir <path>` when running `bootstrap.py`. Example:

```bash
export COSMIC_BOOTSTRAP_TOKEN='<one-time bootstrap token>'
python3 bootstrap.py --memory-repo-dir ~/cosmic-memory provision-vm
```

Without `--memory-repo-dir`, bootstrap does not install the `cosmic-memory` package or service, does not create `/etc/cosmic/memory.env`, and leaves `COSMIC_MEMORY_URL` blank/disabled in the Gateway env.

**Operational note:** `bootstrap.py` is intended to be the first backend command after clone on a Linux VM image that already has a callable Python interpreter. In the current production flow, operators mint a one-time Supabase bootstrap token and export it as `COSMIC_BOOTSTRAP_TOKEN` before running `bootstrap.py provision-vm`. Use `--skip-edge` only when the public DNS record and/or inbound `80/443` are not ready yet, or when you intentionally want an internal-only/non-TLS rollout first. If the base image has no Python at all, a minimal cloud-init/scripted OS bootstrap may install Python first and then hand off to `bootstrap.py`.

### 5.2 Per-Agent Folder (Every Agent Is Identical in Shape)

```
agents/research_agent/
├── agent_card.yaml             # WHO I am + WHAT I can do (capability declaration)
├── __main__.py                 # Entry point: python -m agents.research_agent
├── agent.py                    # Core agent logic
│
├── prompts/                    # READ-ONLY at runtime. Version controlled.
│   ├── system.md               # System prompt (what this agent is)
│   └── policies.md             # Rules, constraints, tool usage policies
│
├── skills/
│   └── SKILLS.md               # Domain knowledge, techniques, reference
│
├── schemas/                    # Machine-readable API contracts
│   ├── intents/                # JSON schema per intent
│   │   ├── research.topic.input.json
│   │   ├── research.topic.output.json
│   │   └── research.recall_session.input.json
│   └── events/                 # Event payload schemas
│       ├── task.accepted.json
│       └── artifact.added.json
│
├── store/                      # PERSISTENT. Survives restarts. Backed up.
│   ├── learnings.md            # Accumulated agent knowledge & facts (see §12)
│   └── data/                   # Agent-managed storage — schema is agent's own
│       └── (agent defines its own DB, files, indexes here)
│
└── runtime/                    # EPHEMERAL. Gitignored. Recreated on restart.
    ├── state.db                # In-flight task state (suspension serialization)
    ├── cache/                  # Cached fetches, embeddings
    └── logs/                   # Agent-level structured logs
```

### 5.3 Critical Runtime Rules

- `prompts/` and `skills/` → **READ-ONLY** at runtime. Agent cannot modify its own instructions.
- `schemas/` → **READ-ONLY** at runtime. Only changed via versioned deploys.
- **Universal tools** (StepPlan, MemoryRead, MemoryWrite) → **INJECTED** by the agent runtime at task start. Not declared in `agent_card.yaml`. All agents get them. See §32.
- `store/` → **PERSISTENT.** Survives restarts. Must be on a persistent volume in containerized deployments. Backed up.
- `store/learnings.md` → Agent's accumulated knowledge and facts, persisted across sessions. Updated by the agent after tasks. Each agent owns its own learnings — no cross-agent reads.
- `store/data/` → **Agent-managed storage.** Each agent defines its own database schema, session tables, indexes, and data files here. No uniform schema is imposed — a research agent might track source credibility scores while a docs agent tracks edit history. Must be on a persistent volume.
- `runtime/` → **EPHEMERAL.** Gitignored. Recreated on restart. Used for in-flight state, caches, and logs only.
- `runs/artifacts/<task_id>/` → **PER-TASK isolation.** Never shared across tasks.
- Any agent that makes a metered LLM or embedding API call **must** emit one usage event to `POST /internal/usage/log` using the Usage Ledger contract in §3.4a. This is a runtime obligation, not an optional capability.
- Model SDK choice, provider `base_url`, context-window limits, output limits, and token pricing are **NOT** defined per agent. Resolve them from `shared/model_specs.json`, not from `agent_card.yaml` and not from agent-local constants.
- Orchestrator **NEVER** silently rewrites prompts live. Use versioned rollout + rollback.
- Track sha256 hashes of prompt files in registry so audits detect drift.

### 5.3a Agent Memory Contract

Every agent/subagent created from this document must follow the same memory boundary rules:

1. **Agent-private memory lives in the agent folder.**
   - durable private notes: `agents/<agent>/store/learnings.md`
   - agent-owned structured history/state: `agents/<agent>/store/data/`
2. **Shared long-term memory lives outside the agent folder.**
   - it is owned by the same-VM internal `cosmic-memory` service
   - agents never read/write shared memory files directly
   - agents access it only through Gateway-injected universal tools (`MemoryRead`, `MemoryWrite`) and internal HTTP
3. **Live session continuity is not agent-owned.**
   - daily session state, active working set, compaction packet, carry-forward packet, and deterministic revisit live in the Gateway/session layer
   - agents may receive this context in `TaskEnvelope.input` or through explicit internal session APIs, but they do not own or mutate the canonical session ledger
4. **Shared memory is for high-signal durable knowledge, not raw execution exhaust.**
   - good writes: reusable facts, stable preferences, validated task summaries, artifact pointers, curated agent notes
   - bad writes: raw chain-of-thought, raw tool payloads, temporary progress chatter, repeated intermediate drafts
5. **Large outputs must spill to artifacts, not memory text.**
   - store bulky bodies under `runs/artifacts/<task_id>/`
   - write a compact `artifact_pointer` memory if the content must be retrievable later
   - keep the prompt-visible memory text small and source-oriented
6. **Exact prior context uses revisit/replay paths, not semantic search alone.**
   - use recall intents for agent-private history
   - use task replay / task notebooks for task continuity
   - use `/internal/session/*` deterministic revisit when the exact prior turn history matters
7. **Task execution remains isolated from the main conversation.**
   - final user-visible outcome enters the shared session
   - deep task state remains in task memory, per-agent storage, task notebooks, and artifacts

This contract is intentionally strict so every new agent follows the same mental model: private store for agent-owned history, shared memory for durable system-wide retrieval, Gateway/session layer for live continuity.

---

## 6. Agent Card (`agent_card.yaml`)

The Agent Card is the machine-readable capability declaration registered at startup. The orchestrator reads it to populate the registry.

**Agent Card vs Model Card:** An Agent Card is written for machines — it describes the agent's runtime interface (intents, schemas, SLA, health contract). A Model Card is written for humans — it describes the underlying AI model (evals, training data, limitations). The orchestrator only reads Agent Cards.

### 6.1 Full `agent_card.yaml`

```yaml
# agents/research_agent/agent_card.yaml
agent_id: cosmic/research-agent:1.0.0
display_name: Research Agent
description: >
  Specialist agent for web research, document fetching,
  citation extraction, and image discovery.

intents:
  - name: research.topic
    description: Deep-research a topic and return citations + summary
    input_schema: schemas/intents/research.topic.input.json
    output_schema: schemas/intents/research.topic.output.json
    timeout_sec: 180

  - name: research.find_image
    description: Find a relevant image for a given query
    input_schema: schemas/intents/research.find_image.input.json
    output_schema: schemas/intents/research.find_image.output.json
    timeout_sec: 60

  - name: research.recall_session
    description: Recall what happened in a previous session for this agent
    input_schema: schemas/intents/research.recall_session.input.json
    output_schema: schemas/intents/research.recall_session.output.json
    timeout_sec: 30

# No auth_requirements — research agent uses its own tools (web_search,
# web_fetch), not user OAuth credentials. Only declare auth_requirements
# for intents that call external provider APIs on behalf of the user.

artifact_types:
  - web_page
  - pdf
  - image
  - citation_pack

policies:
  network_access: true
  writable_paths:
    - runs/artifacts/research_agent
    - agents/research_agent/store
    - agents/research_agent/runtime
  tool_access:
    - web_search
    - web_fetch
    - file_write
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  intent_authorization:
    research.topic: [cosmic/orchestrator:1.0.0]
    research.find_image: [cosmic/orchestrator:1.0.0]
    research.recall_session: [cosmic/orchestrator:1.0.0]

sla:
  max_concurrency: 4
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: 180    # longest possible task — used for XAUTOCLAIM tuning
  health_endpoint: /health
  retry_policy:
    max_attempts: 3
    backoff: exponential
    backoff_base_sec: 2         # 2s, 4s, 8s
    backoff_max_sec: 60
    retryable_codes:
      - TIMEOUT
      - NETWORK_ERROR
      - RATE_LIMITED
    non_retryable_codes:
      - INVALID_INPUT
      - AUTH_ERROR
      - SCHEMA_VIOLATION

stream_key: streams:cosmic/research-agent:1.0.0

version_info:
  semver: 1.0.0
  released_at: 2025-01-01
  deprecated_at: null
  remove_after: null
  changelog: CHANGELOG.md
```

**Tool access** is declared per agent under `policies.tool_access`. Agents can invoke tools via any mechanism — MCP servers, direct SDK calls, REST clients, shell commands — the contract only cares *what* tools are permitted, not *how* they're called. **Note:** Universal tools (StepPlan, MemoryRead, MemoryWrite) are NOT listed in `policies.tool_access` — they are injected by the agent runtime and available to all agents automatically (see §32).

**Important:** `agent_card.yaml` is not the source of truth for model runtime metadata. It does not carry provider SDK choice, `base_url`, context window, output limits, or token pricing. Those belong in the shared model registry at `shared/model_specs.json` (see §7.2c).

**Session recall intents** like `research.recall_session` allow the orchestrator to query agents about past sessions. For example, when a user asks "explain the last edits we did on this doc," the orchestrator sends a `docs.recall_session` task to the docs agent, which queries its own `store/data/` storage and returns the relevant history. Each agent is the authority on its own past work.

### 6.2 `auth_requirements` — Provider Credential Declaration

Agents that call external provider APIs on behalf of the user declare `auth_requirements` per intent. The orchestrator reads this at dispatch time to determine whether credential resolution is needed (see §22.3).

```yaml
# agents/docs_agent/agent_card.yaml (excerpt)
agent_id: cosmic/docs-agent:2.1.0
display_name: Docs Agent

intents:
  - name: docs.edit
    description: Edit a Google Doc section
    input_schema: schemas/intents/docs.edit.input.json
    output_schema: schemas/intents/docs.edit.output.json
    timeout_sec: 120

  - name: docs.create
    description: Create a new Google Doc
    input_schema: schemas/intents/docs.create.input.json
    output_schema: schemas/intents/docs.create.output.json
    timeout_sec: 60

  - name: docs.resolve_resource
    description: Search for a document by name across connected accounts
    input_schema: schemas/intents/docs.resolve_resource.input.json
    output_schema: schemas/intents/docs.resolve_resource.output.json
    timeout_sec: 30

  - name: docs.recall_session
    description: Recall what happened in a previous session
    input_schema: schemas/intents/docs.recall_session.input.json
    output_schema: schemas/intents/docs.recall_session.output.json
    timeout_sec: 30

auth_requirements:
  docs.edit:
    provider: google
    scopes:
      - https://www.googleapis.com/auth/documents
  docs.create:
    provider: google
    scopes:
      - https://www.googleapis.com/auth/documents
      - https://www.googleapis.com/auth/drive.file
  docs.resolve_resource:
    provider: google
    scopes:
      - https://www.googleapis.com/auth/drive.metadata.readonly
  # docs.recall_session has no auth_requirements — it only reads
  # from the agent's own store/data/, no provider API calls needed.
```

**Rules:**

- Only intents that call external provider APIs declare `auth_requirements`.
- Intents that only use agent-local tools or read from `store/` do not declare them.
- The orchestrator checks `auth_requirements` before dispatch. If present, it resolves a `credential_ref` and short-lived access token via the Gateway's internal credential endpoint and includes them in `TaskEnvelope.input.auth` (see §7.3, §22.3).
- If the required scopes are not granted by the user's connected account, the orchestrator escalates via `user.input_required` asking the user to re-consent with the needed scopes.

---

## 7. Agent Runtime Contract v1.6

Seven models every agent speaks. Defined once in `shared/contracts.py`. Transport-agnostic — the same shapes work whether moving through Redis Streams or HTTP.

### 7.1 Shared Time Utilities

All datetime operations use these helpers. **Never use `datetime.utcnow()` anywhere** — it is deprecated in Python 3.12 and produces naive datetimes that crash when mixed with timezone-aware values from APIs.

```python
# shared/time_utils.py
from datetime import datetime, timezone

def utcnow() -> datetime:
    """Always returns a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)

def to_utc_aware(dt: datetime) -> datetime:
    """Normalize any datetime to timezone-aware UTC.
    - Already aware → convert to UTC.
    - Naive → assume UTC, attach tzinfo.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def deadline_remaining_sec(deadline_ts: datetime) -> float:
    """Safe deadline arithmetic — normalizes both sides before subtracting."""
    return (to_utc_aware(deadline_ts) - utcnow()).total_seconds()
```

### 7.2 Redis Client Configuration

All Redis connections use `decode_responses=True` to ensure consistent string handling throughout the system. No manual `.decode()` calls anywhere.

```python
# shared/redis_client.py
import redis.asyncio as redis

async def get_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get('REDIS_HOST', 'localhost'),
        port=int(os.environ.get('REDIS_PORT', 6379)),
        decode_responses=True,      # all reads return str, not bytes
    )
```

### 7.2a SQLite Connection Hardening

All SQLite connections use WAL mode and a generous busy timeout. Without WAL, SQLite serializes all writes with `SQLITE_BUSY` under contention. Under `asyncio` concurrency — where the orchestrator's event consumer, deferred check loop, plan executor, and crash recovery all write to `task_ledger.db` — this will produce `database is locked` errors. WAL mode allows concurrent readers with a single writer, and `busy_timeout` makes writers wait instead of failing immediately.

```python
# shared/sqlite_client.py
import sqlite3
import aiosqlite

SQLITE_PRAGMAS = [
    'PRAGMA journal_mode=WAL',         # concurrent reads + single-writer without SQLITE_BUSY
    'PRAGMA busy_timeout=5000',        # wait up to 5s for lock instead of failing immediately
    'PRAGMA synchronous=NORMAL',       # safe with WAL — fsync on checkpoint, not every commit
    'PRAGMA wal_autocheckpoint=1000',  # checkpoint every 1000 pages (default)
    'PRAGMA foreign_keys=ON',          # enforce FK constraints
]

def connect_sync(db_path: str) -> sqlite3.Connection:
    """Synchronous SQLite connection with hardened pragmas.
    Used by agent store/data/ databases and the registry."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for pragma in SQLITE_PRAGMAS:
        conn.execute(pragma)
    return conn

async def connect_async(db_path: str) -> aiosqlite.Connection:
    """Async SQLite connection with hardened pragmas.
    Used by the Gateway (sessions, credentials, scheduler, webhooks)
    and the orchestrator (task_ledger)."""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    for pragma in SQLITE_PRAGMAS:
        await conn.execute(pragma)
    return conn
```

**Why these pragmas:**

| Pragma | Purpose |
|---|---|
| `journal_mode=WAL` | Write-Ahead Logging. Readers never block writers, writers never block readers. Essential for async concurrency. |
| `busy_timeout=5000` | If a write lock is held, wait up to 5 seconds before raising `SQLITE_BUSY`. Prevents spurious failures during concurrent plan execution. |
| `synchronous=NORMAL` | Safe with WAL — data is fsynced on checkpoint, not every commit. Reduces write latency without risking corruption. |
| `foreign_keys=ON` | Enforces FK constraints. SQLite disables this by default — every connection must enable it explicitly. |

**Critical rule:** Every SQLite database in the system — `sessions.db`, `credentials.db`, `scheduler.db`, `webhooks.db`, `task_ledger.db`, `registry.db`, and all `agents/*/store/data/*.db` — **must** be opened via these helper functions. Direct `sqlite3.connect()` calls without pragmas are a bug.

### 7.2b Usage Logging Contract

Usage logging is part of the runtime contract for any agent that initiates a metered LLM or embedding
API call. It is not optional, and it is not defined separately by each agent.

- The agent code path that initiates the outbound metered call generates `llm_call_id`.
- The agent records `llm_call_placed_at` at the moment the outbound call is initiated.
- After the provider returns (or fails), the agent emits exactly one usage event for that call to
  `POST /internal/usage/log`.
- Retries of the usage log write must reuse the same `llm_call_id` so the Gateway can deduplicate
  idempotently.
- Agents do not write `gateway/usage.db` directly. The Gateway is the only writer to the Usage
  Ledger.
- The request body and validation rules are defined in §3.4a `Internal Usage Event Contract`.
- If the agent computes `estimated_cost_usd`, context-headroom telemetry, or other model-limit
  analytics, it resolves provider/model metadata from `shared/model_specs.json` (see §7.2c).

**Canonical shared helper (`shared/usage.py`):**

To avoid per-agent drift, metered call paths SHOULD use a small shared helper instead of
hand-rolling `llm_call_id`, token normalization, cost/headroom math, and usage-event payload
assembly independently in each component.

Recommended responsibilities:

- `begin_metered_call(...)` — allocate `llm_call_id`, capture `llm_call_placed_at`, and start a
  local monotonic timer for latency measurement
- `normalize_usage(model_key, raw_usage)` — use `shared/model_specs.json` `token_field_map` to
  return canonical `input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, and
  `reasoning_tokens`
- `build_usage_event(...)` — construct the exact `/internal/usage/log` request body, including
  correlation fields, provider/model/usage kind, success/failure, latency, and optional
  cost/headroom telemetry
- `post_usage_event(...)` — write the event through the Gateway with idempotent retry that reuses
  the same `llm_call_id`

Recommended call flow:

1. Call `begin_metered_call(...)` immediately before the outbound provider request.
2. After the provider returns or fails, build exactly one final usage event for that call.
3. Post that event once the provider outcome is known.
4. If the usage-log write is retried, reuse the same `llm_call_id`.

This helper is a caller-side convenience layer. The authoritative runtime contract remains §3.4a
and `POST /internal/usage/log`.

Gateway direct LLM adapters may persist locally via `gateway/usage.py` instead of self-calling
HTTP, but they SHOULD reuse the same shared normalization and event-building logic.

**Optional implementation note (LangChain / LangGraph):**

If an agent is built with LangChain or LangGraph, it is acceptable to derive usage from the
returned `AIMessage` before posting the COSMIC usage event:

- read `AIMessage.usage_metadata` first
- then fall back to `AIMessage.response_metadata['token_usage']` or
  `AIMessage.response_metadata['usage']`
- normalize `prompt_tokens` / input tokens, `completion_tokens` / output tokens, `total_tokens`,
  and, when present, cached-input and reasoning token details

This is only a suggested extraction pattern. The hard requirement is that the final event sent to
`POST /internal/usage/log` matches the COSMIC usage contract in §3.4a.

**Canonical local telemetry guidance:**

When a code path computes local token/cost/headroom telemetry before posting the usage event, or
for task/session-local observability:

- Normalize provider payloads into one canonical shape:
  `input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`.
- Use `shared/model_specs.json` `token_field_map` to interpret provider-specific field names.
- Equivalent aliases are fallback names for the same metric, never additive fields. If an SDK
  wrapper exposes the same count in multiple places, normalize to one value rather than summing
  aliases.
- If `total_tokens` is absent, fall back to `input_tokens + output_tokens`.
- Clamp `cached_input_tokens <= input_tokens` and `reasoning_tokens <= output_tokens`.
- Emit one usage event per outbound metered call. Per-task or per-session totals are local
  aggregates only; they do not replace per-call Usage Ledger events.
- Context-pressure telemetry is based on the **peak single-call prompt/input tokens observed**
  during the task. Do not use cumulative tokens across multiple calls as a context-window measure.

This requirement applies to:

- specialist agents calling LLM/chat APIs
- specialist agents calling embedding APIs
- agent runtime helper paths that make metered model calls on behalf of the agent

It does not apply to:

- agents that complete a task without making any metered model/API call
- purely local deterministic work such as SQLite queries, filesystem operations, schema validation,
  hashing, or Redis operations

### 7.2c Global Model Spec Registry

All metered model definitions live in one shared declarative registry:

- file: `shared/model_specs.json`
- optional helper loaders: `shared/model_specs.py`, `shared/usage.py`
- ownership: shared runtime layer, not any individual agent

This registry is the single source of truth for:

- `provider`
- `model`
- `sdk` family used by code to talk to that model
- default provider `base_url`
- `usage_kind`
- context-window and output-token limits
- recommended reserve/headroom
- token pricing
- token-field normalization hints

The registry is used by:

- Gateway direct LLM adapters
- Model Router
- orchestrator model calls
- Session Manager compaction and embedding paths
- specialist agents that make metered LLM or embedding calls
- usage/cost estimation code

It is **not** duplicated into `agent_card.yaml`. The agent card declares capabilities and intent
contracts; `shared/model_specs.json` declares runtime model metadata.

**Recommended shape:**

```json
{
  "version": 1,
  "models": {
    "anthropic:claude-opus": {
      "provider": "anthropic",
      "model": "claude-opus",
      "sdk": "anthropic",
      "base_url": "https://api.anthropic.com/v1",
      "usage_kind": "chat_completion",
      "context_window_tokens": 200000,
      "max_output_tokens": 32000,
      "recommended_headroom_reserve_tokens": 12000,
      "pricing": {
        "input_per_1m_usd": null,
        "cached_input_per_1m_usd": null,
        "output_per_1m_usd": null
      },
      "capabilities": {
        "supports_cached_input_tokens": true,
        "supports_reasoning_tokens": false,
        "supports_streaming": true
      },
      "token_field_map": {
        "prompt_tokens": ["input_tokens", "prompt_tokens"],
        "completion_tokens": ["output_tokens", "completion_tokens"],
        "total_tokens": ["total_tokens"],
        "cached_tokens": ["cache_read_input_tokens", "cached_tokens"],
        "reasoning_tokens": ["reasoning_tokens"]
      },
      "status": "active"
    },
    "groq:openai/gpt-oss-20b": {
      "provider": "groq",
      "model": "openai/gpt-oss-20b",
      "sdk": "openai_compatible",
      "base_url": "https://api.groq.com/openai/v1",
      "usage_kind": "classifier",
      "context_window_tokens": null,
      "max_output_tokens": null,
      "recommended_headroom_reserve_tokens": 0,
      "pricing": {
        "input_per_1m_usd": null,
        "cached_input_per_1m_usd": null,
        "output_per_1m_usd": null
      },
      "capabilities": {
        "supports_cached_input_tokens": false,
        "supports_reasoning_tokens": false,
        "supports_streaming": false
      },
      "token_field_map": {
        "prompt_tokens": ["prompt_tokens", "input_tokens"],
        "completion_tokens": ["completion_tokens", "output_tokens"],
        "total_tokens": ["total_tokens"],
        "cached_tokens": [],
        "reasoning_tokens": []
      },
      "status": "active"
    },
    "anthropic:claude-haiku-4-5": {
      "provider": "anthropic",
      "model": "claude-haiku-4-5",
      "sdk": "anthropic",
      "base_url": "https://api.anthropic.com",
      "usage_kind": "messages",
      "context_window_tokens": null,
      "max_output_tokens": null,
      "recommended_headroom_reserve_tokens": 8000,
      "pricing": {
        "input_per_1m_usd": null,
        "cached_input_per_1m_usd": null,
        "output_per_1m_usd": null
      },
      "capabilities": {
        "supports_cached_input_tokens": false,
        "supports_reasoning_tokens": false,
        "supports_streaming": true
      },
      "token_field_map": {
        "prompt_tokens": ["promptTokenCount", "input_tokens", "prompt_tokens"],
        "completion_tokens": ["candidatesTokenCount", "output_tokens", "completion_tokens"],
        "total_tokens": ["totalTokenCount", "total_tokens"],
        "cached_tokens": [],
        "reasoning_tokens": []
      },
      "status": "active"
    },
    "openrouter:qwen3-embedding-8b": {
      "provider": "openrouter",
      "model": "qwen3-embedding-8b",
      "sdk": "openai_compatible",
      "base_url": "https://openrouter.ai/api/v1",
      "usage_kind": "embedding",
      "context_window_tokens": null,
      "max_output_tokens": 0,
      "recommended_headroom_reserve_tokens": 0,
      "pricing": {
        "input_per_1m_usd": null,
        "cached_input_per_1m_usd": null,
        "output_per_1m_usd": null
      },
      "capabilities": {
        "supports_cached_input_tokens": false,
        "supports_reasoning_tokens": false,
        "supports_streaming": false
      },
      "token_field_map": {
        "prompt_tokens": ["prompt_tokens", "input_tokens", "total_tokens"],
        "completion_tokens": [],
        "total_tokens": ["total_tokens"],
        "cached_tokens": [],
        "reasoning_tokens": []
      },
      "status": "active"
    }
  }
}
```

**Rules:**

- The lookup key is `{provider}:{model}`.
- Every metered model actually used anywhere in the system must have an entry.
- `sdk` identifies the client family the code path should use, for example `anthropic`,
  `google_genai`, or `openai_compatible`.
- `base_url` is the default provider endpoint for that model family. Environment overrides are
  allowed for testing or self-hosted proxies, but the registry remains the canonical default.
- Provider-reported token counts are authoritative when present. `token_field_map` exists for
  normalization and fallback extraction across SDK/provider response shapes.
- Callers SHOULD consume this registry through `shared/model_specs.py` and `shared/usage.py`
  rather than re-implementing token normalization, cost estimation, or headroom math separately in
  each component.
- Pricing fields may be `null` when not yet tracked, but the entry must still exist if the model is
  used for metered work or context-budget calculations.
- A shared helper SHOULD normalize provider usage payloads into:
  `input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, and `reasoning_tokens`.
- If the provider omits `total_tokens`, the shared helper SHOULD fall back to
  `input_tokens + output_tokens`.
- Cost telemetry SHOULD use:
  `((input_tokens - cached_input_tokens) * input_per_1m_usd + cached_input_tokens * cached_input_per_1m_usd + output_tokens * output_per_1m_usd) / 1_000_000`
  when pricing metadata is available.
- Context telemetry SHOULD use `peak_input_tokens` for the task's heaviest single metered call:
  - `prompt_context_left_tokens = max(context_window_tokens - peak_input_tokens, 0)`
  - `safe_headroom_left_tokens = max((context_window_tokens - recommended_headroom_reserve_tokens) - peak_input_tokens, 0)`
- If pricing or context-limit fields are `null`, raw token usage still logs normally, but the
  corresponding local cost/headroom telemetry stays unset rather than inventing values.

### 7.3 TaskEnvelope (Bidirectional)

The standard message format for all orchestrator ↔ agent communication. Created by the orchestrator for forward tasks (§7) and by agents for reverse tasks (§13.1 — clarify, delegate, refresh_credential, etc.).

```python
from pydantic import field_validator

class TaskEnvelope(BaseModel):
    task_id: str                                    # 'tsk_abc123'
    task_list_id: str                               # Groups related tasks
    parent_task_id: str | None                      # For subtask trees
    session_id: str | None                          # Links task to session context
    sender: str                                     # 'cosmic/orchestrator:1.0.0'
    recipient: str                                  # 'cosmic/research-agent:1.0.0'
    intent: str                                     # 'research.topic'
    input: dict                                     # Intent-specific payload
    input_artifacts: list[ArtifactManifest] = []    # Files passed TO the agent
    idempotency_key: str                            # UUID — required, enforced via SETNX
    deadline_ts: datetime | None                    # Timezone-aware or naive UTC
    priority: Literal['high', 'normal', 'low'] = 'normal'
    leader_epoch: int | None = None                 # Fencing token set by orchestrator
    signature: str                                  # HMAC-SHA256 of canonical payload
    contract_version: str = CURRENT_WRITE_VERSION   # dynamic — see §21
    created_at: datetime = Field(default_factory=utcnow)
    source: Literal['user', 'cron', 'webhook', 'heartbeat', 'hook', 'agent'] = 'user'
    source_id: str | None = None                    # Origin identifier: 'cron_morning_email', 'wh_gmail_001'
    channel: str | None = None                      # Platform: 'desktop:desk_a1b2c3', 'whatsapp:+1234', 'telegram:chat_123', 'cli'

    @field_validator('contract_version')
    @classmethod
    def check_contract_version(cls, v: str) -> str:
        """Validates on deserialization (inbound from streams). Accepts any
        version in the read window. Outbound validation uses
        validate_outbound_version() at dispatch time — see §21.

        NOTE: load_task_envelope() uses model_construct() to bypass this
        validator when loading from storage, because stored envelopes may
        predate the current acceptance window. prepare_for_redispatch()
        stamps CURRENT_WRITE_VERSION before dispatch — which passes both
        this validator and validate_outbound_version()."""
        if v not in ACCEPTED_READ_VERSIONS:
            raise ValueError(
                f'Unacceptable contract version: {v}. '
                f'Accepted: {ACCEPTED_READ_VERSIONS}'
            )
        return v
```

| Field | Notes |
|---|---|
| `task_id` | Unique task identifier. Format: `tsk_abc123` |
| `task_list_id` | Groups related tasks in a session |
| `parent_task_id` | For subtask trees — links child to parent |
| `session_id` | Links task to session context. Agents use this to load/store session data in their own `store/data/` |
| `input_artifacts` | Typed file references passed TO the agent — verified by sha256 before read |
| `idempotency_key` | UUID. Enforced via SETNX before every execution (see §14) |
| `leader_epoch` | Fencing token. Agents reject tasks from stale epochs (see §16) |
| `signature` | HMAC-SHA256 of full canonical payload. Stripped before passing to agent logic (see §15) |
| `contract_version` | Set from `CURRENT_WRITE_VERSION` config — not a hardcoded literal. Validated on read by `validate_incoming_version()` (see §21) |
| `source` | Origin type: `user` (human message), `cron` (scheduled job), `webhook` (external system event), `heartbeat` (periodic timer), `hook` (internal state change), `agent` (agent-initiated reverse task). Defaults to `user`. Propagated to child tasks by the orchestrator so the full provenance chain is preserved. |
| `source_id` | Identifies the specific origin instance: `cron_morning_email`, `wh_gmail_001`, `hook_session_reset`. Null for direct user messages. Propagated to child tasks. Used for log filtering, metrics per-source, and audit trails. |
| `channel` | Platform/interface the task originated from or should deliver results to: `desktop:<device_id>`, `whatsapp:+1234567890`, `telegram:chat_123`, `slack:C0123456`, `cli`. Null for internal events (hooks, agent-initiated) that don't need user-facing delivery. The Gateway uses this field to route responses back to the correct channel adapter. Format is usually `{platform}:{channel_id}`. Bare `desktop` is reserved as a delivery alias meaning "the configured primary desktop device" for non-message sources such as cron, heartbeat, and webhook delivery. |

#### Reverse-task conventions (specialist → orchestrator)

When a specialist needs help from the orchestrator, it sends a normal `TaskEnvelope` with **`source='agent'`** and **`recipient='cosmic/orchestrator:1.0.0'`**. Reverse tasks are part of the same signed task protocol as forward dispatches; they are **not** a side channel.

- `parent_task_id` SHOULD point at the currently running specialist task that will suspend and later resume.
- `sender` is the specialist agent id; the orchestrator verifies the reverse task using that agent's signing secret.
- For `intent='orchestrator.delegate'`, `input` SHOULD include:
  - `target_intent`: the sibling specialist capability needed
  - `target_input`: exact input object for that sibling task
  - optional `target_agent_id`: only when a specific registered agent must be used
  - optional `resume_payload`: compact specialist-owned state needed when the suspended task resumes
- Specialists SHOULD describe the needed capability/output, not assume direct registry access or sibling topology. Live sibling discovery remains the orchestrator's job.

#### `input.auth` Convention (Credential Passthrough)

When an intent has `auth_requirements` declared in `agent_card.yaml` (see §6.2), the orchestrator resolves credentials at dispatch time and places them in `input.auth`. This is a convention on the `input: dict` field, not a separate model field.

```python
# Example: TaskEnvelope.input for a docs.edit task
{
    'query': 'Add a conclusion section to the project proposal',
    'doc_id': 'abc123',
    'auth': {
        'credential_ref': 'cred_9f3a...',       # opaque reference for audit
        'access_token': 'ya29.a0AfH6SM...',      # short-lived (5-15 min)
        'provider': 'google',
        'scopes': ['https://www.googleapis.com/auth/documents'],
        'expires_at': '2025-01-15T10:05:00+00:00',
    },
}
```

**Hard rules for `input.auth`:**

- **Never** included in EventEnvelopes, artifacts, logs, `store/`, `runtime/`, or `learnings.md`.
- The agent base class extracts `input.auth` before passing `input` to the agent's execute method and exposes it via `self.auth` — a runtime-only context that is never serialized (see §12.6).
- `input.auth` **is** covered by the HMAC signature (it is part of `input: dict`), ensuring it cannot be tampered with in transit.
- If `input.auth` is absent, the agent must not attempt any provider API calls requiring user credentials.
- If the access token expires mid-task, the agent suspends and requests a refresh via `orchestrator.refresh_credential` reverse task (see §13.1, §22.5).

### 7.4 EventEnvelope (Agent → Orchestrator)

How agents report progress. Agents emit these asynchronously as work proceeds.

```python
class EventEnvelope(BaseModel):
    task_id: str
    agent_id: str                   # who emitted this
    event_type: Literal[
        'task.accepted',            # agent received and validated task
        'task.progress',            # intermediate progress update
        'task.suspended',           # agent waiting for reverse-task reply
        'task.resumed',             # agent continuing after suspension
        'task.deferred',            # NON-TERMINAL: another instance executing — orchestrator owns recovery
        'artifact.added',           # artifact produced, manifest in payload
        'task.completed',           # TERMINAL: success — closes task state
        'task.failed',              # TERMINAL: failure — closes task state
        'task.dlq',                 # TERMINAL: dead letter queue — closes task state
        'task.rejected',            # NON-TERMINAL: stale epoch — triggers redrive
    ]
    seq: int                        # monotonic PER-TASK via redis.incr()
    payload: dict                   # event-type-specific data
    emitted_at: datetime
    contract_version: str = CURRENT_WRITE_VERSION

    @field_validator('contract_version')
    @classmethod
    def check_contract_version(cls, v: str) -> str:
        """Inbound validation — accepts any version in the read window."""
        if v not in ACCEPTED_READ_VERSIONS:
            raise ValueError(
                f'Unacceptable contract version: {v}. '
                f'Accepted: {ACCEPTED_READ_VERSIONS}'
            )
        return v
```

**Terminal vs Non-Terminal events:**

```python
TERMINAL_EVENTS = {'task.completed', 'task.failed', 'task.dlq'}
NON_TERMINAL_EVENTS = {'task.accepted', 'task.progress', 'task.suspended',
                       'task.resumed', 'task.deferred', 'artifact.added',
                       'task.rejected'}
```

- **TERMINAL** events expire the seq key, close task state in consumers.
- **NON-TERMINAL** events leave the seq key alive and task state open.
- `task.rejected` is explicitly **NON-TERMINAL** — the task will be redriven by the orchestrator with a current epoch.

**`seq` is per-task, not per-agent.** It resets to 1 for every new `task_id`. Sort by `(task_id, seq)` to reconstruct exact execution order during replay or debugging.

### 7.5 AgentResult (Returned on `task.completed` or `task.failed`)

```python
class AgentResult(BaseModel):
    status: Literal['completed', 'failed']
    output: dict
    artifacts: list[ArtifactManifest]
    error: AgentError | None

class AgentError(BaseModel):
    code: str               # 'TIMEOUT', 'NETWORK_ERROR', 'DEADLINE_EXCEEDED'
    retryable: bool
    message: str
    next_action: str | None # 'retry', 'escalate', 'skip'
```

### 7.6 TaskInProgress (Idempotency Sentinel)

Returned when another instance is currently executing this task. The orchestrator understands this as "check back later" — not an error, not a requeue.

```python
class TaskInProgress(BaseModel):
    task_id: str
    idempotency_key: str
    executing_since: datetime
    check_after_sec: int        # orchestrator should retry lookup after this delay

# Union return type for idempotency enforcement
ExecutionResult = AgentResult | TaskInProgress
```

### 7.7 Heartbeat (Agent → Registry, every N seconds)

```python
class Heartbeat(BaseModel):
    agent_id: str
    instance_id: str        # unique per worker process
    healthy: bool
    current_load: int       # active tasks right now
    max_concurrency: int
    heartbeat_ttl_sec: int  # self-describing — used by router
    last_seen: datetime
```

Router only dispatches to agents where `healthy == True`, `current_load < max_concurrency`, and `last_seen` is within `heartbeat_ttl_sec`.

### 7.8 ArtifactManifest (What Was Produced)

```python
class ArtifactManifest(BaseModel):
    artifact_id: str        # 'art_xyz789'
    task_id: str
    mime: str               # 'application/pdf', 'image/png'
    sha256: str             # integrity hash
    path: str               # 'runs/artifacts/tsk_abc123/paper.pdf'
    source_url: str | None
    created_by_agent: str   # agent_id
    created_at: datetime
    kind: Literal['input', 'output', 'intermediate'] = 'output'
    audience: Literal['deliverable', 'supporting', 'debug'] = 'deliverable'
```

#### User-Facing Produced Artifact Delivery

`ArtifactManifest` is also the canonical bridge between specialist outputs and user-deliverable files.

When a specialist creates a user-facing file:

- the file must be persisted under the normal task-scoped artifact tree
- the producing `AgentResult.artifacts` must contain the relevant `ArtifactManifest`
- only artifacts with `audience='deliverable'` should flow into default user-facing `produced_artifacts`
- the orchestrator should preserve a compact `produced_artifacts` list on the parent turn
- Gateway should persist those compact artifact descriptors in assistant-message metadata
- client surfaces may render those as downloadable output-file cards
- later turns may look those deliverable artifacts up again and either re-surface them or re-bind them into future child tasks via `TaskEnvelope.input_artifacts`

Supporting notes:

- `audience='supporting'` is for internal byproducts such as scrape payloads, page markdown, parse-bundle manifests, and other artifacts that help the specialist do its work but are not the user's requested deliverable
- `audience='debug'` is for diagnostics and should stay hidden from normal user-facing output flows unless explicitly requested
- this prevents specialists from dumping research/supporting files next to the actual deliverable file in the default UI

The client must **not** receive raw filesystem paths as a UX contract. Download/open flows should resolve through Gateway-owned artifact delivery endpoints or channel-native delivery paths.

---

## 8. Redis Streams: Queue Design

Redis Streams provide consumer groups, acknowledgement, replay, and crash recovery without adding a new service. Priority is implemented via stream tiers.

### 8.1 Stream & Key Naming

```
# Task streams: PER-AGENT (shared across all instances via consumer group)
streams:cosmic/research-agent:1.0.0:high
streams:cosmic/research-agent:1.0.0:normal
streams:cosmic/research-agent:1.0.0:low

# Liveness keys: PER-INSTANCE (each worker writes independently)
registry:cosmic/research-agent:1.0.0:research-1
registry:cosmic/research-agent:1.0.0:research-2

# Intent index: Redis Set mapping intent → agent_ids
intent:research.topic            → {cosmic/research-agent:1.0.0}
intent:research.find_image       → {cosmic/research-agent:1.0.0}

# Shared event stream — orchestrator AND gateway listen here
streams:events

# Orchestrator's own input streams (for reverse tasks from agents)
streams:cosmic/orchestrator:1.0.0:high
streams:cosmic/orchestrator:1.0.0:normal

# Dead letter queue
streams:dlq

# Capability updates (push-based discovery)
streams:capability.updates

# Idempotency keys
idempotency:{idempotency_key}           # execution lock
idempotency:result:{idempotency_key}    # stored terminal result

# Seq counters
event_seq:{task_id}                     # atomic per-task sequence allocation

# Leader election
orchestrator:leader                     # current leader instance_id
orchestrator:epoch                      # monotonic fencing token
```

**Critical distinction:** Liveness keys are per-instance (each worker reports its own load independently). Stream keys are per-agent (all workers consume from the same stream via a shared consumer group). Redis distributes messages across consumers in the group and tracks pending-per-consumer for crash recovery.

### 8.2 Consumer Group Setup

```python
async def join_consumer_group(agent_id: str, instance_id: str, redis):
    for priority in ['high', 'normal', 'low']:
        stream = f'streams:{agent_id}:{priority}'
        try:
            await redis.xgroup_create(
                stream, 'workers',
                id='0',             # start from beginning
                mkstream=True       # create stream if not exists
            )
        except ResponseError as e:
            if 'BUSYGROUP' not in str(e):
                raise               # group already exists — expected, ignore
```

### 8.3 Dispatch (Orchestrator → Redis)

```python
# shared/redis_bus.py

def load_task_envelope(task_id: str) -> TaskEnvelope:
    """Load a stored envelope from the orchestrator's task ledger.
    Uses model_construct() to bypass contract_version validation —
    stored envelopes may predate the current acceptance window.
    prepare_for_redispatch() will stamp CURRENT_WRITE_VERSION before dispatch."""
    row = db.execute(
        'SELECT envelope_json FROM tasks WHERE task_id = ?', [task_id]
    ).fetchone()
    data = json.loads(row['envelope_json'])
    return TaskEnvelope.model_construct(**data)

def prepare_for_redispatch(
    task: TaskEnvelope, new_epoch: int, secrets: dict
) -> TaskEnvelope:
    """Up-convert an older in-flight envelope to current write version.
    Called before every redispatch — recover_orphaned, task.rejected redrive,
    and deferred recovery. Preserves idempotency_key (no re-execution risk).
    MUST re-sign: contract_version and leader_epoch are covered by the HMAC,
    so mutating them without re-signing will fail verify_incoming()."""
    task.contract_version = CURRENT_WRITE_VERSION
    task.leader_epoch = new_epoch
    task.signature = sign_task(task, secrets[task.recipient])
    return task

async def dispatch(task: TaskEnvelope, redis: Redis):
    validate_outbound_version(task)     # enforce write-version at dispatch
    stream = f'streams:{task.recipient}:{task.priority}'

    # Backpressure check (see §8.5)
    length = await redis.xlen(stream)
    if length > STREAM_MAXLEN:
        raise BackpressureError(
            f'Stream {stream} at capacity ({length}/{STREAM_MAXLEN})'
        )

    await redis.xadd(
        stream,
        {'envelope': task.model_dump_json()},
        maxlen=STREAM_MAXLEN_APPROX,    # approximate trim as safety net
    )
```

### 8.4 Worker Loop (Agent Consuming from Redis)

```python
STREAMS = {
    'high':   f'streams:{AGENT_ID}:high',
    'normal': f'streams:{AGENT_ID}:normal',
    'low':    f'streams:{AGENT_ID}:low',
}

# XAUTOCLAIM min_idle_time MUST exceed max_task_duration_sec from agent_card.yaml.
# If your longest intent is 180s, set this to at least 360s (2x).
# Too low → healthy workers mid-task get their messages reclaimed → duplicate pressure.
CLAIM_MIN_IDLE_MS = self.max_task_duration_sec * 2 * 1000

async def run(self):
    while True:
        # Crash recovery: reclaim messages from dead consumers
        for priority in ['high', 'normal', 'low']:
            claimed = await redis.xautoclaim(
                STREAMS[priority], 'workers', self.instance_id,
                min_idle_time=CLAIM_MIN_IDLE_MS,
                start_id='0-0'
            )
            if claimed[1]:
                for msg in claimed[1]:
                    await self._process_message(msg, STREAMS[priority])

        # Normal consumption: priority ordering with aging (see §18)
        consume_order = await self._priority_order_with_aging()
        for priority in consume_order:
            messages = await redis.xreadgroup(
                groupname='workers',
                consumername=self.instance_id,
                streams={STREAMS[priority]: '>'},
                count=1,
                block=100,              # ms — fall through to next tier
            )
            if messages:
                task = TaskEnvelope.model_validate_json(
                    messages[0]['envelope']
                )
                await self.handle(task, messages[0].id, STREAMS[priority])
                break                   # restart priority loop after handling
```

**Crash recovery is free.** If a worker crashes mid-task, the message stays unacknowledged. On restart, `XAUTOCLAIM` reclaims stale messages. After N failed reclaims, the message moves to `streams:dlq` for manual inspection.

**XAUTOCLAIM tuning rule:** `min_idle_time` must be **at least 2x** the agent's `max_task_duration_sec` from its `agent_card.yaml`. If `research.topic` has `timeout_sec: 180`, set `min_idle_time` to at least `360000` ms. Otherwise a healthy worker running a long task will have its message reclaimed, creating duplicate processing pressure.

### 8.5 Backpressure

If the orchestrator dispatches faster than agents consume, Redis streams grow unbounded. Backpressure prevents this.

```python
# shared/config.py
STREAM_MAXLEN = 10000           # hard cap — dispatch rejects above this
STREAM_MAXLEN_APPROX = 12000    # approximate trim on XADD (allows brief overshoot)
EVENTS_STREAM_MAXLEN = 50000    # trim streams:events (events are archived to disk before eviction)
MEMORY_WRITE_MAX_PER_HOUR = 50  # per-agent memory write rate limit (prevents runaway flooding)
```

**Two-layer protection:**

| Layer | Mechanism | Behavior |
|---|---|---|
| **Dispatch-time check** | `XLEN` before `XADD` | If stream length exceeds `STREAM_MAXLEN`, dispatch raises `BackpressureError`. Orchestrator retries after backoff. |
| **XADD approximate trim** | `maxlen=STREAM_MAXLEN_APPROX` on every `XADD` | Safety net — Redis trims the stream approximately to this length. Uses `~` (approximate) to avoid O(N) trim on every write. |

**Gateway surfaces backpressure as HTTP 503** (Service Unavailable) with a `Retry-After` header. The desktop app shows a "system busy" state and retries automatically.

```python
# Orchestrator handles BackpressureError
try:
    await dispatch(task, redis)
except BackpressureError:
    logger.warning(f'Backpressure on {task.recipient}, retrying after delay')
    await asyncio.sleep(backoff_sec)
    # Re-attempt or queue internally
```

---

## 9. Process Management: supervisord

supervisord manages long-running COSMIC processes in containerized deployments: gateway, model_router, bridge services, and agent workers. Each supervised process restarts automatically on crash.

| Tool | When to Use |
|---|---|
| supervisord | User-space. Simple INI config. Natural fit inside Docker containers. Install via pip. |
| systemd | OS-level init. Linux only. Better for bare-metal/VM. Journald log integration. Use on COSMIC lab Dell servers. |
| **Rule** | Container → supervisord. Bare-metal lab server → systemd. Never mix within one deployment target. |

**Provisioning before process start:** dependency/bootstrap work happens **before** `systemd` or `supervisord` takes over. On VM deployments, the expected first backend command after cloning is:

```bash
export COSMIC_BOOTSTRAP_TOKEN='<one-time bootstrap token>'
python bootstrap.py provision-vm
```

That flow assumes the VM already has a public DNS record and open inbound `80/443`, fetches the per-VM env payload from Supabase, installs the shared Python virtual environment from `requirements.txt`, installs bridge-local dependencies such as `bridges/whatsapp_bridge/package.json`, syncs `/etc/cosmic/*.env`, configures the Caddy/TLS edge, and then hands long-running service lifecycle to the selected process manager (`systemd` on VMs, `supervisord` in containers). If DNS or ingress are not ready yet, operators may temporarily use `python bootstrap.py provision-vm --skip-edge` and finish the edge later with `python bootstrap.py setup-edge`.

For containerized deployments, the image/Dockerfile should bake in the equivalent of `bootstrap.py setup-python` and any required bridge dependency installation during build time. Do not rely on an interactive bootstrap step at container start.

### 9.1 `supervisord.conf`

```ini
[supervisord]
nodaemon=true
logfile=/var/log/supervisord.log

[program:gateway]
command=uvicorn gateway.main:app --host 0.0.0.0 --port 8080
autostart=true
autorestart=true
environment=GATEWAY_INTERNAL_TOKEN='<internal-service-token>',GATEWAY_SIGNING_SECRET='<gateway-signing-secret>',CREDENTIAL_ENCRYPTION_KEY='<fernet-key>',OPENROUTER_API_KEY='<openrouter-key>',EMBEDDING_MODEL='qwen3-embedding-8b',QDRANT_PATH='./qdrant_data',MEMORY_STORE_PATH='./memory',SESSION_RESET_HOUR='4',COMPACTION_THRESHOLD='0.70',MEMORY_TOKEN_BUDGET='12000'
stderr_logfile=/var/log/gateway.err.log
stdout_logfile=/var/log/gateway.out.log

[program:qdrant]
command=qdrant --storage-path ./qdrant_data
autostart=true
autorestart=true
stderr_logfile=/var/log/qdrant.err.log
stdout_logfile=/var/log/qdrant.out.log

[program:model_router]
command=uvicorn model_router.main:app --host 0.0.0.0 --port 8742
autostart=true
autorestart=true
environment=GROQ_API_KEY='<groq-api-key>',CLASSIFIER_MODEL='openai/gpt-oss-20b'
stderr_logfile=/var/log/model_router.err.log
stdout_logfile=/var/log/model_router.out.log

[program:whatsapp_bridge]
command=node bridges/whatsapp_bridge/src/index.js
autostart=true
autorestart=true
environment=WHATSAPP_BRIDGE_HOST='127.0.0.1',WHATSAPP_BRIDGE_PORT='8091',WHATSAPP_BRIDGE_TOKEN='<bridge-token>',WHATSAPP_AUTH_DIR='./bridges/whatsapp_bridge/store/auth',GATEWAY_INTERNAL_URL='http://127.0.0.1:8080',GATEWAY_INTERNAL_TOKEN='<internal-service-token>'
stderr_logfile=/var/log/whatsapp_bridge.err.log
stdout_logfile=/var/log/whatsapp_bridge.out.log

[program:orchestrator]
command=python -m agents.orchestrator
autostart=true
autorestart=true
environment=INSTANCE_ID='orchestrator-1',AGENT_SECRETS='{"cosmic/gateway:1.0.0": "<gateway-signing-secret>", "cosmic/research-agent:1.0.0": "...", "cosmic/docs-agent:2.1.0": "..."}',GATEWAY_INTERNAL_TOKEN='<internal-service-token>'
stderr_logfile=/var/log/orchestrator.err.log
stdout_logfile=/var/log/orchestrator.out.log

[program:research_agent_1]
command=python -m agents.research_agent
autostart=true
autorestart=true
environment=INSTANCE_ID='research-1',AGENT_SECRET='<research-agent-specific-secret>'
stderr_logfile=/var/log/research_agent_1.err.log
stdout_logfile=/var/log/research_agent_1.out.log

[program:research_agent_2]
command=python -m agents.research_agent
autostart=true
autorestart=true
environment=INSTANCE_ID='research-2',AGENT_SECRET='<research-agent-specific-secret>'
stderr_logfile=/var/log/research_agent_2.err.log
stdout_logfile=/var/log/research_agent_2.out.log

[program:docs_agent]
command=python -m agents.docs_agent
autostart=true
autorestart=true
environment=INSTANCE_ID='docs-1',AGENT_SECRET='<docs-agent-specific-secret>'
stderr_logfile=/var/log/docs_agent.err.log
stdout_logfile=/var/log/docs_agent.out.log

[program:browser_agent]
command=python -m agents.browser_agent
autostart=true
autorestart=true
environment=INSTANCE_ID='browser-1',AGENT_SECRET='<browser-agent-specific-secret>',PLAYWRIGHT_BROWSERS_PATH='/opt/browsers'
stderr_logfile=/var/log/browser_agent.err.log
stdout_logfile=/var/log/browser_agent.out.log

[program:system_agent]
command=python -m agents.system_agent
autostart=true
autorestart=true
environment=INSTANCE_ID='system-1',AGENT_SECRET='<system-agent-specific-secret>'
stderr_logfile=/var/log/system_agent.err.log
stdout_logfile=/var/log/system_agent.out.log

[program:cli_agent]
command=python -m agents.cli_agent
autostart=false
autorestart=false
environment=INSTANCE_ID='cli-1',AGENT_SECRET='<cli-agent-specific-secret>',CLI_AGENT_MODE='sleeping'
stderr_logfile=/var/log/cli_agent.err.log
stdout_logfile=/var/log/cli_agent.out.log
; Alpha agent. autostart=false — wakes on demand only.
; autorestart=false — does not restart after exit.
```

### 9.1a Bridge Services on Bare-Metal / VM

On bare-metal or VM deployments, bridges follow the same process-management rule as the rest of the system: use `systemd`, not `supervisord`.

```ini
# /etc/systemd/system/cosmic-whatsapp-bridge.service
[Unit]
Description=COSMIC WhatsApp Bridge
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cosmic-agents/bridges/whatsapp_bridge
ExecStart=/usr/bin/node src/index.js
Restart=always
EnvironmentFile=/etc/cosmic/whatsapp-bridge.env

[Install]
WantedBy=multi-user.target
```

**Operational rule:** Bridges are first-class backend processes. Do not run them ad hoc in a terminal. They must be started, restarted, and monitored by the same process-management layer as the Gateway and agents.

### 9.1b Service Environment Files on Bare-Metal / VM

On VM deployments, the recommended pattern is **one env file per long-running service**, referenced by `systemd` `EnvironmentFile=` entries. Do **not** put Gateway, Model Router, Bridge, Orchestrator, and agent variables into one giant shared env file.

**Why this is the default:**

- least privilege: each process receives only the secrets it actually needs
- simpler rotation: one service can change its config/secrets without rewriting unrelated services
- clearer ownership: `GROQ_API_KEY` belongs to the Model Router, not the Gateway or Bridge
- easier debugging: service configuration is inspectable and isolated at the process boundary

**Canonical VM env-file layout:**

```text
/etc/cosmic/gateway.env
/etc/cosmic/model-router.env
/etc/cosmic/whatsapp-bridge.env
/etc/cosmic/orchestrator.env
/etc/cosmic/agents/research-agent.env
/etc/cosmic/agents/docs-agent.env
...
```

**Model Router example:**

```ini
# /etc/cosmic/model-router.env
GROQ_API_KEY=<groq-api-key>
CLASSIFIER_MODEL=openai/gpt-oss-20b
MODEL_ROUTER_HOST=0.0.0.0
MODEL_ROUTER_PORT=8742
HTTP2_ENABLED=true
CONNECTION_POOL_SIZE=10
KEEPALIVE_EXPIRY=30
```

```ini
# /etc/systemd/system/cosmic-model-router.service
[Unit]
Description=COSMIC Model Router
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cosmic-agents
ExecStart=/opt/cosmic-agents/.venv/bin/uvicorn model_router.main:app --host 0.0.0.0 --port 8742
Restart=always
EnvironmentFile=/etc/cosmic/model-router.env

[Install]
WantedBy=multi-user.target
```

**Gateway example:**

- `gateway.env` contains Gateway-owned config such as `ANTHROPIC_API_KEY`, `HAIKU_MODEL`, `PERPLEXITY_API_KEY`, `GATEWAY_INTERNAL_TOKEN`, `GATEWAY_SIGNING_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, `OPENROUTER_API_KEY`, and session/memory tunables.
- It may also include references to other internal services such as `MODEL_ROUTER_URL`, but it should not carry Model Router-only provider secrets like `GROQ_API_KEY` unless the Gateway itself truly needs them.

**Shared-secret note:** some values will appear in multiple files by design:

- `GATEWAY_INTERNAL_TOKEN` in Gateway + WhatsApp Bridge + Orchestrator
- `GATEWAY_SIGNING_SECRET` in Gateway + Orchestrator

Duplication of a small number of shared inter-service secrets is acceptable. A single all-services env file is not.

**Orchestrator Kimi local code sandbox:** when `COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER=fireworks_kimi`, the orchestrator does not get Anthropic's hosted `code_execution` container. COSMIC exposes a local bounded Python tool named `cosmic_code_execution` for calculations, quick checks, small data transforms, charts, and generated artifacts. The sandbox writes under `COSMIC_ARTIFACTS_ROOT/<task>/orchestrator/local_code_execution/<run>/`, captures deliverables from `outputs/`, and returns standard artifact descriptors so the existing artifact pipeline can surface files. Pip installs are enabled by default inside isolated cached venvs so normal scientific/charting packages can be used, while arbitrary network from executed user code stays disabled by default. It is not a shell/project runner; Alpha remains responsible for project edits, deployment, screenshots, and long-running VM work.

```ini
# /etc/cosmic/orchestrator.env
ORCHESTRATOR_CODE_SANDBOX_ENABLED=true
ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC=45
ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK=false
ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP=true
ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC=120
ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT=
ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES=256000
ORCHESTRATOR_CODE_SANDBOX_MAX_FILES=12
ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES=26214400
```

### 9.2 `__main__.py` Pattern (Every Agent)

```python
# agents/research_agent/__main__.py
import asyncio
from .agent import ResearchAgent
from shared.redis_client import get_redis

async def main():
    redis = await get_redis()
    agent = ResearchAgent(redis=redis)
    await agent.register()
    await agent.run()

if __name__ == '__main__':
    asyncio.run(main())
```

---

## 10. Orchestrator Routing Config

Pin exact agent versions in routing config. Never use `latest` in production — it is a silent breaking change waiting to happen.

```yaml
# routing.yaml — orchestrator reads this at startup
intents:
  research.topic:
    agent: cosmic/research-agent:1.0.0
    priority: normal
    fallback: null
  research.find_image:
    agent: cosmic/research-agent:1.0.0
    priority: normal
    fallback: null
  research.recall_session:
    agent: cosmic/research-agent:1.0.0
    priority: normal
    fallback: null
  docs.edit:
    agent: cosmic/docs-agent:2.1.0
    priority: high
    fallback: null
  docs.insert_image:
    agent: cosmic/docs-agent:2.1.0
    priority: high
    fallback: null
  docs.recall_session:
    agent: cosmic/docs-agent:2.1.0
    priority: normal
    fallback: null
  browser.navigate:
    agent: cosmic/browser-agent:1.0.0
    priority: normal
    fallback: null
  browser.extract:
    agent: cosmic/browser-agent:1.0.0
    priority: normal
    fallback: null
  browser.interact:
    agent: cosmic/browser-agent:1.0.0
    priority: normal
    fallback: null
  browser.screenshot:
    agent: cosmic/browser-agent:1.0.0
    priority: low
    fallback: null
  system.file_operation:
    agent: cosmic/system-agent:1.0.0
    priority: normal
    fallback: null
  system.process_manage:
    agent: cosmic/system-agent:1.0.0
    priority: high
    fallback: null
  system.shell_execute:
    agent: cosmic/system-agent:1.0.0
    priority: normal
    fallback: null
  system.clipboard:
    agent: cosmic/system-agent:1.0.0
    priority: normal
    fallback: null
  cli.execute:
    agent: cosmic/cli-agent:1.0.0
    priority: high
    fallback: null
```

**Orchestrator dispatch algorithm:**

1. Receive TaskEnvelope (`orchestrator.process` intent)
2. Classify complexity: simple (single intent) vs complex (multi-step) — see §31.2
3. **Simple path:** Look up `routing.yaml` → get target `agent_id` → query registry (healthy? `current_load < max_concurrency`?) → dispatch
4. **Complex path:** Create a structured plan (§31.3) → execute steps in dependency order (§31.4) → synthesize results (§31.6)
5. For each dispatch (simple or plan step): query registry, resolve credentials if needed (§22.3), dispatch to agent's Redis stream at correct priority tier
6. If unhealthy → retry after backoff, escalate to DLQ after N attempts

---

## 11. Skills Registry: Storage & Lookup

The registry is two systems working together. SQLite knows what capabilities exist. Redis knows who is alive right now. The orchestrator needs both to make a good dispatch decision.

| Store | Scope | Survives Restart | Updated By |
|---|---|---|---|
| **SQLite** | Persistent capabilities (WHAT agents can do) | Yes | Agent registration at startup |
| **Redis** | Ephemeral liveness (WHO is alive + load) | No | Agent heartbeat every N seconds |

### 11.1 SQLite Registry Schema

```sql
-- registry/schema.sql
CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT,
    status TEXT,                     -- 'registered', 'deprecated'
    max_concurrency INTEGER,
    heartbeat_ttl INTEGER,
    max_task_duration_sec INTEGER,   -- for XAUTOCLAIM tuning
    card_json TEXT,
    registered_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE agent_intents (
    agent_id TEXT,
    intent TEXT,
    timeout_sec INTEGER,
    PRIMARY KEY (agent_id, intent),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

CREATE TABLE agent_usage_daily (
    agent_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    usage_date TEXT NOT NULL,              -- YYYY-MM-DD (UTC day bucket)
    usage_count INTEGER NOT NULL DEFAULT 0,
    first_used_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (agent_id, intent, usage_date),
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

CREATE TABLE featured_specialists (
    rank INTEGER PRIMARY KEY,              -- 1..N prompt shortlist slot
    agent_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    agent_summary TEXT,
    common_intents_json TEXT NOT NULL,     -- compact JSON array of common intents
    score REAL NOT NULL DEFAULT 0,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMP,
    refreshed_at TIMESTAMP NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);
```

`agent_usage_daily` is the durable promotion/demotion input: every successful specialist dispatch increments the `(agent_id, intent, usage_date)` bucket and updates `last_used_at`. `featured_specialists` is the compact prompt-facing snapshot produced by a periodic orchestrator refresh loop from recent usage **plus** recently registered specialist cards. Newly registered specialists seed into the shortlist by default, and are only demoted after more than 15 days with no activity. The snapshot is intentionally small and partial; it does **not** replace live registry lookup.

### 11.2 Redis Live State (Updated by Heartbeats)

Each agent instance writes its liveness state to Redis every N seconds. The `heartbeat_ttl` is stored in the hash itself so it is always available in the routing hot path.

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
        # Auto-expire the key if this instance stops heartbeating.
        # Grace period of +5s prevents flapping during GC pauses or
        # transient network blips. If the instance crashes and never
        # writes again, Redis deletes the key automatically — no
        # accumulation of dead instance keys.
        await redis.expire(instance_key, self.heartbeat_ttl_sec + 5)

        await asyncio.sleep(self.heartbeat_interval_sec)
```

### 11.3 Redis Intent Index (Maintained at Registration)

A Redis Set per intent, maintained at registration/deregistration time — not at query time.

```python
async def register_intent_index(agent_id: str, card: dict, redis):
    for intent in card['intents']:
        intent_name = intent['name']
        await redis.sadd(f'intent:{intent_name}', agent_id)

async def deregister_intent_index(agent_id: str, card: dict, redis):
    for intent in card['intents']:
        await redis.srem(f'intent:{intent["name"]}', agent_id)
```

### 11.4 Orchestrator Lookup: Hot-Path Query

Intent set gives agent candidates (`O(1)` set read), then `SCAN` finds instance keys. Never use `redis.keys()` — it blocks the entire Redis event loop.

**Note:** All Redis values are strings because the client uses `decode_responses=True` (see §7.2). No `.decode()` calls needed.

```python
# shared/registry.py
async def find_available_instance(intent: str, redis) -> tuple[str, str] | None:
    # Step 1: SMEMBERS — O(n_agents_with_intent), typically 1-3
    agent_ids = await redis.smembers(f'intent:{intent}')

    for agent_id in agent_ids:
        # Step 2: SCAN for instance keys
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor,
                match=f'registry:{agent_id}:*',
                count=10
            )
            for key in keys:
                state = await redis.hgetall(key)
                if not state:
                    continue
                load = int(state.get('current_load', '999'))
                max_c = int(state.get('max_conc', '1'))
                ttl = int(state.get('heartbeat_ttl', '30'))
                last = to_utc_aware(datetime.fromisoformat(state['last_seen']))
                is_alive = (utcnow() - last).total_seconds() < ttl
                if is_alive and load < max_c:
                    instance_id = key.split(':')[-1]
                    return agent_id, instance_id
            if cursor == 0:
                break

    return None, None
```

### 11.5 Agent Startup: Self-Registration + Atomic Liveness

On startup every agent reads its own `agent_card.yaml` and writes itself into both stores. The startup sequence guarantees the agent is fully ready before receiving its first task.

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

    # Step 4: Run any startup checks (DB migrations, model load, etc)
    await self.on_startup()

    # Step 5: Mark healthy — orchestrator will now dispatch to this instance
    await redis.hset(instance_key, 'status', 'healthy')

    # Step 6: Start heartbeat loop
    asyncio.create_task(self.heartbeat_loop())

    # Step 7: NOW start consuming from Redis streams
    await self.run()
```

**Startup sequence contract:** `SQLite write → Redis 'starting' → on_startup() → Redis 'healthy' → begin consuming.` An agent stuck in `starting` beyond `startup_timeout_sec` is marked dead. Orchestrator routing requires `status == 'healthy'`.

### 11.6 Dynamic Specialist Shortlist (Prompt Hint Only)

The orchestrator may surface a **small dynamic specialist shortlist** in its system prompt. This shortlist is **not** the full registry and must never be treated as the source of truth for capability discovery.

Promotion and demotion are deterministic:

1. every successful specialist dispatch increments `agent_usage_daily`
2. the orchestrator periodically recomputes a ranked shortlist from recent usage + recency
3. the top `N` specialists are written into `featured_specialists`
4. prompt assembly injects only that compact snapshot, with explicit wording that it is just a subset

The runtime still uses the full live registry path for real work:

- `agent_catalog_search` remains the authoritative discovery surface for exact intents
- `delegate_to_agent` still resolves the actual healthy instance from SQLite + Redis
- the prompt shortlist exists only to help Opus form better first-pass plans without dumping the whole registry into context

This is intentionally lightweight. It should be implemented as a small periodic refresh loop inside the orchestrator runtime or an equivalent same-VM scheduled job. A second LLM is **not required** for promotion/demotion; deterministic scoring from usage frequency and recency is the default.

---

## 12. Agent Learnings, Session Data & Input Artifacts

### 12.1 Agent Learnings (`store/learnings.md`)

Each agent accumulates knowledge over time in `store/learnings.md` — facts, preferences, and patterns it discovers across sessions. This is the agent's long-term memory.

```markdown
# Research Agent — Learnings

## User Preferences
- User prefers academic sources over blog posts
- User wants APA citation format

## Source Quality Notes
- arxiv.org: high reliability for CS topics
- medium.com: treat as opinion, always cross-reference

## Session Patterns
- When intent is research.topic with input containing "architecture",
  user typically wants both academic papers and industry blog posts
```

The agent reads `learnings.md` at task start (alongside the system prompt) and appends to it after completing tasks when new knowledge is worth persisting. This file is agent-private — no other agent reads it directly. However, the Session Manager syncs agent learnings into the `memory/agent_notes/` directory and indexes them in Qdrant as high-priority memories (see §23.7). This allows the orchestrator and all LLM backends to benefit from agent-curated knowledge when assembling context.

### 12.2 Agent-Managed Session Data (`store/data/`)

Each agent manages its own session storage in `store/data/` with whatever schema fits its domain. **No uniform session schema is imposed across agents.** A research agent might track source credibility; a docs agent might track edit history and document versions.

Example: docs agent's schema:

```sql
-- agents/docs_agent/store/data/sessions.db
CREATE TABLE edit_sessions (
    session_id TEXT,
    task_id TEXT,
    doc_id TEXT,
    operation TEXT,          -- 'insert', 'delete', 'reformat', 'rollback'
    target TEXT,             -- block ID, document range, table cell, etc.
    summary TEXT,
    before_hash TEXT,
    after_hash TEXT,
    revision_before TEXT,    -- provider version / revision / etag before write
    revision_after TEXT,     -- provider version / revision / etag after write
    verified INTEGER DEFAULT 0,
    metadata_json TEXT,      -- provider-specific rollback / audit metadata
    created_at TIMESTAMP,
    PRIMARY KEY (session_id, task_id)
);
```

Docs agents that support rollback often also persist enough `before` / `after` material or
provider-native patch metadata to safely reverse prior edits. The exact shape remains
domain-specific.

Example: research agent's schema:

```sql
-- agents/research_agent/store/data/sessions.db
CREATE TABLE research_sessions (
    session_id TEXT,
    task_id TEXT,
    query TEXT,
    sources_json TEXT,
    citations_count INTEGER,
    confidence_score REAL,
    created_at TIMESTAMP,
    PRIMARY KEY (session_id, task_id)
);
```

### 12.2a Mutable Provider Resource Safety

Agents that mutate provider-owned remote state (Docs, Drive, calendars, tickets, issues, etc.)
must use provider-native optimistic concurrency or precondition tokens when the provider offers
them.

**Rules:**

- Read the target object's latest version / revision / ETag / precondition token immediately before
  the write when available.
- Send that token on the mutation call (`requiredRevisionId`, `etag`, `If-Match`, or provider
  equivalent) so concurrent remote edits fail closed instead of silently overwriting.
- If the caller supplied an expected anchor/snippet/hash for the remote object, compare it against
  freshly-read provider state before writing and abort on mismatch.
- After the write, inspect the provider response or re-read enough state to verify the intended
  change landed. If verification is inconclusive, do not claim unconditional success.
- Domains with meaningful user-facing edits SHOULD persist an edit ledger in `store/data/` with
  before/after summary or hashes, provider revision/version before and after, verification status,
  and metadata needed for recall or rollback.
- Best-effort rollback is allowed, but rollback must also respect the provider's current
  revision/precondition token. If the remote object has changed since the recorded edit, block or
  downgrade the rollback rather than blindly replaying stale inverse operations.

### 12.3 Session Context Flow

The orchestrator maintains session-wide context and passes relevant information to agents via the TaskEnvelope `input` field. Agents do **not** read from each other's databases or from a shared session store.

```
Orchestrator maintains session context
    │
    ├── Passes relevant context in TaskEnvelope.input
    │   e.g., input: { query: "...", session_context: { doc_id: "...", goal: "..." } }
    │
    ├── Queries agent about past work via recall intents
    │   e.g., intent: "docs.recall_session", input: { session_id: "sess_abc" }
    │   Agent queries its own store/data/ and returns structured history
    │
    └── Promotes important facts to future task inputs
        e.g., research_agent discovered "user prefers academic sources"
        → orchestrator includes this in future tasks to other agents
```

**Why no shared session database:** Agents run as separate processes (potentially separate containers). A shared SQLite file requires filesystem-level coordination. A shared Redis store couples all agents' session schemas. Instead, the orchestrator asks agents about their past work using recall intents, and passes relevant context forward — keeping agents fully decoupled.

### 12.4 Recall Intents: Querying Agents About Past Work

When the orchestrator needs an agent's session history (e.g., user asks "explain the last edits we did"), it dispatches a recall intent. The agent queries its own `store/data/` and returns structured results.

```python
# Orchestrator dispatches to docs_agent:
TaskEnvelope(
    task_id='tsk_recall_001',
    session_id='sess_abc',
    sender='cosmic/orchestrator:1.0.0',
    recipient='cosmic/docs-agent:2.1.0',
    intent='docs.recall_session',
    input={
        'session_id': 'sess_abc',
        'query': 'what edits were made',
        'limit': 10,
    },
    ...
)

# docs_agent handles this by querying its own sessions.db:
# SELECT * FROM edit_sessions WHERE session_id = 'sess_abc' ORDER BY created_at DESC LIMIT 10
# Returns structured AgentResult with edit history in output dict
```

Each agent defines its own recall intent schema in `schemas/intents/`. The orchestrator doesn't need to know the agent's internal data model — it just sends the query and receives structured output.

### 12.5 Input Artifacts: Passing Files TO Agents

`input_artifacts` allows the orchestrator to pass files — PDFs, images, prior results — as typed, validated inputs to any agent.

```python
task = TaskEnvelope(
    task_id='tsk_analyze_001',
    session_id='sess_abc',
    sender='cosmic/orchestrator:1.0.0',
    recipient='cosmic/research-agent:1.0.0',
    intent='research.topic',
    input={'query': 'summarize key architecture decisions'},
    input_artifacts=[
        ArtifactManifest(
            artifact_id='art_001',
            task_id='tsk_prev_001',
            mime='application/pdf',
            sha256='abc123...',
            path='runs/artifacts/tsk_prev_001/paper.pdf',
            created_by_agent='cosmic/docs-agent:2.1.0'
        )
    ]
)
```

**Why `input_artifacts` is not just a path string in `input: dict`:** Typed (agent knows mime type), verified (sha256 integrity check), traceable (links back to producing task), reusable (same artifact can be passed to multiple agents without copying), auditable (orchestrator knows exactly which files were passed to which agents).

When a prior produced file is needed again, the orchestrator should first resolve it from session/turn artifact history and then attach the normalized artifact descriptor into `TaskEnvelope.input_artifacts`. The prompt/tool layer should never browse raw artifact directories directly.

### 12.6 Agent Task Handler

Every agent base class handles session data loading, artifact verification, and idempotency.

```python
async def handle(self, task: TaskEnvelope, msg_id: str, stream: str):
    # Verify signature
    if not verify_incoming(task):
        raise AuthError(f'Invalid signature on task {task.task_id}')

    # Check epoch before any execution
    if task.leader_epoch is not None:
        current_epoch = int(await redis.get(EPOCH_KEY) or 0)
        if task.leader_epoch < current_epoch:
            await self._reject_stale_epoch(task, msg_id, stream, current_epoch)
            return

    # ── Extract and isolate auth context ───────────────────────────
    # input.auth is stripped from the task input BEFORE any processing.
    # It is exposed to the agent's execute() method via self.auth —
    # a runtime-only attribute that is NEVER serialized to events,
    # logs, artifacts, store/, or learnings.md.
    self.auth = task.input.pop('auth', None)

    # Verify input artifacts
    for artifact in task.input_artifacts:
        verify_artifact(artifact)

    # Execute with idempotency enforcement
    result = await execute_with_idempotency(
        task, self.execute, redis,
        agent_max_duration_sec=self.max_task_duration_sec,
    )

    # Clear auth context after execution — never persisted
    self.auth = None

    if isinstance(result, AgentResult):
        # Update agent's own session data + learnings as needed
        if task.session_id and result.status == 'completed':
            self.save_session_data(task.session_id, task, result)
            self.maybe_update_learnings(task, result)
        await redis.xack(stream, 'workers', msg_id)
        await self.emit_terminal_event(task.task_id, result)

    elif isinstance(result, TaskInProgress):
        # Another instance is executing. Recovery is orchestrator-owned.
        #
        # ORDER IS CRITICAL: emit task.deferred BEFORE XACK.
        # If agent crashes after XACK but before emit, the message is gone
        # and no deferred_checks row is created — task is silently lost.
        # By emitting first, the orchestrator has the deferred record in its
        # event stream before we remove the message from pending.
        # If agent crashes after emit but before XACK: message is reclaimed
        # by XAUTOCLAIM → idempotency returns TaskInProgress again → another
        # task.deferred is emitted (harmless duplicate, orchestrator upserts).
        await self.emit_event(
            task_id=task.task_id,
            event_type='task.deferred',
            payload={
                'reason': 'already_executing',
                'idempotency_key': task.idempotency_key,
                'executing_since': result.executing_since.isoformat(),
                'check_after_sec': result.check_after_sec,
            },
        )
        await redis.xack(stream, 'workers', msg_id)
```

---

### 12.7 Deferred Task Recovery (Orchestrator-Owned)

When an agent emits `task.deferred`, the orchestrator — not the agent — owns the recovery lifecycle. This preserves the "all communication is orchestrator-mediated" rule and ensures recovery survives process crashes.

**Why the orchestrator, not the agent?**
- Agent-side timers (`asyncio.sleep`) block the worker loop and are not durable — a crash loses the timer.
- Agent-side redispatch bypasses the leader ledger and fencing controls.
- The orchestrator already consumes `streams:events` and already has crash recovery for orphaned tasks.

**Durable state:** The orchestrator persists deferred tasks in its own SQLite ledger so recovery survives crashes.

```sql
-- Orchestrator's task ledger (already exists for task tracking)
CREATE TABLE deferred_checks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    check_after TIMESTAMP NOT NULL,      -- UTC: when to next check
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 6,
    created_at TIMESTAMP NOT NULL
);
```

**Orchestrator event handler:**

```python
async def on_event(event: EventEnvelope):
    if event.event_type == 'task.deferred':
        # Persist durable check — survives orchestrator crash
        db.execute('''
            INSERT OR REPLACE INTO deferred_checks
            (task_id, idempotency_key, check_after, attempts, max_attempts, created_at)
            VALUES (?, ?, datetime('now', '+' || ? || ' seconds'), 0, 6, datetime('now'))
        ''', [
            event.task_id,
            event.payload['idempotency_key'],
            event.payload['check_after_sec'],
        ])
```

**Orchestrator polling loop** (runs alongside the main event consumer):

```python
async def deferred_check_loop(redis):
    """Non-blocking poll — runs every 10s, never sleeps for minutes."""
    while True:
        await asyncio.sleep(10)     # poll interval — not per-task blocking

        due = db.execute('''
            SELECT task_id, idempotency_key, attempts, max_attempts
            FROM deferred_checks
            WHERE check_after <= datetime('now')
        ''').fetchall()

        for task_id, idem_key, attempts, max_attempts in due:
            result_key = f'idempotency:result:{idem_key}'
            dedupe_key = f'idempotency:{idem_key}'

            # Case 1: result exists — task completed, clean up
            if await redis.exists(result_key):
                db.execute('DELETE FROM deferred_checks WHERE task_id = ?', [task_id])
                continue

            # Case 2: lock still held — executor likely still running.
            # Keep checking regardless of attempt count while lock exists.
            # The lock has its own TTL — it will expire if executor is dead.
            if await redis.exists(dedupe_key):
                db.execute('''
                    UPDATE deferred_checks
                    SET check_after = datetime('now', '+30 seconds'),
                        attempts = attempts + 1
                    WHERE task_id = ?
                ''', [task_id])
                # Alert after max_attempts so operators know, but don't redrive
                if attempts >= max_attempts:
                    logger.warning(
                        f'Deferred check for {task_id}: {attempts} attempts, '
                        f'lock still held — executor may be stuck'
                    )
                continue

            # Case 3: lock gone — executor finished without storing result,
            # or crashed and lock expired. Safe to redrive.
            logger.warning(f'Deferred recovery: redriving {task_id}')
            task = await load_task_envelope(task_id)
            new_epoch = await get_current_epoch(redis)
            task = prepare_for_redispatch(task, new_epoch, self.secrets)
            await dispatch(task, redis)
            db.execute('DELETE FROM deferred_checks WHERE task_id = ?', [task_id])
```

**Crash safety:** On orchestrator startup, `recover_orphaned_tasks()` (§16.3) already scans for stale in-progress tasks. `deferred_checks` rows survive in SQLite. The polling loop picks them up immediately after restart. Worst-case recovery time: poll interval (10s) + one check cycle (30s) = 40s after orchestrator restarts.

### 12.8 Event Stream Trimming & Archival

`streams:events` is trimmed to `EVENTS_STREAM_MAXLEN` (50,000 entries) on every `XADD`. Without archival, old events would be silently lost when the stream is trimmed. The archiver writes completed task events to disk as `.jsonl` files before they become eligible for eviction.

**Why trim at all?** Redis holds the stream in memory. An unbounded stream grows forever — after months of use, even a single-user system accumulates hundreds of thousands of events. Trimming keeps memory usage predictable. The per-task event index (`task_events:{task_id}`) expires after `RESULT_TTL_SEC` (7 days), so recent tasks always have fast O(k) replay. Older tasks fall through to the disk archive.

**Archiver (runs in Gateway or orchestrator):**

```python
# shared/event_archiver.py
import json
from pathlib import Path

ARCHIVE_DIR = Path('./logs/events')
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

async def archive_task_events(task_id: str, events: list[EventEnvelope]):
    """Write a completed task's events to disk as a .jsonl file.
    Called after task.completed or task.failed is received."""
    path = ARCHIVE_DIR / f'{task_id}.jsonl'
    with open(path, 'w') as f:
        for event in events:
            f.write(event.model_dump_json() + '\n')

async def load_archived_events(task_id: str) -> list[EventEnvelope]:
    """Load events from disk archive. Used when per-task Redis index
    has expired and events have been trimmed from the stream."""
    path = ARCHIVE_DIR / f'{task_id}.jsonl'
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            events.append(EventEnvelope.model_validate_json(line.strip()))
    return sorted(events, key=lambda e: e.seq)
```

**When archival happens:** The orchestrator archives a task's events immediately after processing a terminal event (`task.completed` or `task.failed`). At that point, the per-task index still has all message IDs, so the archiver fetches the events from the stream and writes them to disk. After archival, the events can safely be trimmed from Redis by the `MAXLEN` cap — the `.jsonl` file is the permanent record.

```python
# Orchestrator terminal event handler
async def on_terminal_event(event: EventEnvelope, redis):
    task_id = event.task_id
    # Fetch all events for this task while they're still in Redis
    events = await get_task_events(task_id, redis)
    # Archive to disk
    await archive_task_events(task_id, events)
```

**Lifecycle of an event:**

```
emit_event()
  → XADD to streams:events (with MAXLEN trim)
  → RPUSH msg_id to task_events:{task_id} (per-task index)
  → Both consumer groups (orchestrator + gateway) receive the event
  → ...task completes...
  → Orchestrator archives all task events to logs/events/{task_id}.jsonl
  → Per-task index expires after RESULT_TTL_SEC (7 days)
  → Event is eventually trimmed from streams:events by MAXLEN
  → Disk archive remains for historical queries and debugging
```

---

## 13. Bidirectional Communication: Agent ↔ Orchestrator

**Core rule: All communication is orchestrator-mediated.** Agents NEVER talk to each other directly. All messages route through the orchestrator. This preserves the task ledger, observability, and retry logic for every interaction.

### 13.1 Case 1: Agent Asks Orchestrator (Reverse Task)

Agent hits ambiguity or a missing capability it cannot resolve. It creates a TaskEnvelope addressed TO the orchestrator, suspends its current task, and waits for a reply.

```python
# Agent sends a clarification request
{
    'task_id': 'tsk_question_001',
    'parent_task_id': 'tsk_abc123',
    'sender': 'cosmic/research-agent:1.0.0',
    'recipient': 'cosmic/orchestrator:1.0.0',
    'intent': 'orchestrator.clarify',
    'input': {
        'question': 'Found conflicting sources. Which do I prioritize?',
        'options': ['source_a', 'source_b'],
        'context_artifact': 'art_xyz'
    },
    'idempotency_key': 'uuid'
}

# Agent emits task.suspended event for parent task
# Serializes in-progress state to runtime/state.db
# Waits on streams:cosmic/research-agent:1.0.0:replies

# Orchestrator processes clarify intent, replies with TaskEnvelope:
# intent: 'agent.resume', input: { decision: 'use source_a', task_id: 'tsk_abc123' }
# Agent deserializes state, resumes work
```

Orchestrator must define these intents:

| Intent | Purpose |
|---|---|
| `orchestrator.clarify` | Agent needs a decision between options |
| `orchestrator.approve` | Agent needs permission before a side effect |
| `orchestrator.decide` | Agent hit a branch it cannot resolve |
| `orchestrator.delegate` | Agent needs another agent's output |
| `orchestrator.escalate` | Push to gateway for human input |
| `orchestrator.refresh_credential` | Agent's access token expired mid-task — orchestrator refreshes via Gateway and resumes agent with new token (see §22.5) |

**Important specialist rule:** a specialist does **not** need to know the full sibling-agent inventory. Its responsibility is to recognize "I am blocked and need a different capability" and emit the appropriate reverse task. Agent discovery, health checks, routing, retries, and result fan-in remain orchestrator concerns.

### 13.2 Case 2: Agent Needs Human Input (Task Input Queue)

Orchestrator cannot answer the agent's question itself. It publishes a user input request to the `user_input:requests` Redis stream. The Gateway consumes this, surfaces it to the user via WebSocket, collects the reply, and publishes it to `user_input:replies`. The orchestrator picks up the reply and resumes the suspended agent.

**This mechanism is exclusively for tasks** (background agent execution). For conversational replies (inline Q&A, planning), the `<awaiting_reply/>` control tag and sticky routing handle it — see §3.7 and §3.8.

```
Full escalation chain:

1. research_agent → orchestrator (intent: orchestrator.clarify)
   Agent suspends, emits task.suspended event.

2. Orchestrator cannot resolve → publishes to user_input:requests:
   {
       'input_request_id': 'uir_001',
       'task_id': 'tsk_abc123',
       'agent': 'cosmic/research-agent:1.0.0',
       'channel': 'desktop:desk_a1b2c3',
       'question': 'Found conflicting sources. Which to prioritize?',
       'options': ['source_a', 'source_b'],
       'status': 'pending',
       'timestamp': '2025-01-15T10:00:00Z'
   }

3. Gateway consumes from user_input:requests
   → sends WebSocket event: { type: 'task.input_required', ... }
   → UI shows notification/modal (NOT inline chat)

4. User answers → WebSocket message: { type: 'task.input_reply', input_request_id: 'uir_001', ... }

5. Gateway publishes to user_input:replies:
   {
       'input_request_id': 'uir_001',
       'task_id': 'tsk_abc123',
       'content': 'Use source A',
       'timestamp': '2025-01-15T10:01:30Z'
   }

6. Orchestrator consumes from user_input:replies
   → matches by input_request_id
   → sends resume envelope to research_agent:
     intent: 'agent.resume', input: { decision: 'use source_a', task_id: 'tsk_abc123' }

Task state throughout: task.suspended
```

```python
# Orchestrator publishes user input request
async def request_user_input(task_id: str, agent: str, question: str,
                               options: list, channel: str | None, redis):
    input_request_id = f'uir_{uuid4().hex[:12]}'
    await redis.xadd('user_input:requests', {
        'payload': json.dumps({
            'input_request_id': input_request_id,
            'task_id': task_id,
            'agent': agent,
            'channel': channel,
            'question': question,
            'options': options,
            'status': 'pending',
            'timestamp': utcnow().isoformat(),
        }),
    })
    return input_request_id

# Orchestrator startup: create consumer group for replies (idempotent)
try:
    await redis.xgroup_create('user_input:replies', 'orchestrator', id='0', mkstream=True)
except ResponseError as e:
    if 'BUSYGROUP' not in str(e):
        raise

# Orchestrator consumes user replies
async def user_reply_consumer(redis):
    """Runs in orchestrator. Picks up user replies and resumes agents."""
    while True:
        entries = await redis.xreadgroup(
            groupname='orchestrator',
            consumername=ORCHESTRATOR_INSTANCE_ID,
            streams={'user_input:replies': '>'},
            count=5,
            block=1000,
        )
        for stream, messages in entries:
            for msg_id, data in messages:
                reply = json.loads(data['payload'])
                await resume_agent_with_reply(
                    reply['task_id'],
                    reply['input_request_id'],
                    reply['content'],
                )
                await redis.xack('user_input:replies', 'orchestrator', msg_id)
```

**Timeout handling:** If the user doesn't respond, the request stays in `pending` status. A future heartbeat process (not in this spec) will handle reminders, escalation (push notification, etc.), and eventually putting the task on hold. The task is NOT killed on timeout — it remains suspended with state preserved, resumable whenever the user responds.

### 13.3 Case 3: Agent Needs Another Agent

Route through orchestrator. Never direct agent-to-agent.

Specialists SHOULD stay **registry-agnostic** by default. They may know a likely target intent (for example, `firecrawl.scrape`), but they should not depend on direct sibling connections, ad-hoc RPCs, or shared specialist databases. The orchestrator owns sibling selection, dispatch, retries, and resumption.

```python
# WRONG — do not do this:
# result = await docs_agent.get_context(doc_id)  # bypasses task ledger

# CORRECT — send reverse task to orchestrator:
{
    'task_id': 'tsk_ctx_001',
    'parent_task_id': 'tsk_abc123',
    'sender': 'cosmic/research-agent:1.0.0',
    'recipient': 'cosmic/orchestrator:1.0.0',
    'intent': 'orchestrator.delegate',
    'input': {
        'target_intent': 'docs.get_section_context',
        'target_input': { 'doc_id': 'doc_xyz', 'section': 'architecture' },
        'resume_payload': { 'phase': 'collect_sources' }
    }
}
# Orchestrator fans out to docs_agent, waits for result,
# then returns result to research_agent via resume envelope.
```

#### 13.3.1 Implemented reverse-delegate lifecycle

The durable lifecycle for `orchestrator.delegate` is:

1. Specialist decides its own tools are insufficient and emits a signed reverse task to the orchestrator.
2. Orchestrator verifies that:
   - `source='agent'`
   - the sender owns `parent_task_id`
   - `target_intent` / `target_input` are valid
3. Orchestrator records the reverse task plus a durable **reverse wait** entry in the task ledger.
4. The specialist emits `task.suspended` on the waiting task and returns non-terminal control to its runtime.
5. Only after the waiting task is durably suspended does the orchestrator dispatch the sibling specialist task.
6. When the sibling specialist completes or fails, the orchestrator sends an `agent.resume` task back to the waiting specialist.
7. The resumed specialist receives:
   - `reverse_task`: metadata about the reverse delegate (`reverse_task_id`, `target_intent`, optional `target_agent_id`, delegated task id)
   - `reverse_result`: the delegated specialist `AgentResult`
   - the specialist's own `resume_payload`
8. The specialist continues inside its original intent, consuming the delegated result as new evidence rather than inventing a second conversation/channel.

This ordering matters. The orchestrator MUST register the reverse wait before sibling fan-out, so delegated work never races ahead of the waiting specialist's durable suspended state.

#### 13.3.2 Prompt contract for future specialists

Future specialist prompts/policies SHOULD follow this pattern:

- Exhaust specialist-local tools and memory first.
- If blocked on external information or an out-of-domain capability, ask the orchestrator to delegate rather than guessing.
- Prefer a **target intent** when known; use `target_agent_id` only when a specific sibling agent is truly required.
- Keep delegation **bounded**. Recommended default: at most one reverse delegation per specialist task unless that specialist is explicitly designed for multi-hop orchestration.
- Do not claim sibling registry awareness. The prompt can say "the system has sibling specialists and you may ask the orchestrator for one," but should not hardcode a brittle inventory list as operational truth.
- On resume, treat `reverse_result` as authoritative delegated output and continue the original task; do not start a new unrelated plan.

### 13.4 Task Lifecycle (Suspendable Tasks)

```
task.accepted
    ↓
task.progress (emitted as work proceeds)
    ↓
task.suspended ← agent waiting for input before continuing
    ↓  (agent serializes state to runtime/state.db)
... question resolved, resume envelope arrives ...
    ↓
task.resumed → task.progress (continues)
    ↓
task.completed  OR  task.failed  OR  task.suspended again
```

### 13.5 Communication Flow Diagram

```
FIVE INPUT SOURCES
    │
    ├── ① Messages ──── Channel Adapters ────┐
    │   Desktop App (WebSocket)              │
    │   WhatsApp (Adapter)                   │
    │   Telegram (Adapter)                   │
    │   Slack (Adapter)                      │
    │   CLI Agent (Internal)                 │
    │                                         │
    ├── ② Heartbeats ── Scheduler ───────────┤
    ├── ③ Crons ──────── Scheduler ───────────┤
    ├── ④ Hooks ──────── Hooks Engine ────────┤
    ├── ⑤ Webhooks ───── Webhook Handler ─────┤
    │                                         ▼
    │   All tagged with: source, source_id, channel
    │   Priority: user=high, webhook=normal, cron/heartbeat=low
    │
GATEWAY (FastAPI :8080)
    │  ┌─ awaiting_reply check (§3.7) ──► sticky route to last_route
    │  └─ no flag → proceed to classifier
    │  Session Manager assembles context (conversation + memories)
    ↕  HTTP (internal)
MODEL ROUTER (FastAPI :8742)
    │  classifies → route (opus / haiku / perplexity)
    │
    ├─ route=opus ──────────────────────────────────────┐
    │  (tasks, continuations, ambiguous, fallback)      │
    ├─ route=haiku ──► Claude Haiku 4.5 API (direct)    │
    │  (response may contain <awaiting_reply/> tag)     │
    └─ route=perplexity ──► Perplexity API (direct)     │
       (response may contain <awaiting_reply/> tag)     │
                                                        ▼
                                                   ORCHESTRATOR
                                                   Propagates source, source_id,
                                                   channel to all child tasks.
                                                        ↕  TaskEnvelope (all directions)
                                                   AGENTS (never talk to each other)
                                                   research, docs, diagram,
                                                   browser, system agents
                                                        │
                                                   CLI AGENT (alpha, sleeping)
                                                   Full system access on demand.

    Conversational replies:  <awaiting_reply/> tag → sticky routing (§3.7)
    Task input requests:     user_input:requests → Gateway → channel adapter → user_input:replies (§13.2)

All orchestrator-mediated messages are TaskEnvelopes.
The orchestrator is both a dispatcher AND a participant in the task graph.
All three routes receive the same assembled context from the Session Manager.
Responses route back via the originating channel adapter (§27).
```

---

## 14. Idempotency Enforcement

Every task execution is guarded by a SETNX-based idempotency check. The ordering of operations is critical and non-negotiable.

### 14.1 Configuration

```python
# shared/config.py
DEDUPE_TTL_MIN_SEC = 60
DEDUPE_TTL_FALLBACK_SEC = 600       # only if registry lookup fails
RESULT_TTL_SEC = 604800             # 7 days
```

| TTL Key | Purpose |
|---|---|
| `dedupe_key` TTL | Prevents re-execution of in-flight or completed task. Derived from `2 × deadline` if deadline exists, otherwise `2 × max_task_duration_sec` from the agent's registry entry. Floor: 60s. |
| `result_key` TTL | How long terminal results are cached for replay and auditing. Not safety-critical. Default: 7 days. |

### 14.2 Canonical `execute_with_idempotency`

**Order: (1) replay check, (2) deadline guard, (3) lock + execute.** This ordering is not optional.

```python
# shared/idempotency.py
async def execute_with_idempotency(
    task: TaskEnvelope,
    handler: Callable,
    redis: Redis,
    agent_max_duration_sec: int = 0,    # from agent_card.yaml sla.max_task_duration_sec
) -> ExecutionResult:

    dedupe_key = f'idempotency:{task.idempotency_key}'
    result_key = f'idempotency:result:{task.idempotency_key}'

    # ── Step 1: Replay path — ALWAYS first ──────────────────────────────
    stored = await redis.get(result_key)
    if stored:
        return AgentResult.model_validate_json(stored)

    # ── Step 2: Deadline guard — only for tasks not yet executed ─────────
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
        # No deadline — derive from agent's declared max task duration
        base_ttl = agent_max_duration_sec * 2
    else:
        base_ttl = DEDUPE_TTL_FALLBACK_SEC

    dedupe_ttl = max(DEDUPE_TTL_MIN_SEC, base_ttl)

    # ── Step 3: Acquire lock ────────────────────────────────────────────
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

    # ── Step 4: Execute ─────────────────────────────────────────────────
    try:
        result = await handler(task)
        await redis.set(result_key, result.model_dump_json(), ex=RESULT_TTL_SEC)
        return result
    except Exception:
        await redis.delete(dedupe_key)
        raise
```

**Critical invariant:** If a terminal result is stored → replay it, always, regardless of deadline. If no result stored AND deadline exceeded → `DEADLINE_EXCEEDED`. If no result stored AND deadline not exceeded → acquire lock and execute.

**Store-before-ack ordering:** `result stored → stream message acked → terminal event emitted`. If crash happens between store and ack: message redelivered, result replayed (safe). If crash happens between ack and event: result is stored, orchestrator can query it (safe).

---

## 15. Security

**Trust model:** COSMIC runs as a single-user-per-VM deployment. The VM boundary is the security perimeter — all processes inside the VM serve one user. There is no application-level user isolation, no tenant scoping on Redis keys, no per-user access control within the backend. All agents are first-party code authored by us. The security concerns in this section focus on message integrity (HMAC), secret management, and external credential protection — not on isolating users from each other.

### 15.1 HMAC Signing: Per-Channel Shared Secret

Every orchestrator ↔ agent pair shares ONE secret (the agent's secret). Both directions sign and verify with that same secret. This is an integrity check — all agents are first-party code authored by us, so the goal is message authenticity and tamper detection, not adversarial isolation.

```python
# shared/auth.py
import hmac, hashlib, json, os

ORCHESTRATOR_ID = 'cosmic/orchestrator:1.0.0'

def canonical_payload(task: TaskEnvelope) -> bytes:
    data = task.model_dump(
        mode='json',
        exclude={'signature'},
    )
    return json.dumps(
        data,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')

def sign_task(task: TaskEnvelope, secret: str) -> str:
    return hmac.new(
        secret.encode(),
        canonical_payload(task),
        hashlib.sha256
    ).hexdigest()

def verify_task(task: TaskEnvelope, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_task(task, secret), signature)

# --- Orchestrator side ---

def dispatch_signed(task: TaskEnvelope, secrets: dict) -> TaskEnvelope:
    """Orchestrator signs outgoing task with the recipient agent's secret."""
    secret = secrets[task.recipient]
    task.signature = sign_task(task, secret)
    return task

def verify_incoming_orchestrator(task: TaskEnvelope, secrets: dict) -> bool:
    """Orchestrator verifies reverse-task from agent using that agent's secret."""
    secret = secrets[task.sender]
    return verify_task(task, task.signature, secret)

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

**Per-channel shared secret model:** Each agent has its OWN secret set via supervisord environment variable. The orchestrator holds all agent secrets. Each agent-orchestrator channel uses the agent's secret for both directions. The `sender` field in the envelope determines which secret the orchestrator uses to verify incoming reverse-tasks.

### 15.2 Artifact Path Security

Artifact paths are validated against an allowlist before any file read. Path traversal attacks are blocked before sha256 verification.

```python
# shared/artifact_security.py
from pathlib import Path
import hashlib

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATIC_ROOTS = [
    (PROJECT_ROOT / 'runs' / 'artifacts').resolve(),
]

def _get_allowed_roots() -> list[Path]:
    """Build allowlist dynamically on every call.
    Includes all agents/*/store/ and agents/*/runtime/ directories,
    even those created after process startup."""
    roots = list(STATIC_ROOTS)
    agents_dir = PROJECT_ROOT / 'agents'
    if agents_dir.exists():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            for subdir in ['store', 'runtime']:
                path = agent_dir / subdir
                if path.exists():
                    roots.append(path.resolve())
    return roots

def safe_artifact_path(raw_path: str) -> Path:
    # Resolve relative paths against PROJECT_ROOT, not process CWD.
    # This prevents environment-dependent behavior when agents run
    # from different working directories.
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    allowed = any(resolved.is_relative_to(root) for root in _get_allowed_roots())
    if not allowed:
        raise SecurityError(f'Path outside allowlist: {resolved}')
    return resolved

def verify_artifact(manifest: ArtifactManifest) -> Path:
    safe_path = safe_artifact_path(manifest.path)
    actual_hash = hashlib.sha256(safe_path.read_bytes()).hexdigest()
    if actual_hash != manifest.sha256:
        raise IntegrityError(
            f'sha256 mismatch for {manifest.artifact_id}: '
            f'expected {manifest.sha256}, got {actual_hash}'
        )
    return safe_path
```

### 15.3 Secret Management

All API keys and secrets **must** be externalized to environment variables. Never hardcode secrets in source files.

| Secret | Owner | Environment Variable |
|---|---|---|
| Groq API key | Model Router | `GROQ_API_KEY` |
| Anthropic API key | Gateway (Haiku adapter) + Orchestrator (Opus) | `ANTHROPIC_API_KEY` |
| Perplexity API key | Gateway (Perplexity adapter) | `PERPLEXITY_API_KEY` |
| Gateway local API token | Gateway + Desktop App | `GATEWAY_LOCAL_API_TOKEN` |
| Per-agent HMAC secrets | Orchestrator (all), Agents (own) | `AGENT_SECRET` / `AGENT_SECRETS` |
| Gateway → Orchestrator signing secret | Gateway + Orchestrator (in `AGENT_SECRETS` as `cosmic/gateway:1.0.0`) | `GATEWAY_SIGNING_SECRET` |
| Gateway internal service token | Gateway + Orchestrator | `GATEWAY_INTERNAL_TOKEN` |
| Credential encryption key | Gateway (Credential Manager) | `CREDENTIAL_ENCRYPTION_KEY` |
| OAuth client secrets (per provider) | Gateway (Credential Manager) | `OAUTH_GOOGLE_CLIENT_SECRET`, `OAUTH_GITHUB_CLIENT_SECRET`, etc. |
| OpenRouter API key | Gateway (Session Manager — embeddings) | `OPENROUTER_API_KEY` |

**Storage:** In development, secrets live in `.env` files (gitignored). In production, use a secrets manager or encrypted environment injection (Docker secrets, Kubernetes secrets, systemd `CredentialDirectory`). In the current bare-VM Supabase flow, shared provider keys are stored centrally in Supabase Vault and materialized into per-service env files at bootstrap time. The per-VM desktop-facing `GATEWAY_LOCAL_API_TOKEN` is stored in `public.user_vms.api_token` and installed into `gateway.env` by bootstrap.

**Scoping rule:** production env injection is per service/process, not one backend-wide env blob. Gateway, Model Router, Bridges, Orchestrator, and agents should each receive the smallest env surface they require. This reduces accidental secret exposure and matches the process boundaries defined in §9.

**Important distinction:** environment variables are for deploy-time static secrets/configuration. They are **not** the storage mechanism for user OAuth refresh tokens or channel device/session state:

- user OAuth tokens live in `gateway/credentials.db`, encrypted at rest
- channel/device auth such as Baileys multi-file state lives in the bridge's persistent `store/` path
- env vars hold keys, client secrets, shared service tokens, and filesystem references to those persistent stores

**Rotation:** HMAC agent secrets can be rotated by updating the orchestrator's `AGENT_SECRETS` map and each agent's `AGENT_SECRET` in a rolling deploy. The contract version mechanism (§21) ensures in-flight envelopes signed with the old key are drained before the old key is removed.

**Credential encryption key rotation:** Generate a new Fernet key, re-encrypt all stored refresh tokens under the new key (batch migration), then swap the env var. During migration, the Credential Manager should accept both old and new keys (try new first, fall back to old). See §22 for details.

---

## 16. Leader Election + Fencing

The orchestrator uses leader election with monotonic epoch/fencing tokens to prevent split-brain writes.

### 16.1 Leader Election via Redis

```python
# shared/leader.py
LEADER_KEY = 'orchestrator:leader'
EPOCH_KEY = 'orchestrator:epoch'
LEADER_TTL = 15
RENEW_EVERY = 5

ACQUIRE_SCRIPT = '''
local ok = redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
if ok then
    local epoch = redis.call('incr', KEYS[2])
    return epoch
else
    return 0
end
'''

async def acquire_leadership(instance_id: str, redis) -> int | None:
    result = await redis.eval(
        ACQUIRE_SCRIPT, 2,
        LEADER_KEY, EPOCH_KEY,
        instance_id, str(LEADER_TTL)
    )
    return int(result) if result else None

RENEW_SCRIPT = '''
if redis.call('get', KEYS[1]) == ARGV[1]
   and tostring(redis.call('get', KEYS[2])) == ARGV[2] then
    return redis.call('expire', KEYS[1], ARGV[3])
else return 0 end
'''

async def renew_leadership(instance_id: str, epoch: int, redis) -> bool:
    result = await redis.eval(
        RENEW_SCRIPT, 2,
        LEADER_KEY, EPOCH_KEY,
        instance_id, str(epoch), str(LEADER_TTL)
    )
    return bool(result)
```

### 16.2 Orchestrator Main Loop

```python
async def orchestrator_main(instance_id: str, redis):
    while True:
        epoch = await acquire_leadership(instance_id, redis)
        if epoch:
            logger.info(f'{instance_id} elected leader, epoch={epoch}')
            asyncio.create_task(renew_loop(instance_id, epoch, redis))
            await recover_orphaned_tasks(redis, epoch, self.secrets)
            await run_as_leader(epoch)
        else:
            await asyncio.sleep(LEADER_TTL)

async def renew_loop(instance_id: str, epoch: int, redis):
    while True:
        await asyncio.sleep(RENEW_EVERY)
        still_leader = await renew_leadership(instance_id, epoch, redis)
        if not still_leader:
            logger.error(f'Lost leadership (epoch={epoch}), halting dispatch')
            raise LeadershipLost()
```

### 16.3 Crash Recovery

**Why `suspended` is excluded from orphan recovery:** Suspended tasks are *intentionally* waiting — an agent serialized its state and is blocked on a reverse-task reply or user input. Redispatching a suspended task creates a fresh execution that knows nothing about the prior agent state, producing duplicate work or corrupt results. Suspended tasks have their own lifecycle (see §16.3a).

```python
async def recover_orphaned_tasks(redis, epoch, secrets):
    # NOTE: 'suspended' is intentionally excluded — see §16.3a for suspension lifecycle.
    # Redispatching a suspended task would start a fresh execution with no knowledge
    # of the agent's serialized state, causing duplicate work or data corruption.
    orphaned = db.execute('''
        SELECT task_id, recipient, priority FROM tasks
        WHERE status IN ('accepted', 'in_progress')
        AND updated_at < datetime('now', '-60 seconds')
    ''').fetchall()
    for task_id, recipient, priority in orphaned:
        logger.warning(f'Recovering orphaned task: {task_id}')
        task = load_task_envelope(task_id)
        task = prepare_for_redispatch(task, epoch, secrets)
        await dispatch(task, redis)

    # Separately: expire long-stale suspended tasks (see §16.3a)
    await expire_stale_suspensions(redis, epoch)
```

### 16.3a Suspension Lifecycle & Garbage Collection

Suspended tasks are legitimate — an agent is waiting for a reverse-task reply or user input. They must NOT be redispatched by orphan recovery (§16.3). However, suspensions cannot live forever. A user may never reply, an agent's runtime state in `runtime/state.db` may become stale, and the `user_input:requests` PEL can accumulate unbounded entries.

**Suspension timeout:** After `MAX_SUSPENSION_SEC` (default: 24 hours), the orchestrator expires the suspension. The task is cancelled — not retried — because the agent's serialized state is no longer trustworthy after that long.

```python
# shared/config.py
MAX_SUSPENSION_SEC = 86400       # 24 hours — suspended tasks expire after this

# agents/orchestrator/suspension.py
async def expire_stale_suspensions(redis, epoch):
    """Called by recover_orphaned_tasks() and periodically by the
    deferred check loop. Cancels suspended tasks that have been
    waiting longer than MAX_SUSPENSION_SEC.

    Does NOT redispatch — the agent's serialized state in runtime/state.db
    is stale after 24 hours. The task is terminated with SUSPENSION_EXPIRED."""
    stale = db.execute('''
        SELECT task_id, session_id, channel FROM tasks
        WHERE status = 'suspended'
        AND updated_at < datetime('now', '-' || ? || ' seconds')
    ''', [MAX_SUSPENSION_SEC]).fetchall()

    for task_id, session_id, channel in stale:
        logger.warning(f'Expiring stale suspension: {task_id} '
                       f'(suspended > {MAX_SUSPENSION_SEC}s)')

        # Mark task as failed
        db.execute('''
            UPDATE tasks SET status = 'failed', error_json = ?,
            completed_at = ?, updated_at = ?
            WHERE task_id = ?
        ''', [
            json.dumps({
                'code': 'SUSPENSION_EXPIRED',
                'retryable': False,
                'message': f'Task was suspended for over {MAX_SUSPENSION_SEC // 3600} hours '
                           f'without a reply. Cancelled to prevent stale state.',
                'next_action': 'notify_user',
            }),
            utcnow(), utcnow(), task_id,
        ])

        # Emit terminal event so Gateway notifies the user
        await emit_event(
            task_id=task_id,
            event_type='task.failed',
            payload={
                'error': {
                    'code': 'SUSPENSION_EXPIRED',
                    'message': 'This task was waiting for your reply but timed out '
                               'after 24 hours. You can re-request it.',
                },
            },
        )

        # Clean up any pending user_input:requests for this task
        # (Gateway will stop surfacing them)
```

**Configuration:**

```ini
MAX_SUSPENSION_SEC=86400         # 24 hours (default). Set lower in development.
```

**Lifecycle diagram:**

```
task.suspended (agent waiting for reply)
    │
    ├── Reply arrives within MAX_SUSPENSION_SEC
    │   → task.resumed → continues normally
    │
    └── No reply after MAX_SUSPENSION_SEC
        → expire_stale_suspensions() fires
        → task.failed (SUSPENSION_EXPIRED)
        → Gateway notifies user via channel adapter
        → User can re-request the original action
```

### 16.4 Stale Epoch Rejection + Redrive

When an agent receives a task with a stale `leader_epoch`, it acks the message (removes from pending), does NOT execute, and emits `task.rejected`. The new leader redispatches with the current epoch.

```python
# Agent-side
async def _reject_stale_epoch(self, task, msg_id, stream, current_epoch):
    await redis.xack(stream, 'workers', msg_id)
    await self.emit_event(
        task_id=task.task_id,
        event_type='task.rejected',
        payload={
            'reason': 'stale_epoch',
            'task_epoch': task.leader_epoch,
            'current_epoch': current_epoch,
        },
    )

# Orchestrator-side
async def on_event(event: EventEnvelope):
    if event.event_type == 'task.rejected':
        if event.payload.get('reason') == 'stale_epoch':
            task = await load_task_envelope(event.task_id)
            new_epoch = await get_current_epoch(redis)
            task = prepare_for_redispatch(task, new_epoch, self.secrets)
            # idempotency_key is UNCHANGED — stale reject = no execution happened
            await dispatch(task, redis)
```

---

## 17. Atomic Seq Allocation

`seq` on EventEnvelopes must be unique and monotonically increasing per task. With multiple worker instances, allocation must be atomic.

```python
# shared/events.py
SEQ_KEY_TTL_SEC = RESULT_TTL_SEC

async def next_seq(task_id: str, redis) -> int:
    return int(await redis.incr(f'event_seq:{task_id}'))

async def expire_seq_key(task_id: str, redis):
    await redis.expire(f'event_seq:{task_id}', SEQ_KEY_TTL_SEC)

async def emit_event(self, task_id: str, event_type: str, payload: dict):
    seq = await next_seq(task_id, self.redis)
    event = EventEnvelope(
        task_id=task_id,
        agent_id=self.agent_id,
        event_type=event_type,
        seq=seq,
        payload=payload,
        emitted_at=utcnow(),
    )
    # Trim stream to bounded size (old events archived to disk — see §12.8)
    msg_id = await self.redis.xadd(
        'streams:events',
        {'event': event.model_dump_json()},
        maxlen=EVENTS_STREAM_MAXLEN,
        approximate=True,
    )

    # Maintain per-task event index for O(1) replay (see §23.6)
    await self.redis.rpush(f'task_events:{task_id}', msg_id)

    if event_type in TERMINAL_EVENTS:
        await expire_seq_key(task_id, self.redis)
        # Expire the per-task index after same TTL as results
        await self.redis.expire(f'task_events:{task_id}', RESULT_TTL_SEC)
```

Never use local counters or timestamps as seq — they are not safe across instances.

---

## 18. Priority Fairness: Aging-Based Promotion

Strict `high → normal → low` polling starves low-priority tasks under sustained load. Aging promotes waiting tasks.

### 18.1 Aging Thresholds

```python
AGING_THRESHOLDS = {
    'low': 120,         # promote to normal after 2 min
    'normal': 60,       # promote to high after 1 min
}
```

### 18.2 Worker Loop Integration

The worker loop calls `_priority_order_with_aging()` before each consumption cycle. This method inspects `XPENDING` summaries (O(1) per call) to detect aged messages and reorders the consumption priority.

```python
async def _priority_order_with_aging(self) -> list[str]:
    """Check if lower-priority streams have messages that exceed aging
    thresholds. If so, consume from the aged stream first.

    Uses XPENDING summary form — O(1), returns min/max message IDs
    and pending count. Cost: 2 Redis calls per cycle (one per lower
    priority tier). Acceptable overhead for fairness guarantee."""
    for original, threshold in [
        ('low', AGING_THRESHOLDS['low']),
        ('normal', AGING_THRESHOLDS['normal']),
    ]:
        info = await self.redis.xpending(
            STREAMS[original], 'workers',
        )
        # info = {'pending': N, 'min': '<id>', 'max': '<id>', 'consumers': [...]}
        if info and info['pending'] > 0 and info['min']:
            oldest_ts_ms = int(info['min'].split('-')[0])
            age_sec = (time.time() * 1000 - oldest_ts_ms) / 1000
            if age_sec > threshold:
                # Aged message found — promote this stream to front of queue
                order = [original] + [
                    p for p in ['high', 'normal', 'low'] if p != original
                ]
                return order

    return ['high', 'normal', 'low']    # default strict ordering
```

**How it works with the worker loop (§8.4):** The worker loop calls `consume_order = await self._priority_order_with_aging()` and then iterates streams in the returned order. When a low-priority message has been pending for over 120 seconds, the worker consumes from `low` first, then `high`, then `normal`. This ensures no priority tier is permanently starved while preserving priority ordering under normal conditions.

**Performance impact:** Two `XPENDING` summary calls per iteration (O(1) each). Under typical load (sub-second iterations), this adds < 1ms overhead. The aging check only changes behavior when messages have been waiting for minutes — it does not affect the fast path.

---

## 19. DLQ + Retry Policy

### 19.1 Retry Behavior

Retry policy is per intent, declared in `agent_card.yaml` under `sla.retry_policy`.

| Error Code | Behavior |
|---|---|
| `TIMEOUT`, `NETWORK_ERROR`, `RATE_LIMITED` | Retryable — exponential backoff up to `backoff_max_sec`, up to `max_attempts` |
| `INVALID_INPUT`, `AUTH_ERROR`, `SCHEMA_VIOLATION` | Non-retryable — straight to DLQ |
| `DEADLINE_EXCEEDED` | Non-retryable — pre-flight rejection, never executed |

### 19.2 DLQ Routing

After `max_attempts`: `XADD streams:dlq` with full task envelope + error history. The DLQ record includes: original `task_id`, all attempt timestamps, all error codes, final error message. The DLQ is inspected manually or by a separate remediation agent.

---

## 20. Broadcast & Dynamic Capability Discovery

### 20.1 Pattern A: Capability Advertisement (Push — Default)

Agents publish to a shared stream when capabilities change. Orchestrator subscribes and updates the registry live. Zero latency on dispatch.

```python
await redis.xadd('streams:capability.updates', {
    'agent_id': 'cosmic/research-agent:1.0.0',
    'event': 'capability.added',
    'intent': 'research.summarize_pdf',
    'available_from': utcnow().isoformat()
})
```

### 20.2 Pattern B: Capability Query Broadcast (Pull — Dynamic Discovery)

Use only for genuinely unknown external agents at runtime.

```python
await redis.xadd('streams:broadcast', {
    'type': 'capability.query',
    'query_id': 'qry_abc123',
    'intent_needed': 'research.summarize_pdf',
    'reply_to': 'streams:cosmic/orchestrator:1.0.0:replies',
    'deadline_ts': (utcnow() + timedelta(seconds=5)).isoformat()
})
```

| Pattern | When to Use |
|---|---|
| **A (push)** | Your agents. Known at deploy time. 99% of cases. Zero dispatch latency. |
| **B (broadcast)** | External agents, plugins, dynamically loaded specialists. Adds ~deadline_ts latency. Always cache result back into SQLite. |

---

## 21. Rolling Deploy Compatibility

Safe deployment requires readers to be upgraded before writers. Never advance writers past what the oldest live reader can parse.

### 21.1 Version Configuration

`contract_version` on envelopes is a **dynamic string** set from config, not a hardcoded literal. This allows the same codebase to write different versions during a rolling deploy.

**Enforcement is at the model level.** Both `TaskEnvelope` and `EventEnvelope` have a `@field_validator` on `contract_version` that rejects any version not in `ACCEPTED_READ_VERSIONS` at **deserialization** time. This is the inbound (read) check.

**Outbound (write) validation** is a separate function called at dispatch time. This decouples the rules: during a rolling deploy, readers accept `{'1.6', '1.7'}` while writers are constrained to emit only `CURRENT_WRITE_VERSION`.

```python
# shared/config.py
CURRENT_WRITE_VERSION = '1.6'
ACCEPTED_READ_VERSIONS = {'1.6'}

def validate_outbound_version(envelope) -> None:
    """Called at dispatch time — ensures we only write the current version."""
    if envelope.contract_version != CURRENT_WRITE_VERSION:
        raise ContractVersionError(
            f'Outbound envelope has version {envelope.contract_version}, '
            f'but CURRENT_WRITE_VERSION is {CURRENT_WRITE_VERSION}'
        )
```

### 21.2 Three-Phase Deploy

| Phase | Writers | Readers | Purpose |
|---|---|---|---|
| **Phase 1** (deploy readers) | `CURRENT_WRITE_VERSION = '1.6'` | `ACCEPTED_READ_VERSIONS = {'1.6', '1.7'}` | All nodes can parse both versions |
| **Phase 2** (flip writers) | `CURRENT_WRITE_VERSION = '1.7'` | `ACCEPTED_READ_VERSIONS = {'1.6', '1.7'}` | New envelopes flow, old readers already handle them |
| **Phase 3** (narrow window) | `CURRENT_WRITE_VERSION = '1.7'` | `ACCEPTED_READ_VERSIONS = {'1.7'}` | Cluster fully on new version |

**Deploy checklist:**

1. Merge Phase 1 config. Deploy to all nodes. Verify no parse errors in logs.
2. Merge Phase 2 config. Deploy to all nodes. Verify warnings (not errors) in logs.
3. Once warning volume drops to zero: merge Phase 3 config. Deploy.
4. Done — cluster is fully on new version.

---

## 22. Credential Management

The Credential Manager is a module inside the Gateway that handles user-connected provider accounts (Google, GitHub, etc.), OAuth token lifecycle, and secure credential resolution for agents. It is the single owner of all user OAuth credentials in the system.

**Design principle:** Agents call external provider APIs directly using short-lived access tokens passed in `TaskEnvelope.input.auth`. Refresh tokens never leave the Gateway. The orchestrator resolves credentials at dispatch time via an internal Gateway endpoint. No proxy layer — agents are first-party code on the same VM, and the trust boundary is the VM itself.

### 22.1 Architecture Overview

```
Desktop App (Settings Panel)
    │
    │  "Connect Google Account"
    ▼
Gateway (/auth/connect/google)
    │
    │  OAuth PKCE + state
    ▼
Provider (Google, GitHub, etc.)
    │
    │  Authorization code callback
    ▼
Gateway (/auth/callback/google)
    │
    │  Exchange code → tokens
    │  Encrypt refresh token → credentials.db
    │  Return credential_ref to desktop app
    ▼
credentials.db
    │
    │  [Later, at task dispatch time]
    │
Orchestrator
    │  "This intent needs google/documents scope"
    │  POST /internal/credentials/resolve
    ▼
Gateway (internal endpoint)
    │  Validate scope, refresh if needed
    │  Return short-lived access token
    ▼
Orchestrator
    │  Place in TaskEnvelope.input.auth
    │  Dispatch via Redis
    ▼
Agent
    │  self.auth.access_token
    │  Call Google Docs API directly
    ▼
Google Docs API
```

### 22.2 Data Model

All credential data lives in `gateway/credentials.db` — separate from `sessions.db` for security isolation. This database **must** be on a persistent volume in containerized deployments.

```sql
-- gateway/credentials.db

-- Connected provider accounts
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,           -- 'acc_a1b2c3'
    user_id TEXT NOT NULL,                 -- always same value in single-user-per-VM deployment; exists for future extensibility
    provider TEXT NOT NULL,                -- 'google', 'github', 'microsoft'
    provider_account_id TEXT NOT NULL,     -- provider's own account ID
    email TEXT,                            -- display email for account selection UI
    display_name TEXT,                     -- 'Work Gmail', 'Personal Google'
    is_primary BOOLEAN DEFAULT FALSE,      -- default account for this provider
    status TEXT DEFAULT 'active',          -- 'active', 'revoked', 'expired'
    connected_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    UNIQUE (user_id, provider, provider_account_id)
);

-- Encrypted credentials per account
CREATE TABLE credentials (
    credential_ref TEXT PRIMARY KEY,       -- 'cred_9f3a...' — opaque reference
    account_id TEXT NOT NULL,
    granted_scopes TEXT NOT NULL,           -- JSON array of granted OAuth scopes
    encrypted_refresh_token BLOB NOT NULL,  -- Fernet-encrypted refresh token
    access_token_cache TEXT,               -- cached access token (encrypted)
    access_token_expires_at TIMESTAMP,     -- when cached access token expires
    revoked_at TIMESTAMP,                  -- set on disconnect/revocation
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

-- Resource-to-account bindings (learned over time)
CREATE TABLE resource_bindings (
    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,            -- 'google_doc', 'github_repo', etc.
    external_id TEXT NOT NULL,             -- provider's resource ID
    display_name TEXT,                     -- 'Project Proposal', 'cosmic-os'
    account_id TEXT NOT NULL,
    last_accessed_at TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id),
    UNIQUE (resource_type, external_id, account_id)
);

CREATE INDEX idx_bindings_name ON resource_bindings(resource_type, display_name);

-- Credential usage audit log
CREATE TABLE credential_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    provider TEXT NOT NULL,
    scopes_used TEXT NOT NULL,             -- JSON array of scopes used
    action TEXT NOT NULL,                  -- 'resolve', 'refresh', 'revoke'
    result TEXT NOT NULL,                  -- 'success', 'failed', 'expired'
    FOREIGN KEY (credential_ref) REFERENCES credentials(credential_ref)
);

CREATE INDEX idx_audit_task ON credential_audit(task_id);
CREATE INDEX idx_audit_credential ON credential_audit(credential_ref);
```

### 22.3 Orchestrator Credential Resolution (Dispatch-Time)

When the orchestrator decomposes a user request into sub-tasks, it checks each target agent's `agent_card.yaml` for `auth_requirements` (see §6.2). If the intent requires provider credentials, the orchestrator resolves them before dispatch.

**Orchestrator dispatch flow (updated from §10):**

```python
async def dispatch_to_agent(self, intent: str, input_data: dict,
                             session_context: dict, redis):
    # 1. Look up routing.yaml → target agent_id
    routing = self.routing_config[intent]
    agent_id = routing['agent']

    # 2. Check agent_card for auth_requirements
    card = self.registry.get_card(agent_id)
    auth_req = card.get('auth_requirements', {}).get(intent)

    if auth_req:
        # 3. Resolve account + credential
        credential = await self._resolve_credential(
            auth_req, session_context, input_data
        )
        if credential is None:
            # No account connected or ambiguous — escalate to user
            return await self._escalate_account_connection(
                auth_req, session_context
            )
        input_data['auth'] = credential

    # 4. Build and dispatch TaskEnvelope as normal
    task = TaskEnvelope(
        intent=intent,
        input=input_data,
        recipient=agent_id,
        ...
    )
    await dispatch(task, redis)

async def _resolve_credential(self, auth_req: dict,
                                session_context: dict,
                                input_data: dict) -> dict | None:
    """Call Gateway internal API to resolve credential."""
    resp = await self.gateway_client.post(
        f'{GATEWAY_INTERNAL_URL}/internal/credentials/resolve',
        json={
            'provider': auth_req['provider'],
            'required_scopes': auth_req['scopes'],
            'user_id': session_context['user_id'],
            'account_id': session_context.get('account_id'),  # if user specified
            'resource_hint': input_data.get('doc_id') or input_data.get('repo'),
        },
        headers={'X-Internal-Token': GATEWAY_INTERNAL_TOKEN},
    )
    if resp.status_code == 200:
        return resp.json()
        # Returns: { credential_ref, access_token, provider, scopes, expires_at }
    elif resp.status_code == 404:
        return None  # no matching account/credential
    elif resp.status_code == 403:
        return None  # scopes not granted — needs re-consent
    else:
        raise CredentialResolveError(resp.text)
```

### 22.4 Account Resolution Policy

When the orchestrator needs to determine which provider account to use, it follows a deterministic policy. The orchestrator owns this decision — agents never guess accounts.

**Resolution order (first match wins):**

| Priority | Condition | Action |
|---|---|---|
| 1 | User explicitly names account ("use my work Gmail") | Orchestrator extracts account hint from intent parsing, passes `account_id` to resolve |
| 2 | User names a resource ("update Project Proposal doc") | Orchestrator dispatches `{agent}.resolve_resource` intent to the agent (e.g., `docs.resolve_resource`). Agent searches across connected accounts, returns match + account binding. |
| 3 | Session context has an active account for this provider | Use the account from the current session's last successful provider interaction |
| 4 | Exactly one connected account for the provider | Use it — no ambiguity |
| 5 | Multiple connected accounts, ambiguous | Orchestrator escalates to user via `user.input_required` with the list of connected accounts |
| 6 | No account connected for the provider | Orchestrator escalates to user via `user.input_required` asking to connect an account |

**Resource resolution example (Priority 2):**

```python
# User says: "Update the Project Proposal doc"
# Orchestrator doesn't know which account or doc_id

# Step 1: Check if resource binding already exists
binding_resp = await self.gateway_client.post(
    f'{GATEWAY_INTERNAL_URL}/internal/credentials/lookup-resource',
    json={
        'resource_query': 'Project Proposal',
        'resource_type': 'google_doc',
        'provider': 'google',
    },
    headers={'X-Internal-Token': GATEWAY_INTERNAL_TOKEN},
)

if binding_resp.status_code == 200:
    # Binding found — resolve credential for that specific account
    binding = binding_resp.json()
    credential = await self._resolve_credential(
        auth_req, session_context,
        input_data={'doc_id': binding['resource_id']},
    )
    # → proceeds with single credential, done
else:
    # Step 2: No binding — search across accounts one at a time
    accounts = await self.gateway_client.get(
        f'{GATEWAY_INTERNAL_URL}/internal/credentials/accounts/google',
        headers={'X-Internal-Token': GATEWAY_INTERNAL_TOKEN},
    ).json()  # [{ account_id, email, ... }]

    all_matches = []
    for account in accounts:
        # Resolve single credential for this account
        cred = await self._resolve_credential(
            auth_req, session_context,
            input_data={'account_id': account['account_id']},
        )
        if cred is None:
            continue

        # Dispatch resolve_resource with SINGLE credential (input.auth is always a dict)
        result = await self._dispatch_and_wait(
            intent='docs.resolve_resource',
            input={
                'query': 'Project Proposal',
                'resource_type': 'google_doc',
                'account_id': account['account_id'],
                'auth': cred,  # single credential dict — same schema as §7.3
            },
            recipient='cosmic/docs-agent:2.1.0',
        )
        all_matches.extend(result.get('matches', []))

# Step 3: If exactly 1 match → proceed with that account + doc_id
#          If multiple → orchestrator asks user (shows title + account + date)
#          If none → orchestrator tells user "document not found"

# Step 4: Gateway stores the binding in resource_bindings table
#          Next time, Step 1 resolves instantly via lookup
```

**Why sequential per-account, not batch?** `input.auth` is always a single dict (§7.3). The agent base class does `self.auth = task.input.pop('auth', None)` expecting one credential. Passing a list would crash. Sequential dispatch keeps the contract clean — the agent gets one credential, searches one account, returns results. The orchestrator aggregates. For a personal assistant, users have 1-3 accounts per provider — the overhead is negligible.

### 22.5 Token Refresh Mid-Task

Access tokens are short-lived (5-15 minutes). Most tasks complete within this window. If a task runs longer and the token expires, the agent uses the existing suspension mechanism (§13.1):

```python
# Inside agent's execute() method — provider API call fails with 401
async def execute(self, task: TaskEnvelope):
    try:
        result = await self.call_provider_api(...)
    except ProviderAuthError:
        # Token expired mid-task — request refresh via orchestrator
        await self._send_reverse_task(
            intent='orchestrator.refresh_credential',
            input={
                'credential_ref': self.auth['credential_ref'],
                'provider': self.auth['provider'],
                'parent_task_id': task.task_id,
            },
        )
        # Agent suspends (task.suspended event emitted)
        # State serialized to runtime/state.db
        # Orchestrator calls Gateway to refresh, gets new token
        # Orchestrator resumes agent with:
        #   intent: 'agent.resume'
        #   input: { task_id, auth: { new access_token, ... } }
        return  # will be resumed
```

**Orchestrator-side refresh handling:**

```python
async def on_refresh_credential(self, event):
    credential_ref = event.input['credential_ref']
    # Call dedicated refresh endpoint — NOT /resolve (different operation, different schema)
    resp = await self.gateway_client.post(
        f'{GATEWAY_INTERNAL_URL}/internal/credentials/refresh',
        json={
            'credential_ref': credential_ref,
        },
        headers={'X-Internal-Token': GATEWAY_INTERNAL_TOKEN},
    )
    # Returns: { credential_ref, access_token, provider, scopes, expires_at }
    new_auth = resp.json()

    # Resume agent with fresh token
    await self._resume_agent(
        task_id=event.input['parent_task_id'],
        input={'auth': new_auth},
    )
```

**Why a separate `/refresh` endpoint?** Resolve and refresh are different operations. `/resolve` takes `provider + scopes + user_id` and finds or creates a credential. `/refresh` takes an existing `credential_ref` and uses the stored refresh token to get a new access token. Overloading one endpoint with two incompatible schemas creates a fragile branching mess. Two endpoints, two schemas, zero ambiguity.

### 22.6 OAuth Flow

OAuth flows are initiated from the desktop app's settings panel and handled entirely by the Gateway.

```
Desktop App                    Gateway                         Provider
    │                            │                                │
    │  GET /auth/connect/google  │                                │
    │  ──────────────────────►   │                                │
    │                            │  Generate PKCE verifier+challenge
    │                            │  Generate state parameter      │
    │                            │  Store state in session         │
    │  ◄──────────────────────   │                                │
    │  302 Redirect to provider  │                                │
    │                            │                                │
    │  ─────────────────────────────────────────────────────────► │
    │  User consents in browser  │                                │
    │  ◄───────────────────────────────────────────────────────── │
    │  Redirect to callback      │                                │
    │                            │                                │
    │  GET /auth/callback/google?code=...&state=...               │
    │  ──────────────────────►   │                                │
    │                            │  Verify state parameter        │
    │                            │  Exchange code + PKCE verifier  │
    │                            │  ──────────────────────────►   │
    │                            │  ◄──────────────────────────   │
    │                            │  Receive access + refresh token │
    │                            │  Encrypt refresh token          │
    │                            │  Store in credentials.db        │
    │                            │  Create account + credential_ref│
    │  ◄──────────────────────   │                                │
    │  { account_id, email,      │                                │
    │    provider, scopes,       │                                │
    │    credential_ref }        │                                │
```

**Provider adapter pattern:** Each provider (Google, GitHub, Microsoft) has a small adapter that knows the provider's OAuth endpoints, scopes format, and token refresh mechanism. New providers are added by implementing the adapter interface.

```python
# gateway/credentials/providers.py
class ProviderAdapter:
    """Base class for OAuth provider adapters."""
    provider: str
    authorize_url: str
    token_url: str
    revoke_url: str | None

    def get_authorize_params(self, scopes: list[str], state: str,
                              code_challenge: str) -> dict: ...
    def exchange_code(self, code: str, code_verifier: str) -> TokenResponse: ...
    def refresh_token(self, refresh_token: str) -> TokenResponse: ...
    def revoke_token(self, token: str) -> bool: ...

class GoogleAdapter(ProviderAdapter):
    provider = 'google'
    authorize_url = 'https://accounts.google.com/o/oauth2/v2/auth'
    token_url = 'https://oauth2.googleapis.com/token'
    revoke_url = 'https://oauth2.googleapis.com/revoke'
    ...

class GitHubAdapter(ProviderAdapter):
    provider = 'github'
    authorize_url = 'https://github.com/login/oauth/authorize'
    token_url = 'https://github.com/login/oauth/access_token'
    revoke_url = None  # GitHub doesn't support programmatic revocation
    ...
```

### 22.7 Token Encryption

Refresh tokens are encrypted at rest using Fernet symmetric encryption. The encryption key is loaded from the `CREDENTIAL_ENCRYPTION_KEY` environment variable.

```python
# gateway/credentials/encryption.py
from cryptography.fernet import Fernet

FERNET_KEY = os.environ['CREDENTIAL_ENCRYPTION_KEY']
cipher = Fernet(FERNET_KEY)

def encrypt_token(plaintext: str) -> bytes:
    return cipher.encrypt(plaintext.encode('utf-8'))

def decrypt_token(ciphertext: bytes) -> str:
    return cipher.decrypt(ciphertext).decode('utf-8')
```

**Why Fernet:** Simple, authenticated encryption (AES-128-CBC + HMAC-SHA256). No IV/nonce management. Built into the `cryptography` library. Sufficient for the single-user-per-VM deployment model — one key per VM, one user per VM. If you move to multi-node or multi-tenant, swap to a KMS envelope encryption pattern.

### 22.8 Revocation & Disconnect

When a user disconnects a provider account from the settings panel:

1. Gateway calls the provider's revoke endpoint (if supported).
2. Gateway marks the credential as `revoked` in `credentials.db` (`revoked_at = now()`).
3. Gateway marks the account as `revoked` in `accounts` table.
4. Any future `resolve` calls for this credential_ref return `403 REVOKED`.
5. In-flight tasks using this credential will fail with `AUTH_ERROR` (non-retryable per §19.1) on their next provider API call.

```python
# gateway/credentials/manager.py
async def disconnect_account(account_id: str):
    account = db.execute(
        'SELECT * FROM accounts WHERE account_id = ?', [account_id]
    ).fetchone()

    creds = db.execute(
        'SELECT * FROM credentials WHERE account_id = ? AND revoked_at IS NULL',
        [account_id]
    ).fetchall()

    for cred in creds:
        # Attempt provider-side revocation (best-effort)
        adapter = get_provider_adapter(account['provider'])
        if adapter.revoke_url:
            refresh_token = decrypt_token(cred['encrypted_refresh_token'])
            adapter.revoke_token(refresh_token)

        # Mark revoked in DB
        db.execute('''
            UPDATE credentials SET revoked_at = ?, updated_at = ?
            WHERE credential_ref = ?
        ''', [utcnow(), utcnow(), cred['credential_ref']])

    db.execute('''
        UPDATE accounts SET status = 'revoked', updated_at = ?
        WHERE account_id = ?
    ''', [utcnow(), account_id])

    # Audit
    db.execute('''
        INSERT INTO credential_audit
        (timestamp, task_id, agent_id, credential_ref, provider, scopes_used, action, result)
        VALUES (?, 'system', 'system', ?, ?, '[]', 'revoke', 'success')
    ''', [utcnow(), creds[0]['credential_ref'] if creds else 'none', account['provider']])
```

### 22.9 Credential Flow Diagram (Full Runtime)

```
User: "Add a conclusion to the Project Proposal doc"
    │
    ▼
Desktop App → Gateway → Model Router → route=opus
    │
    ▼
Gateway → Orchestrator (TaskEnvelope: orchestrator.process)
    │
    ▼
Orchestrator decomposes:
    intent = docs.edit
    agent  = cosmic/docs-agent:2.1.0
    │
    ├── Check agent_card: auth_requirements.docs.edit
    │   → provider: google, scopes: [documents]
    │
    ├── Account resolution (§22.4):
    │   "Project Proposal" → resource_bindings lookup
    │   → Found: doc_id=xyz, account_id=acc_work
    │
    ├── POST /internal/credentials/resolve
    │   { provider: google, account_id: acc_work,
    │     required_scopes: [documents] }
    │   → { credential_ref, access_token (5min TTL),
    │       provider, scopes, expires_at }
    │
    ├── Build TaskEnvelope:
    │   input: {
    │     query: "Add a conclusion...",
    │     doc_id: "xyz",
    │     auth: { credential_ref, access_token, ... }
    │   }
    │
    ▼
Dispatch via Redis → docs_agent
    │
    ├── handle() strips input.auth → self.auth
    ├── execute() reads self.auth.access_token
    ├── Calls Google Docs API directly
    ├── self.auth = None (cleared)
    │
    ▼
EventEnvelope (task.completed) — NO auth data in payload
    │
    ▼
Orchestrator → Gateway → Desktop App
    "Done — conclusion added to Project Proposal"
```

### 22.10 Hard Rules

These rules are non-negotiable. Violating any of them is a security incident.

1. **Refresh tokens never leave `credentials.db`.** Only the Gateway Credential Manager reads them. Not in Redis, not in TaskEnvelopes, not in events, not in logs.
2. **Access tokens only appear in `TaskEnvelope.input.auth`.** The agent base class strips this field before any serialization (see §12.6). Never in EventEnvelopes, artifacts, `store/`, `runtime/`, or `learnings.md`.
3. **`credential_audit` logs `credential_ref`, never token values.** Audit by reference, not by secret.
4. **Agents never guess accounts.** Account resolution is orchestrator-owned (§22.4). If ambiguous, the orchestrator asks the user.
5. **Revocation is immediate.** When a user disconnects an account, the credential is marked revoked in the DB. Future resolve calls fail. In-flight tasks fail on next provider API call.
6. **OAuth client secrets are env vars.** Never in source code, never in `credentials.db`, never in agent folders.
7. **One credential_ref per (account, agent, scope-set).** No shared "global token" across agents. If two agents need the same provider, each gets its own credential_ref with its own scope validation.
8. **Channel runtime auth is not Credential Manager data.** Device/session state for channel bridges (for example, Baileys multi-file auth) does **not** belong in `gateway/credentials.db`. `credentials.db` is only for provider OAuth credentials. Channel/device auth lives in the owning bridge's persistent `store/`.

---

## 23. Session & Memory Management

The Session Manager is a module inside the Gateway that creates a perpetual conversational experience. It assembles context for every query by combining today's conversation with retrieved long-term memories, manages daily session lifecycles, and handles context window pruning and compaction.

**Design principle:** Three separate memory layers with different lifecycles — today's conversation (short-term, compactable), retrieved memories (long-term, never compacted, retrieved fresh each turn), and task execution (isolated, retrievable on demand). All LLM backends (Opus, Haiku, Perplexity) receive the same assembled context — the user gets a consistent assistant regardless of which model answers. The entire memory store (SQLite, .md files, Qdrant) serves a single user per the deployment model — no user-scoped queries or tenant filtering needed.

### 23.1 Architecture Overview

```
User sends message
    │
    ▼
Gateway receives message
    │
    ▼
Session Manager: assemble_context()
    │
    ├── 1. Load today's conversation from sessions.db
    │       (pruned to fit context window — oldest messages drop off)
    │
    ├── 2. If mid-day compaction has occurred:
    │       prepend compacted_summary to conversation
    │
    ├── 3. Retrieve memories from Qdrant hybrid search (dense + sparse vectors)
    │       (10-12k token budget, ranked by relevance)
    │
    ├── 4. Deduplicate memories by ID
    │       (no memory repeated within same context)
    │
    └── 5. Return assembled_context:
            { memories, conversation, compacted_summary }
    │
    ▼
Model Router classifies (using assembled context)
    │
    ▼
Route to Opus / Haiku / Perplexity
    (all receive the same assembled_context)
```

### 23.2 Session Lifecycle

Each day is a session. The user sees one perpetual conversation — session boundaries are transparent.

**Authoritative timezone rule:** "4:00 AM" always means 4:00 AM in the user's persisted local IANA timezone, not the VM's timezone. The desktop reports this timezone to the Gateway on login/startup/resume and whenever the OS timezone changes. The Gateway persists that value and the Session Manager evaluates rollover boundaries against it. If no desktop timezone has ever been reported yet, the system may use a configured fallback only until the first desktop sync occurs.

**Implementation rule:** the 4 AM rollover is a Gateway-owned scheduled event, not a machine-level `crontab` entry. It uses the same persisted user-timezone snapshot as the Cron Manager, is visible to Gateway observability, and fires the `session.reset` hook path rather than relying on an external OS scheduler.

```
                    4:00 AM                           4:00 AM
    ─────────────────┼─────────────────────────────────┼──────────
    Session A        │  Session B (today)               │  Session C
                     │                                  │
    At boundary:     │                                  │
    1. Force compact │                                  │
       Session A     │                                  │
    2. Finalize      │                                  │
       transcript    │                                  │
       → logs/       │                                  │
         sessions/   │                                  │
    3. Write summary │                                  │
       as memory     │                                  │
       → memory/     │                                  │
         sessions/   │                                  │
       → Qdrant      │                                  │
    4. Start fresh   │                                  │
       Session B     │                                  │
```

**Session lifecycle:**

| Event | What Happens |
|---|---|
| App startup | Gateway loads or creates today's session. Desktop app receives the tail of today's conversation history for display. |
| Each message | Session Manager assembles context (conversation + memories). Message and response stored in `sessions.db`. After the SQLite write succeeds, the transcript writer appends a rendered entry to `logs/sessions/YYYY-MM-DD.md`. |
| Context at 70% | Mid-day compaction triggers: older messages summarized, summary replaces them in context. Conversation continues with `[compacted summary] + [recent messages]`. |
| 4:00 AM | Forced compaction of remaining session. The previous day's transcript in `logs/sessions/` is finalized. The compacted summary is written as a memory to `memory/sessions/` + Qdrant. New session ID created. |

### 23.3 Context Window Management

Two mechanisms keep the context window under control:

**Pruning (continuous):** As new messages arrive, the oldest messages in the context window are pruned — they stop being sent to the LLM. Pruned messages remain in `sessions.db` for full history browsing in the desktop app UI. Pruning is lightweight and always running.

**Compaction (triggered):** When context usage hits 70% of the target model's context window, compaction runs:

1. Extract memories from the conversation before summarizing (important facts, decisions, outcomes are written to the memory store at full fidelity)
2. Send the older conversation messages to a fast/cheap model (Claude Haiku 4.5 or Sonnet — not Opus) with a summarization prompt
3. The summary replaces the older messages in the session context
4. Conversation continues with: `[compacted summary] + [recent messages since compaction]`

```python
# gateway/session/compaction.py

async def check_and_compact(session_id: str, context_tokens: int,
                             model_context_window: int):
    threshold = model_context_window * 0.70
    if context_tokens < threshold:
        return  # no compaction needed

    # Step 1: Extract memories before compacting
    messages = await get_compactable_messages(session_id)
    await extract_and_store_memories(messages)

    # Step 2: Summarize via cheap/fast model (NOT Opus)
    summary = await compaction_llm.summarize(
        messages=messages,
        instruction='Summarize this conversation preserving key decisions, '
                    'facts, user preferences, and action items.',
        max_output_tokens=4000,
    )

    # Step 3: Replace messages with summary in session
    await store_compacted_summary(session_id, summary)
    db.execute('''
        UPDATE sessions SET compaction_count = compaction_count + 1,
        compacted_summary = ?, updated_at = ?
        WHERE session_id = ?
    ''', [summary, utcnow(), session_id])
```

**Token budget allocation per LLM call:**

```
Total context window (model-specific)
│
├── System prompt:              ~1-2k tokens
├── Retrieved memories:         10-12k tokens (fixed budget)
├── Compaction reserve:         ~4-6k tokens (space for compaction output)
├── Output reserve:             ~4k tokens (for model's response)
└── Today's conversation:       fills remaining space
    └── Compaction triggers at 70% of THIS allocation
```

**Why a compaction reserve?** The compaction process itself produces a summary (2-4k tokens) that replaces the older messages. The reserve ensures there's space for this summary plus headroom for incoming messages while compaction runs. This matches the pattern used by Claude Code's auto-compression.

### 23.4 Memory Retrieval

On every turn, the Session Manager retrieves relevant memories from the long-term store using hybrid search. Memories are assembled fresh each turn — they are NOT part of the conversation that gets compacted.

**Retrieval pipeline:**

```
User query
    │
    ▼
Embed query via Qwen3-embedding-8b (OpenRouter API)
    │   dense vector (semantic meaning)
    │
    ▼
Generate sparse vector for query (Qdrant FastEmbed / SPLADE)
    │   sparse vector (exact keyword matching)
    │
    ▼
Qdrant hybrid search (single request via Query API)
    │
    ├── Dense vector search → semantic similarity candidates
    ├── Sparse vector search → keyword/term match candidates
    ├── Server-side Reciprocal Rank Fusion (RRF) → merged ranking
    │
    ▼
Post-retrieval re-ranking
    │
    ├── Recency weight: recent memories score higher (decay curve)
    ├── Source priority: agent_notes > user_data
    ├── Deduplicate by memory_id (no repeats in context)
    │
    ▼
Select top memories within 10-12k token budget
    │
    ▼
Format as memory block in prompt
```

**Why Qdrant-native hybrid search instead of a separate BM25 index over .md files?**

1. **Single query, single system.** Qdrant 1.10+ supports dense and sparse vectors on the same collection with server-side fusion via its Query API. No external BM25 library, no file scanning at query time, no client-side result merging.
2. **Sync guarantee.** Both vectors (dense + sparse) are written atomically in the same `qdrant.upsert()` call (see §23.5). They are always in sync — there is no window where a memory exists in one index but not the other. A separate BM25 index over `.md` files creates a sync gap: the file exists but the index hasn't been rebuilt, or the index contains a deleted memory.
3. **Performance.** BM25 over raw `.md` files requires scanning and tokenizing files at query time. After months of daily session summaries, task summaries, and agent notes, this directory grows to thousands of files. Qdrant's sparse vector index is pre-built and serves results in milliseconds regardless of collection size.
4. **Operational simplicity.** One system to back up, restore, and monitor. If Qdrant data is lost, the rebuild process (§23.5a) re-generates both dense and sparse vectors from the `.md` source of truth in a single pass.

**Qdrant collection configuration:**

```python
# gateway/session/memory_retriever.py

from qdrant_client import QdrantClient, models

async def create_memory_collection(qdrant: QdrantClient):
    """Create the memories collection with both dense and sparse vector support.
    Called once at Gateway startup. Idempotent — skips if collection exists."""
    qdrant.create_collection(
        collection_name='memories',
        vectors_config={
            'dense': models.VectorParams(
                size=1024,                          # Qwen3-embedding-8b dimension
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            'sparse': models.SparseVectorParams(
                modifier=models.Modifier.IDF,       # IDF weighting (BM25-style)
            ),
        },
    )

async def hybrid_search(qdrant: QdrantClient, query_dense: list[float],
                         query_sparse: models.SparseVector,
                         limit: int = 20,
                         type_filter: list[str] | None = None) -> list:
    """Qdrant-native hybrid search using the Query API.
    Dense + sparse vectors fused server-side via RRF."""

    filter_condition = None
    if type_filter:
        filter_condition = models.Filter(
            must=[models.FieldCondition(
                key='type',
                match=models.MatchAny(any=type_filter),
            )]
        )

    results = qdrant.query_points(
        collection_name='memories',
        prefetch=[
            models.Prefetch(
                query=query_dense,
                using='dense',
                limit=limit * 2,
                filter=filter_condition,
            ),
            models.Prefetch(
                query=query_sparse,
                using='sparse',
                limit=limit * 2,
                filter=filter_condition,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
    )
    return results.points
```

**Memory types and their priority:**

| Type | Source | Priority | Description |
|---|---|---|---|
| Compacted session summaries | `memory/sessions/*.md` | Normal | Past day summaries — broad conversational context |
| Agent notes | `memory/agent_notes/*/learnings.md` | **High** | Agent-curated facts: user preferences, task-specific notes, learned patterns |
| User data | `memory/user_data/` | Normal | Indexed emails, files, documents — provides grounding |
| Task summaries | `memory/tasks/*.md` | Normal | Past task results — retrievable context about completed work |

**Agent notes are prioritized over user data.** Agent notes are curated, high-signal information that the system has already processed and found important. Raw user data is bulk context that needs relevance ranking to surface the right pieces.

**Critical rule: memories are excluded from compaction input.** When compaction runs, it only sees the raw conversation messages. The retrieved memories block is stripped before the compaction LLM call. This prevents a feedback loop: memory retrieved → included in context → compacted into summary → that summary becomes a memory → retrieved again → compacted again. Memories persist at source fidelity and are re-retrieved each turn based on relevance.

### 23.5 Memory Storage: .md Files as Source of Truth

All memories are stored as `.md` files in the `memory/` directory tree. Qdrant is the **index**, not the source of truth. If Qdrant data is lost, it is rebuilt by re-embedding the `.md` files.

**File format:**

```markdown
<!-- memory/sessions/2025-01-15.md -->
---
memory_id: mem_sess_20250115
type: session_summary
date: 2025-01-15
token_count: 1850
tags: [architecture, oauth, credential-management]
---

# Session Summary — January 15, 2025

User worked on the COSMIC multi-agent architecture. Key decisions:
- Credential Manager lives inside the Gateway as a module
- Agents receive short-lived access tokens in TaskEnvelope.input.auth
- Refresh tokens never leave the Gateway
- Account resolution is orchestrator-owned

User preferences noted:
- Prefers simple solutions over enterprise patterns
- Wants implementation-ready specs, not abstract descriptions
```

```markdown
<!-- memory/agent_notes/docs_agent/learnings.md -->
---
memory_id: mem_agent_docs_001
type: agent_note
agent_id: cosmic/docs-agent:2.1.0
updated_at: 2025-01-15
tags: [user-preference, formatting]
---

# Docs Agent — Learnings

## User Preferences
- User prefers APA citation format
- User wants section headings in title case
- Default Google account for docs: work@company.com
```

```markdown
<!-- memory/tasks/tsk_abc123.md -->
---
memory_id: mem_task_tsk_abc123
type: task_summary
task_id: tsk_abc123
agent_ids: [cosmic/research-agent:1.0.0, cosmic/docs-agent:2.1.0]
completed_at: 2025-01-15T14:30:00Z
tags: [research, quantum-computing]
---

# Task: Research quantum computing advances

## Result
Found 5 papers on recent quantum error correction breakthroughs.
Summary delivered to user. Key sources: arxiv:2401.xxxxx, arxiv:2401.yyyyy.

## Artifacts
- runs/artifacts/tsk_abc123/summary.md
- runs/artifacts/tsk_abc123/citations.json
```

**Memory write pipeline:**

```python
# gateway/session/memory_writer.py

async def write_memory(memory_id: str, memory_type: str, content: str,
                        metadata: dict):
    # 1. Write .md file (source of truth)
    path = get_memory_path(memory_type, metadata)
    write_md_file(path, memory_id, memory_type, metadata, content)

    # 2. Generate both vectors
    dense_vector = await embed_text(content)              # Qwen3-embedding-8b via OpenRouter
    sparse_vector = await generate_sparse_vector(content) # FastEmbed SPLADE or BM25 tokenizer

    # 3. Atomic upsert — both vectors written in the same call.
    #    There is no window where a memory has a dense vector but no sparse
    #    vector, or vice versa. Hybrid search always sees consistent state.
    qdrant.upsert(
        collection_name='memories',
        points=[
            models.PointStruct(
                id=memory_id,
                vector={
                    'dense': dense_vector,
                    'sparse': sparse_vector,
                },
                payload={
                    'type': memory_type,
                    'date': metadata.get('date'),
                    'tags': metadata.get('tags', []),
                    'path': str(path),
                    'token_count': count_tokens(content),
                    'content': content,   # stored for retrieval without filesystem read
                },
            ),
        ],
    )
```

**Sparse vector generation:**

```python
# gateway/session/memory_retriever.py

from fastembed import SparseTextEmbedding

sparse_model = SparseTextEmbedding(model_name='Qdrant/bm25')

async def generate_sparse_vector(text: str) -> models.SparseVector:
    """Generate a sparse vector using FastEmbed's BM25 implementation.
    This runs locally — no API call. FastEmbed handles tokenization,
    term frequency, and IDF weighting. The resulting sparse vector
    is indexed by Qdrant alongside the dense vector."""
    embeddings = list(sparse_model.embed([text]))
    return models.SparseVector(
        indices=embeddings[0].indices.tolist(),
        values=embeddings[0].values.tolist(),
    )
```

### 23.5a Memory Sync Guarantees & Qdrant Rebuild

`.md` files are the source of truth (§23.5 rule 6). Qdrant is the index. The system guarantees consistency between them through three mechanisms:

**1. Atomic write path.** Every `write_memory()` call writes the `.md` file first, then upserts both vectors into Qdrant. If the Qdrant upsert fails (process crash, disk full), the `.md` file exists but the index is stale. The rebuild process (below) detects and fixes this. The reverse cannot happen — Qdrant never receives a point without the `.md` file existing first.

**2. `memory_id` as the join key.** The `.md` file's frontmatter `memory_id` and the Qdrant point `id` are always identical. This enables:
- Detecting orphaned Qdrant points (point exists, no `.md` file) → delete the point.
- Detecting unindexed `.md` files (file exists, no Qdrant point) → re-embed and upsert.
- Detecting stale indexes (file content hash differs from stored hash) → re-embed and upsert.

**3. Startup consistency check + full rebuild.**

```python
# gateway/session/memory_sync.py

async def check_memory_consistency(qdrant: QdrantClient, memory_store_path: str):
    """Called at Gateway startup. Detects and repairs drift between
    .md source of truth and Qdrant index.

    Fast path: compare file count vs Qdrant point count. If equal and
    no files modified since last check, skip the full scan.
    Slow path: full reconciliation — scan all .md files, compare with
    Qdrant points, repair mismatches."""

    md_files = scan_all_memory_files(memory_store_path)
    md_ids = {parse_frontmatter(f)['memory_id'] for f in md_files}

    qdrant_ids = set()
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name='memories',
            limit=100,
            offset=offset,
            with_payload=['path'],
        )
        qdrant_ids.update(p.id for p in points)
        if offset is None:
            break

    # Unindexed files — exist on disk but not in Qdrant
    unindexed = md_ids - qdrant_ids
    for memory_id in unindexed:
        logger.warning(f'Memory sync: re-indexing unindexed file {memory_id}')
        content, metadata = read_memory_file(memory_id, memory_store_path)
        await write_memory_to_qdrant(memory_id, content, metadata)

    # Orphaned points — exist in Qdrant but no .md file
    orphaned = qdrant_ids - md_ids
    for memory_id in orphaned:
        logger.warning(f'Memory sync: removing orphaned Qdrant point {memory_id}')
        qdrant.delete(
            collection_name='memories',
            points_selector=models.PointIdsList(points=[memory_id]),
        )

    if unindexed or orphaned:
        logger.info(f'Memory sync complete: {len(unindexed)} re-indexed, '
                     f'{len(orphaned)} orphans removed')
    else:
        logger.info('Memory sync: index is consistent')


async def full_rebuild(qdrant: QdrantClient, memory_store_path: str):
    """Nuclear option: drop and recreate the Qdrant collection from .md files.
    Used when Qdrant storage is corrupted or after a major schema change.

    Invoked via CLI Agent or manual script — never automatically.
    Re-generates both dense and sparse vectors for every .md file."""
    qdrant.delete_collection('memories')
    await create_memory_collection(qdrant)

    md_files = scan_all_memory_files(memory_store_path)
    for i, filepath in enumerate(md_files):
        content, metadata = read_memory_file_from_path(filepath)
        memory_id = metadata['memory_id']
        memory_type = metadata['type']
        await write_memory(memory_id, memory_type, content, metadata)
        if (i + 1) % 100 == 0:
            logger.info(f'Rebuild progress: {i + 1}/{len(md_files)}')

    logger.info(f'Full rebuild complete: {len(md_files)} memories indexed')
```

**When each mechanism runs:**

| Mechanism | Trigger | Cost |
|---|---|---|
| Atomic write path | Every `write_memory()` call | Zero — it is the write path |
| Startup consistency check | Gateway startup | O(n) scroll of Qdrant + O(n) filesystem scan. Seconds for thousands of memories. |
| Full rebuild | Manual invocation only (CLI Agent or script) | Re-embeds every file. Minutes for thousands of memories (rate-limited by OpenRouter API). |

### 23.5b Derived Daily Transcript Archive

In addition to the canonical SQLite session store, the Gateway maintains a **derived append-only Markdown transcript** for each daily session under `logs/sessions/`.

**Purpose:** human-readable archival, export, debugging, and operator inspection. This transcript is **not** part of the retrieved memory store and is **not** used as live session state.

**Rules:**

- `gateway/sessions.db` remains authoritative for live session state, sticky routing, and replay.
- The transcript writer appends entries **after** the SQLite message write succeeds. This is a one-way derivation, not a second writable source of truth.
- Transcripts are append-only while the day is active. At the 4AM rollover, the previous day's transcript is finalized and a new day's file begins.
- Transcripts are **not** indexed in Qdrant and are **not** part of the memory retrieval set in §23.4.
- If a transcript file is missing or corrupted, it is regenerated from `sessions.db` by replaying the day's messages in `created_at` order.
- Agents do not edit transcript files directly.

**Path and naming:**

```text
logs/sessions/2025-01-15.md
```

**Example format:**

```markdown
<!-- logs/sessions/2025-01-15.md -->
---
session_id: sess_20250115
date: 2025-01-15
derived_from: gateway/sessions.db
status: finalized
---

# Session Transcript — January 15, 2025

[2025-01-15T10:03:12Z] [channel=desktop:desk_a1b2c3] [role=user]
Update the Project Proposal doc

[2025-01-15T10:03:26Z] [route=opus] [role=assistant]
Done — I added a conclusion section.
```

**Why one-way derived, not bi-directional sync?** The session transcript preserves readability without introducing a second live system of record. SQLite keeps the structured state; the transcript is a rendered archive.

### 23.6 Task Memory Isolation

Task execution happens in independent sessions. The full back-and-forth of task execution (agent progress, intermediate results, retries, internal clarifications) does NOT enter the main conversation session. Only the final task result is inserted as a message in the main session.

**Why isolation?** A task like "research quantum computing" might involve the research agent making 10 web searches, evaluating sources, reading papers, and synthesizing findings. All of that internal chatter would overwhelm the main session. The user sees: `"Here's what I found about quantum computing: [summary]"` — clean and focused.

**Where task memories live:**

| Data | Location | Accessible By |
|---|---|---|
| Task execution events | `streams:events` (Redis) | Orchestrator, Gateway (real-time forwarding to UI) |
| Agent session data | `agents/*/store/data/` (per-agent SQLite) | The agent that owns it, via recall intents |
| Agent learnings | `agents/*/store/learnings.md` → synced to `memory/agent_notes/` | Session Manager (retrieved as high-priority memories) |
| Task result summary | `memory/tasks/<task_id>.md` | Session Manager (retrieved when relevant) |
| Task artifacts | `runs/artifacts/<task_id>/` | Any agent via ArtifactManifest |

**How the orchestrator retrieves task memories:**

The orchestrator has two paths for accessing past task context:

**Path 1: Event index lookup.** Every `emit_event` call appends the stream message ID to a per-task Redis list (`task_events:{task_id}`). To replay a task's events, the orchestrator reads this list and fetches only those specific messages from `streams:events` — O(events for this task), not O(total events).

```python
# Orchestrator retrieves task event history via per-task index
async def get_task_events(task_id: str, redis) -> list[EventEnvelope]:
    """Fetch events for a specific task using the per-task index.
    Falls back to disk archive if events have been trimmed from Redis."""
    # 1. Get message IDs from per-task index
    msg_ids = await redis.lrange(f'task_events:{task_id}', 0, -1)

    if not msg_ids:
        # Index expired or never existed — check disk archive
        return await load_archived_events(task_id)

    # 2. Fetch each event by ID (O(1) per event, O(k) total where k = events for this task)
    events = []
    for msg_id in msg_ids:
        results = await redis.xrange('streams:events', msg_id, msg_id)
        if results:
            _, data = results[0]
            event = EventEnvelope.model_validate_json(data['event'])
            events.append(event)
        # If message was trimmed from stream, skip — event is in disk archive

    return sorted(events, key=lambda e: e.seq)
```

**Path 2: Recall intents to agents.** For detailed agent-specific history (what sources were evaluated, what edits were made, what decisions were taken), the orchestrator dispatches recall intents to the relevant agents. Each agent queries its own `store/data/` and returns structured results. This is already designed in §12.4.

```python
# Orchestrator asks docs_agent about a past task
recall_task = TaskEnvelope(
    intent='docs.recall_session',
    input={
        'task_id': 'tsk_abc123',
        'query': 'what edits were made and which account was used',
    },
    recipient='cosmic/docs-agent:2.1.0',
    ...
)
# docs_agent queries its own store/data/sessions.db
# Returns: structured edit history with doc_id, account, changes
```

**Current implementation note:** the main session still keeps task/sub-agent chatter isolated, but the parent assistant turn now carries a compact `specialist_receipts` summary in its metadata when a specialist/sub-agent produced part of the answer. These receipts are intentionally small and can include the delegated `intent`, `agent_id`, a short activity summary, compact source domains/sample, and artifact counts. The Gateway surfaces only the most recent few receipts into the Active Working Set so Opus can preserve confidence/provenance continuity across follow-up turns without replaying full sub-agent transcripts into prompt history.

**How task memories enter the retrieval store:**

After task completion, the orchestrator writes a task summary to `memory/tasks/<task_id>.md`. This summary includes: what was requested, which agents were involved, the final result, and any artifacts produced. This file is embedded and indexed in Qdrant. When the user later asks "what did we do about quantum computing?", the memory retriever finds this task summary via hybrid search and includes it in the assembled context.

```python
# Orchestrator writes task summary after completion
async def on_task_completed(task_id: str, result: AgentResult,
                             original_request: str, agents_used: list[str]):
    summary = await compaction_llm.summarize(
        f'Task: {original_request}\n'
        f'Agents: {", ".join(agents_used)}\n'
        f'Result: {json.dumps(result.output)}\n'
        f'Artifacts: {[a.path for a in result.artifacts]}',
        instruction='Write a brief summary of this completed task.',
        max_output_tokens=500,
    )
    await memory_writer.write_memory(
        memory_id=f'mem_task_{task_id}',
        memory_type='task_summary',
        content=summary,
        metadata={
            'task_id': task_id,
            'agent_ids': agents_used,
            'completed_at': utcnow().isoformat(),
        },
    )
```

### 23.7 Agent Notes Sync

Agent learnings (`agents/*/store/learnings.md`) are the agents' own persistent memories (see §12.1). The Session Manager syncs these into `memory/agent_notes/` and indexes them in Qdrant so they're retrievable as high-priority memories.

**Sync mechanism:** After each task completion, the Session Manager checks if the agent updated its `learnings.md` (by comparing file hash). If changed, it re-indexes the file in Qdrant and copies it to `memory/agent_notes/`. Agents continue to own their learnings files — the sync is read-only from the Session Manager's perspective.

### 23.8 Configuration

```ini
# Session Manager configuration (gateway environment)
SESSION_RESET_HOUR=4                          # daily reset at 4:00 AM in persisted user-local timezone
COMPACTION_THRESHOLD=0.70                     # compact at 70% context usage
COMPACTION_MODEL=claude-haiku-4-5             # cheap/fast model for summarization
MEMORY_TOKEN_BUDGET=12000                     # max tokens for retrieved memories
MEMORY_MAX_RESULTS=20                         # max memories per retrieval
EMBEDDING_MODEL=qwen3-embedding-8b            # dense vectors — via OpenRouter
SPARSE_MODEL=Qdrant/bm25                     # sparse vectors — local via FastEmbed (no API call)
OPENROUTER_API_KEY=<secret>                   # for embedding API calls
QDRANT_PATH=./qdrant_data                     # local Qdrant storage path
MEMORY_STORE_PATH=./memory                    # .md file tree root
SESSION_TRANSCRIPT_PATH=./logs/sessions       # derived append-only daily session transcript archive (.md)
MEMORY_SYNC_ON_STARTUP=true                   # run consistency check at Gateway startup (§23.5a)
USER_TIMEZONE_FALLBACK=America/Chicago        # used only until desktop reports a real IANA timezone
```

`COMPACTION_MODEL` and `EMBEDDING_MODEL` must resolve to entries in `shared/model_specs.json`
(§7.2c). The Session Manager uses that registry for SDK/base-URL selection, context budgeting, and
cost estimation rather than maintaining a second copy of model metadata.

### 23.9 Context Assembly Diagram (Per Turn)

```
┌─────────────────────────────────────────────────────────────┐
│                    ASSEMBLED CONTEXT                         │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SYSTEM PROMPT                              ~1-2k tok │  │
│  │  Agent identity, capabilities, rules                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  RETRIEVED MEMORIES (10-12k budget)                    │  │
│  │  ┌──────────────────┐  Each memory has:                │  │
│  │  │ mem_agent_docs_  │  - memory_id (for dedup)         │  │
│  │  │ 001              │  - type (agent_note/session/     │  │
│  │  │ [agent_note]     │         task/user_data)          │  │
│  │  │ HIGH PRIORITY    │  - date + relevance score        │  │
│  │  └──────────────────┘  - content                       │  │
│  │  ┌──────────────────┐                                  │  │
│  │  │ mem_sess_0115    │  Ranked by: recency × similarity │  │
│  │  │ [session_summary]│  Agent notes > user data          │  │
│  │  │ NORMAL PRIORITY  │  No repeated memory_ids          │  │
│  │  └──────────────────┘                                  │  │
│  │  ┌──────────────────┐  *** EXCLUDED FROM COMPACTION ***│  │
│  │  │ mem_task_abc123  │  Retrieved fresh every turn.     │  │
│  │  │ [task_summary]   │  Never summarized. Never lost.   │  │
│  │  └──────────────────┘                                  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  TODAY'S CONVERSATION                                  │  │
│  │                                                        │  │
│  │  [compacted_summary if mid-day compaction occurred]    │  │
│  │                                                        │  │
│  │  user: "Update the Project Proposal doc"               │  │
│  │  assistant: "Done — conclusion added."                 │  │
│  │  user: "What sources did you use for the intro?"       │  │
│  │  ...                                                   │  │
│  │                                                        │  │
│  │  Oldest messages prune off as new ones arrive.         │  │
│  │  Compaction triggers at 70% of this allocation.        │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  RESERVES                                              │  │
│  │  Compaction reserve: ~4-6k    Output reserve: ~4k      │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 23.10 Hard Rules

1. **All LLM backends receive the same assembled context.** Whether the query routes to Opus, Haiku, or Perplexity, the Session Manager provides identical context. The user gets a consistent assistant.
2. **Memories are never compacted.** The memory block is stripped before compaction input. Memories persist at source fidelity and are re-retrieved each turn.
3. **Each memory has a unique `memory_id`.** No memory is repeated within the same context window. The Session Manager tracks which IDs are in context.
4. **Task execution is isolated from the main session.** Only final task results enter the main session as messages. Full task details are retrievable via recall intents and the memory store.
5. **Agent notes have priority over user data** in memory ranking. Curated, task-proven facts outrank bulk external data.
6. **.md files are the source of truth.** Qdrant is the index (dense + sparse vectors). If Qdrant is lost, `full_rebuild()` re-generates both vector types from `memory/` (see §23.5a). Startup consistency check detects and repairs drift automatically.
7. **Daily full-session transcripts are derived archives.** `logs/sessions/*.md` is append-only, human-readable, and regenerated from SQLite if needed. It is not indexed in Qdrant and is not a second writable source of truth.
8. **Compaction uses a cheap model.** Never use Opus for summarization. Claude Haiku 4.5 or Sonnet — fast, cheap, good enough.
9. **Daily reset is transparent to the user.** Session boundaries at 4AM are invisible. The compacted summary becomes a retrievable memory. The user sees one perpetual conversation.
10. **Conversational replies use sticky routing, task input uses queues.** Two separate mechanisms for two separate problems. `<awaiting_reply/>` tag + `last_route` for inline conversation (§3.7). `user_input:requests/replies` Redis streams for async background task input (§13.2). They never overlap.
11. **`awaiting_reply` flag is cleared on first use.** Once the user replies and sticky routing fires, the flag is cleared. The next message goes through normal classification. No sticky routing chains.

---

## 24. Five Input Sources

COSMIC accepts five types of input. When combined, they create a system that acts proactively without any autonomous reasoning — it is purely reactive to events that the user has preconfigured. Every input source is normalized and tagged at the Gateway boundary with `source`, `source_id`, and `channel`, then enters the same processing pipeline through the Gateway.

**Important nuance for message sources:** infrastructure-driven inputs (heartbeats, crons, hooks, webhooks) become TaskEnvelopes immediately. Human messages first enter the Gateway's session/routing path. Only the `opus` route is converted into a TaskEnvelope and dispatched to the orchestrator; `haiku` and `perplexity` are handled directly by the Gateway.

### 24.1 Input Source Overview

| # | Source | What Produces It | Priority | Example |
|---|---|---|---|---|
| ① | **Messages** | Human typing on any channel (Desktop, WhatsApp, Telegram, Slack, CLI) | `high` | "Draft an email to my team" |
| ② | **Heartbeats** | Timer (default every 30 minutes) | `low` | "Check inbox for anything urgent" |
| ③ | **Crons** | Scheduled jobs (cron expressions) | `low` (configurable) | "9 AM daily: review calendar for conflicts" |
| ④ | **Hooks** | Internal state changes (gateway startup, session reset, agent registration) | `normal` | "On startup: load saved preferences" |
| ⑤ | **Webhooks** | External system callbacks (Gmail, GitHub, Jira, Slack) | `normal` | "New email arrived → agent processes it" |

### 24.2 How It Works

```
① Human types on WhatsApp ──► WhatsApp Bridge ──► Gateway internal route ──► WhatsApp adapter ──► tagged user message
② Timer fires every 30m ────► Scheduler ────────► Gateway ──► TaskEnvelope(source='heartbeat', source_id='default')
③ Cron job at 9 AM ─────────► Scheduler ────────► Gateway ──► TaskEnvelope(source='cron', source_id='cron_morning_email')
④ Gateway starts up ─────────► Hooks Engine ─────► Gateway ──► TaskEnvelope(source='hook', source_id='hook_gateway_startup')
⑤ Gmail webhook fires ──────► Webhook Handler ──► Gateway ──► TaskEnvelope(source='webhook', source_id='wh_gmail_001')
                                                      │
                                                      ▼
                                              Same processing pipeline:
                                              Session Manager → sticky route / Model Router
                                                           │
                                                           ├── haiku/perplexity → direct Gateway LLM path
                                                           └── opus → TaskEnvelope → orchestrator
```

**The orchestrator doesn't care where the input came from.** It receives a TaskEnvelope, decomposes it, dispatches to agents. The `source`, `source_id`, and `channel` fields are metadata for observability and response routing — they do not affect orchestration logic.

### 24.3 Source-to-Priority Mapping

User queries always take precedence. Background tasks (crons, heartbeats) yield to human input under full-bandwidth operation.

```python
# gateway/config.py
SOURCE_PRIORITY_MAP = {
    'user':      'high',       # human is waiting
    'webhook':   'normal',     # time-sensitive but no human staring at screen
    'hook':      'normal',     # internal lifecycle — process promptly
    'cron':      'low',        # background — can wait seconds or minutes
    'heartbeat': 'low',        # background — can wait
    'agent':     'normal',     # agent-initiated reverse task
}
```

Individual cron jobs can override this default via their `priority` field in the cron definition (see §25). For example, a "check for critical production alerts" cron might be set to `high`.

### 24.4 Heartbeat Suppression

When a heartbeat fires and nothing needs attention, the orchestrator responds with a special suppression token. The Gateway detects this and silently discards the response — the user never sees it. If something IS urgent, the response is delivered normally via the configured `delivery_channel`.

```python
HEARTBEAT_SUPPRESS_TOKEN = 'heartbeat_ok'

async def handle_heartbeat_response(response: str, delivery_channel: str | None):
    if response.strip() == HEARTBEAT_SUPPRESS_TOKEN:
        return  # suppress — nothing to report
    # Something needs attention — deliver to user
    adapter = channel_registry.get_adapter(delivery_channel or 'desktop')  # bare 'desktop' = primary desktop alias
    await adapter.send({
        'type': 'notification',
        'source': 'heartbeat',
        'content': response,
    })
```

---

## 25. Scheduler Module / Cron Manager (Crons & Heartbeats)

The Scheduler is a Gateway module that manages cron jobs and heartbeats. It stores definitions in SQLite, runs a polling loop to check what's due, and fires TaskEnvelopes to the orchestrator when jobs trigger. The orchestrator creates and manages cron jobs via the Scheduler's internal API. The same module also serves as the **Cron Manager** for future desktop observability/control: it owns durable cron state, execution history, timezone handling, and pause/resume behavior so the desktop UI can inspect and manage scheduled work without talking to the orchestrator directly.

**Design principle:** Scheduling is infrastructure, not AI. No LLM is involved in deciding "is it 9 AM yet?" — the Scheduler is a simple timer loop. The AI reasoning happens when the orchestrator receives the fired TaskEnvelope and processes it like any other input.

**Timezone rule:** All user-facing schedules are evaluated in the user's persisted local IANA timezone as last reported by the desktop app. The VM timezone is never authoritative for user intent. `next_fire_at` is stored in UTC, but it is always computed from `(cron expression + cron timezone)` where the cron timezone defaults to the persisted user timezone snapshot.

### 25.1 Data Model

```sql
-- gateway/scheduler/scheduler.db

CREATE TABLE cron_jobs (
    cron_id TEXT PRIMARY KEY,                    -- 'cron_morning_email'
    schedule TEXT NOT NULL,                       -- cron expression: '0 9 * * *'
    timezone TEXT NOT NULL,                       -- IANA timezone for interpreting the cron expression; defaults to persisted user timezone
    prompt TEXT NOT NULL,                         -- what to send to orchestrator
    description TEXT,                             -- human-readable description
    delivery_channel TEXT DEFAULT 'desktop',      -- where to deliver results: 'desktop' (primary desktop alias), 'desktop:<device_id>', 'whatsapp:+1234', etc.
    priority TEXT DEFAULT 'low',                  -- TaskEnvelope priority override
    active_hours TEXT,                            -- '08:00-22:00' or null for always
    enabled BOOLEAN DEFAULT TRUE,
    paused_at TIMESTAMP,                          -- set when user/operator pauses the cron
    pause_reason TEXT,                            -- 'user_paused', 'maintenance', etc.
    created_by TEXT DEFAULT 'user',               -- 'user', 'orchestrator' (tracks who created it)
    metadata_json TEXT,                           -- arbitrary metadata (tags, labels)
    last_fired_at TIMESTAMP,
    last_result_status TEXT,                      -- 'success', 'failed', 'suppressed', null before first run
    last_result_summary TEXT,                     -- short human-readable result summary for observability UI
    next_fire_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE heartbeat_config (
    config_id TEXT PRIMARY KEY DEFAULT 'default',
    interval_sec INTEGER DEFAULT 1800,            -- 30 minutes
    prompt TEXT NOT NULL,                          -- heartbeat check prompt
    delivery_channel TEXT DEFAULT 'desktop',       -- where to deliver results; bare 'desktop' = primary desktop alias
    timezone TEXT,                                 -- null = use persisted user timezone snapshot
    active_hours TEXT DEFAULT '08:00-22:00',       -- suppress outside these hours
    priority TEXT DEFAULT 'low',
    enabled BOOLEAN DEFAULT TRUE,
    paused_at TIMESTAMP,
    pause_reason TEXT,
    suppress_token TEXT DEFAULT 'heartbeat_ok',    -- token that suppresses delivery
    updated_at TIMESTAMP NOT NULL
);

-- Seed default heartbeat config
INSERT OR IGNORE INTO heartbeat_config (config_id, prompt, updated_at)
VALUES ('default',
    'Check for anything that needs my attention: urgent emails, upcoming calendar events, overdue tasks. If nothing needs attention, respond with exactly: heartbeat_ok',
    datetime('now'));

CREATE TABLE cron_execution_log (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cron_id TEXT NOT NULL,
    fired_at TIMESTAMP NOT NULL,
    task_id TEXT,                                  -- TaskEnvelope.task_id that was created
    status TEXT NOT NULL,                          -- 'fired', 'skipped_inactive_hours', 'skipped_disabled'
    FOREIGN KEY (cron_id) REFERENCES cron_jobs(cron_id)
);

CREATE INDEX idx_cron_log_cron ON cron_execution_log(cron_id, fired_at);

CREATE TABLE scheduler_profile (
    profile_id TEXT PRIMARY KEY DEFAULT 'default',
    user_timezone TEXT NOT NULL,                  -- latest IANA timezone reported by desktop
    timezone_source TEXT DEFAULT 'desktop',       -- 'desktop', 'fallback'
    timezone_reported_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL
);
```

### 25.2 Scheduler Polling Loop

```python
# gateway/scheduler/polling.py

async def scheduler_polling_loop(redis):
    """Main scheduler loop. Runs inside the Gateway process.
    Checks due crons and heartbeats every 10 seconds."""
    while True:
        await asyncio.sleep(10)  # poll interval

        # Check heartbeat
        await check_and_fire_heartbeat(redis)

        # Check cron jobs
        due_crons = db.execute('''
            SELECT * FROM cron_jobs
            WHERE enabled = TRUE
            AND next_fire_at <= datetime('now')
        ''').fetchall()

        for cron in due_crons:
            if not is_within_active_hours(cron['active_hours'], cron['timezone']):
                log_execution(cron['cron_id'], 'skipped_inactive_hours')
                update_next_fire(cron['cron_id'], cron['schedule'], cron['timezone'])
                continue

            # Deterministic idempotency key: same cron + same scheduled time = same key.
            # If the scheduler crashes after dispatch but before updating next_fire_at,
            # the next poll re-fires this cron with the same next_fire_at. A random UUID
            # would bypass idempotency and execute the job twice. This key ensures the
            # existing idempotency layer (§14) deduplicates the second fire.
            idemp_key = f'cron:{cron["cron_id"]}:{cron["next_fire_at"]}'

            task_id = generate_task_id()
            task = TaskEnvelope(
                task_id=task_id,
                task_list_id=f'cron:{cron["cron_id"]}',
                session_id=None,             # crons don't belong to a conversation session
                sender='cosmic/gateway:1.0.0',
                recipient='cosmic/orchestrator:1.0.0',
                intent='orchestrator.process',
                input={
                    'query': cron['prompt'],
                    'cron_metadata': json.loads(cron['metadata_json'] or '{}'),
                },
                idempotency_key=idemp_key,
                priority=cron['priority'],
                source='cron',
                source_id=cron['cron_id'],
                channel=cron['delivery_channel'],
                signature='',  # signed below
            )
            task.signature = sign_task(task, GATEWAY_SIGNING_SECRET)
            await dispatch(task, redis)

            # Update execution tracking
            db.execute('''
                UPDATE cron_jobs SET last_fired_at = ?, updated_at = ?
                WHERE cron_id = ?
            ''', [utcnow(), utcnow(), cron['cron_id']])
            update_next_fire(cron['cron_id'], cron['schedule'], cron['timezone'])
            log_execution(cron['cron_id'], 'fired', task_id)


async def check_and_fire_heartbeat(redis):
    """Check if the heartbeat is due and fire it."""
    config = db.execute(
        'SELECT * FROM heartbeat_config WHERE config_id = ?', ['default']
    ).fetchone()

    if not config or not config['enabled']:
        return

    effective_timezone = config['timezone'] or load_scheduler_profile()['user_timezone']
    if not is_within_active_hours(config['active_hours'], effective_timezone):
        return

    # Check if enough time has passed since last heartbeat
    last_key = 'scheduler:last_heartbeat'
    last_fired = await redis.get(last_key)
    if last_fired:
        elapsed = (utcnow() - datetime.fromisoformat(last_fired)).total_seconds()
        if elapsed < config['interval_sec']:
            return

    # Deterministic idempotency key: bucket the current time by interval_sec.
    # If the scheduler crashes after dispatch but before redis.set(last_key),
    # the next poll re-fires the heartbeat. A random UUID would bypass
    # idempotency. The time bucket ensures the same heartbeat interval
    # produces the same key, so §14 deduplicates the second fire.
    now_ts = int(utcnow().timestamp())
    bucket = now_ts - (now_ts % config['interval_sec'])
    idemp_key = f'heartbeat:default:{bucket}'

    task_id = generate_task_id()
    task = TaskEnvelope(
        task_id=task_id,
        task_list_id='heartbeat:default',
        session_id=None,
        sender='cosmic/gateway:1.0.0',
        recipient='cosmic/orchestrator:1.0.0',
        intent='orchestrator.process',
        input={'query': config['prompt']},
        idempotency_key=idemp_key,
        priority=config['priority'],
        source='heartbeat',
        source_id='default',
        channel=config['delivery_channel'],
        signature='',
    )
    task.signature = sign_task(task, GATEWAY_SIGNING_SECRET)
    await dispatch(task, redis)
    await redis.set(last_key, utcnow().isoformat())
```

### 25.3 Internal API Endpoints

The orchestrator creates and manages crons via these endpoints. Protected by `X-Internal-Token` (same auth as credential endpoints).

```python
# gateway/scheduler/routes.py

@app.post('/internal/scheduler/crons')
async def create_cron(request: Request):
    """Create a new cron job. Called by orchestrator when user says
    'remind me to check email every morning at 9.'"""
    await verify_internal_auth(request)
    body = await request.json()

    cron_id = body.get('cron_id') or f'cron_{uuid4().hex[:12]}'
    db.execute('''
        INSERT INTO cron_jobs
        (cron_id, schedule, timezone, prompt, description, delivery_channel,
         priority, active_hours, enabled, created_by, metadata_json,
         next_fire_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [
        cron_id,
        body['schedule'],              # '0 9 * * *'
        body.get('timezone') or load_scheduler_profile()['user_timezone'],
        body['prompt'],                # 'Check my email and flag anything urgent'
        body.get('description', ''),
        body.get('delivery_channel', 'desktop'),
        body.get('priority', 'low'),
        body.get('active_hours'),
        body.get('enabled', True),
        body.get('created_by', 'orchestrator'),
        json.dumps(body.get('metadata', {})),
        compute_next_fire(
            body['schedule'],
            body.get('timezone') or load_scheduler_profile()['user_timezone'],
        ),
        utcnow(), utcnow(),
    ])
    return {
        'cron_id': cron_id,
        'next_fire_at': compute_next_fire(
            body['schedule'],
            body.get('timezone') or load_scheduler_profile()['user_timezone'],
        ),
    }

@app.get('/internal/scheduler/crons')
async def list_crons(request: Request):
    await verify_internal_auth(request)
    crons = db.execute('SELECT * FROM cron_jobs ORDER BY created_at').fetchall()
    return {'crons': [dict(c) for c in crons]}

@app.get('/internal/scheduler/crons/{cron_id}')
async def get_cron(cron_id: str, request: Request):
    await verify_internal_auth(request)
    cron = db.execute('SELECT * FROM cron_jobs WHERE cron_id = ?', [cron_id]).fetchone()
    if not cron:
        raise HTTPException(404, 'Cron not found')
    return dict(cron)

@app.patch('/internal/scheduler/crons/{cron_id}')
async def update_cron(cron_id: str, request: Request):
    await verify_internal_auth(request)
    body = await request.json()
    updates = []
    params = []
    for field in ['schedule', 'timezone', 'prompt', 'description', 'delivery_channel',
                  'priority', 'active_hours', 'enabled', 'metadata_json']:
        if field in body:
            updates.append(f'{field} = ?')
            params.append(body[field] if field != 'metadata_json'
                         else json.dumps(body[field]))
    if 'schedule' in body:
        updates.append('next_fire_at = ?')
        params.append(compute_next_fire(
            body['schedule'],
            body.get('timezone') or get_existing_timezone(cron_id),
        ))
    updates.append('updated_at = ?')
    params.append(utcnow())
    params.append(cron_id)
    db.execute(f'UPDATE cron_jobs SET {", ".join(updates)} WHERE cron_id = ?', params)
    return {'updated': True}

@app.delete('/internal/scheduler/crons/{cron_id}')
async def delete_cron(cron_id: str, request: Request):
    await verify_internal_auth(request)
    db.execute('DELETE FROM cron_jobs WHERE cron_id = ?', [cron_id])
    return {'deleted': True}

@app.post('/internal/scheduler/crons/{cron_id}/pause')
async def pause_cron(cron_id: str, request: Request):
    await verify_internal_auth(request)
    body = await request.json()
    db.execute('''
        UPDATE cron_jobs SET
            enabled = FALSE,
            paused_at = ?,
            pause_reason = ?,
            updated_at = ?
        WHERE cron_id = ?
    ''', [utcnow(), body.get('reason', 'user_paused'), utcnow(), cron_id])
    return {'paused': True}

@app.post('/internal/scheduler/crons/{cron_id}/resume')
async def resume_cron(cron_id: str, request: Request):
    await verify_internal_auth(request)
    cron = db.execute(
        'SELECT schedule, timezone FROM cron_jobs WHERE cron_id = ?',
        [cron_id]
    ).fetchone()
    db.execute('''
        UPDATE cron_jobs SET
            enabled = TRUE,
            paused_at = NULL,
            pause_reason = NULL,
            next_fire_at = ?,
            updated_at = ?
        WHERE cron_id = ?
    ''', [compute_next_fire(cron['schedule'], cron['timezone']), utcnow(), cron_id])
    return {'resumed': True}

@app.get('/internal/scheduler/heartbeat/config')
async def get_heartbeat_config(request: Request):
    await verify_internal_auth(request)
    config = db.execute(
        'SELECT * FROM heartbeat_config WHERE config_id = ?', ['default']
    ).fetchone()
    return dict(config)

@app.post('/internal/scheduler/heartbeat/config')
async def update_heartbeat_config(request: Request):
    await verify_internal_auth(request)
    body = await request.json()
    db.execute('''
        UPDATE heartbeat_config SET
            interval_sec = COALESCE(?, interval_sec),
            prompt = COALESCE(?, prompt),
            delivery_channel = COALESCE(?, delivery_channel),
            timezone = COALESCE(?, timezone),
            active_hours = COALESCE(?, active_hours),
            priority = COALESCE(?, priority),
            enabled = COALESCE(?, enabled),
            suppress_token = COALESCE(?, suppress_token),
            updated_at = ?
        WHERE config_id = 'default'
    ''', [
        body.get('interval_sec'), body.get('prompt'),
        body.get('delivery_channel'), body.get('timezone'),
        body.get('active_hours'),
        body.get('priority'), body.get('enabled'),
        body.get('suppress_token'), utcnow(),
    ])
    return {'updated': True}
```

### 25.3a Desktop-Facing Cron Manager Surface

The desktop app should not manipulate raw SQLite rows. It talks to the Gateway's desktop-authenticated Cron Manager surface (`/scheduler/*`), which returns a stable management view over the same scheduler state used by the orchestrator.

**Design goals:**

- show all scheduled jobs and heartbeat config in one place,
- expose computed state (`active`, `paused`, `error`),
- show `timezone`, `next_fire_at`, `last_fired_at`, and recent outcomes,
- allow pause/resume without deleting the underlying cron,
- support future observability UI without giving the desktop direct write access to internal-only orchestrator APIs.

**Pause semantics:** a paused cron is represented durably in the scheduler DB (`enabled = FALSE`, `paused_at`, `pause_reason`). The Cron Manager surfaces this as `state='paused'` in desktop responses.

### 25.4 Orchestrator Intent: `orchestrator.schedule`

Agents can request cron creation via a reverse task. The orchestrator validates and calls the Scheduler's internal API — agents never touch the Scheduler directly.

```python
# Orchestrator handles scheduling requests
async def on_schedule_request(self, task: TaskEnvelope):
    """Agent or user requested a new cron job.
    Example: user says 'remind me to check email every morning at 9.'
    Orchestrator parses this, creates cron via Scheduler API."""

    resp = await self.gateway_client.post(
        f'{GATEWAY_INTERNAL_URL}/internal/scheduler/crons',
        json={
            'schedule': task.input['schedule'],       # '0 9 * * *'
            'timezone': task.input.get('timezone'),   # defaults to persisted user timezone
            'prompt': task.input['prompt'],            # 'Check email, flag urgent'
            'description': task.input.get('description', ''),
            'delivery_channel': task.channel or 'desktop',
            'priority': task.input.get('priority', 'low'),
            'active_hours': task.input.get('active_hours'),
            'created_by': 'orchestrator',
        },
        headers={'X-Internal-Token': GATEWAY_INTERNAL_TOKEN},
    )
    return resp.json()  # { cron_id, next_fire_at }
```

### 25.5 Configuration

```ini
# Scheduler configuration (gateway environment)
SCHEDULER_POLL_INTERVAL_SEC=10          # how often the polling loop runs
SCHEDULER_TIMEZONE=America/Chicago      # fallback only until desktop reports a real IANA timezone
GATEWAY_SIGNING_SECRET=<secret>         # for signing gateway-generated TaskEnvelopes (scheduler, heartbeat, webhook, hooks)
```

**Important:** `SCHEDULER_TIMEZONE` is only a bootstrap fallback. Once the desktop has reported the user's current IANA timezone, the persisted scheduler profile becomes authoritative. All cron evaluation, heartbeat active-hours checks, and the 4 AM session rollover must use the user-local timezone snapshot, not the VM timezone.

---

## 26. Webhook Ingestion

The Webhook Handler is a Gateway module that receives HTTP POST callbacks from external systems, verifies provider-specific signatures, converts payloads into TaskEnvelopes, and dispatches them to the orchestrator.

**Current Gmail nuance:** user-owned Gmail uses a specialist-first path instead of sending every mailbox change directly to the orchestrator. Gmail Pub/Sub enters the Gateway, Gateway resolves the connected Google credential, dispatches `gmail.process_inbound` to the Gmail Agent, and the Gmail Agent performs semantic triage using thread data, shared memory retrieval, and compact current user state. If an item is worth surfacing, Gateway stores a compact surfaced Gmail reference in `gateway/gmail_context.db` and notifies Desktop/Mobile. Later user turns receive those recent Gmail references, so phrases like "reply to this" can be grounded to the exact Gmail account/thread/message before delegating to `gmail.read_thread` or `gmail.draft_reply`.

This keeps routine inbox noise out of the orchestrator while preserving continuity. Cross-domain action still belongs to the orchestrator: when a surfaced Gmail item affects active goals, or a future semantic automation rule matches an inbound event, Gateway should dispatch an orchestrator task with the Gmail reference, triage summary, matching evidence, and original standing instruction.

Longer term, Gmail should share a generic event automation registry with calendar, files, Slack, GitHub, Cosmic Mail, heartbeats, and custom webhooks. The generic primitives are event, semantic condition, context resolver, action plan, confidence policy, approval policy, and learning loop. Provider-specific agents remain sensors/capability specialists; Gateway matches events; orchestrator plans cross-domain work.

### 26.1 Architecture

```
External System (Gmail, GitHub, Jira, Slack)
    │
    │  HTTP POST /webhooks/{webhook_id}
    ▼
Gateway: Webhook Handler
    │
    ├── 1. Look up webhook_id in webhooks.db
    ├── 2. Verify provider signature (HMAC, JWT, etc.)
    ├── 3. Convert payload → TaskEnvelope
    │      source='webhook', source_id=webhook_id
    │      channel=webhook.delivery_channel
    │      priority='normal' (configurable per webhook)
    ├── 4. Dispatch to orchestrator via Redis
    └── 5. Return 200 OK to provider (or 401 if sig fails)
```

### 26.2 Data Model

```sql
-- gateway/webhooks/webhooks.db

CREATE TABLE webhooks (
    webhook_id TEXT PRIMARY KEY,                  -- 'wh_gmail_001'
    provider TEXT NOT NULL,                        -- 'gmail', 'github', 'jira', 'slack', 'custom'
    description TEXT,                              -- 'Gmail inbox notifications'
    secret TEXT NOT NULL,                           -- provider-specific verification secret
    delivery_channel TEXT DEFAULT 'desktop',        -- where to deliver results; bare 'desktop' = primary desktop alias
    priority TEXT DEFAULT 'normal',
    prompt_template TEXT,                           -- how to format the webhook payload for the LLM
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE webhook_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id TEXT NOT NULL,
    received_at TIMESTAMP NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,                           -- 'processed', 'rejected_signature', 'rejected_disabled'
    task_id TEXT,                                   -- TaskEnvelope.task_id if processed
    payload_summary TEXT,                           -- truncated payload for debugging
    FOREIGN KEY (webhook_id) REFERENCES webhooks(webhook_id)
);

CREATE INDEX idx_webhook_log ON webhook_log(webhook_id, received_at);
```

### 26.3 Webhook Endpoint

```python
# gateway/webhooks/routes.py

@app.post('/webhooks/{webhook_id}')
async def receive_webhook(webhook_id: str, request: Request):
    """Public endpoint — no Gateway auth token required.
    Authentication is via provider-specific signature verification."""

    webhook = db.execute(
        'SELECT * FROM webhooks WHERE webhook_id = ?', [webhook_id]
    ).fetchone()

    if not webhook:
        raise HTTPException(404, 'Webhook not found')
    if not webhook['enabled']:
        log_webhook(webhook_id, 'rejected_disabled')
        return {'status': 'disabled'}

    # Provider-specific signature verification
    body = await request.body()
    verifier = get_webhook_verifier(webhook['provider'])
    if not verifier.verify(request.headers, body, webhook['secret']):
        log_webhook(webhook_id, 'rejected_signature')
        raise HTTPException(401, 'Invalid signature')

    payload = json.loads(body)

    # Format the webhook payload for the LLM
    if webhook['prompt_template']:
        prompt = webhook['prompt_template'].format(**payload)
    else:
        prompt = f'Webhook event from {webhook["provider"]}: {json.dumps(payload, indent=2)}'

    # Extract provider-specific event ID for deterministic idempotency.
    # Without this, provider redeliveries would bypass §14 deduplication
    # because each delivery got a random uuid4() key.
    provider_event_id = verifier.extract_event_id(request.headers, payload)

    task_id = generate_task_id()
    task = TaskEnvelope(
        task_id=task_id,
        task_list_id=f'webhook:{webhook_id}',
        session_id=None,
        sender='cosmic/gateway:1.0.0',
        recipient='cosmic/orchestrator:1.0.0',
        intent='orchestrator.process',
        input={'query': prompt, 'webhook_payload': payload},
        idempotency_key=f'webhook:{webhook_id}:{provider_event_id}',
        priority=webhook['priority'],
        source='webhook',
        source_id=webhook_id,
        channel=webhook['delivery_channel'],
        signature='',
    )
    task.signature = sign_task(task, GATEWAY_SIGNING_SECRET)
    await dispatch(task, redis)

    log_webhook(webhook_id, 'processed', task_id, payload)
    return {'status': 'accepted', 'task_id': task_id}
```

### 26.4 Provider Signature Verifiers

```python
# gateway/webhooks/providers.py

class WebhookVerifier:
    """Base class for webhook signature verification."""
    provider: str
    def verify(self, headers: dict, body: bytes, secret: str) -> bool: ...
    def extract_event_id(self, headers: dict, payload: dict) -> str:
        """Extract a provider-specific event ID for deterministic idempotency.
        Returns a stable ID that is identical across redeliveries of the same event.
        Subclasses MUST override this — the base implementation falls back to uuid4()
        which defeats deduplication (only used for unknown providers)."""
        return str(uuid4())

class GmailPushVerifier(WebhookVerifier):
    provider = 'gmail'
    def verify(self, headers, body, secret):
        # Gmail uses Google Cloud Pub/Sub — verify JWT bearer token
        ...
    def extract_event_id(self, headers, payload):
        # Pub/Sub message_id is stable across redeliveries
        return payload.get('message', {}).get('message_id', str(uuid4()))

class GitHubVerifier(WebhookVerifier):
    provider = 'github'
    def verify(self, headers, body, secret):
        sig = headers.get('X-Hub-Signature-256', '')
        expected = 'sha256=' + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    def extract_event_id(self, headers, payload):
        # X-GitHub-Delivery is a unique GUID per event, stable across redeliveries
        return headers.get('X-GitHub-Delivery', str(uuid4()))

class SlackVerifier(WebhookVerifier):
    provider = 'slack'
    def verify(self, headers, body, secret):
        # Slack signing secret verification
        timestamp = headers.get('X-Slack-Request-Timestamp', '')
        sig_basestring = f'v0:{timestamp}:{body.decode()}'
        expected = 'v0=' + hmac.new(
            secret.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(headers.get('X-Slack-Signature', ''), expected)
    def extract_event_id(self, headers, payload):
        # Slack event_id is stable per event delivery
        return payload.get('event_id', str(uuid4()))

class GenericHMACVerifier(WebhookVerifier):
    provider = 'custom'
    def verify(self, headers, body, secret):
        sig = headers.get('X-Webhook-Signature', '')
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    def extract_event_id(self, headers, payload):
        # Convention: providers may send X-Webhook-Event-ID header
        return headers.get('X-Webhook-Event-ID', str(uuid4()))
```

---

## 27. Channel Adapters

Channel Adapters normalize platform-specific messages into the unified COSMIC processing pipeline and route responses back to the originating platform. Each adapter handles authentication, message parsing, and response delivery for its platform. Most adapters run directly inside the Gateway process; when a platform requires a non-Python SDK or an isolated long-lived runtime, the adapter may delegate transport/auth concerns to a sidecar bridge (§27.6).

**Design principle:** The Gateway's core logic (session management, routing, model classification) is platform-agnostic. Channel adapters are thin translation layers. Adding a new platform means implementing the adapter interface — no changes to Gateway internals.

### 27.1 Adapter Interface

```python
# gateway/channels/base.py

class ChannelAdapter(ABC):
    """Base class for all channel adapters. Each platform implements
    this interface to connect to the COSMIC Gateway."""

    platform: str                     # 'desktop', 'whatsapp', 'telegram', 'slack', 'discord', 'cli'

    @abstractmethod
    async def start(self):
        """Initialize platform connection (WebSocket server, bot login, etc.)."""
        ...

    @abstractmethod
    async def stop(self):
        """Graceful shutdown — disconnect from platform."""
        ...

    @abstractmethod
    async def send(self, message: dict, channel: str | None = None):
        """Send a message/event to the user via this platform.
        The message dict follows the same schema as WebSocket server→client
        messages (§3.3): response.chunk, response.complete, task.created,
        task.progress, task.completed, task.failed, task.input_required, etc.
        `channel` is the fully-qualified concrete destination when the
        platform supports multiple active endpoints (for example,
        `desktop:<device_id>` or `whatsapp:+1234567890`)."""
        ...

    @abstractmethod
    async def on_message(self, callback: Callable):
        """Register a callback for incoming messages from this platform.
        The callback receives normalized messages in the Gateway's internal format."""
        ...

    def channel_id(self, platform_context: dict) -> str:
        """Generate a channel identifier for session routing.
        Format: '{platform}:{platform_specific_id}'
        Examples: 'desktop:desk_a1b2c3', 'whatsapp:+1234567890',
        'telegram:chat_123', 'slack:C0123456'"""
        return f'{self.platform}:{platform_context.get("id", "default")}'

    def normalize_message(self, raw_message: Any) -> dict:
        """Convert platform-specific message format to Gateway's internal format.
        Returns: { content: str, session_id: str | None, channel: str, metadata: dict }"""
        ...
```

### 27.2 Unified Sessions with Channel Tagging

Sessions are **channel-agnostic** — all channels share a single session per day. Individual messages carry their originating channel in the `channel` column (see §3.11 messages table), enabling responses to be routed back to the correct platform. This gives the assistant full cross-channel context continuity: a user can start a complex task on the Desktop App and seamlessly continue it on WhatsApp or Telegram.

```python
# Session ID generation is channel-agnostic
def generate_session_id() -> str:
    """Unified session ID. All channels share one session per day,
    enabling cross-channel context continuity for a personal assistant."""
    date_part = utcnow().strftime('%Y%m%d')
    return f'sess_{date_part}'

# Examples:
# Any channel on Jan 15: sess_20250115
# Next day:              sess_20250116
```

**Why unified sessions?** COSMIC is a single-user personal assistant. Splitting context by channel fragments the assistant's understanding — it wouldn't know about a task you started on Desktop when you message on WhatsApp. Unified sessions ensure full continuity. Each message stores its originating `channel` so responses route back correctly, and **sticky routing is channel-scoped** (§3.7): an `awaiting_reply` flag on `desktop:desk_a1b2c3` doesn't capture a WhatsApp message or another desktop device. Concrete desktop message channels use the form `desktop:<device_id>`, where `device_id` is a stable per-installation identifier generated once and stored locally. Bare `desktop` remains a delivery alias for "the configured primary desktop device" in cron / heartbeat / webhook delivery configuration. The Session Manager's memory retrieval (§23.4) and context assembly work across the unified session — all messages from all channels are visible during context assembly, giving the LLM the complete picture.

### 27.3 Channel Adapter Registry

```python
# gateway/channels/registry.py

class ChannelAdapterRegistry:
    """Manages all active channel adapters. The Gateway uses this
    to route responses back to the originating channel."""

    def __init__(self):
        self.adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter):
        self.adapters[adapter.platform] = adapter

    def get_adapter(self, channel: str) -> ChannelAdapter | None:
        """Look up adapter by channel string.
        'whatsapp:+1234' → WhatsApp adapter
        'desktop:desk_a1b2c3' → Desktop adapter
        'cli' → CLI adapter"""
        platform = channel.split(':')[0] if channel else 'desktop'
        return self.adapters.get(platform)

    async def start_all(self):
        for adapter in self.adapters.values():
            await adapter.start()

    async def stop_all(self):
        for adapter in self.adapters.values():
            await adapter.stop()
```

### 27.4 Available Adapters

| Adapter | Platform | Transport | Status |
|---|---|---|---|
| `DesktopAdapter` | Desktop App (Electron) | WebSocket (persistent) | **Primary** — ships with v1.0 |
| `CLIAdapter` | CLI Agent | Internal pipe (in-process) | **Alpha** — ships with v1.0 |
| `WhatsAppAdapter` | WhatsApp | Baileys (via Node.js sidecar bridge) | **Implemented** — ships with v1.0 |
| `TelegramAdapter` | Telegram | Bot API webhook (Gateway-owned) | **Implemented** — private DM + media manifest support |
| `SlackAdapter` | Slack | Bolt SDK / Events API | **Planned** |
| `DiscordAdapter` | Discord | discord.py | **Planned** |

**Adding a new adapter:** Implement `ChannelAdapter`, register it in the `ChannelAdapterRegistry`, configure its credentials in environment variables. No Gateway code changes required.

**Telegram implementation notes (current):**

- Telegram is implemented as a **Gateway-owned Bot API webhook adapter**, not a sidecar bridge.
- Inbound entrypoint: `POST /channels/telegram/webhook`
- Secret verification: `X-Telegram-Bot-Api-Secret-Token`
- Scope: **private chats only** for user ↔ COSMIC communication
- Media handling: inbound Telegram attachments are normalized into the common `attachments` / `input_artifacts` pipeline; raw bytes are materialized later via `GET /internal/channels/telegram/media/{file_id}`
- Control-plane routes:
  - `POST /channels/telegram/webhook/sync`
  - `DELETE /channels/telegram/webhook`
  - `POST /channels/telegram/send`

#### 27.4a Telegram Provisioning Model

Under the current direct-to-VM COSMIC architecture, Telegram is a **per-VM bot**, not a single centralized platform bot.

- Users do **not** create their own bots.
- The operator creates **one BotFather bot per VM / deployment**.
- That bot token is installed into the VM's `gateway.env`.
- The Gateway on that VM owns the Telegram webhook and routes messages into that user's COSMIC session space.

**Why per-VM?** A single global Telegram bot would require a centralized ingress/router service that receives all Telegram traffic and forwards it to the correct user VM. COSMIC does not currently have that central Telegram ingress layer; adapters live inside each user's Gateway (§27.4, §27.5a).

#### 27.4b Telegram Bot Setup (Per VM)

Create the bot in Telegram via `@BotFather`.

**Operator steps:**

1. `/newbot`
2. Choose bot display name and username
3. Copy the Bot API token returned by BotFather
4. `/setjoingroups` → select the bot → `Disable`

**Design rule:** Telegram support is **private-DM only** in the current architecture. Group chats are intentionally ignored by the adapter.

#### 27.4c Telegram VM Configuration

Install these values into the VM's `gateway.env` (and mirror them into the repo-local `Backend/gateway.env` if you keep repo-local envs aligned with live service envs):

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<botfather-token>
TELEGRAM_WEBHOOK_SECRET=<random-secret-token>
TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true
TELEGRAM_ALLOWED_USER_ID=
TELEGRAM_ALLOWED_CHAT_ID=
```

**Where each value comes from:**

- `TELEGRAM_BOT_TOKEN`: returned by `@BotFather` during `/newbot`
- `TELEGRAM_WEBHOOK_SECRET`: operator-generated random secret used to verify `X-Telegram-Bot-Api-Secret-Token`
- `TELEGRAM_ALLOWED_USER_ID`, `TELEGRAM_ALLOWED_CHAT_ID`: intentionally blank on first boot, then locked after first successful `/start` from the intended user

#### 27.4d Telegram Webhook Activation

Once the VM has:

- a valid public hostname in `GATEWAY_PUBLIC_HOST`
- working HTTPS via Caddy / TLS edge
- `TELEGRAM_ENABLED=true`
- a valid `TELEGRAM_BOT_TOKEN`

restart the Gateway and sync the webhook:

```text
POST /channels/telegram/webhook/sync
```

Expected webhook target:

```text
https://<vm-public-host>/channels/telegram/webhook
```

The adapter verifies inbound requests using Telegram's secret-token header:

```text
X-Telegram-Bot-Api-Secret-Token
```

#### 27.4e Telegram User Locking / Pairing

After webhook activation, the intended human user sends `/start` to the bot from Telegram.

**Initial state:**

- `TELEGRAM_ALLOWED_USER_ID` is blank
- `TELEGRAM_ALLOWED_CHAT_ID` is blank
- the adapter accepts the first private-DM traffic

**Operator flow after first `/start`:**

1. Inspect the inbound Telegram event through Gateway observability:
   - Gateway logs (`journalctl -u cosmic-gateway | grep telegram`)
   - session store (`gateway/sessions.db`, `channel='telegram:chat_<id>'`)
2. Extract:
   - Telegram `user_id`
   - Telegram `chat_id`
3. Write both into:

```dotenv
TELEGRAM_ALLOWED_USER_ID=<telegram-user-id>
TELEGRAM_ALLOWED_CHAT_ID=<telegram-chat-id>
```

4. Restart `cosmic-gateway`

After that, only that exact Telegram private chat is accepted by the adapter.

**Private-DM note:** for Telegram private chats, `chat_id` typically matches the human user's Telegram ID. COSMIC still stores and enforces both fields explicitly for clarity and future-proofing.

#### 27.4f Telegram Operational Checks

Useful operational checks for a live Telegram adapter:

- `GET /channels`
  - verify `telegram` appears as configured + healthy
- `POST /channels/telegram/webhook/sync`
  - force webhook registration / repair
- `DELETE /channels/telegram/webhook`
  - clear webhook during maintenance
- `POST /channels/telegram/send`
  - send an explicit test message to a known `chat_id`
- `journalctl -u cosmic-gateway | grep telegram`
  - inspect inbound acceptance, allowlist rejects, webhook timing, outbound sends, typing actions, and media download timing

#### 27.4g Telegram Media Behavior

Telegram media is handled exactly like other attachment-capable channels in COSMIC:

- Adapter normalizes Telegram `photo`, `video`, `animation`, `audio`, `voice`, `document`, `sticker`, and `video_note` messages into attachment metadata
- Gateway persists those attachments into the common artifact/input-manifest path
- Downstream components consume them via `input_artifacts`
- Raw bytes are fetched lazily through:

```text
GET /internal/channels/telegram/media/{file_id}
```

This keeps Telegram aligned with the architecture's channel-agnostic attachment model rather than introducing Telegram-specific orchestration semantics.

### 27.5 Response Routing

When the Gateway creates a TaskEnvelope, it registers the originating **channel string** in a lookup table keyed by `task_id`. When events or responses arrive for that task, the Gateway resolves the platform adapter from that channel and sends to the concrete destination.

```python
# Gateway maintains task → channel mapping
active_task_channels: dict[str, str] = {}

# On task creation (dispatch_to_orchestrator, stream_from_haiku, etc.):
active_task_channels[task_id] = channel

# On task completion/failure:
del active_task_channels[task_id]

# For cron/heartbeat results: look up adapter from TaskEnvelope.channel
channel = task.channel or 'desktop'
adapter = channel_registry.get_adapter(channel)
await adapter.send(message, channel=channel)
```

**Fast path vs authority:** `active_task_channels` is an in-memory fast path, not the source of truth. The authoritative route is the persisted `channel` associated with the task/session state. After a desktop reconnect or Gateway restart, the concrete `desktop:<device_id>` connection is re-bound during `resume`, and live delivery continues for tasks carrying that channel.

**Cron/heartbeat delivery:** Cron definitions and heartbeat config include a `delivery_channel` field. When the result comes back, the Gateway uses this field to look up the correct adapter. If the specified channel is unavailable (e.g., user is offline on WhatsApp), the result is held in a pending queue and delivered on reconnect — same mechanism as the `user_input:requests` pending entries list (§3.12). Bare `desktop` means "deliver to the configured primary desktop device."

### 27.5a Channel Management Control Plane

Channel integrations have two distinct planes. They must not be conflated.

**1. Message / data plane**

- Purpose: receive human messages, normalize them, run them through `handle_query(...)`, and route responses back to the originating channel.
- Entry points:
  - Desktop WebSocket messages
  - Internal bridge intake routes such as `/internal/channels/whatsapp/incoming`
  - Public webhook adapters such as `POST /channels/telegram/webhook`
- Behavior:
  - Message arrives
  - Adapter normalizes it to `{ content, session_id, channel, metadata }`
  - Gateway applies session assembly, sticky routing, and model routing
  - `route='opus'` → TaskEnvelope → Redis → orchestrator
  - `route='haiku'|'perplexity'` → direct Gateway LLM path

**2. Channel management / control plane**

- Purpose: operational channel actions such as status checks, pairing, disconnect, relink, and bridge health inspection.
- Entry points:
  - Desktop-authenticated Gateway routes under `/channels/*`
  - Internal bridge callbacks under `/internal/channels/*`
- Behavior:
  - Explicit FastAPI route handlers decide the action
  - No model router classification
  - No orchestrator dispatch
  - No Redis unless a future operation is intentionally designed as a background task

**Design rule:** the Gateway determines which flow to execute from the route or message entrypoint, not by asking an LLM to infer intent. A desktop WebSocket `query` goes to the message pipeline. A desktop `POST /channels/whatsapp/pairing/qr` call goes to the control plane. A bridge `POST /internal/channels/whatsapp/incoming` call goes to the message pipeline after adapter normalization.

**Recommended implementation location:** channel-management HTTP handlers live in `gateway/channels/routes.py`, mounted by the existing Gateway FastAPI app alongside the other subsystem route modules (`credentials/routes.py`, `scheduler/routes.py`, `webhooks/routes.py`).

**Why separate the planes?**

1. Query routing is AI/runtime behavior; channel management is operational control.
2. Pairing/status/disconnect operations must be deterministic and auditable.
3. Channel management routes need standard request/response semantics for the desktop settings UI.
4. Keeping control actions out of the query pipeline avoids accidental model-router/orchestrator involvement in infrastructure operations.

**Example control-plane flow (WhatsApp QR):**

```text
Desktop Settings "Get QR"
  └── POST /channels/whatsapp/pairing/qr
        └── Gateway authenticates desktop token
              └── Gateway asks WhatsAppAdapter / bridge for fresh pairing QR
                    └── Bridge returns QR payload
                          └── Gateway returns QR payload to desktop
                                └── Desktop renders QR locally
```

**Example message-plane flow (WhatsApp text/image/audio/etc.):**

```text
WhatsApp user message
  └── Baileys Bridge receives socket event
        └── POST /internal/channels/whatsapp/incoming
              └── Gateway authenticates bridge token
                    └── WhatsApp adapter normalizes payload
                          └── handle_query(...)
                                ├── haiku/perplexity → direct Gateway route
                                └── opus → TaskEnvelope → Redis → orchestrator
```

### 27.6 Sidecar-Backed Adapters

Most adapters should remain in-process Python modules. Introduce a sidecar bridge only when the platform runtime forces it.

**Examples that justify a bridge:**

- The required client library is effectively single-runtime (for example, Node.js-only or vendor-maintained only in another language).
- The platform connection is a long-lived socket/session process that is operationally cleaner to isolate from the Gateway.
- The platform persists local device/session state that should not live inside Gateway code or `gateway/credentials.db`.

**Architecture rule:**

```text
Gateway core
  └── gateway/channels/<platform>.py      # Python ChannelAdapter
        └── talks to
bridges/<platform>_bridge/                # sidecar process (language/runtime as needed)
```

**Responsibility split:**

| Concern | Gateway ChannelAdapter | Sidecar Bridge |
|---|---|---|
| Session assignment | Owns | Never |
| `source` / `source_id` / `channel` tagging | Owns | Never |
| Model routing / sticky routing | Owns | Never |
| Task dispatch / response routing | Owns | Never |
| Provider-specific socket/session lifecycle | Never | Owns |
| Platform reconnect logic | Never | Owns |
| Platform auth/session/device state | Never | Owns |
| Platform send / receive primitives | Delegates | Owns |

**Auth model:**

- Bridge traffic is internal-only and authenticated.
- The bridge must not expose a public internet callback surface unless the platform itself requires it.
- Gateway internal OAuth credentials continue to live in `gateway/credentials.db` (§22).
- Channel/device auth state lives in `bridges/<name>_bridge/store/`.

**Implementation guidance for future integrations:**

1. Start with an in-process adapter by default.
2. Introduce a bridge only if the SDK/runtime requirements justify it.
3. Keep bridge persistence local to the bridge (`store/`) and Gateway-owned OAuth credentials in `gateway/credentials.db`.
4. Treat the bridge as a transport/auth shim, not a second Gateway.

### 27.7 WhatsApp Reference Implementation (Baileys)

WhatsApp is the reference sidecar-backed adapter because Baileys runs in Node.js and maintains its own device/session auth state.

**Folder structure:**

```text
gateway/channels/whatsapp.py              # Python ChannelAdapter
bridges/whatsapp_bridge/
├── package.json                          # Node.js runtime/dependencies (Baileys 6.7.21)
├── src/
│   └── index.js                          # Baileys socket lifecycle + Express bridge API
├── store/
│   ├── auth/                             # PERSISTENT. Baileys multi-file auth state
│   └── bridge-config.json                # PERSISTENT. Bridge-level config (allowed phone, self-chat-only)
└── runtime/                              # EPHEMERAL. Logs, cache, temp files
    └── inbound_media/                    # Optional temp storage for downloaded/decrypted inbound media
```

**Auth/state placement:**

- Baileys auth files live in `bridges/whatsapp_bridge/store/auth/`.
- These files are persistent runtime state, not source code.
- They are not OAuth credentials and do not belong in `gateway/credentials.db`.
- In containerized deployments, map `bridges/whatsapp_bridge/store/` to a persistent volume.
- In bare-metal / VM deployments, keep the same logical separation: version-controlled code in the repo, persistent auth state on durable disk.
- Typical VM deployment pattern: keep bridge code under the repo checkout and point `WHATSAPP_AUTH_DIR` at an absolute persistent path such as `/var/lib/cosmic/whatsapp/auth`.

**Operational considerations:**

1. The Gateway talks to the bridge over an internal authenticated channel only.
2. The bridge is responsible for QR/pairing state, reconnects, and send/receive primitives.
3. The Gateway remains the single entry point for COSMIC logic: session context, routing, response policy, and task dispatch.
4. `gateway/channels/whatsapp.py` is still the canonical adapter that the Channel Adapter Registry knows about. The Node bridge is behind it.
5. If a future WhatsApp integration uses an official provider API with OAuth credentials, those provider credentials belong in `gateway/credentials.db`; Baileys-style local device auth still belongs in bridge `store/`.

**Baileys protocol version management (CRITICAL):**

WhatsApp servers periodically expire old client protocol versions. Baileys ships with a hardcoded version number that will eventually become stale, causing `Connection failure (405)` during QR pairing. The bridge **must** dynamically fetch the current WhatsApp Web protocol version at connection time using Baileys' `fetchLatestBaileysVersion()` API:

```javascript
import makeWASocket, {
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  Browsers,
} from '@whiskeysockets/baileys';

// Before creating the socket:
let waVersion;
try {
  const { version } = await fetchLatestBaileysVersion();
  waVersion = version;
  console.log(`Fetched latest WhatsApp Web version: ${version.join('.')}`);
} catch (err) {
  console.warn('Failed to fetch latest version, using Baileys default:', err?.message);
}

const sock = makeWASocket({
  auth: state,
  ...(waVersion ? { version: waVersion } : {}),
  browser: Browsers.macOS('Google Chrome'),  // deliberate fingerprint — see note below
  connectTimeoutMs: 60_000,
  defaultQueryTimeoutMs: 0,
  // ...
});
```

Without this dynamic fetch, the bridge will work fine while the Baileys-bundled version is current, then silently break when WhatsApp rotates it. This is the single most common cause of Baileys pairing failures and has been a persistent production issue.

**Browser fingerprint note:** The `Browsers.macOS('Google Chrome')` fingerprint is deliberate. It presents the linked device as a macOS Chrome session to WhatsApp. This is a widely-used safety convention in the Baileys community to reduce the risk of account restrictions. It is cosmetic only — WhatsApp's "Linked Devices" screen will show "Mac OS" for this session.

**Allowed-phone enforcement:**

The bridge supports restricting inbound and outbound traffic to a single configured phone number. This is persisted in `store/bridge-config.json` and enforced at the bridge level:

- **Inbound:** Messages from numbers other than the allowed phone are silently dropped before forwarding to the Gateway.
- **Outbound:** Send requests targeting numbers other than the allowed phone are rejected with `403`.
- **Self-chat-only mode:** When `selfChatOnly` is true, only messages from the linked WhatsApp account itself are processed (useful for personal-assistant mode where the user talks to themselves).
- **Config persistence:** The bridge reads `store/bridge-config.json` on startup and updates it via `POST /config`. The Gateway adapter proxies config read/write through `GET/POST /channels/whatsapp/config`.

**Sender identity normalization (IMPORTANT):** modern WhatsApp traffic may include both a phone-backed sender identity and a Linked Identity (`@lid`) for the same user. Allowed-phone enforcement must normalize the sender using the phone-backed identity (`pnJid`) when present and only fall back to the LID if no phone-backed JID is available. Otherwise, valid messages from the configured allowed number can be incorrectly dropped as "outside configured user scope" even though the human sender is correct.

#### 27.7.1 Bridge -> Gateway Inbound Contract

The Bridge posts inbound WhatsApp traffic to an internal Gateway route such as `/internal/channels/whatsapp/incoming`. This route is owned by the Gateway and hands the payload to `gateway/channels/whatsapp.py`.

**Hard requirements:**

1. The payload must be delivered over an internal authenticated channel only.
2. The Bridge must forward **all** inbound WhatsApp message types, not just plain text.
3. The Bridge must preserve attachment metadata for images, video, audio, voice notes, documents, stickers, contacts, locations, reactions, button/list replies, poll replies, and future unknown message types.
4. The Gateway adapter normalizes this into COSMIC's internal `{ content, session_id, channel, metadata }` format. The Bridge does not assign sessions.

**Ingress ownership:** only the Bridge receives real WhatsApp traffic from Baileys. The Gateway receives WhatsApp messages second-hand from the Bridge over the internal route above. The Gateway must not embed a second WhatsApp socket or create a separate FastAPI service just for WhatsApp.

**Recommended payload shape:**

```json
{
  "schema_version": 1,
  "event": "message.inbound",
  "event_id": "evt_123",
  "sender": {
    "jid": "15551234567@s.whatsapp.net",
    "phone": "+15551234567",
    "push_name": "Alice"
  },
  "chat": {
    "jid": "15551234567@s.whatsapp.net",
    "type": "dm"
  },
  "message": {
    "id": "wamid_abc",
    "type": "image",
    "text": null,
    "caption": "look at this",
    "timestamp_unix_ms": 1710000000000,
    "quoted_message_id": null,
    "mentions": [],
    "attachments": [
      {
        "id": "att_1",
        "kind": "image",
        "mime_type": "image/jpeg",
        "filename": null,
        "size_bytes": 183920,
        "width": 1280,
        "height": 720,
        "duration_ms": null,
        "sha256": "base64-or-hex-hash",
        "bridge_media_ref": "wamid_abc:att_1",
        "download_url": "http://127.0.0.1:8091/media/wamid_abc/att_1"
      }
    ]
  }
}
```

**Normalization rule:** if a message has no user-visible text, the adapter still emits a non-empty `content` placeholder such as `[image]`, `[video]`, `[voice note]`, `Location shared`, or `[unsupported whatsapp message]`. This ensures every inbound event enters the Gateway even before downstream media processing is implemented.

**Media storage rule:** raw inbound media remains Bridge-owned at intake time. If the Bridge downloads or decrypts media, it stores temporary files under `bridges/whatsapp_bridge/runtime/` (for example `runtime/inbound_media/`) and exposes stable references such as `bridge_media_ref` and/or an internal authenticated `download_url`. The Gateway and `gateway/channels/whatsapp.py` store metadata plus references during intake; they should **not** treat a local filesystem path as the primary cross-process contract.

**Downstream processing rule:** when later processing actually needs the media bytes, the downstream consumer fetches them via the Bridge reference/URL and may then persist its own working copy or artifact in the appropriate durable location. Bridge runtime media is ephemeral transport state, not long-term storage.

#### 27.7.2 Gateway -> Bridge Outbound Contract

The Gateway adapter sends outbound WhatsApp text via the Bridge's internal send API.

**Bridge endpoints:**

- `GET /health` — internal liveness/readiness probe
- `GET /status` — internal connection state (returns `pairing_state`, `qr` string if available, `connected` flag)
- `POST /send` — send one WhatsApp message
- `GET /config` — retrieve bridge-level config (allowed phone, self-chat-only flag)
- `POST /config` — update bridge-level config; persisted to `store/bridge-config.json`
- `POST /pairing/qr` — create or refresh a renderable QR payload for WhatsApp device linking
- `DELETE /session` — disconnect and clear bridge-owned Baileys auth state

All bridge endpoints are authenticated via `X-Bridge-Token` header. The token value is set by the `WHATSAPP_BRIDGE_TOKEN` environment variable. The Gateway adapter (`gateway/channels/whatsapp.py`) reads the bridge base URL from `WHATSAPP_BRIDGE_URL` (default `http://127.0.0.1:3000`) and authenticates with `X-Bridge-Token`.

**Send request shape:**

```json
{
  "number": "+15551234567",
  "message": "Rendered text chunk"
}
```

The Gateway adapter owns delivery policy:

1. `response.chunk` events are buffered in the adapter; they are **not** forwarded token-by-token to WhatsApp.
2. `response.complete` sends the final conversational text.
3. `task.input_required` sends the question plus numbered options.
4. `task.progress` may be rate-limited by the adapter to avoid chat spam.
5. `task.failed` and `error` are rendered into concise failure text.

#### 27.7.3 WhatsApp Text Chunking Policy

WhatsApp delivery must be chunk-aware. The adapter, not the Bridge, is responsible for splitting long text into ordered sends.

**Required behavior:**

1. Default chunk limit: `4000` characters.
2. Default mode: `newline` — prefer paragraph boundaries first, then line boundaries, then sentence boundaries, then hard wrap.
3. Alternative mode: `length` — direct hard wrapping at the configured limit.
4. Chunks are delivered sequentially per destination channel, with a small inter-send delay to preserve order.
5. The chunker must not drop content. Long code blocks may be split, but the text must remain complete.

#### 27.7.4 WhatsApp Pairing / QR Flow

WhatsApp account linking is a **channel management control-plane flow**, not a query flow.

**User experience goal:** the user clicks a `Get QR` button in the desktop app settings, sees a scannable QR locally, scans it with WhatsApp on their phone, and the Bridge transitions to connected state automatically.

**Architecture rule:** the Gateway brokers this pairing flow, but it does not own the Baileys socket lifecycle. The WhatsApp Bridge remains the runtime component responsible for generating pairing state, receiving the pairing confirmation from WhatsApp, and persisting device auth state.

**Recommended flow:**

```text
1. User clicks "Get QR" in Desktop Settings
2. Desktop calls Gateway: POST /channels/whatsapp/pairing/qr
3. Gateway authenticates the desktop/local API token (Authorization: Bearer <GATEWAY_LOCAL_API_TOKEN>)
4. Gateway resolves the registered WhatsAppAdapter
5. WhatsAppAdapter calls the Bridge's internal pairing endpoint (POST /pairing/qr, X-Bridge-Token auth)
6. Bridge calls fetchLatestBaileysVersion() to get the current WhatsApp Web protocol version
7. Bridge starts or refreshes pairing state inside the existing Baileys process with the fetched version
8. Bridge returns a renderable QR payload (raw QR string)
9. Gateway returns that payload to the desktop app
10. Desktop renders the QR locally (using qrcode library)
11. User scans it from WhatsApp on their phone
12. Baileys receives the successful pairing/connection update automatically
13. Bridge persists auth state in store/auth/ and reports `connected`
14. Desktop checks `GET /channels/whatsapp/status` or receives a later status update
```

**Critical prerequisite:** Step 6 (dynamic version fetch) is essential. Without it, QR pairing will fail with `Connection failure (405)` once the Baileys-bundled protocol version expires on WhatsApp's servers. See "Baileys protocol version management" in §27.7 above.

**Bridge-side control endpoints** (internal, Gateway-only):

- `GET /health` — process liveness/readiness
- `GET /status` — connection/pairing state (`pairing_state`, `qr`, `connected`)
- `POST /pairing/qr` — create or refresh a renderable pairing QR payload
- `DELETE /session` — disconnect and clear bridge-owned device auth state
- `GET /config` — retrieve bridge-level config (allowed phone, self-chat-only)
- `POST /config` — update bridge-level config, persisted to `store/bridge-config.json`

These are Bridge implementation details behind `gateway/channels/whatsapp.py`. The desktop app never calls them directly — it calls the Gateway's `/channels/whatsapp/*` routes, which proxy to the bridge.

**Operational rule:** this flow assumes the WhatsApp Bridge is already managed as a long-running service (`systemd` on a VM, `supervisord` in a container deployment per §9). The Gateway should request pairing state from the running Bridge. It should **not** become the process manager that starts/stops the Bridge on every QR request.

**Why keep the Bridge running?**

1. Once paired, the same process must remain available to receive WhatsApp traffic.
2. Process supervision belongs to deployment/runtime management, not to a desktop button click.
3. On-demand service startup adds race conditions around readiness, reconnect, and auth persistence.

**Desktop → Gateway → Bridge connectivity:**

The desktop app connects directly to the Gateway's public HTTPS/WSS endpoint using the provisioned `gateway_url`. Recommended deployment: Caddy listens publicly on `:443`, terminates TLS, and reverse-proxies to the Gateway. There is no SSH tunnel or VPN layer between the desktop and Gateway. The `GATEWAY_LOCAL_API_TOKEN` provides the authentication boundary.

```text
Desktop App (Electron)
  └── HTTPS / WSS (direct, over public internet)
        └── Caddy / TLS edge (:443, public)
              └── Gateway (FastAPI, 127.0.0.1:8080)
                    └── HTTP (localhost only)
                          └── WhatsApp Bridge (Express, 127.0.0.1:3000)
                                └── WebSocket (Baileys)
                                      └── WhatsApp servers
```

The Gateway → Bridge link is internal-only (`127.0.0.1`). The bridge does not need to be reachable from outside the VM.

**Desktop / Gateway / Bridge responsibility split during pairing:**

| Concern | Desktop App | Gateway | WhatsApp Bridge |
|---|---|---|---|
| User clicks `Get QR` | Owns | Never | Never |
| Authenticate the request | Never | Owns | Never |
| Decide this is a control-plane action | Never | Owns | Never |
| Generate/refresh QR pairing state | Never | Delegates | Owns |
| Render QR for the user | Owns | Returns payload only | Never |
| Detect successful scan / pairing | Never | Observes status only | Owns |
| Persist Baileys auth/device state | Never | Never | Owns |

**Status model:** `GET /channels/whatsapp/status` should return control-plane state useful for the desktop settings UI, for example:

- `disconnected`
- `pairing_required`
- `pairing_qr_ready`
- `connected`
- `bridge_unreachable`
- `error`

This route is for operational visibility only. It is not part of the message routing pipeline and does not create TaskEnvelopes.

---

## 28. Internal Hooks

The Hooks Engine is a Gateway module that fires TaskEnvelopes in response to internal state changes. Hooks provide lifecycle automation — setup on startup, cleanup on shutdown, memory operations on session boundaries.

### 28.1 Hook Types

| Hook | Fires When | Typical Use |
|---|---|---|
| `gateway.startup` | Gateway process starts | Load saved preferences, warm caches, verify service health |
| `gateway.shutdown` | Gateway process is stopping (SIGTERM) | Save in-progress state, flush pending memories |
| `session.reset` | Daily session reset (4 AM — see §23.2) | Archive day summary, promote important memories |
| `session.compact` | Context compaction triggers (§23.3) | Extract high-value facts before summarization |
| `agent.registered` | New agent registers with the registry | Update routing config, notify orchestrator |
| `agent.deregistered` | Agent is removed or deprecated | Update routing, alert if critical capability lost |
| `heartbeat.missed` | Agent misses heartbeat beyond TTL | Trigger health investigation, potential restart |
| `task.dlq` | Task sent to dead letter queue | Notify user, log for review |

### 28.2 Hook Definitions

```python
# gateway/hooks/definitions.py

BUILT_IN_HOOKS = {
    'gateway.startup': {
        'prompt': 'COSMIC gateway has started. Check system health and report any issues.',
        'priority': 'normal',
        'enabled': True,
    },
    'gateway.shutdown': {
        'prompt': 'COSMIC gateway is shutting down. Save any in-progress state.',
        'priority': 'high',
        'enabled': True,
    },
    'session.reset': {
        'prompt': 'Daily session reset. Review yesterday\'s conversation and promote any important facts to long-term memory.',
        'priority': 'low',
        'enabled': True,
    },
    'agent.deregistered': {
        'prompt': 'Agent {agent_id} has been deregistered. Verify no critical capabilities are lost.',
        'priority': 'normal',
        'enabled': True,
    },
    'task.dlq': {
        'prompt': 'Task {task_id} has been sent to the dead letter queue after {attempts} failed attempts. Error: {error}. Notify the user.',
        'priority': 'high',
        'enabled': True,
    },
}
```

### 28.3 Hook Engine

```python
# gateway/hooks/engine.py

class HooksEngine:
    """Fires TaskEnvelopes in response to internal state changes."""

    def __init__(self, redis, hook_definitions: dict):
        self.redis = redis
        self.hooks = hook_definitions

    async def fire(self, hook_name: str, context: dict | None = None):
        """Fire a hook event. Creates a TaskEnvelope and dispatches
        to the orchestrator."""
        hook = self.hooks.get(hook_name)
        if not hook or not hook.get('enabled', True):
            return

        prompt = hook['prompt']
        if context:
            prompt = prompt.format(**context)

        task_id = generate_task_id()
        task = TaskEnvelope(
            task_id=task_id,
            task_list_id=f'hook:{hook_name}',
            session_id=None,
            sender='cosmic/gateway:1.0.0',
            recipient='cosmic/orchestrator:1.0.0',
            intent='orchestrator.process',
            input={'query': prompt, 'hook_context': context or {}},
            idempotency_key=str(uuid4()),
            priority=hook['priority'],
            source='hook',
            source_id=f'hook_{hook_name}',
            channel=None,       # hooks are internal — no user-facing delivery by default
            signature='',
        )
        task.signature = sign_task(task, GATEWAY_SIGNING_SECRET)
        await dispatch(task, self.redis)

# Usage in Gateway lifecycle:
# On startup:
await hooks_engine.fire('gateway.startup')

# On session reset (4 AM):
await hooks_engine.fire('session.reset', {'date': '2025-01-15'})

# On agent deregistration:
await hooks_engine.fire('agent.deregistered', {'agent_id': 'cosmic/research-agent:1.0.0'})

# On DLQ:
await hooks_engine.fire('task.dlq', {
    'task_id': 'tsk_abc123',
    'attempts': 3,
    'error': 'NETWORK_ERROR: upstream timeout',
})
```

---

## 29. Browser & System Automation Agents

### 29.1 Browser Agent

The Browser Agent is a specialist for browser automation via Playwright. It navigates pages, fills forms, clicks elements, extracts content, and takes screenshots. It runs in a sandboxed browser context — no access to the user's actual browser profile or cookies.

**Agent Card:**

```yaml
# agents/browser_agent/agent_card.yaml
agent_id: cosmic/browser-agent:1.0.0
display_name: Browser Agent
description: >
  Specialist agent for browser automation. Navigates web pages,
  fills forms, clicks elements, extracts structured content,
  takes screenshots, and manages multi-step web workflows.

intents:
  - name: browser.navigate
    description: Navigate to a URL and extract page content
    input_schema: schemas/intents/browser.navigate.input.json
    output_schema: schemas/intents/browser.navigate.output.json
    timeout_sec: 120

  - name: browser.interact
    description: Perform a multi-step interaction on a web page (fill, click, wait, etc.)
    input_schema: schemas/intents/browser.interact.input.json
    output_schema: schemas/intents/browser.interact.output.json
    timeout_sec: 180

  - name: browser.extract
    description: Extract structured data from a web page (tables, lists, specific elements)
    input_schema: schemas/intents/browser.extract.input.json
    output_schema: schemas/intents/browser.extract.output.json
    timeout_sec: 60

  - name: browser.screenshot
    description: Take a screenshot of a web page or specific element
    input_schema: schemas/intents/browser.screenshot.input.json
    output_schema: schemas/intents/browser.screenshot.output.json
    timeout_sec: 30

artifact_types:
  - screenshot
  - extracted_data
  - page_content

policies:
  network_access: true
  writable_paths:
    - runs/artifacts/browser_agent
    - agents/browser_agent/store
    - agents/browser_agent/runtime
  tool_access:
    - playwright_navigate
    - playwright_click
    - playwright_fill
    - playwright_screenshot
    - playwright_evaluate
    - file_write
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  sandbox:
    browser_profile: isolated          # never uses user's real browser profile
    network_allowlist: ['*']           # all domains (restrict per-deployment if needed)
    download_dir: runtime/downloads/   # sandboxed download directory
    max_pages: 5                       # max concurrent browser pages

sla:
  max_concurrency: 2
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: 180
  retry_policy:
    max_attempts: 2
    backoff: exponential
    backoff_base_sec: 5
    retryable_codes: [TIMEOUT, NETWORK_ERROR]
    non_retryable_codes: [INVALID_INPUT, NAVIGATION_ERROR]

stream_key: streams:cosmic/browser-agent:1.0.0
```

### 29.2 System Agent

The System Agent handles OS-level automation: file system operations, process management, clipboard access, and shell command execution. It is sandboxed by declared tool policies and writable path allowlists.

**Agent Card:**

```yaml
# agents/system_agent/agent_card.yaml
agent_id: cosmic/system-agent:1.0.0
display_name: System Agent
description: >
  Specialist agent for operating system automation. Manages files,
  processes, clipboard, and executes shell commands within declared
  security boundaries.

intents:
  - name: system.file_operation
    description: Read, write, copy, move, or delete files/directories
    input_schema: schemas/intents/system.file_operation.input.json
    output_schema: schemas/intents/system.file_operation.output.json
    timeout_sec: 60

  - name: system.process_manage
    description: List, start, stop, or monitor system processes
    input_schema: schemas/intents/system.process_manage.input.json
    output_schema: schemas/intents/system.process_manage.output.json
    timeout_sec: 30

  - name: system.shell_execute
    description: Execute a shell command and return output
    input_schema: schemas/intents/system.shell_execute.input.json
    output_schema: schemas/intents/system.shell_execute.output.json
    timeout_sec: 120

  - name: system.clipboard
    description: Read from or write to the system clipboard
    input_schema: schemas/intents/system.clipboard.input.json
    output_schema: schemas/intents/system.clipboard.output.json
    timeout_sec: 5

artifact_types:
  - file
  - command_output

policies:
  network_access: false              # system agent operates locally
  writable_paths:
    - runs/artifacts/system_agent
    - agents/system_agent/store
    - agents/system_agent/runtime
    # Additional writable paths are declared per-deployment (e.g., ~/Documents, ~/Projects)
  tool_access:
    - file_read
    - file_write
    - file_delete
    - file_move
    - process_list
    - process_start
    - process_stop
    - shell_execute
    - clipboard_read
    - clipboard_write
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  sandbox:
    shell_allowlist:                   # commands the agent may execute
      - ls
      - cat
      - grep
      - find
      - wc
      - pip
      - npm
      - git
      - python
      - node
    shell_denylist:                    # explicitly blocked commands
      - rm -rf /
      - sudo
      - chmod 777
      - mkfs
    max_file_size_mb: 100              # max file size for read/write operations

sla:
  max_concurrency: 2
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: 120
  retry_policy:
    max_attempts: 2
    backoff: exponential
    backoff_base_sec: 2
    retryable_codes: [TIMEOUT]
    non_retryable_codes: [INVALID_INPUT, PERMISSION_DENIED, AUTH_ERROR]

stream_key: streams:cosmic/system-agent:1.0.0
```

---

## 30. CLI Agent

> **Note:** The CLI Agent has access to the StepPlan universal tool (see §32) like all agents. Given its unrestricted access and complex multi-step operations, the CLI Agent's StepPlan usage is particularly important — every plan is logged to `cli_audit` alongside command logs.

The CLI Agent is an alpha-stage embedded terminal assistant with **full system access**. It can read and modify agent code, prompts, configurations, and system state. It is the maintenance and development console for the entire COSMIC runtime.

**Design philosophy:** Every complex system needs a "root shell" — a way for the operator to inspect, diagnose, and modify the system itself. The CLI Agent is that root shell, but AI-powered. It operates like Claude Code embedded inside COSMIC.

### 30.1 Capabilities

| Capability | Scope | Example |
|---|---|---|
| **Read agent code** | All `agents/*/` directories | "Show me the research agent's system prompt" |
| **Modify agent prompts** | `agents/*/prompts/`, `agents/*/skills/` | "Update the docs agent's formatting rules" |
| **Edit configurations** | `routing.yaml`, `supervisord.conf`, agent cards | "Add a new intent to the research agent" |
| **Inspect system state** | Redis keys, SQLite databases, logs | "Show me all pending tasks in the orchestrator queue" |
| **Run diagnostics** | Health checks, event replay, memory inspection | "Why did the last docs task fail?" |
| **Execute shell commands** | Unrestricted within the VM | "Restart the research agent process" |
| **Manage crons** | Full CRUD via Gateway Scheduler API | "List all active cron jobs and disable the nightly one" |
| **Inspect memory** | Qdrant queries, .md file reads, session history | "What does the system remember about my email preferences?" |

### 30.2 Agent Card

```yaml
# agents/cli_agent/agent_card.yaml
agent_id: cosmic/cli-agent:1.0.0
display_name: CLI Agent
description: >
  Alpha-stage embedded terminal assistant with full system access.
  Sleeps by default, wakes on demand. Can read and modify agent code,
  prompts, configurations, and system state. Operates as a root-level
  maintenance console for the COSMIC runtime.

intents:
  - name: cli.execute
    description: Execute a CLI command or multi-step system operation
    input_schema: schemas/intents/cli.execute.input.json
    output_schema: schemas/intents/cli.execute.output.json
    timeout_sec: 300

policies:
  network_access: true
  writable_paths:
    - '*'                              # full filesystem access within the VM
  tool_access:
    - shell_execute
    - file_read
    - file_write
    - file_delete
    - redis_query
    - sqlite_query
    - process_manage
    - supervisord_control
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  safety:
    require_confirmation: true         # orchestrator MUST get user confirmation before dispatch
    audit_all_commands: true           # every command logged to credential_audit equivalent
    alpha_warning: true                # UI shows alpha warning before invocation

sla:
  max_concurrency: 1                   # single instance only — no parallel CLI sessions
  heartbeat_interval_sec: 30
  heartbeat_ttl_sec: 90
  max_task_duration_sec: 300
  retry_policy:
    max_attempts: 1                    # no retries — destructive commands must not retry
    retryable_codes: []
    non_retryable_codes: ['*']

stream_key: streams:cosmic/cli-agent:1.0.0
```

### 30.3 Sleeping Mode

The CLI Agent runs in **sleeping mode** by default. It is not started by supervisord (`autostart=false`). When the orchestrator needs the CLI Agent, it starts it on demand via supervisord's XML-RPC API, dispatches the task, and the agent goes back to sleep after completion.

```python
# Orchestrator wakes CLI agent on demand
async def wake_cli_agent(self):
    """Start the CLI agent via supervisord. It will register,
    process the task, then exit (autorestart=false)."""
    proc = await asyncio.create_subprocess_exec(
        'supervisorctl', 'start', 'cli_agent',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    # Wait for agent to register (poll registry)
    for _ in range(30):
        instance = await find_available_instance('cli.execute', self.redis)
        if instance[0]:
            return True
        await asyncio.sleep(1)
    return False
```

### 30.4 Safety & Audit

The CLI Agent has unrestricted access by design — it is the "break glass" tool. Safety is enforced by:

1. **User confirmation required.** The orchestrator MUST get explicit user approval before dispatching any task to the CLI Agent. This is enforced via the `require_confirmation: true` policy — the orchestrator's dispatch logic checks this flag and routes through `user.input_required` before dispatch.

2. **Full audit trail.** Every command executed by the CLI Agent is logged to an audit table with: timestamp, command, output, user who approved, task_id.

3. **Single concurrency.** Only one CLI session at a time (`max_concurrency: 1`). No parallel destructive operations.

4. **No automatic retry.** `max_attempts: 1`. If a command fails, it fails. No retry loop that might repeat a destructive action.

5. **Alpha status.** The UI displays a clear alpha warning before invocation. The agent's prompts include guardrails about confirming destructive operations with the user mid-task.

```python
# CLI Agent audit logging
async def log_cli_command(task_id: str, command: str, output: str,
                          approved_by: str):
    db.execute('''
        INSERT INTO cli_audit
        (timestamp, task_id, command, output, output_truncated,
         approved_by, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', [
        utcnow(), task_id, command,
        output[:10000],                # truncate large outputs
        len(output) > 10000,
        approved_by,
        'cosmic/cli-agent:1.0.0',
    ])
```

### 30.5 Hard Rules

1. **CLI Agent is alpha.** It ships for development and diagnostics. It is NOT a production automation tool.
2. **User must approve every invocation.** The orchestrator never dispatches to the CLI Agent without explicit user confirmation.
3. **Single instance, no retry.** Prevents parallel destructive operations and retry-induced damage.
4. **Full audit.** Every command is logged. No silent execution.
5. **Sleeping by default.** Does not consume resources when not in use. Does not restart after exit.

---

## 31. Orchestrator Task Planner

The Orchestrator Task Planner is the mechanism by which the orchestrator decomposes complex user requests into structured plans, tracks execution of each step, manages multiple concurrent plans, and provides observability into what the system is doing and why.

**Design principle:** The orchestrator is an LLM-powered coordinator. When a task arrives (route=opus), the orchestrator doesn't just pick one agent and fire — it reasons about the request, breaks it into steps, identifies dependencies, and executes them in order. The Task Planner formalizes this process. Simple requests (single intent, one agent) skip planning entirely. Complex requests (multi-step, multi-agent) get a structured plan before any dispatch happens.

### 31.1 Task Ledger Schema

The orchestrator maintains a SQLite ledger that tracks all tasks and plans. This schema formalizes the `tasks` table referenced in §12.7 and §16.3, and adds plan-level tracking.

```sql
-- agents/orchestrator/store/data/task_ledger.db

-- ═══════════════════════════════════════════════════════════
-- TASKS: Every TaskEnvelope the orchestrator creates or receives
-- ═══════════════════════════════════════════════════════════

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    plan_id TEXT,                              -- NULL for simple (planless) tasks
    parent_task_id TEXT,                       -- subtask tree linkage (from TaskEnvelope)
    session_id TEXT,
    recipient TEXT NOT NULL,                   -- target agent_id
    intent TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'pending',    -- see lifecycle below
    envelope_json TEXT NOT NULL,               -- full TaskEnvelope for redispatch
    result_json TEXT,                          -- AgentResult on completion
    error_json TEXT,                           -- AgentError on failure
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    source TEXT DEFAULT 'user',                -- propagated from parent
    source_id TEXT,
    channel TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE INDEX idx_tasks_plan ON tasks(plan_id);
CREATE INDEX idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX idx_tasks_session ON tasks(session_id);

-- Task status lifecycle:
--   pending → dispatched → accepted → in_progress → completed
--                                                  → failed → (retry → superseded + new task | dlq)
--                                    → suspended → resumed → in_progress
--                       → rejected → (redrive → dispatched)
--                       → superseded  (replaced by retry — terminal state, prevents zombie rows)

-- ═══════════════════════════════════════════════════════════
-- PLANS: Structured decomposition of complex requests
-- ═══════════════════════════════════════════════════════════

CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,                  -- 'plan_abc123'
    session_id TEXT,
    original_query TEXT NOT NULL,              -- the user's original request
    status TEXT NOT NULL DEFAULT 'planning',   -- planning → executing → completed → failed
    total_steps INTEGER DEFAULT 0,
    completed_steps INTEGER DEFAULT 0,
    current_step INTEGER DEFAULT 0,            -- which step is actively executing
    plan_json TEXT NOT NULL,                   -- structured plan (see PlanStep model)
    source TEXT DEFAULT 'user',
    source_id TEXT,
    channel TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP
);

CREATE INDEX idx_plans_status ON plans(status);
CREATE INDEX idx_plans_session ON plans(session_id);

-- ═══════════════════════════════════════════════════════════
-- PLAN STEPS: Individual steps within a plan
-- ═══════════════════════════════════════════════════════════

CREATE TABLE plan_steps (
    step_id TEXT PRIMARY KEY,                  -- 'step_001'
    plan_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,              -- execution order (1-based)
    description TEXT NOT NULL,                 -- human-readable: "Search for recent papers on quantum error correction"
    intent TEXT,                               -- target intent: 'research.topic' (NULL if orchestrator handles directly)
    agent_id TEXT,                             -- target agent (NULL if orchestrator handles directly)
    depends_on TEXT,                           -- JSON array of step_ids this step waits for: '["step_001"]'
    status TEXT NOT NULL DEFAULT 'pending',    -- pending → in_progress → completed → failed → skipped
    task_id TEXT,                              -- linked TaskEnvelope.task_id once dispatched
    attempt INTEGER DEFAULT 0,                -- current retry attempt for this step (increments across task rows)
    max_attempts INTEGER DEFAULT 3,           -- max retries (sourced from agent_card SLA on plan creation)
    input_json TEXT,                           -- planned input for this step (may reference prior step outputs)
    output_json TEXT,                          -- result from the agent (populated on completion)
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX idx_steps_plan ON plan_steps(plan_id, step_number);
CREATE INDEX idx_steps_status ON plan_steps(plan_id, status);
```

### 31.2 Planning Loop

When the orchestrator receives a TaskEnvelope (intent: `orchestrator.process`), it decides whether the request needs a plan or can be handled directly.

```python
# agents/orchestrator/planner.py

async def process_request(self, task: TaskEnvelope, context: dict):
    """Main orchestrator entry point for incoming requests."""
    query = task.input['query']

    # Step 1: Fast-path — single intent, no planning needed
    #   "Send this email" → docs.edit, one agent, done.
    #   LLM classifies: is this a simple single-step task?
    classification = await self.classify_complexity(query, context)

    if classification['complexity'] == 'simple':
        # Direct dispatch — no plan needed
        intent = classification['intent']
        await self.dispatch_simple(task, intent, context)
        return

    # Step 2: Complex request — create a structured plan
    plan = await self.create_plan(task, query, context)

    # Step 3: Execute the plan step by step
    await self.execute_plan(plan, task, context)


async def classify_complexity(self, query: str, context: dict) -> dict:
    """Ask the LLM: is this a single-step or multi-step task?
    Returns: { complexity: 'simple'|'complex', intent: str|None }

    Uses the same model router (Opus) — this is a lightweight
    classification, not a full planning call. Typical RTT: 1-2s.

    Fallback: if the LLM call fails, times out, or returns invalid JSON,
    default to 'complex'. A plan with one step is functionally equivalent
    to simple dispatch, so this is the safe default."""
    try:
        response = await self.llm.classify(
            system='You are a task decomposition classifier for a multi-agent system.',
            prompt=f'''Given this request and the available agent intents, determine:
1. Is this a single-step task (one agent, one intent) or multi-step (multiple agents or sequential steps)?
2. If single-step, which intent handles it?

Available intents: {json.dumps(self.available_intents)}

Request: {query}
Session context: {json.dumps(context.get('recent_messages', [])[-3:])}

Respond as JSON: {{ "complexity": "simple"|"complex", "intent": "intent_name"|null }}''',
        )
        result = json.loads(response)
        if result.get('complexity') not in ('simple', 'complex'):
            raise ValueError(f'Invalid complexity: {result.get("complexity")}')
        return result
    except Exception as e:
        logger.warning(f'classify_complexity failed, defaulting to complex: {e}')
        return {'complexity': 'complex', 'intent': None}
```

### 31.3 Plan Creation

For complex requests, the orchestrator calls the LLM to decompose the task into ordered steps with dependencies.

```python
async def create_plan(self, task: TaskEnvelope, query: str,
                       context: dict) -> Plan:
    """LLM-driven task decomposition. Creates a structured plan
    with steps, dependencies, and agent assignments."""

    plan_id = f'plan_{uuid4().hex[:12]}'

    # LLM creates the plan
    plan_response = await self.llm.plan(
        system='''You are a task planner for a multi-agent system.
Decompose the user's request into concrete, ordered steps.
Each step should map to exactly one agent intent (or 'orchestrator' for decisions/synthesis).
Identify dependencies between steps — which steps need outputs from previous steps.
Keep plans minimal — don't over-decompose simple parts.''',
        prompt=f'''Available agent intents:
{json.dumps(self.available_intents, indent=2)}

User request: {query}

Recent context: {json.dumps(context.get('recent_messages', [])[-5:])}

Create a plan as JSON:
{{
  "steps": [
    {{
      "step_number": 1,
      "description": "human readable description",
      "intent": "agent.intent_name",
      "agent_id": "cosmic/agent-name:version",
      "depends_on": [],
      "input_template": {{ ... }}
    }},
    ...
  ],
  "reasoning": "brief explanation of why this decomposition"
}}''',
    )
    plan_data = json.loads(plan_response)

    # ── Validate plan before persisting ─────────────────────
    valid_step_numbers = {s['step_number'] for s in plan_data['steps']}

    for step in plan_data['steps']:
        # Validate agent_id exists in registry
        agent_id = step.get('agent_id')
        if agent_id and not self.registry.get_card(agent_id):
            raise PlanValidationError(
                f'Step {step["step_number"]}: unknown agent_id "{agent_id}"'
            )

        # Validate intent exists in routing config
        intent = step.get('intent')
        if intent and intent not in self.available_intents:
            raise PlanValidationError(
                f'Step {step["step_number"]}: unknown intent "{intent}"'
            )

        # Validate dependency references point to real steps
        for dep in step.get('depends_on', []):
            dep_num = dep if isinstance(dep, int) else int(dep)
            if dep_num not in valid_step_numbers:
                raise PlanValidationError(
                    f'Step {step["step_number"]}: depends_on references '
                    f'non-existent step {dep_num}'
                )

    # Detect dependency cycles (topological sort)
    def _detect_cycle(steps):
        adj = {s['step_number']: [
            (d if isinstance(d, int) else int(d))
            for d in s.get('depends_on', [])
        ] for s in steps}
        visited, in_stack = set(), set()
        def dfs(node):
            visited.add(node); in_stack.add(node)
            for dep in adj.get(node, []):
                if dep in in_stack:
                    return True
                if dep not in visited and dfs(dep):
                    return True
            in_stack.discard(node)
            return False
        return any(dfs(n) for n in adj if n not in visited)

    if _detect_cycle(plan_data['steps']):
        raise PlanValidationError('Plan contains a dependency cycle')

    # ── Persist plan ────────────────────────────────────────
    db.execute('''
        INSERT INTO plans
        (plan_id, session_id, original_query, status, total_steps,
         plan_json, source, source_id, channel, created_at, updated_at)
        VALUES (?, ?, ?, 'executing', ?, ?, ?, ?, ?, ?, ?)
    ''', [
        plan_id, task.session_id, query, len(plan_data['steps']),
        json.dumps(plan_data), task.source, task.source_id,
        task.channel, utcnow(), utcnow(),
    ])

    # Persist individual steps — normalize depends_on from step_numbers to step_ids
    for step in plan_data['steps']:
        step_id = f'{plan_id}_step_{step["step_number"]:03d}'

        # Canonical format: depends_on stores step_ids, not step_numbers.
        # The LLM outputs step_numbers (e.g., [1, 2]). We convert here.
        raw_deps = step.get('depends_on', [])
        normalized_deps = [
            f'{plan_id}_step_{(d if isinstance(d, int) else int(d)):03d}'
            for d in raw_deps
        ]

        db.execute('''
            INSERT INTO plan_steps
            (step_id, plan_id, step_number, description, intent,
             agent_id, depends_on, status, input_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ''', [
            step_id, plan_id, step['step_number'], step['description'],
            step.get('intent'), step.get('agent_id'),
            json.dumps(normalized_deps),
            json.dumps(step.get('input_template', {})),
        ])

    # Emit plan creation as a progress event (visible to user via Gateway)
    await self.emit_event(
        task_id=task.task_id,
        event_type='task.progress',
        payload={
            'type': 'plan_created',
            'plan_id': plan_id,
            'total_steps': len(plan_data['steps']),
            'steps': [
                {'step': s['step_number'], 'description': s['description']}
                for s in plan_data['steps']
            ],
        },
    )

    return plan_id
```

### 31.4 Plan Execution

The orchestrator executes plan steps in dependency order. Steps with no unmet dependencies can run in parallel.

```python
async def execute_plan(self, plan_id: str, parent_task: TaskEnvelope,
                        context: dict):
    """Execute a plan step by step, respecting dependencies.
    Steps with satisfied dependencies can run in parallel."""

    while True:
        # Find all steps that are ready to execute
        ready_steps = db.execute('''
            SELECT * FROM plan_steps
            WHERE plan_id = ? AND status = 'pending'
            ORDER BY step_number
        ''', [plan_id]).fetchall()

        if not ready_steps:
            # Check if plan is done or stuck
            remaining = db.execute('''
                SELECT COUNT(*) as cnt FROM plan_steps
                WHERE plan_id = ? AND status NOT IN ('completed', 'skipped')
            ''', [plan_id]).fetchone()['cnt']

            if remaining == 0:
                await self._complete_plan(plan_id, parent_task, context)
                return
            else:
                # Steps exist but none are ready — all waiting on deps or in_progress
                # Wait for in-progress steps to complete (event-driven, not polling)
                break

        for step in ready_steps:
            # Check dependencies — depends_on stores step_ids (normalized in create_plan)
            deps = json.loads(step['depends_on'] or '[]')
            deps_met = all(
                self._is_dep_satisfied(dep_id) for dep_id in deps
            )
            if not deps_met:
                continue

            # Dispatch this step
            await self._dispatch_step(plan_id, step, parent_task, context)

        # Wait for any in-progress steps to complete.
        # The orchestrator's event consumer routes task.completed / task.failed
        # events to on_step_completed / on_step_failed (see wiring below),
        # which re-enter execute_plan for the next batch.
        break


def _is_dep_satisfied(self, step_id: str) -> bool:
    """Check if a dependency step has finished in a way that allows
    dependents to proceed. A step satisfies its dependents if:
    - status == 'completed' (normal case)
    - status == 'skipped' (continue-after-failure case — the LLM
      decided the plan can proceed without this step's output)
    """
    row = db.execute(
        'SELECT status FROM plan_steps WHERE step_id = ?', [step_id]
    ).fetchone()
    return row is not None and row['status'] in ('completed', 'skipped')


async def _dispatch_step(self, plan_id: str, step: dict,
                          parent_task: TaskEnvelope, context: dict):
    """Dispatch a single plan step to the appropriate agent."""

    step_id = step['step_id']
    intent = step['intent']
    agent_id = step['agent_id']

    # Build input — resolve references to prior step outputs
    input_data = self._resolve_step_input(
        plan_id, step, context
    )

    # Credential resolution (same as §22.3)
    card = self.registry.get_card(agent_id)
    auth_req = card.get('auth_requirements', {}).get(intent)
    if auth_req:
        credential = await self._resolve_credential(auth_req, context, input_data)
        if credential:
            input_data['auth'] = credential
        else:
            # No valid credential — escalate to user instead of dispatching
            # without auth (which would cause the agent to fail immediately).
            # The step stays 'pending' until the credential is provided.
            await self.request_user_credential(
                plan_id=plan_id,
                step_id=step['step_id'],
                agent_id=agent_id,
                intent=intent,
                auth_requirement=auth_req,
                parent_task=parent_task,
            )
            return

    # Create and dispatch TaskEnvelope
    task_id = generate_task_id()
    task = TaskEnvelope(
        task_id=task_id,
        task_list_id=parent_task.task_list_id,
        parent_task_id=parent_task.task_id,
        session_id=parent_task.session_id,
        sender='cosmic/orchestrator:1.0.0',
        recipient=agent_id,
        intent=intent,
        input=input_data,
        idempotency_key=str(uuid4()),
        priority=parent_task.priority,
        source=parent_task.source,
        source_id=parent_task.source_id,
        channel=parent_task.channel,
        signature='',
    )
    task.signature = sign_task(task, self.secrets[agent_id])

    # Track in task ledger
    db.execute('''
        INSERT INTO tasks
        (task_id, plan_id, parent_task_id, session_id, recipient,
         intent, priority, status, envelope_json, source, source_id,
         channel, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatched', ?, ?, ?, ?, ?, ?)
    ''', [
        task_id, plan_id, parent_task.task_id, parent_task.session_id,
        agent_id, intent, parent_task.priority,
        task.model_dump_json(), parent_task.source, parent_task.source_id,
        parent_task.channel, utcnow(), utcnow(),
    ])

    # Link step to task
    db.execute('''
        UPDATE plan_steps SET status = 'in_progress', task_id = ?,
        started_at = ? WHERE step_id = ?
    ''', [task_id, utcnow(), step_id])

    await dispatch(task, self.redis)

    # Emit step progress
    await self.emit_event(
        task_id=parent_task.task_id,
        event_type='task.progress',
        payload={
            'type': 'step_dispatched',
            'plan_id': plan_id,
            'step_number': step['step_number'],
            'description': step['description'],
            'agent_id': agent_id,
            'task_id': task_id,
        },
    )


def _resolve_step_input(self, plan_id: str, step: dict,
                          context: dict) -> dict:
    """Build input for a step by resolving references to prior step outputs.

    Input templates can reference prior outputs using $step_N syntax:
    { "query": "Summarize these findings", "data": "$step_1.output.citations" }
    """
    template = json.loads(step['input_json'] or '{}')
    resolved = {}

    for key, value in template.items():
        if isinstance(value, str) and value.startswith('$step_'):
            # Resolve reference: "$step_1.output.citations"
            parts = value[1:].split('.')  # ['step_1', 'output', 'citations']
            ref_step_num = int(parts[0].split('_')[1])
            ref_step = db.execute('''
                SELECT output_json FROM plan_steps
                WHERE plan_id = ? AND step_number = ?
            ''', [plan_id, ref_step_num]).fetchone()

            if ref_step and ref_step['output_json']:
                output = json.loads(ref_step['output_json'])
                # Navigate the path: output.citations
                for part in parts[1:]:
                    output = output.get(part, {}) if isinstance(output, dict) else output
                resolved[key] = output
            else:
                resolved[key] = value  # leave unresolved — agent handles missing data
        else:
            resolved[key] = value

    # Always include the original query for context
    if 'query' not in resolved:
        plan = db.execute(
            'SELECT original_query FROM plans WHERE plan_id = ?', [plan_id]
        ).fetchone()
        resolved['query'] = plan['original_query']

    return resolved
```

### 31.5 Step Completion & Plan Advancement

When an agent completes a step, the orchestrator's event consumer updates the plan and advances to the next ready steps.

```python
async def on_step_completed(self, event: EventEnvelope):
    """Called when an agent emits task.completed for a plan step."""

    task_id = event.task_id
    step = db.execute('''
        SELECT * FROM plan_steps WHERE task_id = ?
    ''', [task_id]).fetchone()

    if not step:
        return  # not a plan step — handle normally

    plan_id = step['plan_id']
    result = event.payload.get('result', {})

    # Update step
    db.execute('''
        UPDATE plan_steps SET status = 'completed', output_json = ?,
        completed_at = ? WHERE step_id = ?
    ''', [json.dumps(result), utcnow(), step['step_id']])

    # Update plan progress
    db.execute('''
        UPDATE plans SET completed_steps = completed_steps + 1,
        updated_at = ? WHERE plan_id = ?
    ''', [utcnow(), plan_id])

    # Emit step completion progress
    plan = db.execute('SELECT * FROM plans WHERE plan_id = ?', [plan_id]).fetchone()
    parent_task_id = db.execute(
        'SELECT parent_task_id FROM tasks WHERE task_id = ?', [task_id]
    ).fetchone()['parent_task_id']

    await self.emit_event(
        task_id=parent_task_id,
        event_type='task.progress',
        payload={
            'type': 'step_completed',
            'plan_id': plan_id,
            'step_number': step['step_number'],
            'description': step['description'],
            'completed': plan['completed_steps'] + 1,
            'total': plan['total_steps'],
            'percent': round((plan['completed_steps'] + 1) / plan['total_steps'] * 100),
        },
    )

    # Re-enter plan execution to dispatch newly unblocked steps
    parent_task = TaskEnvelope.model_construct(
        **json.loads(
            db.execute(
                'SELECT envelope_json FROM tasks WHERE task_id = ?',
                [parent_task_id]
            ).fetchone()['envelope_json']
        )
    )
    context = await self._load_session_context(plan['session_id'])
    await self.execute_plan(plan_id, parent_task, context)


async def on_step_failed(self, event: EventEnvelope):
    """Called when an agent emits task.failed for a plan step.
    Orchestrator decides whether to retry, skip, or fail the plan."""

    task_id = event.task_id
    step = db.execute('SELECT * FROM plan_steps WHERE task_id = ?', [task_id]).fetchone()
    if not step:
        return

    error = event.payload.get('error', {})

    # Check retry policy (step-level retry tracking prevents zombie task rows)
    if error.get('retryable') and step['attempt'] < step['max_attempts']:
        # Mark the failed task as superseded — terminal state, not a zombie.
        # _dispatch_step will create a fresh task row for the retry.
        db.execute('''
            UPDATE tasks SET status = 'superseded', updated_at = ?
            WHERE task_id = ?
        ''', [utcnow(), task_id])
        # Advance step retry counter and reset for re-dispatch
        db.execute('''
            UPDATE plan_steps SET status = 'pending', task_id = NULL,
            attempt = attempt + 1 WHERE step_id = ?
        ''', [step['step_id']])
        # Re-enter execute_plan — it will re-dispatch this step
        parent_task_id = db.execute(
            'SELECT parent_task_id FROM tasks WHERE task_id = ?', [task_id]
        ).fetchone()['parent_task_id']
        parent_task = TaskEnvelope.model_construct(
            **json.loads(
                db.execute(
                    'SELECT envelope_json FROM tasks WHERE task_id = ?',
                    [parent_task_id]
                ).fetchone()['envelope_json']
            )
        )
        plan = db.execute(
            'SELECT * FROM plans WHERE plan_id = ?', [step['plan_id']]
        ).fetchone()
        context = await self._load_session_context(plan['session_id'])
        await self.execute_plan(step['plan_id'], parent_task, context)
        return

    # Non-retryable or max attempts exceeded
    db.execute('''
        UPDATE plan_steps SET status = 'failed', completed_at = ?
        WHERE step_id = ?
    ''', [utcnow(), step['step_id']])

    # Ask LLM: can we continue without this step, or is the plan failed?
    plan = db.execute('SELECT * FROM plans WHERE plan_id = ?', [step['plan_id']]).fetchone()
    remaining_steps = db.execute('''
        SELECT * FROM plan_steps WHERE plan_id = ? AND status = 'pending'
    ''', [step['plan_id']]).fetchall()

    decision = await self.llm.decide(
        prompt=f'''A step in the plan failed.
Plan: {plan["original_query"]}
Failed step: {step["description"]}
Error: {json.dumps(error)}
Remaining steps: {[s["description"] for s in remaining_steps]}

Can the plan continue without this step's output, or should the entire plan fail?
Respond: {{ "action": "continue"|"fail", "reason": "..." }}'''
    )

    decision_data = json.loads(decision)

    if decision_data['action'] == 'fail':
        await self._fail_plan(step['plan_id'], error)
        return

    # ── action == 'continue': proceed without this step's output ──
    # Mark dependent steps that CANNOT proceed without the failed step's
    # output as 'skipped'. Steps that don't depend on this step are unaffected.
    await self._cascade_skip(step['plan_id'], step['step_id'])

    # Re-enter execute_plan — it will dispatch any newly unblocked steps.
    # (Steps that depended on the failed step and were skipped above
    #  will be ignored. Steps that depended on the failed step but were
    #  already completed are fine. Steps with no dependency on the failed
    #  step continue as normal.)
    parent_task_id = db.execute(
        'SELECT parent_task_id FROM tasks WHERE task_id = ?', [task_id]
    ).fetchone()['parent_task_id']
    parent_task = TaskEnvelope.model_construct(
        **json.loads(
            db.execute(
                'SELECT envelope_json FROM tasks WHERE task_id = ?',
                [parent_task_id]
            ).fetchone()['envelope_json']
        )
    )
    context = await self._load_session_context(plan['session_id'])
    await self.execute_plan(step['plan_id'], parent_task, context)


async def _cascade_skip(self, plan_id: str, failed_step_id: str):
    """When a step fails and the LLM says 'continue', mark all
    downstream steps that exclusively depend on the failed step
    (directly or transitively) as 'skipped'.

    A pending step is skipped if ALL paths to it go through the
    failed step. If a step has other satisfied dependencies, it
    can still proceed — _is_dep_satisfied treats 'skipped' as
    satisfied so the dependency check in execute_plan handles this.

    However, a step whose input_json references $step_N (where N is
    the failed step) cannot produce a useful result. We skip those."""

    failed_step = db.execute(
        'SELECT step_number FROM plan_steps WHERE step_id = ?',
        [failed_step_id]
    ).fetchone()
    failed_num = failed_step['step_number']

    pending_steps = db.execute('''
        SELECT step_id, input_json FROM plan_steps
        WHERE plan_id = ? AND status = 'pending'
    ''', [plan_id]).fetchall()

    for ps in pending_steps:
        input_json = ps['input_json'] or '{}'
        # Skip steps that reference the failed step's output in their input
        if f'$step_{failed_num}' in input_json:
            db.execute('''
                UPDATE plan_steps SET status = 'skipped', completed_at = ?
                WHERE step_id = ?
            ''', [utcnow(), ps['step_id']])
```

**Event consumer wiring — connecting terminal events to plan handlers:**

The orchestrator's existing event consumer (the single async loop that reads from `streams:events` via the `orchestrator` consumer group) routes terminal events to the plan handlers. This wiring is added alongside the existing event handlers (deferred checks in §12.7, epoch redrives in §16, event archival in §12.8):

```python
# agents/orchestrator/event_router.py
# (Added to the orchestrator's main event consumer loop)

async def route_event(self, event: EventEnvelope):
    """Extended orchestrator event handler — routes plan-related events
    to the Task Planner. Called for every event the orchestrator consumes."""

    # ── Existing handlers (unchanged) ─────────────────────────
    if event.event_type == 'task.deferred':
        await self.on_deferred(event)           # §12.7
    elif event.event_type == 'task.rejected':
        await self.on_rejected(event)           # §16

    # ── Plan-aware terminal event routing ─────────────────────
    elif event.event_type in ('task.completed', 'task.failed'):
        # Check if this task_id belongs to a plan step
        step = db.execute(
            'SELECT * FROM plan_steps WHERE task_id = ?',
            [event.task_id]
        ).fetchone()

        if step:
            # This is a plan step — route to plan handlers
            if event.event_type == 'task.completed':
                await self.planner.on_step_completed(event)
            else:
                await self.planner.on_step_failed(event)
        else:
            # Not a plan step — handle as a normal terminal event
            # (update task ledger, store result, emit to session, etc.)
            await self.on_terminal_event(event)

        # Archive events regardless of plan membership (§12.8)
        events = await get_task_events(event.task_id, self.redis)
        await archive_task_events(event.task_id, events)
```

### 31.6 Plan Completion & Synthesis

When all steps complete (or all remaining steps are skipped/failed with continue), the orchestrator synthesizes the results into a final response.

```python
async def _complete_plan(self, plan_id: str, parent_task: TaskEnvelope,
                          context: dict):
    """All plan steps are done. Synthesize final result."""

    plan = db.execute('SELECT * FROM plans WHERE plan_id = ?', [plan_id]).fetchone()
    steps = db.execute('''
        SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_number
    ''', [plan_id]).fetchall()

    # Collect all step outputs — include status so the LLM knows about failures
    step_results = []
    failed_or_skipped = []
    for step in steps:
        entry = {
            'step': step['step_number'],
            'description': step['description'],
            'status': step['status'],
            'output': json.loads(step['output_json'] or '{}'),
        }
        step_results.append(entry)
        if step['status'] in ('failed', 'skipped'):
            failed_or_skipped.append(entry)

    # Build failure context for the synthesis prompt
    failure_note = ''
    if failed_or_skipped:
        failure_note = f'''
NOTE: {len(failed_or_skipped)} of {len(steps)} steps did not complete successfully:
{json.dumps([{{"step": s["step"], "description": s["description"], "status": s["status"]}} for s in failed_or_skipped], indent=2)}
Be transparent about what could not be completed. Do not fabricate results for failed/skipped steps.'''

    # LLM synthesizes a final user-facing response
    synthesis = await self.llm.synthesize(
        system='You are synthesizing the results of a multi-step task into a clear response for the user.',
        prompt=f'''Original request: {plan["original_query"]}

Step results:
{json.dumps(step_results, indent=2)}
{failure_note}
Synthesize a clear, concise response for the user that addresses their original request.
Include relevant details from each step but do not expose internal agent mechanics.
If some steps failed or were skipped, explain what was accomplished and what remains incomplete.''',
    )

    # Collect all artifacts from all steps
    all_artifacts = []
    for step in steps:
        if step['task_id']:
            task_result = db.execute(
                'SELECT result_json FROM tasks WHERE task_id = ?', [step['task_id']]
            ).fetchone()
            if task_result and task_result['result_json']:
                result = json.loads(task_result['result_json'])
                all_artifacts.extend(result.get('artifacts', []))

    # Update plan status
    db.execute('''
        UPDATE plans SET status = 'completed', completed_at = ?, updated_at = ?
        WHERE plan_id = ?
    ''', [utcnow(), utcnow(), plan_id])

    # Emit terminal event for the parent task
    result = AgentResult(
        status='completed',
        output={'response': synthesis, 'plan_id': plan_id, 'steps_completed': len(steps)},
        artifacts=[ArtifactManifest(**a) for a in all_artifacts if isinstance(a, dict)],
        error=None,
    )
    await self.emit_terminal_event(parent_task.task_id, result)
```

### 31.7 Concurrent Plan Management

The orchestrator handles multiple plans simultaneously. Each plan is independent — steps from different plans interleave naturally through the async event loop.

```
User sends: "Research quantum computing advances"     ← Plan A (3 steps)
User sends: "Draft an email to my team about Friday"  ← Plan B (2 steps)
Cron fires:  "Check inbox for anything urgent"        ← Plan C (1 step, or planless)

Timeline:
  t0: Plan A created → step 1 dispatched (research.topic)
  t1: Plan B created → step 1 dispatched (docs.create)      ← concurrent
  t2: Plan C arrives → simple task, no plan, direct dispatch
  t3: Plan A step 1 completes → step 2 dispatched
  t4: Plan B step 1 completes → step 2 dispatched
  t5: Plan A step 2 completes → step 3 dispatched
  t6: Plan B step 2 completes → Plan B done, synthesize
  t7: Plan C completes → response delivered
  t8: Plan A step 3 completes → Plan A done, synthesize
```

**Why this works without explicit concurrency management:**

1. Each plan has its own `plan_id` — steps reference their plan, not each other's.
2. The orchestrator's event consumer is a single async loop. When a `task.completed` event arrives, it looks up which plan the step belongs to and advances that specific plan.
3. Redis Streams ensure ordered delivery. The orchestrator processes events one at a time — no race conditions on plan state.
4. SQLite is the ledger. All plan state is durable. If the orchestrator crashes, `recover_orphaned_tasks()` (§16.3) picks up in-progress tasks, and the plan execution resumes from where it left off.

**Priority between concurrent plans:** Plans inherit the `priority` from their source TaskEnvelope. A user-initiated plan (high) dispatches steps at high priority. A cron-initiated plan (low) dispatches at low. The existing priority tier mechanism (§8, §18) ensures user work is served first.

### 31.8 Plan-Aware Observability

The Gateway forwards plan progress events to the UI. The desktop app can render:

```
┌─────────────────────────────────────────────┐
│  Task: "Research quantum computing advances" │
│  Plan: 3 steps                               │
│                                              │
│  ✓ Step 1: Search for recent papers          │
│  ◉ Step 2: Analyze key findings        ← active │
│  ○ Step 3: Synthesize summary report         │
│                                              │
│  Progress: 33% (1/3 steps)                   │
└─────────────────────────────────────────────┘
```

Plan progress events use the existing `task.progress` event type with structured payloads:

| Payload `type` | When | Contains |
|---|---|---|
| `plan_created` | Plan decomposition complete | step count, step descriptions |
| `step_dispatched` | Step sent to agent | step number, agent, task_id |
| `step_completed` | Step finished successfully | step number, percent complete |
| `step_failed` | Step failed | step number, error, retry/skip/fail decision |
| `plan_completed` | All steps done, synthesis ready | total steps, final response |

### 31.9 Simple vs Planned Tasks

Not every request needs a plan. The orchestrator classifies complexity first.

| Complexity | Criteria | Behavior |
|---|---|---|
| **Simple** | Single intent, one agent, no dependencies | Direct dispatch — no plan created. Task tracked in `tasks` table only. |
| **Complex** | Multiple intents, multiple agents, sequential dependencies, or ambiguous decomposition | Full plan created. Steps tracked in `plan_steps`. Synthesis on completion. |

**Examples:**

```
"Send this email"                    → simple: docs.edit, one agent
"What time is it in Tokyo?"          → simple: not even opus — routes to haiku
"Research X and then write a report" → complex: research.topic → docs.create
"Check my email and calendar,        → complex: research (email) + research (calendar)
 then draft a summary"                          → docs.create (depends on both)
```

### 31.10 Integration with Existing Systems

The Task Planner integrates with these existing mechanisms — it does not replace them:

| System | Integration |
|---|---|
| **Dispatch (§8.3, §10)** | Plan steps are dispatched as normal TaskEnvelopes. The dispatch function is unchanged. |
| **Idempotency (§14)** | Each step gets its own `idempotency_key`. Replay safety is per-step. |
| **Retry/DLQ (§19)** | Failed steps follow the same retry policy from `agent_card.yaml`. After max attempts, the planner decides: continue without this step or fail the plan. |
| **Deferred Recovery (§12.7)** | Deferred steps are recovered by the existing deferred check loop. On recovery, the plan executor is re-entered. |
| **Crash Recovery (§16.3)** | `recover_orphaned_tasks()` finds stale in-progress tasks (including plan steps). The plan state in SQLite survives the crash. |
| **Event Archival (§12.8)** | Plan progress events are archived alongside all other events. |
| **Bidirectional Communication (§13)** | Agents can still send reverse tasks (clarify, delegate, escalate) during plan step execution. Suspension and resumption work as before. |
| **Credential Resolution (§22)** | Each step resolves credentials independently at dispatch time. |
| **Source Tags (§24)** | Plan steps inherit `source`, `source_id`, and `channel` from the parent task. |

---

## 32. Universal Agent Tools

Every agent in COSMIC receives a set of universal tools injected by the agent runtime at startup. These tools handle cognitive/coordination tasks that all agents need. Agents cannot opt out of universal tools — they are part of the runtime contract.

**Design principle:** Separate cognitive tools (planning, memory) from capability tools (shell, browser, file I/O). Every agent must plan and report. Only specific agents should access the filesystem or run commands.

### 32.1 Tool Tiers

| Tier | Tools | Injection | Opt-out |
|---|---|---|---|
| **Universal** | StepPlan, MemoryRead, MemoryWrite | Injected by agent runtime at startup | **Not allowed** — all agents get these |
| **Declared** | All other tools (web_search, playwright_*, shell_execute, file_*, etc.) | Declared per agent in `agent_card.yaml` `policies.tool_access` | Per agent — only tools declared in the card are available |

### 32.2 StepPlan: Agent-Level Execution Planning

StepPlan is a lightweight, single-task planning tool that prevents LLM drift during complex agent execution. It is fundamentally different from the orchestrator's Task Planner (§31) — the orchestrator manages a DAG of tasks across agents, while StepPlan manages a flat checklist within one agent's single task.

| Aspect | Orchestrator Task Planner (§31) | Agent StepPlan (§32) |
|---|---|---|
| **Scope** | Multiple tasks across agents | One task, internal steps |
| **Dependencies** | DAG — step B waits for step A's output | Flat ordered list — agent handles sequencing |
| **Lifetime** | Lives until user's request is fully done | Ephemeral — created per-task, destroyed on completion |
| **Persistence** | SQLite ledger (survives crashes) | In-memory during task, emitted via events |
| **Visibility** | Orchestrator's own state + UI progress | Auto-emits progress events to orchestrator |
| **Complexity** | Full planning loop, synthesis, failure handling | Single tool, three operations |

**Optional implementation note (LangChain / LangGraph):**

LangChain or LangGraph may be used inside an individual agent as a local implementation detail for
tool loops, checkpoints, or state machines. They do **not** replace COSMIC's contracts:
`TaskEnvelope`, `EventEnvelope`, `AgentResult`, `StepPlan`, reverse tasks, suspend/resume, usage
logging, auth isolation, artifact rules, and orchestrator-mediated routing remain authoritative.
Cross-agent planning still belongs to the orchestrator. Any framework memory/checkpointer is
agent-local convenience state, not COSMIC's system of record.

**Tool specification:**

```python
# shared/step_plan.py

class StepPlan:
    """Universal tool injected into every agent's runtime.
    Three operations: create, update, list."""

    def __init__(self, agent_id: str, task_id: str, emit_fn: Callable):
        self.agent_id = agent_id
        self.task_id = task_id
        self.emit_fn = emit_fn  # bound to agent's emit_event method
        self.steps: list[dict] = []
        self.active = False

    async def create(self, steps: list[str]) -> dict:
        """Create a new plan for the current task.
        Called BEFORE starting work on a complex task.

        Args:
            steps: ordered list of step descriptions

        Returns:
            { plan_active: True, total_steps: N, steps: [...] }
        """
        self.steps = [
            {
                'step': i + 1,
                'text': text,
                'status': 'pending',
                'note': None,
            }
            for i, text in enumerate(steps)
        ]
        self.active = True

        # Auto-emit plan creation event
        await self.emit_fn(
            task_id=self.task_id,
            event_type='task.progress',
            payload={
                'type': 'agent_plan_created',
                'total_steps': len(self.steps),
                'steps': [{'step': s['step'], 'text': s['text']} for s in self.steps],
            },
        )
        return {
            'plan_active': True,
            'total_steps': len(self.steps),
            'steps': self.steps,
        }

    async def update(self, step: int,
                      status: str,  # 'in_progress' | 'completed' | 'skipped'
                      note: str | None = None) -> dict:
        """Mark a step's status. Auto-emits a progress event.

        Args:
            step: step number (1-based)
            status: new status
            note: optional completion note (e.g., "Found 3 results")

        Returns:
            { step: N, status: str, completed: M, total: T, percent: P }
        """
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

        # Auto-emit progress event
        await self.emit_fn(
            task_id=self.task_id,
            event_type='task.progress',
            payload={
                'type': 'agent_step_update',
                'step': step,
                'text': self.steps[step - 1]['text'],
                'status': status,
                'note': note,
                'completed': completed,
                'total': total,
                'percent': percent,
            },
        )
        return {
            'step': step,
            'status': status,
            'completed': completed,
            'total': total,
            'percent': percent,
        }

    async def list(self) -> dict:
        """Return current plan state. Used by the agent to re-ground
        itself on where it is in the execution.

        Returns:
            { plan_active: bool, steps: [...], completed: M, total: T }
        """
        if not self.active:
            return {'plan_active': False, 'steps': [], 'completed': 0, 'total': 0}

        completed = sum(1 for s in self.steps if s['status'] in ('completed', 'skipped'))
        return {
            'plan_active': True,
            'steps': self.steps,
            'completed': completed,
            'total': len(self.steps),
        }

    def has_pending_steps(self) -> bool:
        """Check if any steps are still pending or in_progress.
        Used by the agent runtime to enforce completion."""
        if not self.active:
            return False
        return any(s['status'] in ('pending', 'in_progress') for s in self.steps)
```

### 32.3 Tool Injection at Runtime

Universal tools are injected by the agent base class during task handling. The agent's `execute()` method receives them as runtime context — not declared in `agent_card.yaml`.

```python
# Updated agent base class (extends §12.6)

class AgentRuntime:
    """Base class for all agents. Injects universal tools."""

    async def handle(self, task: TaskEnvelope, msg_id: str, stream: str):
        # ... existing signature verification, epoch check, auth extraction ...

        # ── Inject universal tools ─────────────────────────────────
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

        # Execute with idempotency enforcement
        result = await execute_with_idempotency(
            task, self.execute, redis,
            agent_max_duration_sec=self.max_task_duration_sec,
        )

        # ── Enforce plan completion ────────────────────────────────
        if isinstance(result, AgentResult) and result.status == 'completed':
            if self.step_plan.has_pending_steps():
                # Agent said "done" but steps remain — reject
                plan_state = await self.step_plan.list()
                result = AgentResult(
                    status='failed',
                    output={'error': 'Plan has incomplete steps', 'plan': plan_state},
                    artifacts=[],
                    error=AgentError(
                        code='PLAN_INCOMPLETE',
                        retryable=False,
                        message=f'Agent returned done but {plan_state["total"] - plan_state["completed"]}'
                                f' of {plan_state["total"]} plan steps are still pending.',
                        next_action='escalate',
                    ),
                )

        # ── Cleanup ────────────────────────────────────────────────
        self.step_plan = None
        self.memory_read = None
        self.memory_write = None
        self.auth = None

        # ... existing result handling (session data, learnings, ack, emit) ...
```

### 32.4 Behavioral Prompt: Planning Rules

This prompt is injected into every agent's system prompt by the agent runtime. It governs when and how agents use StepPlan.

```markdown
## Execution Planning

You have access to a StepPlan tool for tracking your execution on complex tasks.

**When to use StepPlan:**
- Your assigned task involves 3 or more logical steps
- The task requires sequential operations where losing track would produce incomplete work
- You are unsure about the full scope — create a plan to clarify your own thinking

**When to skip StepPlan:**
- The task is 1-2 obvious steps (e.g., "search for X and return results")
- The task is a simple lookup or single API call

**Rules:**
1. Call `StepPlan.create(steps=[...])` BEFORE doing any work
2. Call `StepPlan.update(step=N, status='in_progress')` before starting each step
3. Call `StepPlan.update(step=N, status='completed', note='...')` after finishing each step
4. You CANNOT return a successful result while steps remain pending — the runtime will reject it
5. If you realize mid-task that the plan needs to change, call `StepPlan.create()` again with the new steps
6. Use `StepPlan.list()` if you lose track of where you are

**Completion notes:** When completing a step, include a brief note about what was accomplished.
This helps the orchestrator understand your progress without reading full outputs.
```

### 32.5 MemoryRead & MemoryWrite

Agents can read from and write to the shared memory store. MemoryRead retrieves relevant memories by semantic search. MemoryWrite persists new learnings or facts.

**Note:** These tools complement the existing agent learnings mechanism (§12.1). `store/learnings.md` is the agent's private persistent memory. MemoryRead/MemoryWrite access the shared `memory/` store that all LLM backends can retrieve from.

**Architecture:** The shared long-term memory subsystem is owned by the internal `cosmic-memory` service on the same VM, not embedded directly inside the Gateway process. Gateway remains the single integration surface for the rest of COSMIC: it assembles memory into prompts, proxies `/internal/memory/*` calls to `cosmic-memory` when memory is enabled, and degrades safely when memory is absent. Agents run in separate processes and cannot import Gateway modules directly, so MemoryRead/MemoryWrite still communicate with the Gateway via internal HTTP API endpoints — the same pattern used by credential resolution (§22.3). The agent runtime injects `GATEWAY_INTERNAL_URL` (not a Python object) into the tools at startup.

**Current agent-builder rules:**

- `MemoryRead` is for **shared retrievable memory** (`core_fact`, `session_summary`, `task_summary`, `agent_note`, `user_data`, `transcript`, `artifact_pointer`).
- `MemoryWrite` is for **high-signal durable memory only**. Do not use it as a task scratchpad.
- Exact prior session context should come from deterministic revisit (`/internal/session/state`, `/internal/session/turns`, `/internal/session/history`, `/internal/session/task-notebook`, `/internal/session/revisit`) rather than broad semantic recall.
- Exact prior agent-specific context should come from recall intents against that agent's own `store/data/`.
- Large outputs belong in `runs/artifacts/<task_id>/` plus a compact `artifact_pointer`, not as huge shared memory blobs.
- Agents should read their own `store/learnings.md` at task start, update it only when something durable was learned, and let Gateway/session sync project it into shared `memory/agent_notes/`.
- Agents must never persist raw chain-of-thought or raw tool chatter into shared memory.

```python
# shared/memory_tools.py

import httpx

class MemoryRead:
    """Universal tool: read from the shared memory store.
    Agents use this to access memories beyond their own learnings.

    Communicates with Gateway via /internal/memory/search endpoint."""

    def __init__(self, gateway_url: str, agent_id: str, service_token: str):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.service_token = service_token  # same internal service token as §22.3

    async def search(self, query: str, max_results: int = 5,
                      memory_types: list[str] | None = None) -> list[dict]:
        """Search memories by semantic similarity.

        Args:
            query: natural language search query
            max_results: max memories to return (default 5)
            memory_types: filter by type — ['agent_note', 'session_summary',
                          'task_summary', 'user_data']. None = all types.

        Returns:
            list of { memory_id, type, content, date, relevance_score }
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f'{self.gateway_url}/internal/memory/search',
                json={
                    'query': query,
                    'max_results': max_results,
                    'memory_types': memory_types,
                    'agent_id': self.agent_id,
                },
                headers={'X-Internal-Token': self.service_token},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()['results']


class MemoryWrite:
    """Universal tool: write to the shared memory store.
    Agents use this to persist facts or learnings that should
    be accessible to the entire system.

    Communicates with Gateway via /internal/memory/write endpoint."""

    def __init__(self, gateway_url: str, agent_id: str, service_token: str):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.service_token = service_token

    async def write(self, content: str, tags: list[str] | None = None,
                     memory_type: str = 'agent_note') -> dict:
        """Write a memory to the shared store.

        Args:
            content: the memory content (markdown text)
            tags: searchability tags
            memory_type: 'agent_note' (default) or 'task_summary'

        Returns:
            { memory_id, indexed: True }
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f'{self.gateway_url}/internal/memory/write',
                json={
                    'content': content,
                    'tags': tags or [],
                    'memory_type': memory_type,
                    'agent_id': self.agent_id,
                },
                headers={'X-Internal-Token': self.service_token},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
```

**Gateway-side endpoints** (added to Gateway's internal API alongside credential endpoints):

```python
# gateway/internal_api.py (extends existing internal routes)

@app.post('/internal/memory/search')
async def memory_search(request: MemorySearchRequest):
    """Internal endpoint for agents to search shared memory.
    Delegates to Session Manager's memory_retriever."""
    results = await session_manager.memory_retriever.search(
        query=request.query,
        limit=request.max_results,
        type_filter=request.memory_types,
    )
    return {
        'results': [
            {
                'memory_id': r.memory_id,
                'type': r.type,
                'content': r.content[:2000],  # truncate large memories
                'date': r.date,
                'relevance_score': r.score,
            }
            for r in results
        ]
    }


@app.post('/internal/memory/write')
async def memory_write(request: MemoryWriteRequest):
    """Internal endpoint for agents to write to shared memory.
    Rate-limited and deduplicated to prevent runaway agents from
    flooding the memory store.

    Delegates to Session Manager's memory_writer after checks."""

    # ── Rate limiting: per-agent, per-hour ─────────────────────────
    rate_key = f'memory_write_rate:{request.agent_id}'
    current = int(await redis.get(rate_key) or 0)
    if current >= MEMORY_WRITE_MAX_PER_HOUR:
        raise HTTPException(
            429,
            f'Memory write rate limit exceeded for {request.agent_id}. '
            f'Max {MEMORY_WRITE_MAX_PER_HOUR} writes per hour.'
        )
    pipe = redis.pipeline()
    pipe.incr(rate_key)
    pipe.expire(rate_key, 3600)        # reset counter after 1 hour
    await pipe.execute()

    # ── Content deduplication: reject near-identical writes ────────
    # Generate memory_id BEFORE the dedup check so the SETNX stores a valid ID
    # from the start. The old pattern set an empty-string placeholder first,
    # then overwrote with the real ID after the write — concurrent duplicates
    # arriving between SETNX and the overwrite would get an empty memory_id.
    content_hash = hashlib.sha256(request.content.encode()).hexdigest()[:16]
    dedup_key = f'memory_write_dedup:{request.agent_id}:{content_hash}'
    memory_id = f'mem_{request.agent_id.split("/")[1].split(":")[0]}_{uuid4().hex[:8]}'

    # Atomic set-if-not-exists: first writer wins, stores the real memory_id
    was_set = await redis.set(dedup_key, memory_id, ex=86400, nx=True)
    if not was_set:
        # Another write already claimed this content hash — return the winner's ID
        existing_id = await redis.get(dedup_key)
        return {'memory_id': existing_id, 'indexed': True, 'deduplicated': True}

    # ── Write (we won the race — dedup key already holds our memory_id) ───
    await session_manager.memory_writer.write_memory(
        memory_id=memory_id,
        memory_type=request.memory_type,
        content=request.content,
        metadata={
            'agent_id': request.agent_id,
            'tags': request.tags,
            'date': utcnow().isoformat(),
        },
    )
    return {'memory_id': memory_id, 'indexed': True}
```

### 32.6 Universal Tool Summary

| Tool | Operations | Purpose | Auto-emits |
|---|---|---|---|
| **StepPlan** | `create`, `update`, `list` | Prevent drift on complex tasks. Externalize thinking. | Yes — every `create` and `update` emits `task.progress` events |
| **MemoryRead** | `search` | Access shared memory store (all types). Complements agent's own `learnings.md`. | No |
| **MemoryWrite** | `write` | Persist learnings to shared store. Indexed in Qdrant for retrieval by all backends. | No |

### 32.6a Current Internal Memory / Session Surface

In the current runtime, the Gateway exposes these internal memory/session endpoints to agents and internal control flows:

- `/internal/memory/search`
- `/internal/memory/active-search`
- `/internal/memory/memories/{memory_id}`
- `/internal/memory/schema-context`
- `/internal/memory/plan`
- `/internal/memory/resolve-identity`
- `/internal/memory/current-state`
- `/internal/memory/temporal-facts`
- `/internal/memory/memory-brief`
- `/internal/memory/write`
- `/internal/memory/core-facts`
- `/internal/memory/episodes`
- `/internal/memory/index-status`
- `/internal/memory/index-sync`
- `/internal/memory/index-rebuild`
- `/internal/session/state/{session_id}`
- `/internal/session/turns/{session_id}`
- `/internal/session/history/{session_id}`
- `/internal/session/task-notebook/{task_id}`
- `/internal/session/revisit`

Agent authors should treat these as the canonical same-VM memory/session control surface. Shared memory search/write goes through `/internal/memory/*`; exact historical recovery and live continuity inspection go through `/internal/session/*`. When a prior search or control flow already identified an exact `memory_id`, use `/internal/memory/memories/{memory_id}` to retrieve the full canonical memory block instead of relying on a truncated search hit alone.

**Current implementation note (thin orchestrator):** the current production orchestrator assembles its system prompt from read-only asset files under `orchestrator/prompts/`, generates its exposed tool catalog from the centralized runtime registry in `orchestrator/tools/registry.py`, and exposes both the registry snapshot and prompt-asset SHA-256 hashes on `/health` for drift inspection. The shipped always-visible tool set currently includes `memory_search`, `memory_fetch`, `memory_write`, `session_state`, `session_turns`, `session_history`, `task_notebook`, `session_revisit`, reminder tools, and a compact specialist-agent surface (`agent_catalog_search`, `delegate_to_agent`) alongside server-side `web_search` / `web_fetch`. The prompt may also include a **Current Specialist Shortlist** derived from recent successful specialist usage and recently registered specialists in the registry; when a specialist is on that shortlist, its first-class wrapper tools may also be surfaced for convenience. Specialists seed into the shortlist by default when newly registered and only fall out after more than 15 days of inactivity. Otherwise, specialist discovery remains authoritative through `agent_catalog_search`. Specialist-specific hygiene and usage nuance should not live as always-on global prompt text; instead, lookup results may carry compact per-intent `usage_hints` sourced from agent cards.

### 32.7 How Universal Tools Appear to the LLM

The agent runtime exposes universal tools to the LLM as standard tool definitions in the tool-use API call. The LLM calls them like any other tool — it doesn't know they're "universal" vs "declared."

```python
# Agent runtime builds the tool list for the LLM call
def build_tool_definitions(self) -> list[dict]:
    """Combine universal + declared tools into a single tool list."""
    tools = []

    # Universal tools (always present)
    tools.append({
        'name': 'step_plan',
        'description': 'Create and track execution steps for complex tasks.',
        'parameters': {
            'action': {'type': 'string', 'enum': ['create', 'update', 'list']},
            'steps': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'For create: ordered list of step descriptions'},
            'step': {'type': 'integer', 'description': 'For update: step number (1-based)'},
            'status': {'type': 'string', 'enum': ['in_progress', 'completed', 'skipped'],
                       'description': 'For update: new status'},
            'note': {'type': 'string', 'description': 'For update: optional completion note'},
        },
    })
    tools.append({
        'name': 'memory_read',
        'description': 'Search the shared memory store for relevant information.',
        'parameters': {
            'query': {'type': 'string', 'description': 'Natural language search query'},
            'max_results': {'type': 'integer', 'default': 5},
        },
    })
    tools.append({
        'name': 'memory_write',
        'description': 'Save a fact or learning to the shared memory store.',
        'parameters': {
            'content': {'type': 'string', 'description': 'Memory content (markdown)'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
        },
    })

    # Declared tools (from agent_card.yaml policies.tool_access)
    for tool_name in self.declared_tools:
        tools.append(self.tool_registry.get_definition(tool_name))

    return tools
```

### 32.8 Hard Rules

1. **Universal tools cannot be opted out of.** Every agent gets StepPlan, MemoryRead, MemoryWrite. The agent card does not declare them — the runtime injects them.
2. **StepPlan enforcement is at the runtime level.** If the agent creates a plan and returns `completed` with pending steps, the runtime converts the result to `failed` with code `PLAN_INCOMPLETE`. This is not a prompt suggestion — it is a hard guard.
3. **MemoryWrite is append-only from the agent's perspective.** Agents can add memories but cannot delete or modify existing shared memories. Memory cleanup is a Session Manager responsibility.
4. **Universal tools do not appear in `agent_card.yaml`.** They are part of the runtime contract, not the capability declaration. The card only lists domain-specific tools under `policies.tool_access`.
5. **StepPlan is per-task, not per-session.** Each task gets a fresh StepPlan instance. Plans do not carry over between tasks.

---

## Appendix A: Quick Reference — All Redis Keys

| Key Pattern | Type | Scope | TTL |
|---|---|---|---|
| `streams:{agent_id}:{priority}` | Stream | Per-agent | Persistent (trimmed by MAXLEN) |
| `streams:events` | Stream | Global | Trimmed to `EVENTS_STREAM_MAXLEN` (50k). Archived to `logs/events/` before eviction (§12.8) |
| `task_events:{task_id}` | List | Per-task | `RESULT_TTL_SEC` (7 days). Per-task event index for O(k) replay |
| `streams:dlq` | Stream | Global | Persistent |
| `streams:capability.updates` | Stream | Global | Persistent |
| `streams:broadcast` | Stream | Global | Persistent |
| `registry:{agent_id}:{instance_id}` | Hash | Per-instance | `heartbeat_ttl_sec + 5` (auto-expired) |
| `intent:{intent_name}` | Set | Per-intent | Registration-driven |
| `idempotency:{idempotency_key}` | String | Per-task | `2 × deadline` or `2 × max_task_duration_sec` (floor 60s) |
| `idempotency:result:{idempotency_key}` | String | Per-task | `RESULT_TTL_SEC` (7 days) |
| `event_seq:{task_id}` | Counter | Per-task | `RESULT_TTL_SEC` after terminal |
| `orchestrator:leader` | String | Global | `LEADER_TTL` (15s) |
| `orchestrator:epoch` | Counter | Global | Persistent |
| `user_input:requests` | Stream | Global | Persistent |
| `user_input:replies` | Stream | Global | Persistent |
| `scheduler:last_heartbeat` | String | Global | None (overwritten each heartbeat) |
| `memory_write_rate:{agent_id}` | Counter | Per-agent | 3600s (1 hour). Rate limit for MemoryWrite (§32.5) |
| `memory_write_dedup:{agent_id}:{hash}` | String | Per-agent per-content | 86400s (24 hours). Content dedup for MemoryWrite (§32.5) |

**Consumer groups:**

| Stream | Group | Consumer | Purpose |
|---|---|---|---|
| `streams:events` | `orchestrator` | Orchestrator instances | Routing, retry, DLQ, deferred recovery |
| `streams:events` | `gateway` | Gateway instances | Forward events to desktop app via WebSocket |
| `user_input:requests` | `gateway` | Gateway instances | Surface task input requests to UI (§3.12) |
| `user_input:replies` | `orchestrator` | Orchestrator instances | Receive user replies, resume agents (§13.2) |

**SQLite databases:**

| Database | Location | Owner | Contents |
|---|---|---|---|
| `agents/orchestrator/store/data/task_ledger.db` | Orchestrator | Orchestrator (Task Planner) | Plans, plan steps, tasks, deferred checks (§31.1) |
| `registry/registry.db` | Registry | Agents (write), Orchestrator (read) | Agent capabilities, intents |
| `gateway/sessions.db` | Gateway | Session Manager | Conversation history, messages |
| `gateway/routing_audit.db` | Gateway | Gateway routing layer | Durable inspection of final route decisions, classifier payloads, sticky-routing hits, overrides, and routing latency |
| `gateway/credentials.db` | Gateway | Credential Manager | OAuth accounts, encrypted tokens, audit |
| `gateway/usage.db` | Gateway | Usage Ledger | Append-only token/cost telemetry for direct LLM routes, model router, orchestrator, and agents |
| `gateway/scheduler/scheduler.db` | Gateway | Scheduler | Cron definitions, heartbeat config, execution log |
| `gateway/webhooks/webhooks.db` | Gateway | Webhook Handler | Webhook registrations, webhook log |
| `agents/*/store/data/*.db` | Per-agent | Each agent | Agent-specific session data (§12.2) |
| `agents/*/runtime/state.db` | Per-agent | Each agent | In-flight task state (ephemeral) |
| `resources/user_data.db` | Desktop App | Settings bridge (`resources/settings_bridge.py`) | Desktop app settings (`app_settings` key-value table) including `cosmicAuth`, `gatewayBaseUrl`, `gatewayApiToken`, and all UI preferences. Fernet encryption available via `resources/database.py`. Gitignored (`*.db`). See §3.5a |

**External databases (Supabase — cloud-hosted):**

| Table / Function | Location | Owner | Contents |
|---|---|---|---|
| `public.users` | Supabase | Platform | User accounts, API keys, privilege flags |
| `public.user_vms` | Supabase | Platform | Per-user VM provisioning: gateway URL, API token, VM IP/DNS, region, status. `api_token` is the source of truth for the VM's `GATEWAY_LOCAL_API_TOKEN`. RLS-protected — users can only read their own row (§3.5a.1) |
| `public.authenticate_with_api_key()` | Supabase RPC | Platform | SECURITY DEFINER function — validates Cosmic API key, returns user profile + VM config (§3.5a.2) |
| `public.provision_user_vm()` | Supabase RPC | Platform | SECURITY DEFINER provisioning helper — creates/updates a VM row while preserving the current gateway API token for an existing VM (§3.5a.2) |
| `app_private.vm_bootstrap_tokens` | Supabase | Platform | One-time bootstrap token hashes, expiry timestamps, and consumed-at markers for VM provisioning/sync (§3.5a.3) |
| `app_private.issue_vm_bootstrap_token()` | Supabase RPC | Platform | SECURITY DEFINER helper — issues a short-lived raw bootstrap token for an active VM and stores only its hash (§3.5a.4) |
| `public.consume_bootstrap_token()` | Supabase RPC | Platform | SECURITY DEFINER bootstrap RPC — validates one-time token, reads `user_vms`, reads shared provider secrets from Vault, marks the token as used, and returns the env payload for the VM (§3.5a.5) |
| `vault.decrypted_secrets` | Supabase Vault | Platform | Shared platform provider secrets used during VM bootstrap, such as Anthropic, Perplexity, Deepgram, and Groq API keys (§3.5a.5) |

## Appendix B: Quick Reference — All Pydantic Models

| Model | Defined In | Direction |
|---|---|---|
| `TaskEnvelope` | `shared/contracts.py` | Bidirectional (Orchestrator ↔ Agent) — see §7.3 |
| `EventEnvelope` | `shared/contracts.py` | Agent → Orchestrator |
| `AgentResult` | `shared/contracts.py` | Agent → Orchestrator (terminal) |
| `AgentError` | `shared/contracts.py` | Inside AgentResult |
| `TaskInProgress` | `shared/contracts.py` | Idempotency sentinel |
| `Heartbeat` | `shared/contracts.py` | Agent → Registry |
| `ArtifactManifest` | `shared/contracts.py` | Attached to tasks and results |
| `AgentID` | `shared/contracts.py` | ID parsing utility |
| `ProviderAdapter` | `gateway/credentials/providers.py` | Base class for OAuth provider adapters (§22.6) |
| `ChannelAdapter` | `gateway/channels/base.py` | Base class for channel adapters (§27.1) |
| `WebhookVerifier` | `gateway/webhooks/providers.py` | Base class for webhook signature verifiers (§26.4) |
| `HooksEngine` | `gateway/hooks/engine.py` | Internal state change event dispatcher (§28) |
| `StepPlan` | `shared/step_plan.py` | Universal agent tool — per-task execution planning (§32.2) |
| `MemoryRead` | `shared/memory_tools.py` | Universal agent tool — shared memory search (§32.5) |
| `MemoryWrite` | `shared/memory_tools.py` | Universal agent tool — shared memory write (§32.5) |

**Note:** Credential data (accounts, credentials, resource_bindings, audit) is stored in SQLite tables in `gateway/credentials.db`, not as Pydantic models. The `input.auth` dict in TaskEnvelopes is a convention on the existing `input: dict` field (see §7.3), not a separate model. Scheduler data (cron_jobs, heartbeat_config) is in `gateway/scheduler/scheduler.db`. Webhook data (webhooks, webhook_log) is in `gateway/webhooks/webhooks.db`. Task Planner data (plans, plan_steps, tasks) is in `agents/orchestrator/store/data/task_ledger.db` (see §31.1).

## Appendix C: Quick Reference — Event Type Enum

| Event | Terminal? | Triggers |
|---|---|---|
| `task.accepted` | No | Agent received and validated task |
| `task.progress` | No | Intermediate progress update |
| `task.suspended` | No | Agent waiting for reverse-task reply |
| `task.resumed` | No | Agent continuing after suspension |
| `task.deferred` | **No** | Another instance executing — orchestrator owns recovery timer |
| `artifact.added` | No | Artifact produced |
| `task.rejected` | **No** | Stale epoch — triggers orchestrator redrive |
| `task.completed` | **Yes** | Success — closes task state, expires seq key |
| `task.failed` | **Yes** | Failure — closes task state, expires seq key |
| `task.dlq` | **Yes** | Dead letter queue — closes task state, expires seq key |

## Appendix D: Quick Reference — Source Tags

| `source` Value | Origin | Typical `source_id` | Typical `channel` | Default Priority |
|---|---|---|---|---|
| `user` | Human message from any channel | `null` | `desktop:<device_id>`, `whatsapp:+1234`, `telegram:chat_123`, `slack:C0123`, `cli` | `high` |
| `cron` | Scheduled job fired by Scheduler | `cron_morning_email`, `cron_weekly_review` | Cron's `delivery_channel` | `low` (configurable) |
| `heartbeat` | Periodic timer fired by Scheduler | `default` | Heartbeat's `delivery_channel` | `low` |
| `webhook` | External system callback | `wh_gmail_001`, `wh_github_pr` | Webhook's `delivery_channel` | `normal` |
| `hook` | Internal state change | `hook_gateway_startup`, `hook_session_reset` | `null` (internal) | `normal` |
| `agent` | Agent-initiated reverse task | Originating agent's `agent_id` | Inherited from parent task | `normal` |

**Propagation rule:** When the orchestrator decomposes a task into child tasks, it copies `source`, `source_id`, and `channel` from the parent TaskEnvelope to all children. This preserves the full provenance chain: a webhook event from Gmail that triggered a research task that delegated to a docs agent — all three TaskEnvelopes carry `source='webhook'`, `source_id='wh_gmail_001'`.

## Appendix E: Quick Reference — All Agent IDs

| Agent ID | Display Name | Status | Key Intents |
|---|---|---|---|
| `cosmic/orchestrator:1.0.0` | Orchestrator | **Core** | `orchestrator.process`, `orchestrator.clarify`, `orchestrator.delegate`, `orchestrator.schedule` |
| `cosmic/research-agent:1.0.0` | Research Agent | **Core** | `research.topic`, `research.find_image`, `research.recall_session` |
| `cosmic/docs-agent:2.1.0` | Docs Agent | **Core** | `docs.edit`, `docs.create`, `docs.resolve_resource`, `docs.recall_session` |
| `cosmic/browser-agent:1.0.0` | Browser Agent | **Core** | `browser.navigate`, `browser.interact`, `browser.extract`, `browser.screenshot` |
| `cosmic/system-agent:1.0.0` | System Agent | **Core** | `system.file_operation`, `system.process_manage`, `system.shell_execute`, `system.clipboard` |
| `cosmic/cli-agent:1.0.0` | CLI Agent | **Alpha** | `cli.execute` |
