from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .channels.registry import ChannelAdapterRegistry
from .channels.whatsapp import WhatsAppAdapter, WhatsAppConfig
from .config import GatewayConfig
from .router_client import ModelRouterClient


@dataclass(slots=True)
class SessionHistoryEntry:
    role: str
    content: str
    route: str | None = None


class GatewayRuntime:
    """Single-process Gateway runtime for channel ingress and control-plane routes."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.registry = ChannelAdapterRegistry()
        self.model_router = ModelRouterClient(
            base_url=config.model_router_url,
            timeout_sec=config.model_router_timeout_sec,
        )
        self.started = False
        self.adapter_errors: dict[str, str] = {}
        self.active_task_channels: dict[str, str] = {}
        self.request_records: dict[str, dict[str, Any]] = {}
        self.session_history: dict[str, list[SessionHistoryEntry]] = defaultdict(list)
        self.awaiting_reply_routes: dict[str, str] = {}

    async def start(self) -> None:
        await self.model_router.start()
        await self._register_adapters()
        self.started = True

    async def stop(self) -> None:
        await self.registry.stop_all()
        await self.model_router.stop()
        self.started = False

    async def _register_adapters(self) -> None:
        if self.config.enable_whatsapp and "whatsapp" not in self.registry.adapters:
            adapter = WhatsAppAdapter(WhatsAppConfig.from_env())
            await adapter.on_message(self._handle_normalized_incoming_message)
            self.registry.register(adapter)

            try:
                await adapter.start()
                self.adapter_errors.pop(adapter.platform, None)
            except Exception as exc:  # pragma: no cover - startup health is environment-dependent
                self.adapter_errors[adapter.platform] = str(exc)

    async def _handle_normalized_incoming_message(self, message: dict[str, Any]) -> None:
        await self.process_incoming_user_message(message)

    async def process_incoming_user_message(self, message: dict[str, Any]) -> dict[str, Any]:
        content = str(message.get("content") or "").strip()
        channel = str(message.get("channel") or "").strip()
        metadata = message.get("metadata")
        if not channel:
            raise ValueError("Incoming message is missing channel")
        if not isinstance(metadata, dict):
            metadata = {}

        session_id = self._assign_session_id(channel)
        source_id = (
            self._safe_text(metadata.get("sender_jid"))
            or self._safe_text(metadata.get("chat_jid"))
            or channel
        )
        normalized_message = {
            **message,
            "session_id": session_id,
            "metadata": metadata,
        }

        classification = await self._classify_message(
            session_id=session_id,
            content=content,
            metadata=metadata,
        )
        request_id = uuid4().hex
        dispatch_target = "orchestrator" if classification["route"] == "opus" else "gateway"

        self._append_session_message(
            session_id,
            role="user",
            content=content or "[non-text inbound message]",
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
        self.active_task_channels[request_id] = channel
        self.request_records[request_id] = result
        return result

    async def _classify_message(
        self,
        *,
        session_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        sticky_route = self.awaiting_reply_routes.get(session_id)
        if sticky_route:
            return {
                "route": sticky_route,
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

        conversation_context = self._build_conversation_context(session_id)
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

    def _assign_session_id(self, channel: str) -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{day}:{channel}"

    def _build_conversation_context(self, session_id: str, limit: int = 10) -> list[dict[str, str]]:
        history = self.session_history.get(session_id, [])
        context: list[dict[str, str]] = []
        for item in history[-limit:]:
            entry: dict[str, str] = {
                "role": item.role,
                "content": item.content,
            }
            if item.route and item.role == "assistant":
                entry["route"] = item.route
            context.append(entry)
        return context

    def _append_session_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        route: str | None = None,
    ) -> None:
        if not content:
            return
        self.session_history[session_id].append(
            SessionHistoryEntry(role=role, content=content, route=route),
        )

    async def deliver_channel_event(self, event: dict[str, Any]) -> None:
        channel = self._safe_text(event.get("channel"))
        adapter = self.registry.get_adapter(channel)
        if adapter is None:
            raise ValueError(f"No adapter registered for channel: {channel!r}")
        await adapter.send(event)

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
        }

    async def readiness_payload(self) -> dict[str, Any]:
        healthy_channels = [item for item in self.list_channels() if item["healthy"]]
        return {
            "status": "ready" if self.started else "starting",
            "gateway_started": self.started,
            "healthy_channel_count": len(healthy_channels),
            "adapter_errors": self.adapter_errors,
        }

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
