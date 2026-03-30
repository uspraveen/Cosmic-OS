# Tabular agent interoperability (Redis, registry, task envelopes)

This document records how `cosmic/tabular-agent:1.0.0` fits into COSMIC’s existing plumbing so you can verify a deployment.

## Agent identity (must match everywhere)

| Location | Value |
|----------|--------|
| `agent_card.yaml` → `agent_id` | `cosmic/tabular-agent:1.0.0` |
| `agent_card.yaml` → `stream_key` | `streams:cosmic/tabular-agent:1.0.0` |
| Gateway `GATEWAY_TABULAR_AGENT_ID` (default in `gateway/config.py`) | `cosmic/tabular-agent:1.0.0` |
| Orchestrator `sheets_*` tools (`executor.py`) | `agent_id="cosmic/tabular-agent:1.0.0"` (includes `sheets_reason` → `tabular.reason_workbook`, `sheets_create_workbook` → `tabular.create_workbook`) |

If any of these drift, gateway parse dispatch or orchestrator delegation will target the wrong recipient or fail discovery.

## Registry (SQLite + Redis intent index)

On startup, `AgentRuntime.register()` (see `shared/agent_runtime.py`):

1. **`RegistryStore.upsert_agent_card`** — writes the YAML card into `Backend/registry/registry.db` (same DB the orchestrator uses for `list_agents_for_intent`).
2. **`register_intent_index`** — publishes intent → agent mappings in **Redis** so `find_available_instance` / `find_available_instance_for_agent` can resolve healthy workers.

The orchestrator’s `dispatch_agent_task` calls `_find_available_agent(intent, preferred_agent_id=…)` which:

- Reads **registry DB** for who advertises `tabular.*` intents.
- Uses **Redis** to pick a **healthy instance** (heartbeat from the agent loop).

**Requirement:** Orchestrator and tabular agent must share the same **`REDIS_URL`** and the same **registry DB path** (default `Backend/registry/registry.db`) in a typical single-host dev setup.

## Task envelopes

Orchestrator builds a child `TaskEnvelope` with:

- `sender` = orchestrator agent id (`cosmic/orchestrator:1.0.0`)
- `recipient` = resolved agent id (`cosmic/tabular-agent:1.0.0`)
- `intent` = e.g. `tabular.query_workbook`
- `input` = tool payload (bundle_id, artifact_id, sql, …)
- `signature` = HMAC via `sign_task_envelope` using the recipient’s agent secret

The tabular worker verifies inbound tasks with `verify_task_envelope` / shared runtime (see `shared/agent_runtime.py`). **`AGENT_SECRET`** must match what the orchestrator uses to sign for `cosmic/tabular-agent:1.0.0`.

## Redis streams

- Tasks are dispatched with **`dispatch_task`** → Redis streams named from `task_stream_name(agent_id, priority)` (high / normal / low).
- The tabular agent **creates consumer groups** for those streams on `register()`.

**Requirement:** Tabular agent process is running, connected to Redis, and heartbeating so it is considered “healthy.”

## Gateway vs orchestrator

- **Gateway** dispatches `tabular.parse_bundle` directly to the tabular agent for uploaded spreadsheets (same agent id config).
- **Orchestrator** dispatches other tabular intents via `sheets_*` tools when Opus runs tools (same `TaskEnvelope` path as above), including new-workbook creation through `sheets_create_workbook`.

Both paths assume the tabular worker is registered and healthy.

## Quick manual checklist

1. Redis up; same `REDIS_URL` for gateway, orchestrator, tabular agent.
2. `registry/registry.db` writable; start tabular agent once so the card is upserted.
3. `AGENT_SECRET` for tabular agent matches signing secret configured for that agent id.
4. `python -m agents.tabular_agent` (or your process manager) shows healthy heartbeat.
5. `tabular.reason_workbook` returns **`failed`** (not `completed`) when internal reasoning is unavailable (e.g. MiMo disabled); check `AgentResult.status` and `error.code`.
6. `tabular.create_workbook` returns both:
   - a reusable bundle summary (`bundle_id`, `workbooks[]`, internal `artifact_id`)
   - a downloadable `.xlsx` output artifact under `runs/artifacts/<task_id>/parsed/<artifact_id>/generated/...`

