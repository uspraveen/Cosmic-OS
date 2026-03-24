# COSMIC Background Tasks Plan

**Status:** Finalized design plan for concurrent foreground/background task execution within a single daily session
**Scope:** Gateway event routing, session history linkage, Desktop WebSocket protocol, task panel UI rail
**Alignment:** Preserves the existing single-session-per-day model, TaskEnvelope contract, orchestrator agentic loop, and specialist agent dispatch — no branching, no merging, no temporary document holding pens

---

## 1. Objective

Let a user move a long-running COSMIC task to the background, immediately start a new foreground query, and monitor or resurface background tasks from a dedicated task panel — without splitting the session, without branching history, and without breaking the existing orchestrator or specialist agent contracts.

---

## 2. Observations on Current State

### 2.1 Session Model

Sessions follow a one-per-calendar-day model keyed by user-local date, rolling over at a configurable hour (default 4 AM).

- Session ID format: `sess_YYYYMMDD`
- `_resolve_session_id()` always forces the current day's session ID, even if the client sends a different one
- `task_list_id = session_id` on every `TaskEnvelope` — all tasks within a day belong to the same session scope
- Child tasks spawned by the orchestrator inherit `parent_task.task_list_id`

### 2.2 Request Lifecycle

Each user message maps 1:1 to a single request-response flow:

1. Gateway normalizes the message, resolves session, routes, and stages artifacts
2. Gateway appends the user message to session history immediately
3. Gateway creates an `ActiveRequest` and spawns a fulfillment worker (`asyncio.Task`)
4. The worker streams the response (via orchestrator or direct model)
5. On completion, the response is appended to session history
6. Turn finalization writes a turn ledger entry and refreshes the active working set

### 2.3 ActiveRequest (Current Shape)

```python
@dataclass(slots=True)
class ActiveRequest:
    request_id: str
    session_id: str
    channel: str
    route: str
    worker: asyncio.Task[None] | None = None
    task_id: str | None = None
    cancel_requested: bool = False
    partial_content: str = ""
    partial_thinking: str = ""
    completed: bool = False
```

The Gateway tracks active requests in three maps:

- `active_requests: dict[str, ActiveRequest]` — keyed by `request_id`
- `active_requests_by_task: dict[str, str]` — maps `task_id` to `request_id`
- `active_task_channels: dict[str, str]` — maps `task_id` to `channel`

### 2.4 Session History Schema (Current)

```sql
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    route TEXT,
    request_id TEXT,
    awaiting_reply INTEGER NOT NULL DEFAULT 0,
    channel TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT
);
```

Key observation: `request_id` exists as a column but **`in_reply_to_request_id` does not**. There is no structural linkage between a user message and its assistant response beyond chronological ordering within the same `request_id`.

### 2.5 Turn Ledger (Current)

```sql
CREATE TABLE IF NOT EXISTS turn_ledger (
    turn_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    task_id TEXT,
    channel TEXT NOT NULL,
    route TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    user_message_id TEXT,
    assistant_message_id TEXT,
    user_goal TEXT NOT NULL,
    ...
);
```

The turn ledger already links `request_id` to both `user_message_id` and `assistant_message_id`. This is strong linkage within the turn, but it is not exposed on the `messages` table itself.

### 2.6 Event Delivery (Current)

`_deliver_or_queue_channel_event()` sends every event to the resolved channel's WebSocket. There is no concept of routing events to different UI surfaces — everything goes to the main response stream.

### 2.7 Resume Payload (Current)

On desktop reconnect, `build_resume_payload()` returns:

- `history_tail` — full session history
- `active_tasks` — currently running orchestrator tasks (from `TaskLedger.list_active_tasks()`)
- `pending_inputs` — any orchestrator questions waiting for user reply

There is no foreground/background distinction in the active tasks payload.

### 2.8 Compaction (Current)

Session compaction splits history into `recent_history` (last 12 messages) and `older_history`, then uses Haiku to compress older messages into a rolling summary. The compaction prompt receives:

- The existing compacted summary
- Compactable turn ledger entries
- Older raw conversation messages
- Active task refs

