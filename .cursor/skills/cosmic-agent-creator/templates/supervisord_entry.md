# Template: supervisord.conf Entry

Add this block to the project's `supervisord.conf` for each new agent.

```ini
[program:<agent_name>]
command=python -m agents.<agent_name>
autostart=true
autorestart=true
environment=INSTANCE_ID='<agent_name>-1',AGENT_SECRET='<agent-specific-secret>'
stderr_logfile=/var/log/<agent_name>.err.log
stdout_logfile=/var/log/<agent_name>.out.log
```

Use the generic block above for normal always-on agents. Do NOT use it for the CLI agent.
Browser agents commonly add `PLAYWRIGHT_BROWSERS_PATH` in the environment.

## For Multiple Instances (Horizontal Scaling)

```ini
[program:<agent_name>_1]
command=python -m agents.<agent_name>
autostart=true
autorestart=true
environment=INSTANCE_ID='<agent_name>-1',AGENT_SECRET='<agent-specific-secret>'
stderr_logfile=/var/log/<agent_name>_1.err.log
stdout_logfile=/var/log/<agent_name>_1.out.log

[program:<agent_name>_2]
command=python -m agents.<agent_name>
autostart=true
autorestart=true
environment=INSTANCE_ID='<agent_name>-2',AGENT_SECRET='<agent-specific-secret>'
stderr_logfile=/var/log/<agent_name>_2.err.log
stdout_logfile=/var/log/<agent_name>_2.out.log
```

## For Alpha/On-Demand Agents

```ini
[program:<agent_name>]
command=python -m agents.<agent_name>
autostart=false
autorestart=false
environment=INSTANCE_ID='<agent_name>-1',AGENT_SECRET='<agent-specific-secret>',<AGENT_NAME>_MODE='sleeping'
stderr_logfile=/var/log/<agent_name>.err.log
stdout_logfile=/var/log/<agent_name>.out.log
; Alpha agent. autostart=false — wakes on demand only.
; autorestart=false — does not restart after exit.
```

## Orchestrator AGENT_SECRETS Update

When adding a new agent, also update the orchestrator's environment to include the
new agent's secret in the `AGENT_SECRETS` JSON map:

```ini
[program:orchestrator]
environment=INSTANCE_ID='orchestrator-1',AGENT_SECRETS='{"cosmic/gateway:1.0.0": "<gateway-signing-secret>", "cosmic/research-agent:1.0.0": "...", "cosmic/docs-agent:2.1.0": "...", "cosmic/<NEW_AGENT>:<VERSION>": "<new-agent-secret>"}',GATEWAY_INTERNAL_TOKEN='<internal-service-token>'
```

## Required Environment Variables

| Variable | Owner | Purpose |
|---|---|---|
| `INSTANCE_ID` | This agent instance | Unique per worker process. Format: `<name>-N`. |
| `AGENT_SECRET` | This agent + Orchestrator | Shared HMAC secret. Same value in agent's env AND orchestrator's AGENT_SECRETS map. |
| Agent-specific vars | This agent | E.g., `PLAYWRIGHT_BROWSERS_PATH`, `CLI_AGENT_MODE`. |
