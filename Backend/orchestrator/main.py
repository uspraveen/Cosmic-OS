from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from shared import TaskEnvelope

from .config import OrchestratorConfig
from .runtime import OrchestratorRuntime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = OrchestratorRuntime(OrchestratorConfig.from_env())
    app.state.orchestrator_runtime = runtime
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title="COSMIC Orchestrator",
    description="Thin internal Opus orchestrator service for Gateway-dispatched tasks",
    version="1.0.0",
    lifespan=lifespan,
)


class TaskInputRequestBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    options: list[str] = Field(default_factory=list, max_length=12)
    channel: str | None = Field(default=None, max_length=128)
    agent: str = Field(default="cosmic/orchestrator:1.0.0", min_length=1, max_length=200)
    wait_timeout_sec: float | None = Field(default=None, ge=0, le=3600)


def get_runtime(request: Request) -> OrchestratorRuntime:
    runtime = getattr(request.app.state, "orchestrator_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator runtime is not initialized",
        )
    return runtime


def require_internal_token(
    request: Request,
    runtime: OrchestratorRuntime = Depends(get_runtime),
) -> None:
    expected = runtime.config.internal_token
    if not expected:
        return
    token = request.headers.get("X-Internal-Token", "").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    runtime: OrchestratorRuntime = request.app.state.orchestrator_runtime
    return {
        "status": "ok" if runtime.started else "starting",
        "model": runtime.config.anthropic_model,
        "anthropic_configured": bool(runtime.config.anthropic_api_key),
        "ledger_path": str(runtime.config.task_ledger_db_path),
        "task_input_relay": {
            "enabled": bool(runtime.config.redis_url),
            "requests_stream": runtime.config.task_input_requests_stream,
            "replies_stream": runtime.config.task_input_replies_stream,
            "group": runtime.config.task_input_orchestrator_group,
        },
    }


@app.get("/internal/tasks/active")
async def list_active_tasks(
    session_id: str | None = None,
    channel: str | None = None,
    _: None = Depends(require_internal_token),
    runtime: OrchestratorRuntime = Depends(get_runtime),
) -> dict[str, object]:
    return {
        "tasks": runtime.list_active_tasks(session_id=session_id, channel=channel),
    }


@app.post("/internal/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    _: None = Depends(require_internal_token),
    runtime: OrchestratorRuntime = Depends(get_runtime),
) -> dict[str, object]:
    cancelled = runtime.cancel_task(task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "cancelled": cancelled,
    }


@app.post("/internal/tasks/{task_id}/request-input")
async def request_task_input(
    task_id: str,
    body: TaskInputRequestBody,
    _: None = Depends(require_internal_token),
    runtime: OrchestratorRuntime = Depends(get_runtime),
) -> dict[str, object]:
    payload = await runtime.request_user_input(
        task_id,
        question=body.question,
        options=body.options,
        channel=body.channel,
        agent=body.agent,
        wait_timeout_sec=body.wait_timeout_sec,
    )
    return {
        "ok": True,
        **payload,
    }


@app.post("/internal/process/stream")
async def process_stream(
    task: TaskEnvelope,
    _: None = Depends(require_internal_token),
    runtime: OrchestratorRuntime = Depends(get_runtime),
) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[bytes]:
        async for event in runtime.stream_task(task):
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def main() -> int:
    config = OrchestratorConfig.from_env()
    uvicorn.run(
        "orchestrator.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