Compaction treats all messages as one linear conversation. There is no awareness of interleaved reply chains from concurrent tasks.

### 2.9 Orchestrator (Current)

The orchestrator handles each `TaskEnvelope` as an independent HTTP streaming call (`POST /internal/process/stream`). Multiple tasks can run concurrently — each is its own HTTP connection. The orchestrator does not know or care about foreground/background state.

### 2.10 What Does NOT Exist Today

- No foreground/background concept on `ActiveRequest`
- No mechanism for the user to send a new message while a previous task is still running
- No `in_reply_to_request_id` linkage on session history messages
- No event routing to different UI surfaces
- No background task metadata on assistant messages
- No compaction awareness of interleaved task threads
- No persistent background task state that survives Gateway restart

---

## 3. Core Principles

1. **Session stays singular.** One per day. No branching, no merging, no shadow sessions.
2. **Task lineage carries the structure.** Reply-chain linkage via `request_id` and `in_reply_to_request_id` — not chronological ordering.
3. **Background is a delivery state, not a memory model.** The orchestrator and specialist agents are unaware of foreground/background. This is purely a Gateway + Desktop concern.
4. **History is append-only, chronologically ordered by completion.** Background task responses land in session history when they complete, tagged with explicit metadata.
5. **One foreground stream at a time per channel.** Multiple background tasks are allowed concurrently. The user can only bring a background task to foreground when no other foreground stream is active.
6. **No work is lost on backgrounding.** The orchestrator stream continues running. Only the event delivery target changes.

---

## 4. Architecture Design

### 4.1 High-Level Flow

```mermaid
sequenceDiagram
    participant U as User (Desktop)
    participant G as Gateway
    participant O as Orchestrator
    participant A as Specialist Agent

    U->>G: Send query A
    G->>G: Append user_A to history
    G->>O: Stream TaskEnvelope A
    O->>A: Delegate to specialist
    Note over U,G: User sees streaming response...

    U->>G: "Background this" (request_id=A)
    G->>G: ActiveRequest A → foreground=False
    Note over G: Streaming continues; events now route to task panel

    U->>G: Send query B (new foreground)
    G->>G: Append user_B to history
    G->>O: Stream TaskEnvelope B
    Note over U,G: User sees streaming response for B...

    O-->>G: Task A completes
    G->>G: Append assistant_A to history (background=true, in_reply_to=req_A)
    G->>U: task.background.complete

    O-->>G: Task B completes
    G->>G: Append assistant_B to history (in_reply_to=req_B)
    G->>U: response.complete
```

### 4.2 Session History with Interleaved Background Tasks

After the flow above, session history looks like:

| # | role | request_id | in_reply_to_request_id | background | content |
|---|------|-----------|------------------------|------------|---------|
| 1 | user | req_A | — | false | "Research AWS Lambda pricing..." |
| 2 | user | req_B | — | false | "What's the weather in Chennai?" |
| 3 | assistant | req_B | req_B | false | "It's 34°C and humid in Chennai..." |
| 4 | assistant | req_A | req_A | true | "AWS Lambda pricing tiers are..." |

This is storage-correct and semantically unambiguous:

- Every assistant message has `in_reply_to_request_id` pointing to which user message it answers
- Background completions are tagged with `background: true`
- The model, compaction, and working-set extraction can all reconstruct thread structure

### 4.3 What the Model Sees

When the next user message is dispatched, `get_pruned_history()` returns all messages in chronological order. Background results are included with an annotation the model can reason about:

```
User: Research AWS Lambda pricing...
User: What's the weather in Chennai?
Assistant: It's 34°C and humid in Chennai...
Assistant: [Background task completed — in reply to "Research AWS Lambda pricing..."]
AWS Lambda pricing tiers are...
```

The annotation is injected by the history builder when `background: true` is set in metadata, using the linked user message's content excerpt as context.

---

## 5. Changes by Layer

### 5.1 Gateway: `ActiveRequest`

