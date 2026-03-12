# Current Memory Architecture Summary

This document summarizes the **current memory architecture** defined in [cosmic_architecture.md](./cosmic_architecture.md). It is a structured restatement of the existing spec, focused on how COSMIC currently handles sessions, summarization, storage, retrieval, task memory, and agent access to memory.

This version is intentionally closer to an **extracted memory/session spec** than a short overview. It keeps the operational rules, file paths, storage boundaries, lifecycle steps, and implementation patterns from the main architecture document, while reorganizing them by concern.

This summary is based on the following sections of the architecture spec:

- `§3.11 Session Management`
- `§5.1 Top-Level Project Layout`
- `§12.1 Agent Learnings`
- `§12.2 Agent-Managed Session Data`
- `§12.3 Session Context Flow`
- `§12.4 Recall Intents`
- `§12.8 Event Stream Trimming & Archival`
- `§23 Session & Memory Management`
- `§27.2 Unified Sessions with Channel Tagging`
- `§32.5 MemoryRead & MemoryWrite`
- `§32.6 Universal Tool Summary`

Terminology note:

- **Session Manager** in this summary refers to the logical session/memory coordination role from the architecture.
- In the current implementation, that role is split across the Gateway runtime/session store and the same-VM internal `cosmic-memory` service.
- So this document stays architecture-first in naming, while the runtime remains service-backed in deployment.

## 1. Core Design Model

The current architecture defines memory as a **Gateway-integrated, service-backed system** with three distinct layers:

- **Today’s conversation**
  - Short-term conversational context stored canonically in `gateway/sessions.db`
  - Structured continuity state also stored in `gateway/sessions.db` as:
    - turn ledger rows,
    - task notebooks,
    - active working set,
    - carry-forward packet,
    - compaction packet
  - Derived append-only daily transcript archive in `logs/sessions/`
  - Subject to pruning and compaction
- **Retrieved long-term memories**
  - Stored as `.md` files under `memory/`
  - Indexed in Qdrant for hybrid retrieval
  - Owned by the internal `cosmic-memory` service on the same VM
  - Accessed by Gateway over internal HTTP and then assembled into prompts
  - Re-retrieved fresh on every turn
- **Task execution memory**
  - Isolated from the main conversation
  - Stored across Redis event streams, per-agent local stores, task summaries, and artifacts

The design principle is that these layers have different lifecycles and should not be mixed together.

Two current architecture-wide assumptions matter here:

- COSMIC is deployed as a **single-user-per-instance** backend, so the memory store serves one user and does not need tenant filtering in the hot path.
- All LLM backends receive the same assembled context, so the user experiences one consistent assistant regardless of whether the request ultimately routes to Opus, Haiku, or Perplexity.

## 2. Session Model

### 2.1 One Daily Session

The current spec says:

- Each day is a session.
- The user experiences one perpetual conversation.
- Session boundaries are transparent to the user.
- The Gateway maintains session state in SQLite.
- There is one shared **daily** session across all channels.
- WebSocket reconnects do not create new sessions.
- Daily sessions reset at **4:00 AM local time** with forced compaction.
- In addition to SQLite, the Gateway maintains a derived append-only daily transcript Markdown in `logs/sessions/`.
- SQLite remains the source of truth for live session state, routing, and replay.

### 2.2 Unified Cross-Channel Sessions

Sessions are **channel-agnostic**:

- All channels share the same session for the same day.
- Messages still carry their originating `channel`.
- This preserves cross-channel continuity.
- A user can start a task on Desktop and continue it on WhatsApp or Telegram.
- Sticky routing is still **channel-scoped**, so an `awaiting_reply` on one concrete channel does not capture replies from another channel.

The session ID format is channel-agnostic and date-based:

```python
def generate_session_id() -> str:
    date_part = utcnow().strftime('%Y%m%d')
    return f'sess_{date_part}'
```

Examples:

- Any channel on Jan 15: `sess_20250115`
- Next day: `sess_20250116`

## 3. Session Storage in SQLite

The current session state lives in `gateway/sessions.db`.

### 3.1 `sessions` Table

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    compaction_count INTEGER DEFAULT 0,
    compacted_summary TEXT,
    metadata_json TEXT
);
```

Meaning:

- `session_id`: daily session key
- `user_id`: present for future extensibility, but effectively constant in the single-user-per-VM model
- `compaction_count`: how many mid-day compactions occurred
- `compacted_summary`: the current compacted summary if compaction has already happened in the current day
- `metadata_json`: extra session metadata, currently including:
  - `active_working_set`
  - `carry_forward_packet`
  - `compaction_packet`
  - rollover summary bookkeeping

### 3.2 `messages` Table

```sql
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    route TEXT,
    channel TEXT,
    task_id TEXT,
    awaiting_reply BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
```

Meaning:

- `role`: `user`, `assistant`, or `system`
- `content`: stored message content
- `route`: which backend handled the message (`opus`, `haiku`, `perplexity`)
- `channel`: concrete originating channel such as `desktop:desk_a1b2c3` or `whatsapp:+123...`
- `task_id`: populated for `opus` task flows
- `awaiting_reply`: used for sticky routing

Indexes:

```sql
CREATE INDEX idx_messages_session ON messages(session_id, created_at);
CREATE INDEX idx_messages_channel ON messages(session_id, channel, created_at);
```

### 3.2a `turn_ledger` Table

`turn_ledger` is the canonical structured continuity ledger for completed visible turns.

```sql
CREATE TABLE turn_ledger (
    turn_id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE,
    session_id TEXT NOT NULL,
    task_id TEXT,
    channel TEXT NOT NULL,
    route TEXT NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    user_message_id TEXT,
    assistant_message_id TEXT,
    user_goal TEXT,
    user_message_excerpt TEXT,
    assistant_outcome TEXT,
    compact_line TEXT,
    facts_learned_json TEXT,
    preferences_detected_json TEXT,
    decisions_made_json TEXT,
    accomplished_json TEXT,
    tool_summary_json TEXT,
    touched_entities_json TEXT,
    task_refs_json TEXT,
    artifact_refs_json TEXT,
    failures_to_avoid_json TEXT,
    open_loops_json TEXT,
    metadata_json TEXT
);
```

Operational rule:

- one row is finalized per completed visible turn,
- only after the visible `response.complete` delivery succeeds.

### 3.2b `task_notebooks` Table

`task_notebooks` persists compact per-task continuity state.

```sql
CREATE TABLE task_notebooks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_state TEXT,
    notebook_json TEXT NOT NULL,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

