# Alpha Agent Plan

Yes, I agree with your direction. Alpha should expose **high-level intents**, not granular CLI verbs.

The clean shape is:

```yaml
intents:
  - alpha.execute
  - alpha.resume_session
  - alpha.recall_project
```

Maybe even just `alpha.execute` + `alpha.recall_project` at first. The payload should carry the shape of the work, not the exact command:

```json
{
  "goal": "Build and host a website for my portfolio",
  "project_ref": null,
  "mode": "new_project | existing_project | auto",
  "constraints": {},
  "preferred_harness": "codex | opencode | cursor | auto",
  "deliverables": ["deployed_url", "repo", "summary"],
  "approval_policy": "container_autonomous"
}
```

The orchestrator should decide **when Alpha is needed**, but Alpha should decide **how to execute the project**.

## Core Thought

Alpha should be a COSMIC specialist at the boundary, but a project operator inside.

So externally it follows COSMIC:

- `agent_card.yaml`
- `TaskEnvelope`
- `EventEnvelope`
- `task.progress`
- `artifact.added`
- `task.suspended`
- `task.completed`
- `task.failed`
- `store/`
- `runtime/`
- `runs/artifacts/<task_id>/`

Internally it can run Codex/OpenCode/Cursor CLI inside an isolated workspace.

The agent directory should look like a normal COSMIC agent:

```text
Backend/agents/alpha_agent/
  agent_card.yaml
  agent.py
  config.py
  schemas/intents/alpha.execute.input.json
  schemas/intents/alpha.execute.output.json
  harnesses/
    codex_harness.py
    opencode_harness.py
    cursor_harness.py
  workspace_manager.py
  project_registry.py
  orchestrator_bridge.py
  prompts/
    system.md
  store/
    projects.db
  runtime/
  tests/
```

But actual project work should live outside the agent source tree:

```text
/var/lib/cosmic/alpha/
  workspaces/
    prj_xxx/
  homes/
    codex/
    opencode/
    cursor/
  artifacts/
    tsk_xxx/
  deployments/
    app_xxx/
  caches/
```

## Container

For now, yes: make container isolation the main boundary.

I would run the Alpha service as a normal COSMIC agent, but each Alpha project task runs inside a dedicated container/workspace. That means inside the container you can allow much more freedom. This matches Codex's own guidance: `--dangerously-bypass-approvals-and-sandbox` is only appropriate inside an isolated runner, while normal unattended work should use `--sandbox workspace-write` when possible. Codex also supports `codex exec --json`, `--output-last-message`, `--output-schema`, `--cd`, `--sandbox`, and resumable non-interactive sessions, which maps well to COSMIC event streaming and resume flow.

Important: don't give the Alpha task container raw host Docker socket access by default. For hosting/deployment, Alpha should call a narrow "deployment broker" on the host: create service, allocate port, register Caddy route, restart service. Otherwise Docker socket equals host root.

## Back And Forth With Orchestrator

You are right: Alpha should treat the orchestrator like the human.

Practically, Alpha needs an `orchestrator_bridge` with tools like:

```text
ask_orchestrator(question, choices?, context?)
report_progress(summary, step_status?)
publish_artifact(path, kind, audience)
request_specialist(intent, payload, artifacts?)
checkpoint(state)
```

The existing `AgentRuntime` already has useful pieces: reverse tasks to orchestrator, `agent.resume`, deferred tasks, progress events, artifacts, and request-input routes. Alpha should use that instead of inventing a side channel.

Best implementation: expose a tiny COSMIC MCP server or local tool bridge to Codex/OpenCode/Cursor. Then the underlying CLI agent can literally call `cosmic.ask_orchestrator` when it needs clarification. If MCP is too much for v1, the wrapper can ask the CLI to emit structured JSON directives and translate those into COSMIC events.

## Codex / OpenCode / Cursor Findings

Codex is the best first harness. It has non-interactive `codex exec`, JSONL streaming, structured output schemas, resumable sessions, sandbox flags, project `AGENTS.md`, and configurable `~/.codex/config.toml` / `$CODEX_HOME`. That is exactly what Alpha needs.

OpenCode is useful as a second harness because it is model-provider flexible. Its docs show provider auth stored under `~/.local/share/opencode/auth.json`, config via `opencode.json/jsonc`, `OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG`, and MCP support. It can also run an ACP server, which may become useful if COSMIC wants a persistent agent protocol.

