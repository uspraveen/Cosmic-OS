from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import uuid4

from .adapters import GeminiAdapter, PerplexityAdapter
from .channels.desktop import DesktopAdapter
from .channels.registry import ChannelAdapterRegistry
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .router_client import ModelRouterClient
from .session_store import SessionStore


class GatewayRuntime:
    """Single-process Gateway runtime for channel ingress and control-plane routes."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.registry = ChannelAdapterRegistry()
        self.model_router = ModelRouterClient(
            base_url=config.model_router_url,
            timeout_sec=config.model_router_timeout_sec,
        )
        self.session_store = SessionStore(config.sessions_db_path)
        self.gemini_adapter = GeminiAdapter(
            api_key=config.gemini_api_key,
            model=config.gemini_model,
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

    async def start(self) -> None:
        self.session_store.initialize()
        await self.model_router.start()
        await self._register_adapters()
        self.started = True

    async def stop(self) -> None:
        await self.registry.stop_all()
        await self.model_router.stop()
        await self.gemini_adapter.close()
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

        classification = await self._classify_message(
            session_id=session_id,
            content=content,
            metadata=metadata,
            channel=channel,
            fallback_context=conversation_context,
        )
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
        }
        self.request_records[request_id] = result
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

        async def send(event: dict[str, Any]) -> None:
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

        if route == "gemini":
            await self.gemini_adapter.stream(
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

        await send(
            {
                "type": "error",
                "request_id": request_id,
                "code": "OPUS_UNAVAILABLE",
                "message": "Opus/orchestrator dispatch is not implemented in this backend build yet.",
            }
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.session_store.list_sessions()

    def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.session_store.get_history(session_id)

    async def _classify_message(
        self,
        *,
        session_id: str,
        content: str,
        metadata: dict[str, Any],
        channel: str,
        fallback_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sticky_message = self.session_store.get_last_awaiting_reply(session_id, channel)
        if sticky_message:
            self.session_store.clear_awaiting_reply(sticky_message["message_id"])
            return {
                "route": self._safe_text(sticky_message.get("route")) or "opus",
                "needs_latest": False,
                "needs_citations": False,
                "is_task": False,
                "is_continuation": True,
                "confidence": 1.0,
                "signals": ["awaiting_reply"],
            }

        attachments = metadata.get("attachments")
        if (not content or content.startswith("[")) and isinstance(attachments, list) and attachments:
            return {
                "route": "opus",
                "needs_latest": False,
                "needs_citations": False,
                "is_task": False,
                "is_continuation": False,
                "confidence": 1.0,
                "signals": ["non_text_inbound"],
            }

        conversation_context = self._build_conversation_context(
            session_id,
            fallback_context=fallback_context,
        )
        try:
            classification = await self.model_router.classify(
                query=content or "[empty message]",
                conversation_context=conversation_context,
            )
        except Exception as exc:  # pragma: no cover - depends on external service availability
            return {
                "route": "opus",
                "needs_latest": False,
                "needs_citations": False,
                "is_task": False,
                "is_continuation": False,
                "confidence": 0.0,
                "signals": ["router_unavailable"],
                "error": str(exc),
            }

        return {
            "route": self._safe_text(classification.get("route")) or "opus",
            "needs_latest": bool(classification.get("needs_latest")),
            "needs_citations": bool(classification.get("needs_citations")),
            "is_task": bool(classification.get("is_task")),
            "is_continuation": bool(classification.get("is_continuation")),
            "confidence": self._coerce_float(classification.get("confidence"), 0.0),
            "signals": classification.get("signals") if isinstance(classification.get("signals"), list) else [],
        }

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
        active_tasks = self._active_task_summaries(channel=channel, known_task_ids=known_task_ids or [])
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
        }

    def _active_task_summaries(self, *, channel: str, known_task_ids: list[str]) -> list[dict[str, Any]]:
        if not known_task_ids:
            return []

        active: list[dict[str, Any]] = []
        for task_id in known_task_ids:
            route = self._safe_text(self.active_task_channels.get(task_id))
            if route != channel:
                continue
            active.append({"task_id": task_id, "channel": channel})
        return active

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