Operational rule:

- the notebook is updated as orchestrator task events arrive,
- and enriched again when the final visible task result is delivered.

### 3.3 Derived Daily Transcript Archive

In addition to the canonical SQLite session store, the current architecture defines a **derived append-only daily transcript archive** under `logs/sessions/`.

Purpose:

- human-readable archival,
- export,
- debugging,
- operator inspection.

What it is not:

- not the source of truth for live session state,
- not used for sticky routing,
- not part of Qdrant retrieval.

Rules:

- the transcript is derived one-way from successful SQLite message writes,
- it is append-only while the day is active,
- it is finalized at the 4:00 AM rollover,
- if missing or corrupted, it is regenerated from `sessions.db`,
- agents do not edit it directly.

Path shape:

```text
logs/sessions/2025-01-15.md
```

Example rendered format:

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

Why the architecture uses one-way derivation instead of bi-directional sync:

- SQLite keeps the structured live session state,
- the transcript keeps readability and exportability,
- and the system avoids creating a second writable system of record.

## 4. Per-Turn Context Assembly

Every input goes through the Session Manager’s `assemble_context()` flow before routing:

1. Load today’s conversation from `sessions.db`
2. If compaction already occurred, prepend `compacted_summary`
3. Load the bounded active working set from session metadata
4. Retrieve memories from Qdrant hybrid search
5. Deduplicate memories by `memory_id`
6. Optionally attach deterministic revisit payloads when a higher-level flow explicitly asks for exact prior context
7. Return:

```python
{
    "memories": ...,
    "conversation": ...,
    "compacted_summary": ...,
    "active_working_set": ...,
    "deterministic_revisit": ...
}
```

The assembled context is then used for classification and sent to the final backend.

Important current rule:

- **All LLM backends receive the same assembled context**
  - Opus, Haiku, and Perplexity all get the same Session Manager output.

## 5. Session Lifecycle

The current lifecycle is:

- **App startup**
  - Gateway loads or creates today’s session
  - Desktop app receives the tail of today’s history for display
- **Each message**
  - Session Manager assembles context
  - Message and response are stored in `sessions.db`
  - After the SQLite write succeeds, a rendered entry is appended to `logs/sessions/YYYY-MM-DD.md`
- **Context reaches 70%**
  - Mid-day compaction runs
  - Older messages are summarized
  - Summary replaces them in prompt context
- **4:00 AM**
  - Forced compaction of the remaining session
  - The previous day’s transcript in `logs/sessions/` is finalized
  - Compacted summary is written as memory
  - Summary is stored under `memory/sessions/`
  - Summary is indexed into Qdrant
  - New session ID is created for the new day

## 6. Context Window Management

The architecture defines two separate mechanisms:

### 6.1 Pruning

Pruning is continuous:

- As new messages arrive, the oldest messages stop being sent to the LLM
- Pruned messages still remain in `sessions.db`
- Pruning is lightweight and always running

### 6.2 Compaction

Compaction is triggered when context usage hits **70%** of the target model’s context allocation.

The current compaction flow is:

1. Extract memories from the conversation before summarization
2. Send older conversation messages to a fast/cheap model
3. Replace old messages in the session context with the summary
4. Continue the conversation as:
   - `[compacted summary] + [recent messages since compaction]`

The compaction model must be cheap/fast:

- Claude Haiku 4.5 or Sonnet
- Explicitly **not Opus**

Reference implementation in the spec:

```python
async def check_and_compact(session_id: str, context_tokens: int,
                             model_context_window: int):
    threshold = model_context_window * 0.70
    if context_tokens < threshold:
        return

    messages = await get_compactable_messages(session_id)
    await extract_and_store_memories(messages)

    summary = await compaction_llm.summarize(
        messages=messages,
        instruction='Summarize this conversation preserving key decisions, '
                    'facts, user preferences, and action items.',
        max_output_tokens=4000,
    )

    await store_compacted_summary(session_id, summary)
    db.execute('''
        UPDATE sessions SET compaction_count = compaction_count + 1,
        compacted_summary = ?, updated_at = ?
        WHERE session_id = ?
    ''', [summary, utcnow(), session_id])
```

For implementation, the compaction system should be treated as a **structured state-reduction system**, not a naive “summarize old chat” pass. The model-visible summary should be derived from a canonical turn ledger and a narrow compaction template, not from raw tool chatter or raw model thinking.

#### 6.2a Canonical Turn Ledger Entry

Each completed visible turn should yield one structured turn ledger entry:

```python
TurnLedgerEntry = TypedDict(
    'TurnLedgerEntry',
    {
        'turn_id': str,                    # stable unique turn id
        'session_id': str,                 # sess_YYYYMMDD
        'channel': str,                    # concrete channel
        'route': Literal['opus', 'haiku', 'perplexity'],
        'started_at': str,                 # ISO timestamp
        'completed_at': str,               # ISO timestamp
        'user_goal': str,                  # normalized user intent
        'user_message_excerpt': str,       # bounded user-visible excerpt
        'assistant_outcome': str,          # what COSMIC actually delivered
        'facts_learned': list[str],        # new durable facts or constraints
        'preferences_detected': list[str], # stable user preferences
        'decisions_made': list[str],       # choices that should persist
        'accomplished': list[str],         # work completed this turn
        'tool_summary': list[str],         # normalized categories only
        'touched_entities': list[dict],    # files/docs/urls/artifacts/accounts
        'task_refs': list[str],            # task ids touched or created
        'artifact_refs': list[str],        # artifact ids or paths
        'failures_to_avoid': list[str],    # dead ends worth preserving
        'open_loops': list[str],           # unanswered questions / pending follow-up
        'compact_line': str,               # single-line durable summary
    },
)
```

