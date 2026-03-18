from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ..channels.routes import get_runtime, require_internal_token
from ..runtime import GatewayRuntime

router = APIRouter(tags=["wishlist"])


class WishlistSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(default=3, ge=1, le=10)


class WishlistCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., min_length=1, max_length=2000)
    desired_outcome: str | None = Field(default=None, max_length=1200)
    domain: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=16)
    evidence: str | None = Field(default=None, max_length=3000)
    source_component: str | None = Field(default=None, max_length=120)
    source_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)
    task_id: str | None = Field(default=None, max_length=120)
    route: str | None = Field(default=None, max_length=80)
    created_by: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/internal/cosmics-capability-wishlist/search")
async def capability_wishlist_search(
    payload: WishlistSearchRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.capability_wishlist_search(payload.model_dump(mode="python"))


@router.post("/internal/cosmics-capability-wishlist/capture", status_code=status.HTTP_201_CREATED)
async def capability_wishlist_capture(
    payload: WishlistCaptureRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.capability_wishlist_capture(payload.model_dump(mode="python"))


@router.get("/internal/cosmics-capability-wishlist/items/{capability_id}")
async def capability_wishlist_get(
    capability_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    item = await runtime.capability_wishlist_get(capability_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capability wishlist item not found")
    return item


@router.get("/internal/cosmics-capability-wishlist/summary")
async def capability_wishlist_summary(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.capability_wishlist_summary()


@router.get("/internal/cosmics-capability-wishlist/recent")
async def capability_wishlist_recent(
    limit: int = Query(default=50, ge=1, le=500),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"items": runtime.capability_wishlist_recent(limit=limit)}
