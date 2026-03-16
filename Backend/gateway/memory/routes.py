from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..channels.routes import get_runtime, require_internal_token
from .client import MemoryClientError, MemoryClientHTTPError
from ..runtime import GatewayRuntime

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


@router.get("/internal/memory/memories/{memory_id}")
async def memory_get(
    memory_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_get(memory_id)
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


@router.get("/internal/memory/graph-status")
async def memory_graph_status(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_graph_status()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/graph-sync")
async def memory_graph_sync(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_graph_sync()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.post("/internal/memory/graph-rebuild")
async def memory_graph_rebuild(
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return await runtime.memory_graph_rebuild()
    except Exception as exc:
        _raise_memory_http_error(exc)


@router.get("/internal/memory/write-audit")
async def memory_write_audit(
    limit: int = Query(default=50, ge=1, le=500),
    request_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    writer_id: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    status_value: str | None = Query(default=None, alias="status"),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {
        "entries": runtime.list_memory_write_audit(
            limit=limit,
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            writer_id=writer_id,
            operation=operation,
            status=status_value,
        )
    }


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


@router.get("/internal/session/state/{session_id}")
async def session_state(
    session_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.get_session_state(session_id)


@router.get("/internal/session/turns/{session_id}")
async def session_turns(
    session_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return {"session_id": session_id, "turns": runtime.list_turn_ledger(session_id, limit=limit)}


@router.get("/internal/session/history/{session_id}")
async def session_history(
    session_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    return runtime.get_session_history_page(session_id, limit=limit, offset=offset)


@router.get("/internal/session/task-notebook/{task_id}")
async def task_notebook(
    task_id: str,
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    notebook = runtime.get_task_notebook(task_id)
    if notebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task notebook not found")
    return notebook


@router.post("/internal/session/revisit")
async def session_revisit(
    payload: dict[str, Any],
    _: None = Depends(require_internal_token),
    runtime: GatewayRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="session_id is required")
    task_id = str(payload.get("task_id") or "").strip() or None
    request_id = str(payload.get("request_id") or "").strip() or None
    turn_limit = int(payload.get("turn_limit") or 8)
    raw_history_limit = int(payload.get("raw_history_limit") or 12)
    return runtime.build_revisit_payload(
        session_id=session_id,
        task_id=task_id,
        request_id=request_id,
        turn_limit=max(1, min(200, turn_limit)),
        raw_history_limit=max(1, min(200, raw_history_limit)),
    )
