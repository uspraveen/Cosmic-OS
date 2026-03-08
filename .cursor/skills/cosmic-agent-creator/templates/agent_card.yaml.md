# Template: agent_card.yaml

Copy this template and fill in the placeholders marked with `<...>`.

```yaml
# agents/<AGENT_NAME>/agent_card.yaml

agent_id: cosmic/<AGENT_NAME_KEBAB>:<VERSION>
display_name: <DISPLAY_NAME>
description: >
  <DESCRIPTION — one paragraph describing specialization and capabilities>

intents:
  - name: <DOMAIN>.<ACTION_1>
    description: <What this intent does>
    input_schema: schemas/intents/<DOMAIN>.<ACTION_1>.input.json
    output_schema: schemas/intents/<DOMAIN>.<ACTION_1>.output.json
    timeout_sec: <TIMEOUT>

  - name: <DOMAIN>.<ACTION_2>
    description: <What this intent does>
    input_schema: schemas/intents/<DOMAIN>.<ACTION_2>.input.json
    output_schema: schemas/intents/<DOMAIN>.<ACTION_2>.output.json
    timeout_sec: <TIMEOUT>

  - name: <DOMAIN>.recall_session
    description: Recall what happened in a previous session for this agent
    input_schema: schemas/intents/<DOMAIN>.recall_session.input.json
    output_schema: schemas/intents/<DOMAIN>.recall_session.output.json
    timeout_sec: 30

  # Add this when users may name remote provider resources instead of passing IDs.
  # Search exactly ONE account per task — the orchestrator chooses the account and
  # passes a single input.auth dict.
  # - name: <DOMAIN>.resolve_resource
  #   description: Search for a resource by name within one connected account
  #   input_schema: schemas/intents/<DOMAIN>.resolve_resource.input.json
  #   output_schema: schemas/intents/<DOMAIN>.resolve_resource.output.json
  #   timeout_sec: 30

# ONLY declare if the intent calls external provider APIs on behalf of the user.
# Remove this section entirely if no intents need user OAuth credentials.
auth_requirements:
  <DOMAIN>.<ACTION>:
    provider: <PROVIDER>
    scopes:
      - <SCOPE_URL>

artifact_types:
  - <TYPE_1>
  - <TYPE_2>

policies:
  network_access: <true|false>
  writable_paths:
    - runs/artifacts                  # root for per-task dirs: runs/artifacts/<task_id>/
    - agents/<AGENT_NAME>/store
    - agents/<AGENT_NAME>/runtime
  tool_access:
    - <TOOL_1>
    - <TOOL_2>
    # Do NOT list StepPlan, MemoryRead, MemoryWrite — they are universal
  allowed_senders:
    - cosmic/orchestrator:1.0.0
  intent_authorization:
    <DOMAIN>.<ACTION_1>: [cosmic/orchestrator:1.0.0]
    <DOMAIN>.<ACTION_2>: [cosmic/orchestrator:1.0.0]
    <DOMAIN>.recall_session: [cosmic/orchestrator:1.0.0]
  # For browser agents, add the sandbox block from §29.1:
  # sandbox:
  #   browser_profile: isolated
  #   network_allowlist: ['*']
  #   download_dir: runtime/downloads/
  #   max_pages: 5
  # For system agents, add the sandbox block from §29.2:
  # sandbox:
  #   shell_allowlist: [ls, cat, grep, find, wc, pip, npm, git, python, node]
  #   shell_denylist: ['rm -rf /', sudo, 'chmod 777', mkfs]
  #   max_file_size_mb: 100
  # For the CLI agent, add the safety block from §30.2:
  # safety:
  #   require_confirmation: true
  #   audit_all_commands: true
  #   alpha_warning: true

sla:
  max_concurrency: <N>
  heartbeat_interval_sec: 10
  heartbeat_ttl_sec: 30
  max_task_duration_sec: <LONGEST_TIMEOUT>
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

stream_key: streams:cosmic/<AGENT_NAME_KEBAB>:<VERSION>

version_info:
  semver: <VERSION>
  released_at: <YYYY-MM-DD>
  deprecated_at: null
  remove_after: null
  changelog: CHANGELOG.md
```

## Placeholder Reference

| Placeholder | Example | Rule |
|---|---|---|
| `<AGENT_NAME>` | `research_agent` | Snake case, matches directory name |
| `<AGENT_NAME_KEBAB>` | `research-agent` | Kebab case, used in agent_id |
| `<VERSION>` | `1.0.0` | Semver |
| `<DISPLAY_NAME>` | `Research Agent` | Title case, human-readable |
| `<DOMAIN>` | `research` | Lowercase, maps to agent domain |
| `<ACTION>` | `topic`, `find_image` | Lowercase with underscores |
| `<TIMEOUT>` | `180` | Seconds. Set max_task_duration_sec to the largest |
| `<PROVIDER>` | `google`, `github` | OAuth provider |
| `<N>` | `4` | Max concurrent tasks |
| `<TOOL_1>` | `web_search` | From: web_search, web_fetch, file_write, file_read, shell_execute, playwright_navigate, playwright_click, playwright_fill, playwright_extract, clipboard_read, clipboard_write, process_list, process_kill |