```python
@dataclass(slots=True)
class ActiveRequest:
    request_id: str
    session_id: str
    channel: str
    route: str
    worker: asyncio.Task[None] | None = None
    task_id: str | None = None
    cancel_requested: bool = False
    partial_content: str = ""
    partial_thinking: str = ""
    completed: bool = False
    foreground: bool = True                          # NEW
    backgrounded_at: str | None = None               # NEW — ISO timestamp
    user_query_excerpt: str = ""                      # NEW — for task panel display
```

### 5.2 Gateway: Session Store Schema

**New column on `messages` table:**

```sql
ALTER TABLE messages ADD COLUMN in_reply_to_request_id TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to
    ON messages(session_id, in_reply_to_request_id);
```

Added via `_ensure_column()` in `SessionStore.initialize()`, consistent with how `request_id` was added post-hoc.

**`append_message()` change:**

Accept a new `in_reply_to_request_id` parameter. When present, write it to the column. When absent, default to `None`.

```python
def append_message(
    self,
    session_id: str,
    *,
    role: str,
    content: str,
    route: str | None = None,
    awaiting_reply: bool = False,
    channel: str | None = None,
    metadata: dict[str, Any] | None = None,
    in_reply_to_request_id: str | None = None,       # NEW
) -> str:
```

### 5.3 Gateway: Event Delivery Routing

The core change. In `_deliver_or_queue_channel_event()` or its caller, check whether the event's `request_id` belongs to a foreground or background `ActiveRequest`:

```python
async def _deliver_or_queue_channel_event(self, event, *, channel=None):
    resolved_channel = ...
    request_id = self._safe_text(event.get("request_id"))
    state = self.active_requests.get(request_id) if request_id else None

    if state is not None and not state.foreground:
        # Re-namespace the event for the task panel
        event = {**event, "type": f"task.background.{event.get('type', 'unknown')}"}

    # ... existing delivery logic unchanged ...
```

This means the same event stream — `response.chunk`, `response.thinking.chunk`, `tool.call`, `tool.result`, `response.complete`, `task.input_required` — is delivered with a `task.background.*` prefix when the owning request is backgrounded.

### 5.4 Gateway: Background/Foreground Transitions

**Backgrounding a request:**

```python
async def background_active_request(self, *, channel: str, request_id: str) -> bool:
    state = self.active_requests.get(request_id)
    if state is None or state.channel != channel or state.completed:
        return False
    state.foreground = False
    state.backgrounded_at = utcnow_iso()
    # Notify desktop that the task moved to background
    await self._deliver_or_queue_channel_event(
        {
            "type": "task.backgrounded",
            "request_id": request_id,
            "session_id": state.session_id,
            "task_id": state.task_id,
            "channel": channel,
            "route": state.route,
            "user_query_excerpt": state.user_query_excerpt,
            "partial_content": state.partial_content[:500] if state.partial_content else "",
        },
        channel=channel,
    )
    return True
```

**Foregrounding a background task:**

```python
async def foreground_background_request(self, *, channel: str, request_id: str) -> bool:
    state = self.active_requests.get(request_id)
    if state is None or state.channel != channel or state.completed:
        return False
    if state.foreground:
        return True  # already foreground
    # Check no other foreground stream is active on this channel
    has_foreground = any(
        s.foreground and not s.completed and s.channel == channel
        for s in self.active_requests.values()
    )
    if has_foreground:
        return False  # reject — foreground is occupied
    state.foreground = True
    state.backgrounded_at = None
    await self._deliver_or_queue_channel_event(
        {
            "type": "task.foregrounded",
            "request_id": request_id,
            "session_id": state.session_id,
            "task_id": state.task_id,
            "channel": channel,
            "partial_content": state.partial_content,
            "partial_thinking": state.partial_thinking,
        },
        channel=channel,
    )
    return True
```

### 5.5 Gateway: History Append Changes

When a background task completes and its response is appended to session history, include explicit metadata:

```python
self._append_session_message(
    session_id,
    role="assistant",
    content=assistant_content,
    route=state.route,
    channel=state.channel,
    in_reply_to_request_id=state.request_id,      # NEW — explicit reply chain
    metadata={
        "request_id": state.request_id,
        "task_id": state.task_id,
        "background": not state.foreground,         # NEW — was this completed in background?
        "thinking_text": state.partial_thinking or None,
        **({"interrupted": True} if state.cancel_requested else {}),
    },
)
```