**Design rules:**

- One entry is created per completed user-visible turn.
- It is finalized only after the visible delivery succeeds.
- Tool calls are normalized into categories/results, not stored as raw JSON blobs.
- File/doc/artifact references are preserved as identifiers plus short notes.
- Raw model thinking / chain-of-thought is **not** stored in the turn ledger.
- Internal task event chatter stays in task memory, not in the main conversational compaction path.

This pattern combines the strongest parts of the `docs_agent` turn summarizer and the `cosmic-browser-use` step-memory compressor: preserve outcomes, constraints, and touched entities while keeping raw execution detail retrievable elsewhere.

#### 6.2b Compaction Input Contract

The compaction model should see:

- the existing `compacted_summary` if one already exists,
- the compactable `TurnLedgerEntry` rows for the older range,
- the raw user/assistant message text for that same older range,
- the current recent-message window that will remain uncompressed,
- session-level metadata that matters for continuity (active tasks, `awaiting_reply`, current channel state).

The compaction model should **not** see:

- the retrieved long-term memory block,
- raw tool call payloads or raw tool results,
- raw task event streams,
- raw model thinking / reasoning blocks,
- full artifact bodies or large extracted documents,
- derived transcripts from `logs/sessions/`.

The purpose of compaction is to preserve **operationally useful continuity**, not to archive every token the model produced.

#### 6.2c Default Compaction Template

The default compaction template should be:

```md
# Session Compaction

## Goal
What the user is trying to accomplish overall.

## Active Workstreams
Current threads, tasks, or investigations still in progress.

## Key Facts
Facts established during the compacted range that matter going forward.

## User Preferences
Stable user preferences learned or reinforced during the compacted range.

## Decisions Made
Choices that were made and should not be re-litigated unless the user asks.

## Accomplished
What COSMIC completed during the compacted range.

## Files / Docs / Artifacts Touched
Normalized references only: files, docs, urls, artifact ids, account refs.

## Failures / Dead Ends
Important failed attempts, rejected paths, or constraints to avoid repeating.

## Open Loops
Questions awaiting reply, pending follow-up, or unresolved subproblems.

## Next Best Actions
What COSMIC should most likely do next if the conversation resumes here.
```

The compaction prompt should explicitly instruct the model to:

- preserve facts, decisions, preferences, and open loops,
- preserve touched-entity references when they are useful,
- omit decorative prose,
- omit raw reasoning,
- omit repetitive tool chatter,
- stay concise enough to leave headroom for continued conversation.

#### 6.2d Compaction Output Packet

The compaction run should produce a structured packet, not just a free-form paragraph:

```python
CompactionPacket = TypedDict(
    'CompactionPacket',
    {
        'goal': str,
        'active_workstreams': list[str],
        'key_facts': list[str],
        'user_preferences': list[str],
        'decisions_made': list[str],
        'accomplished': list[str],
        'touched_entities': list[dict],
        'failures_to_avoid': list[str],
        'open_loops': list[str],
        'next_best_actions': list[str],
        'rendered_summary': str,             # compact model-visible summary
        'compacted_turn_count': int,         # how many turn-ledger entries are represented
        'compacted_until_completed_at': str, # checkpoint for incremental re-compaction
    },
)
```

**Storage rule:**

- `sessions.compacted_summary` stores `rendered_summary` for prompt assembly.
- The full packet should also be persisted as structured session state so rollover, debugging, and future retrieval can use more than the flattened text.
- `compacted_until_completed_at` is the incremental compaction checkpoint. Once a turn is represented in the packet, later compaction runs should only summarize newly eligible older turns instead of re-summarizing the same historical range forever.

### 6.3 Token Budget Model

The spec defines the high-level budget shape:

- System prompt: about `1-2k`
- Retrieved memories: about `10-12k`
- Compaction reserve: about `4-6k`
- Output reserve: about `4k`
- Today’s conversation fills the remaining space

Compaction triggers when today’s conversation reaches 70% of its allowed portion.

### 6.4 Why Compaction Reserve Exists

The compaction reserve exists so the system has space for:

- the summary itself,
- some headroom while compaction is happening,
- continued conversation after the summary is inserted.

## 7. 4 AM Daily Summarization and Reset

At the daily session boundary, the current architecture does the following:

1. Force-compact the remaining session
2. Finalize the previous day’s transcript in `logs/sessions/`
3. Write the compacted summary as a memory
4. Store it in `memory/sessions/`
5. Index it in Qdrant
6. Start a fresh new daily session

The doc treats this as transparent to the user:

- the user still experiences one ongoing assistant,
- the full-day transcript is preserved as a readable append-only archive,
- the summary becomes retrievable long-term memory,
- the old day’s conversation is preserved as memory,
- the new day starts with a fresh daily session.

Configuration:

```ini
SESSION_RESET_HOUR=4
```

### 7.1 Carry-Forward Packet for the New Day

To preserve the **illusion of a perpetual assistant**, the 4 AM reset should not behave like amnesia. The old session ends, but a compact carry-forward packet should seed the next day.

The carry-forward packet should contain:

```python
CarryForwardPacket = TypedDict(
    'CarryForwardPacket',
    {
        'goal': str,
        'active_workstreams': list[str],
        'open_loops': list[str],
        'active_task_refs': list[str],
        'current_focus_entities': list[dict],   # docs/files/urls/artifacts in active use
        'stable_user_preferences': list[str],
        'failures_to_avoid': list[str],
        'bootstrap_note': str,                  # short text injected into the new day
    },
)
```

**Rule:** the new day should start with a fresh `session_id`, but the assistant should still know what is in flight. The carry-forward packet is the bridge between “daily session reset” and “perpetual assistant feel.”

The carry-forward packet should be built from:

