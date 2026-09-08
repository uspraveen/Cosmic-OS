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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["browser"])

_MIN_WAIT_SEC = 5.0
_MAX_WAIT_SEC = 600.0


class AskUserRequest(BaseModel):
    request_id: str
    question: str
    kind: str = "generic"
    task_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    timeout_sec: float = 240.0


class LiveFrameRequest(BaseModel):
    task_id: str
    frame: str


class RespondInterruptRequest(BaseModel):
    answer: str = ""


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
    result = await runtime.browser_interrupts.create_and_wait(
        request_id=str(body.request_id or "").strip(),
        question=question,
        kind=str(body.kind or "generic").strip().lower(),
        task_id=body.task_id,
        session_id=body.session_id,
        channel=body.channel,
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


@router.post("/channels/browser/interrupts/{request_id}/respond")
async def respond_interrupt(request_id: str, body: RespondInterruptRequest, request: Request) -> dict[str, Any]:
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    answer = str(body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required.")
    interrupt = runtime.browser_interrupts.resolve(request_id, answer)
    if interrupt is None:
        return {"status": "ignored"}
    return {"status": "answered", "request_id": interrupt.request_id}


@router.post("/channels/browser/interrupts/{request_id}/skip")
async def skip_interrupt(request_id: str, request: Request) -> dict[str, Any]:
    _check_local_token(request)
    runtime = request.app.state.gateway_runtime
    interrupt = runtime.browser_interrupts.skip(request_id)
    if interrupt is None:
        return {"status": "ignored"}
    return {"status": "skipped", "request_id": interrupt.request_id}