User messages also get `in_reply_to_request_id=None` (they are the root, not a reply).

Foreground assistant messages also get `in_reply_to_request_id=state.request_id` — the field is always present, not only for background tasks. This ensures every assistant message is structurally linked to its originating request regardless of foreground/background state.

### 5.6 Gateway: Model-Visible History Builder

`_get_model_visible_history()` calls `get_pruned_history()` which returns raw messages. A post-processing step annotates background completions:

```python
def _annotate_background_results(self, history: list[dict]) -> list[dict]:
    # Build a lookup: request_id → user message content excerpt
    user_excerpts: dict[str, str] = {}
    for msg in history:
        if msg["role"] == "user" and msg.get("request_id"):
            excerpt = (msg.get("content") or "")[:120].strip()
            user_excerpts[msg["request_id"]] = excerpt

    annotated = []
    for msg in history:
        metadata = msg.get("metadata") or {}
        if msg["role"] == "assistant" and metadata.get("background"):
            reply_to = msg.get("in_reply_to_request_id") or metadata.get("request_id")
            user_excerpt = user_excerpts.get(reply_to, "a prior request")
            prefix = f"[Background task result — in reply to: \"{user_excerpt}\"]\n\n"
            annotated.append({**msg, "content": prefix + msg["content"]})
        else:
            annotated.append(msg)
    return annotated
```

This gives the model unambiguous context about which background result answers which question.

### 5.7 Gateway: Compaction Awareness

The compaction prompt already receives turn ledger entries. Each turn has `request_id`, `user_message_id`, and `assistant_message_id`. The compaction user prompt is extended to include background task metadata:

```
Compactable turn ledger:
[req_abc] user: "Research AWS Lambda pricing..." → assistant: "AWS Lambda pricing tiers..." (background task, completed 14:32)
[req_def] user: "What's the weather?" → assistant: "34°C in Chennai..." (foreground, completed 14:30)
```

This lets the compaction model understand thread structure when summarizing. The turn ledger entry already stores `metadata_json` — extend it to include `{"background": true}` when the request was backgrounded.

### 5.8 Gateway: Resume Payload

`build_resume_payload()` is extended to include background task state:

```python
async def build_resume_payload(self, *, channel, request_id=None, ...):
    ...
    background_tasks = [
        {
            "request_id": state.request_id,
            "task_id": state.task_id,
            "session_id": state.session_id,
            "route": state.route,
            "user_query_excerpt": state.user_query_excerpt,
            "partial_content": state.partial_content,
            "backgrounded_at": state.backgrounded_at,
            "completed": state.completed,
        }
        for state in self.active_requests.values()
        if not state.foreground and state.channel == channel
    ]
    return {
        ...
        "background_tasks": background_tasks,       # NEW
    }
```

### 5.9 Gateway: Request Fulfillment Guards

Currently, `start_request_fulfillment()` does not check for existing foreground requests. Add a guard:

```python
def start_request_fulfillment(self, request_record):
    channel = request_record["channel"]

    # Reject if there is already a foreground stream on this channel
    has_foreground = any(
        s.foreground and not s.completed and s.channel == channel
        for s in self.active_requests.values()
    )
    if has_foreground:
        raise ValueError(
            "A foreground task is already active on this channel. "
            "Background it first or wait for it to complete."
        )

    state = ActiveRequest(
        request_id=...,
        session_id=...,
        channel=channel,
        route=...,
        foreground=True,
        user_query_excerpt=(request_record.get("query") or "")[:120].strip(),
    )
    ...
```

### 5.10 Desktop WebSocket Protocol

**New client → Gateway messages:**

| Message Type | Payload | Purpose |
|---|---|---|
| `background` | `{ request_id }` | Move a foreground request to background |
| `foreground` | `{ request_id }` | Move a background request to foreground (rejected if foreground is occupied) |

**New Gateway → client events:**