- the final `CompactionPacket`,
- unresolved recent turns after the last compaction,
- active task notebook state,
- sticky conversational state that still matters.

### 7.2 Daily Session Summary Memory

The daily session summary written to `memory/sessions/<session_id>.md` should be derived from:

- the final `CompactionPacket`,
- the final carry-forward packet,
- any recent un-compacted turns near the day boundary.

This summary is not just a recap. It is the **retrievable long-term memory object** for that day.

## 8. Memory Retrieval

On every turn, the Session Manager retrieves long-term memory using hybrid search.

Important rule:

- Retrieved memories are assembled **fresh every turn**
- They are **not** part of the conversation that gets compacted
- The derived daily transcript archive is also **not** part of retrieved memory

### 8.1 Retrieval Pipeline

The current pipeline is:

1. Embed the query using `Qwen3-embedding-8b` via OpenRouter
2. Generate a sparse vector locally with FastEmbed / BM25
3. Run Qdrant hybrid search using a single Query API request
4. Use server-side Reciprocal Rank Fusion (RRF)
5. Re-rank results using:
   - recency weighting
   - source priority (`agent_notes > user_data`)
   - deduplication by `memory_id`
6. Select the top memories within the `10-12k` token budget
7. Format them as a memory block in the prompt

### 8.2 Why the Spec Chooses Qdrant-Native Hybrid Search

The architecture explicitly prefers Qdrant-native hybrid search over maintaining a separate BM25 index over `.md` files because of:

- single query / single system
- atomic sync between dense and sparse vectors
- better performance than scanning raw `.md` files
- simpler operations and rebuilds

### 8.3 Qdrant Collection Shape

The current `memories` collection includes:

- dense vectors
- sparse vectors
- server-side fusion via RRF

The spec’s example:

```python
async def create_memory_collection(qdrant: QdrantClient):
    qdrant.create_collection(
        collection_name='memories',
        vectors_config={
            'dense': models.VectorParams(
                size=1024,
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            'sparse': models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            ),
        },
    )
```

And retrieval:

```python
async def hybrid_search(qdrant: QdrantClient, query_dense: list[float],
                         query_sparse: models.SparseVector,
                         limit: int = 20,
                         type_filter: list[str] | None = None) -> list:
    results = qdrant.query_points(
        collection_name='memories',
        prefetch=[
            models.Prefetch(query=query_dense, using='dense', limit=limit * 2, filter=filter_condition),
            models.Prefetch(query=query_sparse, using='sparse', limit=limit * 2, filter=filter_condition),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
    )
    return results.points
```

### 8.4 Per-Turn Context Layout

The spec’s per-turn context layout is effectively:

- **System prompt**
  - about `1-2k` tokens
  - identity, capabilities, and rules
- **Active working set**
  - a bounded structured packet for what is currently in flight
  - current focus, open loops, recent decisions, active task refs, and touched entities
  - always preferred over broad older memory when it already contains the needed context
- **Retrieved memories**
  - about `10-12k` token budget
  - each memory carries:
    - `memory_id`
    - `type`
    - date / relevance signal
    - content
  - ranked by recency and similarity
  - `agent_note` memories outrank `user_data`
  - duplicate `memory_id`s are not allowed in the same context
  - this block is explicitly excluded from compaction
- **Today’s conversation**
  - includes `compacted_summary` if compaction already happened
  - oldest messages are pruned as new ones arrive
  - compaction triggers at 70% of this allocation
- **Deterministic revisit payloads**
  - narrow readbacks from turn/task/artifact history when similarity search is not enough
  - only injected on demand
- **Reserves**
  - compaction reserve: about `4-6k`
  - output reserve: about `4k`

This layout is what the Session Manager assembles before routing.

### 8.5 Active Working Set

In addition to the long-term memory block, COSMIC should maintain a bounded **active working set** for each live daily session.

This is the layer that creates the feeling of “the assistant is still actively holding what matters right now,” without replaying the whole transcript.

```python
ActiveWorkingSet = TypedDict(
    'ActiveWorkingSet',
    {
        'session_id': str,
        'goal': str,
        'active_workstreams': list[str],
        'recent_decisions': list[str],
        'open_loops': list[str],
        'current_focus_entities': list[dict],   # docs/files/urls/accounts/artifacts
        'active_task_refs': list[str],
        'pending_artifact_pointers': list[str],
        'user_preferences_in_play': list[str],
        'last_updated_at': str,
    },
)
```

**Design rules:**

- The active working set is bounded and should stay small enough to be included on every turn.
- It is not the same as long-term memory and should not be embedded into Qdrant as-is.
- It is rebuilt continuously from:
  - the latest `TurnLedgerEntry` records,
  - the current `TaskNotebook` states,
  - the carry-forward packet,
  - and any explicitly pinned active artifacts.
- It should be persisted with session state so reconnects and restarts do not erase it.
- `open_loops` should be derived from **currently unresolved** state:
  - assistant messages that still have `awaiting_reply = true`,
  - task notebooks that are still non-terminal and waiting on input/follow-up,
  - and carry-forward loops that have not yet been resolved.
- Historical turn-ledger `open_loops` are useful for compaction and revisit, but they should not keep polluting the live working set after the user has already answered them.

### 8.6 Deterministic History Revisit

Similarity retrieval is not enough for all memory problems. COSMIC also needs a **deterministic revisit path**.

This is the pattern borrowed from the best parts of `docs_agent` and `cosmic-browser-use`: if something important is not in the active working set, do not guess and do not rely only on vector recall. Re-open the relevant structured history directly.

Deterministic revisit should support:

- reading a range of turn-ledger entries,
- reading a range of raw session turns from `sessions.db`,
- reading one task notebook,
- reading one task’s recent raw event history,
- reading one artifact-pointer record,
- reading a bounded slice of a large stored artifact body.

This should be the preferred path for:

- “what did we decide earlier?”
- “what changed in that task?”
- “show me the source extract behind that summary”
- “resume the exact file/doc/artifact context from before”