6. Optional: MiMo API smoke test (no Redis):
   - From **`Backend/`:** `python scripts/local_test_mimo_langchain.py`
   - From **repo root (`Cosmic-OS/`):** `python scripts/local_test_mimo_langchain.py` (wrapper runs the same file under `Backend/scripts/`)
   - Full path example: `python "C:\Users\...\Cosmic-OS\Backend\scripts\local_test_mimo_langchain.py"`

## Mid-task clarification (`clarify` action, LangGraph)

When internal reasoning hits **blocking ambiguity** (e.g. multiple plausible sheets, unclear metric), the planner may emit action **`clarify`** with a **`question`** and optional **`options`**. This **must** use COSMIC’s existing **orchestrator task-input relay**, not conversational sticky routing:

| Mechanism | Role |
|-----------|------|
| `TaskEnvelope.parent_task_id` | **Orchestrator active task id** — required for `clarify` (child tasks dispatched from `sheets_reason` / delegate include this). |
| `POST {ORCHESTRATOR}/internal/tasks/{parent_task_id}/request-input` | Specialist calls with `X-Internal-Token` (`TABULAR_AGENT_ORCHESTRATOR_INTERNAL_TOKEN` or `ORCHESTRATOR_INTERNAL_TOKEN`). Publishes to **`user_input:requests`** and returns an `input_request_id`. |
| `task.suspended` | Emitted on the original `tabular.reason_workbook` child task id when clarification is required. |
| `agent.resume` + `task.resumed` | After the user reply reaches **`user_input:replies`**, the orchestrator dispatches a new child task with `intent="agent.resume"` and the original input + reply payload. `AgentRuntime` inflates that back into the original specialist intent and the resumed invocation emits `task.resumed`. |

Configure **`TABULAR_AGENT_ORCHESTRATOR_URL`** (default `http://127.0.0.1:8743`) so the tabular worker can reach the orchestrator. **Do not** add a second clarification protocol or reuse conversational `<awaiting_reply/>` for this path.

The orchestrator now records these non-terminal child-task events in its task ledger and keeps the parent wait alive:
- `task.suspended` → task status becomes `suspended`
- `task.resumed` → task status returns to `running`

### v1 actual behavior

The specialist publishes the clarification request, emits `task.suspended`, and returns `TaskInProgress`. The original worker invocation ends. After the user reply arrives, the orchestrator creates a new child task with `intent="agent.resume"` and aliased provenance/future state. `shared/agent_runtime.py` unwraps that into the original specialist intent with a `_resume` payload, so the tabular graph continues in a second invocation without inventing a parallel channel.

`tabular.reason_workbook` also uses COSMIC’s injected **StepPlan** per invocation. The initial child task creates a flat plan for `inspect -> analyze -> summarize`; if clarification suspends that task, the incomplete plan remains attached to that task only. The resumed child starts a **fresh** StepPlan (`resume -> analyze -> summarize`) because StepPlan is **per-task**, not carried across tasks.

### Clarify finish reasons (explicit)

| `clarify_status` | Meaning | What the planner sees next |
|---|---|---|
| `suspended` | Clarification request was published successfully; worker returns `TaskInProgress` and waits for orchestrator resume | Orchestrator resumes the graph in a second invocation |
| `relay_error` | HTTP call to orchestrator failed before request publication | Planner should `done` with honest failure |
| `missing_parent_task` | No `parent_task_id` on TaskEnvelope | Error; logged but no orchestrator call made |
| `missing_question` | Planner omitted `question` field | Validation error; round still counts |
| `clarify_already_used` | Second `clarify` in same run | Blocked; routes to `finalize` with `finish_reason=clarify_repeat` |

### Provenance in events

Both `task.suspended` and `task.resumed` payloads include: `child_task_id`, `session_id`, `request_id`, `channel`, `source`, `source_id`, `parent_task_id`. Resumed events also include the resumed child task id and `input_request_id`.

## Mid-task sibling delegation (`delegate` action, reverse task)

When internal workbook tools are insufficient and the tabular specialist needs another capability, it uses **orchestrator-mediated reverse delegation** instead of direct agent-to-agent calls.

| Mechanism | Role |
|---|---|
| `TaskEnvelope.parent_task_id` | Points to the currently running `tabular.reason_workbook` child task that will suspend and later resume. |
| `POST {ORCHESTRATOR}/internal/reverse-tasks` | Shared runtime endpoint for signed reverse tasks. Tabular submits `intent="orchestrator.delegate"` here. |
| `orchestrator.delegate` input | Includes `target_intent`, `target_input`, optional `target_agent_id`, and `resume_payload`. |
| `reverse_task_waits` | Durable orchestrator ledger table that records the waiting specialist task, delegated target, and eventual resumed task. |
| `task.suspended` | Emitted on the original tabular child task after the reverse task is registered. |
| `agent.resume` + `task.resumed` | After the sibling specialist completes or fails, the orchestrator resumes tabular with `reverse_task` metadata and `reverse_result`. |