| Event Type | Payload | Purpose |
|---|---|---|
| `task.backgrounded` | `{ request_id, task_id, session_id, route, user_query_excerpt, partial_content }` | Confirms a task was moved to background; Desktop clears main screen |
| `task.foregrounded` | `{ request_id, task_id, session_id, partial_content, partial_thinking }` | Confirms a task was moved to foreground; Desktop renders accumulated content |
| `task.background.response.chunk` | Same as `response.chunk` | Streaming text for a background task |
| `task.background.response.thinking.chunk` | Same as `response.thinking.chunk` | Thinking text for a background task |
| `task.background.tool.call` | Same as `tool.call` | Tool call in a background task |
| `task.background.tool.result` | Same as `tool.result` | Tool result in a background task |
| `task.background.response.complete` | Same as `response.complete` | Background task finished |
| `task.background.task.input_required` | Same as `task.input_required` | Background task needs user input |
| `task.background.task.progress` | Same as `task.progress` | Progress update for a background task |

The `task.background.*` namespace is generated by prefixing the original event type. The Desktop routes these to the task panel UI.

### 5.11 Orchestrator Changes

**None.**

The orchestrator receives a `TaskEnvelope`, streams a response via HTTP. Whether the user is watching on the main screen or the task panel is irrelevant to the orchestrator. Each request is already an independent HTTP stream.

### 5.12 Specialist Agent Changes

**None.**

Specialist agents receive child `TaskEnvelope`s via Redis, execute, and return `AgentResult`s. They are unaware of foreground/background state.

### 5.13 Redis Bus Changes

**None.**

The Redis stream topology, priority queues, event streams, and backpressure mechanisms are unchanged.

---

## 6. Desktop UI Design Guidance

This section describes the expected UI behavior. Implementation details are the desktop team's domain.

### 6.1 Main Screen Components (Unchanged)

- **Spotlight query/text box** — where the user types
- **AI response screen** — where the streaming response renders

### 6.2 Background Trigger

A small icon/button in or near the query text box. When clicked while a response is streaming:

1. Sends `{ type: "background", request_id: "<current>" }` over WebSocket
2. Receives `task.backgrounded` confirmation
3. Clears the main response screen
4. The query text box becomes available for a new message
5. A badge/indicator appears showing 1 background task

### 6.3 Task Panel

A collapsible side panel or overlay showing background tasks:

- Each entry shows: user query excerpt, route (Opus/Haiku/etc.), status (streaming/completed/needs input), elapsed time
- Clicking an entry expands it to show the accumulated response stream
- Streaming tasks show live `task.background.response.chunk` content
- Completed tasks show the full response
- A "Bring to foreground" button is available only when the main screen is idle (no active foreground stream)
- A "Cancel" button is always available

### 6.4 Foreground Recovery

When the user clicks "Bring to foreground" on a still-running background task:

1. Desktop sends `{ type: "foreground", request_id: "<target>" }`
2. Gateway validates no foreground stream is active, flips the state
3. Desktop receives `task.foregrounded` with `partial_content` and `partial_thinking`
4. Desktop renders the accumulated content in the main response screen and continues streaming live

### 6.5 Task Input from Background

When a background task emits `task.background.task.input_required`:

- The task panel shows a "Needs your input" badge
- The user can click into it to see the question and respond
- The response is sent as `{ type: "task.input_reply", task_id: "...", content: "..." }` — the existing task input reply mechanism, unchanged
- The user does NOT need to bring the task to foreground to respond

### 6.6 Completed Background Tasks

When a background task completes:

- The task panel entry updates to "Completed" with the full response
- A subtle notification appears (toast or badge)
- The response is already in session history — if the user starts a new foreground conversation, the model sees it

---

## 7. Edge Cases

### 7.1 Multiple Background Tasks

User backgrounds A, then backgrounds B, starts C in foreground. Three concurrent workers in the Gateway. The task panel shows A and B. Main screen shows C. All three are independent orchestrator HTTP streams.

### 7.2 Background Task Needs User Input

The existing `task.input_required` / `task.input_reply` mechanism routes by `task_id`, not by foreground assumption. The task panel renders the question; the user responds via the panel. No foreground occupation needed.

