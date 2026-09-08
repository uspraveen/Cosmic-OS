"""Password vault API routes.

Two trust surfaces:
- /channels/vault/*      → desktop settings panel (local API token). Full CRUD,
  reveal, policy management, pending approvals, audit trail.
- /internal/vault/*      → orchestrator only (internal token). Lookup returns a
  credential_ref and enforces the per-entry policy; resolve returns decrypted
  secrets and is called by the orchestrator runtime only at child-task dispatch
  so values are injected into the task envelope, never into model context.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .store import (
    POLICY_ALWAYS_ASK,
    POLICY_MODES,
    POLICY_WINDOW,
    VaultStore,
    decrypt_entry_secrets,
    derive_site_domain,
    mask_secret,
)
from .totp import generate_totp_code

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vault"])

CREDENTIAL_REF_PREFIX = "vault:"


# ── Request models ───────────────────────────────────────────────────────────


class CreateEntryRequest(BaseModel):
    title: str = ""
    site_url: str = ""
    username: str = ""
    password: str = ""
    totp_seed: str = ""
    notes: str = ""
    tags: list[str] = Field(default_factory=list)


class UpdateEntryRequest(BaseModel):
    title: str | None = None
    site_url: str | None = None
    username: str | None = None
    password: str | None = None
    totp_seed: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class PolicyRequest(BaseModel):
    mode: str = POLICY_ALWAYS_ASK
    window_expires_at: str | None = None
    window_seconds: float | None = None


class LookupRequest(BaseModel):
    query: str
    task_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    purpose: str | None = None


class ResolveRequest(BaseModel):
    credential_ref: str
    task_id: str | None = None


class AgentSaveEntryRequest(BaseModel):
    title: str = ""
    site_url: str = ""
    username: str = ""
    password: str = ""
    totp_seed: str = ""
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    purpose: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_store(request: Request) -> VaultStore:
    return request.app.state.gateway_runtime.vault_store


def _check_local_token(request: Request) -> None:
    runtime = request.app.state.gateway_runtime
    expected = runtime.config.local_api_token
    if not expected:
        return  # dev mode
    provided = _extract_local_request_token(request)
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid local token")


def _extract_local_request_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for header_name in ("X-Local-Token", "X-API-Token"):
        token = request.headers.get(header_name, "").strip()
        if token:
            return token
    return ""


def _check_internal_token(request: Request) -> None:
    runtime = request.app.state.gateway_runtime
    expected = runtime.config.internal_token
    if not expected:
        return  # dev mode
    provided = request.headers.get("X-Internal-Token", "").strip()
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid internal token")


def _entry_summary(store: VaultStore, entry: dict[str, Any]) -> dict[str, Any]:
    policy = store.get_policy(entry["entry_id"])
    return {
        "entry_id": entry["entry_id"],
        "title": entry["title"],
        "site_url": entry["site_url"],
        "site_domain": entry["site_domain"],
        "username": entry["username"],
        "tags": entry.get("tags") or [],
        "source": entry.get("source") or "user",
        "has_password": entry.get("has_password", False),
        "has_totp": entry.get("has_totp", False),
        "has_notes": entry.get("has_notes", False),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "policy": {
            "mode": policy.get("mode") or POLICY_ALWAYS_ASK,
            "window_expires_at": policy.get("window_expires_at"),
            "updated_at": policy.get("updated_at"),
        },
    }


def _vault_block_for_pending(pending: dict[str, Any]) -> dict[str, Any]:
    payload = pending.get("payload") or {}
    status = str(pending.get("status") or "pending")
    return {
        "id": f"vault_request:{pending.get('request_id')}",
        "type": "vault_permission_request",
        "request_id": pending.get("request_id"),
        "action": pending.get("action"),
        "entry_id": pending.get("entry_id"),
        "title": str(payload.get("title") or "").strip() or None,
        "site_domain": derive_site_domain(payload.get("site_url") or payload.get("site_domain") or ""),
        "username": str(payload.get("username") or "").strip() or None,
        "purpose": pending.get("purpose"),
        "status": status,
        "can_respond": status == "pending",
        "created_at": pending.get("created_at"),
    }


# ── Desktop (settings panel) routes ──────────────────────────────────────────


@router.get("/channels/vault/entries")
async def list_entries(request: Request):
    _check_local_token(request)
    store = _get_store(request)
    return {"entries": [_entry_summary(store, entry) for entry in store.list_entries()]}


@router.post("/channels/vault/entries")
async def create_entry(body: CreateEntryRequest, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    entry = store.add_entry(
        {
            "title": body.title,
            "site_url": body.site_url,
            "username": body.username,
            "password": body.password,
            "totp_seed": body.totp_seed,
            "notes": body.notes,
            "tags": body.tags,
            "source": "user",
        }
    )
    return {"entry": _entry_summary(store, entry)}


@router.get("/channels/vault/entries/{entry_id}")
async def get_entry(entry_id: str, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    summary = _entry_summary(store, entry)
    summary["notes"] = decrypt_entry_secrets(entry)["notes"]
    summary["audit"] = store.list_audit(limit=25, entry_id=entry_id)
    return {"entry": summary}


@router.patch("/channels/vault/entries/{entry_id}")
async def update_entry(entry_id: str, body: UpdateEntryRequest, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    entry = store.update_entry(entry_id, patch)
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    store.append_audit(entry_id, "user", "update")
    return {"entry": _entry_summary(store, entry)}


@router.delete("/channels/vault/entries/{entry_id}")
async def delete_entry(entry_id: str, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    deleted = store.delete_entry(entry_id)
    store.append_audit(entry_id, "user", "delete", result="deleted" if deleted else "missing")
    return {"status": "deleted" if deleted else "missing"}


@router.post("/channels/vault/entries/{entry_id}/reveal-password")
async def reveal_password(entry_id: str, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    store.append_audit(entry_id, "user", "view_password")
    return {"password": decrypt_entry_secrets(entry)["password"]}


@router.get("/channels/vault/entries/{entry_id}/totp")
async def get_totp(entry_id: str, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    code = generate_totp_code(decrypt_entry_secrets(entry)["totp_seed"])
    if not code:
        return {"code": None, "seconds_remaining": None}
    store.append_audit(entry_id, "user", "view_totp")
    return {"code": code[0], "seconds_remaining": code[1]}


@router.put("/channels/vault/entries/{entry_id}/policy")
async def set_policy(entry_id: str, body: PolicyRequest, request: Request):
    _check_local_token(request)
    store = _get_store(request)
    if not store.get_entry(entry_id):
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    if body.mode not in POLICY_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {list(POLICY_MODES)}")
    expires_at = body.window_expires_at
    if body.mode == POLICY_WINDOW and not expires_at and body.window_seconds:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=float(body.window_seconds))
        ).isoformat().replace("+00:00", "Z")
    if body.mode == POLICY_WINDOW and not expires_at:
        raise HTTPException(status_code=400, detail="window mode requires window_expires_at or window_seconds")
    if body.mode != POLICY_WINDOW:
        expires_at = None
    policy = store.set_policy(entry_id, body.mode, expires_at)
    store.append_audit(entry_id, "user", "policy_change", detail=f"mode={body.mode}")
    return {"policy": policy}


@router.get("/channels/vault/audit")
async def list_audit(request: Request, limit: int = Query(100, le=500)):
    _check_local_token(request)
    return {"audit": _get_store(request).list_audit(limit=limit)}


@router.get("/channels/vault/pending")
async def list_pending(request: Request):
    _check_local_token(request)
    store = _get_store(request)
    return {"pending": store.list_pending()}


@router.post("/channels/vault/pending/{request_id}/approve")
async def approve_pending(request_id: str, request: Request):
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    return await runtime.approve_vault_request(request_id)


@router.post("/channels/vault/pending/{request_id}/reject")
async def reject_pending(request_id: str, request: Request):
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    return await runtime.reject_vault_request(request_id)


# ── Orchestrator-only routes ─────────────────────────────────────────────────


@router.get("/internal/vault/sites")
async def internal_list_sites(request: Request):
    """Metadata-only site list so the orchestrator knows what is available."""
    _check_internal_token(request)
    store = _get_store(request)
    sites = []
    for entry in store.list_entries():
        policy = store.get_policy(entry["entry_id"])
        sites.append(
            {
                "entry_id": entry["entry_id"],
                "title": entry["title"],
                "site_url": entry["site_url"],
                "site_domain": entry["site_domain"],
                "username": entry["username"],
                "has_totp": entry.get("has_totp", False),
                "policy_mode": policy.get("mode") or POLICY_ALWAYS_ASK,
            }
        )
    return {"sites": sites}


@router.post("/internal/vault/lookup")
async def internal_lookup(body: LookupRequest, request: Request):
    """Resolve a site query to a credential the orchestrator may use.

    Never returns the password: the caller receives a credential_ref and hands
    it to delegate_to_agent; the runtime resolves the secret at dispatch time.
    """
    _check_internal_token(request)
    store = _get_store(request)
    matches = store.find_entries(body.query)
    if not matches:
        raise HTTPException(status_code=404, detail=f"No vault entry matches {body.query!r}.")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Multiple vault entries match. Retry with entry_id or a more specific site.",
                "candidates": [
                    {
                        "entry_id": entry["entry_id"],
                        "title": entry["title"],
                        "site_domain": entry["site_domain"],
                        "username": entry["username"],
                    }
                    for entry in matches[:8]
                ],
            },
        )
    entry = matches[0]
    entry_id = entry["entry_id"]

    allowed = store.policy_allows_use(entry_id)
    if not allowed:
        allowed = store.take_approved_use(entry_id, body.task_id) is not None
    if not allowed:
        pending = await request.app.state.gateway_runtime.create_vault_pending_and_notify(
            {
                "action": "use_entry",
                "entry_id": entry_id,
                "task_id": body.task_id,
                "session_id": body.session_id,
                "channel": body.channel,
                "purpose": body.purpose,
                "payload": {
                    "title": entry["title"],
                    "site_url": entry["site_url"],
                    "site_domain": entry["site_domain"],
                    "username": entry["username"],
                },
            }
        )
        store.append_audit(
            entry_id, "orchestrator", "lookup_denied", body.task_id, result="permission_required"
        )
        return {
            "status": "permission_required",
            "request_id": pending.get("request_id"),
            "entry_id": entry_id,
            "title": entry["title"],
            "username": entry["username"],
            "_cosmic_ui": {
                "render": "trusted_inline_block",
                "block_type": "vault_permission_request",
                "request_id": pending.get("request_id"),
                "summary": (
                    f"Vault access needed for {entry['title']} — waiting for user approval."
                ),
            },
        }

    totp_code = None
    totp_remaining = None
    if entry.get("has_totp"):
        code = generate_totp_code(decrypt_entry_secrets(entry)["totp_seed"])
        if code:
            totp_code, totp_remaining = code
    store.append_audit(entry_id, "orchestrator", "use", body.task_id)
    return {
        "status": "ok",
        "entry_id": entry_id,
        "credential_ref": f"{CREDENTIAL_REF_PREFIX}{entry_id}",
        "title": entry["title"],
        "site_url": entry["site_url"],
        "site_domain": entry["site_domain"],
        "username": entry["username"],
        "totp_code": totp_code,
        "totp_seconds_remaining": totp_remaining,
    }


@router.post("/internal/vault/resolve")
async def internal_resolve(body: ResolveRequest, request: Request):
    """Decrypt full credentials for dispatch-time envelope injection.

    Consumed by orchestrator runtime only (X-Internal-Token). The response goes
    straight into a child TaskEnvelope auth field and is never shown to a model.
    """
    _check_internal_token(request)
    store = _get_store(request)
    ref = str(body.credential_ref or "").strip()
    if not ref.startswith(CREDENTIAL_REF_PREFIX):
        raise HTTPException(status_code=400, detail="credential_ref must be a vault ref.")
    entry = store.get_entry(ref[len(CREDENTIAL_REF_PREFIX):])
    if not entry:
        raise HTTPException(status_code=404, detail="Vault entry not found.")
    secrets = decrypt_entry_secrets(entry)
    store.append_audit(entry["entry_id"], "orchestrator", "resolve", body.task_id)
    return {
        "credential_ref": ref,
        "entry_id": entry["entry_id"],
        "title": entry["title"],
        "site_url": entry["site_url"],
        "site_domain": entry["site_domain"],
        "username": secrets["username"],
        "password": secrets["password"],
        "totp_seed": secrets["totp_seed"],
        "notes": secrets["notes"],
    }


@router.post("/internal/vault/entries")
async def internal_save_entry(body: AgentSaveEntryRequest, request: Request):
    """Agent-created credentials require user approval before being stored."""
    _check_internal_token(request)
    runtime = request.app.state.gateway_runtime
    store = _get_store(request)
    if not str(body.title or "").strip() and not derive_site_domain(body.site_url):
        raise HTTPException(status_code=400, detail="Vault entry needs a title or site_url.")
    pending = await runtime.create_vault_pending_and_notify(
        {
            "action": "add_entry",
            "task_id": body.task_id,
            "session_id": body.session_id,
            "channel": body.channel,
            "purpose": body.purpose,
            "payload": {
                "title": body.title,
                "site_url": body.site_url,
                "username": body.username,
                # Encrypted here so the pending row and the approval card never
                # carry plaintext; decryption only happens if the user approves.
                "password_encrypted": _encrypt_or_empty(body.password),
                "totp_seed_encrypted": _encrypt_or_empty(body.totp_seed),
                "notes_encrypted": _encrypt_or_empty(body.notes),
                "tags": body.tags,
                "has_password": bool(body.password),
                "has_totp": bool(body.totp_seed),
                "password_mask": mask_secret(body.password),
            },
        }
    )
    store.append_audit(
        pending.get("entry_id"),
        "orchestrator",
        "save_requested",
        body.task_id,
        result="permission_required",
        detail=pending.get("request_id"),
    )
    return {
        "status": "permission_required",
        "request_id": pending.get("request_id"),
        "_cosmic_ui": {
            "render": "trusted_inline_block",
            "block_type": "vault_permission_request",
            "request_id": pending.get("request_id"),
            "summary": "Waiting for user approval to save these credentials in the vault.",
        },
    }


def _encrypt_or_empty(value: str) -> str:
    from ..credentials.encryption import encrypt_token_str

    return encrypt_token_str(str(value or ""))
