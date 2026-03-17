from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from shared.usage import UsageEvent

from ..channels.routes import get_runtime, require_internal_token
from ..runtime import GatewayRuntime

router = APIRouter(tags=["usage"])


@router.post("/internal/usage/log")
async def log_usage_event(
    payload: UsageEvent,
    response: Response,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, object]:
    inserted = runtime.log_usage_event(payload)
    response.status_code = status.HTTP_201_CREATED if inserted else status.HTTP_200_OK
    return {
        "ok": True,
        "llm_call_id": payload.llm_call_id,
        "deduplicated": not inserted,
    }


@router.get("/internal/usage/recent")
async def list_recent_usage(
    request: Request,
    limit: int = 100,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, object]:
    del request
    return {
        "events": runtime.list_recent_usage(limit=limit),
    }