### 7.3 Page Reload / Reconnect

On `resume`, the Gateway returns `background_tasks` in the payload. The Desktop reconstructs the task panel from `partial_content` and `completed` state. If a background task completed during the disconnection, its response is in `history_tail` with `background: true` metadata and in the `background_tasks` array with `completed: true`.

### 7.4 Gateway Restart

`ActiveRequest` is in-memory today. On Gateway restart, in-flight requests are lost — this is the existing behavior. The orchestrator task may still complete and emit events, but the Gateway's HTTP stream connection is broken.

For phase 1, this is acceptable. For phase 2, `ActiveRequest` state could be persisted to SQLite (a new `active_requests` table) and recovered on restart by re-establishing the orchestrator HTTP stream or marking the task as interrupted.

### 7.5 Session Compaction with Background Results

The compaction prompt builder tags turn ledger entries with `(background task)` when `metadata_json` contains `"background": true`. This gives the compaction model thread-awareness without changing the compaction architecture.

### 7.6 Cross-Channel (WhatsApp / Telegram)

Background tasks are a Desktop-only concept. Other channels continue with one-at-a-time foreground behavior. Cross-channel sync broadcasts foreground messages only. Background task completions are not synced to non-desktop channels — they are available via session history on reconnect.

### 7.7 Rate Limiting / Resource Management

Each background task is an independent orchestrator HTTP stream and an independent Anthropic API call. There should be a configurable maximum number of concurrent background tasks per session to prevent runaway resource consumption:

```python
MAX_BACKGROUND_TASKS_PER_SESSION = 5  # configurable via env
```

The Gateway rejects `background` requests when the limit is reached and returns a clear error to the Desktop.

### 7.8 Foreground Slot Contention

If the user tries to foreground a background task while a foreground stream is active, the Gateway rejects the request. The Desktop should disable the "Bring to foreground" button while the main screen is occupied.

### 7.9 Cancel While Backgrounded

Cancellation works the same as today. `cancel_active_fulfillment()` finds the `ActiveRequest` by `request_id` or `task_id`, sets `cancel_requested = True`, and cancels the worker `asyncio.Task`. The fact that the request is backgrounded does not change the cancellation flow.

---

## 8. Implementation Phases

### Phase 1: Core Backend (Gateway)

**Goal:** Enable concurrent request execution and event routing by foreground/background state.

**Changes:**

1. Add `foreground`, `backgrounded_at`, `user_query_excerpt` to `ActiveRequest`
2. Add `in_reply_to_request_id` column to `messages` table
3. Update `append_message()` to accept and persist `in_reply_to_request_id`
4. Write `in_reply_to_request_id` on every assistant message (foreground and background)
5. Write `background: true` in `metadata_json` for background-completed assistant messages
6. Add `background_active_request()` and `foreground_background_request()` methods
7. Add `task.background.*` event re-namespacing in event delivery
8. Add foreground guard in `start_request_fulfillment()`
9. Add `background_tasks` to `build_resume_payload()`
10. Add `MAX_BACKGROUND_TASKS_PER_SESSION` config guard
11. Handle `background` and `foreground` WebSocket message types in `routes.py`

**Does not include:** UI, compaction awareness, history annotation for model context.

### Phase 2: Model Context Awareness

**Goal:** Make the model and compaction system understand interleaved background results.

**Changes:**

1. Add `_annotate_background_results()` to the history builder
2. Extend turn ledger entries with `background` metadata
3. Update compaction prompt to tag background turns
4. Update working-set extraction to understand thread structure

### Phase 3: Desktop UI

**Goal:** Full task panel UI with background/foreground controls.

**Changes:**

1. Background icon in query text box
2. Task panel sidebar/overlay
3. Live streaming in task panel via `task.background.*` events
4. Foreground recovery with accumulated content
5. Task input from background
6. Cancel from background
7. Completed task notification
8. Foreground slot indicator (occupied/free)

### Phase 4: Persistence and Recovery

**Goal:** Background tasks survive Gateway restart.

