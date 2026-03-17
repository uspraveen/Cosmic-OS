from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig
from gateway.runtime import GatewayRuntime
from shared.usage import begin_metered_call, build_usage_event


def _build_runtime(tmp_path: Path, *, owner_user_id: str = "user_supabase_123") -> GatewayRuntime:
    return GatewayRuntime(
        GatewayConfig(
            local_api_token="local-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            enable_whatsapp=False,
            enable_telegram=False,
            owner_user_id=owner_user_id,
            sessions_db_path=tmp_path / "sessions.db",
            usage_db_path=tmp_path / "usage.db",
            routing_audit_db_path=tmp_path / "routing_audit.db",
            artifacts_db_path=tmp_path / "artifacts.db",
            delivery_queue_db_path=tmp_path / "delivery_queue.db",
            scheduler_db_path=tmp_path / "scheduler.db",
            memory_write_audit_db_path=tmp_path / "memory_write_audit.db",
        )
    )


def test_gateway_usage_stamps_owner_user_id(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.usage_store.initialize()

    event = build_usage_event(
        metered_call=begin_metered_call(prefix="call"),
        source_component="gateway",
        source_id="gateway:haiku",
        session_id="sess_1",
        route="haiku",
        operation="gateway.direct_chat",
        model_key="anthropic:claude-haiku-4-5",
        request_id="req_1",
        raw_usage={"input_tokens": 11, "output_tokens": 4},
    )

    assert runtime.log_usage_event(event) is True
    recent = runtime.list_recent_usage(limit=1)
    assert len(recent) == 1
    assert recent[0]["user_id"] == "user_supabase_123"


@pytest.mark.asyncio
async def test_gateway_usage_submit_queues_and_flushes(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.usage_store.initialize()
    runtime._usage_event_queue = asyncio.Queue(maxsize=8)
    runtime._usage_worker = asyncio.create_task(runtime._usage_worker_loop())

    try:
        event = build_usage_event(
            metered_call=begin_metered_call(prefix="call"),
            source_component="orchestrator",
            source_id="cosmic/orchestrator:1.0.0",
            task_id="tsk_usage_queue",
            session_id="sess_usage_queue",
            route="opus",
            operation="orchestrator.process",
            model_key="anthropic:claude-opus-4-6",
            request_id="req_usage_queue",
            raw_usage={"input_tokens": 36, "output_tokens": 5},
        )

        result = runtime.submit_usage_event(event)
        assert result.queued is True
        assert result.inserted is None

        await asyncio.wait_for(runtime._usage_event_queue.join(), timeout=1.0)

        recent = runtime.list_recent_usage(limit=1)
        assert len(recent) == 1
        assert recent[0]["llm_call_id"] == event.llm_call_id
        assert recent[0]["user_id"] == "user_supabase_123"
        assert recent[0]["total_tokens"] == 41
    finally:
        if runtime._usage_worker is not None:
            runtime._usage_worker.cancel()
            await asyncio.gather(runtime._usage_worker, return_exceptions=True)
            runtime._usage_worker = None
