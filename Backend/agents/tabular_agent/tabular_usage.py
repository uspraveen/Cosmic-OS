"""Usage logging for tabular specialist operations (non-LLM metering)."""

from __future__ import annotations

import time
from typing import Any

from shared.usage import begin_metered_call, build_usage_event, post_usage_event, serialize_usage_metadata

from .config import TabularAgentConfig


async def log_tabular_specialist_operation(
    *,
    cfg: TabularAgentConfig,
    http_client,
    operation: str,
    task,
    latency_ms: int,
    success: bool,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """POST a usage event for deterministic tabular work (parse, query, export, create_sheet, etc.)."""
    if not cfg.gateway_internal_token or not str(cfg.gateway_internal_token).strip():
        return
    if http_client is None:
        return

    metered = begin_metered_call(prefix="tabular_op")
    request_id = None
    if isinstance(getattr(task, "input", None), dict):
        request_id = str(task.input.get("request_id") or "").strip() or None  # type: ignore[union-attr]

    meta = dict(metadata or {})
    if error_code:
        meta["error_code"] = error_code

    event = build_usage_event(
        metered_call=metered,
        source_component="cosmic/tabular-agent:1.0.0",
        operation=operation,
        provider="cosmic",
        model="tabular-agent",
        usage_kind="specialist",
        task_id=getattr(task, "task_id", None),
        session_id=getattr(task, "session_id", None),
        route="tabular",
        source_id=getattr(task, "source_id", None),
        request_id=request_id,
        raw_usage=None,
        success=success,
        error_code=error_code if not success else None,
        latency_ms=latency_ms,
        metadata_json=serialize_usage_metadata(meta),
    )
    await post_usage_event(
        client=http_client,
        gateway_url=cfg.gateway_url,
        internal_token=cfg.gateway_internal_token,
        event=event,
    )


def monotonic_ms_since(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
