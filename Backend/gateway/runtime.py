from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import HaikuAdapter, PerplexityAdapter
from .artifact_store import ArtifactStore
from .channels.base import ChannelUnavailableError, PermanentDeliveryError, RetryableDeliveryError
from .channels.desktop import DesktopAdapter
from .channels.registry import ChannelAdapterRegistry
from .channels.telegram import TelegramAdapter, TelegramConfig
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .delivery_queue_store import DeliveryQueueStore, utcnow_iso
from .memory_client import CosmicMemoryClient, MemoryPromptContext
from .orchestrator_client import OrchestratorClient
from .router_client import ModelRouterClient
from .routing_audit_store import RoutingAuditStore
from .scheduler_store import SchedulerStore
from .session_store import SessionStore
from shared import (
    SOURCE_PRIORITY_MAP,
    TaskEnvelope,
    create_redis_client,
    ensure_stream_group,
    generate_task_id,
    parse_stream_payload,
    sign_task_envelope,
    utcnow,
)

logger = logging.getLogger(__name__)

CHANNEL_WELCOME_MESSAGES = {
    "whatsapp": "COSMIC is connected on WhatsApp. You can message me here anytime.",
    "telegram": "COSMIC is connected on Telegram. You can message me here anytime.",
}
MEMORY_HEALTH_REFRESH_SEC = 30.0
SESSION_SUMMARY_SOURCE_CHAR_LIMIT = 60_000
COMPACTION_TRIGGER_CHAR_THRESHOLD = 33_600
COMPACTION_RECENT_WINDOW_MESSAGES = 12
COMPACTION_RAW_MESSAGE_CHAR_LIMIT = 24_000
SYSTEM_CRON_DAILY_ROLLOVER = "system.daily_rollover"
TURN_LEDGER_WINDOW_SIZE = 10
TASK_NOTEBOOK_WINDOW_SIZE = 5
EPHEMERAL_CHANNEL_EVENT_TYPES = {
    "route_result",
    "response.chunk",
    "response.thinking.chunk",
    "task.created",
    "task.progress",
}


@dataclass(slots=True)
class ActiveRequest:
    request_id: str
    session_id: str
    channel: str
    route: str
    worker: asyncio.Task[None] | None = None
    task_id: str | None = None
    cancel_requested: bool = False
    partial_content: str = ""
    partial_thinking: str = ""
    completed: bool = False


@dataclass(slots=True)
class RoutingDecision:
    classification: dict[str, Any]
    decision_source: str
    route_override: str | None = None
    sticky_hit: bool = False
    classifier_payload: dict[str, Any] | None = None
    classifier_metrics: dict[str, Any] | None = None
    classifier_model: str | None = None
    classifier_latency_ms: float | None = None
    raw_classifier_output: str | None = None
    error_text: str | None = None


