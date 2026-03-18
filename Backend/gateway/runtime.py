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
from .adapters.response_processor import DirectRouteHandoff
from .artifacts.store import ArtifactStore
from .channels.base import ChannelUnavailableError, PermanentDeliveryError, RetryableDeliveryError
from .channels.desktop import DesktopAdapter
from .channels.registry import ChannelAdapterRegistry
from .channels.telegram import TelegramAdapter, TelegramConfig
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .delivery.queue_store import DeliveryQueueStore, utcnow_iso
from .memory import MemoryWriteAuditStore
from .memory.client import CosmicMemoryClient, MemoryClientHTTPError, MemoryPromptContext
from .orchestrator_client import OrchestratorClient
from .routing.router_client import ModelRouterClient
from .routing.audit_store import RoutingAuditStore
from .scheduler import CronExpressionError, SchedulerStore, compute_next_fire_at, normalize_timezone_name, render_local_fire_time
from .session.compaction import build_compaction_prompts
from .session.summary import (
    build_rollover_summary_prompts,
    session_summary_source_text,
)
from .session_store import SessionStore
from .usage_store import UsageStore
from .wishlist import CapabilityWishlistService, CapabilityWishlistStore
from shared import (
    MeteredCall,
    ModelSpec,
    SOURCE_PRIORITY_MAP,
    TaskEnvelope,
    UsageEvent,
    begin_metered_call,
    build_model_key,
    build_usage_event,
    create_redis_client,
    estimate_text_tokens,
    ensure_stream_group,
    generate_task_id,
    lookup_model_spec,
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
COMPACTION_RECENT_WINDOW_MESSAGES = 12
COMPACTION_RAW_MESSAGE_CHAR_LIMIT = 24_000
COMPACTION_TRIGGER_FRACTION = 0.70
CONTEXT_SYSTEM_PROMPT_TOKEN_BUDGET = 2_000
CONTEXT_MIN_CONVERSATION_BUDGET_TOKENS = 8_000
DEFAULT_CONTEXT_WINDOW_TOKENS = 48_000
MEMORY_WRITE_RATE_WINDOW_SEC = 3_600
MEMORY_WRITE_PREVIEW_CHARS = 400
SYSTEM_CRON_DAILY_ROLLOVER = "system.daily_rollover"
TURN_LEDGER_WINDOW_SIZE = 10
TASK_NOTEBOOK_WINDOW_SIZE = 5
EPHEMERAL_CHANNEL_EVENT_TYPES = {
    "route_result",
    "response.chunk",
    "response.thinking.chunk",
    "task.created",
    "task.progress",
    "tool.call",
    "tool.result",
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


@dataclass(frozen=True, slots=True)
class MemoryWriteAuditEvent:
    operation: str
    write_source: str
    writer_id: str | None
    request_id: str | None
    session_id: str | None
    task_id: str | None
    channel: str | None
    source_kind: str | None
    source_id: str | None
    title: str | None
    original_kind: str | None
    normalized_kind: str | None
    canonical_key: str | None
    content_hash: str | None
    content_preview: str | None
    tags: list[str]
    metadata: dict[str, Any]
    provenance: dict[str, Any]
    guard_applied: bool


@dataclass(frozen=True, slots=True)
class UsageSubmitResult:
    queued: bool
    inserted: bool | None
    deduplicated: bool | None
    queue_depth: int = 0
    used_sync_fallback: bool = False


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
        self.usage_store = UsageStore(config.usage_db_path)
        self.routing_audit_store = RoutingAuditStore(config.routing_audit_db_path)
        self.memory_write_audit_store = MemoryWriteAuditStore(config.memory_write_audit_db_path)
        self.capability_wishlist_store = CapabilityWishlistStore(config.capability_wishlist_db_path)
        self.artifact_store = ArtifactStore(config.artifacts_db_path)
        self.delivery_queue_store = DeliveryQueueStore(config.delivery_queue_db_path)
        self.scheduler_store = SchedulerStore(config.scheduler_db_path)
        self.capability_wishlist_service = CapabilityWishlistService(
            store=self.capability_wishlist_store,
            export_dir=config.capability_wishlist_export_dir,
            perplexity_api_key=config.perplexity_api_key,
            embedding_model=config.capability_wishlist_embedding_model,
            embedding_dimensions=config.capability_wishlist_embedding_dimensions,
            xai_api_key=config.xai_api_key,
            adjudicator_model=config.capability_wishlist_adjudicator_model,
            usage_recorder=self._record_local_usage_event,
            owner_user_id=config.owner_user_id or None,
        )
        self.memory_client = CosmicMemoryClient(
            base_url=config.cosmic_memory_url,
            timeout_sec=config.cosmic_memory_timeout_sec,
            write_timeout_sec=config.cosmic_memory_write_timeout_sec,
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
        self._memory_write_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._usage_event_queue: asyncio.Queue[UsageEvent] | None = None
        self._usage_worker: asyncio.Task[None] | None = None
        self._usage_queue_metrics: dict[str, int] = {
            "queued_events": 0,
            "persisted_events": 0,
            "deduplicated_events": 0,
            "sync_fallback_events": 0,
            "append_failures": 0,
            "max_queue_depth": 0,
        }
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
        self.usage_store.initialize()
        self.routing_audit_store.initialize()
        self.memory_write_audit_store.initialize()
        self.artifact_store.initialize()
        self.delivery_queue_store.initialize()
        self.scheduler_store.initialize(default_timezone=self.config.user_timezone_fallback)
        await self.capability_wishlist_service.initialize()
        self._usage_event_queue = asyncio.Queue(maxsize=self.config.usage_queue_max_size)
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
        self._usage_worker = asyncio.create_task(
            self._usage_worker_loop(),
            name="gateway-usage-worker",
        )
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
        await self._flush_usage_queue()
        if self._usage_worker is not None:
            self._usage_worker.cancel()
            await asyncio.gather(self._usage_worker, return_exceptions=True)
            self._usage_worker = None
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
        await self.capability_wishlist_service.close()
        await self.haiku_adapter.close()
        await self.perplexity_adapter.close()
        if self._redis is not None:
            await self._redis.aclose()
        self._usage_event_queue = None
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

    def _scheduler_effective_timezone(self, timezone_name: str | None = None) -> str:
        if self._safe_text(timezone_name):
            return normalize_timezone_name(self._safe_text(timezone_name) or "")
        return self.current_user_timezone()

    def _channel_platform(self, channel: str | None) -> str | None:
        normalized_channel = self._safe_text(channel)
        if not normalized_channel:
            return None
        platform, separator, _ = normalized_channel.partition(":")
        if separator:
            return platform or None
        if normalized_channel in self.registry.adapters or normalized_channel == "desktop":
            return normalized_channel
        return None

    def _normalize_delivery_target(self, value: Any) -> str | None:
        text = self._safe_text(value)
        if not text:
            return None
        alias = text.casefold()
        alias_map = {
            "current": "current",
            "incoming": "current",
            "same": "current",
            "same_channel": "current",
            "same channel": "current",
            "desktop": "desktop",
            "desktop_primary": "desktop",
            "primary_desktop": "desktop",
            "primary desktop": "desktop",
            "whatsapp": "whatsapp",
            "telegram": "telegram",
        }
        return alias_map.get(alias, text)

    def _preferred_linked_channel(
        self,
        platform: str,
        *,
        current_channel: str | None = None,
    ) -> str | None:
        normalized_current = self._safe_text(current_channel)
        if normalized_current and self._channel_platform(normalized_current) == platform:
            return normalized_current
        if platform == "desktop":
            return "desktop"

        links = self.session_store.list_channel_links(platform=platform, limit=20)
        if platform == "whatsapp":
            for item in links:
                channel = self._safe_text(item.get("channel"))
                if not channel or not channel.startswith("whatsapp:"):
                    continue
                destination = channel.split(":", 1)[1]
                if destination and "@g.us" not in destination:
                    return channel
        for item in links:
            channel = self._safe_text(item.get("channel"))
            if channel:
                return channel
        return None

    def resolve_channel_target(
        self,
        *,
        delivery_target: str | None,
        current_channel: str | None = None,
        fallback_channel: str | None = None,
    ) -> dict[str, Any]:
        normalized_current = self._safe_text(current_channel)
        normalized_fallback = self._safe_text(fallback_channel)
        normalized_target = self._normalize_delivery_target(delivery_target)

        if not normalized_target:
            resolved_channel = normalized_current or "desktop"
            return {
                "delivery_target": resolved_channel,
                "resolved_channel": resolved_channel,
                "platform": self._channel_platform(resolved_channel) or "desktop",
                "matched_by": "current_channel" if normalized_current else "desktop_default",
            }

        if normalized_target == "current":
            resolved_channel = normalized_current or "desktop"
            return {
                "delivery_target": normalized_target,
                "resolved_channel": resolved_channel,
                "platform": self._channel_platform(resolved_channel) or "desktop",
                "matched_by": "current_channel" if normalized_current else "desktop_default",
            }

        if ":" in normalized_target:
            platform = self._channel_platform(normalized_target)
            if not platform:
                raise ValueError(f"Unsupported delivery target: {normalized_target}")
            if platform not in self.registry.adapters and platform != "desktop":
                raise ValueError(f"Channel platform is not configured: {platform}")
            return {
                "delivery_target": normalized_target,
                "resolved_channel": normalized_target,
                "platform": platform,
                "matched_by": "explicit_channel",
            }

        platform = normalized_target
        if platform not in self.registry.adapters and platform != "desktop":
            raise ValueError(f"Unknown delivery target: {platform}")
        if platform == "desktop":
            return {
                "delivery_target": platform,
                "resolved_channel": "desktop",
                "platform": "desktop",
                "matched_by": "desktop_alias",
            }

        resolved_channel = self._preferred_linked_channel(platform, current_channel=normalized_current)
        if resolved_channel:
            return {
                "delivery_target": platform,
                "resolved_channel": resolved_channel,
                "platform": platform,
                "matched_by": "linked_channel",
            }

        if normalized_fallback and self._channel_platform(normalized_fallback) == platform:
            return {
                "delivery_target": platform,
                "resolved_channel": normalized_fallback,
                "platform": platform,
                "matched_by": "stored_fallback",
            }

        raise ValueError(
            f"No linked {platform} channel is available yet. Ask from that channel first or provide an exact channel."
        )

    def _compact_scheduler_working_set(self, working_set: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(working_set, dict):
            return None
        snapshot: dict[str, Any] = {}
        goal = self._safe_text(working_set.get("goal"))
        if goal:
            snapshot["goal"] = self._bounded_excerpt(goal, limit=240)
        active_workstreams = self._normalize_string_list(working_set.get("active_workstreams"), limit=4)
        if active_workstreams:
            snapshot["active_workstreams"] = active_workstreams
        open_loops = self._normalize_string_list(working_set.get("open_loops"), limit=4)
        if open_loops:
            snapshot["open_loops"] = open_loops
        active_task_refs = self._normalize_string_list(working_set.get("active_task_refs"), limit=4)
        if active_task_refs:
            snapshot["active_task_refs"] = active_task_refs
        focus_entities = self._normalize_entity_list(working_set.get("current_focus_entities"), limit=4)
        if focus_entities:
            snapshot["current_focus_entities"] = [
                self._safe_text(item.get("label")) or self._safe_text(item.get("id")) or "entity"
                for item in focus_entities
            ]
        return snapshot or None

    def _build_scheduler_context_packet(
        self,
        *,
        prompt: str,
        created_request_id: str | None,
        created_session_id: str | None,
        created_channel: str | None,
        context_summary: str | None,
    ) -> dict[str, Any] | None:
        packet: dict[str, Any] = {
            "captured_at": utcnow_iso(),
        }
        if created_session_id:
            packet["created_session_id"] = created_session_id
        if created_request_id:
            packet["created_request_id"] = created_request_id
        if created_channel:
            packet["created_channel"] = created_channel
        if self._safe_text(context_summary):
            packet["context_summary"] = self._bounded_excerpt(context_summary, limit=500)

        request_record = None
        normalized_request_id = self._safe_text(created_request_id)
        if normalized_request_id and isinstance(self.request_records.get(normalized_request_id), dict):
            request_record = self.request_records[normalized_request_id]

        original_request = None
        prior_context: list[dict[str, Any]] = []
        working_set_snapshot = None
        memory_context_excerpt = None

        if isinstance(request_record, dict):
            message = request_record.get("message") if isinstance(request_record.get("message"), dict) else {}
            original_request = self._safe_text(message.get("content"))
            raw_context = request_record.get("assembled_conversation_context")
            prior_context = raw_context if isinstance(raw_context, list) else []
            working_set_snapshot = self._compact_scheduler_working_set(
                request_record.get("active_working_set")
                if isinstance(request_record.get("active_working_set"), dict)
                else None
            )
            memory_context_excerpt = self._bounded_excerpt(
                request_record.get("memory_context"),
                limit=1_200,
            )
        elif created_session_id:
            metadata = self._ensure_session_state_seeded(created_session_id)
            working_set_snapshot = self._compact_scheduler_working_set(
                metadata.get("active_working_set")
                if isinstance(metadata.get("active_working_set"), dict)
                else None
            )
            prior_context = self._build_conversation_context(created_session_id, limit=6)

        original_request = original_request or prompt
        if original_request:
            packet["original_request"] = self._bounded_excerpt(original_request, limit=500)

        normalized_context: list[dict[str, str]] = []
        for item in prior_context[-4:]:
            if not isinstance(item, dict):
                continue
            role = self._safe_text(item.get("role"))
            content = self._safe_text(item.get("content"))
            if role not in {"user", "assistant"} or not content:
                continue
            normalized_context.append(
                {
                    "role": role,
                    "content": self._bounded_excerpt(content, limit=320) or "",
                }
            )
        if normalized_context:
            packet["conversation_tail"] = normalized_context
        if working_set_snapshot:
            packet["working_set_snapshot"] = working_set_snapshot
        if memory_context_excerpt:
            packet["memory_context_excerpt"] = memory_context_excerpt
        return packet or None

    def _scheduler_context_conversation(self, context_packet: dict[str, Any] | None) -> list[dict[str, str]]:
        if not isinstance(context_packet, dict):
            return []
        raw_tail = context_packet.get("conversation_tail")
        if not isinstance(raw_tail, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in raw_tail:
            if not isinstance(item, dict):
                continue
            role = self._safe_text(item.get("role"))
            content = self._safe_text(item.get("content"))
            if role not in {"user", "assistant"} or not content:
                continue
            normalized.append({"role": role, "content": content})
        return normalized

    def _render_scheduler_context_block(self, context_packet: dict[str, Any] | None) -> str | None:
        if not isinstance(context_packet, dict):
            return None

        lines = [
            "## Stored Reminder Context",
            "This reminder context was captured when the job was created. Use it as background, but verify against the current state before acting.",
        ]

        context_summary = self._safe_text(context_packet.get("context_summary"))
        if context_summary:
            lines.extend(["", f"- Why this exists: {context_summary}"])

        original_request = self._safe_text(context_packet.get("original_request"))
        if original_request:
            lines.extend(["", f"- Original request: {original_request}"])

        created_channel = self._safe_text(context_packet.get("created_channel"))
        if created_channel:
            lines.extend(["", f"- Created from channel: {created_channel}"])

        created_at = self._safe_text(context_packet.get("captured_at"))
        if created_at:
            lines.extend(["", f"- Captured at: {created_at}"])

        working_set = (
            context_packet.get("working_set_snapshot")
            if isinstance(context_packet.get("working_set_snapshot"), dict)
            else None
        )
        if isinstance(working_set, dict):
            goal = self._safe_text(working_set.get("goal"))
            if goal:
                lines.extend(["", f"- Goal at creation: {goal}"])
            active_workstreams = self._normalize_string_list(working_set.get("active_workstreams"), limit=4)
            if active_workstreams:
                lines.extend(["", "- Active workstreams at creation:"])
                lines.extend(f"  - {item}" for item in active_workstreams)
            open_loops = self._normalize_string_list(working_set.get("open_loops"), limit=4)
            if open_loops:
                lines.extend(["", "- Open loops at creation:"])
                lines.extend(f"  - {item}" for item in open_loops)

        memory_context_excerpt = self._safe_text(context_packet.get("memory_context_excerpt"))
        if memory_context_excerpt:
            lines.extend(["", "### Memory Snapshot", memory_context_excerpt])

        conversation_tail = self._scheduler_context_conversation(context_packet)
        if conversation_tail:
            lines.extend(["", "### Prior Conversation Snapshot"])
            for item in conversation_tail:
                lines.append(f"- {item['role']}: {item['content']}")

        return "\n".join(lines)

    def _join_context_blocks(self, *blocks: str | None) -> str | None:
        normalized_blocks = [block for block in blocks if self._safe_text(block)]
        if not normalized_blocks:
            return None
        return "\n\n".join(normalized_blocks)

    def _scheduler_record(self, record: dict[str, Any], *, include_history: bool = False) -> dict[str, Any]:
        payload = dict(record)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        payload["label"] = self._safe_text(payload.get("name")) or ""
        payload["cron_expression"] = self._safe_text(payload.get("cron_expr")) or ""
        payload["next_fire_local"] = render_local_fire_time(
            self._safe_text(payload.get("next_fire_at")),
            self._safe_text(payload.get("timezone")) or self.current_user_timezone(),
        )
        payload["last_fired_local"] = render_local_fire_time(
            self._safe_text(payload.get("last_fired_at")),
            self._safe_text(payload.get("timezone")) or self.current_user_timezone(),
        )
        payload["prompt"] = self._safe_text(metadata.get("prompt"))
        payload["delivery_target"] = self._safe_text(metadata.get("delivery_target")) or None
        payload["delivery_channel"] = self._safe_text(metadata.get("delivery_channel")) or "desktop"
        payload["resolved_delivery_channel"] = payload["delivery_channel"]
        payload["one_shot"] = bool(metadata.get("one_shot"))
        payload["created_by"] = self._safe_text(metadata.get("created_by"))
        payload["created_request_id"] = self._safe_text(metadata.get("created_request_id"))
        payload["created_session_id"] = self._safe_text(metadata.get("created_session_id"))
        payload["created_channel"] = self._safe_text(metadata.get("created_channel"))
        payload["explicit_timezone"] = bool(metadata.get("explicit_timezone"))
        context_packet = metadata.get("context_packet") if isinstance(metadata.get("context_packet"), dict) else {}
        payload["context_summary"] = self._safe_text(metadata.get("context_summary")) or self._safe_text(
            context_packet.get("context_summary")
        )
        if include_history:
            payload["history"] = self.scheduler_store.list_cron_history(
                self._safe_text(payload.get("cron_id")) or "",
                limit=20,
            )
        return payload

    def _list_scheduler_crons(self, *, include_system: bool, active_only: bool) -> list[dict[str, Any]]:
        records = self.scheduler_store.list_crons()
        if not include_system:
            records = [item for item in records if self._safe_text(item.get("kind")) != "system"]
        if active_only:
            records = [
                item for item in records
                if bool(self._safe_text(item.get("next_fire_at")) or item.get("paused"))
            ]
        return [self._scheduler_record(item, include_history=False) for item in records]

    def _cron_execution_identity(self, cron_id: str, scheduled_for: str | None) -> tuple[str, str]:
        base = f"{cron_id}:{scheduled_for or 'unscheduled'}".encode("utf-8")
        digest = hashlib.sha256(base).hexdigest()[:16]
        return (f"req_cron_{digest}", f"cron:{cron_id}:{scheduled_for or 'unscheduled'}")

    async def create_scheduler_cron(
        self,
        *,
        cron_id: str | None,
        label: str,
        cron_expression: str,
        prompt: str,
        one_shot: bool,
        description: str | None = None,
        timezone_name: str | None = None,
        delivery_target: str | None = None,
        delivery_channel: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_by: str | None = None,
        created_request_id: str | None = None,
        created_session_id: str | None = None,
        created_channel: str | None = None,
        context_summary: str | None = None,
    ) -> dict[str, Any]:
        normalized_cron_id = self._safe_text(cron_id) or f"cron_{uuid4().hex[:12]}"
        if self.scheduler_store.get_cron(normalized_cron_id) is not None:
            raise ValueError(f"Cron already exists: {normalized_cron_id}")

        normalized_label = self._safe_text(label)
        normalized_prompt = self._safe_text(prompt)
        normalized_cron_expression = self._safe_text(cron_expression)
        if not normalized_label or not normalized_prompt or not normalized_cron_expression:
            raise ValueError("label, cron_expression, and prompt are required")

        effective_timezone = self._scheduler_effective_timezone(timezone_name)
        next_fire_at = compute_next_fire_at(
            normalized_cron_expression,
            effective_timezone,
        )
        resolution = self.resolve_channel_target(
            delivery_target=self._safe_text(delivery_target) or self._safe_text(delivery_channel),
            current_channel=self._safe_text(created_channel),
            fallback_channel=self._safe_text(delivery_channel),
        )
        normalized_delivery_target = self._safe_text(resolution.get("delivery_target"))
        normalized_delivery_channel = self._safe_text(resolution.get("resolved_channel")) or "desktop"
        context_packet = self._build_scheduler_context_packet(
            prompt=normalized_prompt,
            created_request_id=self._safe_text(created_request_id),
            created_session_id=self._safe_text(created_session_id),
            created_channel=self._safe_text(created_channel),
            context_summary=self._safe_text(context_summary),
        )
        record = self.scheduler_store.upsert_cron(
            cron_id=normalized_cron_id,
            name=normalized_label,
            kind="reminder",
            description=self._safe_text(description),
            cron_expr=normalized_cron_expression,
            timezone_name=effective_timezone,
            next_fire_at=next_fire_at,
            metadata={
                **(metadata or {}),
                "prompt": normalized_prompt,
                "one_shot": bool(one_shot),
                "delivery_target": normalized_delivery_target,
                "delivery_channel": normalized_delivery_channel,
                "created_by": self._safe_text(created_by) or "orchestrator",
                "created_request_id": self._safe_text(created_request_id),
                "created_session_id": self._safe_text(created_session_id),
                "created_channel": self._safe_text(created_channel),
                "explicit_timezone": bool(self._normalize_timezone_name(timezone_name)),
                "context_summary": self._safe_text(context_summary),
                "context_packet": context_packet,
            },
        )
        self._scheduler_wakeup.set()
        return self._scheduler_record(record, include_history=True)

    def delete_scheduler_cron(self, cron_id: str) -> bool:
        normalized_cron_id = self._safe_text(cron_id)
        if not normalized_cron_id:
            return False
        existing = self.scheduler_store.get_cron(normalized_cron_id)
        if existing is None:
            return False
        if self._safe_text(existing.get("kind")) == "system":
            raise ValueError("System crons cannot be deleted.")
        deleted = self.scheduler_store.delete_cron(normalized_cron_id)
        if deleted:
            self._scheduler_wakeup.set()
        return deleted

    async def _build_scheduler_request_record(self, cron: dict[str, Any]) -> dict[str, Any]:
        cron_id = self._safe_text(cron.get("cron_id")) or ""
        metadata = cron.get("metadata") if isinstance(cron.get("metadata"), dict) else {}
        prompt = self._safe_text(metadata.get("prompt"))
        resolution = self.resolve_channel_target(
            delivery_target=self._safe_text(metadata.get("delivery_target")) or self._safe_text(metadata.get("delivery_channel")),
            current_channel=self._safe_text(metadata.get("created_channel")),
            fallback_channel=self._safe_text(metadata.get("delivery_channel")),
        )
        channel = self._safe_text(resolution.get("resolved_channel")) or "desktop"
        timezone_name = self._safe_text(cron.get("timezone")) or self.current_user_timezone()
        scheduled_for = self._safe_text(cron.get("next_fire_at"))
        if not prompt:
            raise RuntimeError(f"Cron {cron_id} is missing its prompt payload.")

        request_id, idempotency_key = self._cron_execution_identity(cron_id, scheduled_for)
        session_id = self._current_session_id()
        session_metadata = self._ensure_session_state_seeded(session_id)
        active_working_set = (
            session_metadata.get("active_working_set")
            if isinstance(session_metadata.get("active_working_set"), dict)
            else None
        )
        context_packet = metadata.get("context_packet") if isinstance(metadata.get("context_packet"), dict) else None
        stored_conversation_context = self._scheduler_context_conversation(context_packet)
        cron_context_block = self._render_scheduler_context_block(context_packet)
        memory_prompt_context = await self._assemble_memory_prompt_context(query=prompt)
        combined_memory_context = self._join_context_blocks(
            cron_context_block,
            memory_prompt_context.rendered,
        )
        request_record = {
            "status": "accepted",
            "request_id": request_id,
            "session_id": session_id,
            "source": "cron",
            "source_id": cron_id,
            "channel": channel,
            "route": "opus",
            "dispatch_target": "orchestrator",
            "classification": {
                "route": "opus",
                "needs_latest": False,
                "needs_citations": False,
                "is_task": True,
                "is_continuation": False,
                "confidence": 1.0,
                "signals": ["scheduler_cron"],
            },
            "message": {
                "content": prompt,
                "channel": channel,
                "request_id": request_id,
                "metadata": {
                    "platform": "scheduler",
                    "cron_id": cron_id,
                    "cron_label": self._safe_text(cron.get("name")),
                    "scheduled_for": scheduled_for,
                    "delivery_channel": channel,
                    "delivery_target": self._safe_text(resolution.get("delivery_target")),
                },
            },
            "assembled_conversation_context": stored_conversation_context or self._build_conversation_context(session_id),
            "memory_context": self._compose_prompt_context(
                active_working_set=active_working_set,
                memory_context=combined_memory_context,
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
            "routing_decision_source": "scheduler",
            "input_artifacts": [],
            "accepted_at": utcnow_iso(),
            "idempotency_key": idempotency_key,
            "cron_timezone": timezone_name,
            "cron_scheduled_for": scheduled_for,
            "cron_context": context_packet,
            "cron_delivery_target": self._safe_text(resolution.get("delivery_target")),
        }
        self.request_records[request_id] = request_record
        self.routing_audit_store.append(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            source="cron",
            source_id=cron_id,
            query_text=prompt,
            route_override=None,
            sticky_hit=False,
            decision_source="scheduler",
            classifier_route="opus",
            final_route="opus",
            dispatch_target="orchestrator",
            confidence=1.0,
            signals=["scheduler_cron"],
            conversation_context=request_record["assembled_conversation_context"],
            classifier_payload=None,
            classifier_metrics=None,
            classifier_model=None,
            classifier_latency_ms=None,
            decision_latency_ms=0.0,
            error_text=None,
        )
        return request_record

    async def _execute_custom_scheduler_cron(self, cron: dict[str, Any]) -> tuple[str, str, str | None]:
        cron_id = self._safe_text(cron.get("cron_id")) or ""
        cron_expr = self._safe_text(cron.get("cron_expr"))
        timezone_name = self._safe_text(cron.get("timezone")) or self.current_user_timezone()
        metadata = cron.get("metadata") if isinstance(cron.get("metadata"), dict) else {}
        one_shot = bool(metadata.get("one_shot"))
        if not cron_expr:
            raise RuntimeError(f"Cron {cron_id} is missing its cron expression.")

        request_record = await self._build_scheduler_request_record(cron)
        await self.fulfill_processed_message(request_record)

        scheduled_for = self._safe_text(cron.get("next_fire_at"))
        next_fire_at = None
        if not one_shot:
            after = None
            if scheduled_for:
                text = scheduled_for[:-1] + "+00:00" if scheduled_for.endswith("Z") else scheduled_for
                try:
                    after = datetime.fromisoformat(text)
                except ValueError:
                    after = None
            next_fire_at = compute_next_fire_at(
                cron_expr,
                timezone_name,
                after=after,
            )
        label = self._safe_text(cron.get("name")) or cron_id
        summary = (
            f"Reminder ran: {label}"
            if one_shot else
            f"Scheduled task ran: {label}"
        )
        return ("completed", summary, next_fire_at)

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
                else:
                    status, summary, next_fire_at = await self._execute_custom_scheduler_cron(cron)
            except Exception as exc:
                logger.exception("gateway.scheduler_cron_failed cron_id=%s", cron_id)
                status = "failed"
                summary = str(exc)
                metadata = cron.get("metadata") if isinstance(cron.get("metadata"), dict) else {}
                if not bool(metadata.get("one_shot")) and self._safe_text(cron.get("cron_expr")):
                    try:
                        next_fire_at = compute_next_fire_at(
                            self._safe_text(cron.get("cron_expr")) or "",
                            self._safe_text(cron.get("timezone")) or self.current_user_timezone(),
                            after=datetime.now(timezone.utc),
                        )
                    except CronExpressionError:
                        next_fire_at = None
                else:
                    next_fire_at = None
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
            "crons": self._list_scheduler_crons(include_system=True, active_only=False),
            "heartbeat": self.scheduler_store.get_heartbeat(),
        }

    def list_scheduler_crons(self, *, include_system: bool = True, active_only: bool = False) -> list[dict[str, Any]]:
        return self._list_scheduler_crons(include_system=include_system, active_only=active_only)

    def get_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        record = self.scheduler_store.get_cron(cron_id)
        if record is None:
            return None
        return self._scheduler_record(record, include_history=True)

    def pause_scheduler_cron(self, cron_id: str, *, reason: str | None = None) -> dict[str, Any] | None:
        record = self.scheduler_store.pause_cron(cron_id, reason=reason)
        if record is None:
            return None
        self._scheduler_wakeup.set()
        return self._scheduler_record(record, include_history=False)

    def resume_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        next_fire_at = None
        if cron_id == SYSTEM_CRON_DAILY_ROLLOVER:
            next_fire_at = self._next_rollover_fire_at(timezone_name=self.current_user_timezone())
        else:
            existing = self.scheduler_store.get_cron(cron_id)
            if existing is not None:
                cron_expr = self._safe_text(existing.get("cron_expr"))
                timezone_name = self._safe_text(existing.get("timezone")) or self.current_user_timezone()
                if cron_expr:
                    try:
                        next_fire_at = compute_next_fire_at(cron_expr, timezone_name)
                    except CronExpressionError:
                        next_fire_at = None
        record = self.scheduler_store.resume_cron(cron_id, next_fire_at=next_fire_at)
        self._scheduler_wakeup.set()
        if record is None:
            return None
        return self._scheduler_record(record, include_history=False)

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

        # Cross-channel sync: push the user message to connected desktop clients
        if channel and not channel.startswith("desktop:"):
            self._track_background_task(
                self._broadcast_cross_channel_to_desktop(
                    session_id,
                    role="user",
                    content=content or "[non-text inbound message]",
                    channel=channel,
                )
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
        if routing_decision.decision_source == "model_router":
            self._record_classifier_usage(
                request_id=request_id,
                session_id=session_id,
                route=self._safe_text(classification.get("route")) or "opus",
                router_response=routing_decision.classifier_payload,
            )
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

        history = self._get_model_visible_history(session_id)
        memory_context = self._safe_text(request_record.get("memory_context"))
        active_request = self.active_requests.get(request_id)
        source = self._safe_text(request_record.get("source")) or "user"
        source_id = self._safe_text(request_record.get("source_id"))
        direct_usage_metadata = {
            "channel": channel,
            "source": source,
            "source_id": source_id,
        }

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

        try:
            if route in {"haiku", "gemini"}:
                await self.haiku_adapter.stream(
                    request_id=request_id,
                    session_id=session_id,
                    history=history,
                    send=send,
                    store_assistant_message=store_assistant_message,
                    channel=channel,
                    memory_context=memory_context,
                    usage_recorder=self._build_gateway_usage_recorder(
                        source_id="gateway:haiku",
                        operation="gateway.direct_chat",
                        route="haiku",
                        request_id=request_id,
                        session_id=session_id,
                        extra_metadata=direct_usage_metadata,
                    ),
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
                    usage_recorder=self._build_gateway_usage_recorder(
                        source_id="gateway:perplexity",
                        operation="gateway.direct_chat",
                        route="perplexity",
                        request_id=request_id,
                        session_id=session_id,
                        extra_metadata=direct_usage_metadata,
                    ),
                )
                return
        except DirectRouteHandoff as handoff:
            handoff_route = self._normalize_route(handoff.route)
            if handoff_route != "opus":
                raise RuntimeError(f"Unsupported direct-model handoff route: {handoff.route}")
            await self._handle_direct_model_handoff_to_opus(
                request_record=request_record,
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                prior_route=route,
                active_request=active_request,
                send=send,
            )

        await self._stream_orchestrator_fulfillment(
            request_record=request_record,
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            active_request=active_request,
            send=send,
            store_assistant_message=store_assistant_message,
        )

    async def _handle_direct_model_handoff_to_opus(
        self,
        *,
        request_record: dict[str, Any],
        request_id: str,
        session_id: str,
        channel: str,
        prior_route: str,
        active_request: ActiveRequest | None,
        send,
    ) -> None:
        normalized_prior_route = self._normalize_route(prior_route)
        if active_request is not None and active_request.partial_content.strip():
            raise RuntimeError(
                f"{normalized_prior_route} requested Opus handoff after a direct response had already started."
            )

        handoff_count = self._coerce_int(request_record.get("direct_model_handoff_count")) or 0
        if handoff_count >= 1:
            raise RuntimeError("Direct model requested more than one Opus handoff for the same request.")

        original_decision_source = self._safe_text(request_record.get("routing_decision_source")) or "model_router"
        request_record.setdefault("initial_route", normalized_prior_route)
        request_record.setdefault("initial_routing_decision_source", original_decision_source)

        classification = dict(request_record.get("classification")) if isinstance(request_record.get("classification"), dict) else {}
        signals = list(classification.get("signals")) if isinstance(classification.get("signals"), list) else []
        handoff_signal = f"direct_model_handoff:{normalized_prior_route}->opus"
        if handoff_signal not in signals:
            signals.append(handoff_signal)
        classification.update(
            {
                "route": "opus",
                "is_task": True,
                "is_continuation": True,
                "signals": signals,
            }
        )

        request_record["route"] = "opus"
        request_record["dispatch_target"] = "orchestrator"
        request_record["classification"] = classification
        request_record["routing_decision_source"] = "direct_model_handoff"
        request_record["direct_model_handoff_count"] = handoff_count + 1
        request_record["direct_model_handoff_from"] = normalized_prior_route
        self.request_records[request_id] = request_record

        if active_request is not None:
            active_request.route = "opus"

        message = request_record.get("message") if isinstance(request_record.get("message"), dict) else {}
        query_text = self._safe_text(message.get("content")) or "[empty message]"
        assembled_conversation_context = (
            request_record.get("assembled_conversation_context")
            if isinstance(request_record.get("assembled_conversation_context"), list)
            else None
        )
        route_override = normalized_prior_route if "manual_route_override" in signals else None
        sticky_hit = original_decision_source == "sticky_awaiting_reply"
        self.routing_audit_store.append(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            source=self._safe_text(request_record.get("source")) or "user",
            source_id=self._safe_text(request_record.get("source_id")),
            query_text=query_text,
            route_override=route_override,
            sticky_hit=sticky_hit,
            decision_source="direct_model_handoff",
            classifier_route=normalized_prior_route,
            final_route="opus",
            dispatch_target="orchestrator",
            confidence=self._coerce_float(classification.get("confidence"), 0.0),
            signals=signals,
            conversation_context=assembled_conversation_context,
            classifier_payload=None,
            classifier_metrics=None,
            classifier_model=None,
            classifier_latency_ms=None,
            decision_latency_ms=0.0,
            error_text=None,
        )

        if channel.startswith("desktop:"):
            await send(
                {
                    "type": "task.progress",
                    "request_id": request_id,
                    "session_id": session_id,
                    "channel": channel,
                    "route": "opus",
                    "status": "escalating",
                    "message": "Escalating to Opus for deeper handling.",
                    "escalated_from": normalized_prior_route,
                }
            )

    async def _stream_orchestrator_fulfillment(
        self,
        *,
        request_record: dict[str, Any],
        request_id: str,
        session_id: str,
        channel: str,
        active_request: ActiveRequest | None,
        send,
        store_assistant_message,
    ) -> None:
        task = self._build_orchestrator_task(
            request_record=request_record,
            session_id=session_id,
            request_id=request_id,
            channel=channel,
        )
        self.active_task_channels[task.task_id] = channel
        if active_request is not None:
            active_request.route = "opus"
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
                    source=self._safe_text(request_record.get("source")),
                    source_id=self._safe_text(request_record.get("source_id")),
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

    def get_session_history_page(
        self,
        session_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(200, int(limit)))
        normalized_offset = max(0, int(offset))
        messages = self.session_store.get_history_page(
            session_id,
            limit=normalized_limit,
            offset=normalized_offset,
        )
        total_messages = self.session_store.count_history(session_id)
        return {
            "session_id": session_id,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "total_messages": total_messages,
            "has_more": normalized_offset + len(messages) < total_messages,
            "messages": messages,
        }

    def list_routing_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.routing_audit_store.list_entries(limit=limit)

    def log_usage_event(self, event: UsageEvent | dict[str, Any]) -> bool:
        payload = self._normalize_usage_event(event)
        inserted = self.usage_store.append(payload)
        metrics_key = "persisted_events" if inserted else "deduplicated_events"
        self._usage_queue_metrics[metrics_key] = self._usage_queue_metrics.get(metrics_key, 0) + 1
        return inserted

    def submit_usage_event(self, event: UsageEvent | dict[str, Any]) -> UsageSubmitResult:
        payload = self._normalize_usage_event(event)
        queue = self._usage_event_queue
        worker_active = self._usage_worker is not None and not self._usage_worker.done()
        if queue is not None and worker_active:
            try:
                queue.put_nowait(payload)
                queue_depth = queue.qsize()
                self._usage_queue_metrics["queued_events"] = self._usage_queue_metrics.get("queued_events", 0) + 1
                self._usage_queue_metrics["max_queue_depth"] = max(
                    self._usage_queue_metrics.get("max_queue_depth", 0),
                    queue_depth,
                )
                return UsageSubmitResult(
                    queued=True,
                    inserted=None,
                    deduplicated=None,
                    queue_depth=queue_depth,
                    used_sync_fallback=False,
                )
            except asyncio.QueueFull:
                self._usage_queue_metrics["sync_fallback_events"] = (
                    self._usage_queue_metrics.get("sync_fallback_events", 0) + 1
                )
                logger.warning(
                    "gateway.usage_queue_full llm_call_id=%s operation=%s provider=%s model=%s",
                    payload.llm_call_id,
                    payload.operation,
                    payload.provider,
                    payload.model,
                )
        inserted = self.log_usage_event(payload)
        return UsageSubmitResult(
            queued=False,
            inserted=inserted,
            deduplicated=not inserted,
            queue_depth=queue.qsize() if queue is not None else 0,
            used_sync_fallback=queue is not None and worker_active,
        )

    def list_recent_usage(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.usage_store.list_recent(limit=limit)

    def capability_wishlist_summary(self) -> dict[str, Any]:
        return self.capability_wishlist_service.summary()

    def capability_wishlist_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.capability_wishlist_service.list_recent(limit=limit)

    async def capability_wishlist_get(self, capability_id: str) -> dict[str, Any] | None:
        return await self.capability_wishlist_service.get_item(capability_id)

    async def capability_wishlist_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = self._safe_text(payload.get("query"))
        limit = self._coerce_int(payload.get("limit")) or 3
        return await self.capability_wishlist_service.search(query=query, limit=limit)

    async def capability_wishlist_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.capability_wishlist_service.capture(
            title=self._safe_text(payload.get("title")),
            summary=self._safe_text(payload.get("summary")),
            desired_outcome=self._safe_text(payload.get("desired_outcome")) or None,
            domain=self._safe_text(payload.get("domain")) or None,
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            evidence=self._safe_text(payload.get("evidence")) or None,
            source_component=self._safe_text(payload.get("source_component")) or None,
            source_id=self._safe_text(payload.get("source_id")) or None,
            request_id=self._safe_text(payload.get("request_id")) or None,
            session_id=self._safe_text(payload.get("session_id")) or None,
            task_id=self._safe_text(payload.get("task_id")) or None,
            route=self._safe_text(payload.get("route")) or None,
            created_by=self._safe_text(payload.get("created_by")) or None,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    def _record_local_usage_event(self, event: UsageEvent | dict[str, Any]) -> None:
        try:
            payload = self._normalize_usage_event(event)
            result = self.submit_usage_event(payload)
            logger.info(
                "gateway.usage_logged llm_call_id=%s source_component=%s operation=%s provider=%s model=%s queued=%s deduplicated=%s sync_fallback=%s",
                payload.llm_call_id,
                payload.source_component,
                payload.operation,
                payload.provider,
                payload.model,
                result.queued,
                result.deduplicated,
                result.used_sync_fallback,
            )
        except Exception:
            logger.exception("gateway.usage_log_failed")

    def _normalize_usage_event(self, event: UsageEvent | dict[str, Any]) -> UsageEvent:
        payload = event if isinstance(event, UsageEvent) else UsageEvent.model_validate(event)
        owner_user_id = self._safe_text(self.config.owner_user_id) or None
        if owner_user_id is None:
            return payload
        current_user_id = self._safe_text(payload.user_id) or None
        if current_user_id and current_user_id != owner_user_id:
            logger.warning(
                "gateway.usage_owner_override llm_call_id=%s source_component=%s current_user_id=%s owner_user_id=%s",
                payload.llm_call_id,
                payload.source_component,
                current_user_id,
                owner_user_id,
            )
        if current_user_id == owner_user_id:
            return payload
        return payload.model_copy(update={"user_id": owner_user_id})

    async def _flush_usage_queue(self) -> None:
        queue = self._usage_event_queue
        if queue is None:
            return
        try:
            await asyncio.wait_for(
                queue.join(),
                timeout=self.config.usage_queue_flush_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "gateway.usage_queue_flush_timeout remaining=%s timeout_sec=%s",
                queue.qsize(),
                self.config.usage_queue_flush_timeout_sec,
            )

    async def _usage_worker_loop(self) -> None:
        while True:
            queue = self._usage_event_queue
            if queue is None:
                await asyncio.sleep(0.1)
                continue
            payload = await queue.get()
            try:
                for attempt in range(2):
                    try:
                        self.log_usage_event(payload)
                        break
                    except Exception:
                        if attempt >= 1:
                            self._usage_queue_metrics["append_failures"] = (
                                self._usage_queue_metrics.get("append_failures", 0) + 1
                            )
                            logger.exception(
                                "gateway.usage_queue_append_failed llm_call_id=%s operation=%s provider=%s model=%s",
                                payload.llm_call_id,
                                payload.operation,
                                payload.provider,
                                payload.model,
                            )
                        else:
                            await asyncio.sleep(0.1)
            finally:
                queue.task_done()

    def _usage_summary(self) -> dict[str, Any]:
        summary = self.usage_store.summary()
        queue = self._usage_event_queue
        return {
            **summary,
            "owner_user_id": self._safe_text(self.config.owner_user_id) or None,
            "queue_depth": queue.qsize() if queue is not None else 0,
            "queue_max_size": self.config.usage_queue_max_size,
            "worker_running": bool(self._usage_worker is not None and not self._usage_worker.done()),
            "queue_metrics": dict(self._usage_queue_metrics),
        }

    def _record_classifier_usage(
        self,
        *,
        request_id: str,
        session_id: str,
        route: str,
        router_response: dict[str, Any] | None,
    ) -> None:
        if not isinstance(router_response, dict):
            return
        raw_usage = router_response.get("usage")
        classifier_model = self._safe_text(router_response.get("classifier_model"))
        if not classifier_model:
            return
        model_key = build_model_key("groq", classifier_model)
        llm_call_id = self._safe_text(router_response.get("llm_call_id"))
        llm_call_placed_at = self._safe_text(router_response.get("llm_call_placed_at"))
        if not llm_call_id or not llm_call_placed_at:
            metered_call = begin_metered_call(prefix="call")
        else:
            metered_call = MeteredCall(
                llm_call_id=llm_call_id,
                llm_call_placed_at=llm_call_placed_at,
                started_perf_counter=time.perf_counter(),
            )
        metrics = router_response.get("metrics") if isinstance(router_response.get("metrics"), dict) else {}
        event = build_usage_event(
            metered_call=metered_call,
            source_component="model_router",
            source_id=f"model_router:{classifier_model}",
            task_id=None,
            session_id=session_id,
            route=route,
            operation="model_router.classify",
            model_key=model_key,
            request_id=request_id,
            provider_request_id=self._safe_text(router_response.get("provider_request_id")) or None,
            raw_usage=raw_usage,
            success=True,
            latency_ms=self._coerce_int(metrics.get("rtt_ms")),
            metadata_json={
                "classification": router_response.get("classification"),
                "metrics": metrics,
            },
        )
        self._record_local_usage_event(event)

    def _build_gateway_usage_recorder(
        self,
        *,
        source_id: str,
        operation: str,
        route: str | None,
        request_id: str | None,
        session_id: str | None,
        task_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ):
        def normalize_latency(value: Any) -> int | None:
            return self._coerce_int(value)

        def normalize_cost(value: Any) -> float | None:
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        async def recorder(payload: dict[str, Any]) -> None:
            model_key = self._safe_text(payload.get("model_key"))
            provider = self._safe_text(payload.get("provider"))
            model = self._safe_text(payload.get("model"))
            event = build_usage_event(
                metered_call=payload["metered_call"],
                source_component="gateway",
                source_id=source_id,
                task_id=task_id,
                session_id=session_id,
                route=route,
                operation=operation,
                model_key=model_key or None,
                provider=provider or None,
                model=model or None,
                usage_kind=self._safe_text(payload.get("usage_kind")) or None,
                request_id=request_id,
                provider_request_id=self._safe_text(payload.get("provider_request_id")) or None,
                raw_usage=payload.get("raw_usage"),
                success=bool(payload.get("success", True)),
                error_code=self._safe_text(payload.get("error_code")) or None,
                latency_ms=normalize_latency(payload.get("latency_ms")),
                estimated_cost_usd=normalize_cost(payload.get("estimated_cost_usd")),
                metadata_json=self._merge_usage_metadata(
                    extra_metadata,
                    payload.get("metadata_json"),
                ),
            )
            self._record_local_usage_event(event)

        return recorder

    def _merge_usage_metadata(self, primary: dict[str, Any] | None, secondary: Any) -> Any:
        if primary is None and secondary is None:
            return None
        if primary is None:
            return secondary
        if secondary is None:
            return primary
        if isinstance(secondary, dict):
            return {
                **primary,
                **secondary,
            }
        return {
            **primary,
            "extra": secondary,
        }

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
        if (self._safe_text(event.get("source")) or "user") != "user":
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
            episode_payload, audit_event = self._normalize_episode_write_payload(
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
                },
                write_source="gateway_episode_ingest",
            )
            response = await self._ingest_memory_episode(
                payload=episode_payload,
                audit_event=audit_event,
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
        if (self._safe_text(event.get("source")) or "user") != "user":
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
            summary_payload = self._build_task_summary_memory_payload(
                task_id=task_id,
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                user_message=user_message,
                event=event,
            )
            response = await self._write_memory_record(
                payload=summary_payload,
                audit_event=self._build_memory_write_audit_event(
                    payload=summary_payload,
                    operation="task_summary_write",
                    write_source="gateway_task_summary",
                    original_kind=self._safe_text(summary_payload.get("kind")),
                    normalized_kind=self._safe_text(summary_payload.get("kind")),
                    guard_applied=False,
                ),
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

    async def memory_get(self, memory_id: str) -> dict[str, Any]:
        return await self.memory_client.get_memory(memory_id)

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
        normalized_payload, audit_event = self._normalize_tool_memory_write_payload(payload)
        return await self._write_memory_record(
            payload=normalized_payload,
            audit_event=audit_event,
            writer_id=audit_event.writer_id,
        )

    async def memory_write_core_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload, audit_event = self._normalize_tool_core_fact_payload(payload)
        return await self._write_core_fact_record(
            payload=normalized_payload,
            audit_event=audit_event,
            writer_id=audit_event.writer_id,
        )

    async def memory_ingest_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload, audit_event = self._normalize_episode_write_payload(
            payload,
            write_source="gateway_internal_episode",
        )
        return await self._ingest_memory_episode(
            payload=normalized_payload,
            audit_event=audit_event,
        )

    async def memory_core_facts(self, *, max_chars: int = 1500) -> dict[str, Any]:
        return await self.memory_client.get_core_fact_block(max_chars=max_chars)

    async def memory_index_status(self) -> dict[str, Any]:
        return await self.memory_client.index_status()

    async def memory_index_sync(self) -> dict[str, Any]:
        return await self.memory_client.index_sync()

    async def memory_index_rebuild(self) -> dict[str, Any]:
        return await self.memory_client.index_rebuild()

    async def memory_graph_status(self) -> dict[str, Any]:
        return await self.memory_client.graph_status()

    async def memory_graph_sync(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.memory_client.graph_sync(payload)

    async def memory_graph_rebuild(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.memory_client.graph_rebuild(payload)

    def list_memory_write_audit(
        self,
        *,
        limit: int = 50,
        request_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        writer_id: str | None = None,
        operation: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.memory_write_audit_store.list_entries(
            limit=limit,
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            writer_id=writer_id,
            operation=operation,
            status=status,
        )

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
        if (self._safe_text(event.get("source")) or "user") != "user":
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
            elif isinstance(self.request_records.get(request_id), dict):
                request_record = self.request_records[request_id]
                message = request_record.get("message") if isinstance(request_record.get("message"), dict) else {}
                request_text = self._bounded_excerpt(message.get("content"))
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

    def _compaction_target_model_specs(self) -> list[ModelSpec]:
        specs: list[ModelSpec] = []
        candidates = [
            ("anthropic", self.config.haiku_model),
            ("anthropic", "claude-opus-4-6"),
            ("perplexity", self.config.perplexity_model),
        ]
        seen: set[str] = set()
        for provider, model in candidates:
            spec = lookup_model_spec(provider, model)
            if spec is None or spec.key in seen or spec.status != "active":
                continue
            seen.add(spec.key)
            specs.append(spec)
        return specs

    def _conversation_context_budget_tokens(self) -> int:
        specs = self._compaction_target_model_specs()
        finite_contexts = [
            int(spec.context_window_tokens)
            for spec in specs
            if isinstance(spec.context_window_tokens, int) and spec.context_window_tokens > 0
        ]
        if not finite_contexts:
            return DEFAULT_CONTEXT_WINDOW_TOKENS
        context_window = min(finite_contexts)
        reserve_tokens = max(
            (
                int(spec.recommended_headroom_reserve_tokens)
                for spec in specs
                if spec.context_window_tokens
            ),
            default=8_000,
        )
        budget = (
            context_window
            - CONTEXT_SYSTEM_PROMPT_TOKEN_BUDGET
            - self.config.cosmic_memory_passive_token_budget
            - reserve_tokens
        )
        return max(CONTEXT_MIN_CONVERSATION_BUDGET_TOKENS, budget)

    def _compaction_trigger_threshold_tokens(self) -> int:
        return max(
            1_000,
            int(self._conversation_context_budget_tokens() * COMPACTION_TRIGGER_FRACTION),
        )

    def _get_model_visible_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.session_store.get_pruned_history(
            session_id,
            max_messages=None,
            max_chars=None,
            max_approx_tokens=self._conversation_context_budget_tokens(),
        )

    def _estimate_history_tokens(self, history: list[dict[str, Any]]) -> int:
        total = 0
        for item in history:
            total += estimate_text_tokens(self._safe_text(item.get("content")))
            total += 4
        return total

    async def _maybe_compact_session(self, session_id: str) -> None:
        async with self._session_compaction_lock:
            model_visible_history = self._get_model_visible_history(session_id)
            if not model_visible_history:
                return
            model_visible_tokens = self._estimate_history_tokens(model_visible_history)
            if model_visible_tokens < self._compaction_trigger_threshold_tokens():
                return

            history = self.session_store.get_history(session_id)
            if not history:
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

        system_prompt, user_prompt = build_compaction_prompts(
            session_id=session_id,
            existing_summary=existing_summary,
            turn_lines=turn_lines,
            older_lines=older_lines,
            recent_window_count=len(recent_history),
            current_tasks=self._normalize_string_list(current_tasks),
        )
        summary_text, _usage, _stop_reason = await self.haiku_adapter.generate_text(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1200,
            usage_recorder=self._build_gateway_usage_recorder(
                source_id="gateway:haiku",
                operation="gateway.session_compaction",
                route="haiku",
                request_id=None,
                session_id=session_id,
                extra_metadata={
                    "summary_type": "session_compaction",
                    "session_id": session_id,
                },
            ),
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
        # Fetch full day's history so the desktop shows the complete conversation
        history = self.session_store.get_history(session_id)
        pending_inputs = self._pending_inputs_for_channel(channel, session_id=session_id)
        active_tasks = await self._active_task_summaries(session_id=session_id, channel=channel)
        return {
            "type": "resume.ok",
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "user_timezone": self.current_user_timezone(),
            "history_tail": history,
            "active_tasks": active_tasks,
            "pending_inputs": pending_inputs,
        }

    def notify_channel_active(self, channel: str | None) -> None:
        if not channel:
            return
        platform = self._channel_platform(channel)
        if platform:
            self.session_store.upsert_channel_link(
                channel=channel,
                platform=platform,
            )
        self._delivery_wakeup.set()
        if self._redis is not None:
            self._track_background_task(self._drain_pending_task_inputs(channel))

    async def _broadcast_cross_channel_to_desktop(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        channel: str,
        route: str | None = None,
        sources: list[dict[str, str]] | None = None,
        thinking_text: str | None = None,
    ) -> None:
        """Push a cross-channel message to all connected desktop clients for this session.

        Called when a non-desktop channel (WhatsApp, Telegram) produces a user
        message or receives an assistant response, so the desktop UI can
        display the conversation in real-time.
        """
        if not session_id or not channel or channel.startswith("desktop:"):
            return
        from .channels.desktop import DesktopAdapter
        desktop_adapter: DesktopAdapter | None = None
        for adapter in self.registry.adapters.values():
            if isinstance(adapter, DesktopAdapter):
                desktop_adapter = adapter
                break
        if desktop_adapter is None:
            return

        event: dict[str, Any] = {
            "type": "crosschannel.message",
            "session_id": session_id,
            "role": role,
            "content": content,
            "channel": channel,
            "route": route,
            "timestamp": utcnow_iso(),
        }
        if sources:
            event["sources"] = sources
        if thinking_text:
            event["thinking_text"] = thinking_text
        await desktop_adapter.broadcast_to_session(session_id, event)

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
            delivery_status = await self._deliver_or_queue_channel_event(event, channel=channel)
            if delivery_status == "dropped":
                return
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
            if channel.startswith("desktop:") and (self._safe_text(event.get("source")) or "user") != "cron":
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
            "usage": self._usage_summary(),
            "capability_wishlist": self.capability_wishlist_service.summary(),
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
            "usage": self._usage_summary(),
            "capability_wishlist": self.capability_wishlist_service.summary(),
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
                "user_timezone": self._safe_text(request_record.get("cron_timezone")) or self.current_user_timezone(),
            },
            input_artifacts=request_record.get("input_artifacts") or [],
            idempotency_key=self._safe_text(request_record.get("idempotency_key")) or uuid4().hex,
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
            event_channel = self._safe_text(event.get("channel")) or ""
            store_assistant_message(
                str(event.get("content") or ""),
                awaiting_reply=bool(event.get("awaiting_reply")),
                metadata={
                    "task_id": self._safe_text(event.get("task_id")),
                    "metrics": event.get("metrics"),
                    "thinking_text": self._safe_text(event.get("thinking_text")),
                    "source": self._safe_text(event.get("source")),
                    "source_id": self._safe_text(event.get("source_id")),
                },
                channel=event_channel,
                route="opus",
            )
            # Cross-channel sync: push the assistant response to connected desktop clients
            if event_channel and not event_channel.startswith("desktop:") and session_id:
                self._track_background_task(
                    self._broadcast_cross_channel_to_desktop(
                        session_id,
                        role="assistant",
                        content=str(event.get("content") or ""),
                        channel=event_channel,
                        route="opus",
                        sources=event.get("sources") if isinstance(event.get("sources"), list) else None,
                        thinking_text=self._safe_text(event.get("thinking_text")),
                    )
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
        source: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = dict(event)
        normalized.setdefault("task_id", task_id)
        normalized.setdefault("request_id", request_id)
        normalized.setdefault("session_id", session_id)
        normalized.setdefault("channel", channel)
        if source:
            normalized.setdefault("source", source)
        if source_id:
            normalized.setdefault("source_id", source_id)
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
                        summary_payload = self._build_session_summary_memory_payload(
                            session_id=session_id,
                            transcript_path=transcript_path,
                            summary_text=summary_text,
                            history=history,
                        )
                        memory_response = await self._write_memory_record(
                            payload=summary_payload,
                            audit_event=self._build_memory_write_audit_event(
                                payload=summary_payload,
                                operation="session_summary_write",
                                write_source="gateway_session_rollover",
                                original_kind=self._safe_text(summary_payload.get("kind")),
                                normalized_kind=self._safe_text(summary_payload.get("kind")),
                                guard_applied=False,
                            ),
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
        system_prompt, user_message = build_rollover_summary_prompts(
            session_id=session_id,
            message_count=len(history),
            transcript_source=transcript_source,
        )
        summary_text, _usage, _stop_reason = await self.haiku_adapter.generate_text(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=self.config.session_summary_max_output_tokens,
            usage_recorder=self._build_gateway_usage_recorder(
                source_id="gateway:haiku",
                operation="gateway.session_summary",
                route="haiku",
                request_id=None,
                session_id=session_id,
                extra_metadata={
                    "summary_type": "session_rollover",
                    "session_id": session_id,
                    "message_count": len(history),
                },
            ),
        )
        normalized = summary_text.strip()
        return normalized or None

    def _session_summary_source_text(self, transcript_markdown: str) -> str:
        return session_summary_source_text(
            transcript_markdown,
            char_limit=SESSION_SUMMARY_SOURCE_CHAR_LIMIT,
        )

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

    def _coerce_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _derive_memory_title(self, content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return "Untitled memory"
        title = normalized.splitlines()[0].strip()
        if len(title) > 72:
            title = title[:69].rstrip() + "..."
        return title or "Untitled memory"

    def _normalize_tool_memory_write_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], MemoryWriteAuditEvent]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise MemoryClientHTTPError(status_code=400, message="content is required")

        original_kind = self._safe_text(payload.get("kind")) or "agent_note"
        normalized_kind = self._normalize_memory_write_kind(original_kind)
        if normalized_kind is None:
            raise MemoryClientHTTPError(
                status_code=400,
                message=(
                    "Unsupported memory write kind. Use user_data or agent_note. "
                    "Stable always-on facts should use /internal/memory/core-facts."
                ),
            )

        title = self._safe_text(payload.get("title")) or self._derive_memory_title(content)
        tags = self._normalize_string_list(payload.get("tags"), limit=24)
        metadata = dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {}
        provenance = dict(payload.get("provenance")) if isinstance(payload.get("provenance"), dict) else {}
        normalized_payload = {
            "kind": normalized_kind,
            "title": title,
            "content": content,
            "tags": tags,
            "metadata": metadata,
            "provenance": provenance,
        }
        audit_event = self._build_memory_write_audit_event(
            payload=normalized_payload,
            operation="memory_write",
            write_source="tool_memory_write",
            original_kind=original_kind,
            normalized_kind=normalized_kind,
            guard_applied=True,
        )
        return normalized_payload, audit_event

    def _normalize_tool_core_fact_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], MemoryWriteAuditEvent]:
        fact = str(payload.get("fact") or payload.get("content") or "").strip()
        if not fact:
            raise MemoryClientHTTPError(status_code=400, message="fact is required")

        title = self._safe_text(payload.get("title")) or self._derive_memory_title(fact)
        canonical_key = self._safe_text(payload.get("canonical_key"))
        priority = self._coerce_int(payload.get("priority"))
        if priority is None:
            priority = 100
        priority = min(max(priority, 0), 1000)
        always_include = self._coerce_bool(payload.get("always_include"), True)
        tags = self._normalize_string_list(payload.get("tags"), limit=24)
        metadata = dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {}
        provenance = dict(payload.get("provenance")) if isinstance(payload.get("provenance"), dict) else {}

        normalized_payload: dict[str, Any] = {
            "fact": fact,
            "title": title,
            "priority": priority,
            "always_include": always_include,
            "tags": tags,
            "metadata": metadata,
            "provenance": provenance,
        }
        if canonical_key:
            normalized_payload["canonical_key"] = canonical_key

        audit_event = self._build_memory_write_audit_event(
            payload={
                "kind": "core_fact",
                "title": title,
                "content": fact,
                "tags": tags,
                "metadata": metadata,
                "provenance": provenance,
                "canonical_key": canonical_key,
            },
            operation="memory_write_core_fact",
            write_source="tool_memory_write_core_fact",
            original_kind="core_fact",
            normalized_kind="core_fact",
            guard_applied=True,
        )
        return normalized_payload, audit_event

    def _normalize_episode_write_payload(
        self,
        payload: dict[str, Any],
        *,
        write_source: str,
    ) -> tuple[dict[str, Any], MemoryWriteAuditEvent]:
        observations = payload.get("observations")
        if not isinstance(observations, list) or not observations:
            raise MemoryClientHTTPError(status_code=400, message="observations are required")
        provenance = dict(payload.get("provenance")) if isinstance(payload.get("provenance"), dict) else {}
        if not provenance:
            raise MemoryClientHTTPError(status_code=400, message="provenance is required")

        normalized_payload = dict(payload)
        normalized_payload["kind"] = self._safe_text(payload.get("kind")) or "transcript"
        normalized_payload["title"] = self._safe_text(payload.get("title")) or "Transcript episode"
        normalized_payload["tags"] = self._normalize_string_list(payload.get("tags"), limit=24)
        normalized_payload["metadata"] = dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {}
        normalized_payload["provenance"] = provenance

        audit_event = self._build_memory_write_audit_event(
            payload=normalized_payload,
            operation="memory_ingest_episode",
            write_source=write_source,
            original_kind=self._safe_text(normalized_payload.get("kind")) or "transcript",
            normalized_kind=self._safe_text(normalized_payload.get("kind")) or "transcript",
            guard_applied=False,
        )
        return normalized_payload, audit_event

    def _build_memory_write_audit_event(
        self,
        *,
        payload: dict[str, Any],
        operation: str,
        write_source: str,
        original_kind: str | None,
        normalized_kind: str | None,
        guard_applied: bool,
    ) -> MemoryWriteAuditEvent:
        metadata = dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {}
        provenance = dict(payload.get("provenance")) if isinstance(payload.get("provenance"), dict) else {}
        content_preview = self._memory_payload_preview(payload)
        content_hash = self._memory_payload_hash(
            payload,
            operation=operation,
            normalized_kind=normalized_kind,
        )
        writer_id = (
            self._safe_text(provenance.get("created_by"))
            or self._safe_text(metadata.get("agent_id"))
            or self._safe_text(metadata.get("stored_by"))
            or write_source
        )
        request_id = (
            self._safe_text(metadata.get("request_id"))
            or self._safe_text(provenance.get("request_id"))
            or self._safe_text(payload.get("request_id"))
        )
        session_id = (
            self._safe_text(metadata.get("session_id"))
            or self._safe_text(provenance.get("session_id"))
            or self._safe_text(payload.get("session_id"))
        )
        task_id = (
            self._safe_text(metadata.get("task_id"))
            or self._safe_text(provenance.get("task_id"))
            or self._safe_text(payload.get("task_id"))
        )
        channel = (
            self._safe_text(metadata.get("channel"))
            or self._safe_text(provenance.get("channel"))
            or self._safe_text(payload.get("channel"))
        )
        source_kind = (
            self._safe_text(provenance.get("source_kind"))
            or self._safe_text(metadata.get("source"))
            or self._safe_text(payload.get("source_kind"))
        )
        source_id = (
            self._safe_text(provenance.get("source_id"))
            or self._safe_text(metadata.get("source_id"))
            or self._safe_text(payload.get("source_id"))
        )
        canonical_key = (
            self._safe_text(payload.get("canonical_key"))
            or self._safe_text(metadata.get("canonical_key"))
        )
        tags = self._normalize_string_list(payload.get("tags"), limit=24)
        return MemoryWriteAuditEvent(
            operation=operation,
            write_source=write_source,
            writer_id=writer_id,
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            channel=channel,
            source_kind=source_kind,
            source_id=source_id,
            title=self._safe_text(payload.get("title")),
            original_kind=original_kind,
            normalized_kind=normalized_kind,
            canonical_key=canonical_key,
            content_hash=content_hash,
            content_preview=content_preview,
            tags=tags,
            metadata=metadata,
            provenance=provenance,
            guard_applied=guard_applied,
        )

    async def _write_memory_record(
        self,
        *,
        payload: dict[str, Any],
        audit_event: MemoryWriteAuditEvent,
        writer_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._execute_audited_memory_call(
            audit_event=audit_event,
            writer_id=writer_id,
            write_callable=lambda: self.memory_client.write_memory(payload),
        )

    async def _write_core_fact_record(
        self,
        *,
        payload: dict[str, Any],
        audit_event: MemoryWriteAuditEvent,
        writer_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._execute_audited_memory_call(
            audit_event=audit_event,
            writer_id=writer_id,
            write_callable=lambda: self.memory_client.write_core_fact(payload),
        )

    async def _ingest_memory_episode(
        self,
        *,
        payload: dict[str, Any],
        audit_event: MemoryWriteAuditEvent,
    ) -> dict[str, Any]:
        return await self._execute_audited_memory_call(
            audit_event=audit_event,
            writer_id=None,
            write_callable=lambda: self.memory_client.ingest_episode(payload),
        )

    async def _execute_audited_memory_call(
        self,
        *,
        audit_event: MemoryWriteAuditEvent,
        writer_id: str | None,
        write_callable,
    ) -> dict[str, Any]:
        resolved_writer_id = writer_id or audit_event.writer_id
        lock_key = None
        if audit_event.guard_applied and resolved_writer_id and audit_event.content_hash:
            lock_key = f"{resolved_writer_id}:{audit_event.content_hash}"

        if lock_key is None:
            if audit_event.guard_applied and resolved_writer_id:
                if await self._memory_write_is_rate_limited(resolved_writer_id):
                    message = (
                        f"Memory write rate limit exceeded for {resolved_writer_id}. "
                        f"Max {self.config.memory_write_max_per_hour} writes per hour."
                    )
                    self._append_memory_write_audit(
                        audit_event=audit_event,
                        status="rate_limited",
                        memory_id=None,
                        response=None,
                        deduplicated=False,
                        rate_limited=True,
                        indexed=None,
                        error_text=message,
                    )
                    raise MemoryClientHTTPError(status_code=429, message=message)
            return await self._execute_memory_call_without_guard(
                audit_event=audit_event,
                write_callable=write_callable,
            )

        lock = self._get_memory_write_lock(lock_key)
        async with lock:
            duplicate_entry = await self._find_duplicate_memory_write(
                writer_id=resolved_writer_id,
                content_hash=audit_event.content_hash or "",
            )
            if duplicate_entry is not None:
                duplicate_response = self._build_deduplicated_memory_response(
                    duplicate_entry=duplicate_entry,
                    audit_event=audit_event,
                )
                self._append_memory_write_audit(
                    audit_event=audit_event,
                    status="deduplicated",
                    memory_id=self._extract_memory_id(duplicate_response),
                    response=self._summarize_memory_write_response(duplicate_response),
                    deduplicated=True,
                    rate_limited=False,
                    indexed=True,
                    error_text=None,
                )
                return duplicate_response

            if await self._memory_write_is_rate_limited(resolved_writer_id):
                message = (
                    f"Memory write rate limit exceeded for {resolved_writer_id}. "
                    f"Max {self.config.memory_write_max_per_hour} writes per hour."
                )
                self._append_memory_write_audit(
                    audit_event=audit_event,
                    status="rate_limited",
                    memory_id=None,
                    response=None,
                    deduplicated=False,
                    rate_limited=True,
                    indexed=None,
                    error_text=message,
                )
                raise MemoryClientHTTPError(status_code=429, message=message)

            try:
                response = await write_callable()
            except Exception as exc:
                self._append_memory_write_audit(
                    audit_event=audit_event,
                    status="failed",
                    memory_id=None,
                    response=None,
                    deduplicated=False,
                    rate_limited=False,
                    indexed=False,
                    error_text=str(exc),
                )
                raise

            memory_id = self._extract_memory_id(response)
            if resolved_writer_id and audit_event.content_hash and memory_id:
                await self._remember_memory_write(
                    writer_id=resolved_writer_id,
                    content_hash=audit_event.content_hash,
                    memory_id=memory_id,
                )
            self._append_memory_write_audit(
                audit_event=audit_event,
                status="saved",
                memory_id=memory_id,
                response=self._summarize_memory_write_response(response),
                deduplicated=bool(self._coerce_bool(response.get("deduplicated") if isinstance(response, dict) else False, False)),
                rate_limited=False,
                indexed=True,
                error_text=None,
            )
            return response

    async def _execute_memory_call_without_guard(
        self,
        *,
        audit_event: MemoryWriteAuditEvent,
        write_callable,
    ) -> dict[str, Any]:
        try:
            response = await write_callable()
        except Exception as exc:
            self._append_memory_write_audit(
                audit_event=audit_event,
                status="failed",
                memory_id=None,
                response=None,
                deduplicated=False,
                rate_limited=False,
                indexed=False,
                error_text=str(exc),
            )
            raise

        self._append_memory_write_audit(
            audit_event=audit_event,
            status="saved",
            memory_id=self._extract_memory_id(response),
            response=self._summarize_memory_write_response(response),
            deduplicated=bool(self._coerce_bool(response.get("deduplicated") if isinstance(response, dict) else False, False)),
            rate_limited=False,
            indexed=True,
            error_text=None,
        )
        return response

    async def _memory_write_is_rate_limited(self, writer_id: str) -> bool:
        if self._redis is not None:
            rate_key = f"memory_write_rate:{writer_id}"
            try:
                count = await self._redis.incr(rate_key)
                if count == 1:
                    await self._redis.expire(rate_key, MEMORY_WRITE_RATE_WINDOW_SEC)
                return int(count) > self.config.memory_write_max_per_hour
            except Exception:
                logger.exception("gateway.memory_write_rate_limit_redis_failed writer_id=%s", writer_id)

        since_created_at = self._iso_seconds_ago(MEMORY_WRITE_RATE_WINDOW_SEC)
        count = self.memory_write_audit_store.count_recent_guarded_entries(
            writer_id=writer_id,
            since_created_at=since_created_at,
        )
        return count >= self.config.memory_write_max_per_hour

    async def _find_duplicate_memory_write(
        self,
        *,
        writer_id: str,
        content_hash: str,
    ) -> dict[str, Any] | None:
        if self._redis is not None:
            dedup_key = f"memory_write_dedup:{writer_id}:{content_hash}"
            try:
                memory_id = self._safe_text(await self._redis.get(dedup_key))
            except Exception:
                logger.exception("gateway.memory_write_dedup_redis_failed writer_id=%s", writer_id)
            else:
                if memory_id:
                    return {
                        "memory_id": memory_id,
                        "response": {"memory_id": memory_id, "indexed": True, "deduplicated": True},
                    }

        return self.memory_write_audit_store.find_recent_duplicate(
            writer_id=writer_id,
            content_hash=content_hash,
            since_created_at=self._iso_seconds_ago(self.config.memory_write_dedup_ttl_sec),
        )

    async def _remember_memory_write(
        self,
        *,
        writer_id: str,
        content_hash: str,
        memory_id: str,
    ) -> None:
        if self._redis is None:
            return
        dedup_key = f"memory_write_dedup:{writer_id}:{content_hash}"
        try:
            await self._redis.set(
                dedup_key,
                memory_id,
                ex=self.config.memory_write_dedup_ttl_sec,
            )
        except Exception:
            logger.exception("gateway.memory_write_dedup_redis_store_failed writer_id=%s", writer_id)

    def _append_memory_write_audit(
        self,
        *,
        audit_event: MemoryWriteAuditEvent,
        status: str,
        memory_id: str | None,
        response: dict[str, Any] | None,
        deduplicated: bool,
        rate_limited: bool,
        indexed: bool | None,
        error_text: str | None,
    ) -> None:
        self.memory_write_audit_store.append(
            operation=audit_event.operation,
            write_source=audit_event.write_source,
            status=status,
            writer_id=audit_event.writer_id,
            request_id=audit_event.request_id,
            session_id=audit_event.session_id,
            task_id=audit_event.task_id,
            channel=audit_event.channel,
            source_kind=audit_event.source_kind,
            source_id=audit_event.source_id,
            memory_id=memory_id,
            title=audit_event.title,
            original_kind=audit_event.original_kind,
            normalized_kind=audit_event.normalized_kind,
            canonical_key=audit_event.canonical_key,
            content_hash=audit_event.content_hash,
            content_preview=audit_event.content_preview,
            tags=audit_event.tags,
            metadata=audit_event.metadata,
            provenance=audit_event.provenance,
            response=response,
            deduplicated=deduplicated,
            rate_limited=rate_limited,
            guard_applied=audit_event.guard_applied,
            indexed=indexed,
            error_text=error_text,
        )

    def _memory_payload_preview(self, payload: dict[str, Any]) -> str | None:
        content = self._safe_text(payload.get("content"))
        if content:
            return self._bounded_excerpt(content, limit=MEMORY_WRITE_PREVIEW_CHARS)
        fact = self._safe_text(payload.get("fact"))
        if fact:
            return self._bounded_excerpt(fact, limit=MEMORY_WRITE_PREVIEW_CHARS)
        observations = payload.get("observations")
        if isinstance(observations, list):
            parts: list[str] = []
            for item in observations[:2]:
                if not isinstance(item, dict):
                    continue
                role = self._safe_text(item.get("role")) or "unknown"
                observation_content = self._bounded_excerpt(item.get("content"), limit=160)
                if observation_content:
                    parts.append(f"{role}: {observation_content}")
            if parts:
                return " | ".join(parts)
        return None

    def _memory_payload_hash(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
        normalized_kind: str | None,
    ) -> str | None:
        content = self._safe_text(payload.get("content"))
        if content is None:
            content = self._safe_text(payload.get("fact"))
        if content is None:
            observations = payload.get("observations")
            if isinstance(observations, list):
                content = json.dumps(observations, ensure_ascii=False, sort_keys=True, default=str)
        if content is None:
            return None
        canonical_key = self._safe_text(payload.get("canonical_key"))
        digest_input = "\n".join(
            [
                operation,
                normalized_kind or "",
                canonical_key or "",
                content,
            ]
        )
        return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

    def _normalize_memory_write_kind(self, kind: str) -> str | None:
        normalized = kind.strip().lower()
        alias_map = {
            "note": "agent_note",
            "agent_note": "agent_note",
            "user_data": "user_data",
            "preference": "user_data",
            "fact": "user_data",
            "relationship": "user_data",
            "goal": "user_data",
            "event": "user_data",
            "task_summary": "task_summary",
            "session_summary": "session_summary",
        }
        return alias_map.get(normalized)

    def _get_memory_write_lock(self, key: str) -> asyncio.Lock:
        lock = self._memory_write_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_write_locks[key] = lock
        return lock

    def _iso_seconds_ago(self, seconds: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=max(1, seconds))).isoformat().replace("+00:00", "Z")

    def _build_deduplicated_memory_response(
        self,
        *,
        duplicate_entry: dict[str, Any],
        audit_event: MemoryWriteAuditEvent,
    ) -> dict[str, Any]:
        memory_id = self._safe_text(duplicate_entry.get("memory_id"))
        response = duplicate_entry.get("response")
        if isinstance(response, dict):
            normalized = dict(response)
        else:
            normalized = {}
        if memory_id:
            normalized.setdefault("memory_id", memory_id)
        normalized["indexed"] = True
        normalized["deduplicated"] = True
        if audit_event.normalized_kind:
            normalized.setdefault("kind", audit_event.normalized_kind)
        if audit_event.title:
            normalized.setdefault("title", audit_event.title)
        return normalized

    def _summarize_memory_write_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        memory_id = self._extract_memory_id(payload)
        if memory_id:
            summary["memory_id"] = memory_id
        for key in (
            "kind",
            "title",
            "status",
            "version",
            "indexed",
            "deduplicated",
            "observation_count",
            "graph_episode_id",
        ):
            value = payload.get(key)
            if value is not None:
                summary[key] = value
        record = payload.get("record")
        if isinstance(record, dict):
            for key in (
                "kind",
                "title",
                "status",
                "version",
                "supersedes",
                "superseded_by",
                "created_at",
                "updated_at",
            ):
                value = record.get(key)
                if value is not None:
                    summary[f"record_{key}"] = value
        return summary