### 8.7 Large-Artifact Spillover and Pointer Memory

One of the strongest patterns from `cosmic-browser-use` is that large notes should not be stuffed into prompt-visible memory. COSMIC should adopt the same principle for large extracts, transcripts, tables, media-derived notes, and bulky task outputs.

**Rule:** if a memory candidate is too large to be kept as normal inline memory, COSMIC should:

1. write the full body to the artifact store,
2. create a compact pointer record in long-term memory,
3. retrieve the pointer by default,
4. fetch the full body or a bounded slice only when needed.

This avoids prompt bloat while preserving exact recoverability.

```python
ArtifactPointerRecord = TypedDict(
    'ArtifactPointerRecord',
    {
        'pointer_id': str,
        'artifact_id': str,
        'session_id': str | None,
        'task_id': str | None,
        'kind': str,                     # transcript_extract, table_dump, media_transcript, research_extract, etc.
        'contains': str,                 # what is in the artifact
        'source': str,                   # origin url/file/channel/tool
        'why_relevant': str,             # why this was saved
        'summary': str,                  # compact retrieval-facing summary
        'path': str,                     # canonical artifact path
        'line_count': int | None,
        'token_estimate': int | None,
        'created_at': str,
    },
)
```

**Retrieval rule:** the pointer record is normal long-term memory; the full body is not. The full body is fetched only through deterministic revisit when the model or runtime explicitly needs it.

## 9. Memory Types in the Current System

The memory types explicitly present in the current architecture and current `cosmic-memory` integration are:

| Type | Source | Priority | Purpose |
|---|---|---|---|
| `core_fact` | `memory/core_facts/*.md` | **High** | Deterministic always-on profile facts and stable user identity/preferences |
| `session_summary` | `memory/sessions/*.md` | Normal | Broad conversational memory from prior days |
| `agent_note` | `memory/agent_notes/*/learnings.md` | High | Agent-curated user preferences, patterns, task-relevant learnings |
| `user_data` | `memory/user_data/` | Normal | Indexed user emails, files, documents |
| `task_summary` | `memory/tasks/*.md` | Normal | Summaries of completed tasks |
| `transcript` | `memory/transcripts/*.md` | Low | Canonical ingested runtime episodes for recall and provenance, distinct from derived `logs/sessions/` transcripts |
| `artifact_pointer` | `memory/artifact_pointers/*.md` | Normal | Compact pointers to large external bodies stored under `runs/artifacts/`, allowing retrieval without prompt bloat |

The spec explicitly says:

- agent notes are prioritized over user data,
- because agent notes are curated high-signal facts,
- while raw user data is bulk grounding material.

## 10. Critical Compaction Rule

The current architecture is explicit:

- **Memories are excluded from compaction input**
- Compaction only sees raw conversation messages

The reason given is to prevent this feedback loop:

- memory retrieved,
- included in context,
- compacted into a summary,
- summary becomes memory,
- retrieved again,
- compacted again

Instead:

- memories remain at source fidelity,
- are stored independently,
- and are re-retrieved each turn based on relevance.

## 11. `.md` Files as Source of Truth

The current architecture makes this explicit:

- `.md` files are the **source of truth**
- Qdrant is the **index**

If Qdrant data is lost, the system rebuilds the index by re-embedding the `.md` files.

### 11.1 Current Memory File Examples

#### Session summary file

```markdown
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

#### Agent note file

```markdown
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

#### Task summary file

```markdown
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

#### Core fact file

```markdown
---
memory_id: mem_core_pref_default_style
type: core_fact
canonical_key: user.default_response_style
updated_at: 2025-01-15T14:35:00Z
tags: [preference, style]
---

# Core Fact

User prefers concise, implementation-ready answers by default.
```

#### Transcript episode file

```markdown
---
memory_id: mem_transcript_turn_01
type: transcript
session_id: sess_20250115
turn_id: turn_01
channel: desktop:desk_a1b2c3
created_at: 2025-01-15T10:03:26Z
tags: [transcript, episode]
---

# Transcript Episode

User asked to update the project proposal. COSMIC responded that it added a conclusion section.
```

#### Artifact pointer file

```markdown
---
memory_id: mem_artifact_ptr_tbl_01
type: artifact_pointer
artifact_id: art_tbl_01
task_id: tsk_market_scan_01
created_at: 2025-01-15T15:12:00Z
tags: [artifact-pointer, table, research]
---

# Artifact Pointer

Contains: Full comparison table of 25 AI coding tools
Source: https://example.com/ai-coding-tools
Why relevant: Too large for inline memory; needed for later revisit and source-backed comparison
Summary: Large comparison table covering pricing, local-model support, license, and star counts
Path: runs/artifacts/art_tbl_01/table.md
```

## 12. Memory Write Path

The current write path is:

1. Write the `.md` file first
2. Generate both dense and sparse vectors
3. Atomically upsert both into Qdrant

Reference flow:

```python
async def write_memory(memory_id: str, memory_type: str, content: str,
                        metadata: dict):
    path = get_memory_path(memory_type, metadata)
    write_md_file(path, memory_id, memory_type, metadata, content)

    dense_vector = await embed_text(content)
    sparse_vector = await generate_sparse_vector(content)

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
                    'content': content,
                },
            ),
        ],
    )
