# Agent Card Schema (agent_card.yaml)

The Agent Card is the machine-readable capability declaration registered at startup.
The orchestrator reads it to populate the registry and make dispatch decisions.

## Complete Template

```yaml
# agents/<agent_name>/agent_card.yaml

agent_id: cosmic/<name>-agent:<semver>
display_name: <Human Readable Name>
description: >
  <One paragraph describing what this agent does, its specialization,
  and the types of tasks it handles.>

intents:
  - name: <domain>.<action>
    description: <What this intent does>
    input_schema: schemas/intents/<domain>.<action>.input.json
    output_schema: schemas/intents/<domain>.<action>.output.json
    timeout_sec: <seconds>            # Max execution time for this intent

  - name: <domain>.recall_session
    description: Recall what happened in a previous session for this agent
    input_schema: schemas/intents/<domain>.recall_session.input.json
    output_schema: schemas/intents/<domain>.recall_session.output.json
    timeout_sec: 30

  # Add this intent when users may name provider-owned resources instead of
  # passing stable IDs directly. Search exactly ONE account per task — the
  # orchestrator chooses the account and passes a single input.auth dict.
  - name: <domain>.resolve_resource
    description: Search for a resource by name within one connected account
    input_schema: schemas/intents/<domain>.resolve_resource.input.json
    output_schema: schemas/intents/<domain>.resolve_resource.output.json
    timeout_sec: 30

# Only declare auth_requirements for intents that call external provider APIs
# on behalf of the user. Intents using agent-local tools or store/ do NOT declare them.
auth_requirements:
  <domain>.<action>:
    provider: <provider>              # 'google', 'github', 'slack', etc.
    scopes:
      - <oauth_scope_url>

artifact_types:
  - <mime_category>                   # 'web_page', 'pdf', 'image', 'citation_pack', etc.

policies:
  network_access: <true|false>
  writable_paths:
    - runs/artifacts                   # root for per-task dirs: runs/artifacts/<task_id>/
    - agents/<agent_name>/store
    - agents/<agent_name>/runtime
  tool_access:
    - <tool_1>                        # e.g., 'web_search', 'web_fetch', 'file_write'
    - <tool_2>                        # e.g., 'playwright_navigate', 'shell_execute'
    # NOTE: Universal tools (StepPlan, MemoryRead, MemoryWrite) are NOT listed here
  allowed_senders:
    - cosmic/orchestrator:1.0.0       # Always include this
  intent_authorization:
    <domain>.<action>: [cosmic/orchestrator:1.0.0]

sla:
  max_concurrency: <N>                # How many tasks this agent runs in parallel
  heartbeat_interval_sec: 10          # How often to send heartbeat
  heartbeat_ttl_sec: 30               # If no heartbeat for this long, marked dead
  max_task_duration_sec: <seconds>    # Longest possible task — used for XAUTOCLAIM tuning
  health_endpoint: /health
  retry_policy:
    max_attempts: 3
    backoff: exponential
    backoff_base_sec: 2               # 2s, 4s, 8s
    backoff_max_sec: 60
    retryable_codes:
      - TIMEOUT
      - NETWORK_ERROR
      - RATE_LIMITED
    non_retryable_codes:
      - INVALID_INPUT
      - AUTH_ERROR
      - SCHEMA_VIOLATION
      - INTERNAL_ERROR

stream_key: streams:cosmic/<name>-agent:<semver>

version_info:
  semver: <version>
  released_at: <date>
  deprecated_at: null
  remove_after: null
  changelog: CHANGELOG.md
```

## Field Reference

### Top-Level Fields

| Field | Required | Description |
|---|---|---|
| `agent_id` | Yes | `cosmic/<name>-agent:<semver>`. Machine identifier — never changes after creation. |
| `display_name` | Yes | Human-readable label for UI and docs. Can be rebranded freely. |
| `description` | Yes | One paragraph. Used by orchestrator for capability understanding. |
| `intents` | Yes | List of intent declarations. |
| `auth_requirements` | No | Per-intent OAuth credential requirements. |
| `artifact_types` | No | MIME categories this agent can produce. |
| `policies` | Yes | Security and access policies. |
| `sla` | Yes | Performance and reliability contract. |
| `stream_key` | Yes | Redis stream key prefix (without priority suffix). |
| `version_info` | Yes | Versioning metadata. |

### Intent Declaration

| Field | Required | Description |
|---|---|---|
| `name` | Yes | `<domain>.<action>` format. Must be unique across all agents. |
| `description` | Yes | Human-readable. Used by orchestrator for routing decisions. |
| `input_schema` | Yes | Path to JSON Schema file for input validation. |
| `output_schema` | Yes | Path to JSON Schema file for output validation. |
| `timeout_sec` | Yes | Maximum execution time. Used for XAUTOCLAIM tuning. |