Cursor CLI is useful, but I'd treat it as optional/harness #3. It supports headless `cursor-agent -p`, `--force` for file modification, `--output-format stream-json/json/text`, browser login, and `CURSOR_API_KEY` for automation. That is viable, but Codex looks more directly automation-friendly for COSMIC.

OpenClaw's design gives us one very important lesson: separate **model route**, **runtime harness**, **auth**, and **channel**. OpenClaw has separate `openai/*`, `openai-codex/*`, and Codex-harness routes, and it keeps auth in a token sink to avoid Codex CLI/OpenClaw refresh-token conflicts. COSMIC should copy that principle. Don't mix "using GPT model," "using Codex subscription auth," and "using Codex app-server/runtime" into one concept.

## Keep CLI App Structures Untouched

Yes. Don't pollute project repos with Codex/OpenCode/Cursor internal state.

Use isolated homes:

```bash
HOME=/var/lib/cosmic/alpha/homes/codex
CODEX_HOME=/var/lib/cosmic/alpha/homes/codex

HOME=/var/lib/cosmic/alpha/homes/opencode
OPENCODE_CONFIG_DIR=/var/lib/cosmic/alpha/homes/opencode/config
XDG_DATA_HOME=/var/lib/cosmic/alpha/homes/opencode/share

HOME=/var/lib/cosmic/alpha/homes/cursor
CURSOR_API_KEY=...
```

Only write repo-native instruction files like `AGENTS.md`, `.codex/config.toml`, `opencode.json`, or Cursor rules when the project actually owns them or the user asked to create a managed project. For arbitrary cloned repos, pass instructions from outside through the harness prompt.

## Existing Vs New Project

Add an Alpha project registry. This is necessary.

Each Alpha project should have:

```text
project_id
aliases
repo_url
local_path
deployment_url
last_task_id
last_session_id
harness_thread_ids
status
created_at
updated_at
summary
artifact_ids
```

Resolution flow:

1. If user mentions a repo URL, deployment URL, project name, artifact, or path, lookup registry.
2. If the current chat has an active Alpha project and the user says "update it," "fix the site," "continue," use session stickiness.
3. If multiple projects match, orchestrator asks a clarification before Alpha starts.
4. If no match and the task says build/clone/create/train/deploy, create a new project.
5. Alpha can still ask back mid-task if it detects ambiguity.

So yes: high-level Alpha, COSMIC-native envelope boundary, containerized project runtime, Codex first, OpenCode second, Cursor optional, CLI state kept outside project repos, and a project registry so the orchestrator can reason about continuity.

Sources checked: OpenAI Codex CLI/non-interactive/sandbox docs, OpenCode docs, Cursor CLI docs, and OpenClaw provider/Codex-harness/OAuth docs.

## Implementation Decision For V1

The first implementation should be additive and conservative because COSMIC-OS is already in production.

Alpha V1 should add the agent boundary, project registry, workspace manager, and Docker workspace runner without enabling full Codex execution yet. The Alpha service can be installed by bootstrap on new VMs, but it should only be enabled and started when `ALPHA_AGENT_ENABLED=true`.

This prevents a partially implemented Alpha agent from registering in the live specialist catalog before the Codex harness, orchestrator prompts, and approval UX are ready.

### V1 Scope

Implement through step 4:

1. Update this plan into a working implementation spec.
2. Scaffold `Backend/agents/alpha_agent` as a normal COSMIC specialist.
3. Implement the Alpha project registry and workspace manager.
4. Implement the Docker workspace runner.

Do not yet implement full Codex/OpenCode/Cursor task execution. The first `alpha.execute` handler should prepare or recall the project workspace and return a safe structured result showing the workspace, project registry entry, and Docker runner readiness.

### Activation Policy

Bootstrap behavior:

- install `alpha-agent.env`
- install `cosmic-alpha-agent.service`
- do not enable/start the service unless `ALPHA_AGENT_ENABLED=true`

Agent behavior:

- register only when the systemd service is enabled and running
- do not execute arbitrary user goals yet
- do not mount the host Docker socket into Alpha task containers
- do not mutate deployed services or Caddy routes in V1

### Final Runtime Layering

The final Alpha execution path remains:

```text
COSMIC Orchestrator
  -> Alpha Agent service on the VM
    -> per-project/per-task Docker container
      -> codex exec --json --cd /workspace --sandbox workspace-write ...
```

The container is the real host isolation boundary. Codex sandbox flags are the inner execution policy.

## V2 Codex Wiring Update

The Codex execution path is now implemented behind `alpha.execute`.

