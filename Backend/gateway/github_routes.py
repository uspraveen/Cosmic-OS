"""GitHub webhook ingestion for the connected-repository registry.

The GitHub App can deliver `installation`, `installation_repositories`, and
`repository` events to the Gateway, keeping the authorization registry
truthful within seconds of the user changing an installation's repository
selection, renaming a repo, or uninstalling the App.

This is strictly an optional accelerator, not a dependency. A GitHub App has
exactly one webhook URL, so a fleet of per-user gateways can never each
receive it. The primary freshness model is pull-based instead: registry reads
re-enumerate the installation when the stored grant is stale
(`CredentialManager.ensure_github_registry_fresh`), and a resolve miss
triggers one bounded refresh — so correctness never depends on this endpoint
being configured. Where it is configured (single-user or relayed
deployments), it makes revocations land in seconds rather than on next read.

Unlike the generic webhook surface in the architecture spec (§26), these
events do not create TaskEnvelopes: they only keep the authorization registry
truthful. Commit-level progress reaches the same registry through Alpha's
task reporting instead.

Signature verification follows GitHub's scheme: HMAC-SHA256 over the raw
request body, hex-encoded, compared against `X-Hub-Signature-256` (prefixed
with `sha256=`). Like the Gmail webhook, verification is enforced only when a
secret is configured; local development can leave it unset.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from .runtime import GatewayRuntime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["github"])

_RESYNC_INSTALLATION_ACTIONS = {"created", "new_permissions_accepted"}
_SUSPEND_INSTALLATION_ACTIONS = {"suspend"}
_REVOKE_INSTALLATION_ACTIONS = {"deleted"}
_RESYNC_ALL_REPOSITORY_ACTIONS = {"renamed", "privatized", "publicized", "transferred", "edited"}


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    runtime: GatewayRuntime = request.app.state.gateway_runtime
    body = await request.body()
    configured_secret = str(runtime.config.github_webhook_secret or "").strip()
    supplied = str(request.headers.get("X-Hub-Signature-256") or "").strip()
    if configured_secret and not _verify_github_signature(configured_secret, body, supplied):
        raise HTTPException(
            status_code=403,
            detail="Invalid GitHub webhook signature."
            if supplied
            else "Missing GitHub signature.",
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    event_name = str(request.headers.get("X-GitHub-Event") or "").strip().lower()
    if not event_name:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header.")
    classification = classify_github_event(event_name, payload)
    if classification["action"] == "ignore":
        return {"status": "ignored", "reason": classification.get("reason") or "unhandled"}
    asyncio.create_task(
        _apply_github_webhook(
            runtime,
            event_name=event_name,
            classification=classification,
        )
    )
    return {"status": "accepted", "event": event_name, "action": payload.get("action")}


def _verify_github_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    computed = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def classify_github_event(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Map a webhook event to the smallest safe registry action.

    Malformed payloads classify as ignored rather than erroring: a redelivery
    loop over a payload we never understood is worse than quietly waiting for
    the next webhook or the next scheduled sync.
    """
    action = str(payload.get("action") or "").strip().lower()
    if event_name == "ping":
        return {"action": "ignore", "reason": "ping"}
    if event_name == "installation":
        installation = payload.get("installation")
        if not isinstance(installation, dict):
            return {"action": "ignore", "reason": "missing_installation"}
        if action in _RESYNC_INSTALLATION_ACTIONS:
            return {"action": "resync", "installation": installation}
        if action in _REVOKE_INSTALLATION_ACTIONS:
            return {"action": "revoke", "installation": installation}
        if action in _SUSPEND_INSTALLATION_ACTIONS:
            return {"action": "suspend", "installation": installation}
        return {"action": "ignore", "reason": f"unhandled_action:{action or 'none'}"}
    if event_name == "installation_repositories":
        installation = payload.get("installation")
        if not isinstance(installation, dict):
            return {"action": "ignore", "reason": "missing_installation"}
        if action not in {"added", "removed"}:
            return {"action": "ignore", "reason": f"unhandled_action:{action or 'none'}"}
        return {
            "action": "resync",
            "installation": installation,
            "repositories_removed": [
                item
                for item in payload.get("repositories_removed") or []
                if isinstance(item, dict) and item.get("id") is not None
            ],
        }
    if event_name == "repository" and action in _RESYNC_ALL_REPOSITORY_ACTIONS:
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            return {"action": "ignore", "reason": "missing_repository"}
        return {"action": "resync_all", "repository": repository}
    return {"action": "ignore", "reason": f"unhandled_event:{event_name}"}


async def _apply_github_webhook(
    runtime: GatewayRuntime,
    *,
    event_name: str,
    classification: dict[str, Any],
) -> None:
    """Apply one classified webhook event to the repository registry.

    Runs in the background so the HTTP response lands fast; GitHub retries on
    non-2xx, so correctness here matters more than speed.
    """
    mgr = runtime.credential_manager
    action = classification["action"]
    try:
        if action in {"resync", "resync_all"}:
            installation = classification.get("installation")
            if isinstance(installation, dict):
                installation_id = str(installation.get("id") or "").strip()
                account_id = mgr.find_account_id_by_installation(installation_id)
                removed = classification.get("repositories_removed") or []
                removed_ids = [
                    str(item.get("id"))
                    for item in removed
                    if isinstance(item, dict) and item.get("id") is not None
                ]
                if account_id and removed_ids:
                    mgr.mark_github_repositories_status(
                        account_id=account_id,
                        github_repo_ids=removed_ids,
                        status="access_removed",
                        sync_error="Removed from the GitHub App installation.",
                    )
                if account_id:
                    await mgr.sync_github_repositories(account_id)
                else:
                    logger.info(
                        "github_webhook.no_account_for_installation event=%s installation=%s",
                        event_name,
                        installation_id,
                    )
            else:
                # A bare repository rename/visibility change arrives without
                # installation context; resync every active GitHub account.
                await mgr.sync_github_repositories(None)
        elif action == "revoke":
            installation = classification.get("installation") or {}
            installation_id = str(installation.get("id") or "").strip()
            account_id = mgr.find_account_id_by_installation(installation_id)
            if account_id:
                mgr.revoke_github_repositories_for_installation(
                    installation_id, sync_error="GitHub App installation removed."
                )
        elif action == "suspend":
            installation = classification.get("installation") or {}
            installation_id = str(installation.get("id") or "").strip()
            account_id = mgr.find_account_id_by_installation(installation_id)
            if account_id:
                mgr.mark_github_repositories_for_installation(
                    installation_id,
                    status="access_removed",
                    sync_error="GitHub App installation suspended.",
                )
    except Exception:
        logger.exception(
            "github_webhook.apply_failed event=%s action=%s", event_name, action
        )