```

Sparse vector generation uses local FastEmbed BM25:

```python
sparse_model = SparseTextEmbedding(model_name='Qdrant/bm25')
```

## 13. Memory Sync Guarantees and Rebuild

The architecture defines three consistency mechanisms:

### 13.1 Atomic Write Path

- `.md` is written first
- Qdrant upsert happens second
- If Qdrant fails, the file still exists
- The reverse does not happen

### 13.2 `memory_id` as Join Key

The same `memory_id` exists in:

- file frontmatter
- Qdrant point ID

This allows:

- detecting orphaned Qdrant points
- detecting unindexed files
- detecting stale indexes when file content changed

### 13.3 Startup Consistency Check and Full Rebuild

At startup, the Session Manager can:

- scan all `.md` files
- scroll Qdrant point IDs
- re-index files that exist on disk but not in Qdrant
- delete orphaned Qdrant points

If needed, a full rebuild drops and recreates the Qdrant collection from `memory/`.

Important boundary:

- this sync/rebuild logic applies to the shared long-term memory store under `memory/`,
- not to `logs/sessions/`,
- because daily transcripts are derived from SQLite and are regenerated from session history rather than re-embedded into Qdrant.

Configuration:

```ini
MEMORY_SYNC_ON_STARTUP=true
QDRANT_PATH=./qdrant_data
MEMORY_STORE_PATH=./memory
```

## 14. Current Memory-Related File Structure

The architecture’s top-level layout includes these memory-relevant paths:

```text
Cosmic-OS/Backend/
├── gateway/
│   ├── sessions.db
│   ├── delivery_queue.db
│   ├── routing_audit.db
│   ├── artifacts.db
│   └── memory_client.py
├── logs/
│   ├── sessions/
│   └── events/
├── runs/
│   └── artifacts/
└── systemd/
    └── cosmic-memory.service.example

