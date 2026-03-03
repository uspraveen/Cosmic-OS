# Template: routing.yaml Entry

Add these entries to the orchestrator's `routing.yaml` for each intent the new agent handles.

```yaml
# routing.yaml — orchestrator reads this at startup
intents:
  # ... existing intents ...

  <domain>.<action_1>:
    agent: cosmic/<agent-name>:<version>
    priority: normal
    fallback: null

  <domain>.<action_2>:
    agent: cosmic/<agent-name>:<version>
    priority: normal
    fallback: null

  <domain>.recall_session:
    agent: cosmic/<agent-name>:<version>
    priority: normal
    fallback: null
```

## Priority Guidelines

| Priority | When to Use |
|---|---|
| `high` | User-facing actions that block the conversation (e.g., `docs.edit`, `system.process_manage`) |
| `normal` | Standard operations (e.g., `research.topic`, `browser.navigate`) |
| `low` | Background/non-urgent tasks (e.g., `browser.screenshot`) |

## Fallback

Set `fallback: null` for most agents. Fallback is for when you want a different
agent to handle the intent if the primary is unavailable. This is rarely needed.

## Existing Routing Config (for reference)

```yaml
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
