"""Git credential helper backed by the Gateway's GitHub credential.

Alpha has to `git push`, and the token it needs is a GitHub App user-to-server
token that expires every 8 hours. Anything that bakes a token into a file or an
environment variable is therefore wrong within a day - which is exactly how the
previous setup died: a personal access token expired in late July, the Pages
deploy broke, and COSMIC sat waiting for a human to paste a new one.

So resolve at call time instead. Git invokes this as a credential helper, it
asks the Gateway for a live token, and the answer is fresh by construction.
When the Gateway refreshes the token underneath, nothing here has to change.

Run as a helper:  git -c credential.helper='!python3 git_credentials.py' push
Git speaks a tiny line protocol on stdin/stdout; see `main`.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GITHUB_HOSTS = {"github.com", "gist.github.com"}

# Git blocks on this call, so it must fail fast rather than hang a push.
RESOLVE_TIMEOUT_SEC = 10.0

# GitHub accepts any non-empty username when the password is a token; this is
# the documented sentinel and it makes the token's nature obvious in logs.
GIT_USERNAME = "x-access-token"


def resolve_github_token(
    *,
    gateway_url: str | None = None,
    internal_token: str | None = None,
    timeout_sec: float = RESOLVE_TIMEOUT_SEC,
) -> str:
    """Ask the Gateway for a live GitHub access token.

    Returns "" rather than raising: a missing token must surface as git's own
    authentication error, not as a traceback from inside a credential helper,
    where it would be far harder to read.
    """
    base = (gateway_url or os.getenv("GATEWAY_URL") or "http://127.0.0.1:8080").rstrip("/")
    token = internal_token or os.getenv("GATEWAY_INTERNAL_TOKEN") or ""
    if not token:
        return ""

    request = urllib.request.Request(
        f"{base}/internal/credentials/resolve",
        data=json.dumps(
            {
                "provider": "github",
                "required_scopes": [],
                "allow_primary_fallback": True,
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("access_token") or "").strip()


def _read_request(stream) -> dict[str, str]:
    """Parse git's `key=value` block, terminated by a blank line or EOF."""
    fields: dict[str, str] = {}
    for raw in stream:
        line = raw.strip()
        if not line:
            break
        key, _, value = line.partition("=")
        if key:
            fields[key.strip()] = value.strip()
    return fields


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    action = args[0] if args else "get"
    # `store` and `erase` are part of the protocol. There is nothing to persist
    # or forget - the Gateway owns the credential - but they must exit cleanly,
    # because a non-zero status here makes git abort the whole operation.
    if action != "get":
        return 0

    fields = _read_request(sys.stdin)
    host = fields.get("host", "")
    if host and host not in GITHUB_HOSTS:
        # Answer only for GitHub. Emitting a GitHub token for another host
        # would leak it to whoever that remote belongs to.
        return 0

    token = resolve_github_token()
    if not token:
        # Silence makes git fall through to its next helper and then fail with
        # a normal authentication error.
        return 0

    sys.stdout.write(f"username={GIT_USERNAME}\n")
    sys.stdout.write(f"password={token}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