**Changes:**

1. Persist `ActiveRequest` state to SQLite
2. Recover in-flight background tasks on Gateway startup
3. Re-establish orchestrator HTTP streams or mark as interrupted

---

## 9. What Does NOT Change

| Component | Changes? | Why |
|---|---|---|
| **Session model** | No | One per day, no branching |
| **Orchestrator runtime** | No | Each TaskEnvelope is already independent |
| **Orchestrator agentic loop** | No | Streaming, tool execution, agent dispatch — all unchanged |
| **Specialist agents** | No | Receive TaskEnvelope, return AgentResult — unaware of FG/BG |
| **Redis bus** | No | Stream topology, priority queues, events — all unchanged |
| **TaskEnvelope contract** | No | Same fields, same signing, same dispatch |
| **Artifact store** | No | Task-scoped artifacts, same paths |
| **Memory system** | No | Memory writes, reads, compaction — all unchanged |
| **Turn ledger schema** | Minimal | Only metadata_json content changes (add `background` flag) |
| **Task notebooks** | No | Same accumulation, same schema |
| **Session rollover** | No | Rollover, carry-forward, summary — all unchanged |
| **Cross-channel sync** | No | Foreground messages sync as before; background is Desktop-only |
| **Model router** | No | Routing classification is per-request, unchanged |

---

## 10. Key Design Decisions Summary

### Why not branch sessions?

Branching would create parallel session timelines that need merging. Merging conversation history is semantically dangerous — the model would see interleaved context from parallel branches that were generated without knowledge of each other. The result would be incoherent. A single linear session with explicit reply-chain linkage is simpler, safer, and sufficient.

### Why not hold background results in a staging area?

A staging area (temp doc) that buffers background results until the user "accepts" them would delay the model's awareness of completed work. If the user asks a follow-up question that depends on the background task's result, the model would not see it. Immediate append-to-history with metadata tagging gives the model full context while preserving thread structure.

### Why `in_reply_to_request_id` as a first-class column?

Metadata JSON is not indexed and not queryable. Thread reconstruction requires joining user messages to their assistant responses efficiently — for compaction, for history building, for the turn ledger, and for future features like thread-grouped display. A first-class indexed column is the right primitive.

### Why re-namespace events instead of a separate WebSocket channel?

The Desktop already has one WebSocket connection per device. Adding a second WebSocket for background events would complicate connection lifecycle, authentication, and reconnect logic. Re-namespacing events on the same connection is simpler and lets the Desktop route events client-side by prefix.

### Why limit concurrent background tasks?

Each background task is an independent Anthropic API call consuming tokens and compute. Unbounded concurrency would allow a user to spawn dozens of Opus tasks simultaneously, exhausting API rate limits and budget. A configurable cap (default 5) prevents this while still allowing meaningful parallelism.

---

## 11. Scaling and Future Considerations

### 11.1 Specialist Agent Progress Streaming

This plan pairs naturally with the specialist progress streaming feature discussed previously. When agent progress events are forwarded to the streaming response, background tasks would show live specialist status in the task panel (e.g., "Firecrawl agent scraping https://...").

### 11.2 Task Dependencies

A foreground task can reference results from a completed background task through:

- **Session history** — the model sees the background result with its annotation
- **Artifact system** — background tasks produce artifacts that can be looked up via `artifact_lookup`
- **Memory** — if the orchestrator wrote memory during the background task, it is available to future tasks

No explicit dependency graph is needed. The existing mechanisms provide implicit linkage.

### 11.3 Mobile / WhatsApp Background Tasks

If background tasks are extended to non-desktop channels in the future, the same architecture applies — `ActiveRequest.foreground` is per-request, not per-channel-type. The channel adapter would need to support the `background` / `foreground` message types and handle the UI constraints of each platform.

### 11.4 Task Queuing

If the user sends multiple queries rapidly without backgrounding, a future enhancement could auto-queue them rather than rejecting. The Gateway would hold subsequent requests in a per-channel queue and dispatch them as previous foreground tasks complete or are backgrounded. This is not in scope for this plan but the architecture supports it without structural changes.