Current flow:

```text
COSMIC Orchestrator
  -> delegate_to_agent(alpha.execute)
    -> resolve artifact_ids / inherit current TaskEnvelope.input_artifacts
    -> expand parsed document bundle paths from parsed_summary.paths for Alpha
    -> Alpha project registry
    -> /var/lib/cosmic/alpha/workspaces/prj_xxx
    -> stage input artifacts into <workspace>/_cosmic_inputs
    -> Gateway internal Codex status check
    -> codex exec --cd <workspace> --sandbox <ALPHA_CODEX_SANDBOX>
    -> /var/lib/cosmic/alpha/artifacts/tsk_xxx/codex-last-message.md
    -> task.completed / task.failed
```

Alpha handoffs should pass bulky context by file reference, not by pasted text. When the orchestrator has uploaded artifacts or parsed document metadata, `delegate_to_agent(alpha.execute)` should pass `artifact_ids` or `input_artifacts`. For parsed docs, the handoff expands `parsed_summary.paths` into concrete artifacts such as `document.md`, `chunk_index.json`, `document.json`, and `manifest.json`; the Alpha agent then copies those files into `_cosmic_inputs` and gives the CLI absolute staged paths. A bare `bundle_id` is useful metadata, but it is not sufficient by itself unless the corresponding source artifact reference is also passed.

Runtime finding on the current VM: Codex `workspace-write` fails because the VM rejects the bubblewrap loopback setup with `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. `danger-full-access` works in the Alpha workspace. Docker is also not installed on the current VM, so V2 uses:

```env
ALPHA_CODEX_SANDBOX=danger-full-access
```

This is an explicit operational compromise, not the final isolation design. The service still confines project state to `/var/lib/cosmic/alpha` by convention and prompt policy, but host-level sandboxing is not complete until the container/LXD runner is added. The next hardening step is to install/build an Alpha task container image with Codex available inside it and run Codex with full freedom only inside that container.

## Provider Retry Policy

Alpha must not silently switch execution providers. If the selected/configured harness is Cursor, Cursor should be retried first in the same workspace with the previous failure context; if the selected harness is Codex, Codex should be retried first the same way. This preserves user intent and avoids assuming the user has another provider authenticated.

Cross-provider continuation is allowed only when the orchestrator explicitly passes `allow_cross_harness_fallback=true` and the alternate provider is authenticated. The default is same-provider retry, then a clear failure report with terminal logs, return code, timeout/cancel state, artifacts, and retry guidance.

Known recoverable example: a CLI may terminate itself with `SIGTERM` by running an over-broad command such as `pkill -f 'python.*http.server'` from a shell whose own command line contains that pattern. Alpha should classify this as a same-provider retry case and feed the CLI targeted guidance to use a non-self-matching pattern or port-specific cleanup.

## Future Improvement: Intelligent Alpha Project Lookup

Alpha project recall should evolve beyond exact `project_ref` matching. The current registry is good for precise continuity when the orchestrator passes a project id, repo URL, deployment URL, local path, task id, artifact id, or exact alias, but it is not enough when many projects have similar names or goals.

The future lookup path should be layered:

1. Exact match first:
   - `project_id`
   - repo URL
   - deployment URL
   - local path
   - task id
   - artifact id
   - exact alias

2. BM25 keyword search:
   - project aliases
   - project summaries
   - original and recent task goals
   - task logs / last messages
   - repo names
   - deployment URLs and domains
   - produced artifact names

3. Vector similarity search:
   - project summaries
   - task summaries
   - session summaries
   - generated reports
   - compact Alpha terminal/log summaries

4. Combined candidate scoring:
   - exact match strength
   - BM25 score
   - vector similarity
   - recency
   - current session affinity
   - repo/deployment/artifact linkage

5. Ambiguity handling:
   - do not silently choose when top candidates are close
   - return `ambiguous=true`
   - include the top 3-5 candidates with scores and why they matched
   - let the orchestrator resolve from conversation context or ask the user

6. Project graph traversal:
   - project -> tasks
   - tasks -> artifacts
   - artifacts -> deployment URLs
   - project -> repo URL / local path
   - project -> session ids
   - tasks -> specialist receipts

COSMIC should own this durable Alpha memory/index. Codex and Cursor should remain execution engines, not the source of truth for project recall. Their outputs, terminal summaries, produced files, repo/deployment URLs, and task reports should be normalized into Alpha's registry/search store so lookup works consistently across both harnesses.
