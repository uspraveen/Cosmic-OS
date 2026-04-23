from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..channels.routes import get_runtime, require_local_api_token
from ..runtime import GatewayRuntime

router = APIRouter(tags=["preferences"])


class VisualResponseEnhancementPreference(BaseModel):
    enabled: bool
    revision: int = Field(..., ge=1)
    updated_at: str
    updated_source: str | None = None
    updated_device_id: str | None = None


class DesktopPreferencesResponse(BaseModel):
    visual_response_enhancement: VisualResponseEnhancementPreference


class DesktopPreferencesUpdateRequest(BaseModel):
    visual_response_enhancement_enabled: bool = True


def _extract_device_id(request: Request) -> str | None:
    return str(request.headers.get("X-Device-Id") or "").strip() or None


@router.get("/desktop/preferences", response_model=DesktopPreferencesResponse)
async def get_desktop_preferences(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.get_desktop_preferences_snapshot()


@router.patch("/desktop/preferences", response_model=DesktopPreferencesResponse)
async def update_desktop_preferences(
    payload: DesktopPreferencesUpdateRequest,
    request: Request,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return await runtime.save_visual_response_enhancement_preference(
        enabled=payload.visual_response_enhancement_enabled,
        source="desktop_settings",
        device_id=_extract_device_id(request),
    )
