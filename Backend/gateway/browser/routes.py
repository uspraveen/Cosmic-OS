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
    timeout_sec: float = 240.0


class LiveFrameRequest(BaseModel):
    task_id: str
    frame: str


class RespondInterruptRequest(BaseModel):
    answer: str = ""


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
    # Context questions (address, phone, which option) get one deterministic
    # check against what the user already told the orchestrator this session
    # before a human is bothered at all. Secrets and physical actions skip
    # this path entirely — they still go straight to the user/vault.
    if kind == "generic":
        decision: Any = None
        try:
            decision = await runtime.orchestrator.resolve_browser_interrupt(
                session_id=body.session_id,
                task_id=body.task_id,
                channel=body.channel,
                question=question,
                kind=kind,
                page_url=body.page_url,
                timeout_sec=min(8.0, timeout_sec),
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
    interrupt = runtime.browser_interrupts.resolve(request_id, answer)
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
    return {"status": "answered", "request_id": interrupt.request_id}


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
