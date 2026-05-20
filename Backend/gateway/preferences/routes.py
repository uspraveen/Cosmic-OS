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


class CosmicOrchestratorModelPreference(BaseModel):
    provider: str
    model: str
    revision: int = Field(..., ge=1)
    updated_at: str
    updated_source: str | None = None
    updated_device_id: str | None = None


class CosmicHeartbeatPreference(BaseModel):
    enabled: bool
    revision: int = Field(..., ge=1)
    updated_at: str
    updated_source: str | None = None
    updated_device_id: str | None = None
    interval_sec: int | None = None
    next_fire_at: str | None = None
    last_fired_at: str | None = None
    last_suppressed_at: str | None = None
    last_result_status: str | None = None
    last_result_summary: str | None = None


class DesktopPreferencesResponse(BaseModel):
    visual_response_enhancement: VisualResponseEnhancementPreference
    cosmic_orchestrator_model: CosmicOrchestratorModelPreference
    cosmic_heartbeat: CosmicHeartbeatPreference


class DesktopPreferencesUpdateRequest(BaseModel):
    visual_response_enhancement_enabled: bool | None = None
    cosmic_orchestrator_provider: str | None = Field(default=None, max_length=64)
    cosmic_orchestrator_model: str | None = Field(default=None, max_length=256)
    cosmic_heartbeat_enabled: bool | None = None


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
    return await runtime.save_desktop_preferences(
        visual_response_enhancement_enabled=payload.visual_response_enhancement_enabled,
        cosmic_orchestrator_provider=payload.cosmic_orchestrator_provider,
        cosmic_orchestrator_model=payload.cosmic_orchestrator_model,
        cosmic_heartbeat_enabled=payload.cosmic_heartbeat_enabled,
        source="desktop_settings",
        device_id=_extract_device_id(request),
    )
