from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ..channels.routes import get_runtime, require_internal_token, require_local_api_token
from ..runtime import GatewayRuntime
from .store import ProphetValidationError

router = APIRouter(tags=["prophet"])


class ProphetSectionSetting(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(..., min_length=1, max_length=60)
    label: str | None = Field(default=None, max_length=120)
    enabled: bool = True


class ProphetSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    morning_time: str | None = Field(default=None, max_length=5)
    evening_enabled: bool | None = None
    evening_time: str | None = Field(default=None, max_length=5)
    max_stories: int | None = Field(default=None, ge=5, le=30)
    notifications_enabled: bool | None = None
    sections: list[ProphetSectionSetting] | None = Field(default=None, max_length=24)


class ProphetSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default="site", max_length=20)
    value: str = Field(..., min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=160)


class ProphetPreferencesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_interests: list[str] = Field(default_factory=list, max_length=40)
    remove_interests: list[str] = Field(default_factory=list, max_length=40)
    mute_interests: list[str] = Field(default_factory=list, max_length=40)
    unmute_interests: list[str] = Field(default_factory=list, max_length=40)
    add_sources: list[ProphetSourceInput] = Field(default_factory=list, max_length=40)
    remove_source_ids: list[str] = Field(default_factory=list, max_length=40)
    origin: str = Field(default="user", max_length=20)
    allow_user_removal: bool = True


class ProphetPublishRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    edition: dict[str, Any]
    request_id: str | None = Field(default=None, max_length=120)
    slot: str | None = Field(default=None, max_length=20)


def _apply_preferences(
    runtime: GatewayRuntime,
    payload: ProphetPreferencesUpdateRequest,
) -> dict[str, Any]:
    store = runtime.prophet_store
    for topic in payload.add_interests:
        store.upsert_interest(topic, origin=payload.origin)
    for topic in payload.mute_interests:
        store.upsert_interest(topic, muted=True, origin=payload.origin)
    for topic in payload.unmute_interests:
        store.upsert_interest(topic, muted=False, origin=payload.origin)
    for topic in payload.remove_interests:
        if payload.allow_user_removal:
            store.delete_interest(topic)
        else:
            store.remove_interest(topic)
    for source in payload.add_sources:
        store.upsert_source(
            kind=source.kind,
            value=source.value,
            label=source.label,
            origin=payload.origin,
        )
    for source_id in payload.remove_source_ids:
        store.remove_source(source_id)
    return {
        "settings": store.get_settings(),
        "interests": store.list_interests(),
        "sources": store.list_sources(active_only=False),
    }


@router.get("/desktop/prophet/edition")
async def get_desktop_prophet_edition(
    edition_date: str | None = Query(default=None, alias="date"),
    slot: str | None = Query(default=None),
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "edition": runtime.prophet_store.get_edition(edition_date, slot),
        "settings": runtime.prophet_store.get_settings(),
    }


@router.get("/desktop/prophet/editions")
async def list_desktop_prophet_editions(
    limit: int = Query(default=10, ge=1, le=60),
    days: int | None = Query(default=None, ge=1, le=60),
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"editions": runtime.prophet_store.list_editions(limit=limit, days=days)}


@router.get("/desktop/prophet/settings")
async def get_desktop_prophet_settings(
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "settings": runtime.prophet_store.get_settings(),
        "interests": runtime.prophet_store.list_interests(),
        "sources": runtime.prophet_store.list_sources(active_only=False),
    }


@router.put("/desktop/prophet/settings")
async def update_desktop_prophet_settings(
    payload: ProphetSettingsUpdateRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        settings = runtime.update_prophet_settings(
            payload.model_dump(exclude_none=True, exclude_unset=True)
        )
    except ProphetValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc
    return {"settings": settings}


@router.post("/desktop/prophet/preferences")
async def update_desktop_prophet_preferences(
    payload: ProphetPreferencesUpdateRequest,
    _: None = Depends(require_local_api_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return _apply_preferences(runtime, payload)


@router.get("/internal/prophet/edition")
async def get_internal_prophet_edition(
    edition_date: str | None = Query(default=None, alias="date"),
    slot: str | None = Query(default=None),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "edition": runtime.prophet_store.get_edition(edition_date, slot),
        "settings": runtime.prophet_store.get_settings(),
    }


@router.get("/internal/prophet/editions")
async def list_internal_prophet_editions(
    limit: int = Query(default=10, ge=1, le=60),
    days: int | None = Query(default=None, ge=1, le=60),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"editions": runtime.prophet_store.list_editions(limit=limit, days=days)}


@router.post("/internal/prophet/editions", status_code=status.HTTP_201_CREATED)
async def publish_internal_prophet_edition(
    payload: ProphetPublishRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.publish_prophet_edition(
            payload.edition,
            request_id=payload.request_id,
            slot=payload.slot,
        )
    except ProphetValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


@router.post("/internal/prophet/preferences")
async def update_internal_prophet_preferences(
    payload: ProphetPreferencesUpdateRequest,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return _apply_preferences(runtime, payload)