class GatewayRuntime:
    """Single-process Gateway runtime for channel ingress and control-plane routes."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.registry = ChannelAdapterRegistry()
        self.model_router = ModelRouterClient(
            base_url=config.model_router_url,
            timeout_sec=config.model_router_timeout_sec,
        )
        self.orchestrator = OrchestratorClient(
            base_url=config.orchestrator_url,
            internal_token=config.internal_token,
            timeout_sec=config.orchestrator_timeout_sec,
        )
        self.session_store = SessionStore(config.sessions_db_path)
        self.routing_audit_store = RoutingAuditStore(config.routing_audit_db_path)
        self.artifact_store = ArtifactStore(config.artifacts_db_path)
        self.delivery_queue_store = DeliveryQueueStore(config.delivery_queue_db_path)
        self.scheduler_store = SchedulerStore(config.scheduler_db_path)
        self.memory_client = CosmicMemoryClient(
            base_url=config.cosmic_memory_url,
            timeout_sec=config.cosmic_memory_timeout_sec,
            internal_token=config.internal_token,
        )
        self.haiku_adapter = HaikuAdapter(
            api_key=config.haiku_api_key,
            model=config.haiku_model,
            anthropic_version=config.anthropic_version,
            max_tokens=config.haiku_max_tokens,
            thinking_budget_tokens=config.haiku_thinking_budget_tokens,
            timeout_sec=config.direct_llm_timeout_sec,
        )
        self.perplexity_adapter = PerplexityAdapter(
            api_key=config.perplexity_api_key,
            model=config.perplexity_model,
            timeout_sec=config.direct_llm_timeout_sec,
        )
        self._redis = create_redis_client(config.redis_url) if config.redis_url else None
        self.started = False
        self.adapter_errors: dict[str, str] = {}
        self.active_task_channels: dict[str, str] = {}
        self.request_records: dict[str, dict[str, Any]] = {}
        self.active_requests: dict[str, ActiveRequest] = {}
        self.active_requests_by_task: dict[str, str] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._delivery_worker: asyncio.Task[None] | None = None
        self._delivery_wakeup = asyncio.Event()
        self._scheduler_worker: asyncio.Task[None] | None = None
        self._scheduler_wakeup = asyncio.Event()
        self._task_input_worker: asyncio.Task[None] | None = None
        self._rollover_finalize_lock = asyncio.Lock()
        self._session_compaction_lock = asyncio.Lock()
        self._memory_health_worker: asyncio.Task[None] | None = None
        self._memory_health_snapshot: dict[str, Any] = {
            "enabled": self.memory_client.enabled,
            "status": "disabled" if not self.memory_client.enabled else "starting",
        }

    async def start(self) -> None:
        self.session_store.initialize()
        self.routing_audit_store.initialize()
        self.artifact_store.initialize()
        self.delivery_queue_store.initialize()
        self.scheduler_store.initialize(default_timezone=self.config.user_timezone_fallback)
        self._sync_system_crons()
        await self.model_router.start()
        await self.orchestrator.start()
        await self.memory_client.start()
        if self._redis is not None:
            await ensure_stream_group(
                self._redis,
                stream=self.config.task_input_requests_stream,
                group=self.config.task_input_gateway_group,
            )
        if self.memory_client.enabled:
            await self._refresh_memory_health()
            self._memory_health_worker = asyncio.create_task(
                self._memory_health_loop(),
                name="gateway-memory-health",
            )
        await self._register_adapters()
        self._delivery_worker = asyncio.create_task(
            self._delivery_worker_loop(),
            name="gateway-delivery-worker",
        )
        self._scheduler_worker = asyncio.create_task(
            self._scheduler_loop(),
            name="gateway-scheduler",
        )
        if self._redis is not None:
            self._task_input_worker = asyncio.create_task(
                self._task_input_consumer_loop(),
                name="gateway-task-input-consumer",
            )
        await self._finalize_rollover_sessions()
        await self._send_channel_activation_greetings()
        self.started = True

    async def stop(self) -> None:
        if self._memory_health_worker is not None:
            self._memory_health_worker.cancel()
            await asyncio.gather(self._memory_health_worker, return_exceptions=True)
            self._memory_health_worker = None
        if self._delivery_worker is not None:
            self._delivery_worker.cancel()
            await asyncio.gather(self._delivery_worker, return_exceptions=True)
            self._delivery_worker = None
        if self._scheduler_worker is not None:
            self._scheduler_worker.cancel()
            await asyncio.gather(self._scheduler_worker, return_exceptions=True)
            self._scheduler_worker = None
        if self._task_input_worker is not None:
            self._task_input_worker.cancel()
            await asyncio.gather(self._task_input_worker, return_exceptions=True)
            self._task_input_worker = None
        workers = [state.worker for state in self.active_requests.values() if state.worker is not None]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.active_requests.clear()
        self.active_requests_by_task.clear()
        await self.registry.stop_all()
        await self.model_router.stop()
        await self.orchestrator.stop()
        await self.memory_client.stop()
        await self.haiku_adapter.close()
        await self.perplexity_adapter.close()
        if self._redis is not None:
            await self._redis.aclose()
        self.started = False

    def _normalize_timezone_name(self, value: Any) -> str | None:
        text = self._safe_text(value)
        if not text:
            return None
        try:
            ZoneInfo(text)
        except ZoneInfoNotFoundError:
            return None
        return text

    def current_user_timezone(self) -> str:
        profile = self.scheduler_store.get_profile()
        timezone_name = self._normalize_timezone_name(profile.get("user_timezone"))
        if timezone_name:
            return timezone_name
        fallback = self._normalize_timezone_name(self.config.user_timezone_fallback)
        return fallback or "UTC"

    async def update_user_timezone(
        self,
        timezone_name: str | None,
        *,
        source: str = "desktop",
    ) -> dict[str, Any] | None:
        normalized = self._normalize_timezone_name(timezone_name)
        if not normalized:
            return None
        profile = self.scheduler_store.update_user_timezone(normalized, source=source)
        self._sync_system_crons()
        self._scheduler_wakeup.set()
        return profile

    def _current_session_id(self, now: datetime | None = None) -> str:
        return self.session_store.current_session_id(
            now,
            timezone_name=self.current_user_timezone(),
            reset_hour=self.config.session_reset_hour,
        )

    def _next_rollover_fire_at(self, *, timezone_name: str, now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        local_now = current.astimezone(ZoneInfo(timezone_name))
        target = local_now.replace(
            hour=self.config.session_reset_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if local_now >= target:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _sync_system_crons(self) -> None:
        timezone_name = self.current_user_timezone()
        existing = self.scheduler_store.get_cron(SYSTEM_CRON_DAILY_ROLLOVER)
        cron_expr = f"0 {self.config.session_reset_hour} * * *"
        next_fire_at = self._next_rollover_fire_at(timezone_name=timezone_name)
        if (
            existing is not None
            and self._safe_text(existing.get("timezone")) == timezone_name
            and self._safe_text(existing.get("cron_expr")) == cron_expr
            and self._safe_text(existing.get("next_fire_at"))
        ):
            next_fire_at = self._safe_text(existing.get("next_fire_at")) or next_fire_at
        self.scheduler_store.upsert_cron(
            cron_id=SYSTEM_CRON_DAILY_ROLLOVER,
            name="Daily session rollover",
            kind="system",
            description="Finalize the previous daily session at the configured local reset hour and seed carry-forward state into the new day.",
            cron_expr=cron_expr,
            timezone_name=timezone_name,
            next_fire_at=next_fire_at,
            metadata={
                "purpose": "daily_session_rollover",
                "managed_by": "gateway",
            },
        )

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                await self._run_due_crons()
                try:
                    await asyncio.wait_for(
                        self._scheduler_wakeup.wait(),
                        timeout=self.config.scheduler_poll_interval_sec,
                    )
                except asyncio.TimeoutError:
                    continue
                self._scheduler_wakeup.clear()
        except asyncio.CancelledError:
            raise

    async def _run_due_crons(self) -> None:
        self._sync_system_crons()
        due_crons = self.scheduler_store.fetch_due_crons(now_iso=utcnow_iso(), limit=8)
        for cron in due_crons:
            cron_id = self._safe_text(cron.get("cron_id"))
            scheduled_for = self._safe_text(cron.get("next_fire_at")) or None
            status = "ignored"
            summary = "Unknown cron."
            next_fire_at = None
            try:
                if cron_id == SYSTEM_CRON_DAILY_ROLLOVER:
                    await self._finalize_rollover_sessions(current_session_id=self._current_session_id())
                    status = "completed"
                    summary = "Daily rollover finalized."
                    next_fire_at = self._next_rollover_fire_at(
                        timezone_name=self.current_user_timezone()
                    )
            except Exception as exc:
                logger.exception("gateway.scheduler_cron_failed cron_id=%s", cron_id)
                status = "failed"
                summary = str(exc)
                next_fire_at = self._safe_text(cron.get("next_fire_at")) or None
            self.scheduler_store.record_cron_result(
                cron_id=cron_id,
                scheduled_for=scheduled_for,
                status=status,
                summary=summary,
                next_fire_at=next_fire_at,
            )

    def scheduler_overview(self) -> dict[str, Any]:
        return {
            "profile": self.scheduler_store.get_profile(),
            "current_session_id": self._current_session_id(),
            "crons": self.scheduler_store.list_crons(),
            "heartbeat": self.scheduler_store.get_heartbeat(),
        }

    def list_scheduler_crons(self) -> list[dict[str, Any]]:
        return self.scheduler_store.list_crons()

    def get_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        record = self.scheduler_store.get_cron(cron_id)
        if record is None:
            return None
        record["history"] = self.scheduler_store.list_cron_history(cron_id, limit=20)
        return record

    def pause_scheduler_cron(self, cron_id: str, *, reason: str | None = None) -> dict[str, Any] | None:
        return self.scheduler_store.pause_cron(cron_id, reason=reason)

    def resume_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        next_fire_at = None
        if cron_id == SYSTEM_CRON_DAILY_ROLLOVER:
            next_fire_at = self._next_rollover_fire_at(timezone_name=self.current_user_timezone())
        record = self.scheduler_store.resume_cron(cron_id, next_fire_at=next_fire_at)
        self._scheduler_wakeup.set()
        return record

    def get_scheduler_heartbeat(self) -> dict[str, Any]:
        return self.scheduler_store.get_heartbeat()

    def pause_scheduler_heartbeat(self, *, reason: str | None = None) -> dict[str, Any]:
        return self.scheduler_store.pause_heartbeat(reason=reason)

    def resume_scheduler_heartbeat(self) -> dict[str, Any]:
        return self.scheduler_store.resume_heartbeat()

    async def _register_adapters(self) -> None:
        if "desktop" not in self.registry.adapters:
            desktop_adapter = DesktopAdapter()
            await desktop_adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(desktop_adapter)
            await desktop_adapter.start()

        if self.config.enable_whatsapp and "whatsapp" not in self.registry.adapters:
            adapter = WhatsAppAdapter(WhatsAppConfig.from_env())
            await adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(adapter)

            try:
                await adapter.start()
                self.adapter_errors.pop(adapter.platform, None)
            except Exception as exc:  # pragma: no cover - startup health is environment-dependent
                self.adapter_errors[adapter.platform] = str(exc)

        if self.config.enable_telegram and "telegram" not in self.registry.adapters:
            adapter = TelegramAdapter(
                TelegramConfig.from_env(gateway_public_host=self.config.public_host)
            )
            await adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(adapter)

            try:
                await adapter.start()
                self.adapter_errors.pop(adapter.platform, None)
            except Exception as exc:  # pragma: no cover - startup health is environment-dependent
                self.adapter_errors[adapter.platform] = str(exc)

    async def _handle_normalized_incoming_message(self, message: dict[str, Any]) -> dict[str, Any]:
        return await self.process_incoming_user_message(message)

    async def process_incoming_user_message(self, message: dict[str, Any]) -> dict[str, Any]:
        content = str(message.get("content") or "").strip()
        channel = str(message.get("channel") or "").strip()
        metadata = message.get("metadata")
        conversation_context = message.get("conversation_context")
        if not channel:
            raise ValueError("Incoming message is missing channel")
        if not isinstance(metadata, dict):
            metadata = {}
        if not isinstance(conversation_context, list):
            conversation_context = []
        route_override = self._normalize_route_override(
            message.get("route_override") if message.get("route_override") is not None else metadata.get("route_override")
        )

        session_id = self._resolve_session_id(message.get("session_id"))
        source_id = (
            self._safe_text(metadata.get("sender_jid"))
            or self._safe_text(metadata.get("chat_jid"))
            or channel
        )
        request_id = self._safe_text(message.get("request_id")) or uuid4().hex
        normalized_message = {
            **message,
            "request_id": request_id,
            "session_id": session_id,
            "metadata": metadata,
            "channel": channel,
            "conversation_context": conversation_context,
        }
        await self._finalize_rollover_sessions(current_session_id=session_id)
        session_metadata = self._ensure_session_state_seeded(session_id)
        auto_reply = await self._maybe_handle_pending_task_input_reply(
            content=content,
            channel=channel,
            session_id=session_id,
            request_id=request_id,
            source_id=source_id,
            metadata=metadata,
            normalized_message=normalized_message,
        )
        if auto_reply is not None:
            self.request_records[request_id] = auto_reply
            return auto_reply
        active_working_set = (
            session_metadata.get("active_working_set")
            if isinstance(session_metadata.get("active_working_set"), dict)
            else None
        )
        assembled_conversation_context = self._build_conversation_context(
            session_id,
            fallback_context=conversation_context,
        )

        decision_started_at = time.perf_counter()
        memory_context_task = asyncio.create_task(
            self._assemble_memory_prompt_context(
                query=content,
            )
        )
        memory_prompt_context = await memory_context_task
        routing_decision = await self._classify_message(
            session_id=session_id,
            content=content,
            metadata=metadata,
            channel=channel,
            conversation_context=assembled_conversation_context,
            memory_context=memory_prompt_context.rendered,
            route_override=route_override,
        )
        decision_latency_ms = (time.perf_counter() - decision_started_at) * 1000.0
        classification = routing_decision.classification
        dispatch_target = "orchestrator" if classification["route"] == "opus" else "gateway"
        input_artifacts = self._persist_inbound_artifacts(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            metadata=metadata,
        )

        self._append_session_message(
            session_id,
            role="user",
            content=content or "[non-text inbound message]",
            channel=channel,
            metadata={
                "request_id": request_id,
                "platform": metadata.get("platform"),
                "message_type": metadata.get("message_type"),
                "attachments": metadata.get("attachments"),
                "input_artifacts": input_artifacts,
            },
        )

        result = {
            "status": "accepted",
            "request_id": request_id,
            "session_id": session_id,
            "source": "user",
            "source_id": source_id,
            "channel": channel,
            "route": classification["route"],
            "dispatch_target": dispatch_target,
            "classification": classification,
            "message": normalized_message,
            "assembled_conversation_context": assembled_conversation_context,
            "memory_context": self._compose_prompt_context(
                active_working_set=active_working_set,
                memory_context=memory_prompt_context.rendered,
            ),
            "active_working_set": active_working_set,
            "carry_forward_packet": (
                session_metadata.get("carry_forward_packet")
                if isinstance(session_metadata.get("carry_forward_packet"), dict)
                else None
            ),
            "memory_context_payload": {
                "core_facts_rendered": memory_prompt_context.core_facts_rendered,
                "items": memory_prompt_context.recall_items,
                "total_token_count": memory_prompt_context.total_token_count,
                "diagnostics": memory_prompt_context.diagnostics,
            },
            "routing_decision_source": routing_decision.decision_source,
            "input_artifacts": input_artifacts,
            "accepted_at": utcnow_iso(),
        }
        self.request_records[request_id] = result
        self.routing_audit_store.append(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            source="user",
            source_id=source_id,
            query_text=content or "[non-text inbound message]",
            route_override=routing_decision.route_override,
            sticky_hit=routing_decision.sticky_hit,
            decision_source=routing_decision.decision_source,
            classifier_route=self._safe_text(classification.get("route")),
            final_route=self._safe_text(classification.get("route")) or "opus",
            dispatch_target=dispatch_target,
            confidence=self._coerce_float(classification.get("confidence"), 0.0),
            signals=classification.get("signals") if isinstance(classification.get("signals"), list) else [],
            conversation_context=assembled_conversation_context,
            classifier_payload=routing_decision.classifier_payload,
            classifier_metrics=routing_decision.classifier_metrics,
            classifier_model=routing_decision.classifier_model,
            classifier_latency_ms=routing_decision.classifier_latency_ms,
            decision_latency_ms=decision_latency_ms,
            error_text=routing_decision.error_text,
        )
        return result

    async def fulfill_processed_message(self, request_record: dict[str, Any]) -> None:
        route = self._safe_text(request_record.get("route")) or "opus"
        channel = self._safe_text(request_record.get("channel"))
        request_id = self._safe_text(request_record.get("request_id"))
        session_id = self._safe_text(request_record.get("session_id"))
        if not channel or not request_id or not session_id:
            raise ValueError("Request record is missing channel, request_id, or session_id")

        channel_adapter = self.registry.get_adapter(channel)
        if channel_adapter is None:
            raise ValueError(f"No adapter registered for channel: {channel!r}")

        history = self.session_store.get_pruned_history(session_id)
        memory_context = self._safe_text(request_record.get("memory_context"))
        active_request = self.active_requests.get(request_id)

        async def send(event: dict[str, Any]) -> None:
            if active_request is not None:
                self._track_partial_stream(active_request, event)
            delivery_status = await self._deliver_or_queue_channel_event(
                {
                    **event,
                    "channel": channel,
                },
                channel=channel,
            )
            await self._maybe_schedule_delivered_memory_ingest(
                {
                    **event,
                    "channel": channel,
                },
                delivery_status=delivery_status,
            )
            await self._maybe_schedule_delivered_task_summary_write(
                {
                    **event,
                    "channel": channel,
                },
                delivery_status=delivery_status,
            )
            await self._maybe_schedule_delivered_turn_finalization(
                {
                    **event,
                    "channel": channel,
                },
                delivery_status=delivery_status,
            )

        def store_assistant_message(
            content: str,
            *,
            awaiting_reply: bool,
            metadata: dict[str, Any] | None,
            channel: str,
            route: str,
        ) -> None:
            assistant_metadata = dict(metadata or {})
            assistant_metadata.setdefault("request_id", request_id)
            self._append_session_message(
                session_id,
                role="assistant",
                content=content,
                route=route,
                awaiting_reply=awaiting_reply,
                channel=channel,
                metadata=assistant_metadata,
            )

        if route in {"haiku", "gemini"}:
            await self.haiku_adapter.stream(
                request_id=request_id,
                session_id=session_id,
                history=history,
                send=send,
                store_assistant_message=store_assistant_message,
                channel=channel,
                memory_context=memory_context,
            )
            return

        if route == "perplexity":
            await self.perplexity_adapter.stream(
                request_id=request_id,
                session_id=session_id,
                history=history,
                send=send,
                store_assistant_message=store_assistant_message,
                channel=channel,
                memory_context=memory_context,
            )
            return

        task = self._build_orchestrator_task(
            request_record=request_record,
            session_id=session_id,
            request_id=request_id,
            channel=channel,
        )
        self.active_task_channels[task.task_id] = channel
        if active_request is not None:
            active_request.task_id = task.task_id
            self.active_requests_by_task[task.task_id] = request_id

        try:
            async for event in self.orchestrator.stream_task(task):
                normalized_event = self._normalize_orchestrator_event(
                    event,
                    task_id=task.task_id,
                    request_id=request_id,
                    session_id=session_id,
                    channel=channel,
                )
                await self._handle_orchestrator_event(
                    normalized_event,
                    send=send,
                    store_assistant_message=store_assistant_message,
                )
        except Exception as exc:
            self.active_task_channels.pop(task.task_id, None)
            if active_request is not None:
                self.active_requests_by_task.pop(task.task_id, None)
            await send(
                {
                    "type": "error",
                    "request_id": request_id,
                    "session_id": session_id,
                    "task_id": task.task_id,
                    "code": "OPUS_UNAVAILABLE",
                    "message": str(exc),
                }
            )

    def start_request_fulfillment(self, request_record: dict[str, Any]) -> None:
        request_id = self._safe_text(request_record.get("request_id"))
        session_id = self._safe_text(request_record.get("session_id"))
        channel = self._safe_text(request_record.get("channel"))
        route = self._safe_text(request_record.get("route")) or "opus"
        if not request_id or not session_id or not channel:
            raise ValueError("Request record is missing channel, request_id, or session_id")

        state = ActiveRequest(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route=route,
        )
        self.active_requests[request_id] = state
        state.worker = asyncio.create_task(self._run_request_fulfillment(state, request_record))

    async def cancel_active_fulfillment(
        self,
        *,
        channel: str,
        request_id: str | None = None,
        target_request_id: str | None = None,
        task_id: str | None = None,
    ) -> bool:
        normalized_channel = self._safe_text(channel)
        normalized_task_id = self._safe_text(task_id)
        normalized_request_id = self._safe_text(target_request_id) or self._safe_text(request_id)
        if not normalized_channel:
            raise ValueError("cancel requires a channel")
        if not normalized_task_id and not normalized_request_id:
            raise ValueError("cancel requires task_id or target_request_id")

        state: ActiveRequest | None = None
        if normalized_task_id:
            bound_request_id = self.active_requests_by_task.get(normalized_task_id)
            if bound_request_id:
                state = self.active_requests.get(bound_request_id)
        if state is None and normalized_request_id:
            state = self.active_requests.get(normalized_request_id)

        if state is not None and state.channel != normalized_channel:
            state = None

        if state is None and normalized_task_id:
            cancelled = await self.orchestrator.cancel_task(normalized_task_id)
            if cancelled:
                await self.deliver_channel_event(
                    {
                        "type": "task.cancelled",
                        "task_id": normalized_task_id,
                        "request_id": normalized_request_id,
                        "channel": normalized_channel,
                        "route": "opus",
                        "status": "cancelled",
                        "message": "Response stopped.",
                    }
                )
            return cancelled

        if state is None:
            return False

        state.cancel_requested = True
        if state.route == "opus" and state.task_id:
            cancelled = await self.orchestrator.cancel_task(state.task_id)
            if cancelled:
                return True

        worker = state.worker
        if worker is not None and not worker.done():
            worker.cancel()
            return True
        return False

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.session_store.list_sessions()

    def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.session_store.get_history(session_id)

    def list_routing_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.routing_audit_store.list_entries(limit=limit)

    async def _classify_message(
        self,
        *,
        session_id: str,
        content: str,
        metadata: dict[str, Any],
        channel: str,
        conversation_context: list[dict[str, Any]],
        memory_context: str | None = None,
        route_override: str | None = None,
    ) -> RoutingDecision:
        if route_override:
            return RoutingDecision(
                classification={
                    "route": route_override,
                    "needs_latest": route_override == "perplexity",
                    "needs_citations": route_override == "perplexity",
                    "is_task": False,
                    "is_continuation": False,
                    "confidence": 1.0,
                    "signals": ["manual_route_override"],
                },
                decision_source="manual_override",
                route_override=route_override,
            )

        sticky_message = self.session_store.get_last_awaiting_reply(session_id, channel)
        if sticky_message:
            self.session_store.clear_awaiting_reply(sticky_message["message_id"])
            return RoutingDecision(
                classification={
                    "route": self._normalize_route(self._safe_text(sticky_message.get("route")) or "opus"),
                    "needs_latest": False,
                    "needs_citations": False,
                    "is_task": False,
                    "is_continuation": True,
                    "confidence": 1.0,
                    "signals": ["awaiting_reply"],
                },
                decision_source="sticky_awaiting_reply",
                sticky_hit=True,
            )

        attachments = metadata.get("attachments")
        if (not content or content.startswith("[")) and isinstance(attachments, list) and attachments:
            return RoutingDecision(
                classification={
                    "route": "opus",
                    "needs_latest": False,
                    "needs_citations": False,
                    "is_task": False,
                    "is_continuation": False,
                    "confidence": 1.0,
                    "signals": ["non_text_inbound"],
                },
                decision_source="non_text_inbound",
            )

        try:
            router_response = await self.model_router.classify_with_metadata(
                query=content or "[empty message]",
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        except Exception as exc:  # pragma: no cover - depends on external service availability
            return RoutingDecision(
                classification={
                    "route": "opus",
                    "needs_latest": False,
                    "needs_citations": False,
                    "is_task": False,
                    "is_continuation": False,
                    "confidence": 0.0,
                    "signals": ["router_unavailable"],
                    "error": str(exc),
                },
                decision_source="router_unavailable",
                error_text=str(exc),
            )

        classification = router_response.get("classification")
        if not isinstance(classification, dict):
            raise RuntimeError("Model router returned an invalid classification payload")

        normalized_classification = {
            "route": self._normalize_route(self._safe_text(classification.get("route")) or "opus"),
            "needs_latest": bool(classification.get("needs_latest")),
            "needs_citations": bool(classification.get("needs_citations")),
            "is_task": bool(classification.get("is_task")),
            "is_continuation": bool(classification.get("is_continuation")),
            "confidence": self._coerce_float(classification.get("confidence"), 0.0),
            "signals": classification.get("signals") if isinstance(classification.get("signals"), list) else [],
        }
        classifier_metrics = router_response.get("metrics")
        classifier_latency_ms = None
        if isinstance(classifier_metrics, dict):
            classifier_latency_ms = self._coerce_float(classifier_metrics.get("rtt_ms"), 0.0)

        return RoutingDecision(
            classification=normalized_classification,
            decision_source="model_router",
            classifier_payload=router_response,
            classifier_metrics=classifier_metrics if isinstance(classifier_metrics, dict) else None,
            classifier_model=self._safe_text(router_response.get("classifier_model")) or None,
            classifier_latency_ms=classifier_latency_ms,
            raw_classifier_output=self._safe_text(router_response.get("raw_classifier_output")) or None,
        )

    def _resolve_session_id(self, requested_session_id: Any) -> str:
        current_session_id = self._current_session_id()
        requested = self._safe_text(requested_session_id)
        if requested == current_session_id:
            return requested
        return current_session_id

    def _build_conversation_context(
        self,
        session_id: str,
        *,
        fallback_context: list[dict[str, Any]] | None = None,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        history = self.session_store.get_history_tail(session_id, limit=limit)
        if history:
            context: list[dict[str, str]] = []
            for item in history[-limit:]:
                entry: dict[str, str] = {
                    "role": item["role"],
                    "content": item["content"],
                }
                route = self._safe_text(item.get("route"))
                if route and item["role"] == "assistant":
                    entry["route"] = route
                context.append(entry)
            return context

        if not fallback_context:
            return []
        return self._normalize_conversation_context(fallback_context)[:limit]

    def _normalize_conversation_context(self, conversation_context: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for item in conversation_context:
            if not isinstance(item, dict):
                continue
            role = self._safe_text(item.get("role"))
            content = self._safe_text(item.get("content"))
            if role not in {"user", "assistant"} or not content:
                continue

            entry: dict[str, str] = {"role": role, "content": content}
            route = self._safe_text(item.get("route"))
            if route and role == "assistant":
                entry["route"] = route
            normalized.append(entry)
        return normalized

    def _append_session_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        route: str | None = None,
        awaiting_reply: bool = False,
        channel: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not content:
            return
        self.session_store.append_message(
            session_id,
            role=role,
            content=content,
            route=route,
            awaiting_reply=awaiting_reply,
            channel=channel,
            metadata=metadata,
        )

    def _ensure_session_state_seeded(self, session_id: str) -> dict[str, Any]:
        metadata = self.session_store.get_session_metadata(session_id)
        active_working_set = metadata.get("active_working_set")
        if isinstance(active_working_set, dict):
            return metadata

        carry_forward = metadata.get("carry_forward_packet")
        if not isinstance(carry_forward, dict):
            return metadata

        seeded_working_set = {
            "session_id": session_id,
            "goal": self._safe_text(carry_forward.get("goal")) or "",
            "active_workstreams": self._normalize_string_list(carry_forward.get("active_workstreams")),
            "recent_decisions": [],
            "open_loops": self._normalize_string_list(carry_forward.get("open_loops")),
            "current_focus_entities": self._normalize_entity_list(
                carry_forward.get("current_focus_entities")
            ),
            "active_task_refs": self._normalize_string_list(carry_forward.get("active_task_refs")),
            "pending_artifact_pointers": [],
            "user_preferences_in_play": self._normalize_string_list(
                carry_forward.get("stable_user_preferences")
            ),
            "last_updated_at": utcnow_iso(),
        }
        metadata = self.session_store.update_session_metadata(
            session_id,
            {"active_working_set": seeded_working_set},
        )
        return metadata

    def _compose_prompt_context(
        self,
        *,
        active_working_set: dict[str, Any] | None,
        memory_context: str | None,
    ) -> str | None:
        blocks: list[str] = []
        working_set_block = self._render_active_working_set_context(active_working_set)
        if working_set_block:
            blocks.append(working_set_block)
        memory_block = self._safe_text(memory_context)
        if memory_block:
            blocks.append(memory_block)
        if not blocks:
            return None
        return "\n\n".join(blocks)

    def _render_active_working_set_context(self, working_set: dict[str, Any] | None) -> str | None:
        if not isinstance(working_set, dict):
            return None

        lines: list[str] = ["## Active Working Set"]
        goal = self._safe_text(working_set.get("goal"))
        if goal:
            lines.extend(["", f"- Goal: {goal}"])
        active_workstreams = self._normalize_string_list(working_set.get("active_workstreams"))
        if active_workstreams:
            lines.extend(["", "- Active workstreams:"])
            lines.extend(f"  - {item}" for item in active_workstreams[:6])
        open_loops = self._normalize_string_list(working_set.get("open_loops"))
        if open_loops:
            lines.extend(["", "- Open loops:"])
            lines.extend(f"  - {item}" for item in open_loops[:6])
        recent_decisions = self._normalize_string_list(working_set.get("recent_decisions"))
        if recent_decisions:
            lines.extend(["", "- Recent decisions:"])
            lines.extend(f"  - {item}" for item in recent_decisions[:6])
        task_refs = self._normalize_string_list(working_set.get("active_task_refs"))
        if task_refs:
            lines.extend(["", f"- Active task refs: {', '.join(task_refs[:6])}"])
        preferences = self._normalize_string_list(working_set.get("user_preferences_in_play"))
        if preferences:
            lines.extend(["", "- User preferences in play:"])
            lines.extend(f"  - {item}" for item in preferences[:6])
        focus_entities = self._normalize_entity_list(working_set.get("current_focus_entities"))
        if focus_entities:
            lines.extend(["", "- Current focus entities:"])
            for entity in focus_entities[:6]:
                label = self._safe_text(entity.get("label")) or self._safe_text(entity.get("id")) or "entity"
                entity_type = self._safe_text(entity.get("type"))
                if entity_type:
                    lines.append(f"  - {entity_type}: {label}")
                else:
                    lines.append(f"  - {label}")
        artifact_refs = self._normalize_string_list(working_set.get("pending_artifact_pointers"))
        if artifact_refs:
            lines.extend(["", f"- Pending artifact pointers: {', '.join(artifact_refs[:6])}"])

        return "\n".join(lines) if len(lines) > 1 else None

    async def _assemble_memory_prompt_context(self, *, query: str) -> MemoryPromptContext:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return MemoryPromptContext()
        if normalized_query.startswith("[") and normalized_query.endswith("]"):
            return MemoryPromptContext()
        if not self.memory_client.enabled:
            return MemoryPromptContext()
        try:
            return await self.memory_client.build_prompt_context(
                query=normalized_query,
                max_results=self.config.cosmic_memory_passive_max_results,
                token_budget=self.config.cosmic_memory_passive_token_budget,
                core_fact_max_chars=self.config.cosmic_memory_core_fact_max_chars,
                kinds=self.config.cosmic_memory_passive_kinds,
            )
        except Exception:
            logger.exception("gateway.memory_context_failed query=%r", normalized_query[:160])
            return MemoryPromptContext()

    def _schedule_background_task(self, coroutine, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _maybe_schedule_delivered_memory_ingest(
        self,
        event: dict[str, Any],
        *,
        delivery_status: str,
    ) -> None:
        if delivery_status != "sent":
            return
        if not self.config.cosmic_memory_ingest_transcripts or not self.memory_client.enabled:
            return
        if self._safe_text(event.get("type")) != "response.complete":
            return
        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        if not request_id or not session_id:
            return
        if not self.session_store.claim_memory_episode_ingest(
            request_id=request_id,
            session_id=session_id,
        ):
            return
        self._schedule_background_task(
            self._ingest_conversation_episode_from_delivery(
                event=event,
            ),
            name="gateway-memory-episode-ingest",
        )

    async def _ingest_conversation_episode_from_delivery(
        self,
        *,
        event: dict[str, Any],
    ) -> None:
        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        channel = self._safe_text(event.get("channel"))
        assistant_content = self._safe_text(event.get("content"))
        if not request_id or not session_id or not channel or not assistant_content:
            if request_id:
                self.session_store.release_memory_episode_ingest_claim(
                    request_id,
                    error_text="missing delivery event fields",
                )
            return

        user_message = self.session_store.find_message_by_request_id(
            session_id,
            request_id=request_id,
            role="user",
        )
        if user_message is None:
            self.session_store.release_memory_episode_ingest_claim(
                request_id,
                error_text="user message not found for delivered response",
            )
            return

        user_metadata = user_message.get("metadata") if isinstance(user_message.get("metadata"), dict) else {}
        assistant_meta = {
            "request_id": request_id,
            "route": self._safe_text(event.get("route")) or "opus",
            "awaiting_reply": bool(event.get("awaiting_reply")),
            "thinking_text": self._safe_text(event.get("thinking_text")),
            "metrics": event.get("metrics"),
        }

        try:
            response = await self.memory_client.ingest_episode(
                {
                    "observations": [
                        {
                            "role": "user",
                            "content": str(user_message.get("content") or "[empty message]"),
                            "metadata": user_metadata,
                        },
                        {
                            "role": "assistant",
                            "content": assistant_content,
                            "metadata": assistant_meta,
                        },
                    ],
                    "provenance": {
                        "source_kind": "gateway",
                        "source_id": request_id,
                        "created_by": "cosmic/gateway:1.0.0",
                        "session_id": session_id,
                        "channel": channel,
                    },
                    "kind": "transcript",
                    "title": f"Conversation turn {request_id}",
                    "tags": [
                        "conversation_turn",
                        self._safe_text(channel.split(":", 1)[0] if channel else None) or "unknown_channel",
                    ],
                    "metadata": {
                        "request_id": request_id,
                        "assistant_route": assistant_meta["route"],
                        "channel": channel,
                    },
                    "episode_type": "conversation_turn",
                    "extract_graph": self.config.cosmic_memory_episode_extract_graph,
                }
            )
        except Exception as exc:
            self.session_store.release_memory_episode_ingest_claim(
                request_id,
                error_text=str(exc),
            )
            logger.exception(
                "gateway.memory_episode_ingest_failed request_id=%s session_id=%s",
                request_id,
                session_id,
            )
            return

        memory_id = None
        if isinstance(response, dict):
            record = response.get("record")
            if isinstance(record, dict):
                memory_id = self._safe_text(record.get("memory_id"))
        self.session_store.mark_memory_episode_ingested(request_id, memory_id=memory_id)

    async def _maybe_schedule_delivered_task_summary_write(
        self,
        event: dict[str, Any],
        *,
        delivery_status: str,
    ) -> None:
        if delivery_status != "sent" or not self.memory_client.enabled:
            return
        if self._safe_text(event.get("type")) != "response.complete":
            return
        if (self._safe_text(event.get("route")) or "").strip().lower() != "opus":
            return

        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        task_id = self._safe_text(event.get("task_id"))
        if not request_id or not session_id or not task_id:
            return
        if not self.session_store.claim_task_summary_write(
            task_id=task_id,
            request_id=request_id,
            session_id=session_id,
        ):
            return
        self._schedule_background_task(
            self._write_task_summary_from_delivery(event=event),
            name="gateway-memory-task-summary-write",
        )

    async def _write_task_summary_from_delivery(
        self,
        *,
        event: dict[str, Any],
    ) -> None:
        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        task_id = self._safe_text(event.get("task_id"))
        channel = self._safe_text(event.get("channel"))
        assistant_content = self._safe_text(event.get("content"))
        if not request_id or not session_id or not task_id or not channel or not assistant_content:
            if task_id:
                self.session_store.release_task_summary_write_claim(
                    task_id,
                    error_text="missing task summary delivery fields",
                )
            return

        user_message = self.session_store.find_message_by_request_id(
            session_id,
            request_id=request_id,
            role="user",
        )
        if user_message is None:
            self.session_store.release_task_summary_write_claim(
                task_id,
                error_text="user message not found for task summary",
            )
            return

        try:
            response = await self.memory_client.write_memory(
                self._build_task_summary_memory_payload(
                    task_id=task_id,
                    request_id=request_id,
                    session_id=session_id,
                    channel=channel,
                    user_message=user_message,
                    event=event,
                )
            )
        except Exception as exc:
            self.session_store.release_task_summary_write_claim(
                task_id,
                error_text=str(exc),
            )
            logger.exception(
                "gateway.task_summary_memory_write_failed task_id=%s request_id=%s session_id=%s",
                task_id,
                request_id,
                session_id,
            )
            return

        memory_id = self._extract_memory_id(response)
        self.session_store.mark_task_summary_written(task_id, memory_id=memory_id)

    async def memory_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.passive_search(payload)

    async def memory_active_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.active_search(payload)

    async def memory_schema_context(self) -> dict[str, Any]:
        return await self.memory_client.get_schema_context()

    async def memory_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.plan_query(payload)

    async def memory_resolve_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.resolve_identity(payload)

    async def memory_current_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.current_state(payload)

    async def memory_temporal_facts(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.temporal_facts(payload)

    async def memory_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.memory_brief(payload)

    async def memory_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.write_memory(payload)

    async def memory_write_core_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.write_core_fact(payload)

    async def memory_ingest_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_client.ingest_episode(payload)

    async def memory_core_facts(self, *, max_chars: int = 1500) -> dict[str, Any]:
        return await self.memory_client.get_core_fact_block(max_chars=max_chars)

    async def memory_index_status(self) -> dict[str, Any]:
        return await self.memory_client.index_status()

    async def memory_index_sync(self) -> dict[str, Any]:
        return await self.memory_client.index_sync()

    async def memory_index_rebuild(self) -> dict[str, Any]:
        return await self.memory_client.index_rebuild()

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        record = self.session_store.get_session_record(session_id)
        if record is None:
            return {
                "session_id": session_id,
                "compacted_summary": None,
                "active_working_set": None,
                "carry_forward_packet": None,
                "compaction_packet": None,
                "metadata": {},
            }
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        return {
            "session_id": session_id,
            "compacted_summary": record.get("compacted_summary"),
            "active_working_set": metadata.get("active_working_set") if isinstance(metadata.get("active_working_set"), dict) else None,
            "carry_forward_packet": metadata.get("carry_forward_packet") if isinstance(metadata.get("carry_forward_packet"), dict) else None,
            "compaction_packet": metadata.get("compaction_packet") if isinstance(metadata.get("compaction_packet"), dict) else None,
            "metadata": metadata,
        }

    def list_turn_ledger(self, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.session_store.list_turn_ledger(session_id, limit=limit)

    def get_task_notebook(self, task_id: str) -> dict[str, Any] | None:
        return self.session_store.get_task_notebook(task_id)

    def build_revisit_payload(
        self,
        *,
        session_id: str,
        task_id: str | None = None,
        request_id: str | None = None,
        turn_limit: int = 8,
        raw_history_limit: int = 12,
    ) -> dict[str, Any]:
        payload = {
            "session": self.get_session_state(session_id),
            "turn_ledger": self.session_store.list_turn_ledger(session_id, limit=turn_limit),
            "raw_history": self.session_store.get_history_tail(session_id, limit=raw_history_limit),
        }
        normalized_task_id = self._safe_text(task_id)
        if normalized_task_id:
            payload["task_notebook"] = self.session_store.get_task_notebook(normalized_task_id)
        normalized_request_id = self._safe_text(request_id)
        if normalized_request_id:
            payload["turn"] = self.session_store.get_turn_ledger_entry(normalized_request_id)
        return payload

    async def _maybe_schedule_delivered_turn_finalization(
        self,
        event: dict[str, Any],
        *,
        delivery_status: str,
    ) -> None:
        if delivery_status != "sent":
            return
        if self._safe_text(event.get("type")) != "response.complete":
            return
        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        if not request_id or not session_id:
            return
        if self.session_store.get_turn_ledger_entry(request_id) is not None:
            return
        self._schedule_background_task(
            self._finalize_turn_after_delivery(event=event),
            name="gateway-turn-ledger-finalize",
        )

    async def _finalize_turn_after_delivery(self, *, event: dict[str, Any]) -> None:
        request_id = self._safe_text(event.get("request_id"))
        session_id = self._safe_text(event.get("session_id"))
        channel = self._safe_text(event.get("channel"))
        assistant_content = self._safe_text(event.get("content"))
        if not request_id or not session_id or not channel or not assistant_content:
            return

        user_message = self.session_store.find_message_by_request_id(
            session_id,
            request_id=request_id,
            role="user",
        )
        if user_message is None:
            return

        assistant_message = self.session_store.find_message_by_request_id(
            session_id,
            request_id=request_id,
            role="assistant",
        )
        request_record = self.request_records.get(request_id) if isinstance(self.request_records.get(request_id), dict) else {}
        turn_entry = self._build_turn_ledger_entry(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            user_message=user_message,
            assistant_message=assistant_message,
            event=event,
            request_record=request_record,
        )
        self.session_store.upsert_turn_ledger_entry(turn_entry)

        task_id = self._safe_text(event.get("task_id"))
        if task_id:
            notebook = self._merge_task_notebook(
                task_id=task_id,
                session_id=session_id,
                request_id=request_id,
                event=event,
                turn_entry=turn_entry,
            )
            self.session_store.upsert_task_notebook(task_id, session_id, notebook)

        self._refresh_active_working_set(session_id)
        self._schedule_background_task(
            self._maybe_compact_session(session_id),
            name="gateway-session-compaction",
        )

    def _build_turn_ledger_entry(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        user_message: dict[str, Any],
        assistant_message: dict[str, Any] | None,
        event: dict[str, Any],
        request_record: dict[str, Any] | None,
    ) -> dict[str, Any]:
        user_metadata = user_message.get("metadata") if isinstance(user_message.get("metadata"), dict) else {}
        artifacts = user_metadata.get("input_artifacts") if isinstance(user_metadata.get("input_artifacts"), list) else []
        artifact_refs = [
            self._safe_text(item.get("artifact_id")) or self._safe_text(item.get("path"))
            for item in artifacts
            if isinstance(item, dict)
        ]
        touched_entities = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            label = self._safe_text(artifact.get("filename")) or self._safe_text(artifact.get("artifact_id"))
            artifact_id = self._safe_text(artifact.get("artifact_id"))
            if not label and not artifact_id:
                continue
            touched_entities.append(
                {
                    "type": "artifact",
                    "id": artifact_id or label,
                    "label": label or artifact_id,
                }
            )

        route = self._safe_text(event.get("route")) or self._safe_text(assistant_message.get("route") if isinstance(assistant_message, dict) else None) or "opus"
        assistant_excerpt = self._bounded_excerpt(assistant_message.get("content") if isinstance(assistant_message, dict) else event.get("content"))
        awaiting_reply = bool(event.get("awaiting_reply"))
        open_loops = [assistant_excerpt] if awaiting_reply and assistant_excerpt else []
        task_id = self._safe_text(event.get("task_id"))
        tool_summary = [route]
        compact_line_parts = [self._bounded_excerpt(user_message.get("content"), limit=120)]
        if task_id:
            compact_line_parts.append(f"via {route} task {task_id}")
        else:
            compact_line_parts.append(f"via {route}")
        if assistant_excerpt:
            compact_line_parts.append(f"-> {self._bounded_excerpt(assistant_excerpt, limit=120)}")

        started_at = self._safe_text(
            (request_record or {}).get("accepted_at")
        ) or self._safe_text(user_message.get("created_at")) or utcnow_iso()
        completed_at = utcnow_iso()

        return {
            "turn_id": f"turn_{request_id}",
            "request_id": request_id,
            "session_id": session_id,
            "task_id": task_id,
            "channel": channel,
            "route": self._normalize_route(route),
            "started_at": started_at,
            "completed_at": completed_at,
            "user_message_id": self._safe_text(user_message.get("message_id")),
            "assistant_message_id": self._safe_text(assistant_message.get("message_id")) if isinstance(assistant_message, dict) else None,
            "user_goal": self._bounded_excerpt(user_message.get("content")),
            "user_message_excerpt": self._bounded_excerpt(user_message.get("content")),
            "assistant_outcome": assistant_excerpt,
            "facts_learned": [],
            "preferences_detected": [],
            "decisions_made": [],
            "accomplished": [assistant_excerpt] if assistant_excerpt else [],
            "tool_summary": tool_summary,
            "touched_entities": self._normalize_entity_list(touched_entities),
            "task_refs": self._normalize_string_list([task_id] if task_id else []),
            "artifact_refs": self._normalize_string_list(artifact_refs),
            "failures_to_avoid": [],
            "open_loops": open_loops,
            "compact_line": " ".join(part for part in compact_line_parts if part),
            "metadata": {
                "awaiting_reply": awaiting_reply,
                "decision_source": (request_record or {}).get("routing_decision_source"),
                "input_artifacts": artifacts,
            },
        }

    def _merge_task_notebook(
        self,
        *,
        task_id: str,
        session_id: str,
        request_id: str | None,
        event: dict[str, Any],
        turn_entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.session_store.get_task_notebook(task_id) or {}
        notebook = dict(existing)
        notebook.setdefault("task_id", task_id)
        notebook.setdefault("goal", "")
        notebook.setdefault("status", "active")
        notebook.setdefault("current_state", "")
        notebook.setdefault("key_findings", [])
        notebook.setdefault("agents_involved", ["cosmic/orchestrator:1.0.0"])
        notebook.setdefault("files_touched", [])
        notebook.setdefault("artifact_refs", [])
        notebook.setdefault("open_questions", [])
        notebook.setdefault("failures_to_avoid", [])
        notebook.setdefault("next_best_actions", [])
        notebook.setdefault("compact_history", [])
        notebook.setdefault("created_at", utcnow_iso())

        event_type = self._safe_text(event.get("type")) or ""
        request_text = ""
        if request_id:
            user_message = self.session_store.find_message_by_request_id(
                session_id,
                request_id=request_id,
                role="user",
            )
            if user_message is not None:
                request_text = self._bounded_excerpt(user_message.get("content"))
        if request_text and not self._safe_text(notebook.get("goal")):
            notebook["goal"] = request_text

        state_message = self._safe_text(event.get("message")) or self._safe_text(event.get("content")) or self._safe_text(event.get("status")) or ""
        if event_type == "task.created":
            notebook["status"] = "active"
            notebook["current_state"] = state_message or "Task created"
        elif event_type == "task.input_required":
            notebook["status"] = "waiting_for_input"
            notebook["current_state"] = state_message or "Waiting for user input"
            notebook["open_questions"] = self._normalize_string_list(
                [*self._normalize_string_list(notebook.get("open_questions")), state_message]
            )
        elif event_type == "task.completed":
            notebook["status"] = "completed"
            notebook["current_state"] = state_message or "Task completed"
        elif event_type == "task.failed":
            notebook["status"] = "failed"
            notebook["current_state"] = state_message or "Task failed"
            notebook["failures_to_avoid"] = self._normalize_string_list(
                [*self._normalize_string_list(notebook.get("failures_to_avoid")), state_message]
            )
        elif event_type == "task.cancelled":
            notebook["status"] = "cancelled"
            notebook["current_state"] = state_message or "Task cancelled"

        if turn_entry is not None:
            notebook["artifact_refs"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("artifact_refs"), limit=16),
                    *self._normalize_string_list(turn_entry.get("artifact_refs"), limit=16),
                ],
                limit=16,
            )
            notebook["files_touched"] = self._normalize_entity_list(
                [*self._normalize_entity_list(notebook.get("files_touched"), limit=16), *self._normalize_entity_list(turn_entry.get("touched_entities"), limit=16)],
                limit=16,
            )
            notebook["key_findings"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("key_findings"), limit=12),
                    *self._normalize_string_list(turn_entry.get("accomplished"), limit=12),
                ],
                limit=12,
            )
            notebook["compact_history"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("compact_history"), limit=12),
                    self._safe_text(turn_entry.get("compact_line")),
                ],
                limit=12,
            )
            if turn_entry.get("open_loops"):
                notebook["open_questions"] = self._normalize_string_list(
                    [
                        *self._normalize_string_list(notebook.get("open_questions"), limit=12),
                        *self._normalize_string_list(turn_entry.get("open_loops"), limit=12),
                    ],
                    limit=12,
                )

        notebook["updated_at"] = utcnow_iso()
        return notebook

    def _refresh_active_working_set(self, session_id: str) -> dict[str, Any]:
        metadata = self.session_store.get_session_metadata(session_id)
        carry_forward = metadata.get("carry_forward_packet") if isinstance(metadata.get("carry_forward_packet"), dict) else {}
        recent_turns = self.session_store.list_turn_ledger(session_id, limit=TURN_LEDGER_WINDOW_SIZE)
        notebooks = self.session_store.list_task_notebooks(session_id, limit=TASK_NOTEBOOK_WINDOW_SIZE)
        awaiting_reply_messages = self.session_store.list_awaiting_reply_messages(session_id, limit=8)

        active_task_refs = []
        workstreams = self._normalize_string_list(carry_forward.get("active_workstreams"))
        recent_decisions: list[str] = []
        open_loops = self._normalize_string_list(carry_forward.get("open_loops"))
        entities = self._normalize_entity_list(carry_forward.get("current_focus_entities"))
        preferences = self._normalize_string_list(carry_forward.get("stable_user_preferences"))
        artifact_pointers = []
        goal = self._safe_text(carry_forward.get("goal")) or ""

        for turn in recent_turns:
            if not goal:
                goal = self._safe_text(turn.get("user_goal")) or ""
            workstreams = self._normalize_string_list([*workstreams, self._safe_text(turn.get("user_goal"))], limit=8)
            recent_decisions = self._normalize_string_list(
                [*recent_decisions, *self._normalize_string_list(turn.get("decisions_made"), limit=8)],
                limit=8,
            )
            entities = self._normalize_entity_list(
                [*entities, *self._normalize_entity_list(turn.get("touched_entities"), limit=8)],
                limit=8,
            )
            preferences = self._normalize_string_list(
                [*preferences, *self._normalize_string_list(turn.get("preferences_detected"), limit=8)],
                limit=8,
            )
            active_task_refs = self._normalize_string_list(
                [*active_task_refs, *self._normalize_string_list(turn.get("task_refs"), limit=8)],
                limit=8,
            )
            artifact_pointers = self._normalize_string_list(
                [*artifact_pointers, *self._normalize_string_list(turn.get("artifact_refs"), limit=8)],
                limit=8,
            )

        next_actions: list[str] = []
        for notebook in notebooks:
            status = (self._safe_text(notebook.get("status")) or "").lower()
            if status not in {"completed", "cancelled", "failed"}:
                active_task_refs = self._normalize_string_list(
                    [*active_task_refs, self._safe_text(notebook.get("task_id"))],
                    limit=8,
                )
                workstreams = self._normalize_string_list(
                    [*workstreams, self._safe_text(notebook.get("goal"))],
                    limit=8,
                )
                open_loops = self._normalize_string_list(
                    [*open_loops, *self._normalize_string_list(notebook.get("open_questions"), limit=8)],
                    limit=8,
                )
                next_actions = self._normalize_string_list(
                    [*next_actions, *self._normalize_string_list(notebook.get("next_best_actions"), limit=8)],
                    limit=8,
                )
            entities = self._normalize_entity_list(
                [*entities, *self._normalize_entity_list(notebook.get("files_touched"), limit=8)],
                limit=8,
            )
            artifact_pointers = self._normalize_string_list(
                [*artifact_pointers, *self._normalize_string_list(notebook.get("artifact_refs"), limit=8)],
                limit=8,
            )

        open_loops = self._normalize_string_list(
            [
                *open_loops,
                *[
                    self._bounded_excerpt(item.get("content"), limit=220)
                    for item in awaiting_reply_messages
                    if self._bounded_excerpt(item.get("content"), limit=220)
                ],
            ],
            limit=8,
        )

        working_set = {
            "session_id": session_id,
            "goal": goal,
            "active_workstreams": workstreams,
            "recent_decisions": recent_decisions,
            "open_loops": open_loops,
            "current_focus_entities": entities,
            "active_task_refs": active_task_refs,
            "pending_artifact_pointers": artifact_pointers,
            "user_preferences_in_play": preferences,
            "last_updated_at": utcnow_iso(),
        }
        self.session_store.update_session_metadata(
            session_id,
            {
                "active_working_set": working_set,
                "suggested_next_actions": next_actions,
            },
        )
        return working_set

    async def _maybe_compact_session(self, session_id: str) -> None:
        async with self._session_compaction_lock:
            history = self.session_store.get_history(session_id)
            total_chars = sum(len(str(item.get("content") or "")) for item in history)
            if total_chars < COMPACTION_TRIGGER_CHAR_THRESHOLD:
                return
            if len(history) <= COMPACTION_RECENT_WINDOW_MESSAGES:
                return
            if not self.haiku_adapter.api_key or not self.haiku_adapter.model:
                return

            recent_history = history[-COMPACTION_RECENT_WINDOW_MESSAGES:]
            older_history = history[:-COMPACTION_RECENT_WINDOW_MESSAGES]
            if not older_history:
                return

            turn_ledger = self.session_store.list_all_turn_ledger(session_id)
            recent_boundary = self._safe_text(recent_history[0].get("created_at"))
            compactable_turns = [
                item
                for item in turn_ledger
                if not recent_boundary or (self._safe_text(item.get("completed_at")) or "") < recent_boundary
            ]
            if not compactable_turns:
                return
            session_state = self.get_session_state(session_id)
            compaction_packet = (
                session_state.get("compaction_packet")
                if isinstance(session_state.get("compaction_packet"), dict)
                else {}
            )
            compacted_until_completed_at = self._safe_text(compaction_packet.get("compacted_until_completed_at"))
            new_compactable_turns = [
                item
                for item in compactable_turns
                if not compacted_until_completed_at
                or (self._safe_text(item.get("completed_at")) or "") > compacted_until_completed_at
            ]
            if not new_compactable_turns:
                return
            newly_compacted_request_ids = {
                self._safe_text(item.get("request_id"))
                for item in new_compactable_turns
                if self._safe_text(item.get("request_id"))
            }
            compactable_history = (
                [
                    item
                    for item in older_history
                    if self._safe_text(item.get("request_id")) in newly_compacted_request_ids
                ]
                if newly_compacted_request_ids
                else older_history
            )
            summary_text = await self._summarize_session_compaction(
                session_id=session_id,
                existing_summary=self._safe_text(session_state.get("compacted_summary")),
                older_history=compactable_history,
                recent_history=recent_history,
                compactable_turns=new_compactable_turns,
                session_state=session_state,
            )
            if not summary_text:
                return
            compaction_packet = self._build_compaction_packet(
                session_id=session_id,
                compacted_summary=summary_text,
                compactable_turns=compactable_turns,
                recent_history=recent_history,
                session_state=session_state,
            )
            self.session_store.set_compaction_state(
                session_id,
                compacted_summary=summary_text,
                compaction_packet=compaction_packet,
            )

    async def _summarize_session_compaction(
        self,
        *,
        session_id: str,
        existing_summary: str | None,
        older_history: list[dict[str, Any]],
        recent_history: list[dict[str, Any]],
        compactable_turns: list[dict[str, Any]],
        session_state: dict[str, Any],
    ) -> str | None:
        older_lines: list[str] = []
        for item in older_history:
            role = self._safe_text(item.get("role")) or "unknown"
            content = self._bounded_excerpt(item.get("content"), limit=400)
            if not content:
                continue
            older_lines.append(f"[{role}] {content}")
            if sum(len(line) for line in older_lines) >= COMPACTION_RAW_MESSAGE_CHAR_LIMIT:
                break

        turn_lines = [
            f"- {self._safe_text(item.get('compact_line'))}"
            for item in compactable_turns
            if self._safe_text(item.get("compact_line"))
        ][:20]
        active_working_set = session_state.get("active_working_set")
        session_metadata = session_state.get("metadata") if isinstance(session_state.get("metadata"), dict) else {}
        current_tasks = (
            active_working_set.get("active_task_refs")
            if isinstance(active_working_set, dict)
            else session_metadata.get("active_task_refs")
        )

        system_prompt = (
            "You are compacting a COSMIC live session into durable operational context.\n"
            "Preserve only what helps the assistant continue the conversation intelligently.\n"
            "Do not include raw chain-of-thought, raw tool payloads, or chatter.\n"
            "Return concise Markdown using exactly these sections when relevant:\n"
            "## Goal\n"
            "## Active Workstreams\n"
            "## Key Facts\n"
            "## User Preferences\n"
            "## Decisions Made\n"
            "## Accomplished\n"
            "## Files / Docs / Artifacts Touched\n"
            "## Failures / Dead Ends\n"
            "## Open Loops\n"
            "## Next Best Actions"
        )
        user_prompt = (
            f"Session ID: {session_id}\n\n"
            f"Existing compacted summary:\n{existing_summary or '[none]'}\n\n"
            f"Compactable turn ledger:\n{chr(10).join(turn_lines) or '[none]'}\n\n"
            f"Older raw conversation slice:\n{chr(10).join(older_lines) or '[none]'}\n\n"
            f"Recent window retained uncompressed count: {len(recent_history)}\n"
            f"Active task refs: {', '.join(self._normalize_string_list(current_tasks)) or '[none]'}\n"
        )
        summary_text, _usage, _stop_reason = await self.haiku_adapter.generate_text(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1200,
        )
        normalized = summary_text.strip()
        return normalized or None

    def _build_compaction_packet(
        self,
        *,
        session_id: str,
        compacted_summary: str,
        compactable_turns: list[dict[str, Any]],
        recent_history: list[dict[str, Any]],
        session_state: dict[str, Any],
    ) -> dict[str, Any]:
        active_working_set = session_state.get("active_working_set") if isinstance(session_state.get("active_working_set"), dict) else {}
        goal = self._safe_text(active_working_set.get("goal")) or ""
        key_facts: list[str] = []
        preferences: list[str] = []
        decisions: list[str] = []
        accomplished: list[str] = []
        touched_entities = []
        failures: list[str] = []
        open_loops = self._normalize_string_list(active_working_set.get("open_loops"))
        compacted_until_completed_at = ""
        for turn in compactable_turns:
            if not goal:
                goal = self._safe_text(turn.get("user_goal")) or ""
            compacted_until_completed_at = (
                self._safe_text(turn.get("completed_at")) or compacted_until_completed_at
            )
            key_facts = self._normalize_string_list([*key_facts, *self._normalize_string_list(turn.get("facts_learned"))], limit=10)
            preferences = self._normalize_string_list([*preferences, *self._normalize_string_list(turn.get("preferences_detected"))], limit=10)
            decisions = self._normalize_string_list([*decisions, *self._normalize_string_list(turn.get("decisions_made"))], limit=10)
            accomplished = self._normalize_string_list([*accomplished, *self._normalize_string_list(turn.get("accomplished"))], limit=10)
            touched_entities = self._normalize_entity_list([*touched_entities, *self._normalize_entity_list(turn.get("touched_entities"), limit=12)], limit=12)
            failures = self._normalize_string_list([*failures, *self._normalize_string_list(turn.get("failures_to_avoid"))], limit=10)
            open_loops = self._normalize_string_list([*open_loops, *self._normalize_string_list(turn.get("open_loops"))], limit=10)
        next_best_actions = []
        if recent_history:
            next_best_actions.append("Resume from the recent uncompressed window before asking the user to repeat context.")
        return {
            "session_id": session_id,
            "goal": goal,
            "active_workstreams": self._normalize_string_list(active_working_set.get("active_workstreams"), limit=8),
            "key_facts": key_facts,
            "user_preferences": preferences,
            "decisions_made": decisions,
            "accomplished": accomplished,
            "touched_entities": touched_entities,
            "failures_to_avoid": failures,
            "open_loops": open_loops,
            "next_best_actions": next_best_actions,
            "summary_markdown": compacted_summary,
            "compacted_turn_count": len(compactable_turns),
            "compacted_until_completed_at": compacted_until_completed_at,
            "updated_at": utcnow_iso(),
        }

    def _build_carry_forward_packet(self, session_id: str) -> dict[str, Any]:
        session_state = self.get_session_state(session_id)
        active_working_set = session_state.get("active_working_set") if isinstance(session_state.get("active_working_set"), dict) else {}
        compaction_packet = session_state.get("compaction_packet") if isinstance(session_state.get("compaction_packet"), dict) else {}
        open_loops = self._normalize_string_list(
            [
                *self._normalize_string_list(compaction_packet.get("open_loops")),
                *self._normalize_string_list(active_working_set.get("open_loops")),
            ],
            limit=8,
        )
        active_task_refs = self._normalize_string_list(active_working_set.get("active_task_refs"), limit=8)
        bootstrap_note = self._safe_text(compaction_packet.get("summary_markdown")) or self._safe_text(session_state.get("compacted_summary")) or ""
        return {
            "goal": self._safe_text(active_working_set.get("goal")) or self._safe_text(compaction_packet.get("goal")) or "",
            "active_workstreams": self._normalize_string_list(
                [
                    *self._normalize_string_list(compaction_packet.get("active_workstreams")),
                    *self._normalize_string_list(active_working_set.get("active_workstreams")),
                ],
                limit=8,
            ),
            "open_loops": open_loops,
            "active_task_refs": active_task_refs,
            "current_focus_entities": self._normalize_entity_list(
                [
                    *self._normalize_entity_list(compaction_packet.get("touched_entities"), limit=8),
                    *self._normalize_entity_list(active_working_set.get("current_focus_entities"), limit=8),
                ],
                limit=8,
            ),
            "stable_user_preferences": self._normalize_string_list(
                [
                    *self._normalize_string_list(compaction_packet.get("user_preferences")),
                    *self._normalize_string_list(active_working_set.get("user_preferences_in_play")),
                ],
                limit=8,
            ),
            "failures_to_avoid": self._normalize_string_list(compaction_packet.get("failures_to_avoid"), limit=8),
            "bootstrap_note": self._bounded_excerpt(bootstrap_note, limit=400),
        }

    def _apply_carry_forward_packet(self, current_session_id: str, source_session_id: str, packet: dict[str, Any]) -> None:
        if not packet:
            return
        metadata = self.session_store.update_session_metadata(
            current_session_id,
            {
                "carry_forward_packet": packet,
                "carry_forward_source_session_id": source_session_id,
                "carry_forward_updated_at": utcnow_iso(),
            },
        )
        if not isinstance(metadata.get("active_working_set"), dict):
            self._ensure_session_state_seeded(current_session_id)

    async def _send_channel_activation_greetings(self) -> None:
        await self._maybe_send_whatsapp_activation_greeting()
        await self._maybe_send_telegram_activation_greeting()

    async def _maybe_send_whatsapp_activation_greeting(self, allowed_phone: str | None = None) -> None:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            return

        try:
            status = await adapter.get_status()  # type: ignore[attr-defined]
            config = await adapter.get_config()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("gateway.channel_activation whatsapp status/config lookup failed")
            return

        if not bool(status.get("connected")):
            return

        normalized_phone = self._safe_text(allowed_phone or config.get("allowed_phone"))
        if not normalized_phone:
            return

        await self._send_channel_activation_greeting(
            platform="whatsapp",
            channel="whatsapp:{0}".format(normalized_phone),
            channel_adapter=adapter,
            metadata={
                "connected": bool(status.get("connected")),
                "allowed_phone": normalized_phone,
            },
        )

    async def _maybe_send_telegram_activation_greeting(self) -> None:
        adapter = self.registry.adapters.get("telegram")
        if adapter is None:
            return

        try:
            status = await adapter.get_status()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("gateway.channel_activation telegram status lookup failed")
            return

        bot_status = status.get("bot")
        if isinstance(bot_status, dict) and bot_status.get("status") == "error":
            return

        allowed_chat_id = self._coerce_int(status.get("allowed_chat_id"))
        if allowed_chat_id is None:
            return

        await self._send_channel_activation_greeting(
            platform="telegram",
            channel="telegram:chat_{0}".format(allowed_chat_id),
            channel_adapter=adapter,
            metadata={
                "allowed_chat_id": allowed_chat_id,
                "allowed_user_id": self._coerce_int(status.get("allowed_user_id")),
            },
        )

    async def _send_channel_activation_greeting(
        self,
        *,
        platform: str,
        channel: str,
        channel_adapter: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        welcome_message = CHANNEL_WELCOME_MESSAGES.get(platform)
        if not welcome_message:
            return

        claimed = self.session_store.claim_channel_greeting(
            channel=channel,
            platform=platform,
            metadata=metadata,
        )
        if not claimed:
            return

        session_id = self._current_session_id()
        request_id = "channel_welcome_{0}".format(uuid4().hex)
        try:
            await channel_adapter.send(
                {
                    "type": "channel.welcome",
                    "request_id": request_id,
                    "session_id": session_id,
                    "channel": channel,
                    "content": welcome_message,
                    "platform": platform,
                },
                channel=channel,
            )
        except Exception:
            self.session_store.release_channel_greeting_claim(channel)
            logger.exception(
                "gateway.channel_activation failed platform=%s channel=%s",
                platform,
                channel,
            )
            return

        self._append_session_message(
            session_id,
            role="assistant",
            content=welcome_message,
            route="system",
            awaiting_reply=False,
            channel=channel,
            metadata={
                "system_generated": True,
                "channel_welcome": True,
                "platform": platform,
                "request_id": request_id,
            },
        )
        self.session_store.mark_channel_greeting_sent(channel)

    async def build_resume_payload(
        self,
        *,
        channel: str,
        request_id: str | None = None,
        requested_session_id: str | None = None,
        known_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        session_id = self._resolve_session_id(requested_session_id)
        history_tail = self.session_store.get_history_tail(session_id, limit=30)
        pending_inputs = self._pending_inputs_for_channel(channel, session_id=session_id)
        active_tasks = await self._active_task_summaries(session_id=session_id, channel=channel)
        return {
            "type": "resume.ok",
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "user_timezone": self.current_user_timezone(),
            "history_tail": history_tail,
            "active_tasks": active_tasks,
            "pending_inputs": pending_inputs,
        }

    def notify_channel_active(self, channel: str | None) -> None:
        if not channel:
            return
        self._delivery_wakeup.set()
        if self._redis is not None:
            self._track_background_task(self._drain_pending_task_inputs(channel))

    def _track_background_task(self, coroutine: asyncio.Future[Any] | asyncio.Task[Any] | Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def submit_task_input_reply(
        self,
        *,
        input_request_id: str,
        task_id: str,
        content: str,
        channel: str,
    ) -> dict[str, Any]:
        if self._redis is None:
            raise RuntimeError("REDIS_URL is not configured on the Gateway VM.")
        normalized_input_request_id = self._safe_text(input_request_id)
        normalized_task_id = self._safe_text(task_id)
        normalized_content = self._safe_text(content)
        normalized_channel = self._safe_text(channel)
        if not normalized_input_request_id or not normalized_task_id or not normalized_content or not normalized_channel:
            raise ValueError("task.input_reply requires input_request_id, task_id, content, and channel")

        resolved = self.session_store.get_task_input_request(normalized_input_request_id)
        if resolved is None:
            raise ValueError("Unknown input_request_id")
        if self._safe_text(resolved.get("status")) != "pending":
            raise ValueError("input_request_id is no longer pending")
        if self._safe_text(resolved.get("task_id")) != normalized_task_id:
            raise ValueError("task_id does not match the pending input request")
        if self._safe_text(resolved.get("channel")) != normalized_channel:
            raise ValueError("channel does not match the pending input request")

        reply_payload = {
            "input_request_id": normalized_input_request_id,
            "task_id": normalized_task_id,
            "content": normalized_content,
            "channel": normalized_channel,
            "timestamp": utcnow_iso(),
        }
        await self._redis.xadd(
            self.config.task_input_replies_stream,
            {"payload": json.dumps(reply_payload, ensure_ascii=False)},
        )
        self.session_store.mark_task_input_request_replied(
            input_request_id=normalized_input_request_id,
            content=normalized_content,
        )
        return reply_payload

    async def _task_input_consumer_loop(self) -> None:
        assert self._redis is not None
        consumer_name = "gateway-{0}".format(id(self))
        while True:
            entries = await self._redis.xreadgroup(
                groupname=self.config.task_input_gateway_group,
                consumername=consumer_name,
                streams={self.config.task_input_requests_stream: ">"},
                count=5,
                block=1000,
            )
            for _stream, messages in entries:
                for message_id, data in messages:
                    await self._handle_task_input_stream_message(message_id, data)

    async def _handle_task_input_stream_message(self, message_id: str, data: dict[str, Any]) -> None:
        assert self._redis is not None
        try:
            request = parse_stream_payload(data)
            input_request_id = self._safe_text(request.get("input_request_id"))
            task_id = self._safe_text(request.get("task_id"))
            question = self._safe_text(request.get("question"))
            if not input_request_id or not task_id or not question:
                raise ValueError("Task input request is missing required fields.")
            channel = self._resolve_task_input_channel(request, task_id=task_id)
            if not channel:
                return
            event = {
                "type": "task.input_required",
                "input_request_id": input_request_id,
                "task_id": task_id,
                "session_id": self._safe_text(request.get("session_id")),
                "agent": self._safe_text(request.get("agent")),
                "channel": channel,
                "question": question,
                "options": [str(item) for item in request.get("options", []) if str(item).strip()],
                "status": self._safe_text(request.get("status")) or "pending",
                "timestamp": self._safe_text(request.get("timestamp")) or utcnow_iso(),
            }
            self._persist_task_input_request(event)
            await self._send_channel_event_now(event, channel)
            await self._redis.xack(
                self.config.task_input_requests_stream,
                self.config.task_input_gateway_group,
                message_id,
            )
        except ChannelUnavailableError:
            return
        except Exception:
            logger.exception("gateway.task_input_consumer_failed msg_id=%s", message_id)
            await self._redis.xack(
                self.config.task_input_requests_stream,
                self.config.task_input_gateway_group,
                message_id,
            )

    async def _drain_pending_task_inputs(self, channel: str) -> None:
        if self._redis is None:
            return
        consumer_name = "gateway-{0}".format(id(self))
        while True:
            entries = await self._redis.xreadgroup(
                groupname=self.config.task_input_gateway_group,
                consumername=consumer_name,
                streams={self.config.task_input_requests_stream: "0"},
                count=50,
            )
            if not entries:
                return
            delivered_any = False
            for _stream, messages in entries:
                for message_id, data in messages:
                    try:
                        request = parse_stream_payload(data)
                        task_id = self._safe_text(request.get("task_id")) or ""
                        request_channel = self._resolve_task_input_channel(request, task_id=task_id)
                        if request_channel != channel:
                            continue
                        await self._handle_task_input_stream_message(message_id, data)
                        delivered_any = True
                    except Exception:
                        continue
            if not delivered_any:
                return

    def _resolve_task_input_channel(self, request: dict[str, Any], *, task_id: str) -> str | None:
        explicit_channel = self._safe_text(request.get("channel"))
        if explicit_channel:
            return explicit_channel
        return self.active_task_channels.get(task_id)

    def _persist_task_input_request(self, event: dict[str, Any]) -> None:
        input_request_id = self._safe_text(event.get("input_request_id"))
        task_id = self._safe_text(event.get("task_id"))
        session_id = self._safe_text(event.get("session_id"))
        channel = self._safe_text(event.get("channel"))
        question = self._safe_text(event.get("question"))
        if not input_request_id or not task_id or not session_id or not channel or not question:
            return
        self.session_store.upsert_task_input_request(
            input_request_id=input_request_id,
            task_id=task_id,
            session_id=session_id,
            channel=channel,
            question=question,
            options=event.get("options") if isinstance(event.get("options"), list) else [],
            agent=self._safe_text(event.get("agent")),
            metadata={
                "timestamp": self._safe_text(event.get("timestamp")),
            },
            status=self._safe_text(event.get("status")) or "pending",
            created_at=self._safe_text(event.get("timestamp")),
        )

    async def deliver_channel_event(self, event: dict[str, Any]) -> None:
        channel = self._safe_text(event.get("channel"))
        await self._deliver_or_queue_channel_event(event, channel=channel)

    def _pending_inputs_for_channel(self, channel: str, *, session_id: str | None = None) -> list[dict[str, Any]]:
        pending = self.session_store.list_pending_task_inputs(session_id=session_id, channel=channel, limit=50)
        persisted = self.delivery_queue_store.list_pending_inputs(channel)
        if not persisted:
            return pending

        seen: set[tuple[str | None, str | None]] = set()
        merged: list[dict[str, Any]] = []
        for item in pending + persisted:
            key = (
                self._safe_text(item.get("task_id")),
                self._safe_text(item.get("input_request_id")) or self._safe_text(item.get("request_id")),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    async def _maybe_handle_pending_task_input_reply(
        self,
        *,
        content: str,
        channel: str,
        session_id: str,
        request_id: str,
        source_id: str,
        metadata: dict[str, Any],
        normalized_message: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._redis is None:
            return None
        if not content:
            return None
        if channel.startswith("desktop:"):
            return None
        pending_inputs = self.session_store.list_pending_task_inputs(
            session_id=session_id,
            channel=channel,
            limit=2,
        )
        if not pending_inputs:
            return None
        target = pending_inputs[0]
        input_request_id = self._safe_text(target.get("input_request_id"))
        task_id = self._safe_text(target.get("task_id"))
        if not input_request_id or not task_id:
            return None

        self._append_session_message(
            session_id,
            role="user",
            content=content,
            channel=channel,
            metadata={
                "request_id": request_id,
                "platform": metadata.get("platform"),
                "message_type": metadata.get("message_type"),
                "attachments": metadata.get("attachments"),
                "task_input_reply_for": input_request_id,
                "task_id": task_id,
            },
        )
        reply_payload = await self.submit_task_input_reply(
            input_request_id=input_request_id,
            task_id=task_id,
            content=content,
            channel=channel,
        )
        result = {
            "status": "accepted",
            "request_id": request_id,
            "session_id": session_id,
            "source": "user",
            "source_id": source_id,
            "channel": channel,
            "route": "task_input_reply",
            "dispatch_target": "redis",
            "classification": {
                "route": "task_input_reply",
                "needs_latest": False,
                "needs_citations": False,
                "is_task": True,
                "is_continuation": True,
                "confidence": 1.0,
                "signals": ["pending_task_input_reply"],
            },
            "message": normalized_message,
            "assembled_conversation_context": self._build_conversation_context(
                session_id,
                fallback_context=normalized_message.get("conversation_context"),
            ),
            "memory_context": None,
            "active_working_set": None,
            "carry_forward_packet": None,
            "memory_context_payload": None,
            "routing_decision_source": "pending_task_input_reply",
            "input_artifacts": [],
            "task_input_reply": reply_payload,
            "accepted_at": utcnow_iso(),
        }
        self.routing_audit_store.append(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            source="user",
            source_id=source_id,
            query_text=content,
            route_override=None,
            sticky_hit=False,
            decision_source="pending_task_input_reply",
            classifier_route=None,
            final_route="task_input_reply",
            dispatch_target="redis",
            confidence=1.0,
            signals=["pending_task_input_reply"],
            conversation_context=result["assembled_conversation_context"],
            classifier_payload=None,
            classifier_metrics=None,
            classifier_model=None,
            classifier_latency_ms=None,
            decision_latency_ms=0.0,
            error_text=None,
        )
        return result

    async def _delivery_worker_loop(self) -> None:
        while True:
            processed = await self._process_pending_deliveries_once()
            if processed > 0:
                continue
            try:
                await asyncio.wait_for(self._delivery_wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            finally:
                self._delivery_wakeup.clear()

    async def _process_pending_deliveries_once(self, *, limit: int = 25) -> int:
        deliveries = self.delivery_queue_store.fetch_due(limit=limit)
        for record in deliveries:
            await self._attempt_pending_delivery(record)
        return len(deliveries)

    async def _attempt_pending_delivery(self, record: dict[str, Any]) -> None:
        delivery_id = self._safe_text(record.get("delivery_id")) or ""
        channel = self._safe_text(record.get("channel"))
        payload = record.get("payload")
        attempts = int(record.get("attempts") or 0)
        event_type = self._safe_text(record.get("event_type")) or "unknown"
        if not delivery_id or not channel or not isinstance(payload, dict):
            return

        try:
            await self._send_channel_event_now(payload, channel)
        except PermanentDeliveryError as exc:
            self.delivery_queue_store.mark_dead_letter(
                delivery_id,
                attempts=attempts + 1,
                last_error=str(exc),
            )
            logger.warning(
                "gateway.delivery deadlettered delivery_id=%s channel=%s event_type=%s reason=%s",
                delivery_id,
                channel,
                event_type,
                exc,
            )
            return
        except Exception as exc:
            next_attempts = attempts + 1
            if next_attempts >= self.config.delivery_max_attempts:
                self.delivery_queue_store.mark_dead_letter(
                    delivery_id,
                    attempts=next_attempts,
                    last_error=str(exc),
                )
                logger.warning(
                    "gateway.delivery deadlettered delivery_id=%s channel=%s event_type=%s attempts=%s reason=%s",
                    delivery_id,
                    channel,
                    event_type,
                    next_attempts,
                    exc,
                )
                return

            self.delivery_queue_store.reschedule(
                delivery_id,
                next_attempts=next_attempts,
                available_at=self._delivery_available_at(next_attempts),
                last_error=str(exc),
            )
            logger.info(
                "gateway.delivery rescheduled delivery_id=%s channel=%s event_type=%s attempts=%s reason=%s",
                delivery_id,
                channel,
                event_type,
                next_attempts,
                exc,
            )
            return

        self.delivery_queue_store.mark_delivered(delivery_id)
        await self._maybe_schedule_delivered_memory_ingest(payload, delivery_status="sent")
        await self._maybe_schedule_delivered_task_summary_write(payload, delivery_status="sent")
        logger.info(
            "gateway.delivery delivered delivery_id=%s channel=%s event_type=%s",
            delivery_id,
            channel,
            event_type,
        )

    async def _deliver_or_queue_channel_event(
        self,
        event: dict[str, Any],
        *,
        channel: str | None = None,
    ) -> str:
        resolved_channel = self._safe_text(channel or event.get("channel"))
        if not resolved_channel:
            raise ValueError("Outbound event is missing channel")
        event_type = self._safe_text(event.get("type")) or "unknown"
        dedupe_key = self._delivery_dedupe_key(event, resolved_channel)

        try:
            await self._send_channel_event_now(event, resolved_channel)
            return "sent"
        except PermanentDeliveryError as exc:
            if dedupe_key is not None:
                self.delivery_queue_store.mark_dead_letter(
                    self.delivery_queue_store.enqueue(
                        dedupe_key=dedupe_key,
                        channel=resolved_channel,
                        event_type=event_type,
                        payload={**event, "channel": resolved_channel},
                        last_error=str(exc),
                    ),
                    attempts=1,
                    last_error=str(exc),
                )
            logger.warning(
                "gateway.delivery permanent_failure channel=%s event_type=%s reason=%s",
                resolved_channel,
                event_type,
                exc,
            )
            return "deadlettered"
        except Exception as exc:
            if dedupe_key is None:
                logger.info(
                    "gateway.delivery dropped_ephemeral channel=%s event_type=%s reason=%s",
                    resolved_channel,
                    event_type,
                    exc,
                )
                return "dropped"

            self.delivery_queue_store.enqueue(
                dedupe_key=dedupe_key,
                channel=resolved_channel,
                event_type=event_type,
                payload={**event, "channel": resolved_channel},
                last_error=str(exc),
            )
            self._delivery_wakeup.set()
            logger.info(
                "gateway.delivery queued channel=%s event_type=%s reason=%s",
                resolved_channel,
                event_type,
                exc,
            )
            return "queued"

    async def _send_channel_event_now(self, event: dict[str, Any], channel: str) -> None:
        adapter = self.registry.get_adapter(channel)
        if adapter is None:
            raise ChannelUnavailableError(f"No adapter registered for channel: {channel!r}")
        await adapter.send(event, channel=channel)

    def _delivery_dedupe_key(self, event: dict[str, Any], channel: str) -> str | None:
        event_type = self._safe_text(event.get("type")) or ""
        if event_type in EPHEMERAL_CHANNEL_EVENT_TYPES:
            return None

        request_id = self._safe_text(event.get("request_id"))
        task_id = self._safe_text(event.get("task_id"))
        session_id = self._safe_text(event.get("session_id"))

        if event_type == "response.complete":
            if channel.startswith("desktop:"):
                return None
            if request_id:
                return f"{channel}:{event_type}:{request_id}"
            return None

        if event_type == "task.input_required":
            identifier = (
                self._safe_text(event.get("input_request_id"))
                or task_id
                or request_id
                or session_id
            )
            return f"{channel}:{event_type}:{identifier}" if identifier else self._event_fingerprint(event, channel)

        if event_type == "task.completed":
            if not self._task_completed_has_visible_output(event):
                return None
            identifier = task_id or request_id or session_id
            return f"{channel}:{event_type}:{identifier}" if identifier else self._event_fingerprint(event, channel)

        if event_type in {"task.failed", "task.cancelled", "error"}:
            identifier = task_id or request_id or session_id
            return f"{channel}:{event_type}:{identifier}" if identifier else self._event_fingerprint(event, channel)

        return None

    def _task_completed_has_visible_output(self, event: dict[str, Any]) -> bool:
        result = event.get("result")
        if isinstance(result, dict):
            for key in ("content", "text", "summary", "message"):
                if self._safe_text(result.get(key)):
                    return True
        elif isinstance(result, str) and result.strip():
            return True
        return bool(self._safe_text(event.get("content")) or self._safe_text(event.get("message")))

    def _event_fingerprint(self, event: dict[str, Any], channel: str) -> str:
        payload = {
            "channel": channel,
            "event": event,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return f"{channel}:fingerprint:{digest}"

    def _delivery_available_at(self, attempts: int) -> str:
        backoff = min(
            self.config.delivery_retry_max_sec,
            self.config.delivery_retry_base_sec * (2 ** max(0, attempts - 1)),
        )
        if backoff <= 0:
            return utcnow_iso()
        return (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat().replace("+00:00", "Z")

    def list_channels(self) -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        for platform in sorted(self.registry.adapters):
            channels.append(
                {
                    "platform": platform,
                    "configured": True,
                    "healthy": platform not in self.adapter_errors,
                    "last_error": self.adapter_errors.get(platform),
                }
            )
        return channels

    async def get_channel_status(self, platform: str) -> dict[str, Any]:
        adapter = self.registry.adapters.get(platform)
        if adapter is None:
            raise KeyError(platform)

        if platform == "whatsapp":
            try:
                status = await adapter.get_status()  # type: ignore[attr-defined]
                self.adapter_errors.pop(platform, None)
            except Exception as exc:
                self.adapter_errors[platform] = str(exc)
                status = {"status": "error", "error": str(exc)}
            return {
                "platform": platform,
                "configured": True,
                "healthy": platform not in self.adapter_errors,
                "last_error": self.adapter_errors.get(platform),
                "bridge": status,
            }

        if platform == "telegram":
            try:
                status = await adapter.get_status()  # type: ignore[attr-defined]
                self.adapter_errors.pop(platform, None)
            except Exception as exc:
                self.adapter_errors[platform] = str(exc)
                status = {"status": "error", "error": str(exc)}
            return {
                "platform": platform,
                "configured": True,
                "healthy": platform not in self.adapter_errors,
                "last_error": self.adapter_errors.get(platform),
                "bot": status,
            }

        if platform == "desktop":
            status = await adapter.get_status()  # type: ignore[attr-defined]
            return {
                "platform": platform,
                "configured": True,
                "healthy": True,
                "last_error": None,
                "connection": status,
            }

        return {
            "platform": platform,
            "configured": True,
            "healthy": platform not in self.adapter_errors,
            "last_error": self.adapter_errors.get(platform),
        }

    async def request_whatsapp_pairing_qr(
        self,
        *,
        refresh: bool = True,
        wait_timeout_ms: int = 15000,
    ) -> dict[str, Any]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.request_pairing_qr(  # type: ignore[attr-defined]
            refresh=refresh,
            wait_timeout_ms=wait_timeout_ms,
        )
        self.adapter_errors.pop("whatsapp", None)
        return response

    async def clear_whatsapp_session(self) -> dict[str, Any]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.clear_session()  # type: ignore[attr-defined]
        self.adapter_errors.pop("whatsapp", None)
        return response

    async def get_whatsapp_config(self) -> dict[str, Any]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.get_config()  # type: ignore[attr-defined]
        self.adapter_errors.pop("whatsapp", None)
        return response

    async def update_whatsapp_config(
        self,
        *,
        allowed_phone: str | None = None,
        self_chat_only: bool | None = None,
    ) -> dict[str, Any]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.update_config(  # type: ignore[attr-defined]
            allowed_phone=allowed_phone,
            self_chat_only=self_chat_only,
        )
        self.adapter_errors.pop("whatsapp", None)
        await self._maybe_send_whatsapp_activation_greeting(
            allowed_phone=self._safe_text(response.get("allowed_phone")) or allowed_phone
        )
        return response

    async def send_whatsapp_test(
        self,
        *,
        number: str,
        message: str,
    ) -> dict[str, Any]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.send_test_message(  # type: ignore[attr-defined]
            number=number,
            message=message,
        )
        self.adapter_errors.pop("whatsapp", None)
        return response

    async def sync_telegram_webhook(self) -> dict[str, Any]:
        adapter = self.registry.adapters.get("telegram")
        if adapter is None:
            raise KeyError("telegram")
        response = await adapter.sync_webhook()  # type: ignore[attr-defined]
        self.adapter_errors.pop("telegram", None)
        await self._maybe_send_telegram_activation_greeting()
        return response

    async def clear_telegram_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        adapter = self.registry.adapters.get("telegram")
        if adapter is None:
            raise KeyError("telegram")
        response = await adapter.delete_webhook(  # type: ignore[attr-defined]
            drop_pending_updates=drop_pending_updates
        )
        self.adapter_errors.pop("telegram", None)
        return response

    async def send_telegram_test(
        self,
        *,
        chat_id: int,
        message: str,
    ) -> dict[str, Any]:
        adapter = self.registry.adapters.get("telegram")
        if adapter is None:
            raise KeyError("telegram")
        response = await adapter.send_test_message(  # type: ignore[attr-defined]
            chat_id=chat_id,
            message=message,
        )
        self.adapter_errors.pop("telegram", None)
        return response

    async def download_telegram_media(self, file_id: str) -> tuple[bytes, str | None]:
        adapter = self.registry.adapters.get("telegram")
        if adapter is None:
            raise KeyError("telegram")
        response = await adapter.download_file(file_id)  # type: ignore[attr-defined]
        self.adapter_errors.pop("telegram", None)
        return response

    async def health_payload(self) -> dict[str, Any]:
        memory = dict(self._memory_health_snapshot)
        memory.setdefault("enabled", self.memory_client.enabled)
        return {
            "status": self._health_status_from_memory(memory),
            "model_router_url": self.config.model_router_url,
            "orchestrator_url": self.config.orchestrator_url,
            "cosmic_memory_url": self.config.cosmic_memory_url,
            "memory": memory,
            "channels": self.list_channels(),
            "current_session_id": self._current_session_id(),
            "delivery_queue": self.delivery_queue_store.summary(),
            "scheduler": self.scheduler_store.summary(),
        }

    async def readiness_payload(self) -> dict[str, Any]:
        healthy_channels = [item for item in self.list_channels() if item["healthy"]]
        memory = dict(self._memory_health_snapshot)
        memory.setdefault("enabled", self.memory_client.enabled)
        ready = self.started and memory.get("status") not in {"starting", "error"}
        return {
            "status": "ready" if ready else ("starting" if not self.started else "degraded"),
            "gateway_started": self.started,
            "healthy_channel_count": len(healthy_channels),
            "adapter_errors": self.adapter_errors,
            "orchestrator_url": self.config.orchestrator_url,
            "cosmic_memory_url": self.config.cosmic_memory_url,
            "memory": memory,
            "delivery_queue": self.delivery_queue_store.summary(),
            "scheduler": self.scheduler_store.summary(),
        }

    async def _active_task_summaries(self, *, session_id: str, channel: str) -> list[dict[str, Any]]:
        try:
            tasks = await self.orchestrator.list_active_tasks(session_id=session_id, channel=channel)
        except Exception:
            return []
        return tasks

    def _build_orchestrator_task(
        self,
        *,
        request_record: dict[str, Any],
        session_id: str,
        request_id: str,
        channel: str,
    ) -> TaskEnvelope:
        if not self.config.signing_secret:
            raise RuntimeError("GATEWAY_SIGNING_SECRET is not configured on the Gateway VM.")

        message = request_record.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Request record is missing the normalized message payload.")

        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient="cosmic/orchestrator:1.0.0",
            intent="orchestrator.process",
            input={
                "query": self._safe_text(message.get("content")) or "[empty message]",
                "request_id": request_id,
                "conversation_context": request_record.get("assembled_conversation_context") or [],
                "memory_context": self._safe_text(request_record.get("memory_context")),
            },
            input_artifacts=request_record.get("input_artifacts") or [],
            idempotency_key=uuid4().hex,
            priority=SOURCE_PRIORITY_MAP.get(self._safe_text(request_record.get("source")) or "user", "normal"),
            signature="",
            created_at=utcnow(),
            source=self._safe_text(request_record.get("source")) or "user",
            source_id=self._safe_text(request_record.get("source_id")),
            channel=channel,
        )
        signature = sign_task_envelope(task, self.config.signing_secret)
        return task.model_copy(update={"signature": signature})

    def _persist_inbound_artifacts(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return []
        try:
            return self.artifact_store.persist_inbound_attachments(
                request_id=request_id,
                session_id=session_id,
                source_channel=channel,
                source_platform=self._safe_text(metadata.get("platform")),
                source_message_id=self._safe_text(metadata.get("message_id")),
                attachments=attachments,
            )
        except Exception:
            logger.exception(
                "gateway.artifact_persist_failed request_id=%s channel=%s attachment_count=%s",
                request_id,
                channel,
                len(attachments),
            )
            return []

    async def _handle_orchestrator_event(
        self,
        event: dict[str, Any],
        *,
        send,
        store_assistant_message,
    ) -> None:
        event_type = self._safe_text(event.get("type")) or ""
        request_id = self._safe_text(event.get("request_id"))
        task_id = self._safe_text(event.get("task_id"))
        session_id = self._safe_text(event.get("session_id"))
        if request_id and task_id:
            self.active_requests_by_task[task_id] = request_id
            active_request = self.active_requests.get(request_id)
            if active_request is not None:
                active_request.task_id = task_id
        if event_type == "response.complete":
            store_assistant_message(
                str(event.get("content") or ""),
                awaiting_reply=bool(event.get("awaiting_reply")),
                metadata={
                    "task_id": self._safe_text(event.get("task_id")),
                    "metrics": event.get("metrics"),
                    "thinking_text": self._safe_text(event.get("thinking_text")),
                },
                channel=self._safe_text(event.get("channel")) or "",
                route="opus",
            )
        elif event_type == "task.input_required":
            channel = self._safe_text(event.get("channel"))
            if channel and task_id and session_id:
                self._persist_task_input_request(event)
        elif event_type in {"task.completed", "task.failed", "task.cancelled"}:
            if task_id:
                self.active_task_channels.pop(task_id, None)
                self.active_requests_by_task.pop(task_id, None)

        if task_id and session_id and event_type.startswith("task."):
            notebook = self._merge_task_notebook(
                task_id=task_id,
                session_id=session_id,
                request_id=request_id,
                event=event,
            )
            self.session_store.upsert_task_notebook(task_id, session_id, notebook)
            self._refresh_active_working_set(session_id)

        await send(event)

    def _normalize_orchestrator_event(
        self,
        event: dict[str, Any],
        *,
        task_id: str,
        request_id: str,
        session_id: str,
        channel: str,
    ) -> dict[str, Any]:
        normalized = dict(event)
        normalized.setdefault("task_id", task_id)
        normalized.setdefault("request_id", request_id)
        normalized.setdefault("session_id", session_id)
        normalized.setdefault("channel", channel)
        return normalized

    def _safe_text(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return str(value)

    def _normalize_string_list(self, values: Any, *, limit: int = 8) -> list[str]:
        if not isinstance(values, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = self._safe_text(value)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            normalized.append(text)
            seen.add(key)
            if len(normalized) >= limit:
                break
        return normalized

    def _normalize_entity_list(self, values: Any, *, limit: int = 8) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            entity_type = self._safe_text(item.get("type")) or "entity"
            entity_id = self._safe_text(item.get("id")) or self._safe_text(item.get("path")) or self._safe_text(item.get("url")) or self._safe_text(item.get("label"))
            if not entity_id:
                continue
            dedupe = f"{entity_type}:{entity_id}".casefold()
            if dedupe in seen:
                continue
            normalized.append(
                {
                    "type": entity_type,
                    "id": entity_id,
                    "label": self._safe_text(item.get("label")) or entity_id,
                }
            )
            seen.add(dedupe)
            if len(normalized) >= limit:
                break
        return normalized

    def _bounded_excerpt(self, value: Any, *, limit: int = 280) -> str:
        text = " ".join(str(value or "").strip().split())
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _coerce_int(self, value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_route(self, route: str) -> str:
        normalized = route.strip().lower()
        if normalized == "gemini":
            return "haiku"
        if normalized in {"opus", "haiku", "perplexity"}:
            return normalized
        return "opus"

    def _normalize_route_override(self, route_override: Any) -> str | None:
        route = self._safe_text(route_override)
        if route is None:
            return None
        normalized = route.strip().lower()
        if normalized in {"cosmic", "auto", "default"}:
            return None
        if normalized == "gemini":
            return "haiku"
        if normalized in {"opus", "haiku", "perplexity"}:
            return normalized
        return None

    async def _run_request_fulfillment(
        self,
        state: ActiveRequest,
        request_record: dict[str, Any],
    ) -> None:
        try:
            await self.fulfill_processed_message(request_record)
        except asyncio.CancelledError:
            if state.cancel_requested:
                await self._emit_cancelled_event(state)
                return
            raise
        except Exception as exc:
            await self._deliver_or_queue_channel_event(
                {
                    "type": "error",
                    "request_id": state.request_id,
                    "session_id": state.session_id,
                    "task_id": state.task_id,
                    "channel": state.channel,
                    "code": "UPSTREAM_ERROR",
                    "message": str(exc),
                },
                channel=state.channel,
            )
        finally:
            if state.cancel_requested and state.partial_content and not state.completed:
                self._append_session_message(
                    state.session_id,
                    role="assistant",
                    content=state.partial_content,
                    route=state.route,
                    channel=state.channel,
                    metadata={
                        "thinking_text": state.partial_thinking or None,
                        "interrupted": True,
                    },
                )
            self._finalize_active_request(state)

    async def _emit_cancelled_event(self, state: ActiveRequest) -> None:
        await self._deliver_or_queue_channel_event(
            {
                "type": "task.cancelled",
                "request_id": state.request_id,
                "session_id": state.session_id,
                "task_id": state.task_id,
                "route": state.route,
                "channel": state.channel,
                "status": "cancelled",
                "message": "Response stopped.",
            },
            channel=state.channel,
        )

    def _finalize_active_request(self, state: ActiveRequest) -> None:
        current = self.active_requests.get(state.request_id)
        if current is state:
            self.active_requests.pop(state.request_id, None)
        self.request_records.pop(state.request_id, None)
        if state.task_id:
            bound_request_id = self.active_requests_by_task.get(state.task_id)
            if bound_request_id == state.request_id:
                self.active_requests_by_task.pop(state.task_id, None)

    def _track_partial_stream(self, state: ActiveRequest, event: dict[str, Any]) -> None:
        event_type = self._safe_text(event.get("type")) or ""
        if event_type == "response.thinking.chunk":
            state.partial_thinking += str(event.get("content") or "")
            return
        if event_type == "response.chunk":
            state.partial_content += str(event.get("content") or "")
            return
        if event_type == "response.complete":
            state.completed = True
            state.partial_content = str(event.get("content") or state.partial_content)
            thinking_text = self._safe_text(event.get("thinking_text"))
            if thinking_text is not None:
                state.partial_thinking = thinking_text

    def _health_status_from_memory(self, memory: dict[str, Any]) -> str:
        if not self.started:
            return "starting"
        if memory.get("status") == "error":
            return "degraded"
        return "ok"

    async def _refresh_memory_health(self) -> None:
        if not self.memory_client.enabled:
            self._memory_health_snapshot = {
                "enabled": False,
                "status": "disabled",
                "checked_at": utcnow_iso(),
            }
            return

        try:
            payload = await self.memory_client.health()
        except Exception as exc:  # pragma: no cover - defensive network guard
            payload = {
                "enabled": True,
                "status": "error",
                "error": str(exc),
            }

        snapshot = payload if isinstance(payload, dict) else {"enabled": True, "status": "ok"}
        snapshot.setdefault("enabled", True)
        snapshot["checked_at"] = utcnow_iso()
        self._memory_health_snapshot = snapshot

    async def _memory_health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(MEMORY_HEALTH_REFRESH_SEC)
                await self._refresh_memory_health()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive loop guard
                logger.exception("gateway.memory_health_loop_failed")
                self._memory_health_snapshot = {
                    "enabled": self.memory_client.enabled,
                    "status": "error" if self.memory_client.enabled else "disabled",
                    "error": "memory health refresh loop failed",
                    "checked_at": utcnow_iso(),
                }

    async def _finalize_rollover_sessions(self, current_session_id: str | None = None) -> None:
        resolved_current_session_id = current_session_id or self._current_session_id()
        async with self._rollover_finalize_lock:
            candidates = self.session_store.list_rollover_candidates(
                current_session_id=resolved_current_session_id
            )
            latest_carry_forward: tuple[str, dict[str, Any]] | None = None
            for candidate in candidates:
                carry_forward = await self._finalize_single_rollover_session(candidate)
                candidate_session_id = self._safe_text(candidate.get("session_id"))
                if carry_forward and candidate_session_id:
                    latest_carry_forward = (candidate_session_id, carry_forward)
            if latest_carry_forward is not None:
                source_session_id, packet = latest_carry_forward
                self._apply_carry_forward_packet(
                    resolved_current_session_id,
                    source_session_id,
                    packet,
                )

    async def _finalize_single_rollover_session(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        session_id = self._safe_text(candidate.get("session_id"))
        if not session_id:
            return None

        history = self.session_store.get_history(session_id)
        if not history:
            self.session_store.mark_session_rollover_finalized(
                session_id,
                summary_status="empty",
            )
            return None

        transcript_markdown = self._render_session_transcript_markdown(candidate, history)
        transcript_path = self._write_session_transcript(session_id, transcript_markdown)

        summary_text: str | None = None
        summary_status = "skipped"
        summary_memory_id: str | None = None

        try:
            summary_text = await self._summarize_completed_session(
                session_id=session_id,
                transcript_markdown=transcript_markdown,
                history=history,
            )
        except Exception:
            logger.exception("gateway.session_rollover_summary_failed session_id=%s", session_id)
            summary_status = "summary_failed"
        else:
            if summary_text:
                summary_status = "generated"
                if self.memory_client.enabled:
                    try:
                        memory_response = await self.memory_client.write_memory(
                            self._build_session_summary_memory_payload(
                                session_id=session_id,
                                transcript_path=transcript_path,
                                summary_text=summary_text,
                                history=history,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "gateway.session_rollover_memory_write_failed session_id=%s",
                            session_id,
                        )
                        summary_status = "memory_write_failed"
                    else:
                        summary_memory_id = self._extract_memory_id(memory_response)
                        summary_status = "stored"

        self.session_store.mark_session_rollover_finalized(
            session_id,
            transcript_path=transcript_path,
            summary_memory_id=summary_memory_id,
            summary_status=summary_status,
            compacted_summary=summary_text,
        )
        carry_forward_packet = self._build_carry_forward_packet(session_id)
        self.session_store.update_session_metadata(
            session_id,
            {"carry_forward_packet": carry_forward_packet},
        )
        return carry_forward_packet

    def _render_session_transcript_markdown(
        self,
        candidate: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> str:
        session_id = self._safe_text(candidate.get("session_id")) or "unknown_session"
        created_at = self._safe_text(candidate.get("created_at")) or ""
        updated_at = self._safe_text(candidate.get("updated_at")) or ""
        lines = [
            f"# Session Transcript: {session_id}",
            "",
            f"- Created: {created_at}",
            f"- Updated: {updated_at}",
            f"- Message count: {len(history)}",
            "",
        ]

        for item in history:
            role = str(item.get("role") or "unknown").strip().capitalize()
            channel = self._safe_text(item.get("channel"))
            route = self._safe_text(item.get("route"))
            created = self._safe_text(item.get("created_at"))
            meta_parts: list[str] = []
            if channel:
                meta_parts.append(f"channel `{channel}`")
            if route and str(item.get("role") or "") == "assistant":
                meta_parts.append(f"route `{route}`")
            if created:
                meta_parts.append(created)

            lines.append(f"## {role}")
            if meta_parts:
                lines.append("_" + " | ".join(meta_parts) + "_")
            lines.append("")
            lines.append(str(item.get("content") or "").strip() or "[empty]")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

    def _write_session_transcript(self, session_id: str, transcript_markdown: str) -> str:
        transcript_dir = self.config.session_transcript_dir
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"{session_id}.md"
        transcript_path.write_text(transcript_markdown, encoding="utf-8")
        return str(transcript_path)

    async def _summarize_completed_session(
        self,
        *,
        session_id: str,
        transcript_markdown: str,
        history: list[dict[str, Any]],
    ) -> str | None:
        if not history:
            return None
        if not self.haiku_adapter.api_key or not self.haiku_adapter.model:
            return None

        transcript_source = self._session_summary_source_text(transcript_markdown)
        system_prompt = (
            "You are summarizing a completed COSMIC daily session for long-term memory.\n"
            "Capture durable context the assistant should remember tomorrow.\n"
            "Prioritize stable user preferences, notable facts learned, important decisions, current projects, open loops, and concrete follow-ups.\n"
            "Exclude filler chatter, acknowledgements, and repeated back-and-forth.\n"
            "Write concise Markdown with these sections when relevant:\n"
            "## Summary\n"
            "## Durable Facts\n"
            "## Open Loops\n"
            "## Follow-ups\n"
            "Do not mention hidden system internals or implementation details."
        )
        user_message = (
            f"Session ID: {session_id}\n"
            f"Message count: {len(history)}\n\n"
            "Transcript:\n\n"
            f"{transcript_source}"
        )
        summary_text, _usage, _stop_reason = await self.haiku_adapter.generate_text(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=self.config.session_summary_max_output_tokens,
        )
        normalized = summary_text.strip()
        return normalized or None

    def _session_summary_source_text(self, transcript_markdown: str) -> str:
        normalized = transcript_markdown.strip()
        if len(normalized) <= SESSION_SUMMARY_SOURCE_CHAR_LIMIT:
            return normalized

        half_limit = SESSION_SUMMARY_SOURCE_CHAR_LIMIT // 2
        head = normalized[:half_limit].rstrip()
        tail = normalized[-half_limit:].lstrip()
        return head + "\n\n[... transcript truncated for summarization ...]\n\n" + tail

    def _build_session_summary_memory_payload(
        self,
        *,
        session_id: str,
        transcript_path: str,
        summary_text: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session_state = self.get_session_state(session_id)
        carry_forward_packet = self._build_carry_forward_packet(session_id)
        return {
            "kind": "session_summary",
            "title": f"Session summary {session_id}",
            "content": summary_text,
            "tags": ["session_rollover", "daily_session"],
            "metadata": {
                "session_id": session_id,
                "message_count": len(history),
                "transcript_path": transcript_path,
                "carry_forward_packet": carry_forward_packet,
                "compaction_packet": session_state.get("compaction_packet"),
            },
            "provenance": {
                "source_kind": "gateway_rollover",
                "source_id": session_id,
                "created_by": "cosmic/gateway:1.0.0",
                "session_id": session_id,
            },
        }

    def _build_task_summary_memory_payload(
        self,
        *,
        task_id: str,
        request_id: str,
        session_id: str,
        channel: str,
        user_message: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        user_content = self._safe_text(user_message.get("content")) or "[empty message]"
        assistant_content = self._safe_text(event.get("content")) or "[empty response]"
        route = self._safe_text(event.get("route")) or "opus"
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else None
        input_artifacts = user_message.get("metadata", {}).get("input_artifacts") if isinstance(user_message.get("metadata"), dict) else None

        content_lines = [
            f"# Task Summary: {task_id}",
            "",
            "## Request",
            user_content,
            "",
            "## Result",
            assistant_content,
        ]
        if metrics:
            content_lines.extend(
                [
                    "",
                    "## Metrics",
                    "```json",
                    json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True),
                    "```",
                ]
            )

        metadata: dict[str, Any] = {
            "task_id": task_id,
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "route": route,
            "source_message_id": self._safe_text(user_message.get("message_id")),
        }
        if input_artifacts:
            metadata["input_artifacts"] = input_artifacts

        return {
            "kind": "task_summary",
            "title": f"Task summary {task_id}",
            "content": "\n".join(content_lines).strip(),
            "tags": [
                "task_summary",
                route,
                self._safe_text(channel.split(":", 1)[0] if channel else None) or "unknown_channel",
            ],
            "metadata": metadata,
            "provenance": {
                "source_kind": "gateway_task_completion",
                "source_id": request_id,
                "created_by": "cosmic/gateway:1.0.0",
                "session_id": session_id,
                "task_id": task_id,
                "channel": channel,
            },
        }

    def _extract_memory_id(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        direct_id = self._safe_text(payload.get("memory_id"))
        if direct_id:
            return direct_id
        record = payload.get("record")
        if isinstance(record, dict):
            return self._safe_text(record.get("memory_id"))
        return None
