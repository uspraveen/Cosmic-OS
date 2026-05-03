# Alpha Agent Policies

## Execution Safety

- Alpha V1 is workspace-preparation only.
- Do not execute arbitrary user goals.
- Do not run Codex/OpenCode/Cursor harnesses until those harnesses are implemented and reviewed.
- Docker task containers are the intended host isolation boundary.
- Codex sandbox flags are the future inner execution policy.

## Error Handling

- Return `INVALID_INPUT` for malformed payloads.
- Return `PROJECT_NOT_FOUND` when `mode=existing_project` cannot resolve a project.
- Return `DOCKER_UNAVAILABLE` only when Docker execution is explicitly requested and Docker is missing.
- Return `INTERNAL_ERROR` for unexpected failures.

## Credential Safety

- Access credentials only through runtime-provided `self.auth` if a future intent needs them.
- Never serialize credentials into events, artifacts, logs, `store/`, `runtime/`, or project workspaces.

