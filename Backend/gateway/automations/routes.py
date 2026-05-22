from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..channels.routes import get_runtime, require_internal_token
from ..runtime import GatewayRuntime


router = APIRouter(tags=["automations"])


class EventAutomationRequest(BaseModel):
    automation_id: str | None = Field(default=None, max_length=80)
    event_type: str = Field(default="gmail.inbound", min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=200)
    raw_instruction: str = Field(..., min_length=1, max_length=8000)
    condition: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    approval_policy: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=32)
    source: str | None = Field(default=None, max_length=120)
    request_id: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)
    channel: str | None = Field(default=None, max_length=128)


class EventAutomationStatusRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=32)


@router.post("/internal/automations/events")
async def create_internal_event_automation(
    body: EventAutomationRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        automation = runtime.create_event_automation(
            automation_id=body.automation_id,
            event_type=body.event_type,
            label=body.label,
            raw_instruction=body.raw_instruction,
            condition=body.condition,
            action=body.action,
            approval_policy=body.approval_policy,
            status=body.status,
            created_by=body.source or "orchestrator",
            created_request_id=body.request_id,
            created_session_id=body.session_id,
            created_channel=body.channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return automation


@router.get("/internal/automations/events")
async def list_internal_event_automations(
    event_type: str | None = None,
    status_filter: str = "active",
    limit: int = 50,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "automations": runtime.list_event_automations(
            event_type=event_type,
            status=status_filter,
            limit=limit,
        )
    }


@router.get("/internal/automations/events/{automation_id}")
async def get_internal_event_automation(
    automation_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    automation = runtime.get_event_automation(automation_id)
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown automation")
    return automation


@router.patch("/internal/automations/events/{automation_id}")
async def update_internal_event_automation_status(
    automation_id: str,
    body: EventAutomationStatusRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        automation = runtime.set_event_automation_status(
            automation_id=automation_id,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown automation")
    return automation


@router.delete("/internal/automations/events/{automation_id}")
async def delete_internal_event_automation(
    automation_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    automation = runtime.set_event_automation_status(
        automation_id=automation_id,
        status="inactive",
    )
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown automation")
    return {"deleted": True, "automation_id": automation_id, "status": automation.get("status")}