cosmic-memory/
├── src/cosmic_memory/
├── memory/
│   ├── core_facts/
│   ├── sessions/
│   ├── tasks/
│   ├── agent_notes/
│   ├── user_data/
│   ├── artifact_pointers/
│   └── transcripts/
└── qdrant_data/
```

### 14.1 `memory/` Directory

The current shared memory tree is:

```text
memory/
├── core_facts/   # deterministic always-on user/profile facts
├── sessions/     # compacted daily session summaries
├── tasks/        # task result summaries
├── agent_notes/  # synced agent learnings
├── user_data/    # indexed user emails/files/documents
├── artifact_pointers/  # compact references to large external bodies
└── transcripts/  # canonical ingested turn/episode memories
```

### 14.1a `logs/sessions/` Derived Transcript Archive

The current architecture also defines:

```text
logs/sessions/
├── 2025-01-15.md
└── 2025-01-16.md
```

These files are:

- full daily session transcripts,
- append-only,
- derived from `sessions.db`,
- finalized at the daily rollover,
- not part of the shared retrievable memory set.

This is distinct from `memory/transcripts/`:

- `logs/sessions/` is a human-readable derived daily archive,
- `memory/transcripts/` is a canonical retrievable episode memory set,
- both can exist at the same time without conflicting because they serve different purposes.

### 14.2 Agent-Local Stores

Agents also keep their own local persistent stores:

- `agents/*/store/learnings.md`
- `agents/*/store/data/`

These are not the same as the shared `memory/` tree.

### 14.3 Persistent Volume Requirements

The architecture explicitly says these must be persistent:

- `memory/`
- `qdrant_data/`
- `logs/sessions/`
- `logs/events/`
- agent `store/` directories

## 15. Agent Learnings

Each agent maintains `store/learnings.md` as its own long-term private memory.

The spec says agents:

- read `learnings.md` at task start,
- append to it after completed tasks when new knowledge is worth preserving,
- keep it private from other agents,
- and the Gateway/session layer syncs it into the shared `memory/agent_notes/` tree for global retrieval.

This is how agent-private long-term memory becomes part of the shared retrieval layer once the full subagent runtime is active.

## 16. Agent-Managed Session Data

Each agent manages its own domain-specific `store/data/` schema.

Examples in the spec:

- docs agent tracks edits, before/after hashes, revisions, verification, rollback metadata
- research agent tracks sources, citation counts, and confidence

The examples in the architecture document show:

- docs agent `edit_sessions`
  - `session_id`
  - `task_id`
  - `doc_id`
  - `operation`
  - `target`
  - `summary`
  - `before_hash`
  - `after_hash`
  - `revision_before`
  - `revision_after`
  - `verified`
  - `metadata_json`
  - `created_at`
- research agent `research_sessions`
  - `session_id`
  - `task_id`
  - `query`
  - `sources_json`
  - `citations_count`
  - `confidence_score`
  - `created_at`

The architecture explicitly says:

- there is **no shared uniform session schema** across agents
- agents do **not** read each other’s databases
- the orchestrator passes relevant session context through task input instead

## 17. Session Context Flow Across Agents

The current design is:

- the orchestrator maintains session-wide context
- it passes relevant context into `TaskEnvelope.input`
- it asks agents about prior work using recall intents
- it promotes important facts discovered by one agent into future tasks for other agents

The spec explicitly rejects a shared session database because:

- shared SQLite would require filesystem coordination,
- shared Redis would couple agent schemas,
- recall intents keep agents decoupled.

## 18. Recall Intents

When the orchestrator needs detailed agent-specific history, it sends a recall intent such as:

- `docs.recall_session`
- `research.recall_session`

The agent then:

- queries its own local `store/data/`
- returns structured results

This is the current mechanism for agent-specific historical recall.

The example flow in the spec is:

- orchestrator dispatches `docs.recall_session`
- input includes:
  - `session_id`
  - `query`
  - `limit`
- docs agent queries its own `store/data/sessions.db`
- agent returns structured edit history in `AgentResult.output`

## 19. Task Memory Isolation

The architecture is explicit that task execution is isolated from the main conversation.

What does **not** go into the main session:

- agent progress chatter
- intermediate tool work
- retries
- internal clarifications

What **does** go into the main session:

- only the final task result as a user-facing message

This keeps the main conversation clean.

### 19.1 Where Task Memories Live

| Data | Location | Access Pattern |
|---|---|---|
| Task execution events | `streams:events` | Real-time and replay |
| Agent session data | `agents/*/store/data/` | Queried via recall intents |
| Agent learnings | `agents/*/store/learnings.md` and synced `memory/agent_notes/` | Retrieved as high-priority memory |
| Task result summaries | `memory/tasks/<task_id>.md` | Retrieved by hybrid search |
| Task artifacts | `runs/artifacts/<task_id>/` | Passed between agents via `ArtifactManifest` |

### 19.2 How Task Memories Enter Retrieval

After task completion, the orchestrator writes a summary to `memory/tasks/<task_id>.md`.

That summary contains:

- what the task was,
- which agents were involved,
- the final result,
- artifacts produced

The summary is then embedded and indexed into Qdrant, making completed task history retrievable.

### 19.2a Task Notebook Schema

To preserve **per-task perpetual context**, each task should also maintain a compact task notebook in addition to raw events:

```python
TaskNotebook = TypedDict(
    'TaskNotebook',
    {
        'task_id': str,
        'goal': str,
        'status': str,
        'current_state': str,
        'key_findings': list[str],
        'agents_involved': list[str],
        'files_touched': list[dict],
        'artifact_refs': list[str],
        'open_questions': list[str],
        'failures_to_avoid': list[str],
        'next_best_actions': list[str],
        'compact_history': list[str],   # bounded line-level progression
        'updated_at': str,
    },
)
```

**Design rule:** when a task resumes, the orchestrator should rebuild context from the task notebook + recent task events + artifacts, not from the main conversational session alone.

When a task produces a large output that is too big for inline memory, the task notebook should reference an `artifact_pointer` instead of duplicating the full body into the notebook or the shared memory text.

### 19.3 How the Orchestrator Retrieves Past Task Context

The current architecture defines two retrieval paths for past task context.

#### Path 1: Event index lookup

- every emitted event appends its stream message ID to `task_events:{task_id}`,
- to replay one task, the orchestrator reads only those IDs,
- it then fetches those exact messages from `streams:events`,
- this makes replay `O(events for this task)` rather than `O(total events in the stream)`,
- if the per-task index has expired or the events were trimmed, the orchestrator falls back to the disk archive in `logs/events/<task_id>.jsonl`.

This is the spec’s event-history replay path.

#### Path 2: Recall intents to agents

- for richer agent-specific history, the orchestrator dispatches a recall intent,
- the target agent queries its own `store/data/`,
- the agent returns structured history such as edits made, sources evaluated, or decisions taken.

This is the spec’s semantic/domain-specific recall path.

### 19.4 How Task Memories Enter the Shared Retrieval Store

The current flow is:

- task completes,
- orchestrator summarizes the completed work,
- writes `memory/tasks/<task_id>.md`,
- embeds and indexes that summary in Qdrant,
- later user queries can retrieve it through the normal hybrid search path.

The task summary includes:

- the original request,
- agents used,
- the final result,
- and produced artifacts.

If produced artifacts are large:

- the summary should store compact artifact references,
- the full body should live under `runs/artifacts/`,
- and a retrievable `artifact_pointer` memory should be created in `memory/artifact_pointers/`.

## 20. Event-Based Task History and Archival

The architecture also treats task event history as a recoverable memory-like record.

### 20.1 Redis Event Stream

- Task events live in `streams:events`
- The stream is trimmed to `EVENTS_STREAM_MAXLEN`
- Per-task Redis lists (`task_events:{task_id}`) provide fast replay of one task’s events

### 20.2 Disk Archive

To avoid losing old task history when Redis trims the stream:

- completed task events are archived to `logs/events/<task_id>.jsonl`
- archive happens after terminal task events
- recent tasks can be replayed from Redis
- older tasks fall back to the disk archive
- the per-task Redis index `task_events:{task_id}` lives for `RESULT_TTL_SEC` which the spec sets to 7 days in the quick-reference tables

This is part of the current task-memory and observability model.

## 21. Agent Notes Sync

The architecture expects the Gateway/session layer to sync private agent learnings into shared memory.

Architecture sync behavior:

- after each task completion,
- check whether the agent’s `learnings.md` file changed,
- compare by file hash,
- if changed:
  - copy it into `memory/agent_notes/`
  - re-index it in Qdrant

This sync is **read-only** from the Gateway/session layer’s perspective:

- the agent remains the owner of its own `store/learnings.md`
- the shared memory copy is a synchronized retrieval projection

## 22. Agent Access to Shared Memory

Every agent gets universal memory tools injected at runtime:

- `MemoryRead`
- `MemoryWrite`

These are separate from `store/learnings.md`.

The current spec also says:

- universal tools cannot be opted out of,
- they are injected by the runtime,
- they do not appear in `agent_card.yaml`,
- and `MemoryWrite` is append-only from the agent’s perspective.

### 22.1 MemoryRead

Purpose:

- search the shared memory store
- retrieve memories beyond the agent’s own private learnings

Current behavior:

- agent calls `/internal/memory/search` on the Gateway
- Gateway proxies to the internal `cosmic-memory` service on the same VM
- results include:
  - `memory_id`
  - `kind`
  - `title`
  - `content`
  - provenance / metadata
  - `relevance_score`

It supports filtering by memory type:

- `core_fact`
- `agent_note`
- `session_summary`
- `task_summary`
- `user_data`
- `transcript`
- `artifact_pointer`

### 22.2 MemoryWrite

Purpose:

- write new shared memory through the Gateway
- persist facts or learnings that should be visible system-wide

Current behavior:

- agent calls `/internal/memory/write`
- for deterministic profile facts, agents or system flows can call `/internal/memory/core-facts`
- for canonical turn ingest, the Gateway can call `/internal/memory/episodes`
- for deterministic session-state revisit, internal consumers can also use:
  - `/internal/session/state/{session_id}`
  - `/internal/session/turns/{session_id}`
  - `/internal/session/task-notebook/{task_id}`
  - `/internal/session/revisit`
- Gateway enforces:
  - per-agent rate limiting
  - deduplication using a content hash
- Gateway generates the `memory_id`
- Gateway proxies the write to the internal `cosmic-memory` service

Operational details from the spec:

- rate limiting is tracked with `memory_write_rate:{agent_id}`
- the counter resets after `3600s` (1 hour)
- content dedup uses `memory_write_dedup:{agent_id}:{hash}`
- the dedup key TTL is `86400s` (24 hours)
- the first writer wins and stores the real `memory_id`
- duplicate writes within the dedup window return the previously claimed `memory_id`

The current write tool supports:

- `core_fact`
- `agent_note`
- `task_summary`
- `user_data`
- `artifact_pointer`

The transcript ingest path also writes:

- `transcript` episode memories through `/internal/memory/episodes`

Current hard constraint:

- agents can add shared memories through `MemoryWrite`,
- but they cannot modify or delete existing shared memories through that tool,
- and cleanup is a Gateway / memory-service responsibility rather than an agent capability.

### 22.3 Memory Store Location

The architecture says:

- the shared long-term memory store is owned by the internal `cosmic-memory` service on the same VM
- Gateway is still the single integration surface for the rest of COSMIC
- agents cannot import Gateway modules directly
- therefore agents access memory through Gateway internal HTTP APIs, and Gateway proxies to `cosmic-memory` when enabled

## 23. Configuration

The current memory architecture is configured in two layers.

### 23.1 Gateway / Session-Layer Memory Integration

```ini
SESSION_RESET_HOUR=4
COMPACTION_THRESHOLD=0.70
COMPACTION_MODEL=claude-haiku-4-5
COSMIC_MEMORY_URL=http://127.0.0.1:8090
COSMIC_MEMORY_TIMEOUT_SEC=12
COSMIC_MEMORY_CORE_FACT_MAX_CHARS=1500
COSMIC_MEMORY_PASSIVE_MAX_RESULTS=8
COSMIC_MEMORY_PASSIVE_TOKEN_BUDGET=12000
COSMIC_MEMORY_PASSIVE_KINDS=session_summary,task_summary,agent_note,user_data,transcript
COSMIC_MEMORY_INGEST_TRANSCRIPTS=true
COSMIC_MEMORY_EPISODE_EXTRACT_GRAPH=false
SESSION_TRANSCRIPT_PATH=./logs/sessions
```

### 23.2 `cosmic-memory` Service Configuration

```ini
PERPLEXITY_API_KEY=<secret>
XAI_API_KEY=<secret>
GATEWAY_INTERNAL_TOKEN=<secret>
COSMIC_MEMORY_EMBEDDING_MODEL=pplx-embed-v1-4b
COSMIC_MEMORY_EMBEDDING_DIMENSIONS=1024
COSMIC_MEMORY_EMBED_BATCH_SIZE=128
COSMIC_MEMORY_EMBED_MAX_PARALLEL=4
COSMIC_MEMORY_EMBED_ENCODING=base64_int8
COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED=false
COSMIC_MEMORY_GRAPH_EXTRACT_MODEL=grok-4-1-fast-reasoning
COSMIC_MEMORY_GRAPH_EXTRACT_MAX_PARALLEL=2
COSMIC_MEMORY_GRAPH_EXTRACT_MAX_RETRIES=3
COSMIC_MEMORY_GRAPH_EXTRACT_RETRY_BASE_SECONDS=1.0
COSMIC_MEMORY_GRAPH_EXTRACT_RETRY_MAX_SECONDS=12.0
COSMIC_MEMORY_TIMEZONE=America/Chicago
```

### 23.3 Provisioning Rule

`cosmic-memory` is provisioned only when VM bootstrap is run with:

```bash
python3 bootstrap.py provision-vm --memory-repo-dir <path-to-cosmic-memory-clone>
```

With that flag, bootstrap:

- installs the `cosmic-memory` package,
- writes `/etc/cosmic/memory.env`,
- installs `cosmic-memory.service`,
- injects `COSMIC_MEMORY_URL` into `gateway.env`,
- and enables the same-VM internal HTTP path between Gateway and `cosmic-memory`.

Without `--memory-repo-dir`, memory stays intentionally disabled and Gateway continues in degraded no-memory mode.

Additional current rules:

- Gateway and `cosmic-memory` must share the same `GATEWAY_INTERNAL_TOKEN`.
- `COMPACTION_MODEL` remains a Gateway/session-layer concern.
- Embedding, index sync, and graph extraction settings belong to the `cosmic-memory` service.

## 24. Current Hard Rules

The current memory-related hard rules in the spec are:

1. All LLM backends receive the same assembled context.
2. Memories are never compacted.
3. Each memory has a unique `memory_id`.
4. Task execution is isolated from the main session.
5. Agent notes have priority over user data in ranking.
6. `.md` files are the source of truth and Qdrant is only the index.
7. `logs/sessions/*.md` is a derived append-only transcript archive, not a second writable source of truth.
8. Compaction uses a cheap model, never Opus.
9. Daily reset is transparent to the user because a carry-forward packet preserves active continuity.
10. Conversational replies and async task input use separate mechanisms.
11. `awaiting_reply` is cleared on first use.
12. Raw model thinking is not part of canonical compaction state.
13. Raw tool chatter is not canonical session memory; normalized turn/task ledgers are.
14. The active working set is bounded session state, not long-term memory.
15. Large extracted bodies should spill into artifacts plus `artifact_pointer` memories, not inline prompt memory.
16. Deterministic revisit should be preferred over guessing when exact prior context is needed.

## 25. Bottom-Line Summary of the Current Design

The current COSMIC memory architecture is a **Gateway-integrated, service-backed hybrid memory system** built around:

- one shared daily cross-channel session in SQLite,
- a derived append-only daily transcript archive under `logs/sessions/`,
- continuous pruning plus structured triggered compaction,
- a forced 4 AM rollover that converts the previous day into retrievable memory,
- a carry-forward packet that preserves continuity across daily resets,
- an active working set that preserves what is currently in flight,
- turn-ledger and task-notebook structures that preserve outcomes without raw execution noise,
- large-artifact spillover with compact pointer memories and on-demand revisit,
- `.md` files as the canonical long-term memory store,
- Qdrant as a rebuildable hybrid dense+sparse index,
- isolated task memory outside the main conversation,
- agent-private learnings synchronized into shared memory,
- internal HTTP memory tools for agent access,
- deterministic revisit for exact history recovery,
- and strict separation between conversational context, retrievable memory, and task execution history.
