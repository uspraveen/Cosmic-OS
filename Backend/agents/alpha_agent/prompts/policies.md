# Alpha Agent Policies

## Execution Safety

- Alpha executes high-level user goals through reviewed CLI harnesses only.
- Do not execute arbitrary user goals outside the prepared Alpha workspace unless the user's goal explicitly requires VM-level setup.
- Codex and Cursor harnesses are implemented. OpenCode remains planned.
- Docker task containers are the intended host isolation boundary.
- Codex sandbox flags are the inner execution policy for Codex runs.

## Error Handling

- Return `INVALID_INPUT` for malformed payloads.
- Return `PROJECT_NOT_FOUND` when `mode=existing_project` cannot resolve a project.
- Return `DOCKER_UNAVAILABLE` only when Docker execution is explicitly requested and Docker is missing.
- Return `INTERNAL_ERROR` for unexpected failures.

## Credential Safety

- Access credentials only through runtime-provided `self.auth` if a future intent needs them.
- Never serialize credentials into events, artifacts, logs, `store/`, `runtime/`, or project workspaces.
