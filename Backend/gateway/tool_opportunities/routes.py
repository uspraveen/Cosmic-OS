from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ..channels.routes import get_runtime, require_internal_token, require_local_api_token
from ..runtime import GatewayRuntime

router = APIRouter(tags=["tool-opportunities"])


class ToolOpportunityCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=1, max_length=200)
    tool_type: str = Field(default="site", max_length=40)
    goal: str = Field(..., min_length=1, max_length=2000)
    reasoning: str = Field(..., min_length=1, max_length=2000)
    proposed_features: list[str] = Field(default_factory=list, max_length=20)
    helpful_materials: list[str] = Field(default_factory=list, max_length=20)
    required_inputs: list[str] = Field(default_factory=list, max_length=12)
    data_sources: list[str] = Field(default_factory=list, max_length=20)
    expected_value: str | None = Field(default=None, max_length=1200)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_context_refs: list[str] = Field(default_factory=list, max_length=20)
    trigger_source: str | None = Field(default=None, max_length=120)
    created_by: str | None = Field(default=None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolOpportunityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=200)
    goal: str | None = Field(default=None, max_length=2000)
    reasoning: str | None = Field(default=None, max_length=2000)
    expected_value: str | None = Field(default=None, max_length=1200)
    proposed_features: list[str] | None = Field(default=None, max_length=20)
    helpful_materials: list[str] | None = Field(default=None, max_length=20)
    required_inputs: list[str] | None = Field(default=None, max_length=12)
    data_sources: list[str] | None = Field(default=None, max_length=20)
    user_feedback: str | None = Field(default=None, max_length=2000)
    declined_reason: str | None = Field(default=None, max_length=1200)
    defer_until: str | None = Field(default=None, max_length=160)
    alpha_project_id: str | None = Field(default=None, max_length=200)
    build_task_id: str | None = Field(default=None, max_length=200)
    deployment_url: str | None = Field(default=None, max_length=2000)
    repo_url: str | None = Field(default=None, max_length=2000)
    health_status: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/desktop/tool-opportunities")
async def list_desktop_tool_opportunities(
    status_filter: str | None = Query(default=None, alias="status"),
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    statuses = [part.strip() for part in str(status_filter or "").split(",") if part.strip()]
    return {"items": runtime.list_tool_opportunities(statuses=statuses or None), "summary": runtime.tool_opportunity_summary()}


@router.patch("/desktop/tool-opportunities/{opportunity_id}")
async def update_desktop_tool_opportunity(
    opportunity_id: str,
    payload: ToolOpportunityUpdateRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    item = await runtime.update_tool_opportunity(opportunity_id, payload.model_dump(exclude_none=True))
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool opportunity not found")
    return {"opportunity": item}


@router.post("/desktop/tool-opportunities/{opportunity_id}/build")
async def build_desktop_tool_opportunity(
    opportunity_id: str,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    handoff = await runtime.build_tool_opportunity_handoff(opportunity_id)
    if handoff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool opportunity not found")
    return handoff


@router.post("/internal/tool-opportunities/capture", status_code=status.HTTP_201_CREATED)
async def capture_internal_tool_opportunity(
    payload: ToolOpportunityCaptureRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.capture_tool_opportunity(payload.model_dump(mode="python"))


@router.patch("/internal/tool-opportunities/{opportunity_id}")
async def update_internal_tool_opportunity(
    opportunity_id: str,
    payload: ToolOpportunityUpdateRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    item = await runtime.update_tool_opportunity(opportunity_id, payload.model_dump(exclude_none=True))
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool opportunity not found")
    return {"opportunity": item}


@router.get("/internal/tool-opportunities")
async def list_internal_tool_opportunities(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"items": runtime.list_tool_opportunities(), "summary": runtime.tool_opportunity_summary()}
