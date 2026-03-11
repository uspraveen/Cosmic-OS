from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .channels.routes import get_runtime, require_internal_token
from .memory_client import MemoryClientError, MemoryClientHTTPError
from .runtime import GatewayRuntime

router = APIRouter(tags=["memory"])


def _raise_memory_http_error(exc: Exception) -> None:
    if isinstance(exc, MemoryClientHTTPError):
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if isinstance(exc, MemoryClientError):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/internal/memory/search")
async def memory_search(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_search(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/active-search")
async def memory_active_search(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_active_search(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.get("/internal/memory/schema-context")
async def memory_schema_context(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_schema_context()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/plan")
async def memory_plan(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_plan(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/resolve-identity")
async def memory_resolve_identity(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_resolve_identity(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/current-state")
async def memory_current_state(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_current_state(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/temporal-facts")
async def memory_temporal_facts(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_temporal_facts(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/memory-brief")
async def memory_brief(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_brief(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/write", status_code=status.HTTP_201_CREATED)
async def memory_write(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_write(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/core-facts", status_code=status.HTTP_201_CREATED)
async def memory_write_core_fact(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_write_core_fact(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.get("/internal/memory/core-facts")
async def memory_core_facts(
    max_chars: int = Query(default=1500, ge=250, le=20000),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_core_facts(max_chars=max_chars)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.get("/internal/memory/index-status")
async def memory_index_status(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_index_status()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/index-sync")
async def memory_index_sync(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_index_sync()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/index-rebuild")
async def memory_index_rebuild(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_index_rebuild()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/episodes", status_code=status.HTTP_201_CREATED)
async def memory_ingest_episode(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_ingest_episode(payload)
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.get("/internal/memory/health")
async def memory_health(
    request: Request,
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    runtime = get_runtime(request)
    return await runtime.memory_client.health()