### Recommended tabular policy

- Use `delegate` only when the workbook genuinely lacks the required information or capability.
- Prefer sibling **intents** over hardcoded sibling agent ids.
- Good default targets for external lookup are `firecrawl.scrape` and `firecrawl.extract`.
- Keep delegation **bounded**. Current tabular policy allows at most one delegation per run.

### v1 actual behavior

1. Tabular planner emits action `delegate`.
2. `tabular_reason_graph.py` calls `agent.request_orchestrator_delegate(...)` via the shared runtime.
3. The orchestrator registers the reverse task and durable wait first.
4. Tabular emits `task.suspended` and returns `TaskInProgress`.
5. Only then does the orchestrator dispatch the sibling specialist task.
6. When the sibling result is ready, the orchestrator sends `agent.resume` to tabular.
7. `shared/agent_runtime.py` inflates that back into `tabular.reason_workbook` with `_resume.reverse_task` and `_resume.reverse_result`.
8. Tabular appends the delegated result into its transcript and continues the original reasoning flow.

This means tabular does **not** need registry awareness. It only needs to know when to ask the orchestrator for sibling help.

## Code sandbox (execution policy)

The tabular specialist runs user-generated Python scripts under COSMIC-owned execution control. This is **not** a kernel-level container — it is a Python-level sandbox with explicit policies.

### Filesystem

An injected prelude patches `open`/`io.open`/`os.*`/`shutil.*` so all resolved paths must stay under `COSMIC_TABULAR_BUNDLE_ROOT` (the bundle root). Best-effort; native extensions may bypass. Scripts are persisted under `codes/<execution_id>.py`; execution receipts under `executions/<execution_id>.json`.

### Network

| `TABULAR_AGENT_SANDBOX_ALLOW_NETWORK` | Behavior |
|---|---|
| `false` (default) | Regex denylist blocks `requests`, `httpx`, `urllib`, `socket` in user code |
| `true` | Network-related deny patterns are removed; receipt logs `network_enabled=true` |

Core denylists (`subprocess`, `os`, `sys`, `ctypes`, `multiprocessing`, `eval`, `exec`, etc.) remain active regardless of network policy.

### Package installation

| `TABULAR_AGENT_SANDBOX_ALLOW_PIP` | Behavior |
|---|---|
| `false` (default) | Planner `pip_install` field is ignored; receipt logs `pip_log.skipped=true` |
| `true` | Per-execution venv is created under `TABULAR_AGENT_SANDBOX_VENV_CACHE_ROOT` (or temp dir); requested packages are installed with `pip install --no-input --disable-pip-version-check`; script runs using the venv python |

- Package names are validated (alphanumeric, max 12 per execution, deny list for `subprocess`/`os`/`sys`/`pip`/`setuptools`).
- Venvs are **cached by package set hash** — identical package lists reuse the same venv.
- Pip output and exit code are logged in the execution receipt under `pip_log`.
- Timeout: `TABULAR_AGENT_SANDBOX_PIP_TIMEOUT_SEC` (default 120s).

### Execution receipt contract

Every `executions/<execution_id>.json` contains:

```json
{
  "execution_id": "exec_...",
  "task_id": "...",
  "session_id": "...",
  "artifact_id": "...",
  "kind": "tabular_sandbox",
  "parent_task_id": "... or null",
  "network_enabled": false,
  "packages_installed": [],
  "pip_log": null,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "duration_ms": 123,
  "script_relative": "codes/exec_....py"
}
```

### What this is NOT

- **Not container isolation.** Native extensions (DuckDB, PyArrow) may open files via non-Python APIs. Defense: pass only relative paths under the bundle to those libraries.
- **Not a hosted code execution API.** Execution is COSMIC-owned, per-task scoped, with the specialist as the owner of what code runs.
- **Not ambient global pollution.** Pip installs go into isolated venvs, not the system interpreter.
- **Not host-environment inheritance.** Execution and pip install run with a minimized environment rooted under the bundle/venv (`.sandbox_home`) instead of inheriting the full user shell environment.