### Auth Requirements

Only for intents calling external provider APIs on behalf of the user:

| Field | Description |
|---|---|
| `provider` | OAuth provider name: `google`, `github`, `slack`, `microsoft`, etc. |
| `scopes` | List of OAuth scope URLs required. |

### Resource Resolution Intent

Add `<domain>.resolve_resource` when the user may refer to provider-owned resources
by human name rather than stable IDs.

Rules:
- Search one account per task. The orchestrator decides which account to search and passes a single `input.auth`.
- Return matches with stable resource identifiers and enough metadata for the orchestrator to create or reuse a binding.
- Do not guess accounts in the agent.
- Do not persist resource bindings in the agent's own `store/data/`; binding ownership lives in the Gateway/orchestrator credential flow.

### Policies

| Field | Description |
|---|---|
| `network_access` | Whether this agent can make outbound network calls. |
| `writable_paths` | Filesystem paths this agent can write to. |
| `tool_access` | Declared tools (NOT universal tools). |
| `allowed_senders` | Agent IDs allowed to send tasks to this agent. |
| `intent_authorization` | Per-intent sender allowlist. |

### Specialized Policy Extensions

The generic template is not sufficient for every agent class.

Browser agents add:

```yaml
policies:
  sandbox:
    browser_profile: isolated
    network_allowlist: ['*']
    download_dir: runtime/downloads/
    max_pages: 5
```

System agents add:

```yaml
policies:
  sandbox:
    shell_allowlist: [ls, cat, grep, find, wc, pip, npm, git, python, node]
    shell_denylist: ['rm -rf /', sudo, 'chmod 777', mkfs]
    max_file_size_mb: 100
```

The CLI agent adds:

```yaml
policies:
  safety:
    require_confirmation: true
    audit_all_commands: true
    alpha_warning: true
```

For browser, system, and CLI agents, copy the full class-specific card shape from architecture §§29-30, including the specialized SLA and retry settings. Do not infer those classes from the generic defaults alone.

### SLA

| Field | Description |
|---|---|
| `max_concurrency` | Max parallel tasks. Orchestrator checks `current_load < max_concurrency`. |
| `heartbeat_interval_sec` | Heartbeat frequency. |
| `heartbeat_ttl_sec` | If no heartbeat for this long, instance is considered dead. |
| `max_task_duration_sec` | Longest possible task. XAUTOCLAIM: `min_idle_time = max_task_duration_sec * 2 * 1000`. |
| `retry_policy.max_attempts` | Max retries for retryable errors. |
| `retry_policy.backoff` | `exponential` or `fixed`. |
| `retry_policy.retryable_codes` | Error codes that trigger retry. |
| `retry_policy.non_retryable_codes` | Error codes that are terminal. |

## Agent ID Design

Format: `{org}/{name}:{version}`

```
cosmic/research-agent:1.0.0
cosmic/docs-agent:2.1.0
cosmic/browser-agent:1.0.0
cosmic/system-agent:1.0.0
cosmic/cli-agent:1.0.0
```

**Why version in the ID?** Lets you run `1.0.0` and `2.0.0` side-by-side during canary rollout.
Audit logs are unambiguous. Orchestrator routing pins exact versions — no silent breaking changes.

## Example: Research Agent

```yaml
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

artifact_types:
  - web_page
  - pdf
  - image
  - citation_pack

policies:
  network_access: true
  writable_paths:
    - runs/artifacts
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
  max_task_duration_sec: 180
  health_endpoint: /health
  retry_policy:
    max_attempts: 3
    backoff: exponential
    backoff_base_sec: 2
    backoff_max_sec: 60
    retryable_codes:
      - TIMEOUT
      - NETWORK_ERROR
      - RATE_LIMITED
    non_retryable_codes:
      - INVALID_INPUT
      - AUTH_ERROR
      - SCHEMA_VIOLATION
      - INTERNAL_ERROR

stream_key: streams:cosmic/research-agent:1.0.0

version_info:
  semver: 1.0.0
  released_at: 2025-01-01
  deprecated_at: null
  remove_after: null
  changelog: CHANGELOG.md
```

## Example: Docs Agent (with auth_requirements)

```yaml
agent_id: cosmic/docs-agent:2.1.0
display_name: Docs Agent

intents:
  - name: docs.edit
    description: Edit a Google Doc section
    timeout_sec: 120
  - name: docs.create
    description: Create a new Google Doc
    timeout_sec: 60
  - name: docs.resolve_resource
    description: Search for a document by name across connected accounts
    timeout_sec: 30
  - name: docs.recall_session
    description: Recall what happened in a previous session
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
  # docs.recall_session: no auth — reads from store/data/ only
```
