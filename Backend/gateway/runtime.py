from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import time
from typing import Any
from uuid import uuid4

from .adapters import HaikuAdapter, PerplexityAdapter
from .channels.desktop import DesktopAdapter
from .channels.registry import ChannelAdapterRegistry
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .orchestrator_client import OrchestratorClient
from .router_client import ModelRouterClient
from .routing_audit_store import RoutingAuditStore
from .session_store import SessionStore
from shared import SOURCE_PRIORITY_MAP, TaskEnvelope, generate_task_id, sign_task_envelope, utcnow


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
        self.started = False
        self.adapter_errors: dict[str, str] = {}
        self.active_task_channels: dict[str, str] = {}
        self.request_records: dict[str, dict[str, Any]] = {}
        self.pending_input_requests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.active_requests: dict[str, ActiveRequest] = {}
        self.active_requests_by_task: dict[str, str] = {}

    async def start(self) -> None:
        self.session_store.initialize()
        self.routing_audit_store.initialize()
        await self.model_router.start()
        await self.orchestrator.start()
        await self._register_adapters()
        self.started = True

    async def stop(self) -> None:
        workers = [state.worker for state in self.active_requests.values() if state.worker is not None]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.active_requests.clear()
        self.active_requests_by_task.clear()
        await self.registry.stop_all()
        await self.model_router.stop()
        await self.orchestrator.stop()
        await self.haiku_adapter.close()
        await self.perplexity_adapter.close()
        self.started = False

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
        assembled_conversation_context = self._build_conversation_context(
            session_id,
            fallback_context=conversation_context,
        )

        decision_started_at = time.perf_counter()
        routing_decision = await self._classify_message(
            session_id=session_id,
            content=content,
            metadata=metadata,
            channel=channel,
            conversation_context=assembled_conversation_context,
            route_override=route_override,
        )
        decision_latency_ms = (time.perf_counter() - decision_started_at) * 1000.0
        classification = routing_decision.classification
        dispatch_target = "orchestrator" if classification["route"] == "opus" else "gateway"

        self._append_session_message(
            session_id,
            role="user",
            content=content or "[non-text inbound message]",
            channel=channel,
            metadata={
                "request_id": request_id,
                "platform": metadata.get("platform"),
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
            "routing_decision_source": routing_decision.decision_source,
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
        active_request = self.active_requests.get(request_id)

        async def send(event: dict[str, Any]) -> None:
            if active_request is not None:
                self._track_partial_stream(active_request, event)
            await channel_adapter.send(event, channel=channel)

        def store_assistant_message(
            content: str,
            *,
            awaiting_reply: bool,
            metadata: dict[str, Any] | None,
            channel: str,
            route: str,
        ) -> None:
            self._append_session_message(
                session_id,
                role="assistant",
                content=content,
                route=route,
                awaiting_reply=awaiting_reply,
                channel=channel,
                metadata=metadata,
            )

        if route in {"haiku", "gemini"}:
            await self.haiku_adapter.stream(
                request_id=request_id,
                session_id=session_id,
                history=history,
                send=send,
                store_assistant_message=store_assistant_message,
                channel=channel,
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
        current_session_id = self.session_store.current_session_id()
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
        pending_inputs = list(self.pending_input_requests.get(channel, []))
        active_tasks = await self._active_task_summaries(session_id=session_id, channel=channel)
        return {
            "type": "resume.ok",
            "request_id": request_id,
            "session_id": session_id,
            "channel": channel,
            "history_tail": history_tail,
            "active_tasks": active_tasks,
            "pending_inputs": pending_inputs,
        }

    async def deliver_channel_event(self, event: dict[str, Any]) -> None:
        channel = self._safe_text(event.get("channel"))
        adapter = self.registry.get_adapter(channel)
        if adapter is None:
            raise ValueError(f"No adapter registered for channel: {channel!r}")
        await adapter.send(event, channel=channel)

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

    async def health_payload(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.started else "starting",
            "model_router_url": self.config.model_router_url,
            "orchestrator_url": self.config.orchestrator_url,
            "channels": self.list_channels(),
            "current_session_id": self.session_store.current_session_id(),
        }

    async def readiness_payload(self) -> dict[str, Any]:
        healthy_channels = [item for item in self.list_channels() if item["healthy"]]
        return {
            "status": "ready" if self.started else "starting",
            "gateway_started": self.started,
            "healthy_channel_count": len(healthy_channels),
            "adapter_errors": self.adapter_errors,
            "orchestrator_url": self.config.orchestrator_url,
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
            },
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
            if channel:
                self.pending_input_requests[channel].append(event)
        elif event_type in {"task.completed", "task.failed", "task.cancelled"}:
            if task_id:
                self.active_task_channels.pop(task_id, None)
                self.active_requests_by_task.pop(task_id, None)

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

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

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
            adapter = self.registry.get_adapter(state.channel)
            if adapter is not None:
                await adapter.send(
                    {
                        "type": "error",
                        "request_id": state.request_id,
                        "session_id": state.session_id,
                        "task_id": state.task_id,
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
        adapter = self.registry.get_adapter(state.channel)
        if adapter is None:
            return
        await adapter.send(
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
