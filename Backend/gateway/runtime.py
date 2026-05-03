from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hmac
import hashlib
import httpx
import json
import logging
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import HaikuAdapter, PerplexityAdapter
from .adapters.response_processor import DirectRouteHandoff
from .agent_auth_store import AgentAuthStore
from .artifacts.store import ArtifactStore
from .channels.base import (
    ChannelUnavailableError,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from .channels.agent_email import AgentEmailAdapter
from .channels.desktop import DesktopAdapter
from .channels.mobile import MobileAdapter
from .channels.registry import ChannelAdapterRegistry
from .channels.telegram import TelegramAdapter, TelegramConfig
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .credentials import CredentialManager
from .credentials.store import CredentialStore
from .delivery.queue_store import DeliveryQueueStore, utcnow_iso
from .memory import MemoryWriteAuditStore
from .memory.client import (
    CosmicMemoryClient,
    MemoryClientHTTPError,
    MemoryPromptContext,
)
from .mobile_device_store import MobileDeviceStore
from .orchestrator_client import OrchestratorClient
from .preferences.store import GatewayPreferenceStore
from .push_dispatcher import ExpoPushDispatcher, PushNotification
from .request_trace_store import RequestTraceStore
from .routing.router_client import ModelRouterClient
from .routing.audit_store import RoutingAuditStore
from .scheduler import (
    CronExpressionError,
    SchedulerStore,
    compute_next_fire_at,
    normalize_timezone_name,
    render_local_fire_time,
)
from .session.compaction import build_compaction_prompts
from .session.summary import (
    build_rollover_summary_prompts,
    session_summary_source_text,
)
from .session_store import SessionStore
from .usage_store import UsageStore
from .wishlist import CapabilityWishlistService, CapabilityWishlistStore
from orchestrator.store.ledger import TaskLedger

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except (
    Exception
):  # pragma: no cover - optional import guard for environments missing Pillow
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception

from shared import (
    AgentEmailIntegrationStore,
    CosmicMailClient,
    MeteredCall,
    ModelSpec,
    SOURCE_PRIORITY_MAP,
    TaskEnvelope,
    UsageEvent,
    agent_email_integration_is_disabled,
    agent_email_integration_is_configured,
    begin_metered_call,
    build_response_blocks,
    build_model_key,
    build_usage_event,
    create_redis_client,
    dispatch_task,
    estimate_text_tokens,
    ensure_stream_group,
    generate_task_id,
    infer_document_mime_from_extension,
    infer_image_mime_from_extension,
    infer_tabular_mime_from_extension,
    is_supported_document_artifact,
    is_supported_image_artifact,
    is_supported_tabular_artifact,
    lookup_model_spec,
    normalize_cosmic_mail_base_url,
    parse_event_envelope,
    parse_stream_payload,
    sign_task_envelope,
    utcnow,
)

logger = logging.getLogger(__name__)
_AGENT_EMAIL_ORG_API_KEY_NAME = "COSMIC Gateway Agent Email"

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
TASK_ACTIVITY_LOG_LIMIT = 64
RECENT_MEMORY_TOOL_RECEIPT_LIMIT = 4
RECENT_RESEARCH_RECEIPT_LIMIT = 3
RECENT_SPECIALIST_RECEIPT_LIMIT = 4
RECENT_MEMORY_TOOL_RECEIPT_SCAN_LIMIT = 12
CONTESTED_MEMORY_RECENT_WRITE_LIMIT = 3
CONTESTED_MEMORY_AUDIT_SCAN_LIMIT = 40
LLM_IMAGE_VARIANT_DIR_NAME = "llm_input"
FAILED_FOREGROUND_STREAM_RETENTION_SEC = 300.0
MEMORY_CONTEST_PHRASES = (
    "i didn't confirm",
    "i did not confirm",
    "i never confirmed",
    "that was your assumption",
    "that's your assumption",
    "it was your assumption",
    "you assumed",
    "you made that up",
)
EPHEMERAL_CHANNEL_EVENT_TYPES = {
    "route_result",
    "response.chunk",
    "response.blocks.snapshot",
    "response.thinking.chunk",
    "task.created",
    "task.progress",
    "tool.call",
    "tool.result",
    # Background-namespaced equivalents (streamed to task panel)
    "task.background.route_result",
    "task.background.response.chunk",
    "task.background.response.blocks.snapshot",
    "task.background.response.thinking.chunk",
    "task.background.task.created",
    "task.background.task.progress",
    "task.background.tool.call",
    "task.background.tool.result",
    # Transition events
    "task.backgrounded",
    "task.foregrounded",
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
    response_blocks_snapshot: list[dict[str, Any]] = field(default_factory=list)
    snapshot_seq: int = 0
    supporting_artifacts: list[dict[str, Any]] = field(default_factory=list)
    completed: bool = False
    failed: bool = False
    foreground: bool = True
    backgrounded_at: str | None = None
    user_query_excerpt: str = ""
    activity: str = ""
    activity_log: list[dict[str, Any]] = field(default_factory=list)
    alpha_terminal_log: list[dict[str, Any]] = field(default_factory=list)
    error_message: str = ""


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
        self.mobile_device_store = MobileDeviceStore(config.mobile_devices_db_path)
        self.usage_store = UsageStore(config.usage_db_path)
        self.request_trace_store = RequestTraceStore(config.request_trace_db_path)
        self.routing_audit_store = RoutingAuditStore(config.routing_audit_db_path)
        self.preference_store = GatewayPreferenceStore(config.preferences_db_path)
        self.memory_write_audit_store = MemoryWriteAuditStore(
            config.memory_write_audit_db_path
        )
        self.capability_wishlist_store = CapabilityWishlistStore(
            config.capability_wishlist_db_path
        )
        self.artifact_store = ArtifactStore(config.artifacts_db_path)
        self.delivery_queue_store = DeliveryQueueStore(config.delivery_queue_db_path)
        self.scheduler_store = SchedulerStore(config.scheduler_db_path)
        self.agent_email_integration_store = AgentEmailIntegrationStore(
            config.agent_email_integrations_db_path
        )
        self.agent_auth_store = AgentAuthStore(config.credentials_db_path)
        self._credential_store = CredentialStore(config.credentials_db_path)
        self.credential_manager = CredentialManager(
            store=self._credential_store,
            google_client_id=config.google_client_id,
            google_client_secret=config.google_client_secret,
            google_redirect_uri=config.google_redirect_uri,
        )
        self.memory_client = CosmicMemoryClient(
            base_url=config.cosmic_memory_url,
            timeout_sec=config.cosmic_memory_timeout_sec,
            write_timeout_sec=config.cosmic_memory_write_timeout_sec,
            internal_token=config.internal_token,
        )
        self.capability_wishlist_service = CapabilityWishlistService(
            store=self.capability_wishlist_store,
            export_dir=config.capability_wishlist_export_dir,
            memory_client=self.memory_client,
            embedding_model=config.capability_wishlist_embedding_model,
            embedding_dimensions=config.capability_wishlist_embedding_dimensions,
            xai_api_key=config.xai_api_key,
            adjudicator_model=config.capability_wishlist_adjudicator_model,
            usage_recorder=self._record_local_usage_event,
            owner_user_id=config.owner_user_id or None,
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
        artifact_timeout = httpx.Timeout(
            config.artifact_download_timeout_sec,
            connect=min(config.artifact_download_timeout_sec, 15.0),
        )
        self._artifact_client = httpx.AsyncClient(timeout=artifact_timeout, http2=True)
        self.push_dispatcher = ExpoPushDispatcher(
            enabled=config.enable_push_notifications,
            access_token=config.expo_access_token,
            push_url=config.expo_push_url,
            timeout_sec=config.expo_push_timeout_sec,
            fcm_project_id=config.fcm_project_id,
            fcm_service_account_file=config.fcm_service_account_file,
            fcm_service_account_json=config.fcm_service_account_json,
            unregister_token=self._clear_mobile_push_token_async,
            unregister_fcm_token=self._clear_mobile_fcm_token_async,
        )
        self._redis = (
            create_redis_client(config.redis_url) if config.redis_url else None
        )
        self._orchestrator_task_ledger = TaskLedger(
            config.orchestrator_task_ledger_db_path
        )
        self._recent_foreground_terminal_streams: dict[str, dict[str, Any]] = {}
        self.started = False
        self.adapter_errors: dict[str, str] = {}
        self.active_task_channels: dict[str, str] = {}
        self.request_records: dict[str, dict[str, Any]] = {}
        self.active_requests: dict[str, ActiveRequest] = {}
        self.active_requests_by_task: dict[str, str] = {}
        self._inflight_agent_email_messages: set[str] = set()
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
        self._specialist_event_worker: asyncio.Task[None] | None = None
        self._rollover_finalize_lock = asyncio.Lock()
        self._session_compaction_lock = asyncio.Lock()
        self._memory_health_worker: asyncio.Task[None] | None = None
        self._memory_health_snapshot: dict[str, Any] = {
            "enabled": self.memory_client.enabled,
            "status": "disabled" if not self.memory_client.enabled else "starting",
        }
        self._system_metrics_lock = asyncio.Lock()
        self._system_metrics_snapshot: dict[str, Any] | None = None
        self._system_metrics_snapshot_at = 0.0
        self._system_metrics_cpu_sample: tuple[int, int] | None = None
        self._system_metrics_network_sample: tuple[int, int, float] | None = None
        self._recent_push_dedupe: dict[str, float] = {}
        self._codex_login_session: dict[str, Any] | None = None

    async def start(self) -> None:
        self.session_store.initialize()
        self.mobile_device_store.initialize()
        self.usage_store.initialize()
        self.request_trace_store.initialize()
        self.routing_audit_store.initialize()
        self.preference_store.initialize()
        self.memory_write_audit_store.initialize()
        self.artifact_store.initialize()
        self.delivery_queue_store.initialize()
        self.scheduler_store.initialize(
            default_timezone=self.config.user_timezone_fallback
        )
        self.agent_email_integration_store.initialize()
        self.agent_auth_store.initialize()
        self._orchestrator_task_ledger.initialize()
        await self.capability_wishlist_service.initialize()
        self._usage_event_queue = asyncio.Queue(
            maxsize=self.config.usage_queue_max_size
        )
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
            await ensure_stream_group(
                self._redis,
                stream=self.config.agent_events_stream,
                group=self.config.agent_events_gateway_group,
            )
        if self.memory_client.enabled:
            await self._refresh_memory_health()
            self._memory_health_worker = asyncio.create_task(
                self._memory_health_loop(),
                name="gateway-memory-health",
            )
        await self._register_adapters()
        if self._agent_email_effectively_enabled():
            try:
                await self.sync_agent_email_webhook()
            except Exception:
                logger.exception("gateway.agent_email_webhook_sync_failed")
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
            self._specialist_event_worker = asyncio.create_task(
                self._specialist_event_consumer_loop(),
                name="gateway-specialist-event-consumer",
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
        if self._specialist_event_worker is not None:
            self._specialist_event_worker.cancel()
            await asyncio.gather(
                self._specialist_event_worker, return_exceptions=True
            )
            self._specialist_event_worker = None
        workers = [
            state.worker
            for state in self.active_requests.values()
            if state.worker is not None
        ]
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
        await self._stop_codex_login_session()
        await self.model_router.stop()
        await self.orchestrator.stop()
        await self.memory_client.stop()
        await self.capability_wishlist_service.close()
        await self.haiku_adapter.close()
        await self.perplexity_adapter.close()
        await self.push_dispatcher.close()
        await self._artifact_client.aclose()
        self.agent_auth_store.close()
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

    def _next_rollover_fire_at(
        self, *, timezone_name: str, now: datetime | None = None
    ) -> str:
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
        if normalized_channel in self.registry.adapters or normalized_channel in {
            "desktop",
            "mobile",
        }:
            return normalized_channel
        return None

    def _is_realtime_client_channel(self, channel: str | None) -> bool:
        return self._channel_platform(channel) in {"desktop", "mobile"}

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
            "email": "agent-email",
            "agent_email": "agent-email",
            "agent-email": "agent-email",
            "primary_email": "agent-email",
            "primary email": "agent-email",
        }
        return alias_map.get(alias, text)

    def _preferred_linked_channel(
        self,
        platform: str,
        *,
        current_channel: str | None = None,
    ) -> str | None:
        normalized_current = self._safe_text(current_channel)
        if (
            normalized_current
            and self._channel_platform(normalized_current) == platform
        ):
            return normalized_current
        if platform == "desktop":
            return "desktop"
        if platform == "agent-email":
            mailbox_address = self._safe_text(
                self._effective_agent_email_settings().get("primary_mailbox_address")
            )
            if mailbox_address:
                return f"agent-email:{mailbox_address}"

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

    def _effective_agent_email_settings(self) -> dict[str, str]:
        stored = self.agent_email_integration_store.get_primary()
        if agent_email_integration_is_disabled(stored):
            return {
                "base_url": "",
                "api_token": "",
                "primary_mailbox_address": "",
                "webhook_secret": "",
                "webhook_signature_header": "X-Cosmic-Mail-Signature",
                "source": "integration_store_disabled",
            }
        if agent_email_integration_is_configured(stored):
            return {
                "base_url": str(stored.base_url).strip(),
                "api_token": str(stored.api_token).strip(),
                "primary_mailbox_address": str(
                    stored.primary_mailbox_address or ""
                ).strip(),
                "webhook_secret": str(stored.webhook_secret or "").strip(),
                "webhook_signature_header": str(
                    stored.webhook_signature_header or ""
                ).strip()
                or "X-Cosmic-Mail-Signature",
                "source": "integration_store",
            }
        return {
            "base_url": str(self.config.cosmic_mail_base_url or "").strip(),
            "api_token": str(self.config.cosmic_mail_api_token or "").strip(),
            "primary_mailbox_address": str(
                self.config.cosmic_mail_primary_mailbox_address or ""
            ).strip(),
            "webhook_secret": str(self.config.cosmic_mail_webhook_secret or "").strip(),
            "webhook_signature_header": str(
                self.config.cosmic_mail_webhook_signature_header or ""
            ).strip()
            or "X-Cosmic-Mail-Signature",
            "source": "env",
        }

    def _agent_email_effectively_enabled(self) -> bool:
        settings = self._effective_agent_email_settings()
        if settings.get("source") == "integration_store_disabled":
            return False
        return self.config.enable_agent_email or bool(
            settings.get("base_url") and settings.get("api_token")
        )

    async def _unregister_adapter(self, platform: str) -> None:
        adapter = self.registry.adapters.pop(platform, None)
        if adapter is None:
            return
        try:
            await adapter.stop()
        finally:
            self.adapter_errors.pop(platform, None)

    async def reconcile_agent_email_adapter(self) -> None:
        settings = self._effective_agent_email_settings()
        existing = self.registry.adapters.get("agent-email")
        if not self._agent_email_effectively_enabled():
            await self._unregister_adapter("agent-email")
            self.adapter_errors.pop("agent-email", None)
            return
        if not settings.get("base_url") or not settings.get("api_token"):
            await self._unregister_adapter("agent-email")
            self.adapter_errors["agent-email"] = (
                "Cosmic Mail base URL or API token is not configured."
            )
            return

        current_key = None
        if isinstance(existing, AgentEmailAdapter):
            current_key = (
                self._safe_text(existing.client.base_url),
                self._safe_text(getattr(existing.client, "api_token", "")),
                self._safe_text(existing.primary_mailbox_address),
                self._safe_text(existing.webhook_secret),
                self._safe_text(existing.webhook_signature_header),
            )
        next_key = (
            self._safe_text(settings.get("base_url")),
            self._safe_text(settings.get("api_token")),
            self._safe_text(settings.get("primary_mailbox_address")),
            self._safe_text(settings.get("webhook_secret")),
            self._safe_text(settings.get("webhook_signature_header")),
        )
        if current_key == next_key and existing is not None:
            return

        if existing is not None:
            await self._unregister_adapter("agent-email")

        adapter = AgentEmailAdapter(
            cosmic_mail_base_url=settings["base_url"],
            cosmic_mail_api_token=settings["api_token"],
            timeout_sec=self.config.cosmic_mail_timeout_sec,
            primary_mailbox_address=settings.get("primary_mailbox_address", ""),
            webhook_secret=settings.get("webhook_secret", ""),
            webhook_signature_header=settings.get(
                "webhook_signature_header", "X-Cosmic-Mail-Signature"
            ),
        )
        await adapter.on_message(self._handle_normalized_incoming_message)
        self.registry.register(adapter)
        try:
            await adapter.start()
            self.adapter_errors.pop("agent-email", None)
        except (
            Exception
        ) as exc:  # pragma: no cover - startup health is environment-dependent
            self.adapter_errors["agent-email"] = str(exc)

    async def get_agent_email_connection_status(self) -> dict[str, Any]:
        settings = self._effective_agent_email_settings()
        stored = self.agent_email_integration_store.get_primary()
        configured = bool(settings.get("base_url") and settings.get("api_token"))
        adapter = self.registry.adapters.get("agent-email")
        mail_status: dict[str, Any] | None = None
        if isinstance(adapter, AgentEmailAdapter):
            try:
                mail_status = await adapter.get_status()
                self.adapter_errors.pop("agent-email", None)
            except Exception as exc:
                self.adapter_errors["agent-email"] = str(exc)
                mail_status = {"status": "error", "error": str(exc)}
        return {
            "configured": configured,
            "explicitly_disconnected": settings.get("source")
            == "integration_store_disabled",
            "connected": bool(mail_status and mail_status.get("connected")),
            "adapter_registered": adapter is not None,
            "healthy": adapter is not None and "agent-email" not in self.adapter_errors,
            "last_error": self.adapter_errors.get("agent-email"),
            "base_url": settings.get("base_url") or "",
            "api_token": settings.get("api_token") or "",
            "primary_mailbox_address": settings.get("primary_mailbox_address") or "",
            "trusted_senders": list(stored.trusted_senders)
            if stored is not None
            else [],
            "config_source": settings.get("source") or "env",
            "mail": mail_status,
        }

    async def save_agent_email_connection(
        self,
        *,
        base_url: str,
        api_token: str,
        primary_mailbox_address: str | None = None,
    ) -> dict[str, Any]:
        normalized_base_url = normalize_cosmic_mail_base_url(base_url)
        normalized_api_token = self._safe_text(api_token)
        normalized_mailbox = self._safe_text(primary_mailbox_address)
        if not normalized_base_url or not normalized_api_token:
            raise ValueError("base_url and api_token are required")

        effective = await self._prepare_agent_email_connection_settings(
            base_url=normalized_base_url,
            api_token=normalized_api_token,
            primary_mailbox_address=normalized_mailbox,
        )
        self.agent_email_integration_store.save_primary(
            base_url=effective["base_url"],
            api_token=effective["api_token"],
            primary_mailbox_address=effective["primary_mailbox_address"],
            webhook_secret=self.config.cosmic_mail_webhook_secret,
            webhook_signature_header=self.config.cosmic_mail_webhook_signature_header,
            updated_at=utcnow_iso(),
        )
        await self.reconcile_agent_email_adapter()
        webhook = await self.sync_agent_email_webhook()
        status = await self.get_agent_email_connection_status()
        status["webhook"] = webhook
        status["token_scope"] = effective.get("token_scope")
        return status

    async def clear_agent_email_connection(self) -> dict[str, Any]:
        webhook = await self.clear_agent_email_webhook()
        self.agent_email_integration_store.clear_primary()
        await self._unregister_adapter("agent-email")
        status = await self.get_agent_email_connection_status()
        status["webhook"] = webhook
        return status

    async def save_agent_email_trusted_senders(
        self, trusted_senders: list[str]
    ) -> dict[str, Any]:
        self.agent_email_integration_store.save_trusted_senders(
            trusted_senders,
            updated_at=utcnow_iso(),
        )
        return await self.get_agent_email_connection_status()

    def _is_email_thread_session(self, session_id: str | None) -> bool:
        normalized = self._safe_text(session_id)
        return normalized.startswith("email-thread:")

    def _agent_email_message_identifiers(
        self, metadata: dict[str, Any] | None
    ) -> list[str]:
        if not isinstance(metadata, dict):
            return []
        identifiers: list[str] = []
        for raw in (metadata.get("message_id"), metadata.get("internet_message_id")):
            value = self._safe_text(raw)
            if value and value not in identifiers:
                identifiers.append(value)
        return identifiers

    def _agent_email_inbound_dedupe_keys(
        self, *, session_id: str, metadata: dict[str, Any] | None
    ) -> list[str]:
        normalized_session_id = self._safe_text(session_id)
        if not normalized_session_id:
            return []
        return [
            f"{normalized_session_id}|{identifier}".casefold()
            for identifier in self._agent_email_message_identifiers(metadata)
        ]

    def _find_duplicate_agent_email_request(
        self, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        session_id = self._safe_text(message.get("session_id"))
        channel = self._safe_text(message.get("channel"))
        if self._channel_platform(
            channel
        ) != "agent-email" and not self._is_email_thread_session(session_id):
            return None
        metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        identifiers = set(self._agent_email_message_identifiers(metadata))
        if not session_id or not identifiers:
            return None

        for request_record in reversed(list(self.request_records.values())):
            if self._safe_text(request_record.get("session_id")) != session_id:
                continue
            existing_message = (
                request_record.get("message")
                if isinstance(request_record.get("message"), dict)
                else {}
            )
            existing_metadata = (
                existing_message.get("metadata")
                if isinstance(existing_message.get("metadata"), dict)
                else {}
            )
            if self._safe_text(existing_metadata.get("platform")) != "agent-email":
                continue
            existing_identifiers = set(
                self._agent_email_message_identifiers(existing_metadata)
            )
            if identifiers.isdisjoint(existing_identifiers):
                continue
            return {
                "status": "duplicate",
                "duplicate": True,
                "request_id": self._safe_text(request_record.get("request_id")) or None,
                "session_id": session_id,
                "channel": self._safe_text(request_record.get("channel")) or channel,
            }

        for entry in reversed(self.session_store.get_history(session_id)):
            if self._safe_text(entry.get("role")) != "user":
                continue
            existing_metadata = (
                entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            )
            if self._safe_text(existing_metadata.get("platform")) != "agent-email":
                continue
            existing_identifiers = set(
                self._agent_email_message_identifiers(existing_metadata)
            )
            if identifiers.isdisjoint(existing_identifiers):
                continue
            return {
                "status": "duplicate",
                "duplicate": True,
                "request_id": self._safe_text(entry.get("request_id"))
                or self._safe_text(existing_metadata.get("request_id"))
                or None,
                "session_id": session_id,
                "channel": self._safe_text(entry.get("channel")) or channel,
            }
        return None

    def reserve_agent_email_inbound(
        self, message: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any] | None]:
        session_id = self._safe_text(message.get("session_id"))
        channel = self._safe_text(message.get("channel"))
        if self._channel_platform(
            channel
        ) != "agent-email" and not self._is_email_thread_session(session_id):
            return ([], None)
        metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        dedupe_keys = self._agent_email_inbound_dedupe_keys(
            session_id=session_id, metadata=metadata
        )
        duplicate = self._find_duplicate_agent_email_request(message)
        if duplicate is not None:
            return ([], duplicate)
        if any(key in self._inflight_agent_email_messages for key in dedupe_keys):
            return (
                [],
                {
                    "status": "duplicate",
                    "duplicate": True,
                    "request_id": None,
                    "session_id": session_id,
                    "channel": channel,
                },
            )
        self._inflight_agent_email_messages.update(dedupe_keys)
        return (dedupe_keys, None)

    def release_agent_email_inbound(self, dedupe_keys: list[str]) -> None:
        for key in dedupe_keys:
            self._inflight_agent_email_messages.discard(self._safe_text(key).casefold())

    def _email_thread_session_patch(
        self, metadata: dict[str, Any], *, channel: str
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "session_scope": "email_thread",
            "rollover_exempt": True,
        }
        mailbox_address = self._safe_text(metadata.get("mailbox_address"))
        mailbox_id = self._safe_text(metadata.get("mailbox_id"))
        thread_id = self._safe_text(metadata.get("thread_id"))
        subject = self._safe_text(metadata.get("subject"))
        from_address = self._safe_text(metadata.get("from_address"))
        from_name = self._safe_text(metadata.get("from_name"))
        if mailbox_address:
            patch["mailbox_address"] = mailbox_address
        if mailbox_id:
            patch["mailbox_id"] = mailbox_id
        if thread_id:
            patch["thread_id"] = thread_id
        if subject:
            patch["thread_subject"] = subject
        if from_address:
            patch["from_address"] = from_address
        if from_name:
            patch["from_name"] = from_name
        patch["channel"] = channel
        return patch

    def _prepare_channel_event_for_delivery(
        self,
        event: dict[str, Any],
        *,
        request_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prepared = dict(event)
        channel = self._safe_text(prepared.get("channel"))
        if self._channel_platform(channel or "") != "agent-email":
            return prepared
        if self._safe_text(prepared.get("type")) != "response.complete":
            return prepared

        request = request_record if isinstance(request_record, dict) else {}
        message = (
            request.get("message") if isinstance(request.get("message"), dict) else {}
        )
        message_meta = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        process_output = (
            request.get("email_process_inbound_output")
            if isinstance(request.get("email_process_inbound_output"), dict)
            else {}
        )
        session_id = self._safe_text(prepared.get("session_id")) or self._safe_text(
            request.get("session_id")
        )
        request_id = self._safe_text(prepared.get("request_id")) or self._safe_text(
            request.get("request_id")
        )
        session_meta = (
            self.session_store.get_session_metadata(session_id) if session_id else {}
        )
        user_message = (
            self.session_store.find_message_by_request_id(
                session_id,
                request_id=request_id,
                role="user",
            )
            if session_id and request_id
            else None
        )
        user_meta = (
            user_message.get("metadata")
            if isinstance(user_message, dict)
            and isinstance(user_message.get("metadata"), dict)
            else {}
        )

        def first_text(*values: Any) -> str | None:
            for value in values:
                text = self._safe_text(value)
                if text:
                    return text
            return None

        thread_id = first_text(
            prepared.get("thread_id"),
            message_meta.get("thread_id"),
            user_meta.get("thread_id"),
            session_meta.get("thread_id"),
        )
        if not thread_id:
            return prepared

        mailbox_address = first_text(
            prepared.get("mailbox_address"),
            message_meta.get("mailbox_address"),
            user_meta.get("mailbox_address"),
            session_meta.get("mailbox_address"),
        )
        mailbox_id = first_text(
            prepared.get("mailbox_id"),
            message_meta.get("mailbox_id"),
            user_meta.get("mailbox_id"),
            session_meta.get("mailbox_id"),
        )
        message_id = first_text(
            prepared.get("message_id"),
            process_output.get("message_id"),
            message_meta.get("message_id"),
            user_meta.get("message_id"),
            session_meta.get("message_id"),
        )
        from_address = first_text(
            prepared.get("from_address"),
            process_output.get("from_address"),
            message_meta.get("from_address"),
            user_meta.get("from_address"),
            session_meta.get("from_address"),
        )
        from_name = first_text(
            prepared.get("from_name"),
            message_meta.get("from_name"),
            user_meta.get("from_name"),
            session_meta.get("from_name"),
        )
        thread_subject = first_text(
            prepared.get("thread_subject"),
            prepared.get("subject"),
            process_output.get("subject"),
            message_meta.get("subject"),
            user_meta.get("subject"),
            session_meta.get("thread_subject"),
        )

        trusted_sender = bool(process_output.get("trusted_sender"))
        auto_reply = (
            process_output.get("auto_reply")
            if isinstance(process_output.get("auto_reply"), dict)
            else {}
        )
        auto_reply_status = self._safe_text(auto_reply.get("delivery_status")) or (
            "sent" if bool(auto_reply.get("sent")) else ""
        )
        auto_reply_acted = auto_reply_status in {"sent", "queued_for_approval"}
        auto_reply_sent = auto_reply_status == "sent"
        sender_role = first_text(
            process_output.get("sender_role"), "owner" if trusted_sender else "external"
        )
        matched_instructions = (
            process_output.get("matched_instructions")
            if isinstance(process_output.get("matched_instructions"), list)
            else []
        )
        if not matched_instructions and isinstance(
            process_output.get("matched_instruction"), dict
        ):
            matched_instructions = [process_output.get("matched_instruction")]

        prepared.setdefault("session_scope", "email_thread")
        prepared["thread_id"] = thread_id
        if mailbox_address:
            prepared["mailbox_address"] = mailbox_address
        if mailbox_id:
            prepared["mailbox_id"] = mailbox_id
        if message_id:
            prepared["message_id"] = message_id
        if from_address:
            prepared["from_address"] = from_address
        if from_name:
            prepared["from_name"] = from_name
        if thread_subject:
            prepared["thread_subject"] = thread_subject
            prepared.setdefault("subject", thread_subject)
        prepared["trusted_sender"] = trusted_sender
        if sender_role:
            prepared["sender_role"] = sender_role
        prepared["email_auto_reply_sent"] = auto_reply_sent
        if auto_reply_status:
            prepared["email_auto_reply_status"] = auto_reply_status
        if matched_instructions:
            prepared["matched_instructions"] = matched_instructions
            prepared["matched_instruction_ids"] = self._normalize_string_list(
                [
                    self._safe_text(item.get("instruction_id"))
                    for item in matched_instructions
                ],
                limit=12,
            )
            match_reason = self._safe_text(
                process_output.get("instruction_match_reason")
            )
            if match_reason:
                prepared["instruction_match_reason"] = match_reason
        prepared["email_thread_reply"] = True
        prepared["email_thread_reply_eligible"] = bool(
            trusted_sender and not auto_reply_acted
        )
        if (
            from_address
            and not prepared.get("to_recipients")
            and not prepared.get("to")
        ):
            prepared["to_recipients"] = [{"email": from_address, "name": from_name}]
        return prepared

    def _should_preprocess_email_inbound(self, request_record: dict[str, Any]) -> bool:
        if self._redis is None or not self._agent_email_effectively_enabled():
            return False
        if self._safe_text(request_record.get("route")) != "opus":
            return False
        channel = self._safe_text(request_record.get("channel")) or ""
        if self._channel_platform(channel) != "agent-email":
            return False
        message = request_record.get("message")
        if not isinstance(message, dict):
            return False
        metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        return bool(
            self._safe_text(metadata.get("thread_id"))
            and self._safe_text(metadata.get("message_id"))
        )

    def _build_email_inbound_orchestrator_query(
        self,
        *,
        original_content: str,
        process_output: dict[str, Any],
    ) -> str:
        summary = self._safe_text(process_output.get("summary")) or self._safe_text(
            process_output.get("response")
        )
        if not summary:
            return original_content

        matched_instruction = (
            process_output.get("matched_instruction")
            if isinstance(process_output.get("matched_instruction"), dict)
            else None
        )
        matched_instructions = (
            process_output.get("matched_instructions")
            if isinstance(process_output.get("matched_instructions"), list)
            else []
        )
        if not matched_instructions and matched_instruction:
            matched_instructions = [matched_instruction]
        auto_reply = (
            process_output.get("auto_reply")
            if isinstance(process_output.get("auto_reply"), dict)
            else None
        )
        attachments = (
            process_output.get("attachments")
            if isinstance(process_output.get("attachments"), list)
            else []
        )
        subject = self._safe_text(process_output.get("subject")) or None
        trusted_sender = bool(process_output.get("trusted_sender"))
        sender_role = self._safe_text(process_output.get("sender_role")) or "external"
        from_address = self._safe_text(process_output.get("from_address")) or None
        instruction_match_reason = (
            self._safe_text(process_output.get("instruction_match_reason")) or None
        )

        lines = [
            "The email specialist already processed this inbound email thread.",
        ]
        if subject:
            lines.append(f"Subject: {subject}")
        if from_address:
            lines.append(f"From: {from_address}")
        lines.append(
            "Trusted sender: yes. Treat this as a direct owner query arriving over email."
            if trusted_sender
            else f"Trusted sender: no. Sender role: {sender_role}."
        )
        lines.extend(
            [
                "",
                "Email specialist summary:",
                summary,
            ]
        )
        if matched_instructions:
            lines.extend(
                [
                    "",
                    "Matched standing instruction(s):",
                ]
            )
            for item in matched_instructions[:4]:
                label = self._safe_text(item.get("label")) or "standing instruction"
                raw_instruction = (
                    self._safe_text(item.get("raw_user_instruction")) or None
                )
                behavior = (
                    item.get("behavior")
                    if isinstance(item.get("behavior"), dict)
                    else {}
                )
                mode = self._safe_text(behavior.get("mode")) or "notify_only"
                completion_mode = (
                    self._safe_text(behavior.get("completion_mode")) or "perpetual"
                )
                lines.append(f"- {label} | mode={mode} | completion={completion_mode}")
                if raw_instruction:
                    lines.append(f"  User instruction: {raw_instruction}")
            if instruction_match_reason:
                lines.extend(
                    [
                        "",
                        f"Match reason: {instruction_match_reason}",
                    ]
                )
        if auto_reply:
            auto_reply_status = self._safe_text(auto_reply.get("delivery_status")) or (
                "sent" if bool(auto_reply.get("sent")) else ""
            )
            lines.append(
                "An automatic reply was already sent by the email specialist."
                if auto_reply_status == "sent"
                else "An automatic reply draft was queued for approval by the email specialist."
                if auto_reply_status == "queued_for_approval"
                else "The email specialist evaluated an auto-reply path but did not send anything."
            )
        if attachments:
            lines.append(
                f"{len(attachments)} attachment(s) were downloaded into the email specialist workflow and are kept private unless explicitly needed later."
            )
        lines.extend(
            [
                "",
                "Use this specialist-generated brief as the primary context for your response to the email thread.",
            ]
        )
        return "\n".join(lines).strip()

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
                "matched_by": "current_channel"
                if normalized_current
                else "desktop_default",
            }

        if normalized_target == "current":
            resolved_channel = normalized_current or "desktop"
            return {
                "delivery_target": normalized_target,
                "resolved_channel": resolved_channel,
                "platform": self._channel_platform(resolved_channel) or "desktop",
                "matched_by": "current_channel"
                if normalized_current
                else "desktop_default",
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

        resolved_channel = self._preferred_linked_channel(
            platform, current_channel=normalized_current
        )
        if resolved_channel:
            return {
                "delivery_target": platform,
                "resolved_channel": resolved_channel,
                "platform": platform,
                "matched_by": "linked_channel",
            }

        if (
            normalized_fallback
            and self._channel_platform(normalized_fallback) == platform
        ):
            return {
                "delivery_target": platform,
                "resolved_channel": normalized_fallback,
                "platform": platform,
                "matched_by": "stored_fallback",
            }

        raise ValueError(
            f"No linked {platform} channel is available yet. Ask from that channel first or provide an exact channel."
        )

    def _compact_scheduler_working_set(
        self, working_set: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(working_set, dict):
            return None
        snapshot: dict[str, Any] = {}
        goal = self._safe_text(working_set.get("goal"))
        if goal:
            snapshot["goal"] = self._bounded_excerpt(goal, limit=240)
        active_workstreams = self._normalize_string_list(
            working_set.get("active_workstreams"), limit=4
        )
        if active_workstreams:
            snapshot["active_workstreams"] = active_workstreams
        open_loops = self._normalize_string_list(working_set.get("open_loops"), limit=4)
        if open_loops:
            snapshot["open_loops"] = open_loops
        active_task_refs = self._normalize_string_list(
            working_set.get("active_task_refs"), limit=4
        )
        if active_task_refs:
            snapshot["active_task_refs"] = active_task_refs
        focus_entities = self._normalize_entity_list(
            working_set.get("current_focus_entities"), limit=4
        )
        if focus_entities:
            snapshot["current_focus_entities"] = [
                self._safe_text(item.get("label"))
                or self._safe_text(item.get("id"))
                or "entity"
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
            packet["context_summary"] = self._bounded_excerpt(
                context_summary, limit=500
            )

        request_record = None
        normalized_request_id = self._safe_text(created_request_id)
        if normalized_request_id and isinstance(
            self.request_records.get(normalized_request_id), dict
        ):
            request_record = self.request_records[normalized_request_id]

        original_request = None
        prior_context: list[dict[str, Any]] = []
        working_set_snapshot = None
        memory_context_excerpt = None

        if isinstance(request_record, dict):
            message = (
                request_record.get("message")
                if isinstance(request_record.get("message"), dict)
                else {}
            )
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
            prior_context = self._build_conversation_context(
                created_session_id, limit=6
            )

        original_request = original_request or prompt
        if original_request:
            packet["original_request"] = self._bounded_excerpt(
                original_request, limit=500
            )

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

    def _scheduler_context_conversation(
        self, context_packet: dict[str, Any] | None
    ) -> list[dict[str, str]]:
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

    def _render_scheduler_context_block(
        self, context_packet: dict[str, Any] | None
    ) -> str | None:
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
            active_workstreams = self._normalize_string_list(
                working_set.get("active_workstreams"), limit=4
            )
            if active_workstreams:
                lines.extend(["", "- Active workstreams at creation:"])
                lines.extend(f"  - {item}" for item in active_workstreams)
            open_loops = self._normalize_string_list(
                working_set.get("open_loops"), limit=4
            )
            if open_loops:
                lines.extend(["", "- Open loops at creation:"])
                lines.extend(f"  - {item}" for item in open_loops)

        memory_context_excerpt = self._safe_text(
            context_packet.get("memory_context_excerpt")
        )
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

    def _scheduler_record(
        self, record: dict[str, Any], *, include_history: bool = False
    ) -> dict[str, Any]:
        payload = dict(record)
        metadata = (
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        )
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
        payload["delivery_target"] = (
            self._safe_text(metadata.get("delivery_target")) or None
        )
        payload["delivery_channel"] = (
            self._safe_text(metadata.get("delivery_channel")) or "desktop"
        )
        payload["resolved_delivery_channel"] = payload["delivery_channel"]
        payload["one_shot"] = bool(metadata.get("one_shot"))
        payload["created_by"] = self._safe_text(metadata.get("created_by"))
        payload["created_request_id"] = self._safe_text(
            metadata.get("created_request_id")
        )
        payload["created_session_id"] = self._safe_text(
            metadata.get("created_session_id")
        )
        payload["created_channel"] = self._safe_text(metadata.get("created_channel"))
        payload["explicit_timezone"] = bool(metadata.get("explicit_timezone"))
        context_packet = (
            metadata.get("context_packet")
            if isinstance(metadata.get("context_packet"), dict)
            else {}
        )
        payload["context_summary"] = self._safe_text(
            metadata.get("context_summary")
        ) or self._safe_text(context_packet.get("context_summary"))
        if include_history:
            payload["history"] = self.scheduler_store.list_cron_history(
                self._safe_text(payload.get("cron_id")) or "",
                limit=20,
            )
        return payload

    def _list_scheduler_crons(
        self, *, include_system: bool, active_only: bool
    ) -> list[dict[str, Any]]:
        records = self.scheduler_store.list_crons()
        if not include_system:
            records = [
                item
                for item in records
                if self._safe_text(item.get("kind")) != "system"
            ]
        if active_only:
            records = [
                item
                for item in records
                if bool(self._safe_text(item.get("next_fire_at")) or item.get("paused"))
            ]
        return [self._scheduler_record(item, include_history=False) for item in records]

    def _cron_execution_identity(
        self, cron_id: str, scheduled_for: str | None
    ) -> tuple[str, str]:
        base = f"{cron_id}:{scheduled_for or 'unscheduled'}".encode("utf-8")
        digest = hashlib.sha256(base).hexdigest()[:16]
        return (
            f"req_cron_{digest}",
            f"cron:{cron_id}:{scheduled_for or 'unscheduled'}",
        )

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
        if (
            not normalized_label
            or not normalized_prompt
            or not normalized_cron_expression
        ):
            raise ValueError("label, cron_expression, and prompt are required")

        effective_timezone = self._scheduler_effective_timezone(timezone_name)
        next_fire_at = compute_next_fire_at(
            normalized_cron_expression,
            effective_timezone,
        )
        resolution = self.resolve_channel_target(
            delivery_target=self._safe_text(delivery_target)
            or self._safe_text(delivery_channel),
            current_channel=self._safe_text(created_channel),
            fallback_channel=self._safe_text(delivery_channel),
        )
        normalized_delivery_target = self._safe_text(resolution.get("delivery_target"))
        normalized_delivery_channel = (
            self._safe_text(resolution.get("resolved_channel")) or "desktop"
        )
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

    async def _build_scheduler_request_record(
        self, cron: dict[str, Any]
    ) -> dict[str, Any]:
        cron_id = self._safe_text(cron.get("cron_id")) or ""
        metadata = (
            cron.get("metadata") if isinstance(cron.get("metadata"), dict) else {}
        )
        prompt = self._safe_text(metadata.get("prompt"))
        resolution = self.resolve_channel_target(
            delivery_target=self._safe_text(metadata.get("delivery_target"))
            or self._safe_text(metadata.get("delivery_channel")),
            current_channel=self._safe_text(metadata.get("created_channel")),
            fallback_channel=self._safe_text(metadata.get("delivery_channel")),
        )
        channel = self._safe_text(resolution.get("resolved_channel")) or "desktop"
        timezone_name = (
            self._safe_text(cron.get("timezone")) or self.current_user_timezone()
        )
        scheduled_for = self._safe_text(cron.get("next_fire_at"))
        if not prompt:
            raise RuntimeError(f"Cron {cron_id} is missing its prompt payload.")

        request_id, idempotency_key = self._cron_execution_identity(
            cron_id, scheduled_for
        )
        session_id = self._current_session_id()
        session_metadata = self._ensure_session_state_seeded(session_id)
        active_working_set = (
            session_metadata.get("active_working_set")
            if isinstance(session_metadata.get("active_working_set"), dict)
            else None
        )
        context_packet = (
            metadata.get("context_packet")
            if isinstance(metadata.get("context_packet"), dict)
            else None
        )
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
                    "delivery_target": self._safe_text(
                        resolution.get("delivery_target")
                    ),
                },
            },
            # Cron jobs should run as fresh tasks. Creation-time chat context stays available in
            # the stored reminder context block, but we do not replay it as live conversation
            # history or the model can continue the reminder-creation thread instead of executing
            # the reminder prompt itself.
            "assembled_conversation_context": [],
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
                "core_fact_items": memory_prompt_context.core_fact_items,
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

    async def _execute_custom_scheduler_cron(
        self, cron: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        cron_id = self._safe_text(cron.get("cron_id")) or ""
        cron_expr = self._safe_text(cron.get("cron_expr"))
        timezone_name = (
            self._safe_text(cron.get("timezone")) or self.current_user_timezone()
        )
        metadata = (
            cron.get("metadata") if isinstance(cron.get("metadata"), dict) else {}
        )
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
                text = (
                    scheduled_for[:-1] + "+00:00"
                    if scheduled_for.endswith("Z")
                    else scheduled_for
                )
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
            f"Reminder ran: {label}" if one_shot else f"Scheduled task ran: {label}"
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
                    await self._finalize_rollover_sessions(
                        current_session_id=self._current_session_id()
                    )
                    status = "completed"
                    summary = "Daily rollover finalized."
                    next_fire_at = self._next_rollover_fire_at(
                        timezone_name=self.current_user_timezone()
                    )
                else:
                    (
                        status,
                        summary,
                        next_fire_at,
                    ) = await self._execute_custom_scheduler_cron(cron)
            except Exception as exc:
                logger.exception("gateway.scheduler_cron_failed cron_id=%s", cron_id)
                status = "failed"
                summary = str(exc)
                metadata = (
                    cron.get("metadata")
                    if isinstance(cron.get("metadata"), dict)
                    else {}
                )
                if not bool(metadata.get("one_shot")) and self._safe_text(
                    cron.get("cron_expr")
                ):
                    try:
                        next_fire_at = compute_next_fire_at(
                            self._safe_text(cron.get("cron_expr")) or "",
                            self._safe_text(cron.get("timezone"))
                            or self.current_user_timezone(),
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

    def list_scheduler_crons(
        self, *, include_system: bool = True, active_only: bool = False
    ) -> list[dict[str, Any]]:
        return self._list_scheduler_crons(
            include_system=include_system, active_only=active_only
        )

    def get_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        record = self.scheduler_store.get_cron(cron_id)
        if record is None:
            return None
        return self._scheduler_record(record, include_history=True)

    def pause_scheduler_cron(
        self, cron_id: str, *, reason: str | None = None
    ) -> dict[str, Any] | None:
        record = self.scheduler_store.pause_cron(cron_id, reason=reason)
        if record is None:
            return None
        self._scheduler_wakeup.set()
        return self._scheduler_record(record, include_history=False)

    def resume_scheduler_cron(self, cron_id: str) -> dict[str, Any] | None:
        next_fire_at = None
        if cron_id == SYSTEM_CRON_DAILY_ROLLOVER:
            next_fire_at = self._next_rollover_fire_at(
                timezone_name=self.current_user_timezone()
            )
        else:
            existing = self.scheduler_store.get_cron(cron_id)
            if existing is not None:
                cron_expr = self._safe_text(existing.get("cron_expr"))
                timezone_name = (
                    self._safe_text(existing.get("timezone"))
                    or self.current_user_timezone()
                )
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

        if "mobile" not in self.registry.adapters:
            mobile_adapter = MobileAdapter()
            await mobile_adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(mobile_adapter)
            await mobile_adapter.start()

        if self.config.enable_whatsapp and "whatsapp" not in self.registry.adapters:
            adapter = WhatsAppAdapter(WhatsAppConfig.from_env())
            await adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(adapter)

            try:
                await adapter.start()
                self.adapter_errors.pop(adapter.platform, None)
            except (
                Exception
            ) as exc:  # pragma: no cover - startup health is environment-dependent
                self.adapter_errors[adapter.platform] = str(exc)

        await self.reconcile_agent_email_adapter()

        if self.config.enable_telegram and "telegram" not in self.registry.adapters:
            adapter = TelegramAdapter(
                TelegramConfig.from_env(gateway_public_host=self.config.public_host)
            )
            await adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(adapter)

            try:
                await adapter.start()
                self.adapter_errors.pop(adapter.platform, None)
            except (
                Exception
            ) as exc:  # pragma: no cover - startup health is environment-dependent
                self.adapter_errors[adapter.platform] = str(exc)

    async def _handle_normalized_incoming_message(
        self, message: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.process_incoming_user_message(message)

    async def process_incoming_user_message(
        self, message: dict[str, Any]
    ) -> dict[str, Any]:
        content = str(message.get("content") or "").strip()
        channel = str(message.get("channel") or "").strip()
        metadata = message.get("metadata")
        conversation_context = message.get("conversation_context")
        if not channel:
            raise ValueError("Incoming message is missing channel")
        if not isinstance(metadata, dict):
            metadata = {}
        else:
            metadata = dict(metadata)
        if not isinstance(conversation_context, list):
            conversation_context = []
        gateway_preferences = self.get_desktop_preferences_snapshot()
        visual_response_preference = (
            gateway_preferences.get("visual_response_enhancement")
            if isinstance(gateway_preferences.get("visual_response_enhancement"), dict)
            else {}
        )
        visual_response_enhancement_enabled = bool(
            visual_response_preference.get("enabled", True)
            and self._channel_platform(channel) == "desktop"
        )
        metadata["gateway_preferences"] = gateway_preferences
        metadata["visual_response_enhancement_enabled"] = (
            visual_response_enhancement_enabled
        )
        route_override = self._normalize_route_override(
            message.get("route_override")
            if message.get("route_override") is not None
            else metadata.get("route_override")
        )

        requested_session_id = self._safe_text(message.get("session_id"))
        session_id = self._resolve_session_id(requested_session_id)
        source_id = (
            self._safe_text(metadata.get("message_id"))
            or self._safe_text(metadata.get("internet_message_id"))
            or self._safe_text(metadata.get("sender_jid"))
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
            "gateway_preferences": gateway_preferences,
            "visual_response_enhancement_enabled": (
                visual_response_enhancement_enabled
            ),
        }
        if self._channel_platform(
            channel
        ) == "agent-email" or self._is_email_thread_session(session_id):
            self.session_store.update_session_metadata(
                session_id,
                self._email_thread_session_patch(metadata, channel=channel),
            )
            await self._finalize_rollover_sessions(
                current_session_id=self._current_session_id()
            )
        else:
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
        self._maybe_record_contested_memory_claims(
            record_session_id=session_id,
            source_session_ids=[requested_session_id] if requested_session_id else None,
            content=content,
        )
        active_working_set = self._refresh_active_working_set(session_id)
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
        memory_prompt_context = self._apply_memory_prompt_overrides(
            session_id=session_id,
            memory_prompt_context=memory_prompt_context,
        )
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
        dispatch_target = (
            "orchestrator" if classification["route"] == "opus" else "gateway"
        )
        input_artifacts = await self._persist_inbound_artifacts(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            metadata=metadata,
        )

        user_message_metadata = {
            "request_id": request_id,
            "platform": metadata.get("platform"),
            "message_type": metadata.get("message_type"),
            "attachments": metadata.get("attachments"),
            "input_artifacts": input_artifacts,
            "gateway_preferences": gateway_preferences,
            "visual_response_enhancement_enabled": (
                visual_response_enhancement_enabled
            ),
        }
        if self._channel_platform(channel) == "agent-email":
            for key in (
                "thread_id",
                "message_id",
                "internet_message_id",
                "mailbox_address",
                "mailbox_id",
                "subject",
                "from_address",
                "from_name",
                "session_scope",
                "rollover_exempt",
            ):
                value = metadata.get(key)
                if value is not None:
                    user_message_metadata[key] = value

        self._append_session_message(
            session_id,
            role="user",
            content=content or "[non-text inbound message]",
            channel=channel,
            metadata=user_message_metadata,
        )

        # Cross-channel sync: push the user message to connected realtime clients on other platforms.
        if channel:
            self._track_background_task(
                self._broadcast_cross_channel_to_realtime_clients(
                    session_id,
                    role="user",
                    content=content or "[non-text inbound message]",
                    channel=channel,
                    attachments=metadata.get("attachments")
                    if isinstance(metadata.get("attachments"), list)
                    else None,
                    input_artifacts=input_artifacts,
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
            "gateway_preferences": gateway_preferences,
            "visual_response_enhancement_enabled": (
                visual_response_enhancement_enabled
            ),
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
            signals=classification.get("signals")
            if isinstance(classification.get("signals"), list)
            else [],
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
            raise ValueError(
                "Request record is missing channel, request_id, or session_id"
            )

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
            delivery_event = self._prepare_channel_event_for_delivery(
                {
                    **event,
                    "channel": channel,
                },
                request_record=request_record,
            )
            delivery_status = await self._deliver_or_queue_channel_event(
                delivery_event,
                channel=channel,
            )
            if self._safe_text(delivery_event.get("type")) == "response.complete":
                self._persist_email_delivery_metadata(delivery_event)
            trace_status = delivery_status
            if self._channel_platform(channel) == "agent-email":
                email_delivery = self._effective_email_delivery(delivery_event)
                email_delivery_status = self._safe_text(email_delivery.get("status"))
                if email_delivery_status:
                    trace_status = email_delivery_status
            else:
                email_delivery = None
            self._trace_request_event(
                request_id=self._safe_text(delivery_event.get("request_id"))
                or request_id,
                session_id=self._safe_text(delivery_event.get("session_id"))
                or session_id,
                channel=self._safe_text(delivery_event.get("channel")) or channel,
                route=self._safe_text(delivery_event.get("route")) or route,
                event_type=f"delivery.{self._safe_text(delivery_event.get('type')) or 'event'}",
                stage="delivery",
                status=trace_status or "delivered",
                title="Channel delivery completed"
                if delivery_status == "sent"
                else "Channel delivery deferred",
                detail=(
                    f"Gateway delivery={delivery_status}"
                    + (
                        f"; email delivery={self._safe_text(email_delivery.get('status'))}"
                        if isinstance(email_delivery, dict)
                        and self._safe_text(email_delivery.get("status"))
                        else ""
                    )
                ),
                task_id=self._safe_text(delivery_event.get("task_id")) or None,
                specialist_receipts=delivery_event.get("specialist_receipts")
                if isinstance(delivery_event.get("specialist_receipts"), list)
                else None,
                delivery=email_delivery if isinstance(email_delivery, dict) else None,
                metadata={"gateway_delivery_status": delivery_status},
                completed=self._safe_text(delivery_event.get("type"))
                in {"response.complete", "task.failed", "task.cancelled", "error"},
            )
            await self._maybe_schedule_delivered_memory_ingest(
                delivery_event,
                delivery_status=delivery_status,
            )
            await self._maybe_schedule_delivered_task_summary_write(
                delivery_event,
                delivery_status=delivery_status,
            )
            await self._maybe_schedule_delivered_email_instruction_update(
                delivery_event,
                delivery_status=delivery_status,
            )
            await self._maybe_schedule_delivered_turn_finalization(
                delivery_event,
                delivery_status=delivery_status,
            )

        def store_assistant_message(
            content: str,
            *,
            awaiting_reply: bool,
            metadata: dict[str, Any] | None,
            channel: str,
            route: str,
        ) -> str | None:
            assistant_metadata = dict(metadata or {})
            assistant_metadata.setdefault("request_id", request_id)
            gateway_preferences = request_record.get("gateway_preferences")
            if isinstance(gateway_preferences, dict):
                assistant_metadata.setdefault("gateway_preferences", gateway_preferences)
            if "visual_response_enhancement_enabled" in request_record:
                assistant_metadata.setdefault(
                    "visual_response_enhancement_enabled",
                    bool(request_record.get("visual_response_enhancement_enabled")),
                )
            if active_request is not None and not active_request.foreground:
                assistant_metadata["background"] = True
            return self._append_session_message(
                session_id,
                role="assistant",
                content=content,
                route=route,
                awaiting_reply=awaiting_reply,
                channel=channel,
                metadata=assistant_metadata,
                in_reply_to_request_id=request_id,
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
                raise RuntimeError(
                    f"Unsupported direct-model handoff route: {handoff.route}"
                )
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

        handoff_count = (
            self._coerce_int(request_record.get("direct_model_handoff_count")) or 0
        )
        if handoff_count >= 1:
            raise RuntimeError(
                "Direct model requested more than one Opus handoff for the same request."
            )

        original_decision_source = (
            self._safe_text(request_record.get("routing_decision_source"))
            or "model_router"
        )
        request_record.setdefault("initial_route", normalized_prior_route)
        request_record.setdefault(
            "initial_routing_decision_source", original_decision_source
        )

        classification = (
            dict(request_record.get("classification"))
            if isinstance(request_record.get("classification"), dict)
            else {}
        )
        signals = (
            list(classification.get("signals"))
            if isinstance(classification.get("signals"), list)
            else []
        )
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
        self._trace_request_event(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route="opus",
            event_type="request.direct_model_handoff",
            stage="routing",
            status="active",
            title="Direct model handed off to Opus",
            detail=f"{normalized_prior_route} -> opus",
            task_id=active_request.task_id if active_request is not None else None,
            metadata={"from_route": normalized_prior_route, "to_route": "opus"},
        )

        if active_request is not None:
            active_request.route = "opus"

        message = (
            request_record.get("message")
            if isinstance(request_record.get("message"), dict)
            else {}
        )
        query_text = self._safe_text(message.get("content")) or "[empty message]"
        assembled_conversation_context = (
            request_record.get("assembled_conversation_context")
            if isinstance(request_record.get("assembled_conversation_context"), list)
            else None
        )
        route_override = (
            normalized_prior_route if "manual_route_override" in signals else None
        )
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

        if self._is_realtime_client_channel(channel):
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
        await self._ensure_request_email_processed(request_record)
        await self._ensure_request_documents_parsed(request_record, send=send)
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
            raise ValueError(
                "Request record is missing channel, request_id, or session_id"
            )

        # Reject if there is already a foreground stream on this channel
        enforce_channel_guard = self._channel_platform(
            channel
        ) != "agent-email" and not self._is_email_thread_session(session_id)
        has_foreground = enforce_channel_guard and any(
            s.foreground and not s.completed and s.channel == channel
            for s in self.active_requests.values()
        )
        if has_foreground:
            raise ValueError(
                "A foreground task is already active on this channel. "
                "Background it first or wait for it to complete."
            )

        query_text = (
            self._safe_text(request_record.get("query"))
            or self._safe_text(request_record.get("content"))
            or ""
        )
        state = ActiveRequest(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route=route,
            user_query_excerpt=query_text[:120].strip(),
        )
        self.active_requests[request_id] = state
        self._trace_request_event(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route=route,
            event_type="request.accepted",
            stage="accepted",
            status="active",
            title="Request accepted",
            detail=query_text[:240].strip() or None,
            source=self._safe_text(request_record.get("source")) or None,
            source_id=self._safe_text(request_record.get("source_id")) or None,
            user_query_excerpt=query_text[:240].strip() or None,
            metadata={
                "gateway_preferences": request_record.get("gateway_preferences"),
                "visual_response_enhancement_enabled": bool(
                    request_record.get("visual_response_enhancement_enabled", True)
                ),
            },
        )
        state.worker = asyncio.create_task(
            self._run_request_fulfillment(state, request_record)
        )

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
        normalized_request_id = self._safe_text(target_request_id) or self._safe_text(
            request_id
        )
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

    async def background_active_request(self, *, channel: str, request_id: str) -> bool:
        """Move a foreground request to background. Returns True on success."""
        state = self.active_requests.get(request_id)
        if state is None or state.channel != channel or state.completed:
            return False
        if not state.foreground:
            return True  # already background

        # Enforce per-session background task limit
        bg_count = sum(
            1
            for s in self.active_requests.values()
            if not s.foreground and not s.completed and s.session_id == state.session_id
        )
        if bg_count >= self.config.max_background_tasks_per_session:
            return False

        state.foreground = False
        state.backgrounded_at = utcnow_iso()
        await self._deliver_or_queue_channel_event(
            {
                "type": "task.backgrounded",
                "request_id": request_id,
                "session_id": state.session_id,
                "task_id": state.task_id,
                "channel": channel,
                "route": state.route,
                "user_query_excerpt": state.user_query_excerpt,
                "partial_content": state.partial_content[:500]
                if state.partial_content
                else "",
                "response_blocks": state.response_blocks_snapshot,
                "snapshot_seq": state.snapshot_seq or None,
            },
            channel=channel,
        )
        return True

    async def foreground_background_request(
        self, *, channel: str, request_id: str
    ) -> bool:
        """Move a background request back to foreground. Returns True on success."""
        state = self.active_requests.get(request_id)

        # ---- Still-running background task (in active_requests) ----
        if state is not None:
            if state.channel != channel:
                return False
            if state.foreground:
                return True  # already foreground
            # Reject if another foreground stream is active on this channel
            has_foreground = any(
                s.foreground and not s.completed and s.channel == channel
                for s in self.active_requests.values()
            )
            if has_foreground:
                return False
            state.foreground = True
            state.backgrounded_at = None
            # If the assistant message was already stored while backgrounded,
            # clear the flag now so build_resume_payload won't reconstruct it.
            self.session_store.clear_background_flag(state.session_id, request_id)
            await self._deliver_or_queue_channel_event(
                {
                    "type": "task.foregrounded",
                    "request_id": request_id,
                    "session_id": state.session_id,
                    "task_id": state.task_id,
                    "channel": channel,
                    "partial_content": state.partial_content,
                    "partial_thinking": state.partial_thinking,
                    "response_blocks": state.response_blocks_snapshot,
                    "snapshot_seq": state.snapshot_seq or None,
                    "completed": state.completed,
                },
                channel=channel,
            )
            return True

        # ---- Completed background task (already finalized) ----
        # Reconstruct from session history so the desktop can show the result
        # in the main chat surface and remove it from the background task list.
        session_id = self._current_session_id()
        history = self.session_store.get_history(session_id)
        completed_msg: dict[str, Any] | None = None
        for msg in history:
            if msg.get("role") != "assistant":
                continue
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            if not meta.get("background"):
                continue
            reply_to = msg.get("in_reply_to_request_id") or meta.get("request_id")
            if reply_to == request_id:
                completed_msg = msg
                break
        if completed_msg is None:
            return False
        meta = (
            completed_msg.get("metadata")
            if isinstance(completed_msg.get("metadata"), dict)
            else {}
        )
        # Clear the background flag so build_resume_payload won't reconstruct it
        self.session_store.clear_background_flag(session_id, request_id)
        await self._deliver_or_queue_channel_event(
            {
                "type": "task.foregrounded",
                "request_id": request_id,
                "session_id": session_id,
                "task_id": meta.get("task_id"),
                "channel": channel,
                "partial_content": completed_msg.get("content") or "",
                "partial_thinking": meta.get("thinking_text") or "",
                "activity_log": meta.get("activity_log"),
                "sources": meta.get("sources"),
                "produced_artifacts": meta.get("produced_artifacts"),
                "response_blocks": meta.get("response_blocks"),
                "completed": True,
            },
            channel=channel,
        )
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.session_store.list_sessions()

    async def list_mobile_devices(self, *, limit: int = 100) -> list[dict[str, Any]]:
        devices = self.mobile_device_store.list_devices(limit=limit)
        adapter = self.registry.adapters.get("mobile")
        active_by_device: dict[str, dict[str, Any]] = {}
        if isinstance(adapter, MobileAdapter):
            for connection in await adapter.list_connections():
                device_id = str(connection.get("device_id") or "").strip()
                if not device_id:
                    continue
                active_by_device[device_id] = connection

        hydrated: list[dict[str, Any]] = []
        for device in devices:
            device_id = str(device.get("device_id") or "").strip()
            live = active_by_device.get(device_id)
            hydrated.append(
                {
                    **device,
                    "active": live is not None,
                    "current_channel": live.get("channel") if live else None,
                    "current_session_id": live.get("session_id") if live else None,
                }
            )
        return hydrated

    def is_mobile_device_revoked(self, device_id: str) -> bool:
        return self.mobile_device_store.is_revoked(device_id)

    def authorize_mobile_device(
        self,
        device_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_device_id = str(device_id or "").strip()
        if not normalized_device_id:
            raise ValueError("device_id is required")
        return self.mobile_device_store.authorize_device(
            normalized_device_id,
            channel=f"mobile:{normalized_device_id}",
            metadata=metadata,
        )

    def record_mobile_device_connection(
        self, device_id: str, *, channel: str | None = None
    ) -> dict[str, Any]:
        return self.mobile_device_store.record_connected(device_id, channel=channel)

    def record_mobile_device_session(
        self,
        device_id: str,
        *,
        session_id: str | None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        return self.mobile_device_store.record_session(
            device_id, session_id=session_id, channel=channel
        )

    def record_mobile_device_disconnected(
        self, device_id: str
    ) -> dict[str, Any] | None:
        return self.mobile_device_store.record_disconnected(device_id)

    def update_mobile_push_token(
        self,
        device_id: str,
        *,
        push_token: str | None,
        fcm_token: str | None = None,
        notifications_enabled: bool | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.mobile_device_store.update_push_token(
            device_id,
            push_token=push_token,
            fcm_token=fcm_token,
            notifications_enabled=notifications_enabled,
            preferences=preferences,
        )

    def clear_mobile_push_token(self, device_id: str) -> dict[str, Any] | None:
        return self.mobile_device_store.clear_push_token(device_id)

    async def _clear_mobile_push_token_async(self, device_id: str) -> None:
        self.clear_mobile_push_token(device_id)

    def clear_mobile_fcm_token(self, device_id: str) -> dict[str, Any] | None:
        return self.mobile_device_store.clear_fcm_token(device_id)

    async def _clear_mobile_fcm_token_async(self, device_id: str) -> None:
        self.clear_mobile_fcm_token(device_id)

    def update_mobile_device_presence(
        self,
        device_id: str,
        *,
        state: str,
        visible_screen: str | None = None,
    ) -> dict[str, Any]:
        return self.mobile_device_store.update_presence(
            device_id,
            state=state,
            visible_screen=visible_screen,
        )

    async def revoke_mobile_device(
        self, device_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        record = self.mobile_device_store.revoke_device(device_id, reason=reason)
        adapter = self.registry.adapters.get("mobile")
        if isinstance(adapter, MobileAdapter):
            await adapter.close_connection(
                f"mobile:{str(device_id or '').strip()}",
                code=4004,
                reason="This device was removed from desktop.",
            )
        return record

    async def revoke_all_mobile_devices(
        self, *, reason: str | None = None
    ) -> dict[str, Any]:
        result = self.mobile_device_store.revoke_all_devices(reason=reason)
        adapter = self.registry.adapters.get("mobile")
        if isinstance(adapter, MobileAdapter):
            for connection in await adapter.list_connections():
                channel = str(connection.get("channel") or "").strip()
                if not channel:
                    continue
                await adapter.close_connection(
                    channel,
                    code=4004,
                    reason="This device was removed from desktop.",
                )
        return result

    def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        history = self.session_store.get_history(session_id)
        return [self._hydrate_history_message_for_client(item) for item in history]

    def list_session_request_traces(
        self, session_id: str, *, limit: int = 40
    ) -> list[dict[str, Any]]:
        return self.request_trace_store.list_session_traces(session_id, limit=limit)

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
            "messages": [
                self._hydrate_history_message_for_client(item) for item in messages
            ],
        }

    def search_session_produced_artifacts(
        self,
        session_id: str | None,
        *,
        query: str | None = None,
        limit: int = 8,
        all_sessions: bool = False,
    ) -> dict[str, Any]:
        normalized_session_id = self._safe_text(session_id)
        normalized_query = self._safe_text(query)
        normalized_limit = max(1, min(20, int(limit)))
        if all_sessions:
            sessions = [
                self._safe_text(item.get("id"))
                for item in self.session_store.list_sessions(limit=200)
                if isinstance(item, dict) and self._safe_text(item.get("id"))
            ]
        elif normalized_session_id:
            sessions = [normalized_session_id]
        else:
            return {
                "session_id": None,
                "query": normalized_query,
                "all_sessions": all_sessions,
                "results": [],
                "count": 0,
                "message": "session_id is required unless all_sessions=true.",
            }

        query_tokens = [
            token
            for token in (normalized_query or "").lower().replace("_", " ").split()
            if token
        ]
        seen: set[tuple[str, str, str]] = set()
        scored: list[tuple[int, dict[str, Any]]] = []
        for current_session_id in sessions:
            for turn in self.session_store.list_all_turn_ledger(current_session_id):
                if not isinstance(turn, dict):
                    continue
                metadata = (
                    turn.get("metadata")
                    if isinstance(turn.get("metadata"), dict)
                    else {}
                )
                produced_artifacts = self._normalize_produced_artifact_list(
                    metadata.get("produced_artifacts")
                )
                for artifact in produced_artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    artifact_id = self._safe_text(artifact.get("artifact_id")) or ""
                    artifact_path = self._safe_text(artifact.get("path")) or ""
                    dedupe_key = (current_session_id, artifact_id, artifact_path)
                    if not any(dedupe_key[1:]) or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    record = self._build_recalled_artifact_record(
                        session_id=current_session_id,
                        turn_entry=turn,
                        artifact=artifact,
                    )
                    score = self._score_recalled_artifact(
                        record, query=normalized_query, query_tokens=query_tokens
                    )
                    if normalized_query and score <= 0:
                        continue
                    scored.append((score, record))

        scored.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("created_at") or ""),
                str(item[1].get("filename") or ""),
            ),
            reverse=True,
        )
        results = [record for _, record in scored[:normalized_limit]]
        response = {
            "session_id": normalized_session_id,
            "query": normalized_query,
            "all_sessions": all_sessions,
            "results": results,
            "count": len(results),
        }
        if not results:
            response["message"] = "No matching produced artifacts found."
        return response

    def resolve_session_artifacts(
        self,
        *,
        session_id: str | None,
        artifact_ids: list[str] | None = None,
        all_sessions: bool = False,
    ) -> dict[str, Any]:
        normalized_session_id = self._safe_text(session_id)
        normalized_ids = [
            self._safe_text(item)
            for item in (artifact_ids or [])
            if self._safe_text(item)
        ]
        if not normalized_ids:
            return {
                "session_id": normalized_session_id,
                "artifact_ids": [],
                "all_sessions": all_sessions,
                "artifacts": [],
                "count": 0,
                "message": "artifact_ids is required.",
            }

        if all_sessions:
            sessions = [
                self._safe_text(item.get("id"))
                for item in self.session_store.list_sessions(limit=200)
                if isinstance(item, dict) and self._safe_text(item.get("id"))
            ]
        elif normalized_session_id:
            sessions = [normalized_session_id]
        else:
            return {
                "session_id": None,
                "artifact_ids": normalized_ids,
                "all_sessions": all_sessions,
                "artifacts": [],
                "count": 0,
                "message": "session_id is required unless all_sessions=true.",
            }

        targets = set(normalized_ids)
        seen: set[str] = set()
        resolved_artifacts: list[dict[str, Any]] = []
        for current_session_id in sessions:
            for turn in self.session_store.list_all_turn_ledger(current_session_id):
                if not isinstance(turn, dict):
                    continue
                metadata = (
                    turn.get("metadata")
                    if isinstance(turn.get("metadata"), dict)
                    else {}
                )
                produced_artifacts = self._normalize_produced_artifact_list(
                    metadata.get("produced_artifacts")
                )
                for artifact in produced_artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    artifact_id = self._safe_text(artifact.get("artifact_id")) or ""
                    if (
                        not artifact_id
                        or artifact_id not in targets
                        or artifact_id in seen
                    ):
                        continue
                    record = self._build_recalled_artifact_record(
                        session_id=current_session_id,
                        turn_entry=turn,
                        artifact=artifact,
                    )
                    if not bool(record.get("downloadable")):
                        continue
                    resolved_artifacts.append(
                        {
                            key: value
                            for key, value in {
                                "artifact_id": record.get("artifact_id"),
                                "task_id": record.get("task_id"),
                                "mime": record.get("mime_type"),
                                "mime_type": record.get("mime_type"),
                                "sha256": record.get("sha256"),
                                "path": record.get("path"),
                                "source_url": record.get("source_url"),
                                "created_by_agent": record.get("created_by_agent"),
                                "created_at": record.get("created_at"),
                                "kind": record.get("kind") or "output",
                                "audience": "deliverable",
                                "filename": record.get("filename"),
                                "session_id": record.get("session_id"),
                                "request_id": record.get("request_id"),
                                "turn_id": record.get("turn_id"),
                                "assistant_message_id": record.get(
                                    "assistant_message_id"
                                ),
                                "route": record.get("route"),
                                "label": record.get("filename"),
                                "summary": record.get("assistant_outcome"),
                            }.items()
                            if value not in (None, "", [], {})
                        }
                    )
                    seen.add(artifact_id)
                if len(seen) >= len(targets):
                    break
            if len(seen) >= len(targets):
                break

        response = {
            "session_id": normalized_session_id,
            "artifact_ids": normalized_ids,
            "all_sessions": all_sessions,
            "artifacts": resolved_artifacts,
            "count": len(resolved_artifacts),
        }
        if not resolved_artifacts:
            response["message"] = "Requested artifact could not be resolved."
        return response

    def list_routing_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.routing_audit_store.list_entries(limit=limit)

    def log_usage_event(self, event: UsageEvent | dict[str, Any]) -> bool:
        payload = self._normalize_usage_event(event)
        inserted = self.usage_store.append(payload)
        metrics_key = "persisted_events" if inserted else "deduplicated_events"
        self._usage_queue_metrics[metrics_key] = (
            self._usage_queue_metrics.get(metrics_key, 0) + 1
        )
        return inserted

    def submit_usage_event(
        self, event: UsageEvent | dict[str, Any]
    ) -> UsageSubmitResult:
        payload = self._normalize_usage_event(event)
        queue = self._usage_event_queue
        worker_active = self._usage_worker is not None and not self._usage_worker.done()
        if queue is not None and worker_active:
            try:
                queue.put_nowait(payload)
                queue_depth = queue.qsize()
                self._usage_queue_metrics["queued_events"] = (
                    self._usage_queue_metrics.get("queued_events", 0) + 1
                )
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

    async def capability_wishlist_get(
        self, capability_id: str
    ) -> dict[str, Any] | None:
        return await self.capability_wishlist_service.get_item(capability_id)

    async def capability_wishlist_search(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        query = self._safe_text(payload.get("query"))
        limit = self._coerce_int(payload.get("limit")) or 3
        return await self.capability_wishlist_service.search(query=query, limit=limit)

    async def capability_wishlist_capture(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
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
            metadata=payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else {},
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
        payload = (
            event if isinstance(event, UsageEvent) else UsageEvent.model_validate(event)
        )
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
            "worker_running": bool(
                self._usage_worker is not None and not self._usage_worker.done()
            ),
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
        metrics = (
            router_response.get("metrics")
            if isinstance(router_response.get("metrics"), dict)
            else {}
        )
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
            provider_request_id=self._safe_text(
                router_response.get("provider_request_id")
            )
            or None,
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
                provider_request_id=self._safe_text(payload.get("provider_request_id"))
                or None,
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

    def _merge_usage_metadata(
        self, primary: dict[str, Any] | None, secondary: Any
    ) -> Any:
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
                    "route": self._normalize_route(
                        self._safe_text(sticky_message.get("route")) or "opus"
                    ),
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
        if self._contains_opus_media_attachments(attachments):
            return RoutingDecision(
                classification={
                    "route": "opus",
                    "needs_latest": False,
                    "needs_citations": False,
                    "is_task": False,
                    "is_continuation": False,
                    "confidence": 1.0,
                    "signals": ["media_attachments"],
                },
                decision_source="media_attachments",
            )
        if (
            (not content or content.startswith("["))
            and isinstance(attachments, list)
            and attachments
        ):
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
        except (
            Exception
        ) as exc:  # pragma: no cover - depends on external service availability
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
            raise RuntimeError(
                "Model router returned an invalid classification payload"
            )

        normalized_classification = {
            "route": self._normalize_route(
                self._safe_text(classification.get("route")) or "opus"
            ),
            "needs_latest": bool(classification.get("needs_latest")),
            "needs_citations": bool(classification.get("needs_citations")),
            "is_task": bool(classification.get("is_task")),
            "is_continuation": bool(classification.get("is_continuation")),
            "confidence": self._coerce_float(classification.get("confidence"), 0.0),
            "signals": classification.get("signals")
            if isinstance(classification.get("signals"), list)
            else [],
        }
        classifier_metrics = router_response.get("metrics")
        classifier_latency_ms = None
        if isinstance(classifier_metrics, dict):
            classifier_latency_ms = self._coerce_float(
                classifier_metrics.get("rtt_ms"), 0.0
            )

        return RoutingDecision(
            classification=normalized_classification,
            decision_source="model_router",
            classifier_payload=router_response,
            classifier_metrics=classifier_metrics
            if isinstance(classifier_metrics, dict)
            else None,
            classifier_model=self._safe_text(router_response.get("classifier_model"))
            or None,
            classifier_latency_ms=classifier_latency_ms,
            raw_classifier_output=self._safe_text(
                router_response.get("raw_classifier_output")
            )
            or None,
        )

    def _resolve_session_id(self, requested_session_id: Any) -> str:
        current_session_id = self._current_session_id()
        requested = self._safe_text(requested_session_id)
        if not requested:
            return current_session_id
        if requested == current_session_id:
            return requested
        if self._is_email_thread_session(requested):
            return requested
        metadata = self.session_store.get_session_metadata(requested)
        if bool(metadata.get("rollover_exempt")):
            return requested
        session_scope = self._safe_text(metadata.get("session_scope"))
        if session_scope and session_scope != "daily":
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

    def _normalize_conversation_context(
        self, conversation_context: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
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
        in_reply_to_request_id: str | None = None,
    ) -> str | None:
        if not content:
            renderable_payload = False
            if isinstance(metadata, dict):
                renderable_payload = any(
                    isinstance(metadata.get(key), list) and bool(metadata.get(key))
                    for key in ("produced_artifacts", "response_blocks")
                )
            if not renderable_payload:
                return None
            content = ""
        if not content and role != "assistant":
            return None
        return self.session_store.append_message(
            session_id,
            role=role,
            content=content,
            route=route,
            awaiting_reply=awaiting_reply,
            channel=channel,
            metadata=metadata,
            in_reply_to_request_id=in_reply_to_request_id,
        )

    def _track_forwarded_foreground_event(self, event: dict[str, Any]) -> None:
        request_id = self._safe_text(event.get("request_id"))
        if not request_id:
            return
        state = self.active_requests.get(request_id)
        if state is None or state.completed:
            return
        task_id = self._safe_text(event.get("task_id"))
        if task_id:
            state.task_id = task_id
            self.active_requests_by_task[task_id] = request_id
        self._track_partial_stream(state, event)

    def _prune_recent_foreground_terminal_streams(self) -> None:
        now = time.monotonic()
        stale_request_ids = [
            request_id
            for request_id, snapshot in self._recent_foreground_terminal_streams.items()
            if now >= float(snapshot.get("expires_at_monotonic") or 0.0)
        ]
        for request_id in stale_request_ids:
            self._recent_foreground_terminal_streams.pop(request_id, None)

    def _cache_recent_foreground_terminal_stream(self, state: ActiveRequest) -> None:
        if not state.foreground or not state.failed:
            return
        visible_content = state.partial_content or state.error_message
        if not (
            visible_content
            or state.partial_thinking
            or state.response_blocks_snapshot
            or state.activity
        ):
            return
        self._recent_foreground_terminal_streams[state.request_id] = {
            "request_id": state.request_id,
            "task_id": state.task_id,
            "session_id": state.session_id,
            "channel": state.channel,
            "route": state.route,
            "content": visible_content,
            "thinking_text": state.partial_thinking,
            "response_blocks": [dict(item) for item in state.response_blocks_snapshot],
            "supporting_artifacts": [dict(item) for item in state.supporting_artifacts],
            "activity": state.activity or "Request failed.",
            "alpha_terminal_log": [dict(item) for item in state.alpha_terminal_log],
            "completed": True,
            "failed": True,
            "error": state.error_message or None,
            "updated_at": utcnow_iso(),
            "expires_at_monotonic": time.monotonic()
            + FAILED_FOREGROUND_STREAM_RETENTION_SEC,
        }

    def _persist_failed_foreground_response(self, state: ActiveRequest) -> bool:
        if not state.foreground or not state.failed:
            return False
        visible_content = state.partial_content or state.error_message
        if not (
            visible_content
            or state.partial_thinking
            or state.response_blocks_snapshot
            or state.supporting_artifacts
        ):
            return False

        metadata: dict[str, Any] = {
            "request_id": state.request_id,
            "failed": True,
        }
        if state.task_id:
            metadata["task_id"] = state.task_id
        if state.partial_thinking:
            metadata["thinking_text"] = state.partial_thinking
        if state.alpha_terminal_log:
            metadata["alpha_terminal_log"] = [dict(item) for item in state.alpha_terminal_log]
        if state.error_message:
            metadata["error"] = state.error_message
        if state.partial_content:
            metadata["partial_response"] = True
        if state.supporting_artifacts:
            metadata["supporting_artifacts"] = list(state.supporting_artifacts)
        if state.response_blocks_snapshot:
            metadata["response_blocks"] = list(state.response_blocks_snapshot)

        return bool(
            self._append_session_message(
                state.session_id,
                role="assistant",
                content=visible_content,
                route=state.route,
                channel=state.channel,
                metadata=metadata,
                in_reply_to_request_id=state.request_id,
            )
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
            "active_workstreams": self._normalize_string_list(
                carry_forward.get("active_workstreams")
            ),
            "recent_decisions": [],
            "open_loops": self._normalize_string_list(carry_forward.get("open_loops")),
            "current_focus_entities": self._normalize_entity_list(
                carry_forward.get("current_focus_entities")
            ),
            "active_task_refs": self._normalize_string_list(
                carry_forward.get("active_task_refs")
            ),
            "pending_artifact_pointers": [],
            "recent_document_artifacts": [],
            "recent_spreadsheet_artifacts": [],
            "recent_research_receipts": [],
            "recent_specialist_receipts": [],
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

    def _render_active_working_set_context(
        self, working_set: dict[str, Any] | None
    ) -> str | None:
        if not isinstance(working_set, dict):
            return None

        lines: list[str] = ["## Active Working Set"]
        goal = self._safe_text(working_set.get("goal"))
        if goal:
            lines.extend(["", f"- Goal: {goal}"])
        active_workstreams = self._normalize_string_list(
            working_set.get("active_workstreams")
        )
        if active_workstreams:
            lines.extend(["", "- Active workstreams:"])
            lines.extend(f"  - {item}" for item in active_workstreams[:6])
        open_loops = self._normalize_string_list(working_set.get("open_loops"))
        if open_loops:
            lines.extend(["", "- Open loops:"])
            lines.extend(f"  - {item}" for item in open_loops[:6])
        recent_decisions = self._normalize_string_list(
            working_set.get("recent_decisions")
        )
        if recent_decisions:
            lines.extend(["", "- Recent decisions:"])
            lines.extend(f"  - {item}" for item in recent_decisions[:6])
        task_refs = self._normalize_string_list(working_set.get("active_task_refs"))
        if task_refs:
            lines.extend(["", f"- Active task refs: {', '.join(task_refs[:6])}"])
        preferences = self._normalize_string_list(
            working_set.get("user_preferences_in_play")
        )
        if preferences:
            lines.extend(["", "- User preferences in play:"])
            lines.extend(f"  - {item}" for item in preferences[:6])
        focus_entities = self._normalize_entity_list(
            working_set.get("current_focus_entities")
        )
        if focus_entities:
            lines.extend(["", "- Current focus entities:"])
            for entity in focus_entities[:6]:
                label = (
                    self._safe_text(entity.get("label"))
                    or self._safe_text(entity.get("id"))
                    or "entity"
                )
                entity_type = self._safe_text(entity.get("type"))
                if entity_type:
                    lines.append(f"  - {entity_type}: {label}")
                else:
                    lines.append(f"  - {label}")
        artifact_refs = self._normalize_string_list(
            working_set.get("pending_artifact_pointers")
        )
        if artifact_refs:
            lines.extend(
                ["", f"- Pending artifact pointers: {', '.join(artifact_refs[:6])}"]
            )
        recent_documents = (
            working_set.get("recent_document_artifacts")
            if isinstance(working_set.get("recent_document_artifacts"), list)
            else []
        )
        if recent_documents:
            lines.extend(["", "- Recent parsed documents:"])
            for document in recent_documents[:6]:
                if not isinstance(document, dict):
                    continue
                label = (
                    self._safe_text(document.get("title"))
                    or self._safe_text(document.get("filename"))
                    or self._safe_text(document.get("artifact_id"))
                    or "document"
                )
                parts = [label]
                doc_id = self._safe_text(document.get("doc_id"))
                bundle_id = self._safe_text(document.get("parse_bundle_id"))
                ingest_state = self._safe_text(document.get("ingest_state"))
                if doc_id:
                    parts.append(f"doc_id={doc_id}")
                if bundle_id:
                    parts.append(f"bundle_id={bundle_id}")
                if ingest_state:
                    parts.append(f"state={ingest_state}")
                lines.append("  - " + "; ".join(parts))
        recent_receipts = (
            working_set.get("recent_tool_receipts")
            if isinstance(working_set.get("recent_tool_receipts"), list)
            else []
        )
        if recent_receipts:
            lines.extend(["", "- Recent tool receipts:"])
            for receipt in recent_receipts[:RECENT_MEMORY_TOOL_RECEIPT_LIMIT]:
                if not isinstance(receipt, dict):
                    continue
                parts = [
                    self._safe_text(receipt.get("operation")) or "tool",
                    self._safe_text(receipt.get("status")) or "status=unknown",
                ]
                canonical_key = self._safe_text(receipt.get("canonical_key"))
                if canonical_key:
                    parts.append(f"key={canonical_key}")
                title = self._safe_text(receipt.get("title"))
                if title:
                    parts.append(title)
                created_at = self._safe_text(receipt.get("created_at"))
                if created_at:
                    parts.append(f"at={created_at}")
                lines.append("  - " + "; ".join(parts))
        recent_research_receipts = (
            working_set.get("recent_research_receipts")
            if isinstance(working_set.get("recent_research_receipts"), list)
            else []
        )
        if recent_research_receipts:
            lines.extend(["", "- Recent research receipts:"])
            for receipt in recent_research_receipts[:RECENT_RESEARCH_RECEIPT_LIMIT]:
                if not isinstance(receipt, dict):
                    continue
                question = self._safe_text(receipt.get("question"))
                route = self._safe_text(receipt.get("route"))
                paths = self._normalize_string_list(receipt.get("paths"), limit=4)
                domains = self._normalize_string_list(
                    receipt.get("source_domains"), limit=3
                )
                source_count = self._coerce_int(receipt.get("source_count"))
                parts: list[str] = []
                if question:
                    parts.append(f'"{self._bounded_excerpt(question, limit=96)}"')
                if route:
                    parts.append(f"via {route}")
                if paths:
                    parts.append(
                        "research="
                        + ", ".join(self._research_path_label(path) for path in paths)
                    )
                if source_count:
                    parts.append(f"sources={source_count}")
                if domains:
                    parts.append(f"domains={', '.join(domains)}")
                if parts:
                    lines.append("  - " + "; ".join(parts))
        recent_specialist_receipts = (
            working_set.get("recent_specialist_receipts")
            if isinstance(working_set.get("recent_specialist_receipts"), list)
            else []
        )
        if recent_specialist_receipts:
            lines.extend(["", "- Recent specialist receipts:"])
            for receipt in recent_specialist_receipts[:RECENT_SPECIALIST_RECEIPT_LIMIT]:
                if not isinstance(receipt, dict):
                    continue
                question = self._safe_text(receipt.get("question"))
                intent_name = self._safe_text(receipt.get("intent"))
                agent_label = self._safe_text(
                    receipt.get("agent_label")
                ) or self._safe_text(receipt.get("agent_id"))
                activity = self._safe_text(receipt.get("activity"))
                domains = self._normalize_string_list(
                    receipt.get("source_domains"), limit=3
                )
                source_count = self._coerce_int(receipt.get("source_count"))
                artifact_count = self._coerce_int(receipt.get("artifact_count"))
                parts: list[str] = []
                if question:
                    parts.append(f'"{self._bounded_excerpt(question, limit=96)}"')
                if intent_name:
                    parts.append(intent_name)
                if agent_label:
                    parts.append(f"via {agent_label}")
                if activity:
                    parts.append(self._bounded_excerpt(activity, limit=120))
                if source_count:
                    parts.append(f"sources={source_count}")
                if domains:
                    parts.append(f"domains={', '.join(domains)}")
                if artifact_count:
                    parts.append(f"artifacts={artifact_count}")
                if parts:
                    lines.append("  - " + "; ".join(parts))
        contested_claims = (
            working_set.get("contested_memory_claims")
            if isinstance(working_set.get("contested_memory_claims"), list)
            else []
        )
        if contested_claims:
            lines.extend(["", "- Current user corrections against memory:"])
            for claim in contested_claims[:6]:
                if not isinstance(claim, dict):
                    continue
                canonical_key = self._safe_text(claim.get("canonical_key"))
                reason = self._safe_text(claim.get("reason"))
                parts = [canonical_key or "memory claim"]
                if reason:
                    parts.append(reason)
                lines.append("  - " + "; ".join(parts))

        return "\n".join(lines) if len(lines) > 1 else None

    async def _assemble_memory_prompt_context(
        self, *, query: str
    ) -> MemoryPromptContext:
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
            logger.exception(
                "gateway.memory_context_failed query=%r", normalized_query[:160]
            )
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
        if (
            not self.config.cosmic_memory_ingest_transcripts
            or not self.memory_client.enabled
        ):
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

        user_metadata = (
            user_message.get("metadata")
            if isinstance(user_message.get("metadata"), dict)
            else {}
        )
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
                            "content": str(
                                user_message.get("content") or "[empty message]"
                            ),
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
                        self._safe_text(channel.split(":", 1)[0] if channel else None)
                        or "unknown_channel",
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
        if (
            not request_id
            or not session_id
            or not task_id
            or not channel
            or not assistant_content
        ):
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

    async def _maybe_schedule_delivered_email_instruction_update(
        self,
        event: dict[str, Any],
        *,
        delivery_status: str,
    ) -> None:
        if delivery_status != "sent":
            return
        if self._safe_text(event.get("type")) != "response.complete":
            return
        channel = self._safe_text(event.get("channel"))
        if self._channel_platform(channel) != "agent-email":
            return
        if not self._email_delivery_counts_as_sent(event):
            return
        if bool(event.get("email_auto_reply_sent")):
            return
        instruction_ids = self._normalize_string_list(
            event.get("matched_instruction_ids"), limit=12
        )
        if not instruction_ids:
            return
        if self._redis is None:
            return
        self._schedule_background_task(
            self._record_email_instruction_delivery(
                event=event, instruction_ids=instruction_ids
            ),
            name="gateway-email-instruction-delivery",
        )

    async def _record_email_instruction_delivery(
        self,
        *,
        event: dict[str, Any],
        instruction_ids: list[str],
    ) -> None:
        session_id = self._safe_text(event.get("session_id"))
        request_id = self._safe_text(event.get("request_id"))
        channel = self._safe_text(event.get("channel"))
        if not session_id or not request_id:
            return
        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient=self.config.email_agent_id,
            intent="email.manage_instruction",
            input={
                "action": "record_delivery",
                "instruction_ids": instruction_ids,
                "mailbox_address": self._safe_text(event.get("mailbox_address"))
                or None,
                "thread_id": self._safe_text(event.get("thread_id")) or None,
                "message_id": self._safe_text(event.get("message_id")) or None,
                "request_id": request_id,
            },
            input_artifacts=[],
            idempotency_key=(
                f"email-instruction-delivery:{request_id}:"
                f"{self._safe_text(event.get('thread_id'))}:{self._safe_text(event.get('message_id'))}:"
                f"{','.join(instruction_ids)}"
            ),
            priority="normal",
            signature="",
            created_at=utcnow(),
            source=self._safe_text(event.get("source")) or "user",
            source_id=request_id,
            channel=channel or None,
        )
        task = task.model_copy(
            update={"signature": sign_task_envelope(task, self.config.signing_secret)}
        )
        try:
            await dispatch_task(task, self._redis)
            result = await self._wait_for_agent_terminal_result(
                task.task_id,
                timeout_sec=20.0,
                poll_interval_sec=1.0,
            )
        except Exception:
            logger.exception(
                "gateway.email_instruction_delivery_callback_failed request_id=%s session_id=%s instruction_ids=%s",
                request_id,
                session_id,
                instruction_ids,
            )
            return
        status = self._safe_text(result.get("status")) or "failed"
        if status != "completed":
            logger.warning(
                "gateway.email_instruction_delivery_callback_non_terminal request_id=%s session_id=%s instruction_ids=%s status=%s error=%s",
                request_id,
                session_id,
                instruction_ids,
                status,
                self._safe_text(result.get("error_message")),
            )

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
        normalized_payload, audit_event = self._normalize_tool_memory_write_payload(
            payload
        )
        return await self._write_memory_record(
            payload=normalized_payload,
            audit_event=audit_event,
            writer_id=audit_event.writer_id,
        )

    async def memory_write_core_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload, audit_event = self._normalize_tool_core_fact_payload(
            payload
        )
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

    async def memory_graph_sync(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.memory_client.graph_sync(payload)

    async def memory_graph_rebuild(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
        metadata = (
            record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        )
        return {
            "session_id": session_id,
            "compacted_summary": record.get("compacted_summary"),
            "active_working_set": metadata.get("active_working_set")
            if isinstance(metadata.get("active_working_set"), dict)
            else None,
            "carry_forward_packet": metadata.get("carry_forward_packet")
            if isinstance(metadata.get("carry_forward_packet"), dict)
            else None,
            "compaction_packet": metadata.get("compaction_packet")
            if isinstance(metadata.get("compaction_packet"), dict)
            else None,
            "metadata": metadata,
        }

    def list_turn_ledger(
        self, session_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
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
            "turn_ledger": self.session_store.list_turn_ledger(
                session_id, limit=turn_limit
            ),
            "raw_history": self.session_store.get_history_tail(
                session_id, limit=raw_history_limit
            ),
        }
        normalized_task_id = self._safe_text(task_id)
        if normalized_task_id:
            payload["task_notebook"] = self.session_store.get_task_notebook(
                normalized_task_id
            )
        normalized_request_id = self._safe_text(request_id)
        if normalized_request_id:
            payload["turn"] = self.session_store.get_turn_ledger_entry(
                normalized_request_id
            )
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
        request_record = (
            self.request_records.get(request_id)
            if isinstance(self.request_records.get(request_id), dict)
            else {}
        )
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
        user_metadata = (
            user_message.get("metadata")
            if isinstance(user_message.get("metadata"), dict)
            else {}
        )
        artifacts = (
            user_metadata.get("input_artifacts")
            if isinstance(user_metadata.get("input_artifacts"), list)
            else []
        )
        artifact_refs = [
            self._safe_text(item.get("artifact_id"))
            or self._safe_text(item.get("path"))
            for item in artifacts
            if isinstance(item, dict)
        ]
        touched_entities = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            label = self._safe_text(artifact.get("filename")) or self._safe_text(
                artifact.get("artifact_id")
            )
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

        route = (
            self._safe_text(event.get("route"))
            or self._safe_text(
                assistant_message.get("route")
                if isinstance(assistant_message, dict)
                else None
            )
            or "opus"
        )
        assistant_excerpt = self._bounded_excerpt(
            assistant_message.get("content")
            if isinstance(assistant_message, dict)
            else event.get("content")
        )
        awaiting_reply = bool(event.get("awaiting_reply"))
        open_loops = [assistant_excerpt] if awaiting_reply and assistant_excerpt else []
        task_id = self._safe_text(event.get("task_id"))
        assistant_metadata = (
            assistant_message.get("metadata")
            if isinstance(assistant_message, dict)
            and isinstance(assistant_message.get("metadata"), dict)
            else {}
        )
        produced_artifacts = self._normalize_produced_artifact_list(
            assistant_metadata.get("produced_artifacts")
        )
        if produced_artifacts:
            for artifact in produced_artifacts:
                if not isinstance(artifact, dict):
                    continue
                artifact_ref = self._safe_text(
                    artifact.get("artifact_id")
                ) or self._safe_text(artifact.get("path"))
                if artifact_ref:
                    artifact_refs.append(artifact_ref)
                label = self._safe_text(artifact.get("filename")) or self._safe_text(
                    artifact.get("artifact_id")
                )
                if label or artifact_ref:
                    touched_entities.append(
                        {
                            "type": "artifact_output",
                            "id": artifact_ref or label,
                            "label": label or artifact_ref,
                        }
                    )
        research_provenance = self._normalize_research_provenance(
            event.get("research_provenance")
            if isinstance(event.get("research_provenance"), dict)
            else assistant_metadata.get("research_provenance"),
            fallback_sources=event.get("sources")
            if isinstance(event.get("sources"), list)
            else assistant_metadata.get("sources"),
        )
        sources = self._normalize_source_list(
            event.get("sources")
            if isinstance(event.get("sources"), list)
            else assistant_metadata.get("sources"),
            limit=8,
        )
        specialist_receipts = self._normalize_specialist_receipts(
            event.get("specialist_receipts")
            if isinstance(event.get("specialist_receipts"), list)
            else assistant_metadata.get("specialist_receipts")
        )
        tool_summary = [route]
        for research_label in self._research_tool_summary_labels(research_provenance):
            if research_label not in tool_summary:
                tool_summary.append(research_label)
        compact_line_parts = [
            self._bounded_excerpt(user_message.get("content"), limit=120)
        ]
        if task_id:
            compact_line_parts.append(f"via {route} task {task_id}")
        else:
            compact_line_parts.append(f"via {route}")
        if assistant_excerpt:
            compact_line_parts.append(
                f"-> {self._bounded_excerpt(assistant_excerpt, limit=120)}"
            )

        started_at = (
            self._safe_text((request_record or {}).get("accepted_at"))
            or self._safe_text(user_message.get("created_at"))
            or utcnow_iso()
        )
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
            "assistant_message_id": self._safe_text(assistant_message.get("message_id"))
            if isinstance(assistant_message, dict)
            else None,
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
                "decision_source": (request_record or {}).get(
                    "routing_decision_source"
                ),
                "input_artifacts": artifacts,
                "produced_artifacts": produced_artifacts,
                "research_provenance": research_provenance,
                "sources": sources,
                "specialist_receipts": specialist_receipts,
                **(
                    {"background": True} if assistant_metadata.get("background") else {}
                ),
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
        notebook.setdefault("activity_log", [])
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
                message = (
                    request_record.get("message")
                    if isinstance(request_record.get("message"), dict)
                    else {}
                )
                request_text = self._bounded_excerpt(message.get("content"))
        if request_text and not self._safe_text(notebook.get("goal")):
            notebook["goal"] = request_text

        state_message = (
            self._safe_text(event.get("message"))
            or self._safe_text(event.get("content"))
            or self._safe_text(event.get("status"))
            or ""
        )
        if event_type == "task.created":
            notebook["status"] = "active"
            notebook["current_state"] = state_message or "Task created"
        elif event_type == "task.suspended":
            notebook["status"] = "waiting_for_input"
            notebook["current_state"] = (
                state_message or "Task suspended waiting for input"
            )
            notebook["open_questions"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("open_questions")),
                    self._safe_text(event.get("question")) or state_message,
                ]
            )
        elif event_type == "task.resumed":
            notebook["status"] = "active"
            notebook["current_state"] = state_message or "Task resumed"
        elif event_type == "task.input_required":
            notebook["status"] = "waiting_for_input"
            notebook["current_state"] = state_message or "Waiting for user input"
            notebook["open_questions"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("open_questions")),
                    state_message,
                ]
            )
        elif event_type == "task.completed":
            notebook["status"] = "completed"
            notebook["current_state"] = state_message or "Task completed"
        elif event_type == "task.failed":
            notebook["status"] = "failed"
            notebook["current_state"] = state_message or "Task failed"
            notebook["failures_to_avoid"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(notebook.get("failures_to_avoid")),
                    state_message,
                ]
            )
        elif event_type == "task.cancelled":
            notebook["status"] = "cancelled"
            notebook["current_state"] = state_message or "Task cancelled"

        activity_entry = self._build_task_activity_entry(event)
        if activity_entry:
            notebook["activity_log"] = self._normalize_activity_log(
                [
                    *self._normalize_activity_log(notebook.get("activity_log")),
                    activity_entry,
                ],
                limit=TASK_ACTIVITY_LOG_LIMIT,
            )

        if turn_entry is not None:
            notebook["artifact_refs"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(
                        notebook.get("artifact_refs"), limit=16
                    ),
                    *self._normalize_string_list(
                        turn_entry.get("artifact_refs"), limit=16
                    ),
                ],
                limit=TASK_ACTIVITY_LOG_LIMIT,
            )
            notebook["files_touched"] = self._normalize_entity_list(
                [
                    *self._normalize_entity_list(
                        notebook.get("files_touched"), limit=16
                    ),
                    *self._normalize_entity_list(
                        turn_entry.get("touched_entities"), limit=16
                    ),
                ],
                limit=16,
            )
            notebook["key_findings"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(
                        notebook.get("key_findings"), limit=12
                    ),
                    *self._normalize_string_list(
                        turn_entry.get("accomplished"), limit=12
                    ),
                ],
                limit=12,
            )
            notebook["compact_history"] = self._normalize_string_list(
                [
                    *self._normalize_string_list(
                        notebook.get("compact_history"), limit=12
                    ),
                    self._safe_text(turn_entry.get("compact_line")),
                ],
                limit=12,
            )
            if turn_entry.get("open_loops"):
                notebook["open_questions"] = self._normalize_string_list(
                    [
                        *self._normalize_string_list(
                            notebook.get("open_questions"), limit=12
                        ),
                        *self._normalize_string_list(
                            turn_entry.get("open_loops"), limit=12
                        ),
                    ],
                    limit=12,
                )

        notebook["updated_at"] = utcnow_iso()
        return notebook

    def _build_task_activity_entry(
        self, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        event_type = self._safe_text(event.get("type")) or ""
        status = self._safe_text(event.get("status"))
        message = self._safe_text(event.get("message"))
        docs_progress = (
            event.get("docs_progress")
            if isinstance(event.get("docs_progress"), dict)
            else None
        )
        tabular_progress = (
            event.get("tabular_progress")
            if isinstance(event.get("tabular_progress"), dict)
            else None
        )
        progress_state = tabular_progress or docs_progress
        label = (
            (
                self._safe_text(progress_state.get("label"))
                if isinstance(progress_state, dict)
                else None
            )
            or message
            or status
        )
        if not label:
            return None
        kind = (
            self._safe_text(progress_state.get("kind"))
            if isinstance(progress_state, dict)
            else None
        ) or (
            "task_lifecycle"
            if event_type in {"task.suspended", "task.resumed", "task.input_required"}
            else "generic"
        )
        stage = (
            self._safe_text(progress_state.get("stage"))
            if isinstance(progress_state, dict)
            else None
        )
        return {
            "id": f"activity_{uuid4().hex}",
            "label": label,
            "detail": message,
            "status": status,
            "stage": stage,
            "kind": kind,
            "created_at": utcnow_iso(),
            **self._extract_activity_specialist_metadata(event),
        }

    def _extract_activity_specialist_metadata(
        self,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        specialist = event.get("specialist") if isinstance(event.get("specialist"), dict) else None
        if not isinstance(specialist, dict):
            return {}
        metadata = {
            "flow_role": "specialist",
            "specialist_task_id": self._safe_text(specialist.get("task_id")),
            "parent_delegated_task_id": self._safe_text(
                specialist.get("attach_to_task_id")
            ),
            "agent_id": self._safe_text(specialist.get("agent_id")),
            "agent_label": self._safe_text(specialist.get("agent_label")),
            "intent": self._safe_text(specialist.get("intent")),
            "specialist_event_type": self._safe_text(specialist.get("event_type")),
        }
        return {key: value for key, value in metadata.items() if value}

    def _normalize_activity_log(
        self,
        entries: Any,
        *,
        limit: int = TASK_ACTIVITY_LOG_LIMIT,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(entries, list):
            return normalized
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            label = self._safe_text(entry.get("label"))
            if not label:
                continue
            item = {
                "id": self._safe_text(entry.get("id")) or f"activity_{uuid4().hex}",
                "label": label,
                "detail": self._safe_text(entry.get("detail")),
                "status": self._safe_text(entry.get("status")),
                "stage": self._safe_text(entry.get("stage")),
                "kind": self._safe_text(entry.get("kind")) or "generic",
                "created_at": self._safe_text(entry.get("created_at")) or utcnow_iso(),
                "flow_role": self._safe_text(entry.get("flow_role")),
                "delegated_task_id": self._safe_text(entry.get("delegated_task_id")),
                "parent_delegated_task_id": self._safe_text(
                    entry.get("parent_delegated_task_id")
                ),
                "specialist_task_id": self._safe_text(entry.get("specialist_task_id")),
                "agent_id": self._safe_text(entry.get("agent_id")),
                "agent_label": self._safe_text(entry.get("agent_label")),
                "intent": self._safe_text(entry.get("intent")),
                "specialist_event_type": self._safe_text(
                    entry.get("specialist_event_type")
                ),
            }
            last = normalized[-1] if normalized else None
            if (
                last
                and last.get("label") == item["label"]
                and (last.get("detail") or "") == (item["detail"] or "")
                and (last.get("status") or "") == (item["status"] or "")
                and (last.get("stage") or "") == (item["stage"] or "")
                and (last.get("kind") or "") == (item["kind"] or "")
                and (last.get("flow_role") or "") == (item.get("flow_role") or "")
                and (last.get("delegated_task_id") or "")
                == (item.get("delegated_task_id") or "")
                and (last.get("parent_delegated_task_id") or "")
                == (item.get("parent_delegated_task_id") or "")
                and (last.get("specialist_task_id") or "")
                == (item.get("specialist_task_id") or "")
            ):
                continue
            normalized.append(item)
        if len(normalized) > limit:
            normalized = normalized[-limit:]
        return normalized

    def _normalize_alpha_terminal_entry(
        self,
        value: Any,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        text = self._safe_text(value.get("text")) or self._safe_text(value.get("message"))
        if not text:
            return None
        stream = (self._safe_text(value.get("stream")) or "stdout").lower()
        if stream not in {"stdout", "stderr", "system"}:
            stream = "stdout"
        entry_task_id = self._safe_text(value.get("task_id")) or self._safe_text(value.get("taskId")) or task_id
        return {
            "id": self._safe_text(value.get("id")) or f"alpha_terminal_{uuid4().hex}",
            "task_id": entry_task_id,
            "stream": stream,
            "event_type": self._safe_text(value.get("event_type"))
            or self._safe_text(value.get("eventType")),
            "text": text[:2000],
            "detail": self._safe_text(value.get("detail")),
            "created_at": self._safe_text(value.get("created_at"))
            or self._safe_text(value.get("createdAt"))
            or utcnow_iso(),
        }

    def _refresh_active_working_set(self, session_id: str) -> dict[str, Any]:
        metadata = self.session_store.get_session_metadata(session_id)
        carry_forward = (
            metadata.get("carry_forward_packet")
            if isinstance(metadata.get("carry_forward_packet"), dict)
            else {}
        )
        recent_turns = self.session_store.list_turn_ledger(
            session_id, limit=TURN_LEDGER_WINDOW_SIZE
        )
        notebooks = self.session_store.list_task_notebooks(
            session_id, limit=TASK_NOTEBOOK_WINDOW_SIZE
        )
        awaiting_reply_messages = self.session_store.list_awaiting_reply_messages(
            session_id, limit=8
        )
        session_artifacts = self.artifact_store.list_for_session(session_id, limit=24)

        active_task_refs = []
        workstreams = self._normalize_string_list(
            carry_forward.get("active_workstreams")
        )
        recent_decisions: list[str] = []
        open_loops = self._normalize_string_list(carry_forward.get("open_loops"))
        entities = self._normalize_entity_list(
            carry_forward.get("current_focus_entities")
        )
        preferences = self._normalize_string_list(
            carry_forward.get("stable_user_preferences")
        )
        artifact_pointers = []
        recent_document_artifacts: list[dict[str, Any]] = []
        recent_tool_receipts = self._recent_memory_tool_receipts(
            session_id, limit=RECENT_MEMORY_TOOL_RECEIPT_LIMIT
        )
        contested_keys, contested_ids, contested_claims = (
            self._active_contested_memory_refs(session_id)
        )
        goal = self._safe_text(carry_forward.get("goal")) or ""
        recent_research_receipts: list[dict[str, Any]] = []
        recent_specialist_receipts: list[dict[str, Any]] = []

        for turn in recent_turns:
            if not goal:
                goal = self._safe_text(turn.get("user_goal")) or ""
            workstreams = self._normalize_string_list(
                [*workstreams, self._safe_text(turn.get("user_goal"))], limit=8
            )
            recent_decisions = self._normalize_string_list(
                [
                    *recent_decisions,
                    *self._normalize_string_list(turn.get("decisions_made"), limit=8),
                ],
                limit=8,
            )
            entities = self._normalize_entity_list(
                [
                    *entities,
                    *self._normalize_entity_list(turn.get("touched_entities"), limit=8),
                ],
                limit=8,
            )
            preferences = self._normalize_string_list(
                [
                    *preferences,
                    *self._normalize_string_list(
                        turn.get("preferences_detected"), limit=8
                    ),
                ],
                limit=8,
            )
            active_task_refs = self._normalize_string_list(
                [
                    *active_task_refs,
                    *self._normalize_string_list(turn.get("task_refs"), limit=8),
                ],
                limit=8,
            )
            artifact_pointers = self._normalize_string_list(
                [
                    *artifact_pointers,
                    *self._normalize_string_list(turn.get("artifact_refs"), limit=8),
                ],
                limit=8,
            )
            research_receipt = self._build_recent_research_receipt(turn)
            if research_receipt:
                recent_research_receipts.append(research_receipt)
                if len(recent_research_receipts) > RECENT_RESEARCH_RECEIPT_LIMIT:
                    recent_research_receipts = recent_research_receipts[
                        -RECENT_RESEARCH_RECEIPT_LIMIT:
                    ]
            specialist_receipts = self._build_recent_specialist_receipts(turn)
            if specialist_receipts:
                recent_specialist_receipts.extend(specialist_receipts)
                if len(recent_specialist_receipts) > RECENT_SPECIALIST_RECEIPT_LIMIT:
                    recent_specialist_receipts = recent_specialist_receipts[
                        -RECENT_SPECIALIST_RECEIPT_LIMIT:
                    ]

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
                    [
                        *open_loops,
                        *self._normalize_string_list(
                            notebook.get("open_questions"), limit=8
                        ),
                    ],
                    limit=8,
                )
                next_actions = self._normalize_string_list(
                    [
                        *next_actions,
                        *self._normalize_string_list(
                            notebook.get("next_best_actions"), limit=8
                        ),
                    ],
                    limit=8,
                )
            entities = self._normalize_entity_list(
                [
                    *entities,
                    *self._normalize_entity_list(
                        notebook.get("files_touched"), limit=8
                    ),
                ],
                limit=8,
            )
            artifact_pointers = self._normalize_string_list(
                [
                    *artifact_pointers,
                    *self._normalize_string_list(
                        notebook.get("artifact_refs"), limit=8
                    ),
                ],
                limit=8,
            )

        latest_artifacts: dict[str, dict[str, Any]] = {}
        for artifact in reversed(session_artifacts):
            if not isinstance(artifact, dict):
                continue
            artifact_id = self._safe_text(artifact.get("artifact_id"))
            if not artifact_id:
                continue
            latest_artifacts[artifact_id] = artifact

        artifact_pointers = []
        parsed_document_rows: list[dict[str, Any]] = []
        parsed_spreadsheet_rows: list[dict[str, Any]] = []
        for artifact in latest_artifacts.values():
            ingest_state = self._safe_text(artifact.get("ingest_state")).lower()
            artifact_id = self._safe_text(artifact.get("artifact_id"))
            pointer = artifact_id or self._safe_text(artifact.get("path"))
            if (
                ingest_state
                in {
                    "staged",
                    "parse_pending",
                    "stage_failed",
                    "metadata_only",
                    "bridge_reference",
                }
                and pointer
            ):
                artifact_pointers = self._normalize_string_list(
                    [*artifact_pointers, pointer], limit=8
                )
            parsed_summary = (
                artifact.get("parsed_summary")
                if isinstance(artifact.get("parsed_summary"), dict)
                else {}
            )
            kind = self._safe_text(artifact.get("kind")).lower()
            if kind == "spreadsheet" and (
                ingest_state == "parsed"
                or self._safe_text(artifact.get("parse_bundle_id"))
                or parsed_summary.get("artifact_id")
            ):
                parsed_spreadsheet_rows.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": self._safe_text(artifact.get("filename")),
                        "ingest_state": self._safe_text(artifact.get("ingest_state")),
                        "parse_bundle_id": self._safe_text(
                            artifact.get("parse_bundle_id")
                        ),
                        "parse_status": self._safe_text(
                            parsed_summary.get("parse_status")
                        ),
                        "request_id": self._safe_text(artifact.get("request_id")),
                    }
                )
            elif kind != "spreadsheet" and (
                ingest_state == "parsed"
                or self._safe_text(artifact.get("parse_bundle_id"))
                or self._safe_text(parsed_summary.get("doc_id"))
            ):
                parsed_document_rows.append(
                    {
                        "artifact_id": artifact_id,
                        "filename": self._safe_text(artifact.get("filename")),
                        "ingest_state": self._safe_text(artifact.get("ingest_state")),
                        "parse_bundle_id": self._safe_text(
                            artifact.get("parse_bundle_id")
                        ),
                        "doc_id": self._safe_text(parsed_summary.get("doc_id")),
                        "title": self._safe_text(parsed_summary.get("title"))
                        or self._safe_text(artifact.get("filename")),
                        "request_id": self._safe_text(artifact.get("request_id")),
                    }
                )

        for receipt in recent_specialist_receipts:
            if not isinstance(receipt, dict):
                continue
            if self._safe_text(receipt.get("intent")) != "tabular.create_workbook":
                continue
            artifact_id = self._safe_text(receipt.get("artifact_id"))
            bundle_id = self._safe_text(receipt.get("bundle_id"))
            if not artifact_id or not bundle_id:
                continue
            if any(
                self._safe_text(item.get("artifact_id")) == artifact_id
                and self._safe_text(item.get("parse_bundle_id")) == bundle_id
                for item in parsed_spreadsheet_rows
            ):
                continue
            parsed_spreadsheet_rows.append(
                {
                    "artifact_id": artifact_id,
                    "filename": self._safe_text(receipt.get("filename")),
                    "ingest_state": "parsed",
                    "parse_bundle_id": bundle_id,
                    "parse_status": self._safe_text(receipt.get("parse_status"))
                    or "completed",
                    "request_id": self._safe_text(receipt.get("request_id")),
                }
            )

        recent_document_artifacts = parsed_document_rows[:6]
        recent_spreadsheet_artifacts = parsed_spreadsheet_rows[:6]

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
            "recent_document_artifacts": recent_document_artifacts,
            "recent_spreadsheet_artifacts": recent_spreadsheet_artifacts,
            "recent_tool_receipts": recent_tool_receipts,
            "recent_research_receipts": recent_research_receipts,
            "recent_specialist_receipts": recent_specialist_receipts,
            "contested_memory_claims": contested_claims,
            "contested_memory_keys": sorted(contested_keys),
            "contested_memory_ids": sorted(contested_ids),
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

    def _recent_memory_tool_receipts(
        self, session_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        entries = self.memory_write_audit_store.list_entries(
            session_id=session_id,
            limit=max(limit, RECENT_MEMORY_TOOL_RECEIPT_SCAN_LIMIT),
        )
        receipts: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            operation = self._safe_text(entry.get("operation"))
            status = self._safe_text(entry.get("status"))
            if operation not in {"memory_write", "memory_write_core_fact"}:
                continue
            if status not in {"saved", "deduplicated", "failed", "rate_limited"}:
                continue
            receipts.append(
                {
                    "operation": operation,
                    "status": status,
                    "canonical_key": self._safe_text(entry.get("canonical_key")),
                    "memory_id": self._safe_text(entry.get("memory_id")),
                    "title": self._safe_text(entry.get("title")),
                    "created_at": self._safe_text(entry.get("created_at")),
                }
            )
            if len(receipts) >= limit:
                break
        return receipts

    def _maybe_record_contested_memory_claims(
        self,
        *,
        record_session_id: str,
        source_session_ids: list[str] | None = None,
        content: str,
    ) -> None:
        normalized_content = str(content or "").strip().lower()
        if not normalized_content:
            return
        if not any(phrase in normalized_content for phrase in MEMORY_CONTEST_PHRASES):
            return

        candidate_session_ids = self._normalize_string_list(
            [record_session_id, *(source_session_ids or [])],
            limit=4,
        )
        recent_core_fact_entries: list[dict[str, Any]] = []
        for candidate_session_id in candidate_session_ids:
            recent_entries = self.memory_write_audit_store.list_entries(
                session_id=candidate_session_id,
                limit=CONTESTED_MEMORY_AUDIT_SCAN_LIMIT,
            )
            for entry in recent_entries:
                if not isinstance(entry, dict):
                    continue
                if self._safe_text(entry.get("operation")) != "memory_write_core_fact":
                    continue
                if self._safe_text(entry.get("status")) not in {
                    "saved",
                    "deduplicated",
                }:
                    continue
                recent_core_fact_entries.append(entry)
                if len(recent_core_fact_entries) >= CONTESTED_MEMORY_RECENT_WRITE_LIMIT:
                    break
            if len(recent_core_fact_entries) >= CONTESTED_MEMORY_RECENT_WRITE_LIMIT:
                break
        if not recent_core_fact_entries:
            return

        metadata = self.session_store.get_session_metadata(record_session_id)
        contested_keys = (
            dict(metadata.get("contested_memory_keys"))
            if isinstance(metadata.get("contested_memory_keys"), dict)
            else {}
        )
        contested_ids = (
            dict(metadata.get("contested_memory_ids"))
            if isinstance(metadata.get("contested_memory_ids"), dict)
            else {}
        )
        contested_at = utcnow_iso()
        reason = self._bounded_excerpt(content, limit=180)

        changed = False
        for entry in recent_core_fact_entries:
            canonical_key = self._safe_text(entry.get("canonical_key"))
            memory_id = self._safe_text(entry.get("memory_id"))
            if canonical_key:
                contested_keys[canonical_key] = {
                    "contested_at": contested_at,
                    "reason": reason,
                }
                changed = True
            if memory_id:
                contested_ids[memory_id] = {
                    "contested_at": contested_at,
                    "reason": reason,
                }
                changed = True
        if not changed:
            return

        self.session_store.update_session_metadata(
            record_session_id,
            {
                "contested_memory_keys": contested_keys,
                "contested_memory_ids": contested_ids,
            },
        )

    def _active_contested_memory_refs(
        self,
        session_id: str,
    ) -> tuple[set[str], set[str], list[dict[str, Any]]]:
        metadata = self.session_store.get_session_metadata(session_id)
        raw_keys = (
            metadata.get("contested_memory_keys")
            if isinstance(metadata.get("contested_memory_keys"), dict)
            else {}
        )
        raw_ids = (
            metadata.get("contested_memory_ids")
            if isinstance(metadata.get("contested_memory_ids"), dict)
            else {}
        )
        if not raw_keys and not raw_ids:
            return set(), set(), []

        recent_entries = self.memory_write_audit_store.list_entries(
            session_id=session_id,
            limit=CONTESTED_MEMORY_AUDIT_SCAN_LIMIT,
        )
        resolved_key_times: dict[str, str] = {}
        resolved_id_times: dict[str, str] = {}
        for entry in recent_entries:
            if not isinstance(entry, dict):
                continue
            if self._safe_text(entry.get("operation")) != "memory_write_core_fact":
                continue
            if self._safe_text(entry.get("status")) not in {"saved", "deduplicated"}:
                continue
            created_at = self._safe_text(entry.get("created_at")) or ""
            canonical_key = self._safe_text(entry.get("canonical_key"))
            memory_id = self._safe_text(entry.get("memory_id"))
            if canonical_key and created_at > (
                resolved_key_times.get(canonical_key) or ""
            ):
                resolved_key_times[canonical_key] = created_at
            if memory_id and created_at > (resolved_id_times.get(memory_id) or ""):
                resolved_id_times[memory_id] = created_at

        contested_keys: set[str] = set()
        contested_ids: set[str] = set()
        contested_claims: list[dict[str, Any]] = []

        for canonical_key, payload in raw_keys.items():
            if not canonical_key or not isinstance(payload, dict):
                continue
            contested_at = self._safe_text(payload.get("contested_at")) or ""
            latest_write = resolved_key_times.get(canonical_key) or ""
            if latest_write and contested_at and latest_write > contested_at:
                continue
            contested_keys.add(canonical_key)
            contested_claims.append(
                {
                    "canonical_key": canonical_key,
                    "reason": self._safe_text(payload.get("reason")),
                    "contested_at": contested_at,
                }
            )

        for memory_id, payload in raw_ids.items():
            if not memory_id or not isinstance(payload, dict):
                continue
            contested_at = self._safe_text(payload.get("contested_at")) or ""
            latest_write = resolved_id_times.get(memory_id) or ""
            if latest_write and contested_at and latest_write > contested_at:
                continue
            contested_ids.add(memory_id)

        return contested_keys, contested_ids, contested_claims

    def _apply_memory_prompt_overrides(
        self,
        *,
        session_id: str,
        memory_prompt_context: MemoryPromptContext,
    ) -> MemoryPromptContext:
        contested_keys, contested_ids, _ = self._active_contested_memory_refs(
            session_id
        )
        if not contested_keys and not contested_ids:
            return memory_prompt_context

        filtered_core_fact_items = [
            item
            for item in memory_prompt_context.core_fact_items
            if self._safe_text(item.get("canonical_key")) not in contested_keys
            and self._safe_text(item.get("memory_id")) not in contested_ids
        ]
        filtered_recall_items = [
            item
            for item in memory_prompt_context.recall_items
            if self._safe_text(item.get("canonical_key")) not in contested_keys
            and self._safe_text(item.get("memory_id")) not in contested_ids
        ]

        if (
            filtered_core_fact_items == memory_prompt_context.core_fact_items
            and filtered_recall_items == memory_prompt_context.recall_items
        ):
            return memory_prompt_context

        filtered_core_facts_rendered = self.memory_client.render_core_fact_block(
            core_fact_items=filtered_core_fact_items,
            core_facts_rendered="",
        )
        rendered = self.memory_client.render_prompt_context(
            core_fact_items=filtered_core_fact_items,
            core_facts_rendered=filtered_core_facts_rendered,
            recall_items=filtered_recall_items,
        )
        return MemoryPromptContext(
            core_fact_items=filtered_core_fact_items,
            core_facts_rendered=filtered_core_facts_rendered,
            recall_items=filtered_recall_items,
            total_token_count=memory_prompt_context.total_token_count,
            rendered=rendered,
            diagnostics=memory_prompt_context.diagnostics,
        )

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
            if isinstance(spec.context_window_tokens, int)
            and spec.context_window_tokens > 0
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
            int(
                self._conversation_context_budget_tokens() * COMPACTION_TRIGGER_FRACTION
            ),
        )

    def _get_model_visible_history(self, session_id: str) -> list[dict[str, Any]]:
        history = self.session_store.get_pruned_history(
            session_id,
            max_messages=None,
            max_chars=None,
            max_approx_tokens=self._conversation_context_budget_tokens(),
        )
        return self._annotate_background_results(history)

    def _annotate_background_results(
        self, history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Prefix background-completed assistant messages with context so the model
        knows which user query they answer."""
        # Build lookup: request_id → user message content excerpt
        user_excerpts: dict[str, str] = {}
        for msg in history:
            if msg.get("role") == "user" and msg.get("request_id"):
                excerpt = (self._safe_text(msg.get("content")) or "")[:120].strip()
                if excerpt:
                    user_excerpts[msg["request_id"]] = excerpt

        annotated: list[dict[str, Any]] = []
        for msg in history:
            metadata = (
                msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            )
            if msg.get("role") == "assistant" and metadata.get("background"):
                reply_to = msg.get("in_reply_to_request_id") or metadata.get(
                    "request_id"
                )
                user_excerpt = (
                    user_excerpts.get(reply_to, "a prior request")
                    if reply_to
                    else "a prior request"
                )
                prefix = f'[Background task result — in reply to: "{user_excerpt}"]\n\n'
                annotated.append(
                    {**msg, "content": prefix + (msg.get("content") or "")}
                )
            else:
                annotated.append(msg)
        return annotated

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
                if not recent_boundary
                or (self._safe_text(item.get("completed_at")) or "") < recent_boundary
            ]
            if not compactable_turns:
                return
            session_state = self.get_session_state(session_id)
            compaction_packet = (
                session_state.get("compaction_packet")
                if isinstance(session_state.get("compaction_packet"), dict)
                else {}
            )
            compacted_until_completed_at = self._safe_text(
                compaction_packet.get("compacted_until_completed_at")
            )
            new_compactable_turns = [
                item
                for item in compactable_turns
                if not compacted_until_completed_at
                or (self._safe_text(item.get("completed_at")) or "")
                > compacted_until_completed_at
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
                    if self._safe_text(item.get("request_id"))
                    in newly_compacted_request_ids
                ]
                if newly_compacted_request_ids
                else older_history
            )
            summary_text = await self._summarize_session_compaction(
                session_id=session_id,
                existing_summary=self._safe_text(
                    session_state.get("compacted_summary")
                ),
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
            if (
                sum(len(line) for line in older_lines)
                >= COMPACTION_RAW_MESSAGE_CHAR_LIMIT
            ):
                break

        turn_lines: list[str] = []
        for item in compactable_turns:
            line = self._safe_text(item.get("compact_line"))
            if not line:
                continue
            turn_meta = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            if turn_meta.get("background"):
                line = f"{line} (background task)"
            turn_lines.append(f"- {line}")
            if len(turn_lines) >= 20:
                break
        active_working_set = session_state.get("active_working_set")
        session_metadata = (
            session_state.get("metadata")
            if isinstance(session_state.get("metadata"), dict)
            else {}
        )
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
        active_working_set = (
            session_state.get("active_working_set")
            if isinstance(session_state.get("active_working_set"), dict)
            else {}
        )
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
                self._safe_text(turn.get("completed_at"))
                or compacted_until_completed_at
            )
            key_facts = self._normalize_string_list(
                [*key_facts, *self._normalize_string_list(turn.get("facts_learned"))],
                limit=10,
            )
            preferences = self._normalize_string_list(
                [
                    *preferences,
                    *self._normalize_string_list(turn.get("preferences_detected")),
                ],
                limit=10,
            )
            decisions = self._normalize_string_list(
                [*decisions, *self._normalize_string_list(turn.get("decisions_made"))],
                limit=10,
            )
            accomplished = self._normalize_string_list(
                [*accomplished, *self._normalize_string_list(turn.get("accomplished"))],
                limit=10,
            )
            touched_entities = self._normalize_entity_list(
                [
                    *touched_entities,
                    *self._normalize_entity_list(
                        turn.get("touched_entities"), limit=12
                    ),
                ],
                limit=12,
            )
            failures = self._normalize_string_list(
                [
                    *failures,
                    *self._normalize_string_list(turn.get("failures_to_avoid")),
                ],
                limit=10,
            )
            open_loops = self._normalize_string_list(
                [*open_loops, *self._normalize_string_list(turn.get("open_loops"))],
                limit=10,
            )
        next_best_actions = []
        if recent_history:
            next_best_actions.append(
                "Resume from the recent uncompressed window before asking the user to repeat context."
            )
        return {
            "session_id": session_id,
            "goal": goal,
            "active_workstreams": self._normalize_string_list(
                active_working_set.get("active_workstreams"), limit=8
            ),
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
        active_working_set = (
            session_state.get("active_working_set")
            if isinstance(session_state.get("active_working_set"), dict)
            else {}
        )
        compaction_packet = (
            session_state.get("compaction_packet")
            if isinstance(session_state.get("compaction_packet"), dict)
            else {}
        )
        open_loops = self._normalize_string_list(
            [
                *self._normalize_string_list(compaction_packet.get("open_loops")),
                *self._normalize_string_list(active_working_set.get("open_loops")),
            ],
            limit=8,
        )
        active_task_refs = self._normalize_string_list(
            active_working_set.get("active_task_refs"), limit=8
        )
        bootstrap_note = (
            self._safe_text(compaction_packet.get("summary_markdown"))
            or self._safe_text(session_state.get("compacted_summary"))
            or ""
        )
        return {
            "goal": self._safe_text(active_working_set.get("goal"))
            or self._safe_text(compaction_packet.get("goal"))
            or "",
            "active_workstreams": self._normalize_string_list(
                [
                    *self._normalize_string_list(
                        compaction_packet.get("active_workstreams")
                    ),
                    *self._normalize_string_list(
                        active_working_set.get("active_workstreams")
                    ),
                ],
                limit=8,
            ),
            "open_loops": open_loops,
            "active_task_refs": active_task_refs,
            "current_focus_entities": self._normalize_entity_list(
                [
                    *self._normalize_entity_list(
                        compaction_packet.get("touched_entities"), limit=8
                    ),
                    *self._normalize_entity_list(
                        active_working_set.get("current_focus_entities"), limit=8
                    ),
                ],
                limit=8,
            ),
            "stable_user_preferences": self._normalize_string_list(
                [
                    *self._normalize_string_list(
                        compaction_packet.get("user_preferences")
                    ),
                    *self._normalize_string_list(
                        active_working_set.get("user_preferences_in_play")
                    ),
                ],
                limit=8,
            ),
            "failures_to_avoid": self._normalize_string_list(
                compaction_packet.get("failures_to_avoid"), limit=8
            ),
            "bootstrap_note": self._bounded_excerpt(bootstrap_note, limit=400),
        }

    def _apply_carry_forward_packet(
        self, current_session_id: str, source_session_id: str, packet: dict[str, Any]
    ) -> None:
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

    async def _maybe_send_whatsapp_activation_greeting(
        self, allowed_phone: str | None = None
    ) -> None:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            return

        try:
            status = await adapter.get_status()  # type: ignore[attr-defined]
            config = await adapter.get_config()  # type: ignore[attr-defined]
        except Exception:
            logger.exception(
                "gateway.channel_activation whatsapp status/config lookup failed"
            )
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
        history = [
            self._hydrate_history_message_for_client(item)
            for item in self.session_store.get_history(session_id)
        ]
        self._prune_recent_foreground_terminal_streams()
        pending_inputs = self._pending_inputs_for_channel(
            channel, session_id=session_id
        )
        active_tasks = await self._active_task_summaries(
            session_id=session_id, channel=channel
        )
        # Still-running background tasks from in-memory state
        background_tasks = [
            {
                "request_id": state.request_id,
                "task_id": state.task_id,
                "session_id": state.session_id,
                "route": state.route,
                "user_query_excerpt": state.user_query_excerpt,
                "partial_content": state.partial_content,
                "partial_thinking": state.partial_thinking,
                "response_blocks": state.response_blocks_snapshot,
                "snapshot_seq": state.snapshot_seq or None,
                "backgrounded_at": state.backgrounded_at,
                "completed": False,
            }
            for state in self.active_requests.values()
            if not state.foreground and state.channel == channel
        ]
        foreground_streams = [
            {
                "request_id": state.request_id,
                "task_id": state.task_id,
                "session_id": state.session_id,
                "channel": state.channel,
                "route": state.route,
                "content": state.partial_content,
                "thinking_text": state.partial_thinking,
                "response_blocks": state.response_blocks_snapshot,
                "snapshot_seq": state.snapshot_seq or None,
                "activity": state.activity or "Working on your request...",
                "activity_log": state.activity_log,
                "alpha_terminal_log": state.alpha_terminal_log,
                "completed": state.completed,
                "failed": state.failed,
                "error": state.error_message or None,
                "updated_at": utcnow_iso(),
            }
            for state in self.active_requests.values()
            if state.foreground
            and state.channel == channel
            and state.session_id == session_id
            and (not state.completed or state.failed)
        ]
        assistant_request_ids_in_history = {
            self._safe_text(item.get("request_id"))
            or self._safe_text(
                (item.get("metadata") or {}).get("request_id")
                if isinstance(item.get("metadata"), dict)
                else None
            )
            for item in history
            if item.get("role") == "assistant"
        }
        for snapshot in self._recent_foreground_terminal_streams.values():
            if (
                self._safe_text(snapshot.get("channel")) != channel
                or self._safe_text(snapshot.get("session_id")) != session_id
            ):
                continue
            request_id_value = self._safe_text(snapshot.get("request_id"))
            if request_id_value and request_id_value in assistant_request_ids_in_history:
                continue
            foreground_streams.append(
                {
                    "request_id": request_id_value,
                    "task_id": self._safe_text(snapshot.get("task_id")) or None,
                    "session_id": session_id,
                    "channel": channel,
                    "route": self._safe_text(snapshot.get("route")) or "opus",
                    "content": self._safe_text(snapshot.get("content")) or "",
                    "thinking_text": self._safe_text(snapshot.get("thinking_text")) or "",
                    "response_blocks": [
                        dict(item)
                        for item in snapshot.get("response_blocks", [])
                        if isinstance(item, dict)
                    ],
                    "snapshot_seq": None,
                    "activity": self._safe_text(snapshot.get("activity"))
                    or "Request failed.",
                    "completed": True,
                    "failed": True,
                    "error": self._safe_text(snapshot.get("error")) or None,
                    "updated_at": self._safe_text(snapshot.get("updated_at"))
                    or utcnow_iso(),
                }
            )
        # Completed background tasks reconstructed from session history
        running_request_ids = {t["request_id"] for t in background_tasks}
        for msg in history:
            if msg.get("role") != "assistant":
                continue
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            if not meta.get("background"):
                continue
            reply_to = msg.get("in_reply_to_request_id") or meta.get("request_id")
            if not reply_to or reply_to in running_request_ids:
                continue
            running_request_ids.add(reply_to)
            # Find the matching user message excerpt
            user_excerpt = ""
            for umsg in history:
                if umsg.get("role") == "user" and umsg.get("request_id") == reply_to:
                    user_excerpt = (self._safe_text(umsg.get("content")) or "")[
                        :120
                    ].strip()
                    break
            background_tasks.append(
                {
                    "request_id": reply_to,
                    "task_id": meta.get("task_id"),
                    "session_id": session_id,
                    "route": msg.get("route"),
                    "user_query_excerpt": user_excerpt,
                    "partial_content": msg.get("content") or "",
                    "partial_thinking": meta.get("thinking_text") or "",
                    "activity_log": meta.get("activity_log"),
                    "alpha_terminal_log": meta.get("alpha_terminal_log"),
                    "sources": meta.get("sources"),
                    "produced_artifacts": self._hydrate_produced_artifact_list_for_client(
                        meta.get("produced_artifacts")
                    ),
                    "backgrounded_at": None,
                    "completed": True,
                }
            )
        return {
            "type": "resume.ok",
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "user_timezone": self.current_user_timezone(),
            "history_tail": history,
            "active_tasks": active_tasks,
            "pending_inputs": pending_inputs,
            "background_tasks": background_tasks,
            "foreground_streams": foreground_streams,
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

    async def _broadcast_cross_channel_to_realtime_clients(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        role: str,
        content: str,
        channel: str,
        route: str | None = None,
        sources: list[dict[str, str]] | None = None,
        thinking_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
        produced_artifacts: list[dict[str, Any]] | None = None,
        supporting_artifacts: list[dict[str, Any]] | None = None,
        activity_log: list[dict[str, Any]] | None = None,
        alpha_terminal_log: list[dict[str, Any]] | None = None,
        response_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Push a cross-channel message to connected desktop/mobile clients on other platforms."""
        if not session_id or not channel:
            return
        origin_platform = self._channel_platform(channel)
        client_artifacts = self._hydrate_artifact_list_for_client(
            produced_artifacts or []
        )
        client_response_blocks = self._build_client_response_blocks(
            content=content,
            produced_artifacts=produced_artifacts,
            supporting_artifacts=supporting_artifacts,
            stored_blocks=response_blocks,
        )

        event: dict[str, Any] = {
            "type": "crosschannel.message",
            "session_id": session_id,
            "role": role,
            "content": content,
            "channel": channel,
            "route": route,
            "timestamp": utcnow_iso(),
        }
        if message_id:
            event["message_id"] = message_id
        if sources:
            event["sources"] = sources
        if thinking_text:
            event["thinking_text"] = thinking_text
        if attachments:
            event["attachments"] = attachments
        if input_artifacts:
            event["input_artifacts"] = input_artifacts
        if client_artifacts:
            event["produced_artifacts"] = client_artifacts
        if activity_log:
            event["activity_log"] = activity_log
        if alpha_terminal_log:
            event["alpha_terminal_log"] = alpha_terminal_log
        if client_response_blocks:
            event["response_blocks"] = client_response_blocks

        for adapter in self.registry.adapters.values():
            if not isinstance(adapter, (DesktopAdapter, MobileAdapter)):
                continue
            if adapter.platform == origin_platform:
                continue
            await adapter.broadcast_to_session(session_id, event)

    def _schedule_mobile_push(
        self,
        *,
        session_id: str | None,
        origin_channel: str | None,
        event_type: str,
        title: str,
        body: str,
        screen: str,
        priority: str = "default",
        data: dict[str, Any] | None = None,
    ) -> None:
        if not self.config.enable_push_notifications:
            return
        if not title or not body:
            return
        dedupe_key = self._push_dedupe_key(
            event_type=event_type,
            session_id=session_id,
            origin_channel=origin_channel,
            data=data or {},
        )
        if self._push_recently_scheduled(dedupe_key):
            return
        self._track_background_task(
            self._send_mobile_push_notifications(
                session_id=session_id,
                origin_channel=origin_channel,
                title=title,
                body=body,
                screen=screen,
                priority=priority,
                data=data or {},
            )
        )

    async def _send_mobile_push_notifications(
        self,
        *,
        session_id: str | None,
        origin_channel: str | None,
        title: str,
        body: str,
        screen: str,
        priority: str,
        data: dict[str, Any],
    ) -> None:
        targets = self.mobile_device_store.list_push_targets(session_id=session_id)
        if not targets and self._channel_platform(origin_channel) != "mobile":
            targets = self.mobile_device_store.list_push_targets(session_id=None)
        if not targets:
            return
        live_mobile_devices = await self._live_mobile_device_ids()
        for target in targets:
            device_id = self._safe_text(target.get("device_id"))
            push_token = self._safe_text(target.get("push_token"))
            fcm_token = self._safe_text(target.get("fcm_token"))
            if not device_id or not (push_token or fcm_token):
                continue
            if self._should_suppress_mobile_push(
                target,
                live_mobile_devices=live_mobile_devices,
                screen=screen,
                priority=priority,
            ):
                continue
            await self.push_dispatcher.send(
                PushNotification(
                    device_id=device_id,
                    token=push_token,
                    fcm_token=fcm_token,
                    platform=self._safe_text(target.get("platform")),
                    title=title,
                    body=body,
                    data={
                        **data,
                        "screen": screen,
                        "session_id": session_id,
                    },
                    priority=priority,
                )
            )

    async def _live_mobile_device_ids(self) -> set[str]:
        adapter = self.registry.adapters.get("mobile")
        if not isinstance(adapter, MobileAdapter):
            return set()
        try:
            connections = await adapter.list_connections()
        except Exception:
            return set()
        return {
            str(item.get("device_id") or "").strip()
            for item in connections
            if str(item.get("device_id") or "").strip()
        }

    def _should_suppress_mobile_push(
        self,
        device: dict[str, Any],
        *,
        live_mobile_devices: set[str],
        screen: str,
        priority: str,
    ) -> bool:
        if priority == "high":
            return False
        device_id = self._safe_text(device.get("device_id"))
        if not device_id or device_id not in live_mobile_devices:
            return False
        if self._safe_text(device.get("presence_state")) != "foreground":
            return False
        if not self._mobile_presence_is_fresh(device):
            return False
        visible_screen = self._safe_text(device.get("visible_screen"))
        return visible_screen == screen

    def _mobile_presence_is_fresh(self, device: dict[str, Any]) -> bool:
        raw = self._safe_text(device.get("last_presence_at"))
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        return age.total_seconds() <= self.config.mobile_presence_stale_sec

    def _push_recently_scheduled(self, dedupe_key: str) -> bool:
        now = time.monotonic()
        stale_before = now - 600.0
        self._recent_push_dedupe = {
            key: value
            for key, value in self._recent_push_dedupe.items()
            if value >= stale_before
        }
        if dedupe_key in self._recent_push_dedupe:
            return True
        self._recent_push_dedupe[dedupe_key] = now
        return False

    def _push_dedupe_key(
        self,
        *,
        event_type: str,
        session_id: str | None,
        origin_channel: str | None,
        data: dict[str, Any],
    ) -> str:
        identifier = (
            self._safe_text(data.get("message_id"))
            or self._safe_text(data.get("task_id"))
            or self._safe_text(data.get("request_id"))
            or self._safe_text(data.get("input_request_id"))
            or self._event_fingerprint(data, origin_channel or "mobile-push")
        )
        return f"{event_type}:{session_id or ''}:{origin_channel or ''}:{identifier}"

    def _channel_display_name(self, channel: str | None) -> str:
        platform = self._channel_platform(channel) or "Cosmic"
        if platform == "desktop":
            return "Desktop"
        if platform == "agent-email":
            return "Email"
        if platform == "mobile":
            return "Mobile"
        return platform.title()

    def _response_push_body(self, content: Any) -> str:
        text = str(content or "").strip()
        if not text:
            return "Cosmic has finished a response."
        return text

    async def _broadcast_cross_channel_to_desktop(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        role: str,
        content: str,
        channel: str,
        route: str | None = None,
        sources: list[dict[str, str]] | None = None,
        thinking_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
        produced_artifacts: list[dict[str, Any]] | None = None,
        supporting_artifacts: list[dict[str, Any]] | None = None,
        activity_log: list[dict[str, Any]] | None = None,
        alpha_terminal_log: list[dict[str, Any]] | None = None,
        response_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Push a cross-channel message to all connected desktop clients for this session.

        Called when a non-desktop channel (WhatsApp, Telegram) produces a user
        message or receives an assistant response, so the desktop UI can
        display the conversation in real-time.
        """
        if not session_id or not channel or channel.startswith("desktop:"):
            return
        await self._broadcast_cross_channel_to_realtime_clients(
            session_id,
            message_id=message_id,
            role=role,
            content=content,
            channel=channel,
            route=route,
            sources=sources,
            thinking_text=thinking_text,
            attachments=attachments,
            input_artifacts=input_artifacts,
            produced_artifacts=produced_artifacts,
            supporting_artifacts=supporting_artifacts,
            activity_log=activity_log,
            alpha_terminal_log=alpha_terminal_log,
            response_blocks=response_blocks,
        )

    def _track_background_task(
        self, coroutine: asyncio.Future[Any] | asyncio.Task[Any] | Any
    ) -> None:
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
        if (
            not normalized_input_request_id
            or not normalized_task_id
            or not normalized_content
            or not normalized_channel
        ):
            raise ValueError(
                "task.input_reply requires input_request_id, task_id, content, and channel"
            )

        resolved = self.session_store.get_task_input_request(
            normalized_input_request_id
        )
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

    async def _handle_task_input_stream_message(
        self, message_id: str, data: dict[str, Any]
    ) -> None:
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
                "options": [
                    str(item)
                    for item in request.get("options", [])
                    if str(item).strip()
                ],
                "status": self._safe_text(request.get("status")) or "pending",
                "timestamp": self._safe_text(request.get("timestamp")) or utcnow_iso(),
            }
            self._persist_task_input_request(event)
            delivery_status = await self._deliver_or_queue_channel_event(
                event, channel=channel
            )
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

    async def _specialist_event_consumer_loop(self) -> None:
        assert self._redis is not None
        consumer_name = "gateway-specialist-{0}".format(id(self))
        while True:
            entries = await self._redis.xreadgroup(
                groupname=self.config.agent_events_gateway_group,
                consumername=consumer_name,
                streams={self.config.agent_events_stream: ">"},
                count=20,
                block=1000,
            )
            for _stream, messages in entries:
                for message_id, data in messages:
                    await self._handle_specialist_event_stream_message(
                        message_id, data
                    )

    async def _handle_specialist_event_stream_message(
        self, message_id: str, data: dict[str, Any]
    ) -> None:
        assert self._redis is not None
        try:
            event = parse_event_envelope(data)
            forwarded = self._build_specialist_flow_event(event)
            if forwarded is None:
                await self._redis.xack(
                    self.config.agent_events_stream,
                    self.config.agent_events_gateway_group,
                    message_id,
                )
                return
            root_task_id = self._safe_text(forwarded.get("task_id"))
            session_id = self._safe_text(forwarded.get("session_id"))
            request_id = self._safe_text(forwarded.get("request_id"))
            if root_task_id and session_id:
                notebook = self._merge_task_notebook(
                    task_id=root_task_id,
                    session_id=session_id,
                    request_id=request_id,
                    event=forwarded,
                )
                self.session_store.upsert_task_notebook(root_task_id, session_id, notebook)
                self._refresh_active_working_set(session_id)
            self._track_forwarded_foreground_event(forwarded)
            await self._deliver_or_queue_channel_event(
                forwarded,
                channel=self._safe_text(forwarded.get("channel")),
            )
            await self._redis.xack(
                self.config.agent_events_stream,
                self.config.agent_events_gateway_group,
                message_id,
            )
        except ChannelUnavailableError:
            return
        except Exception:
            logger.exception(
                "gateway.specialist_event_consumer_failed msg_id=%s", message_id
            )
            await self._redis.xack(
                self.config.agent_events_stream,
                self.config.agent_events_gateway_group,
                message_id,
            )

    def _build_specialist_flow_event(
        self,
        event: Any,
    ) -> dict[str, Any] | None:
        if event is None:
            return None
        agent_id = self._safe_text(getattr(event, "agent_id", None))
        if not agent_id or agent_id == "cosmic/orchestrator:1.0.0":
            return None
        event_type = self._safe_text(getattr(event, "event_type", None)) or ""
        if event_type not in {
            "task.accepted",
            "task.progress",
            "task.suspended",
            "task.resumed",
            "task.completed",
            "task.failed",
            "task.rejected",
            "task.deferred",
        }:
            return None

        context = self._resolve_specialist_request_context(
            self._safe_text(getattr(event, "task_id", None))
        )
        if context is None:
            return None
        payload = getattr(event, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        codex_terminal = (
            dict(payload.get("codex_terminal"))
            if isinstance(payload.get("codex_terminal"), dict)
            else None
        )
        message = (
            ""
            if codex_terminal is not None
            else self._specialist_progress_message(
                event_type=event_type,
                payload=payload,
                agent_label=context["agent_label"],
                intent=context["intent"],
            )
        )
        forwarded = {
            "type": "task.progress",
            "route": "opus",
            "request_id": context["request_id"],
            "session_id": context["session_id"],
            "task_id": context["root_task_id"],
            "channel": context["channel"],
            "status": f"specialist_{event_type.split('.', 1)[-1]}",
            "message": message,
            "specialist": {
                "task_id": context["task_id"],
                "direct_parent_task_id": context["direct_parent_task_id"],
                "root_task_id": context["root_task_id"],
                "attach_to_task_id": context["attach_to_task_id"],
                "agent_id": context["agent_id"],
                "agent_label": context["agent_label"],
                "intent": context["intent"],
                "event_type": event_type,
                "status": self._safe_text(payload.get("status")),
                "message": self._safe_text(payload.get("message")) or message,
                "payload_type": self._safe_text(payload.get("type")),
                "step": payload.get("step"),
                "text": self._safe_text(payload.get("text")),
                "note": self._safe_text(payload.get("note")),
                "completed": payload.get("completed"),
                "total": payload.get("total"),
                "percent": payload.get("percent"),
                "node": self._safe_text(payload.get("node")),
            },
        }
        if codex_terminal is not None:
            forwarded["codex_terminal"] = codex_terminal
            forwarded["message"] = ""
        return forwarded

    def _resolve_specialist_request_context(
        self,
        task_id: str | None,
    ) -> dict[str, str] | None:
        normalized_task_id = self._safe_text(task_id)
        if not normalized_task_id:
            return None
        task_record = self._orchestrator_task_ledger.get_task(normalized_task_id)
        if task_record is None:
            return None
        direct_parent_task_id = self._safe_text(task_record.get("parent_task_id"))
        if not direct_parent_task_id:
            return None

        root_record = task_record
        root_task_id = normalized_task_id
        visited: set[str] = set()
        while True:
            current_task_id = self._safe_text(root_record.get("task_id")) or root_task_id
            if not current_task_id or current_task_id in visited:
                break
            visited.add(current_task_id)
            parent_task_id = self._safe_text(root_record.get("parent_task_id"))
            if not parent_task_id:
                root_task_id = current_task_id
                break
            parent_record = self._orchestrator_task_ledger.get_task(parent_task_id)
            if parent_record is None:
                root_task_id = current_task_id
                break
            root_record = parent_record
            root_task_id = self._safe_text(parent_record.get("task_id")) or parent_task_id

        request_id = (
            self.active_requests_by_task.get(root_task_id)
            or self._safe_text(root_record.get("request_id"))
            or self._safe_text(task_record.get("request_id"))
        )
        if not request_id or request_id not in self.active_requests:
            return None
        active_request = self.active_requests.get(request_id)
        if active_request is None:
            return None

        session_id = (
            self._safe_text(root_record.get("session_id"))
            or self._safe_text(task_record.get("session_id"))
            or active_request.session_id
        )
        channel = (
            self._safe_text(root_record.get("channel"))
            or self._safe_text(task_record.get("channel"))
            or active_request.channel
        )
        if not session_id or not channel:
            return None

        attach_to_task_id = (
            normalized_task_id
            if direct_parent_task_id == root_task_id
            else direct_parent_task_id
        )
        agent_id = (
            self._safe_text(task_record.get("recipient"))
            or self._safe_text(task_record.get("sender"))
            or "specialist"
        )
        intent = self._safe_text(task_record.get("intent")) or "specialist.work"
        return {
            "task_id": normalized_task_id,
            "direct_parent_task_id": direct_parent_task_id,
            "root_task_id": root_task_id,
            "attach_to_task_id": attach_to_task_id or normalized_task_id,
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "agent_id": agent_id,
            "agent_label": self._specialist_agent_label(agent_id),
            "intent": intent,
        }

    def _specialist_agent_label(self, agent_id: str | None) -> str:
        raw = self._safe_text(agent_id) or "specialist"
        normalized = raw
        if "/" in normalized:
            normalized = normalized.split("/", 1)[1]
        if ":" in normalized:
            normalized = normalized.split(":", 1)[0]
        normalized = normalized.replace("-", " ").replace("_", " ").strip()
        return normalized or raw

    def _specialist_progress_message(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        agent_label: str,
        intent: str,
    ) -> str:
        payload_message = self._safe_text(payload.get("message"))
        payload_type = self._safe_text(payload.get("type"))
        intent_label = intent.replace("_", " ")
        prefix = agent_label[0].upper() + agent_label[1:] if agent_label else "Specialist"
        if event_type == "task.progress":
            if payload_type == "agent_plan_created":
                total_steps = payload.get("total_steps")
                try:
                    step_count = max(0, int(total_steps))
                except (TypeError, ValueError):
                    step_count = 0
                if step_count > 0:
                    return f"{prefix} planned {step_count} step{'s' if step_count != 1 else ''}."
            if payload_type == "agent_step_update":
                step = payload.get("step")
                total = payload.get("total")
                step_text = self._safe_text(payload.get("text"))
                note = self._safe_text(payload.get("note"))
                step_status = self._safe_text(payload.get("status")) or "in_progress"
                try:
                    step_label = str(max(1, int(step))) if step is not None else "?"
                except (TypeError, ValueError):
                    step_label = "?"
                try:
                    total_label = str(max(1, int(total))) if total is not None else "?"
                except (TypeError, ValueError):
                    total_label = "?"
                primary = note or step_text or "Working"
                if step_status == "completed":
                    return f"{prefix} step {step_label}/{total_label} completed: {primary}"
                if step_status == "skipped":
                    return f"{prefix} step {step_label}/{total_label} skipped: {primary}"
                return f"{prefix} step {step_label}/{total_label}: {primary}"
            if payload_type == "agent_node_progress":
                node = self._safe_text(payload.get("node"))
                if payload_message:
                    return (
                        payload_message
                        if payload_message.lower().startswith(prefix.lower())
                        else f"{prefix}: {payload_message}"
                    )
                if node:
                    return f"{prefix} is running {node.replace('_', ' ')}."
        if payload_message:
            if payload_message.lower().startswith(prefix.lower()):
                return payload_message
            return f"{prefix}: {payload_message}"
        if event_type == "task.accepted":
            return f"{prefix} accepted {intent_label}."
        if event_type == "task.suspended":
            return f"{prefix} is waiting for more input."
        if event_type == "task.resumed":
            return f"{prefix} resumed {intent_label}."
        if event_type == "task.completed":
            return f"{prefix} completed {intent_label}."
        if event_type == "task.failed":
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            error_message = self._safe_text(error.get("message"))
            return (
                f"{prefix} failed {intent_label}: {error_message}"
                if error_message
                else f"{prefix} failed {intent_label}."
            )
        if event_type == "task.rejected":
            reason = self._safe_text(payload.get("reason"))
            return (
                f"{prefix} rejected {intent_label}: {reason}"
                if reason
                else f"{prefix} rejected {intent_label}."
            )
        if event_type == "task.deferred":
            return f"{prefix} is still running {intent_label}."
        return f"{prefix} is working on {intent_label}."

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
                        request_channel = self._resolve_task_input_channel(
                            request, task_id=task_id
                        )
                        if request_channel != channel:
                            continue
                        await self._handle_task_input_stream_message(message_id, data)
                        delivered_any = True
                    except Exception:
                        continue
            if not delivered_any:
                return

    def _resolve_task_input_channel(
        self, request: dict[str, Any], *, task_id: str
    ) -> str | None:
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
        if (
            not input_request_id
            or not task_id
            or not session_id
            or not channel
            or not question
        ):
            return
        self.session_store.upsert_task_input_request(
            input_request_id=input_request_id,
            task_id=task_id,
            session_id=session_id,
            channel=channel,
            question=question,
            options=event.get("options")
            if isinstance(event.get("options"), list)
            else [],
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

    def _pending_inputs_for_channel(
        self, channel: str, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        pending = self.session_store.list_pending_task_inputs(
            session_id=session_id, channel=channel, limit=50
        )
        persisted = self.delivery_queue_store.list_pending_inputs(channel)
        if not persisted:
            return pending

        seen: set[tuple[str | None, str | None]] = set()
        merged: list[dict[str, Any]] = []
        for item in pending + persisted:
            key = (
                self._safe_text(item.get("task_id")),
                self._safe_text(item.get("input_request_id"))
                or self._safe_text(item.get("request_id")),
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
        if self._is_realtime_client_channel(channel):
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
        await self._maybe_schedule_delivered_memory_ingest(
            payload, delivery_status="sent"
        )
        await self._maybe_schedule_delivered_task_summary_write(
            payload, delivery_status="sent"
        )
        await self._maybe_schedule_delivered_email_instruction_update(
            payload, delivery_status="sent"
        )
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

        # Re-namespace events for backgrounded requests so the desktop
        # routes them to the task panel instead of the main response stream.
        request_id = self._safe_text(event.get("request_id"))
        bg_state = self.active_requests.get(request_id) if request_id else None
        if (
            bg_state is not None
            and not bg_state.foreground
            and not self._safe_text(event.get("type", "")).startswith(
                "task.background."
            )
            and self._safe_text(event.get("type"))
            not in {"task.backgrounded", "task.foregrounded"}
        ):
            event = {**event, "type": f"task.background.{event.get('type', 'unknown')}"}

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

    async def _send_channel_event_now(
        self, event: dict[str, Any], channel: str
    ) -> None:
        adapter = self.registry.get_adapter(channel)
        if adapter is None:
            raise ChannelUnavailableError(
                f"No adapter registered for channel: {channel!r}"
            )
        await adapter.send(event, channel=channel)

    def _delivery_dedupe_key(self, event: dict[str, Any], channel: str) -> str | None:
        event_type = self._safe_text(event.get("type")) or ""
        if event_type in EPHEMERAL_CHANNEL_EVENT_TYPES:
            return None

        request_id = self._safe_text(event.get("request_id"))
        task_id = self._safe_text(event.get("task_id"))
        session_id = self._safe_text(event.get("session_id"))

        if event_type == "response.complete":
            if (
                self._is_realtime_client_channel(channel)
                and (self._safe_text(event.get("source")) or "user") != "cron"
            ):
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
            return (
                f"{channel}:{event_type}:{identifier}"
                if identifier
                else self._event_fingerprint(event, channel)
            )

        if event_type == "task.completed":
            if not self._task_completed_has_visible_output(event):
                return None
            identifier = task_id or request_id or session_id
            return (
                f"{channel}:{event_type}:{identifier}"
                if identifier
                else self._event_fingerprint(event, channel)
            )

        if event_type in {"task.failed", "task.cancelled", "error"}:
            identifier = task_id or request_id or session_id
            return (
                f"{channel}:{event_type}:{identifier}"
                if identifier
                else self._event_fingerprint(event, channel)
            )

        return None

    def _task_completed_has_visible_output(self, event: dict[str, Any]) -> bool:
        result = event.get("result")
        if isinstance(result, dict):
            for key in ("content", "text", "summary", "message"):
                if self._safe_text(result.get(key)):
                    return True
        elif isinstance(result, str) and result.strip():
            return True
        return bool(
            self._safe_text(event.get("content"))
            or self._safe_text(event.get("message"))
        )

    def _event_fingerprint(self, event: dict[str, Any], channel: str) -> str:
        payload = {
            "channel": channel,
            "event": event,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"{channel}:fingerprint:{digest}"

    def _effective_email_delivery(self, event: dict[str, Any]) -> dict[str, Any]:
        delivery = event.get("email_delivery")
        if not isinstance(delivery, dict):
            delivery = {}
        status = self._safe_text(delivery.get("status")) or self._safe_text(
            event.get("email_delivery_status")
        )
        queued_for_approval = bool(
            delivery.get("queued_for_approval")
            if "queued_for_approval" in delivery
            else event.get("email_queued_for_approval")
        )
        approval_id = self._safe_text(delivery.get("approval_id")) or self._safe_text(
            event.get("email_approval_id")
        )
        if not status:
            if queued_for_approval or approval_id:
                status = "queued_for_approval"
            elif (
                self._channel_platform(self._safe_text(event.get("channel")))
                == "agent-email"
            ):
                status = "sent"
        resolved = dict(delivery)
        if status:
            resolved["status"] = status
        if queued_for_approval:
            resolved["queued_for_approval"] = True
        if approval_id:
            resolved["approval_id"] = approval_id
        return resolved

    def _email_delivery_counts_as_sent(self, event: dict[str, Any]) -> bool:
        return (
            self._safe_text(self._effective_email_delivery(event).get("status"))
            == "sent"
        )

    def _email_delivery_counts_as_acted(self, event: dict[str, Any]) -> bool:
        return self._safe_text(self._effective_email_delivery(event).get("status")) in {
            "sent",
            "queued_for_approval",
        }

    def _trace_request_event(
        self,
        *,
        request_id: str | None,
        session_id: str | None,
        channel: str | None,
        route: str | None,
        event_type: str,
        stage: str,
        status: str,
        title: str,
        detail: str | None = None,
        task_id: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        user_query_excerpt: str | None = None,
        final_message: str | None = None,
        specialist_receipts: list[dict[str, Any]] | None = None,
        delivery: dict[str, Any] | None = None,
        completed: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_request_id = self._safe_text(request_id)
        normalized_session_id = self._safe_text(session_id)
        normalized_channel = self._safe_text(channel)
        if (
            not normalized_request_id
            or not normalized_session_id
            or not normalized_channel
        ):
            return
        try:
            self.request_trace_store.record_event(
                request_id=normalized_request_id,
                session_id=normalized_session_id,
                channel=normalized_channel,
                route=self._safe_text(route) or "opus",
                event_type=event_type,
                stage=stage,
                status=status,
                title=title,
                detail=detail,
                task_id=self._safe_text(task_id) or None,
                source=self._safe_text(source) or None,
                source_id=self._safe_text(source_id) or None,
                user_query_excerpt=user_query_excerpt,
                final_message=final_message,
                specialist_receipts=specialist_receipts,
                delivery=delivery,
                completed_at=utcnow_iso() if completed else None,
                metadata=metadata,
            )
        except Exception:
            logger.exception(
                "gateway.request_trace_record_failed request_id=%s event_type=%s stage=%s",
                normalized_request_id,
                event_type,
                stage,
            )

    def _persist_email_delivery_metadata(self, event: dict[str, Any]) -> None:
        if (
            self._channel_platform(self._safe_text(event.get("channel")))
            != "agent-email"
        ):
            return
        message_id = self._safe_text(event.get("message_id"))
        if not message_id:
            return
        delivery = self._effective_email_delivery(event)
        status = self._safe_text(delivery.get("status"))
        if not status:
            return
        self.session_store.merge_message_metadata(
            message_id,
            {
                "email_delivery": delivery,
                "email_delivery_status": status,
                "email_queued_for_approval": bool(delivery.get("queued_for_approval")),
                "email_approval_id": self._safe_text(delivery.get("approval_id"))
                or None,
            },
        )

    def _delivery_available_at(self, attempts: int) -> str:
        backoff = min(
            self.config.delivery_retry_max_sec,
            self.config.delivery_retry_base_sec * (2 ** max(0, attempts - 1)),
        )
        if backoff <= 0:
            return utcnow_iso()
        return (
            (datetime.now(timezone.utc) + timedelta(seconds=backoff))
            .isoformat()
            .replace("+00:00", "Z")
        )

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
        if (
            "agent-email" not in self.registry.adapters
            and self._agent_email_effectively_enabled()
        ):
            channels.append(
                {
                    "platform": "agent-email",
                    "configured": True,
                    "healthy": False,
                    "last_error": self.adapter_errors.get("agent-email"),
                }
            )
        return channels

    async def get_channel_status(self, platform: str) -> dict[str, Any]:
        adapter = self.registry.adapters.get(platform)
        if platform == "agent-email" and adapter is None:
            status = await self.get_agent_email_connection_status()
            return {
                "platform": platform,
                "configured": bool(status.get("configured")),
                "healthy": bool(status.get("healthy")),
                "last_error": status.get("last_error"),
                "mail": status.get("mail"),
                "adapter_registered": status.get("adapter_registered"),
                "connected": status.get("connected"),
                "config_source": status.get("config_source"),
            }
        if adapter is None:
            raise KeyError(platform)

        if platform == "whatsapp":
            try:
                status = await adapter.get_status()  # type: ignore[attr-defined]
                self.adapter_errors.pop(platform, None)
            except Exception as exc:
                self.adapter_errors[platform] = str(exc)
                status = {"status": "error", "error": str(exc)}
            pairing_state = self._safe_text(status.get("pairing_state")) or ""
            connected = bool(status.get("connected"))
            qr_ready = bool(status.get("qr"))
            bridge_healthy = connected or qr_ready or pairing_state in {"connecting"}
            return {
                "platform": platform,
                "configured": True,
                "healthy": platform not in self.adapter_errors and bridge_healthy,
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

        if platform == "agent-email":
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
                "mail": status,
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
            allowed_phone=self._safe_text(response.get("allowed_phone"))
            or allowed_phone
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

    async def clear_telegram_webhook(
        self, *, drop_pending_updates: bool = False
    ) -> dict[str, Any]:
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

    async def download_whatsapp_media(
        self, bridge_media_ref: str
    ) -> tuple[bytes, str | None]:
        adapter = self.registry.adapters.get("whatsapp")
        if adapter is None:
            raise KeyError("whatsapp")
        response = await adapter.download_media(bridge_media_ref)  # type: ignore[attr-defined]
        self.adapter_errors.pop("whatsapp", None)
        return response

    async def get_desktop_registry_agents(self) -> dict[str, Any]:
        """Read-only roster from the orchestrator registry (backed by registry.db)."""
        return await self.orchestrator.list_registry_agents()

    async def get_desktop_system_metrics(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        now = time.monotonic()
        ttl = max(2.0, self.config.desktop_system_metrics_cache_ttl_sec)
        if (
            not force_refresh
            and self._system_metrics_snapshot is not None
            and (now - self._system_metrics_snapshot_at) < ttl
        ):
            return dict(self._system_metrics_snapshot)

        async with self._system_metrics_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._system_metrics_snapshot is not None
                and (now - self._system_metrics_snapshot_at) < ttl
            ):
                return dict(self._system_metrics_snapshot)

            snapshot = await self._build_desktop_system_metrics_snapshot()
            self._system_metrics_snapshot = snapshot
            self._system_metrics_snapshot_at = time.monotonic()
            return dict(snapshot)

    async def _build_desktop_system_metrics_snapshot(self) -> dict[str, Any]:
        usage_summary = self.usage_store.dashboard_summary()
        scheduler_summary = self.scheduler_store.summary()
        delivery_summary = self.delivery_queue_store.summary()
        wishlist_summary = self.capability_wishlist_service.summary()
        channels = self.list_channels()

        orchestrator_result, router_result = await asyncio.gather(
            self._probe_orchestrator_health(),
            self._probe_model_router_health(),
        )

        cpu = self._host_cpu_snapshot()
        memory = self._host_memory_snapshot()
        disk = self._host_disk_snapshot()
        network = self._host_network_snapshot()
        uptime_seconds = self._host_uptime_seconds()

        fetched_at_ms = int(time.time() * 1000)
        budget = {
            "used_usd": float(usage_summary.get("total_cost_usd") or 0.0),
            "currency": "USD",
            "period": usage_summary.get("period_label") or "Rolling 30d",
            "calls": int(usage_summary.get("total_calls") or 0),
            "tokens": int(usage_summary.get("total_tokens") or 0),
            "latest_call_at": usage_summary.get("latest_call_at"),
        }

        services = self._build_desktop_services_snapshot(
            orchestrator_result=orchestrator_result,
            router_result=router_result,
            scheduler_summary=scheduler_summary,
            delivery_summary=delivery_summary,
            channels=channels,
        )

        instance = self._host_instance_snapshot()
        instance.setdefault("type", "VM")
        instance.setdefault("os", f"{platform.system()} {platform.release()}".strip())
        system = {
            "uptime_seconds": uptime_seconds,
            "channel_count": len(channels),
            "channels": channels,
            "scheduler": scheduler_summary,
            "delivery_queue": delivery_summary,
            "capability_wishlist": wishlist_summary,
        }

        return {
            "sourceEndpoint": "/desktop/system-metrics",
            "source": "gateway-desktop-system-metrics",
            "fetched_at": fetched_at_ms,
            "fetchedAt": fetched_at_ms,
            "budget": budget,
            "providers": usage_summary.get("providers") or [],
            "usage_by_feature": usage_summary.get("usage_by_feature") or [],
            "services": services,
            "instance": instance,
            "system": system,
            "cpu": cpu,
            "memory": memory,
            "disk": disk,
            "network": network,
        }

    async def _probe_orchestrator_health(self) -> dict[str, Any]:
        try:
            payload = await self.orchestrator.health(timeout_sec=2.0)
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
            }
        payload = dict(payload)
        payload.setdefault("status", "ok")
        return payload

    async def _probe_model_router_health(self) -> dict[str, Any]:
        try:
            payload = await self.model_router.health(timeout_sec=2.0)
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
            }
        payload = dict(payload)
        payload.setdefault("status", "ok")
        return payload

    def _build_desktop_services_snapshot(
        self,
        *,
        orchestrator_result: dict[str, Any],
        router_result: dict[str, Any],
        scheduler_summary: dict[str, Any],
        delivery_summary: dict[str, Any],
        channels: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        agent_dispatch = orchestrator_result.get("agent_dispatch")
        if not isinstance(agent_dispatch, dict):
            agent_dispatch = {}
        tool_registry = orchestrator_result.get("tool_registry")
        tool_count = len(tool_registry) if isinstance(tool_registry, list) else 0

        healthy_channel_count = sum(1 for item in channels if bool(item.get("healthy")))
        scheduler_live = (
            self._scheduler_worker is not None and not self._scheduler_worker.done()
        )
        delivery_live = (
            self._delivery_worker is not None and not self._delivery_worker.done()
        )
        heartbeat_paused = bool(scheduler_summary.get("heartbeat_paused"))
        pending_delivery = int(delivery_summary.get("pending_count") or 0)
        cron_count = int(scheduler_summary.get("cron_count") or 0)
        memory_status = (
            self._safe_text(self._memory_health_snapshot.get("status")) or "starting"
        )

        return [
            {
                "name": "Gateway",
                "status": "active" if self.started else "down",
                "summary": f"{healthy_channel_count}/{len(channels)} channels healthy",
            },
            {
                "name": "Orchestrator",
                "status": self._probe_service_status(orchestrator_result),
                "summary": self._format_service_summary(
                    primary=int(agent_dispatch.get("healthy_agents") or 0),
                    primary_suffix="healthy",
                    secondary=tool_count,
                    secondary_suffix="tools",
                    fallback="agent runtime",
                ),
            },
            {
                "name": "Model Router",
                "status": self._probe_service_status(router_result),
                "summary": self._safe_text(router_result.get("classifier_model"))
                or "classifier",
            },
            {
                "name": "Memory",
                "status": self._memory_service_status(memory_status),
                "summary": memory_status or "memory store",
            },
            {
                "name": "Scheduler",
                "status": "idle"
                if heartbeat_paused
                else ("active" if scheduler_live else "down"),
                "summary": self._format_service_summary(
                    primary=cron_count,
                    primary_suffix="cron",
                    secondary=int(scheduler_summary.get("paused_cron_count") or 0),
                    secondary_suffix="paused",
                    fallback="scheduler loop",
                ),
            },
            {
                "name": "Delivery Queue",
                "status": "active" if delivery_live else "down",
                "summary": self._format_service_summary(
                    primary=pending_delivery,
                    primary_suffix="pending",
                    secondary=int(delivery_summary.get("deadletter_count") or 0),
                    secondary_suffix="deadletter",
                    fallback="queue worker",
                ),
            },
        ]

    def _probe_service_status(self, payload: dict[str, Any]) -> str:
        status_text = (self._safe_text(payload.get("status")) or "").strip().lower()
        if status_text in {"ok", "healthy", "ready", "active", "running", "up"}:
            return "active"
        if status_text in {"starting", "degraded", "warming"}:
            return "idle"
        if status_text in {"error", "down", "stopped", "failed"}:
            return "down"
        return "idle"

    def _memory_service_status(self, status_text: str) -> str:
        normalized = status_text.strip().lower()
        if normalized in {"ok", "healthy"}:
            return "active"
        if normalized in {"disabled", "starting"}:
            return "idle"
        if normalized == "error":
            return "down"
        return "idle"

    def _format_service_summary(
        self,
        *,
        primary: int | None,
        primary_suffix: str,
        secondary: int | None = None,
        secondary_suffix: str | None = None,
        fallback: str = "—",
    ) -> str:
        parts: list[str] = []
        if primary is not None:
            parts.append(f"{primary} {primary_suffix}")
        if secondary is not None and secondary_suffix:
            parts.append(f"{secondary} {secondary_suffix}")
        return " · ".join(parts) if parts else fallback

    def _host_instance_snapshot(self) -> dict[str, Any]:
        region = (
            os.getenv("AWS_REGION", "").strip()
            or os.getenv("AWS_DEFAULT_REGION", "").strip()
            or "unknown-region"
        )
        provider = (
            "AWS"
            if (os.getenv("AWS_EXECUTION_ENV") or region != "unknown-region")
            else "Self-hosted"
        )
        instance_type = (
            os.getenv("AWS_INSTANCE_TYPE", "").strip()
            or os.getenv("EC2_INSTANCE_TYPE", "").strip()
            or platform.machine()
            or "VM"
        )
        hostname = socket.gethostname() or "unknown-host"
        return {
            "name": hostname,
            "id": hostname,
            "type": instance_type,
            "region": region,
            "provider": provider,
            "os": f"{platform.system()} {platform.release()}".strip(),
        }

    def _host_cpu_snapshot(self) -> dict[str, Any]:
        percent = self._sample_linux_cpu_percent()
        if percent is None:
            percent = self._fallback_cpu_percent()
        return {
            "percent": round(max(0.0, min(100.0, float(percent or 0.0))), 1),
            "cores": os.cpu_count() or 1,
        }

    def _sample_linux_cpu_percent(self) -> float | None:
        sample = self._read_proc_stat_sample()
        if sample is None:
            return None
        previous = self._system_metrics_cpu_sample
        self._system_metrics_cpu_sample = sample
        if previous is None:
            return None
        prev_idle, prev_total = previous
        idle, total = sample
        total_delta = total - prev_total
        idle_delta = idle - prev_idle
        if total_delta <= 0:
            return None
        usage = 100.0 * (1.0 - (idle_delta / total_delta))
        return max(0.0, min(100.0, usage))

    def _read_proc_stat_sample(self) -> tuple[int, int] | None:
        proc_stat = Path("/proc/stat")
        if not proc_stat.exists():
            return None
        try:
            line = proc_stat.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError, UnicodeDecodeError):
            return None
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(value) for value in parts[1:]]
        except ValueError:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total

    def _fallback_cpu_percent(self) -> float | None:
        try:
            load_avg, _, _ = os.getloadavg()
        except (AttributeError, OSError):
            return None
        cores = max(1, os.cpu_count() or 1)
        return max(0.0, min(100.0, (load_avg / cores) * 100.0))

    def _host_memory_snapshot(self) -> dict[str, Any]:
        meminfo = self._read_linux_meminfo()
        total = int(meminfo.get("MemTotal", 0))
        available = int(meminfo.get("MemAvailable", 0))
        if total <= 0:
            return {"total": 0, "used": 0, "available": 0, "percent": 0.0}
        if available <= 0:
            available = int(meminfo.get("MemFree", 0))
        used = max(0, total - available)
        percent = (used / total) * 100.0 if total > 0 else 0.0
        return {
            "total": total,
            "used": used,
            "available": available,
            "percent": round(max(0.0, min(100.0, percent)), 1),
        }

    def _read_linux_meminfo(self) -> dict[str, int]:
        meminfo_path = Path("/proc/meminfo")
        if not meminfo_path.exists():
            return {}
        payload: dict[str, int] = {}
        try:
            lines = meminfo_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return {}
        for line in lines:
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            number_text = raw_value.strip().split(" ", 1)[0]
            try:
                payload[key.strip()] = int(number_text) * 1024
            except ValueError:
                continue
        return payload

    def _host_disk_snapshot(self) -> dict[str, Any]:
        root_path = self._system_root_path()
        try:
            usage = shutil.disk_usage(root_path)
        except OSError:
            return {
                "path": str(root_path),
                "total": 0,
                "used": 0,
                "free": 0,
                "percent": 0.0,
            }
        used = max(0, usage.total - usage.free)
        percent = (used / usage.total) * 100.0 if usage.total > 0 else 0.0
        return {
            "path": str(root_path),
            "total": int(usage.total),
            "used": int(used),
            "free": int(usage.free),
            "percent": round(max(0.0, min(100.0, percent)), 1),
        }

    def _system_root_path(self) -> Path:
        if os.name == "nt":
            drive = Path.cwd().anchor or "C:\\"
            return Path(drive)
        return Path("/")

    def _host_network_snapshot(self) -> dict[str, Any]:
        sample = self._read_proc_network_sample()
        if sample is None:
            return {"rx_mbps": None, "tx_mbps": None, "throughput_mbps": None}
        rx_total, tx_total = sample
        now = time.monotonic()
        previous = self._system_metrics_network_sample
        self._system_metrics_network_sample = (rx_total, tx_total, now)
        if previous is None:
            return {"rx_mbps": None, "tx_mbps": None, "throughput_mbps": None}
        prev_rx, prev_tx, prev_ts = previous
        elapsed = max(0.001, now - prev_ts)
        rx_delta = max(0, rx_total - prev_rx)
        tx_delta = max(0, tx_total - prev_tx)
        rx_mbps = (rx_delta * 8.0) / (1_000_000.0 * elapsed)
        tx_mbps = (tx_delta * 8.0) / (1_000_000.0 * elapsed)
        return {
            "rx_mbps": round(rx_mbps, 2),
            "tx_mbps": round(tx_mbps, 2),
            "throughput_mbps": round(rx_mbps + tx_mbps, 2),
        }

    def _read_proc_network_sample(self) -> tuple[int, int] | None:
        netdev_path = Path("/proc/net/dev")
        if not netdev_path.exists():
            return None
        try:
            lines = netdev_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        rx_total = 0
        tx_total = 0
        for line in lines[2:]:
            if ":" not in line:
                continue
            interface, raw_metrics = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            fields = raw_metrics.split()
            if len(fields) < 9:
                continue
            try:
                rx_total += int(fields[0])
                tx_total += int(fields[8])
            except ValueError:
                continue
        return rx_total, tx_total

    def _host_uptime_seconds(self) -> float | None:
        uptime_path = Path("/proc/uptime")
        if uptime_path.exists():
            try:
                raw_value = uptime_path.read_text(encoding="utf-8").split(" ", 1)[0]
                return float(raw_value)
            except (OSError, UnicodeDecodeError, ValueError):
                return None
        return None

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
            "status": "ready"
            if ready
            else ("starting" if not self.started else "degraded"),
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

    async def _active_task_summaries(
        self, *, session_id: str, channel: str
    ) -> list[dict[str, Any]]:
        try:
            tasks = await self.orchestrator.list_active_tasks(
                session_id=session_id, channel=channel
            )
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
            raise RuntimeError(
                "GATEWAY_SIGNING_SECRET is not configured on the Gateway VM."
            )

        message = request_record.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(
                "Request record is missing the normalized message payload."
            )
        orchestrator_query = (
            self._safe_text(request_record.get("orchestrator_query_override"))
            or self._safe_text(message.get("content"))
            or "[empty message]"
        )

        prepared_input_artifacts = self._prepare_input_artifacts_for_model(
            request_record.get("input_artifacts")
            if isinstance(request_record.get("input_artifacts"), list)
            else []
        )

        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient="cosmic/orchestrator:1.0.0",
            intent="orchestrator.process",
            input={
                "query": orchestrator_query,
                "request_id": request_id,
                "conversation_context": request_record.get(
                    "assembled_conversation_context"
                )
                or [],
                "memory_context": self._safe_text(request_record.get("memory_context")),
                "visual_response_enhancement_enabled": bool(
                    request_record.get("visual_response_enhancement_enabled", True)
                ),
                "user_timezone": self._safe_text(request_record.get("cron_timezone"))
                or self.current_user_timezone(),
            },
            input_artifacts=prepared_input_artifacts,
            idempotency_key=self._safe_text(request_record.get("idempotency_key"))
            or uuid4().hex,
            priority=SOURCE_PRIORITY_MAP.get(
                self._safe_text(request_record.get("source")) or "user", "normal"
            ),
            signature="",
            created_at=utcnow(),
            source=self._safe_text(request_record.get("source")) or "user",
            source_id=self._safe_text(request_record.get("source_id")),
            channel=channel,
        )
        signature = sign_task_envelope(task, self.config.signing_secret)
        return task.model_copy(update={"signature": signature})

    def _prepare_input_artifacts_for_model(
        self, input_artifacts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for artifact in input_artifacts:
            if not isinstance(artifact, dict):
                continue
            enriched = dict(artifact)
            if is_supported_image_artifact(enriched) and self._safe_text(
                enriched.get("path")
            ):
                image_url = self.mint_artifact_access_url(
                    enriched, purpose="llm_image_fetch"
                )
                if image_url:
                    enriched["provider_url"] = image_url
                    enriched["provider_access"] = "signed_url"
            prepared.append(enriched)
        return prepared

    async def _persist_inbound_artifacts(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._channel_platform(channel) == "agent-email":
            return []
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return []
        try:
            manifests = self.artifact_store.persist_inbound_attachments(
                request_id=request_id,
                session_id=session_id,
                source_channel=channel,
                source_platform=self._safe_text(metadata.get("platform")),
                source_message_id=self._safe_text(metadata.get("message_id")),
                attachments=attachments,
            )
            return await self._stage_supported_input_artifacts(
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                manifests=manifests,
            )
        except Exception:
            logger.exception(
                "gateway.artifact_persist_failed request_id=%s channel=%s attachment_count=%s",
                request_id,
                channel,
                len(attachments),
            )
            return []

    async def _ensure_request_email_processed(
        self, request_record: dict[str, Any]
    ) -> None:
        if not self._should_preprocess_email_inbound(request_record):
            return
        if request_record.get("email_process_inbound_state") in {"completed", "failed"}:
            return
        request_id = self._safe_text(request_record.get("request_id"))
        session_id = self._safe_text(request_record.get("session_id"))
        channel = self._safe_text(request_record.get("channel"))
        route = self._safe_text(request_record.get("route")) or "opus"
        self._trace_request_event(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route=route,
            event_type="email.process_inbound.started",
            stage="email_preprocess",
            status="active",
            title="Email preprocess started",
            task_id=self._safe_text(request_record.get("email_process_inbound_task_id"))
            or None,
        )

        try:
            process_result = await self._dispatch_email_process_inbound(
                request_record=request_record
            )
        except Exception:
            logger.exception(
                "gateway.email_process_inbound_failed request_id=%s",
                self._safe_text(request_record.get("request_id")),
            )
            request_record["email_process_inbound_state"] = "failed"
            self._trace_request_event(
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                route=route,
                event_type="email.process_inbound.failed",
                stage="email_preprocess",
                status="failed",
                title="Email preprocess failed",
                detail="Gateway could not dispatch or reconcile email.process_inbound.",
            )
            return

        status = self._safe_text(process_result.get("status")) or "failed"
        if status != "completed":
            logger.warning(
                "gateway.email_process_inbound_non_terminal request_id=%s status=%s error=%s",
                self._safe_text(request_record.get("request_id")),
                status,
                self._safe_text(process_result.get("error_message")),
            )
            request_record["email_process_inbound_state"] = "failed"
            self._trace_request_event(
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                route=route,
                event_type="email.process_inbound.failed",
                stage="email_preprocess",
                status="failed",
                title="Email preprocess failed",
                detail=self._safe_text(process_result.get("error_message"))
                or f"email.process_inbound returned {status}.",
                task_id=self._safe_text(process_result.get("task_id")) or None,
            )
            return

        output = (
            process_result.get("output")
            if isinstance(process_result.get("output"), dict)
            else {}
        )
        message = request_record.get("message")
        if not isinstance(message, dict):
            request_record["email_process_inbound_state"] = "failed"
            self._trace_request_event(
                request_id=request_id,
                session_id=session_id,
                channel=channel,
                route=route,
                event_type="email.process_inbound.failed",
                stage="email_preprocess",
                status="failed",
                title="Email preprocess failed",
                detail="Inbound email message payload was missing after preprocess completion.",
            )
            return
        original_content = (
            self._safe_text(message.get("content")) or "[empty inbound email]"
        )
        request_record["orchestrator_query_override"] = (
            self._build_email_inbound_orchestrator_query(
                original_content=original_content,
                process_output=output,
            )
        )
        request_record["email_process_inbound_state"] = "completed"
        request_record["email_process_inbound_task_id"] = self._safe_text(
            process_result.get("task_id")
        )
        request_record["email_process_inbound_output"] = output
        request_record["email_process_inbound_artifacts"] = (
            process_result.get("artifacts")
            if isinstance(process_result.get("artifacts"), list)
            else []
        )
        self._trace_request_event(
            request_id=request_id,
            session_id=session_id,
            channel=channel,
            route=route,
            event_type="email.process_inbound.completed",
            stage="email_preprocess",
            status="completed",
            title="Email preprocess completed",
            detail=self._safe_text(output.get("summary"))
            or self._safe_text(output.get("response"))
            or None,
            task_id=self._safe_text(process_result.get("task_id")) or None,
            metadata={
                "trusted_sender": bool(output.get("trusted_sender")),
                "sender_role": self._safe_text(output.get("sender_role")) or None,
                "matched_instruction_ids": self._normalize_string_list(
                    [
                        self._safe_text(item.get("instruction_id"))
                        for item in output.get("matched_instructions", [])
                        if isinstance(item, dict)
                    ],
                    limit=12,
                ),
            },
        )

    async def _dispatch_email_process_inbound(
        self,
        *,
        request_record: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = self._safe_text(request_record.get("session_id"))
        request_id = self._safe_text(request_record.get("request_id"))
        channel = self._safe_text(request_record.get("channel"))
        source = self._safe_text(request_record.get("source")) or "user"
        source_id = self._safe_text(request_record.get("source_id"))
        message = request_record.get("message")
        metadata = (
            message.get("metadata")
            if isinstance(message, dict) and isinstance(message.get("metadata"), dict)
            else {}
        )
        thread_id = self._safe_text(metadata.get("thread_id"))
        message_id = self._safe_text(metadata.get("message_id"))
        if not session_id or not request_id or not thread_id or not message_id:
            return {
                "status": "failed",
                "error_message": "Missing session_id, request_id, thread_id, or message_id for email inbound processing.",
            }

        child_input = {
            "thread_id": thread_id,
            "message_id": message_id,
            "mailbox_address": self._safe_text(metadata.get("mailbox_address")),
            "mailbox_id": self._safe_text(metadata.get("mailbox_id")),
            "request_id": request_id,
        }
        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient=self.config.email_agent_id,
            intent="email.process_inbound",
            input=child_input,
            input_artifacts=[],
            idempotency_key=f"email-process-inbound:{request_id}:{thread_id}:{message_id}",
            priority=SOURCE_PRIORITY_MAP.get(source, "normal"),
            signature="",
            created_at=utcnow(),
            source=source,
            source_id=source_id or request_id,
            channel=channel or None,
        )
        task = task.model_copy(
            update={"signature": sign_task_envelope(task, self.config.signing_secret)}
        )
        await dispatch_task(task, self._redis)
        return await self._wait_for_agent_terminal_result(
            task.task_id,
            timeout_sec=self.config.email_process_inbound_timeout_sec,
            poll_interval_sec=self.config.email_process_inbound_poll_interval_sec,
        )

    async def stage_desktop_uploads(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        uploads: list[dict[str, Any]],
        source_platform: str = "desktop",
    ) -> list[dict[str, Any]]:
        staged_manifests: list[dict[str, Any]] = []
        if not request_id or not session_id or not channel:
            return staged_manifests

        for index, upload in enumerate(uploads, start=1):
            if not isinstance(upload, dict):
                continue
            filename = Path(
                self._safe_text(upload.get("filename")) or "attachment"
            ).name
            content = upload.get("content")
            if not filename or not isinstance(content, bytes) or not content:
                continue

            artifact_id = (
                self._safe_text(upload.get("artifact_id")) or f"art_{uuid4().hex}"
            )
            supplied_mime = self._safe_text(upload.get("mime_type"))
            effective_kind = self._supported_artifact_kind(
                {
                    "kind": self._safe_text(upload.get("kind")),
                    "mime": supplied_mime,
                    "filename": filename,
                }
            )
            if effective_kind is None:
                continue
            effective_mime = supplied_mime
            if not effective_mime:
                if effective_kind == "document":
                    effective_mime = infer_document_mime_from_extension(
                        Path(filename).suffix
                    )
                elif effective_kind == "spreadsheet":
                    effective_mime = infer_tabular_mime_from_extension(
                        Path(filename).suffix
                    )
                else:
                    effective_mime = infer_image_mime_from_extension(
                        Path(filename).suffix
                    )
            width, height = (
                self._extract_image_dimensions(
                    content=content,
                    media_type=effective_mime,
                    filename=filename,
                )
                if effective_kind == "image"
                else (None, None)
            )
            original_root = (
                self.config.artifacts_root
                / f"req_ingest_{request_id}"
                / "inputs"
                / artifact_id
                / "original"
            )
            original_root.mkdir(parents=True, exist_ok=True)
            source_path = original_root / filename
            source_path.write_bytes(content)

            sha256 = hashlib.sha256(content).hexdigest()
            logical_path = self._logical_artifact_path(source_path)
            staged_manifest = {
                "artifact_id": artifact_id,
                "source_channel": channel,
                "source_platform": source_platform
                or self._channel_platform(channel)
                or "desktop",
                "source_message_id": None,
                "kind": effective_kind,
                "mime": effective_mime,
                "mime_type": effective_mime,
                "filename": filename,
                "caption": None,
                "size_bytes": len(content),
                "width": width,
                "height": height,
                "duration_ms": None,
                "sha256": sha256,
                "bridge_media_ref": None,
                "download_url": None,
                "ingest_state": "staged",
                "path": logical_path,
                "parse_task_id": None,
                "parse_bundle_id": None,
                "parsed_summary": None,
                "task_id": None,
                "index": index,
                "metadata": None,
            }
            staged_manifest_path = source_path.parent.parent / "manifest.json"
            staged_manifest_path.write_text(
                json.dumps(staged_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            staged_manifests.append(staged_manifest)

        return staged_manifests

    async def _stage_supported_input_artifacts(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        manifests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        staged_manifests: list[dict[str, Any]] = []
        for manifest in manifests:
            if not isinstance(manifest, dict):
                continue
            if self._supported_artifact_kind(manifest) is None:
                staged_manifests.append(manifest)
                continue
            if self._safe_text(manifest.get("path")) and self._safe_text(
                manifest.get("ingest_state")
            ) in {"staged", "parsed"}:
                staged_manifests.append(manifest)
                continue
            try:
                staged_manifests.append(
                    await self._stage_single_supported_artifact(
                        request_id=request_id,
                        session_id=session_id,
                        channel=channel,
                        manifest=manifest,
                    )
                )
            except Exception:
                artifact_id = self._safe_text(manifest.get("artifact_id"))
                logger.exception(
                    "gateway.artifact_stage_failed request_id=%s artifact_id=%s channel=%s",
                    request_id,
                    artifact_id,
                    channel,
                )
                manifest = dict(manifest)
                manifest["ingest_state"] = "stage_failed"
                staged_manifests.append(manifest)
        return staged_manifests

    async def _stage_single_supported_artifact(
        self,
        *,
        request_id: str,
        session_id: str,
        channel: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        artifact_id = self._safe_text(manifest.get("artifact_id"))
        if not artifact_id:
            return manifest

        content, media_type = await self._download_artifact_bytes(manifest)
        if not content:
            manifest = dict(manifest)
            manifest["ingest_state"] = "metadata_only"
            return manifest

        filename = self._safe_text(
            manifest.get("filename")
        ) or self._default_artifact_filename(
            artifact_id=artifact_id,
            media_type=media_type or self._safe_text(manifest.get("mime")),
        )
        original_root = (
            self.config.artifacts_root
            / f"req_ingest_{request_id}"
            / "inputs"
            / artifact_id
            / "original"
        )
        original_root.mkdir(parents=True, exist_ok=True)
        source_path = original_root / filename
        source_path.write_bytes(content)

        sha256 = hashlib.sha256(content).hexdigest()
        logical_path = self._logical_artifact_path(source_path)
        effective_mime = media_type or self._safe_text(manifest.get("mime"))
        if not effective_mime:
            kind = self._supported_artifact_kind(manifest)
            if kind == "image":
                effective_mime = infer_image_mime_from_extension(source_path.suffix)
            elif kind == "spreadsheet":
                effective_mime = infer_tabular_mime_from_extension(source_path.suffix)
            else:
                effective_mime = infer_document_mime_from_extension(source_path.suffix)
        width, height = (
            self._extract_image_dimensions(
                content=content,
                media_type=effective_mime,
                filename=filename,
            )
            if (self._supported_artifact_kind(manifest) == "image")
            else (None, None)
        )
        metadata = dict(manifest.get("metadata") or {})
        staged = {
            **manifest,
            "session_id": session_id,
            "source_channel": channel,
            "kind": self._supported_artifact_kind(manifest)
            or self._safe_text(manifest.get("kind"))
            or "unknown",
            "mime": effective_mime,
            "filename": filename,
            "sha256": sha256,
            "path": logical_path,
            "size_bytes": len(content),
            "width": width if width is not None else manifest.get("width"),
            "height": height if height is not None else manifest.get("height"),
            "ingest_state": "staged",
            "metadata": metadata or None,
        }
        staged_manifest_path = source_path.parent.parent / "manifest.json"
        staged_manifest_path.write_text(
            json.dumps(staged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.artifact_store.update_ingest_state(
            artifact_id,
            sha256=sha256,
            path=logical_path,
            ingest_state="staged",
        )
        return staged

    async def _download_artifact_bytes(
        self, manifest: dict[str, Any]
    ) -> tuple[bytes | None, str | None]:
        download_url = self._safe_text(manifest.get("download_url"))
        bridge_media_ref = self._safe_text(manifest.get("bridge_media_ref"))
        source_platform = (
            self._safe_text(manifest.get("source_platform")) or ""
        ).lower()
        source_channel = (self._safe_text(manifest.get("source_channel")) or "").lower()
        metadata = (
            manifest.get("metadata")
            if isinstance(manifest.get("metadata"), dict)
            else {}
        )

        telegram_file_id = self._safe_text(metadata.get("telegram_file_id"))
        if not telegram_file_id:
            if bridge_media_ref.startswith("telegram:file:"):
                telegram_file_id = bridge_media_ref.split("telegram:file:", 1)[1]
        if (
            not telegram_file_id
            and download_url
            and download_url.startswith("/internal/channels/telegram/media/")
        ):
            telegram_file_id = download_url.rsplit("/", 1)[-1]
        if telegram_file_id:
            return await self.download_telegram_media(telegram_file_id)

        if bridge_media_ref and (
            source_platform == "whatsapp" or source_channel.startswith("whatsapp:")
        ):
            return await self.download_whatsapp_media(bridge_media_ref)

        if not download_url:
            return None, None

        response = await self._artifact_client.get(download_url)
        response.raise_for_status()
        raw_media_type = self._safe_text(response.headers.get("content-type")) or ""
        media_type = raw_media_type.split(";", 1)[0].strip() or None
        return response.content, media_type

    def _supported_artifact_kind(self, artifact: dict[str, Any] | None) -> str | None:
        if not isinstance(artifact, dict):
            return None
        if is_supported_document_artifact(artifact):
            return "document"
        if is_supported_tabular_artifact(artifact):
            return "spreadsheet"
        if is_supported_image_artifact(artifact):
            return "image"
        return None

    def _default_artifact_filename(
        self, *, artifact_id: str, media_type: str | None
    ) -> str:
        mime = (self._safe_text(media_type) or "").lower()
        extension_map = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel.sheet.binary.macroEnabled.12": ".xlsb",
            "text/csv": ".csv",
            "text/tab-separated-values": ".tsv",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        extension = extension_map.get(mime, "")
        safe_artifact_id = (
            re.sub(r"[^A-Za-z0-9._-]+", "_", artifact_id).strip("._") or "document"
        )
        return f"{safe_artifact_id}{extension}"

    def _extract_image_dimensions(
        self,
        *,
        content: bytes,
        media_type: str | None,
        filename: str | None,
    ) -> tuple[int | None, int | None]:
        if Image is None:
            return None, None
        if not is_supported_image_artifact(
            {
                "mime": self._safe_text(media_type),
                "filename": self._safe_text(filename),
            }
        ):
            return None, None
        try:
            from io import BytesIO

            with Image.open(BytesIO(content)) as image:
                normalized = (
                    ImageOps.exif_transpose(image) if ImageOps is not None else image
                )
                width, height = normalized.size
                return int(width), int(height)
        except Exception:
            return None, None

    def _llm_image_target_size(self, width: int, height: int) -> tuple[int, int]:
        max_edge = max(512, int(self.config.llm_image_max_edge_px))
        max_pixels = max(262_144, int(self.config.llm_image_max_pixels))
        largest_dimension = max(width, height)
        if largest_dimension <= 0:
            return width, height
        scale = min(1.0, max_edge / float(largest_dimension))
        scaled_pixels = (width * height) * (scale**2)
        if scaled_pixels > max_pixels:
            scale = min(scale, math.sqrt(max_pixels / float(width * height)))
        if scale >= 0.999:
            return width, height
        target_width = max(1, int(width * scale))
        target_height = max(1, int(height * scale))
        return target_width, target_height

    def _build_llm_image_variant_path(
        self, artifact_path: Path, media_type: str
    ) -> Path:
        suffix_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        suffix = suffix_map.get(media_type, artifact_path.suffix or ".img")
        stem = artifact_path.stem or "image"
        return (
            artifact_path.parent.parent
            / LLM_IMAGE_VARIANT_DIR_NAME
            / f"{stem}.claude-input{suffix}"
        )

    def _maybe_build_llm_image_variant(
        self,
        artifact: dict[str, Any],
        artifact_path: Path,
    ) -> tuple[Path, str, str]:
        media_type = (
            self._safe_text(artifact.get("mime")) or "application/octet-stream"
        ).lower()
        filename = self._safe_text(artifact.get("filename")) or artifact_path.name
        if Image is None or media_type not in {"image/jpeg", "image/png", "image/webp"}:
            return artifact_path, media_type, filename
        try:
            with Image.open(artifact_path) as image:
                normalized = (
                    ImageOps.exif_transpose(image) if ImageOps is not None else image
                )
                width, height = normalized.size
                target_width, target_height = self._llm_image_target_size(
                    int(width), int(height)
                )
                if target_width == width and target_height == height:
                    return artifact_path, media_type, filename
                variant_path = self._build_llm_image_variant_path(
                    artifact_path, media_type
                )
                if (
                    variant_path.exists()
                    and variant_path.stat().st_mtime >= artifact_path.stat().st_mtime
                ):
                    return variant_path, media_type, variant_path.name
                variant_path.parent.mkdir(parents=True, exist_ok=True)
                resized = normalized.resize(
                    (target_width, target_height),
                    getattr(Image, "Resampling", Image).LANCZOS,
                )
                save_kwargs: dict[str, Any]
                if media_type == "image/jpeg":
                    if resized.mode not in {"RGB", "L"}:
                        resized = resized.convert("RGB")
                    save_kwargs = {
                        "format": "JPEG",
                        "quality": int(self.config.llm_image_jpeg_quality),
                        "optimize": True,
                        "progressive": True,
                    }
                elif media_type == "image/png":
                    save_kwargs = {"format": "PNG", "optimize": True}
                else:
                    save_kwargs = {
                        "format": "WEBP",
                        "quality": int(self.config.llm_image_jpeg_quality),
                        "method": 6,
                    }
                resized.save(variant_path, **save_kwargs)
                return variant_path, media_type, variant_path.name
        except (FileNotFoundError, PermissionError, UnidentifiedImageError, OSError):
            return artifact_path, media_type, filename
        except Exception:
            logger.exception(
                "gateway.llm_image_variant_failed artifact_id=%s path=%s",
                self._safe_text(artifact.get("artifact_id")),
                artifact_path,
            )
            return artifact_path, media_type, filename

    def _logical_artifact_path(self, path: Path) -> str:
        resolved = path.resolve()
        relative = resolved.relative_to(self.config.artifacts_root.resolve())
        return (Path("runs") / "artifacts" / relative).as_posix()

    def _resolve_logical_artifact_path(self, logical_path: str | None) -> Path | None:
        normalized = self._safe_text(logical_path)
        if not normalized:
            return None
        relative = Path(normalized)
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == "runs" and parts[1] == "artifacts":
            relative = Path(*parts[2:])
        candidate = (self.config.artifacts_root / relative).resolve()
        try:
            candidate.relative_to(self.config.artifacts_root.resolve())
        except ValueError:
            return None
        return candidate

    def _public_gateway_base_url(self) -> str | None:
        explicit = self._safe_text(self.config.public_base_url)
        if explicit:
            return explicit.rstrip("/")
        public_host = self._safe_text(self.config.public_host)
        if not public_host:
            return None
        if public_host.startswith(("http://", "https://")):
            return public_host.rstrip("/")
        return f"https://{public_host.rstrip('/')}"

    def _agent_email_webhook_url(self) -> str | None:
        base_url = self._public_gateway_base_url()
        if not base_url:
            return None
        return f"{base_url}/internal/channels/agent-email/incoming"

    async def _resolve_agent_email_webhook_mailbox(
        self,
        client: CosmicMailClient,
        *,
        mailbox_address: str | None,
    ) -> dict[str, Any] | None:
        normalized_address = self._safe_text(mailbox_address)
        if normalized_address:
            try:
                return await client.resolve_mailbox(mailbox_address=normalized_address)
            except Exception:
                logger.warning(
                    "gateway.agent_email_webhook_mailbox_resolution_failed mailbox_address=%s",
                    normalized_address,
                )
        try:
            mailboxes = await client.list_mailboxes()
        except Exception:
            return None
        active_mailboxes = [
            mailbox
            for mailbox in mailboxes
            if self._safe_text(mailbox.get("status")).lower() == "active"
        ]
        pool = active_mailboxes or mailboxes
        return pool[0] if pool else None

    async def _prepare_agent_email_connection_settings(
        self,
        *,
        base_url: str,
        api_token: str,
        primary_mailbox_address: str | None,
    ) -> dict[str, Any]:
        normalized_base_url = normalize_cosmic_mail_base_url(base_url)
        normalized_api_token = self._safe_text(api_token)
        normalized_mailbox = self._safe_text(primary_mailbox_address)
        if not normalized_base_url or not normalized_api_token:
            raise ValueError("base_url and api_token are required")

        client = CosmicMailClient(
            base_url=normalized_base_url,
            api_token=normalized_api_token,
            timeout_sec=self.config.cosmic_mail_timeout_sec,
        )
        try:
            auth_context = await client.get_auth_context()
            mailbox = await self._resolve_agent_email_webhook_mailbox(
                client,
                mailbox_address=normalized_mailbox,
            )
            if not isinstance(mailbox, dict):
                raise ValueError(
                    "No active Cosmic Mail mailbox is available for Agent Email."
                )
            mailbox_address = self._safe_text(mailbox.get("address"))
            if mailbox_address:
                normalized_mailbox = mailbox_address
            organization_id = self._safe_text(
                auth_context.get("organization_id")
            ) or self._safe_text(mailbox.get("organization_id"))
            effective_api_token = normalized_api_token
            token_scope = (
                "organization"
                if self._safe_text(auth_context.get("organization_id"))
                else "admin"
                if bool(auth_context.get("is_admin"))
                else "unknown"
            )
            if token_scope == "admin":
                if not organization_id:
                    raise ValueError(
                        "Could not resolve the mailbox organization for Agent Email."
                    )
                minted = await client.create_organization_api_key(
                    organization_id,
                    name=_AGENT_EMAIL_ORG_API_KEY_NAME,
                )
                minted_token = self._safe_text(minted.get("plaintext_key"))
                if not minted_token:
                    raise ValueError(
                        "Cosmic Mail did not return a plaintext org API key."
                    )
                effective_api_token = minted_token
                token_scope = "organization_minted"
            return {
                "base_url": normalized_base_url,
                "api_token": effective_api_token,
                "primary_mailbox_address": normalized_mailbox,
                "mailbox_id": self._safe_text(mailbox.get("id")),
                "organization_id": organization_id,
                "token_scope": token_scope,
            }
        finally:
            await client.aclose()

    def _is_managed_agent_email_webhook(
        self,
        webhook: dict[str, Any],
        *,
        webhook_url: str,
    ) -> bool:
        url = self._safe_text(webhook.get("url")) or ""
        return url == webhook_url or url.endswith(
            "/internal/channels/agent-email/incoming"
        )

    async def sync_agent_email_webhook(self) -> dict[str, Any]:
        settings = self._effective_agent_email_settings()
        if not settings.get("base_url") or not settings.get("api_token"):
            return {"status": "skipped", "reason": "agent_email_not_configured"}
        webhook_url = self._agent_email_webhook_url()
        if not webhook_url:
            return {"status": "skipped", "reason": "gateway_public_url_missing"}

        effective = await self._prepare_agent_email_connection_settings(
            base_url=str(settings["base_url"]),
            api_token=str(settings["api_token"]),
            primary_mailbox_address=settings.get("primary_mailbox_address"),
        )
        if (
            str(effective["api_token"]) != str(settings["api_token"])
            or str(effective["primary_mailbox_address"] or "")
            != str(settings.get("primary_mailbox_address") or "")
        ) and settings.get("source") == "integration_store":
            self.agent_email_integration_store.save_primary(
                base_url=str(effective["base_url"]),
                api_token=str(effective["api_token"]),
                primary_mailbox_address=effective.get("primary_mailbox_address"),
                webhook_secret=self.config.cosmic_mail_webhook_secret,
                webhook_signature_header=self.config.cosmic_mail_webhook_signature_header,
                updated_at=utcnow_iso(),
            )
            settings = {
                **settings,
                "api_token": str(effective["api_token"]),
                "primary_mailbox_address": str(
                    effective.get("primary_mailbox_address") or ""
                ),
            }
        else:
            settings = {
                **settings,
                "api_token": str(effective["api_token"]),
                "primary_mailbox_address": str(
                    effective.get("primary_mailbox_address") or ""
                ),
            }

        client = CosmicMailClient(
            base_url=settings["base_url"],
            api_token=settings["api_token"],
            timeout_sec=self.config.cosmic_mail_timeout_sec,
        )
        try:
            mailbox = await self._resolve_agent_email_webhook_mailbox(
                client,
                mailbox_address=settings.get("primary_mailbox_address"),
            )
            mailbox_id = (
                self._safe_text(mailbox.get("id"))
                if isinstance(mailbox, dict)
                else None
            )
            webhook_secret = self._safe_text(settings.get("webhook_secret")) or None
            webhooks = await client.list_webhooks()
            managed = [
                webhook
                for webhook in webhooks
                if isinstance(webhook, dict)
                and self._is_managed_agent_email_webhook(
                    webhook, webhook_url=webhook_url
                )
            ]
            desired_event_type = "message.received"
            desired_payload = {
                "mailbox_id": mailbox_id,
                "event_type": desired_event_type,
                "url": webhook_url,
                "secret": webhook_secret,
            }

            primary = managed[0] if managed else None
            action = "created"
            result_webhook: dict[str, Any]
            if primary is None:
                result_webhook = await client.create_webhook(desired_payload)
            else:
                webhook_id = self._safe_text(primary.get("id"))
                patch_needed = (
                    self._safe_text(primary.get("url")) != webhook_url
                    or self._safe_text(primary.get("mailbox_id")) != mailbox_id
                    or self._safe_text(primary.get("event_type")) != desired_event_type
                    or not bool(primary.get("is_active"))
                )
                if patch_needed and webhook_id:
                    result_webhook = await client.update_webhook(
                        webhook_id,
                        {
                            "mailbox_id": mailbox_id,
                            "event_type": desired_event_type,
                            "url": webhook_url,
                            "secret": webhook_secret,
                            "is_active": True,
                        },
                    )
                    action = "updated"
                else:
                    result_webhook = primary
                    action = "unchanged"

                primary_id = self._safe_text(result_webhook.get("id")) or webhook_id
                for duplicate in managed[1:]:
                    duplicate_id = self._safe_text(duplicate.get("id"))
                    if duplicate_id and duplicate_id != primary_id:
                        await client.delete_webhook(duplicate_id)

            return {
                "status": action,
                "url": webhook_url,
                "mailbox_id": mailbox_id,
                "webhook_id": self._safe_text(result_webhook.get("id")),
            }
        finally:
            await client.aclose()

    async def clear_agent_email_webhook(self) -> dict[str, Any]:
        settings = self._effective_agent_email_settings()
        if not settings.get("base_url") or not settings.get("api_token"):
            return {"status": "skipped", "reason": "agent_email_not_configured"}
        webhook_url = self._agent_email_webhook_url()
        if not webhook_url:
            return {"status": "skipped", "reason": "gateway_public_url_missing"}

        client = CosmicMailClient(
            base_url=settings["base_url"],
            api_token=settings["api_token"],
            timeout_sec=self.config.cosmic_mail_timeout_sec,
        )
        try:
            deleted = 0
            for webhook in await client.list_webhooks():
                if not isinstance(
                    webhook, dict
                ) or not self._is_managed_agent_email_webhook(
                    webhook, webhook_url=webhook_url
                ):
                    continue
                webhook_id = self._safe_text(webhook.get("id"))
                if not webhook_id:
                    continue
                await client.delete_webhook(webhook_id)
                deleted += 1
            return {
                "status": "deleted" if deleted else "unchanged",
                "url": webhook_url,
                "deleted_count": deleted,
            }
        finally:
            await client.aclose()

    def _build_artifact_access_signature(
        self,
        *,
        artifact_id: str,
        purpose: str,
        expires_at: int,
        sha256: str | None,
    ) -> str:
        secret = self._safe_text(self.config.signing_secret)
        if not secret:
            raise RuntimeError(
                "GATEWAY_SIGNING_SECRET is required for signed artifact URLs."
            )
        payload = "\n".join(
            [
                artifact_id,
                purpose,
                str(expires_at),
                self._safe_text(sha256) or "",
            ]
        ).encode("utf-8")
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def mint_artifact_access_url(
        self, artifact: dict[str, Any], *, purpose: str = "llm_image_fetch"
    ) -> str | None:
        if not isinstance(artifact, dict):
            return None
        artifact_id = self._safe_text(artifact.get("artifact_id"))
        if not artifact_id:
            return None
        if purpose == "llm_image_fetch" and not is_supported_image_artifact(artifact):
            return None
        base_url = self._public_gateway_base_url()
        if not base_url:
            return None
        expires_at = int(time.time()) + max(
            30, int(self.config.artifact_signed_url_ttl_sec)
        )
        signature = self._build_artifact_access_signature(
            artifact_id=artifact_id,
            purpose=purpose,
            expires_at=expires_at,
            sha256=self._safe_text(artifact.get("sha256")),
        )
        query = urlencode({"purpose": purpose, "exp": expires_at, "sig": signature})
        return f"{base_url}/artifacts/content/{artifact_id}?{query}"

    def get_signed_artifact_content(
        self,
        *,
        artifact_id: str,
        purpose: str,
        expires_at: int,
        signature: str,
    ) -> dict[str, Any]:
        normalized_artifact_id = self._safe_text(artifact_id)
        normalized_purpose = self._safe_text(purpose)
        normalized_signature = self._safe_text(signature)
        if (
            not normalized_artifact_id
            or not normalized_purpose
            or not normalized_signature
        ):
            raise ValueError("artifact_id, purpose, and sig are required.")
        now_ts = int(time.time())
        if expires_at < now_ts:
            raise PermissionError("Signed artifact URL has expired.")
        artifact = self.artifact_store.get(normalized_artifact_id)
        if artifact is None:
            artifact = self._lookup_stored_output_artifact(normalized_artifact_id)
            if artifact is not None:
                self._cache_output_artifacts(
                    request_id=self._safe_text(artifact.get("request_id")) or "",
                    session_id=self._safe_text(artifact.get("session_id")) or "",
                    source_channel=self._safe_text(artifact.get("source_channel"))
                    or "",
                    source_message_id=self._safe_text(
                        artifact.get("source_message_id")
                    ),
                    produced_artifacts=[artifact],
                )
        if artifact is None:
            raise FileNotFoundError("Artifact not found.")
        if normalized_purpose == "llm_image_fetch" and not is_supported_image_artifact(
            artifact
        ):
            raise ValueError("Artifact is not an LLM-fetchable image.")
        expected_signature = self._build_artifact_access_signature(
            artifact_id=normalized_artifact_id,
            purpose=normalized_purpose,
            expires_at=expires_at,
            sha256=self._safe_text(artifact.get("sha256")),
        )
        if not hmac.compare_digest(normalized_signature, expected_signature):
            raise PermissionError("Invalid signed artifact URL.")
        artifact_path = self._resolve_logical_artifact_path(
            self._safe_text(artifact.get("path"))
        )
        if (
            artifact_path is None
            or not artifact_path.exists()
            or not artifact_path.is_file()
        ):
            raise FileNotFoundError("Artifact bytes are unavailable.")
        resolved_path = artifact_path
        media_type = self._safe_text(artifact.get("mime")) or "application/octet-stream"
        filename = self._safe_text(artifact.get("filename")) or artifact_path.name
        if normalized_purpose == "llm_image_fetch":
            resolved_path, media_type, filename = self._maybe_build_llm_image_variant(
                artifact, artifact_path
            )
        return {
            "artifact": artifact,
            "content": resolved_path.read_bytes(),
            "media_type": media_type,
            "filename": filename,
        }

    def get_desktop_output_artifact_content(
        self,
        *,
        message_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        normalized_message_id = self._safe_text(message_id)
        normalized_artifact_id = self._safe_text(artifact_id)
        if not normalized_message_id or not normalized_artifact_id:
            raise ValueError("message_id and artifact_id are required.")

        message = self.session_store.get_message(normalized_message_id)
        if message is None:
            raise FileNotFoundError("Assistant message not found.")
        metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        produced_artifacts = self._normalize_produced_artifact_list(
            metadata.get("produced_artifacts")
        )
        target = next(
            (
                item
                for item in produced_artifacts
                if isinstance(item, dict)
                and self._safe_text(item.get("artifact_id")) == normalized_artifact_id
            ),
            None,
        )
        if target is None:
            raise FileNotFoundError("Produced artifact not found on this message.")
        artifact_path = self._resolve_logical_artifact_path(
            self._safe_text(target.get("path"))
        )
        if (
            artifact_path is None
            or not artifact_path.exists()
            or not artifact_path.is_file()
        ):
            raise FileNotFoundError("Produced artifact bytes are unavailable.")
        return {
            "content": artifact_path.read_bytes(),
            "media_type": self._safe_text(target.get("mime_type"))
            or "application/octet-stream",
            "filename": self._safe_text(target.get("filename")) or artifact_path.name,
        }

    def _lookup_stored_output_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        normalized_artifact_id = self._safe_text(artifact_id)
        if not normalized_artifact_id:
            return None
        message = self.session_store.find_message_by_output_artifact_id(
            normalized_artifact_id
        )
        if not isinstance(message, dict):
            return None
        metadata = (
            message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        )
        produced_artifacts = self._normalize_produced_artifact_list(
            metadata.get("produced_artifacts")
        )
        target = next(
            (
                item
                for item in produced_artifacts
                if isinstance(item, dict)
                and self._safe_text(item.get("artifact_id")) == normalized_artifact_id
            ),
            None,
        )
        if target is None:
            return None
        artifact = dict(target)
        artifact["request_id"] = self._safe_text(message.get("request_id"))
        artifact["session_id"] = self._safe_text(message.get("session_id"))
        artifact["source_channel"] = self._safe_text(message.get("channel"))
        artifact["source_platform"] = self._channel_platform(
            self._safe_text(message.get("channel"))
        )
        artifact["source_message_id"] = self._safe_text(message.get("message_id"))
        return artifact

    def _cache_artifact_list(
        self,
        *,
        request_id: str,
        session_id: str,
        source_channel: str,
        source_message_id: str | None,
        artifacts: list[dict[str, Any]],
    ) -> None:
        normalized_request_id = self._safe_text(request_id)
        normalized_session_id = self._safe_text(session_id)
        normalized_source_channel = self._safe_text(source_channel)
        if (
            not normalized_request_id
            or not normalized_session_id
            or not normalized_source_channel
        ):
            return
        if not isinstance(artifacts, list) or not artifacts:
            return
        try:
            self.artifact_store.persist_output_artifacts(
                request_id=normalized_request_id,
                session_id=normalized_session_id,
                source_channel=normalized_source_channel,
                source_platform=self._channel_platform(normalized_source_channel),
                source_message_id=self._safe_text(source_message_id),
                artifacts=artifacts,
            )
        except Exception:
            logger.exception(
                "gateway.output_artifact_cache_failed request_id=%s session_id=%s source_channel=%s artifact_count=%s",
                normalized_request_id,
                normalized_session_id,
                normalized_source_channel,
                len(artifacts),
            )

    def _cache_output_artifacts(
        self,
        *,
        request_id: str,
        session_id: str,
        source_channel: str,
        source_message_id: str | None,
        produced_artifacts: list[dict[str, Any]],
    ) -> None:
        self._cache_artifact_list(
            request_id=request_id,
            session_id=session_id,
            source_channel=source_channel,
            source_message_id=source_message_id,
            artifacts=produced_artifacts,
        )

    async def _ensure_request_documents_parsed(
        self, request_record: dict[str, Any], *, send=None
    ) -> None:
        if self._redis is None:
            return
        if self._safe_text(request_record.get("route")) != "opus":
            return
        if (
            not self.config.docs_auto_parse_enabled
            and not self.config.tabular_auto_parse_enabled
        ):
            return
        channel = self._safe_text(request_record.get("channel")) or ""
        input_artifacts = (
            request_record.get("input_artifacts")
            if isinstance(request_record.get("input_artifacts"), list)
            else []
        )
        document_artifacts = [
            artifact
            for artifact in input_artifacts
            if isinstance(artifact, dict)
            and is_supported_document_artifact(artifact)
            and self._safe_text(artifact.get("path"))
            and self._safe_text(artifact.get("ingest_state")) == "staged"
        ]
        tabular_artifacts = [
            artifact
            for artifact in input_artifacts
            if isinstance(artifact, dict)
            and is_supported_tabular_artifact(artifact)
            and self._safe_text(artifact.get("path"))
            and self._safe_text(artifact.get("ingest_state")) == "staged"
        ]
        if not document_artifacts and not tabular_artifacts:
            return

        tasks: list[Any] = []
        if document_artifacts and self.config.docs_auto_parse_enabled:
            tasks.append(
                self._ensure_docs_parsed_for_request(
                    request_record, document_artifacts, send=send, channel=channel
                )
            )
        if tabular_artifacts and self.config.tabular_auto_parse_enabled:
            tasks.append(
                self._ensure_tabular_parsed_for_request(
                    request_record, tabular_artifacts, send=send, channel=channel
                )
            )
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.exception(
                    "gateway.attachments_autoparse_failed request_id=%s",
                    self._safe_text(request_record.get("request_id")),
                )

    async def _ensure_docs_parsed_for_request(
        self,
        request_record: dict[str, Any],
        document_artifacts: list[dict[str, Any]],
        *,
        send,
        channel: str,
    ) -> None:
        request_id = self._safe_text(request_record.get("request_id"))
        artifact_ids = [
            self._safe_text(artifact.get("artifact_id"))
            for artifact in document_artifacts
            if self._safe_text(artifact.get("artifact_id"))
        ]
        if send is not None and self._is_realtime_client_channel(channel):
            await send(
                self._build_docs_parse_progress_event(
                    request_record=request_record,
                    status="docs_prepare",
                    message="Preparing your attached documents for parsing.",
                    docs_progress={
                        "kind": "docs_parse",
                        "stage": "prepare",
                        "label": "Preparing attached documents",
                        "detail": f"{len(document_artifacts)} document(s) queued for parsing.",
                        "current": 0,
                        "total": len(document_artifacts),
                        "percent": 0.06,
                    },
                )
            )

        try:
            parse_result = await self._dispatch_docs_parse_bundle(
                request_record=request_record,
                input_artifacts=document_artifacts,
                progress_callback=send,
            )
        except Exception as exc:
            logger.exception(
                "gateway.docs_autoparse_failed request_id=%s artifact_count=%s",
                self._safe_text(request_record.get("request_id")),
                len(document_artifacts),
            )
            parse_result = {
                "status": "failed",
                "error_message": str(exc).strip() or "docs.parse_bundle failed",
            }
        status = self._safe_text(parse_result.get("status")) or "failed"
        if status == "completed":
            self._apply_docs_parse_success(
                request_id=request_id,
                parse_result=parse_result,
                artifact_ids=artifact_ids,
            )
            if send is not None and self._is_realtime_client_channel(channel):
                await send(
                    self._build_docs_parse_progress_event(
                        request_record=request_record,
                        task_id=self._safe_text(parse_result.get("task_id")) or None,
                        status="docs_ready",
                        message="Documents parsed successfully. Preparing the answer.",
                        docs_progress={
                            "kind": "docs_parse",
                            "stage": "ready",
                            "label": "Documents parsed",
                            "detail": "Preparing the answer over the parsed bundle.",
                            "current": len(document_artifacts),
                            "total": len(document_artifacts),
                            "percent": 1.0,
                        },
                    )
                )
            return
        task_id = self._safe_text(parse_result.get("task_id")) or None
        error_text = (
            self._safe_text(parse_result.get("error_message"))
            or "docs.parse_bundle failed"
        )
        if status == "pending":
            self._apply_docs_parse_failure(
                request_id=request_id,
                artifact_ids=artifact_ids,
                error_text=error_text,
                ingest_state="parse_pending",
                task_id=task_id,
            )
            if request_id and task_id:
                self._track_background_task(
                    self._reconcile_docs_parse_bundle(
                        request_id=request_id,
                        artifact_ids=artifact_ids,
                        task_id=task_id,
                    )
                )
            return
        self._apply_docs_parse_failure(
            request_id=request_id,
            artifact_ids=artifact_ids,
            error_text=error_text,
            ingest_state="parse_failed",
            task_id=task_id,
        )

    async def _ensure_tabular_parsed_for_request(
        self,
        request_record: dict[str, Any],
        tabular_artifacts: list[dict[str, Any]],
        *,
        send,
        channel: str,
    ) -> None:
        request_id = self._safe_text(request_record.get("request_id"))
        artifact_ids = [
            self._safe_text(artifact.get("artifact_id"))
            for artifact in tabular_artifacts
            if self._safe_text(artifact.get("artifact_id"))
        ]
        if send is not None and self._is_realtime_client_channel(channel):
            await send(
                self._build_tabular_parse_progress_event(
                    request_record=request_record,
                    status="tabular_prepare",
                    message="Preparing your attached spreadsheets for parsing.",
                    tabular_progress={
                        "kind": "tabular_parse",
                        "stage": "prepare",
                        "label": "Preparing spreadsheets",
                        "detail": f"{len(tabular_artifacts)} spreadsheet file(s) queued.",
                        "current": 0,
                        "total": len(tabular_artifacts),
                        "percent": 0.06,
                    },
                )
            )
        try:
            parse_result = await self._dispatch_tabular_parse_bundle(
                request_record=request_record,
                input_artifacts=tabular_artifacts,
                progress_callback=send,
            )
        except Exception as exc:
            logger.exception(
                "gateway.tabular_autoparse_failed request_id=%s artifact_count=%s",
                request_id,
                len(tabular_artifacts),
            )
            parse_result = {
                "status": "failed",
                "error_message": str(exc).strip() or "tabular.parse_bundle failed",
            }
        status = self._safe_text(parse_result.get("status")) or "failed"
        if status == "completed":
            self._apply_tabular_parse_success(
                request_id=request_id,
                parse_result=parse_result,
                artifact_ids=artifact_ids,
            )
            if send is not None and self._is_realtime_client_channel(channel):
                await send(
                    self._build_tabular_parse_progress_event(
                        request_record=request_record,
                        task_id=self._safe_text(parse_result.get("task_id")) or None,
                        status="tabular_ready",
                        message="Spreadsheets parsed. Preparing the answer.",
                        tabular_progress={
                            "kind": "tabular_parse",
                            "stage": "ready",
                            "label": "Spreadsheets parsed",
                            "detail": "Preparing the answer over the tabular bundle.",
                            "current": len(tabular_artifacts),
                            "total": len(tabular_artifacts),
                            "percent": 1.0,
                        },
                    )
                )
            return
        task_id = self._safe_text(parse_result.get("task_id")) or None
        error_text = (
            self._safe_text(parse_result.get("error_message"))
            or "tabular.parse_bundle failed"
        )
        if status == "pending":
            self._apply_tabular_parse_failure(
                request_id=request_id,
                artifact_ids=artifact_ids,
                error_text=error_text,
                ingest_state="parse_pending",
                task_id=task_id,
            )
            if request_id and task_id:
                self._track_background_task(
                    self._reconcile_tabular_parse_bundle(
                        request_id=request_id,
                        artifact_ids=artifact_ids,
                        task_id=task_id,
                    )
                )
            return
        self._apply_tabular_parse_failure(
            request_id=request_id,
            artifact_ids=artifact_ids,
            error_text=error_text,
            ingest_state="parse_failed",
            task_id=task_id,
        )

    def _apply_docs_parse_success(
        self,
        *,
        request_id: str,
        parse_result: dict[str, Any],
        artifact_ids: list[str],
    ) -> None:
        output = (
            parse_result.get("output")
            if isinstance(parse_result.get("output"), dict)
            else {}
        )
        bundle_id = self._safe_text(output.get("bundle_id")) or None
        parse_task_id = self._safe_text(parse_result.get("task_id")) or None
        by_artifact_id: dict[str, dict[str, Any]] = {}
        for document in (
            output.get("documents", [])
            if isinstance(output.get("documents"), list)
            else []
        ):
            if not isinstance(document, dict):
                continue
            artifact_id = self._safe_text(document.get("artifact_id"))
            if artifact_id:
                by_artifact_id[artifact_id] = document

        for artifact_id in artifact_ids:
            document_summary = by_artifact_id.get(artifact_id)
            if document_summary is None:
                continue
            self._update_request_artifact_fields(
                request_id,
                artifact_id,
                ingest_state="parsed",
                parse_task_id=parse_task_id,
                parse_bundle_id=bundle_id,
                parsed_summary=document_summary,
                docs_tools=[
                    "docs_browse",
                    "docs_search",
                    "docs_read",
                    "docs_fetch_asset",
                ],
                parse_error=None,
            )
            self.artifact_store.update_ingest_state(
                artifact_id,
                ingest_state="parsed",
                parse_task_id=parse_task_id,
                parse_bundle_id=bundle_id,
                parsed_summary=document_summary,
            )

    def _apply_docs_parse_failure(
        self,
        *,
        request_id: str,
        artifact_ids: list[str],
        error_text: str,
        ingest_state: str,
        task_id: str | None,
    ) -> None:
        for artifact_id in artifact_ids:
            self._update_request_artifact_fields(
                request_id,
                artifact_id,
                parse_error=error_text,
                ingest_state=ingest_state,
                parse_task_id=task_id,
            )
            self.artifact_store.update_ingest_state(
                artifact_id,
                ingest_state=ingest_state,
                parse_task_id=task_id,
            )

    def _update_request_artifact_fields(
        self, request_id: str, artifact_id: str, **fields: Any
    ) -> None:
        if not request_id or not artifact_id:
            return
        request_record = self.request_records.get(request_id)
        if not isinstance(request_record, dict):
            return
        input_artifacts = request_record.get("input_artifacts")
        if not isinstance(input_artifacts, list):
            return
        for artifact in input_artifacts:
            if not isinstance(artifact, dict):
                continue
            if self._safe_text(artifact.get("artifact_id")) != artifact_id:
                continue
            for key, value in fields.items():
                if value is None:
                    artifact.pop(key, None)
                else:
                    artifact[key] = value
            return

    async def _reconcile_docs_parse_bundle(
        self,
        *,
        request_id: str,
        artifact_ids: list[str],
        task_id: str,
    ) -> None:
        try:
            parse_result = await self._wait_for_agent_terminal_result(
                task_id,
                timeout_sec=self.config.docs_parse_reconcile_timeout_sec,
            )
        except Exception:
            logger.exception(
                "gateway.docs_autoparse_reconcile_failed request_id=%s task_id=%s",
                request_id,
                task_id,
            )
            return
        status = self._safe_text(parse_result.get("status")) or "failed"
        if status == "completed":
            self._apply_docs_parse_success(
                request_id=request_id,
                parse_result=parse_result,
                artifact_ids=artifact_ids,
            )
            return
        if status == "pending":
            logger.warning(
                "gateway.docs_autoparse_reconcile_still_pending request_id=%s task_id=%s artifact_count=%s",
                request_id,
                task_id,
                len(artifact_ids),
            )
            return
        error_text = (
            self._safe_text(parse_result.get("error_message"))
            or "docs.parse_bundle failed"
        )
        self._apply_docs_parse_failure(
            request_id=request_id,
            artifact_ids=artifact_ids,
            error_text=error_text,
            ingest_state="parse_failed",
            task_id=task_id,
        )

    async def _dispatch_docs_parse_bundle(
        self,
        *,
        request_record: dict[str, Any],
        input_artifacts: list[dict[str, Any]],
        progress_callback=None,
    ) -> dict[str, Any]:
        session_id = self._safe_text(request_record.get("session_id"))
        request_id = self._safe_text(request_record.get("request_id"))
        channel = self._safe_text(request_record.get("channel"))
        source = self._safe_text(request_record.get("source")) or "user"
        source_id = self._safe_text(request_record.get("source_id"))
        if not session_id or not request_id:
            return {
                "status": "failed",
                "error_message": "Missing session_id or request_id for docs parsing.",
            }

        child_input = {
            "bundle_label": f"request:{request_id}",
            "ocr_mode": "auto",
            "generate_page_images": False,
            "generate_picture_images": False,
            "request_id": request_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        "artifact_id": self._safe_text(item.get("artifact_id")),
                        "path": self._safe_text(item.get("path")),
                        "sha256": self._safe_text(item.get("sha256")),
                    }
                    for item in input_artifacts
                ],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient=self.config.docs_parser_agent_id,
            intent="docs.parse_bundle",
            input=child_input,
            input_artifacts=input_artifacts,
            idempotency_key=f"docs-parse:{request_id}:{fingerprint}",
            priority=SOURCE_PRIORITY_MAP.get(source, "normal"),
            signature="",
            created_at=utcnow(),
            source=source,
            source_id=source_id or request_id,
            channel=channel or None,
        )
        task = task.model_copy(
            update={"signature": sign_task_envelope(task, self.config.signing_secret)}
        )
        await dispatch_task(task, self._redis)
        return await self._wait_for_agent_terminal_result(
            task.task_id,
            timeout_sec=self.config.docs_parse_timeout_sec,
            progress_callback=(
                (
                    lambda event: progress_callback(
                        self._build_docs_parse_progress_event(
                            request_record=request_record,
                            task_id=task.task_id,
                            status="docs_parsing",
                            message=self._safe_text(event.get("message"))
                            or "Parsing attached documents.",
                            docs_progress=self._derive_docs_parse_progress(
                                message=self._safe_text(event.get("message"))
                                or "Parsing attached documents.",
                                total_documents=len(input_artifacts),
                            ),
                        )
                    )
                )
                if progress_callback is not None
                and self._is_realtime_client_channel(channel)
                else None
            ),
            poll_interval_sec=self.config.docs_parse_poll_interval_sec,
        )

    async def _dispatch_tabular_parse_bundle(
        self,
        *,
        request_record: dict[str, Any],
        input_artifacts: list[dict[str, Any]],
        progress_callback=None,
    ) -> dict[str, Any]:
        session_id = self._safe_text(request_record.get("session_id"))
        request_id = self._safe_text(request_record.get("request_id"))
        channel = self._safe_text(request_record.get("channel"))
        source = self._safe_text(request_record.get("source")) or "user"
        source_id = self._safe_text(request_record.get("source_id"))
        if not session_id or not request_id:
            return {
                "status": "failed",
                "error_message": "Missing session_id or request_id for tabular parsing.",
            }

        child_input = {
            "bundle_label": f"request:{request_id}",
            "request_id": request_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        "artifact_id": self._safe_text(item.get("artifact_id")),
                        "path": self._safe_text(item.get("path")),
                        "sha256": self._safe_text(item.get("sha256")),
                    }
                    for item in input_artifacts
                ],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=session_id,
            session_id=session_id,
            sender="cosmic/gateway:1.0.0",
            recipient=self.config.tabular_agent_id,
            intent="tabular.parse_bundle",
            input=child_input,
            input_artifacts=input_artifacts,
            idempotency_key=f"tabular-parse:{request_id}:{fingerprint}",
            priority=SOURCE_PRIORITY_MAP.get(source, "normal"),
            signature="",
            created_at=utcnow(),
            source=source,
            source_id=source_id or request_id,
            channel=channel or None,
        )
        task = task.model_copy(
            update={"signature": sign_task_envelope(task, self.config.signing_secret)}
        )
        await dispatch_task(task, self._redis)
        return await self._wait_for_agent_terminal_result(
            task.task_id,
            timeout_sec=self.config.tabular_parse_timeout_sec,
            progress_callback=(
                (
                    lambda event: progress_callback(
                        self._build_tabular_parse_progress_event(
                            request_record=request_record,
                            task_id=task.task_id,
                            status="tabular_parsing",
                            message=self._safe_text(event.get("message"))
                            or "Parsing spreadsheets.",
                            tabular_progress=self._derive_tabular_parse_progress(
                                message=self._safe_text(event.get("message"))
                                or "Parsing spreadsheets.",
                                payload=event.get("payload")
                                if isinstance(event.get("payload"), dict)
                                else {},
                                total_files=len(input_artifacts),
                            ),
                        )
                    )
                )
                if progress_callback is not None
                and self._is_realtime_client_channel(channel)
                else None
            ),
            poll_interval_sec=self.config.tabular_parse_poll_interval_sec,
        )

    async def _wait_for_agent_terminal_result(
        self,
        task_id: str,
        *,
        timeout_sec: float,
        progress_callback=None,
        poll_interval_sec: float | None = None,
    ) -> dict[str, Any]:
        if self._redis is None:
            return {
                "status": "failed",
                "error_message": "Redis is not available for agent result tracking.",
            }
        event_ids_key = f"task_events:{task_id}"
        seen_message_ids: set[str] = set()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            message_ids = await self._redis.lrange(event_ids_key, 0, -1)
            for message_id in message_ids:
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                stream_entries = await self._redis.xrange(
                    "streams:events", min=message_id, max=message_id
                )
                for _, fields in stream_entries:
                    event = parse_event_envelope(fields)
                    if event.task_id != task_id:
                        continue
                    if event.event_type == "task.completed":
                        return {
                            "status": "completed",
                            "task_id": task_id,
                            "output": event.payload.get("output")
                            if isinstance(event.payload, dict)
                            else {},
                            "artifacts": event.payload.get("artifacts")
                            if isinstance(event.payload, dict)
                            else [],
                        }
                    if event.event_type == "task.progress":
                        if progress_callback is not None:
                            payload = (
                                event.payload if isinstance(event.payload, dict) else {}
                            )
                            await progress_callback(
                                {
                                    "task_id": task_id,
                                    "message": self._safe_text(payload.get("message")),
                                    "payload": payload,
                                }
                            )
                        continue
                    if event.event_type == "task.failed":
                        error = (
                            event.payload.get("error")
                            if isinstance(event.payload, dict)
                            else {}
                        )
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "error_message": self._safe_text(error.get("message"))
                            or "Agent task failed.",
                            "error": error,
                        }
                    if event.event_type == "task.rejected":
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "error_message": self._safe_text(
                                event.payload.get("reason")
                            )
                            or "Agent task was rejected.",
                        }
            await asyncio.sleep(
                poll_interval_sec
                if poll_interval_sec is not None
                else self.config.docs_parse_poll_interval_sec
            )
        return {
            "status": "pending",
            "task_id": task_id,
            "error_message": f"Timed out waiting for {task_id}.",
        }

    def _derive_tabular_parse_progress(
        self,
        *,
        message: str,
        payload: dict[str, Any],
        total_files: int,
    ) -> dict[str, Any]:
        total = max(1, total_files)
        normalized = self._safe_text(message)
        current = int(payload.get("current_file_index") or 0) or 0
        stage = self._safe_text(payload.get("stage")) or "parse_sheets"
        label = "Parsing spreadsheets"
        detail = normalized or "Parsing spreadsheets."
        percent = 0.12
        if current and total:
            label = f"Parsing file {current} of {total}"
            percent = min(0.85, 0.12 + ((current - 1) / total) * 0.65)
        if stage == "ready":
            percent = 1.0
        return {
            "kind": "tabular_parse",
            "stage": stage,
            "label": label,
            "detail": detail,
            "current": current or 0,
            "total": total,
            "percent": percent,
        }

    def _derive_docs_parse_progress(
        self, *, message: str, total_documents: int
    ) -> dict[str, Any]:
        total = max(1, total_documents)
        normalized = self._safe_text(message)
        current = 0
        stage = "prepare"
        label = "Preparing attached documents"
        detail = normalized or "Preparing attached documents."
        percent = 0.08

        parsing_match = re.search(
            r"Parsing document\s+(\d+)/(\d+):", normalized, re.IGNORECASE
        )
        if parsing_match:
            current = max(1, int(parsing_match.group(1)))
            total = max(1, int(parsing_match.group(2)))
            stage = "parse"
            label = f"Parsing document {current} of {total}"
            percent = min(0.72, 0.14 + ((current - 1) / total) * 0.46)
        elif (
            normalized.lower().startswith("parsing ")
            and "uploaded document" in normalized.lower()
        ):
            stage = "prepare"
            label = "Starting document parsing"
            percent = 0.1
        elif normalized.lower().startswith("escalating "):
            stage = "enhance"
            current = total
            label = "Running visual enrichment"
            percent = 0.84

        return {
            "kind": "docs_parse",
            "stage": stage,
            "label": label,
            "detail": detail,
            "current": current,
            "total": total,
            "percent": percent,
        }

    def _build_docs_parse_progress_event(
        self,
        *,
        request_record: dict[str, Any],
        status: str,
        message: str,
        docs_progress: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "task.progress",
            "request_id": self._safe_text(request_record.get("request_id")) or None,
            "session_id": self._safe_text(request_record.get("session_id")) or None,
            "channel": self._safe_text(request_record.get("channel")) or None,
            "source": self._safe_text(request_record.get("source")) or "user",
            "source_id": self._safe_text(request_record.get("source_id")) or None,
            "task_id": task_id,
            "route": "opus",
            "status": status,
            "message": message,
            "docs_progress": docs_progress,
        }

    def _build_tabular_parse_progress_event(
        self,
        *,
        request_record: dict[str, Any],
        status: str,
        message: str,
        tabular_progress: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "task.progress",
            "request_id": self._safe_text(request_record.get("request_id")) or None,
            "session_id": self._safe_text(request_record.get("session_id")) or None,
            "channel": self._safe_text(request_record.get("channel")) or None,
            "source": self._safe_text(request_record.get("source")) or "user",
            "source_id": self._safe_text(request_record.get("source_id")) or None,
            "task_id": task_id,
            "route": "opus",
            "status": status,
            "message": message,
            "tabular_progress": tabular_progress,
        }

    def _apply_tabular_parse_success(
        self,
        *,
        request_id: str,
        parse_result: dict[str, Any],
        artifact_ids: list[str],
    ) -> None:
        output = (
            parse_result.get("output")
            if isinstance(parse_result.get("output"), dict)
            else {}
        )
        bundle_id = self._safe_text(output.get("bundle_id")) or None
        parse_task_id = self._safe_text(parse_result.get("task_id")) or None
        by_artifact_id: dict[str, dict[str, Any]] = {}
        for workbook in (
            output.get("workbooks", [])
            if isinstance(output.get("workbooks"), list)
            else []
        ):
            if not isinstance(workbook, dict):
                continue
            aid = self._safe_text(workbook.get("artifact_id"))
            if aid:
                by_artifact_id[aid] = workbook

        for artifact_id in artifact_ids:
            wb = by_artifact_id.get(artifact_id)
            if wb is None:
                continue
            self._update_request_artifact_fields(
                request_id,
                artifact_id,
                ingest_state="parsed",
                parse_task_id=parse_task_id,
                parse_bundle_id=bundle_id,
                parsed_summary=wb,
                tabular_tools=[
                    "sheets_browse",
                    "sheets_schema",
                    "sheets_preview",
                    "sheets_query",
                    "sheets_export",
                    "sheets_export_sheet",
                    "sheets_create_workbook",
                    "sheets_create_sheet",
                ],
                parse_error=None,
            )
            self.artifact_store.update_ingest_state(
                artifact_id,
                ingest_state="parsed",
                parse_task_id=parse_task_id,
                parse_bundle_id=bundle_id,
                parsed_summary=wb,
            )

    def _apply_tabular_parse_failure(
        self,
        *,
        request_id: str,
        artifact_ids: list[str],
        error_text: str,
        ingest_state: str,
        task_id: str | None,
    ) -> None:
        for artifact_id in artifact_ids:
            self._update_request_artifact_fields(
                request_id,
                artifact_id,
                parse_error=error_text,
                ingest_state=ingest_state,
                parse_task_id=task_id,
            )
            self.artifact_store.update_ingest_state(
                artifact_id,
                ingest_state=ingest_state,
                parse_task_id=task_id,
            )

    async def _reconcile_tabular_parse_bundle(
        self,
        *,
        request_id: str,
        artifact_ids: list[str],
        task_id: str,
    ) -> None:
        try:
            parse_result = await self._wait_for_agent_terminal_result(
                task_id,
                timeout_sec=self.config.tabular_parse_reconcile_timeout_sec,
                poll_interval_sec=self.config.tabular_parse_poll_interval_sec,
            )
        except Exception:
            logger.exception(
                "gateway.tabular_autoparse_reconcile_failed request_id=%s task_id=%s",
                request_id,
                task_id,
            )
            return
        status = self._safe_text(parse_result.get("status")) or "failed"
        if status == "completed":
            self._apply_tabular_parse_success(
                request_id=request_id,
                parse_result=parse_result,
                artifact_ids=artifact_ids,
            )
            return
        if status == "pending":
            logger.warning(
                "gateway.tabular_autoparse_reconcile_still_pending request_id=%s task_id=%s",
                request_id,
                task_id,
            )
            return
        error_text = (
            self._safe_text(parse_result.get("error_message"))
            or "tabular.parse_bundle failed"
        )
        self._apply_tabular_parse_failure(
            request_id=request_id,
            artifact_ids=artifact_ids,
            error_text=error_text,
            ingest_state="parse_failed",
            task_id=task_id,
        )

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
            previous_bound_request_id = self.active_requests_by_task.get(task_id)
            self.active_requests_by_task[task_id] = request_id
            active_request = self.active_requests.get(request_id)
            task_bound_changed = previous_bound_request_id != request_id
            if active_request is not None:
                task_bound_changed = (
                    task_bound_changed or active_request.task_id != task_id
                )
                active_request.task_id = task_id
            if task_bound_changed:
                self._trace_request_event(
                    request_id=request_id,
                    session_id=session_id,
                    channel=self._safe_text(event.get("channel")),
                    route=self._safe_text(event.get("route")) or "opus",
                    event_type="task.bound",
                    stage="execution",
                    status="active",
                    title="Task bound to request",
                    detail=task_id,
                    task_id=task_id,
                )
        if event_type == "response.blocks.snapshot":
            event_channel = self._safe_text(event.get("channel")) or ""
            produced_artifacts = self._normalize_produced_artifact_list(
                event.get("produced_artifacts")
            )
            supporting_artifacts = self._normalize_produced_artifact_list(
                event.get("supporting_artifacts")
            )
            raw_response_blocks = (
                event.get("response_blocks")
                if isinstance(event.get("response_blocks"), list)
                else event.get("blocks")
                if isinstance(event.get("blocks"), list)
                else []
            )
            if produced_artifacts and request_id and session_id and event_channel:
                self._cache_artifact_list(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=None,
                    artifacts=produced_artifacts,
                )
            if supporting_artifacts and request_id and session_id and event_channel:
                self._cache_artifact_list(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=None,
                    artifacts=supporting_artifacts,
                )
            client_produced_artifacts = self._hydrate_artifact_list_for_client(
                produced_artifacts
            )
            client_supporting_artifacts = self._hydrate_artifact_list_for_client(
                supporting_artifacts
            )
            client_response_blocks = self._hydrate_response_blocks_for_client(
                raw_response_blocks,
                produced_artifacts=client_produced_artifacts,
                supporting_artifacts=client_supporting_artifacts,
            )
            if client_produced_artifacts:
                event["produced_artifacts"] = client_produced_artifacts
            if client_supporting_artifacts:
                event["supporting_artifacts"] = client_supporting_artifacts
            if client_response_blocks:
                event["response_blocks"] = client_response_blocks
                event["blocks"] = client_response_blocks
        elif event_type == "response.complete":
            event_channel = self._safe_text(event.get("channel")) or ""
            research_provenance = self._normalize_research_provenance(
                event.get("research_provenance"),
                fallback_sources=event.get("sources")
                if isinstance(event.get("sources"), list)
                else None,
            )
            produced_artifacts = self._normalize_produced_artifact_list(
                event.get("produced_artifacts")
            )
            supporting_artifacts = self._normalize_produced_artifact_list(
                event.get("supporting_artifacts")
            )
            if produced_artifacts and request_id and session_id and event_channel:
                self._cache_artifact_list(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=None,
                    artifacts=produced_artifacts,
                )
            if supporting_artifacts and request_id and session_id and event_channel:
                self._cache_artifact_list(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=None,
                    artifacts=supporting_artifacts,
                )
            response_blocks = (
                [dict(item) for item in event.get("response_blocks") if isinstance(item, dict)]
                if isinstance(event.get("response_blocks"), list)
                else self._build_stable_response_blocks(
                    content=self._safe_text(event.get("content")),
                    produced_artifacts=produced_artifacts,
                )
            )
            client_produced_artifacts = self._hydrate_artifact_list_for_client(
                produced_artifacts
            )
            client_supporting_artifacts = self._hydrate_artifact_list_for_client(
                supporting_artifacts
            )
            client_response_blocks = self._hydrate_response_blocks_for_client(
                response_blocks,
                produced_artifacts=client_produced_artifacts,
                supporting_artifacts=client_supporting_artifacts,
            )
            task_notebook = (
                self.session_store.get_task_notebook(task_id) if task_id else None
            )
            activity_log = self._normalize_activity_log(
                task_notebook.get("activity_log")
                if isinstance(task_notebook, dict)
                else None,
                limit=TASK_ACTIVITY_LOG_LIMIT,
            )
            alpha_terminal_log = (
                event.get("alpha_terminal_log")
                if isinstance(event.get("alpha_terminal_log"), list)
                else []
            )
            assistant_message_id = store_assistant_message(
                str(event.get("content") or ""),
                awaiting_reply=bool(event.get("awaiting_reply")),
                metadata={
                    "task_id": self._safe_text(event.get("task_id")),
                    "metrics": event.get("metrics"),
                    "thinking_text": self._safe_text(event.get("thinking_text")),
                    "source": self._safe_text(event.get("source")),
                    "source_id": self._safe_text(event.get("source_id")),
                    "research_provenance": research_provenance,
                    "sources": self._normalize_source_list(
                        event.get("sources")
                        if isinstance(event.get("sources"), list)
                        else None,
                        limit=8,
                    ),
                    "specialist_receipts": self._normalize_specialist_receipts(
                        event.get("specialist_receipts")
                    ),
                    "produced_artifacts": produced_artifacts,
                    "supporting_artifacts": supporting_artifacts,
                    "response_blocks": response_blocks,
                    "activity_log": activity_log,
                    "alpha_terminal_log": alpha_terminal_log,
                },
                channel=event_channel,
                route="opus",
            )
            if (
                assistant_message_id
                and produced_artifacts
                and request_id
                and session_id
                and event_channel
            ):
                self._cache_output_artifacts(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=assistant_message_id,
                    produced_artifacts=produced_artifacts,
                )
            if (
                assistant_message_id
                and supporting_artifacts
                and request_id
                and session_id
                and event_channel
            ):
                self._cache_artifact_list(
                    request_id=request_id,
                    session_id=session_id,
                    source_channel=event_channel,
                    source_message_id=assistant_message_id,
                    artifacts=supporting_artifacts,
                )
            if assistant_message_id:
                event["message_id"] = assistant_message_id
            if client_produced_artifacts:
                event["produced_artifacts"] = client_produced_artifacts
            if client_supporting_artifacts:
                event["supporting_artifacts"] = client_supporting_artifacts
            if client_response_blocks:
                event["response_blocks"] = client_response_blocks
            if activity_log:
                event["activity_log"] = activity_log
            if alpha_terminal_log:
                event["alpha_terminal_log"] = alpha_terminal_log
            event_channel_platform = self._channel_platform(event_channel)
            email_delivery = (
                self._effective_email_delivery(event)
                if event_channel_platform == "agent-email"
                else {}
            )
            email_delivery_status = self._safe_text(email_delivery.get("status"))
            email_queued_for_approval = (
                email_delivery_status == "queued_for_approval"
                or bool(email_delivery.get("queued_for_approval"))
                or self._safe_text(event.get("email_auto_reply_status"))
                == "queued_for_approval"
            )
            email_subject = (
                self._safe_text(event.get("thread_subject"))
                or self._safe_text(event.get("subject"))
                or "Draft reply is waiting for approval"
            )
            email_sender = (
                self._safe_text(event.get("from_name"))
                or self._safe_text(event.get("from_address"))
                or "Agent Email"
            )
            push_title = (
                "Email approval needed"
                if email_queued_for_approval
                else "Response ready"
                if event_channel_platform == "mobile"
                else f"{self._channel_display_name(event_channel)} response ready"
            )
            push_body = (
                f"{email_sender}: {email_subject}"
                if email_queued_for_approval
                else self._response_push_body(event.get("content"))
            )
            self._schedule_mobile_push(
                session_id=session_id,
                origin_channel=event_channel,
                event_type="response.complete",
                title=push_title,
                body=push_body,
                screen="agent-email" if email_queued_for_approval else "chat",
                priority="high" if email_queued_for_approval else "default",
                data={
                    "type": (
                        "agent_email.approval"
                        if email_queued_for_approval
                        else "response.complete"
                    ),
                    "request_id": request_id,
                    "message_id": assistant_message_id,
                    "origin_channel": event_channel,
                    "approval_id": self._safe_text(email_delivery.get("approval_id")),
                },
            )
            self._trace_request_event(
                request_id=request_id,
                session_id=session_id,
                channel=event_channel,
                route=self._safe_text(event.get("route")) or "opus",
                event_type="response.complete",
                stage="response",
                status="completed",
                title="Assistant response completed",
                detail=self._safe_text(event.get("content"))[:400] or None,
                task_id=task_id,
                specialist_receipts=self._normalize_specialist_receipts(
                    event.get("specialist_receipts")
                ),
                metadata={
                    "produced_artifact_count": len(produced_artifacts),
                    "supporting_artifact_count": len(supporting_artifacts),
                    "response_block_count": len(response_blocks),
                },
            )
            # Cross-channel sync: push the assistant response to connected realtime clients on other platforms.
            if event_channel and session_id:
                self._track_background_task(
                    self._broadcast_cross_channel_to_realtime_clients(
                        session_id,
                        message_id=assistant_message_id,
                        role="assistant",
                        content=str(event.get("content") or ""),
                        channel=event_channel,
                        route="opus",
                        sources=event.get("sources")
                        if isinstance(event.get("sources"), list)
                        else None,
                        thinking_text=self._safe_text(event.get("thinking_text")),
                        produced_artifacts=produced_artifacts,
                        supporting_artifacts=supporting_artifacts,
                        activity_log=activity_log,
                        alpha_terminal_log=alpha_terminal_log,
                        response_blocks=response_blocks,
                    )
                )
        elif event_type == "task.input_required":
            channel = self._safe_text(event.get("channel"))
            if channel and task_id and session_id:
                self._persist_task_input_request(event)
            self._schedule_mobile_push(
                session_id=session_id,
                origin_channel=channel,
                event_type="task.input_required",
                title="Input needed",
                body=(
                    self._safe_text(event.get("prompt"))
                    or self._safe_text(event.get("message"))
                    or "Cosmic needs your input to continue."
                ),
                screen="tasks",
                priority="high",
                data={
                    "type": "task.input_required",
                    "request_id": request_id,
                    "task_id": task_id,
                    "input_request_id": self._safe_text(event.get("input_request_id")),
                },
            )
        elif event_type in {"task.completed", "task.failed", "task.cancelled"}:
            if task_id:
                self.active_task_channels.pop(task_id, None)
                self.active_requests_by_task.pop(task_id, None)
            self._trace_request_event(
                request_id=request_id,
                session_id=session_id,
                channel=self._safe_text(event.get("channel")),
                route=self._safe_text(event.get("route")) or "opus",
                event_type=event_type,
                stage="terminal"
                if event_type in {"task.failed", "task.cancelled"}
                else "execution",
                status="failed"
                if event_type == "task.failed"
                else "cancelled"
                if event_type == "task.cancelled"
                else "completed",
                title="Task failed"
                if event_type == "task.failed"
                else "Task cancelled"
                if event_type == "task.cancelled"
                else "Task completed",
                detail=(
                    self._safe_text((event.get("error") or {}).get("message"))
                    if isinstance(event.get("error"), dict)
                    else None
                )
                or self._safe_text(event.get("message"))
                or None,
                task_id=task_id,
                completed=event_type in {"task.failed", "task.cancelled"},
            )
            push_title = (
                "Task failed"
                if event_type == "task.failed"
                else "Task cancelled"
                if event_type == "task.cancelled"
                else "Task completed"
            )
            self._schedule_mobile_push(
                session_id=session_id,
                origin_channel=self._safe_text(event.get("channel")),
                event_type=event_type,
                title=push_title,
                body=(
                    self._safe_text(event.get("message"))
                    or self._safe_text(event.get("content"))
                    or push_title
                ),
                screen="tasks",
                priority="default" if event_type == "task.completed" else "high",
                data={
                    "type": event_type,
                    "request_id": request_id,
                    "task_id": task_id,
                },
            )

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

    def _contains_document_attachments(self, attachments: Any) -> bool:
        if not isinstance(attachments, list):
            return False
        return any(
            isinstance(item, dict) and is_supported_document_artifact(item)
            for item in attachments
        )

    def _contains_image_attachments(self, attachments: Any) -> bool:
        if not isinstance(attachments, list):
            return False
        return any(
            isinstance(item, dict) and is_supported_image_artifact(item)
            for item in attachments
        )

    def _contains_opus_media_attachments(self, attachments: Any) -> bool:
        return self._contains_document_attachments(
            attachments
        ) or self._contains_image_attachments(attachments)

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

    def _normalize_entity_list(
        self, values: Any, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            entity_type = self._safe_text(item.get("type")) or "entity"
            entity_id = (
                self._safe_text(item.get("id"))
                or self._safe_text(item.get("path"))
                or self._safe_text(item.get("url"))
                or self._safe_text(item.get("label"))
            )
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

    def _normalize_source_list(
        self, values: Any, *, limit: int = 8
    ) -> list[dict[str, str]]:
        if not isinstance(values, list):
            return []
        normalized: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            url = self._safe_text(item.get("url"))
            if not url or url in seen_urls:
                continue
            normalized.append(
                {
                    "url": url,
                    "title": self._safe_text(item.get("title"))
                    or self._safe_text(item.get("domain"))
                    or url,
                    "domain": self._safe_text(item.get("domain")) or "",
                }
            )
            seen_urls.add(url)
            if len(normalized) >= limit:
                break
        return normalized

    def _normalize_research_provenance(
        self,
        value: Any,
        *,
        fallback_sources: Any = None,
    ) -> dict[str, Any] | None:
        paths: list[str] = []
        source_count: int | None = None
        source_domains: list[str] = []
        source_sample: list[dict[str, str]] = []
        if isinstance(value, dict):
            paths = self._normalize_string_list(value.get("paths"), limit=4)
            source_count = self._coerce_int(value.get("source_count"))
            source_domains = self._normalize_string_list(
                value.get("source_domains"), limit=3
            )
            source_sample = self._normalize_source_list(
                value.get("source_sample"), limit=3
            )

        if not source_sample:
            source_sample = self._normalize_source_list(fallback_sources, limit=3)
        if source_sample and not source_count:
            source_count = len(source_sample)
        if source_sample and not source_domains:
            source_domains = self._normalize_string_list(
                [
                    item.get("domain")
                    for item in source_sample
                    if isinstance(item, dict)
                ],
                limit=3,
            )

        if not paths and not source_sample and not source_count and not source_domains:
            return None

        provenance: dict[str, Any] = {}
        if paths:
            provenance["paths"] = paths
        if source_count:
            provenance["source_count"] = source_count
        if source_domains:
            provenance["source_domains"] = source_domains
        if source_sample:
            provenance["source_sample"] = source_sample
        return provenance

    def _normalize_produced_artifact_list(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            artifact_id = self._safe_text(item.get("artifact_id")) or ""
            logical_path = self._safe_text(item.get("path")) or ""
            dedupe_key = (artifact_id, logical_path)
            if not any(dedupe_key) or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            resolved_path = self._resolve_logical_artifact_path(logical_path)
            filename = (
                self._safe_text(item.get("filename"))
                or (resolved_path.name if resolved_path is not None else None)
                or (Path(logical_path).name if logical_path else None)
                or artifact_id
            )
            size_bytes: int | None = self._coerce_int(
                item.get("size_bytes") if item.get("size_bytes") is not None else item.get("sizeBytes")
            )
            if (
                resolved_path is not None
                and resolved_path.exists()
                and resolved_path.is_file()
            ):
                try:
                    size_bytes = int(resolved_path.stat().st_size)
                except OSError:
                    pass
            explicit_downloadable = item.get("downloadable")
            downloadable = (
                bool(explicit_downloadable)
                if explicit_downloadable is not None
                else bool(
                    resolved_path is not None
                    and resolved_path.exists()
                    and resolved_path.is_file()
                )
            )
            normalized.append(
                {
                    key: val
                    for key, val in {
                        "artifact_id": artifact_id,
                        "task_id": self._safe_text(item.get("task_id")),
                        "mime_type": self._safe_text(item.get("mime"))
                        or self._safe_text(item.get("mime_type")),
                        "filename": filename,
                        "size_bytes": size_bytes,
                        "kind": self._safe_text(item.get("kind")),
                        "created_by_agent": self._safe_text(
                            item.get("created_by_agent")
                        ),
                        "created_at": self._safe_text(item.get("created_at")),
                        "path": logical_path,
                        "sha256": self._safe_text(item.get("sha256")),
                        "caption": self._safe_text(item.get("caption")),
                        "audience": self._safe_text(item.get("audience")),
                        "source_url": self._safe_text(item.get("source_url")),
                        "source_title": self._safe_text(item.get("source_title")),
                        "source_domain": self._safe_text(item.get("source_domain")),
                        "source_image_url": self._safe_text(item.get("source_image_url")),
                        "width": self._coerce_int(item.get("width")),
                        "height": self._coerce_int(item.get("height")),
                        "downloadable": downloadable,
                    }.items()
                    if val not in (None, "", [], {})
                }
            )
            if len(normalized) >= 12:
                break
        return normalized

    def _hydrate_artifact_list_for_client(
        self, value: Any
    ) -> list[dict[str, Any]]:
        normalized = self._normalize_produced_artifact_list(value)
        hydrated: list[dict[str, Any]] = []
        for artifact in normalized:
            enriched = dict(artifact)
            if is_supported_image_artifact(enriched) and self._safe_text(
                enriched.get("path")
            ):
                preview_url = self.mint_artifact_access_url(
                    enriched, purpose="ui_preview"
                )
                if preview_url:
                    enriched["preview_url"] = preview_url
            hydrated.append(enriched)
        return hydrated

    def _hydrate_produced_artifact_list_for_client(
        self, value: Any
    ) -> list[dict[str, Any]]:
        return self._hydrate_artifact_list_for_client(value)

    def _hydrate_response_blocks_for_client(
        self,
        value: Any,
        *,
        produced_artifacts: list[dict[str, Any]] | None = None,
        supporting_artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        all_artifacts: list[dict[str, Any]] = []
        for artifact in (supporting_artifacts or []):
            if isinstance(artifact, dict):
                all_artifacts.append(artifact)
        for artifact in (produced_artifacts or []):
            if isinstance(artifact, dict):
                all_artifacts.append(artifact)
        artifact_by_id = {
            self._safe_text(item.get("artifact_id")): item
            for item in all_artifacts
            if isinstance(item, dict) and self._safe_text(item.get("artifact_id"))
        }
        artifact_by_name = {
            self._safe_text(item.get("filename")).lower(): item
            for item in all_artifacts
            if isinstance(item, dict) and self._safe_text(item.get("filename"))
        }
        hydrated: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            block_type = self._safe_text(item.get("type")) or "markdown"
            block: dict[str, Any] = dict(item)
            if block_type in {"image_artifact", "file_artifact"}:
                artifact = artifact_by_id.get(
                    self._safe_text(item.get("artifact_id"))
                ) or artifact_by_name.get(self._safe_text(item.get("filename")).lower())
                if artifact:
                    for key in (
                        "artifact_id",
                        "filename",
                        "mime_type",
                        "size_bytes",
                        "kind",
                        "downloadable",
                        "preview_url",
                        "caption",
                    ):
                        if artifact.get(key) not in (None, "", [], {}):
                            block[key] = artifact.get(key)
            hydrated.append(
                {
                    key: val
                    for key, val in block.items()
                    if val not in (None, "", [], {})
                }
            )
        return hydrated

    def _build_stable_response_blocks(
        self,
        *,
        content: str | None,
        produced_artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return build_response_blocks(content, produced_artifacts or [])

    def _build_client_response_blocks(
        self,
        *,
        content: str | None,
        produced_artifacts: list[dict[str, Any]] | None = None,
        supporting_artifacts: list[dict[str, Any]] | None = None,
        stored_blocks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        client_produced_artifacts = self._hydrate_artifact_list_for_client(
            produced_artifacts or []
        )
        client_supporting_artifacts = self._hydrate_artifact_list_for_client(
            supporting_artifacts or []
        )
        stable_blocks = (
            stored_blocks
            if isinstance(stored_blocks, list) and stored_blocks
            else self._build_stable_response_blocks(
                content=content,
                produced_artifacts=produced_artifacts,
            )
        )
        return self._hydrate_response_blocks_for_client(
            stable_blocks,
            produced_artifacts=client_produced_artifacts,
            supporting_artifacts=client_supporting_artifacts,
        )

    def get_desktop_preferences_snapshot(self) -> dict[str, Any]:
        try:
            visual_response_enhancement = (
                self.preference_store.get_visual_response_enhancement()
            )
        except Exception:
            logger.exception(
                "gateway.preference_snapshot_failed; using runtime fallback defaults"
            )
            visual_response_enhancement = {
                "enabled": True,
                "revision": 1,
                "updated_at": utcnow_iso(),
                "updated_source": "runtime_fallback",
                "updated_device_id": None,
            }
        return {
            "visual_response_enhancement": visual_response_enhancement
        }

    async def save_visual_response_enhancement_preference(
        self,
        *,
        enabled: bool,
        source: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "visual_response_enhancement": (
                self.preference_store.set_visual_response_enhancement(
                    enabled,
                    source=source,
                    device_id=device_id,
                )
            )
        }
        await self._broadcast_desktop_preferences_updated(snapshot)
        return snapshot

    async def _broadcast_desktop_preferences_updated(
        self, snapshot: dict[str, Any]
    ) -> None:
        adapter = self.registry.adapters.get("desktop")
        if not isinstance(adapter, DesktopAdapter):
            return
        payload = {
            "type": "preferences.updated",
            "preferences": snapshot,
        }
        for connection in await adapter.list_connections():
            channel = self._safe_text(connection.get("channel"))
            if not channel:
                continue
            try:
                await adapter.send(payload, channel=channel)
            except ChannelUnavailableError:
                continue

    async def get_desktop_codex_status(self) -> dict[str, Any]:
        settings = self.agent_auth_store.get_codex(include_secret=False)
        cli_status = await self._codex_cli_status()
        pending_login = self._codex_login_session_snapshot()
        effective_status = self._effective_codex_status(settings, cli_status, pending_login)
        updated = self.agent_auth_store.save_codex(
            status=effective_status["status"],
            login_required_reason=effective_status["login_required_reason"],
            last_cli_status=cli_status,
        )
        return self._redact_codex_status(
            {
                **updated,
                "cli": cli_status,
                "login_session": pending_login,
                "codex_home": str(self.config.alpha_codex_home),
            }
        )

    async def save_desktop_codex_config(
        self,
        *,
        auth_mode: str | None = None,
        api_key: str | None = None,
        preferred_model: str | None = None,
        approval_mode: str | None = None,
        vm_sync_enabled: bool | None = None,
    ) -> dict[str, Any]:
        current = self.agent_auth_store.get_codex(include_secret=False)
        normalized_auth_mode = (
            self._normalize_codex_auth_mode(auth_mode)
            if auth_mode is not None
            else self._normalize_codex_auth_mode(str(current.get("auth_mode") or "chatgpt"))
        )
        api_key_value = self._safe_text(api_key)
        next_status: str | None = None
        next_reason: str | None = None
        if auth_mode is not None:
            if normalized_auth_mode == "chatgpt":
                next_status = "login_required"
                next_reason = "chatgpt_login_required"
            elif current.get("has_api_key") or api_key_value:
                next_status = "stored"
                next_reason = "api_key_login_pending" if api_key_value else "api_key_relogin_required"
            else:
                next_status = "login_required"
                next_reason = "api_key_required"
        if api_key is not None:
            next_status = "stored" if api_key_value else "login_required"
            next_reason = "api_key_login_pending" if api_key_value else "api_key_required"
        settings = self.agent_auth_store.save_codex(
            auth_mode=normalized_auth_mode,
            api_key=api_key_value if api_key is not None else None,
            preferred_model=preferred_model,
            approval_mode=approval_mode,
            vm_sync_enabled=vm_sync_enabled,
            status=next_status,
            login_required_reason=next_reason,
        )

        login_api_key = api_key_value
        if (
            normalized_auth_mode == "api_key"
            and not login_api_key
            and (auth_mode is not None or api_key is not None)
            and settings.get("has_api_key")
        ):
            login_api_key = self._safe_text(
                self.agent_auth_store.get_codex(include_secret=True).get("api_key")
            )

        if normalized_auth_mode == "api_key" and login_api_key and settings.get("vm_sync_enabled", True):
            login_result = await self._codex_login_with_api_key(login_api_key)
            status = "authenticated" if login_result["ok"] else "relogin_required"
            reason = "" if login_result["ok"] else "api_key_login_failed"
            settings = self.agent_auth_store.save_codex(
                auth_mode="api_key",
                status=status,
                login_required_reason=reason,
                last_cli_status=login_result,
            )

        return await self.get_desktop_codex_status()

    async def start_desktop_codex_login(self) -> dict[str, Any]:
        self.agent_auth_store.save_codex(
            auth_mode="chatgpt",
            status="login_required",
            login_required_reason="chatgpt_device_auth_required",
        )
        binary = shutil.which("codex")
        if not binary:
            settings = self.agent_auth_store.save_codex(
                auth_mode="chatgpt",
                status="relogin_required",
                login_required_reason="codex_cli_missing",
                last_cli_status={"ok": False, "reason": "codex_cli_missing"},
            )
            return self._redact_codex_status(
                {
                    **settings,
                    "cli": {"available": False, "reason": "codex_cli_missing"},
                    "login_session": None,
                    "codex_home": str(self.config.alpha_codex_home),
                }
            )

        session = self._codex_login_session_snapshot()
        if session and session.get("state") == "running":
            return await self.get_desktop_codex_status()

        await self._stop_codex_login_session()
        self.config.alpha_codex_home.mkdir(parents=True, exist_ok=True)
        env = self._codex_env()
        process = await asyncio.create_subprocess_exec(
            binary,
            "login",
            "--device-auth",
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        session_id = f"codex_login_{uuid4().hex[:12]}"
        session_state: dict[str, Any] = {
            "session_id": session_id,
            "state": "running",
            "started_at": utcnow_iso(),
            "stdout": [],
            "stderr": [],
            "returncode": None,
        }
        session_state["stdout_task"] = asyncio.create_task(
            self._capture_codex_stream(process.stdout, session_state["stdout"]),
            name=f"{session_id}-stdout",
        )
        session_state["stderr_task"] = asyncio.create_task(
            self._capture_codex_stream(process.stderr, session_state["stderr"]),
            name=f"{session_id}-stderr",
        )
        session_state["watch_task"] = asyncio.create_task(
            self._watch_codex_login_process(process, session_state),
            name=f"{session_id}-watch",
        )
        session_state["process"] = process
        self._codex_login_session = session_state
        await asyncio.sleep(1.25)
        return await self.get_desktop_codex_status()

    async def logout_desktop_codex(self) -> dict[str, Any]:
        await self._stop_codex_login_session()
        logout_result = await self._run_codex_command(["logout"], timeout_sec=15.0)
        settings = self.agent_auth_store.clear_codex_api_key(
            status="logged_out",
            login_required_reason="user_logged_out",
            last_cli_status=logout_result,
        )
        cli_status = await self._codex_cli_status()
        return self._redact_codex_status(
            {
                **settings,
                "cli": cli_status,
                "login_session": None,
                "codex_home": str(self.config.alpha_codex_home),
            }
        )

    async def _codex_login_with_api_key(self, api_key: str) -> dict[str, Any]:
        return await self._run_codex_command(
            ["login", "--with-api-key"],
            stdin=(api_key.strip() + "\n"),
            timeout_sec=30.0,
        )

    async def _codex_cli_status(self) -> dict[str, Any]:
        result = await self._run_codex_command(["login", "status"], timeout_sec=10.0)
        output = "\n".join(
            item
            for item in (self._safe_text(result.get("stdout")), self._safe_text(result.get("stderr")))
            if item
        )
        authenticated = bool(result.get("ok")) and not re.search(
            r"not\s+logged\s+in|not\s+authenticated|no\s+credentials",
            output,
            re.IGNORECASE,
        )
        return {
            **result,
            "authenticated": authenticated,
            "codex_home": str(self.config.alpha_codex_home),
        }

    async def _run_codex_command(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        timeout_sec: float,
    ) -> dict[str, Any]:
        binary = shutil.which("codex")
        if not binary:
            return {
                "ok": False,
                "available": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "reason": "codex_cli_missing",
            }

        self.config.alpha_codex_home.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._codex_env(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(stdin.encode("utf-8") if stdin is not None else None),
                timeout=timeout_sec,
            )
            return {
                "ok": process.returncode == 0,
                "available": True,
                "returncode": process.returncode,
                "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
                "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
            }
        except TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return {
                "ok": False,
                "available": True,
                "returncode": process.returncode,
                "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
                "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
                "reason": "timeout",
            }

    def _codex_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.alpha_codex_home)
        env.setdefault("HOME", str(self.config.alpha_codex_home.parent))
        return env

    async def _capture_codex_stream(
        self,
        stream: asyncio.StreamReader | None,
        lines: list[str],
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
                del lines[:-30]

    async def _watch_codex_login_process(
        self,
        process: asyncio.subprocess.Process,
        session_state: dict[str, Any],
    ) -> None:
        returncode = await process.wait()
        session_state["returncode"] = returncode
        session_state["state"] = "completed" if returncode == 0 else "failed"
        session_state["completed_at"] = utcnow_iso()
        self.agent_auth_store.save_codex(
            auth_mode="chatgpt",
            status="authenticated" if returncode == 0 else "relogin_required",
            login_required_reason="" if returncode == 0 else "chatgpt_login_failed",
            last_cli_status=self._codex_login_session_snapshot() or {},
        )

    def _codex_login_session_snapshot(self) -> dict[str, Any] | None:
        session = self._codex_login_session
        if not session:
            return None
        process = session.get("process")
        returncode = process.returncode if process is not None else session.get("returncode")
        state = session.get("state")
        if returncode is not None and state == "running":
            state = "completed" if returncode == 0 else "failed"
        return {
            "session_id": session.get("session_id"),
            "state": state,
            "started_at": session.get("started_at"),
            "completed_at": session.get("completed_at"),
            "returncode": returncode,
            "stdout": list(session.get("stdout") or []),
            "stderr": list(session.get("stderr") or []),
        }

    async def _stop_codex_login_session(self) -> None:
        session = self._codex_login_session
        if not session:
            return
        process = session.get("process")
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        tasks = [
            task
            for task in (
                session.get("stdout_task"),
                session.get("stderr_task"),
                session.get("watch_task"),
            )
            if isinstance(task, asyncio.Task)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._codex_login_session = None

    def _effective_codex_status(
        self,
        settings: dict[str, Any],
        cli_status: dict[str, Any],
        pending_login: dict[str, Any] | None,
    ) -> dict[str, str]:
        if pending_login and pending_login.get("state") == "running":
            return {"status": "login_pending", "login_required_reason": "chatgpt_login_pending"}
        if cli_status.get("authenticated"):
            return {"status": "authenticated", "login_required_reason": ""}
        if not cli_status.get("available", True):
            return {"status": "relogin_required", "login_required_reason": "codex_cli_missing"}
        if settings.get("auth_mode") == "api_key" and settings.get("has_api_key"):
            return {"status": "relogin_required", "login_required_reason": "api_key_relogin_required"}
        if settings.get("auth_mode") == "chatgpt":
            return {"status": "login_required", "login_required_reason": "chatgpt_login_required"}
        return {"status": "not_configured", "login_required_reason": "auth_not_configured"}

    def _redact_codex_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.pop("api_key", None)
        for key in ("cli", "last_cli_status"):
            cli = payload.get(key)
            if isinstance(cli, dict):
                stdout = self._safe_text(cli.get("stdout"))
                stderr = self._safe_text(cli.get("stderr"))
                cli["stdout"] = self._redact_secret_text(stdout)
                cli["stderr"] = self._redact_secret_text(stderr)
        login_session = payload.get("login_session")
        if isinstance(login_session, dict):
            for stream_name in ("stdout", "stderr"):
                lines = login_session.get(stream_name)
                if isinstance(lines, list):
                    login_session[stream_name] = [
                        self._redact_secret_text(self._safe_text(line)) for line in lines
                    ]
        return payload

    def _redact_secret_text(self, value: str) -> str:
        if not value:
            return ""
        return re.sub(r"sk-[^\s\"']{4,}", "sk-...redacted", value)

    def _normalize_codex_auth_mode(self, value: str | None) -> str:
        normalized = self._safe_text(value)
        return normalized if normalized in {"chatgpt", "api_key"} else "chatgpt"

    def _hydrate_message_metadata_for_client(self, metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        hydrated = dict(metadata)
        produced_artifacts = self._hydrate_produced_artifact_list_for_client(
            metadata.get("produced_artifacts")
        )
        if produced_artifacts:
            hydrated["produced_artifacts"] = produced_artifacts
        supporting_artifacts = self._hydrate_artifact_list_for_client(
            metadata.get("supporting_artifacts")
        )
        if supporting_artifacts:
            hydrated["supporting_artifacts"] = supporting_artifacts
        response_blocks = self._build_client_response_blocks(
            content=None,
            produced_artifacts=self._normalize_produced_artifact_list(
                metadata.get("produced_artifacts")
            ),
            supporting_artifacts=self._normalize_produced_artifact_list(
                metadata.get("supporting_artifacts")
            ),
            stored_blocks=metadata.get("response_blocks")
            if isinstance(metadata.get("response_blocks"), list)
            else None,
        )
        if response_blocks:
            hydrated["response_blocks"] = response_blocks
        return hydrated

    def _hydrate_history_message_for_client(
        self, message: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(message, dict):
            return message
        hydrated = dict(message)
        metadata = (
            message.get("metadata")
            if isinstance(message.get("metadata"), dict)
            else None
        )
        if metadata is not None:
            hydrated_metadata = self._hydrate_message_metadata_for_client(metadata)
            if not isinstance(hydrated_metadata.get("response_blocks"), list):
                produced_artifacts = self._normalize_produced_artifact_list(
                    metadata.get("produced_artifacts")
                )
                supporting_artifacts = self._normalize_produced_artifact_list(
                    metadata.get("supporting_artifacts")
                )
                response_blocks = self._build_client_response_blocks(
                    content=self._safe_text(message.get("content")),
                    produced_artifacts=produced_artifacts,
                    supporting_artifacts=supporting_artifacts,
                    stored_blocks=metadata.get("response_blocks")
                    if isinstance(metadata.get("response_blocks"), list)
                    else None,
                )
                if response_blocks:
                    hydrated_metadata["response_blocks"] = response_blocks
            hydrated["metadata"] = hydrated_metadata
        return hydrated

    def _build_recalled_artifact_record(
        self,
        *,
        session_id: str,
        turn_entry: dict[str, Any],
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        record = dict(artifact)
        record["session_id"] = session_id
        record["turn_id"] = self._safe_text(turn_entry.get("turn_id"))
        record["request_id"] = self._safe_text(turn_entry.get("request_id"))
        record["assistant_message_id"] = self._safe_text(
            turn_entry.get("assistant_message_id")
        )
        record["route"] = self._safe_text(turn_entry.get("route"))
        record["user_goal"] = self._safe_text(turn_entry.get("user_goal"))
        record["assistant_outcome"] = self._safe_text(
            turn_entry.get("assistant_outcome")
        )
        return {
            key: value
            for key, value in record.items()
            if value not in (None, "", [], {})
        }

    def _score_recalled_artifact(
        self,
        record: dict[str, Any],
        *,
        query: str | None,
        query_tokens: list[str],
    ) -> int:
        if not query:
            return 1
        filename = (self._safe_text(record.get("filename")) or "").lower()
        artifact_id = (self._safe_text(record.get("artifact_id")) or "").lower()
        user_goal = (self._safe_text(record.get("user_goal")) or "").lower()
        assistant_outcome = (
            self._safe_text(record.get("assistant_outcome")) or ""
        ).lower()
        created_by_agent = (
            self._safe_text(record.get("created_by_agent")) or ""
        ).lower()
        route = (self._safe_text(record.get("route")) or "").lower()
        normalized_query = query.lower()
        score = 0
        if normalized_query in filename:
            score += 20
        if normalized_query in artifact_id:
            score += 18
        if normalized_query in user_goal:
            score += 12
        if normalized_query in assistant_outcome:
            score += 10
        search_blob = " ".join(
            [
                filename,
                artifact_id,
                user_goal,
                assistant_outcome,
                created_by_agent,
                route,
            ]
        )
        for token in query_tokens:
            if token in filename:
                score += 6
            if token in artifact_id:
                score += 5
            if token in user_goal:
                score += 3
            if token in assistant_outcome:
                score += 2
            if token in created_by_agent:
                score += 2
            if token in route:
                score += 1
        if query_tokens and not any(token in search_blob for token in query_tokens):
            return 0
        return score

    @staticmethod
    def _research_path_label(path: str) -> str:
        labels = {
            "native_web_search": "native web_search",
            "native_web_fetch": "native web_fetch",
            "perplexity_research": "perplexity_research",
            "firecrawl": "firecrawl",
            "x_search_specialist": "x_search_specialist",
        }
        normalized = str(path or "").strip()
        return labels.get(normalized, normalized)

    def _research_tool_summary_labels(
        self, provenance: dict[str, Any] | None
    ) -> list[str]:
        if not isinstance(provenance, dict):
            return []
        return self._normalize_string_list(provenance.get("paths"), limit=4)

    def _build_recent_research_receipt(
        self, turn: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not isinstance(turn, dict):
            return None
        metadata = (
            turn.get("metadata") if isinstance(turn.get("metadata"), dict) else {}
        )
        provenance = self._normalize_research_provenance(
            metadata.get("research_provenance")
        )
        if not provenance:
            return None
        receipt: dict[str, Any] = {
            "request_id": self._safe_text(turn.get("request_id")),
            "route": self._safe_text(turn.get("route")) or "opus",
            "question": self._bounded_excerpt(
                turn.get("user_message_excerpt"), limit=96
            ),
            "completed_at": self._safe_text(turn.get("completed_at")),
        }
        if isinstance(provenance.get("paths"), list):
            receipt["paths"] = provenance.get("paths")
        if self._coerce_int(provenance.get("source_count")):
            receipt["source_count"] = self._coerce_int(provenance.get("source_count"))
        if isinstance(provenance.get("source_domains"), list):
            receipt["source_domains"] = provenance.get("source_domains")
        if isinstance(provenance.get("source_sample"), list):
            receipt["source_sample"] = provenance.get("source_sample")
        return receipt

    def _normalize_specialist_receipts(
        self, value: Any, *, limit: int = 4
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            intent_name = self._safe_text(item.get("intent"))
            agent_id = self._safe_text(item.get("agent_id"))
            activity = self._safe_text(item.get("activity"))
            if not intent_name and not agent_id:
                continue
            dedupe = f"{intent_name or ''}|{agent_id or ''}|{activity or ''}".casefold()
            if dedupe in seen:
                continue
            receipt: dict[str, Any] = {}
            for key in ("tool_name", "intent", "agent_id", "agent_label", "activity"):
                text = self._safe_text(item.get(key))
                if text:
                    receipt[key] = text
            for key in ("bundle_id", "artifact_id", "filename", "parse_status"):
                text = self._safe_text(item.get(key))
                if text:
                    receipt[key] = text
            source_count = self._coerce_int(item.get("source_count"))
            artifact_count = self._coerce_int(item.get("artifact_count"))
            sheet_count = self._coerce_int(item.get("sheet_count"))
            if source_count:
                receipt["source_count"] = source_count
            if artifact_count:
                receipt["artifact_count"] = artifact_count
            if sheet_count is not None:
                receipt["sheet_count"] = sheet_count
            source_domains = self._normalize_string_list(
                item.get("source_domains"), limit=3
            )
            if source_domains:
                receipt["source_domains"] = source_domains
            source_sample = self._normalize_source_list(
                item.get("source_sample"), limit=2
            )
            if source_sample:
                receipt["source_sample"] = source_sample
            if receipt:
                normalized.append(receipt)
                seen.add(dedupe)
            if len(normalized) >= limit:
                break
        return normalized

    def _build_recent_specialist_receipts(
        self, turn: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not isinstance(turn, dict):
            return []
        metadata = (
            turn.get("metadata") if isinstance(turn.get("metadata"), dict) else {}
        )
        stored = self._normalize_specialist_receipts(
            metadata.get("specialist_receipts")
        )
        if not stored:
            return []
        rendered: list[dict[str, Any]] = []
        question = self._safe_text(turn.get("user_message_excerpt"))
        completed_at = self._safe_text(turn.get("completed_at"))
        request_id = self._safe_text(turn.get("request_id"))
        route = self._safe_text(turn.get("route")) or "opus"
        for item in stored:
            receipt = dict(item)
            if request_id:
                receipt["request_id"] = request_id
            if route:
                receipt["route"] = route
            if question:
                receipt["question"] = self._bounded_excerpt(question, limit=96)
            if completed_at:
                receipt["completed_at"] = completed_at
            rendered.append(receipt)
        return rendered

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
            self._trace_request_event(
                request_id=state.request_id,
                session_id=state.session_id,
                channel=state.channel,
                route=state.route,
                event_type="request.fulfillment_started",
                stage="fulfillment",
                status="active",
                title="Gateway fulfillment started",
                task_id=state.task_id,
                source=self._safe_text(request_record.get("source")) or None,
                source_id=self._safe_text(request_record.get("source_id")) or None,
                user_query_excerpt=state.user_query_excerpt or None,
            )
            await self.fulfill_processed_message(request_record)
        except asyncio.CancelledError:
            if state.cancel_requested:
                self._trace_request_event(
                    request_id=state.request_id,
                    session_id=state.session_id,
                    channel=state.channel,
                    route=state.route,
                    event_type="request.cancel_requested",
                    stage="terminal",
                    status="cancelled",
                    title="Request cancelled",
                    detail="The active response was stopped.",
                    task_id=state.task_id,
                    completed=True,
                )
                await self._emit_cancelled_event(state)
                return
            raise
        except Exception as exc:
            state.completed = True
            state.failed = True
            state.error_message = str(exc)
            state.activity = str(exc)
            self._trace_request_event(
                request_id=state.request_id,
                session_id=state.session_id,
                channel=state.channel,
                route=state.route,
                event_type="request.error",
                stage="terminal",
                status="failed",
                title="Gateway fulfillment failed",
                detail=str(exc),
                task_id=state.task_id,
                completed=True,
            )
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
                interrupted_metadata: dict[str, Any] = {
                    "request_id": state.request_id,
                    "thinking_text": state.partial_thinking or None,
                    "interrupted": True,
                }
                if state.supporting_artifacts:
                    interrupted_metadata["supporting_artifacts"] = list(
                        state.supporting_artifacts
                    )
                if state.response_blocks_snapshot:
                    interrupted_metadata["response_blocks"] = list(
                        state.response_blocks_snapshot
                    )
                if not state.foreground:
                    interrupted_metadata["background"] = True
                self._append_session_message(
                    state.session_id,
                    role="assistant",
                    content=state.partial_content,
                    route=state.route,
                    channel=state.channel,
                    metadata=interrupted_metadata,
                    in_reply_to_request_id=state.request_id,
                )
            elif state.failed:
                if not self._persist_failed_foreground_response(state):
                    self._cache_recent_foreground_terminal_stream(state)
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
        state.worker = None  # release the asyncio.Task reference
        current = self.active_requests.get(state.request_id)
        if current is state:
            self.active_requests.pop(state.request_id, None)
        self.request_records.pop(state.request_id, None)
        if state.task_id:
            bound_request_id = self.active_requests_by_task.get(state.task_id)
            if bound_request_id == state.request_id:
                self.active_requests_by_task.pop(state.task_id, None)

    def _track_partial_stream(
        self, state: ActiveRequest, event: dict[str, Any]
    ) -> None:
        event_type = self._safe_text(event.get("type")) or ""
        if event_type == "task.progress":
            terminal_entry = self._normalize_alpha_terminal_entry(
                event.get("codex_terminal"),
                task_id=self._safe_text(event.get("task_id")),
            )
            if terminal_entry:
                state.alpha_terminal_log = [
                    *state.alpha_terminal_log,
                    terminal_entry,
                ][-120:]
                return
            progress_state = event.get("tabular_progress") or event.get("docs_progress")
            progress_label = (
                self._safe_text(progress_state.get("label"))
                if isinstance(progress_state, dict)
                else None
            )
            state.activity = progress_label or self._safe_text(event.get("message")) or state.activity
            activity_entry = self._build_task_activity_entry(event)
            if activity_entry:
                state.activity_log = self._normalize_activity_log(
                    [*state.activity_log, activity_entry],
                    limit=TASK_ACTIVITY_LOG_LIMIT,
                )
            return
        if event_type == "response.thinking.chunk":
            state.partial_thinking += str(event.get("content") or "")
            return
        if event_type == "response.chunk":
            state.partial_content += str(event.get("content") or "")
            return
        if event_type == "response.blocks.snapshot":
            snapshot_seq = 0
            try:
                snapshot_seq = int(event.get("snapshot_seq") or 0)
            except (TypeError, ValueError):
                snapshot_seq = 0
            if snapshot_seq > 0 and snapshot_seq < state.snapshot_seq:
                return
            state.snapshot_seq = max(state.snapshot_seq, snapshot_seq)
            raw_blocks = (
                event.get("response_blocks")
                if isinstance(event.get("response_blocks"), list)
                else event.get("blocks")
                if isinstance(event.get("blocks"), list)
                else []
            )
            state.response_blocks_snapshot = [
                dict(item) for item in raw_blocks if isinstance(item, dict)
            ]
            raw_supporting = (
                event.get("supporting_artifacts")
                if isinstance(event.get("supporting_artifacts"), list)
                else []
            )
            state.supporting_artifacts = [
                dict(item) for item in raw_supporting if isinstance(item, dict)
            ]
            return
        if event_type == "response.complete":
            state.completed = True
            state.partial_content = str(event.get("content") or state.partial_content)
            if state.alpha_terminal_log and not isinstance(event.get("alpha_terminal_log"), list):
                event["alpha_terminal_log"] = [dict(item) for item in state.alpha_terminal_log]
            thinking_text = self._safe_text(event.get("thinking_text"))
            if thinking_text is not None:
                state.partial_thinking = thinking_text
            response_blocks = (
                event.get("response_blocks")
                if isinstance(event.get("response_blocks"), list)
                else []
            )
            if response_blocks:
                state.response_blocks_snapshot = [
                    dict(item) for item in response_blocks if isinstance(item, dict)
                ]
            supporting_artifacts = (
                event.get("supporting_artifacts")
                if isinstance(event.get("supporting_artifacts"), list)
                else []
            )
            if supporting_artifacts:
                state.supporting_artifacts = [
                    dict(item)
                    for item in supporting_artifacts
                    if isinstance(item, dict)
                ]
            return
        if event_type == "task.failed":
            state.completed = True
            state.failed = True
            error_message = (
                self._safe_text((event.get("error") or {}).get("message"))
                if isinstance(event.get("error"), dict)
                else None
            ) or self._safe_text(event.get("message")) or "Opus task failed."
            state.error_message = error_message
            state.activity = error_message or state.activity

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

        snapshot = (
            payload if isinstance(payload, dict) else {"enabled": True, "status": "ok"}
        )
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

    async def _finalize_rollover_sessions(
        self, current_session_id: str | None = None
    ) -> None:
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

    async def _finalize_single_rollover_session(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any] | None:
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

        transcript_markdown = self._render_session_transcript_markdown(
            candidate, history
        )
        transcript_path = self._write_session_transcript(
            session_id, transcript_markdown
        )

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
            logger.exception(
                "gateway.session_rollover_summary_failed session_id=%s", session_id
            )
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
                                original_kind=self._safe_text(
                                    summary_payload.get("kind")
                                ),
                                normalized_kind=self._safe_text(
                                    summary_payload.get("kind")
                                ),
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

    def _write_session_transcript(
        self, session_id: str, transcript_markdown: str
    ) -> str:
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
        metrics = (
            event.get("metrics") if isinstance(event.get("metrics"), dict) else None
        )
        input_artifacts = (
            user_message.get("metadata", {}).get("input_artifacts")
            if isinstance(user_message.get("metadata"), dict)
            else None
        )

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
                self._safe_text(channel.split(":", 1)[0] if channel else None)
                or "unknown_channel",
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

        title = self._safe_text(payload.get("title")) or self._derive_memory_title(
            content
        )
        tags = self._normalize_string_list(payload.get("tags"), limit=24)
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        provenance = (
            dict(payload.get("provenance"))
            if isinstance(payload.get("provenance"), dict)
            else {}
        )
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
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        provenance = (
            dict(payload.get("provenance"))
            if isinstance(payload.get("provenance"), dict)
            else {}
        )
        confirmation_status = self._safe_text(
            payload.get("confirmation_status")
        ) or self._safe_text(metadata.get("confirmation_status"))
        created_in_session_id = self._safe_text(
            payload.get("created_in_session_id")
        ) or self._safe_text(metadata.get("created_in_session_id"))
        created_by_tool = self._safe_text(
            payload.get("created_by_tool")
        ) or self._safe_text(metadata.get("created_by_tool"))
        contested_reason = self._safe_text(
            payload.get("contested_reason")
        ) or self._safe_text(metadata.get("contested_reason"))
        contested_at = self._safe_text(payload.get("contested_at")) or self._safe_text(
            metadata.get("contested_at")
        )
        derived_from_assistant_inference = self._coerce_bool(
            payload.get("derived_from_assistant_inference"),
            self._coerce_bool(metadata.get("derived_from_assistant_inference"), False),
        )
        if confirmation_status:
            metadata["confirmation_status"] = confirmation_status
        else:
            metadata.setdefault("confirmation_status", "confirmed")
        metadata.setdefault(
            "created_in_session_id",
            created_in_session_id
            or self._safe_text(provenance.get("session_id"))
            or self._safe_text(metadata.get("session_id")),
        )
        metadata.setdefault(
            "created_by_tool", created_by_tool or "memory_write_core_fact"
        )
        metadata["derived_from_assistant_inference"] = derived_from_assistant_inference
        if contested_reason:
            metadata["contested_reason"] = contested_reason
        if contested_at:
            metadata["contested_at"] = contested_at

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
            raise MemoryClientHTTPError(
                status_code=400, message="observations are required"
            )
        provenance = (
            dict(payload.get("provenance"))
            if isinstance(payload.get("provenance"), dict)
            else {}
        )
        if not provenance:
            raise MemoryClientHTTPError(
                status_code=400, message="provenance is required"
            )

        normalized_payload = dict(payload)
        normalized_payload["kind"] = (
            self._safe_text(payload.get("kind")) or "transcript"
        )
        normalized_payload["title"] = (
            self._safe_text(payload.get("title")) or "Transcript episode"
        )
        normalized_payload["tags"] = self._normalize_string_list(
            payload.get("tags"), limit=24
        )
        normalized_payload["metadata"] = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        normalized_payload["provenance"] = provenance

        audit_event = self._build_memory_write_audit_event(
            payload=normalized_payload,
            operation="memory_ingest_episode",
            write_source=write_source,
            original_kind=self._safe_text(normalized_payload.get("kind"))
            or "transcript",
            normalized_kind=self._safe_text(normalized_payload.get("kind"))
            or "transcript",
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
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        provenance = (
            dict(payload.get("provenance"))
            if isinstance(payload.get("provenance"), dict)
            else {}
        )
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
        canonical_key = self._safe_text(
            payload.get("canonical_key")
        ) or self._safe_text(metadata.get("canonical_key"))
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
        if (
            audit_event.guard_applied
            and resolved_writer_id
            and audit_event.content_hash
        ):
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
                deduplicated=bool(
                    self._coerce_bool(
                        response.get("deduplicated")
                        if isinstance(response, dict)
                        else False,
                        False,
                    )
                ),
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
            deduplicated=bool(
                self._coerce_bool(
                    response.get("deduplicated")
                    if isinstance(response, dict)
                    else False,
                    False,
                )
            ),
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
                logger.exception(
                    "gateway.memory_write_rate_limit_redis_failed writer_id=%s",
                    writer_id,
                )

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
                logger.exception(
                    "gateway.memory_write_dedup_redis_failed writer_id=%s", writer_id
                )
            else:
                if memory_id:
                    return {
                        "memory_id": memory_id,
                        "response": {
                            "memory_id": memory_id,
                            "indexed": True,
                            "deduplicated": True,
                        },
                    }

        return self.memory_write_audit_store.find_recent_duplicate(
            writer_id=writer_id,
            content_hash=content_hash,
            since_created_at=self._iso_seconds_ago(
                self.config.memory_write_dedup_ttl_sec
            ),
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
            logger.exception(
                "gateway.memory_write_dedup_redis_store_failed writer_id=%s", writer_id
            )

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
                observation_content = self._bounded_excerpt(
                    item.get("content"), limit=160
                )
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
                content = json.dumps(
                    observations, ensure_ascii=False, sort_keys=True, default=str
                )
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
        return (
            (datetime.now(timezone.utc) - timedelta(seconds=max(1, seconds)))
            .isoformat()
            .replace("+00:00", "Z")
        )

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

    def _summarize_memory_write_response(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
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
