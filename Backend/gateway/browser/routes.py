"""Browser agent AskUser bridge.

Two trust surfaces, same shape as the vault's browser-credential flow:
- /internal/browser/*   → browser agent only (internal token). One blocking
  call per AskUser question; it does not return until the desktop answers,
  skips, or the wait times out.
- /channels/browser/*   → desktop app (local API token). Resolves a pending
  interrupt the user answered on the live BrowserRunCard.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from shared.commit_policy import commit_action_class

logger = logging.getLogger(__name__)

router = APIRouter(tags=["browser"])

_MIN_WAIT_SEC = 5.0
_MAX_WAIT_SEC = 600.0

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class AskUserRequest(BaseModel):
    request_id: str
    question: str
    kind: str = "generic"
    task_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    page_url: str | None = None
    commit: dict[str, Any] | None = None
    timeout_sec: float = 240.0


class LiveFrameRequest(BaseModel):
    task_id: str
    frame: str


class RespondInterruptRequest(BaseModel):
    answer: str = ""
    # Commit cards only: values the user corrected inline before approving.
    field_edits: list[dict[str, Any]] | None = None
    # Any card: "say something in addition" — an instruction attached to
    # whichever action the user took. Distinct from the answer; the agent
    # acts on it (e.g. "approve, but uncheck the newsletter box").
    note: str = ""


_APPROVING_ANSWERS = {"approve", "approve_all", "approve-all", "approveall", "allowed", "yes"}
# The card can only ever display these as static text — they are not values.
_NON_EDITABLE_VALUES = {"checked", "unchecked"}


def _sanitize_field_edits(raw: list[Any] | None) -> list[dict[str, str]]:
    """Cap and clamp card edits to what the card could have shown.

    Labels and values keep the engine's payload caps (80/200 chars); masked
    secrets and checkbox/radio state markers are refused — a masked edit must
    never become a real password, and a checkbox is toggled, not typed.
    """
    edits: list[dict[str, str]] = []
    for entry in (raw or [])[:20]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()[:80]
        value = str(entry.get("value") or "").strip()[:200]
        if not label or not value:
            continue
        if value == "********" or value.lower() in _NON_EDITABLE_VALUES:
            continue
        edits.append({"label": label, "value": value})
    return edits


class CommitMissRequest(BaseModel):
    label: str
    url: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    action_class: str | None = None


class TakeoverControlRequest(BaseModel):
    """Pause/resume for a live browser run.

    Input does not come through here - it rides the desktop websocket, because
    one HTTP round trip per mouse move over the public internet is not input.
    Pause and resume are one click each and can afford a request.
    """

    note: str = ""


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


@router.post("/internal/browser/ask-user")
async def internal_ask_user(body: AskUserRequest, request: Request) -> dict[str, Any]:
    """Blocking call: the browser agent awaits this until the user answers.

    The request_id is minted by the browser agent itself — it already used it
    to tag the interrupt on the live progress stream before making this call,
    so the desktop's answer form and this wait are looking at the same id.
    """
    _check_internal_token(request)
    question = str(body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required.")
    runtime = request.app.state.gateway_runtime
    timeout_sec = min(max(float(body.timeout_sec or 240.0), _MIN_WAIT_SEC), _MAX_WAIT_SEC)
    kind = str(body.kind or "generic").strip().lower()
    commit = body.commit if isinstance(body.commit, dict) else {}
    # Commits (submit/apply/save/delete/pay...) are authorized first, always:
    # the orchestrator may authorize on the user's explicit instruction or
    # escalate; the default is a confirmation card showing what will happen.
    if kind == "commit":
        auth_budget = min(25.0, max(5.0, timeout_sec - 15.0))
        try:
            decision = await runtime.orchestrator.authorize_browser_commit(
                session_id=body.session_id,
                task_id=body.task_id,
                question=question,
                commit=commit,
                page_url=body.page_url,
                timeout_sec=auth_budget,
            )
        except Exception:
            decision = None
            logger.warning("browser.commit_authorizer_failed", exc_info=True)
        if isinstance(decision, dict):
            status = str(decision.get("status") or "").strip().lower()
            if status == "authorize":
                logger.info(
                    "browser.commit_authorized task_id=%s source=%s",
                    body.task_id,
                    decision.get("source"),
                )
                return {
                    "request_id": str(body.request_id or "").strip(),
                    "status": "answered",
                    "answer": "approve",
                    "source": str(decision.get("source") or "orchestrator_authority"),
                    "reason": str(decision.get("reason") or "")[:200],
                }
            if status == "deny":
                return {
                    "request_id": str(body.request_id or "").strip(),
                    "status": "answered",
                    "answer": "deny",
                    "source": "orchestrator",
                    "reason": str(decision.get("reason") or "")[:200],
                }
        # confirm (or anything unexpected): the human card below decides.
    # Context questions (address, phone, which option) get one deterministic
    # check against what the user already told the orchestrator this session
    # before a human is bothered at all. Secrets and physical actions skip
    # this path entirely — they still go straight to the user/vault.
    if kind == "generic":
        decision: Any = None
        # Give the orchestrator room for one bounded model call (it runs on
        # the Fireworks brain, typically a few seconds) while still leaving
        # most of the browser agent's wait for the human card if it escalates.
        resolver_budget = min(25.0, max(5.0, timeout_sec - 15.0))
        try:
            decision = await runtime.orchestrator.resolve_browser_interrupt(
                session_id=body.session_id,
                task_id=body.task_id,
                channel=body.channel,
                question=question,
                kind=kind,
                page_url=body.page_url,
                timeout_sec=resolver_budget,
            )
        except Exception:
            logger.warning("browser.interrupt_resolver_failed", exc_info=True)
        if isinstance(decision, dict) and str(decision.get("status") or "") == "answered":
            answer = str(decision.get("answer") or "").strip()
            if answer:
                logger.info(
                    "browser.ask_user_answered_from_memory task_id=%s topic=%s",
                    body.task_id,
                    decision.get("topic"),
                )
                return {
                    "request_id": str(body.request_id or "").strip(),
                    "status": "answered",
                    "answer": answer,
                    "source": str(decision.get("source") or "orchestrator"),
                }
        options = decision.get("options") if isinstance(decision, dict) else None
        if isinstance(options, list) and options:
            try:
                await runtime.publish_browser_interrupt_options(
                    task_id=body.task_id,
                    options=[str(item) for item in options],
                )
            except Exception:
                logger.warning("browser.interrupt_options_publish_failed", exc_info=True)
    result = await runtime.browser_interrupts.create_and_wait(
        request_id=str(body.request_id or "").strip(),
        question=question,
        kind=kind,
        task_id=body.task_id,
        session_id=body.session_id,
        channel=body.channel,
        page_url=body.page_url,
        commit=commit,
        timeout_sec=timeout_sec,
    )
    return result


@router.post("/internal/browser/live-frame")
async def internal_live_frame(body: LiveFrameRequest, request: Request) -> dict[str, Any]:
    """Fire-and-forget: one CDP screencast frame from an active browser.run.

    Never blocks the caller and never raises on a bad/unmapped task_id — a
    frame with nowhere to go is simply dropped, exactly like a dropped video
    frame on a flaky connection. See GatewayRuntime.publish_browser_live_frame.
    """
    _check_internal_token(request)
    task_id = str(body.task_id or "").strip()
    frame = str(body.frame or "").strip()
    if not task_id or not frame:
        return {"status": "dropped"}
    runtime = request.app.state.gateway_runtime
    await runtime.publish_browser_live_frame(task_id=task_id, frame=frame)
    return {"status": "ok"}


@router.post("/channels/browser/runs/{task_id}/pause")
async def pause_run(task_id: str, request: Request) -> dict[str, Any]:
    """Ask a live run to park at its next step boundary and hand over.

    The run does not stop here - it finishes the action it is in, so the page
    the user is handed is a settled one, and only then parks. The desktop
    learns it actually happened from the browser_progress takeover flag, never
    from this response.
    """
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    delivered = await runtime.publish_browser_takeover_control(
        task_id=str(task_id or "").strip(),
        payload={"op": "pause"},
    )
    if not delivered:
        raise HTTPException(status_code=404, detail="No live browser run for that task.")
    return {"status": "pausing", "task_id": task_id}


@router.post("/channels/browser/runs/{task_id}/resume")
async def resume_run(task_id: str, body: TakeoverControlRequest, request: Request) -> dict[str, Any]:
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    delivered = await runtime.publish_browser_takeover_control(
        task_id=str(task_id or "").strip(),
        payload={"op": "resume", "note": str(body.note or "")[:500]},
    )
    if not delivered:
        raise HTTPException(status_code=404, detail="No live browser run for that task.")
    return {"status": "resuming", "task_id": task_id}


@router.post("/channels/browser/interrupts/{request_id}/respond")
async def respond_interrupt(request_id: str, body: RespondInterruptRequest, request: Request) -> dict[str, Any]:
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    answer = str(body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required.")
    interrupt = runtime.browser_interrupts.resolve(
        request_id,
        answer,
        field_edits=_sanitize_field_edits(body.field_edits) if answer.lower() in _APPROVING_ANSWERS else None,
        note=str(body.note or "").strip()[:500],
    )
    if interrupt is None:
        raise HTTPException(
            status_code=410,
            detail="This question already timed out or the run moved on — the agent is no longer waiting for it.",
        )
    if interrupt.kind == "password":
        # The one place a typed password is known. Offer it to the vault from
        # here, so saving it never requires the plaintext to pass through the
        # orchestrator's context (the route that leaked last time).
        await _offer_password_to_vault(runtime, interrupt, answer)
    elif interrupt.kind == "commit" and str(answer).strip().lower() in {
        "approve_all",
        "approve-all",
        "approveall",
    }:
        # "Approve all for this task": the rest of the batch (apply to 50 jobs)
        # skips the card entirely, scoped to this task + action class.
        try:
            await runtime.orchestrator.record_browser_commit_grant(
                task_id=str(getattr(interrupt, "task_id", "") or ""),
                action_class=commit_action_class(
                    interrupt.commit if isinstance(interrupt.commit, dict) else {}
                ),
            )
        except Exception:
            logger.warning("browser.commit_grant_record_failed", exc_info=True)
    return {"status": "answered", "request_id": interrupt.request_id}


@router.post("/internal/browser/commit-miss")
async def internal_commit_miss(body: CommitMissRequest, request: Request) -> dict[str, Any]:
    """Record a commit label the deterministic classifier did not flag.

    The browser model declared it a commit (RequestCommitAuthorization); the
    label feeds the next expansion of the deterministic verb list. Append-only
    and best-effort — this must never block an authorization flow.
    """
    _check_internal_token(request)
    runtime = request.app.state.gateway_runtime
    label = str(body.label or "").strip()[:200]
    if not label:
        return {"status": "ignored"}
    from ..commit_miss import commit_miss_path, record_commit_miss

    path = commit_miss_path(runtime.config.sessions_db_path)
    recorded = record_commit_miss(
        path,
        {
            "label": label,
            "url": str(body.url or "")[:500],
            "task_id": str(body.task_id or ""),
            "session_id": str(body.session_id or ""),
            "action_class": str(body.action_class or ""),
        },
    )
    logger.info(
        "browser.commit_miss label=%s task_id=%s recorded=%s",
        label,
        body.task_id,
        recorded,
    )
    return {"status": "recorded" if recorded else "failed", "path": str(path)}


@router.post("/channels/browser/interrupts/{request_id}/skip")
async def skip_interrupt(request_id: str, request: Request) -> dict[str, Any]:
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    interrupt = runtime.browser_interrupts.skip(request_id)
    if interrupt is None:
        raise HTTPException(
            status_code=410,
            detail="This question already timed out or the run moved on — there is nothing left to skip.",
        )
    return {"status": "skipped", "request_id": interrupt.request_id}


async def _offer_password_to_vault(runtime: Any, interrupt: Any, password: str) -> None:
    """Approval-gated vault offer for a password typed into a browser card.

    Best-effort by design: the browser agent is blocked waiting on the answer,
    so a vault hiccup must never turn into a failed response. Deduped against
    stored entries and other pending offers for the same site.
    """
    try:
        from ..vault.routes import _encrypt_or_empty
        from ..vault.store import derive_site_domain, mask_secret

        page_url = str(getattr(interrupt, "page_url", "") or "").strip()
        question = str(getattr(interrupt, "question", "") or "")
        if not page_url:
            match = re.search(r"https?://[^\s<>\"')]+", question)
            if match:
                page_url = match.group(0)
        domain = derive_site_domain(page_url)
        if not domain:
            return
        store = getattr(runtime, "vault_store", None)
        if store is None:
            return
        try:
            if store.find_entries(domain):
                return
        except Exception:
            pass
        try:
            for row in store.list_pending() or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("action") or "") != "add_entry":
                    continue
                if str(row.get("status") or "pending") == "resolved":
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                prior = str(payload.get("site_url") or payload.get("site_domain") or "")
                if derive_site_domain(prior) == domain:
                    return
        except Exception:
            pass
        email_match = _EMAIL_RE.search(question)
        username = email_match.group(0) if email_match else ""
        pending = await runtime.create_vault_pending_and_notify(
            {
                "action": "add_entry",
                "task_id": getattr(interrupt, "task_id", None),
                "session_id": getattr(interrupt, "session_id", None),
                "channel": getattr(interrupt, "channel", None),
                "purpose": (
                    f"Save the password you just entered for {domain} so future browser runs "
                    "can sign in without asking you again."
                ),
                "payload": {
                    "title": domain,
                    "site_url": page_url,
                    "username": username,
                    "password_encrypted": _encrypt_or_empty(password),
                    "totp_seed_encrypted": _encrypt_or_empty(""),
                    "notes_encrypted": _encrypt_or_empty(""),
                    "tags": [],
                    "credential_kind": "login",
                    "expires_at": None,
                    "has_password": bool(password),
                    "has_totp": False,
                    "password_mask": mask_secret(password),
                },
            }
        )
        try:
            store.append_audit(
                pending.get("entry_id"),
                "user",
                "save_requested",
                str(getattr(interrupt, "task_id", "") or ""),
                result="permission_required",
                detail=pending.get("request_id"),
            )
        except Exception:
            pass
    except Exception:
        logger.warning("browser.password_vault_offer_failed", exc_info=True)
