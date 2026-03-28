from __future__ import annotations

from .desktop import DesktopAdapter, DesktopConnection


class MobileAdapter(DesktopAdapter):
    """Gateway-side mobile transport using one WebSocket per installation."""

    platform = "mobile"

    async def register_connection(
        self,
        websocket,
        *,
        device_id: str,
        channel: str,
    ) -> None:
        if not channel.startswith("mobile:"):
            raise ValueError(f"Invalid mobile channel: {channel!r}")

        replaced: DesktopConnection | None = None
        async with self._lock:
            replaced = self._connections.get(channel)
            self._connections[channel] = DesktopConnection(
                channel=channel,
                device_id=device_id,
                websocket=websocket,
            )
            self._primary_channel = channel

        if replaced is not None and replaced.websocket is not websocket:
            try:
                await replaced.websocket.close(code=1012, reason="Replaced by a newer connection")
            except Exception:
                pass

    def normalize_message(self, raw_message):
        if not isinstance(raw_message, dict):
            raise TypeError("Mobile inbound payload must be a dict")

        message_type = self._coerce_str(raw_message.get("type")) or "query"
        if message_type != "query":
            raise ValueError(f"Mobile adapter only normalizes query messages, got: {message_type!r}")

        channel = self._coerce_str(raw_message.get("_channel")) or self._coerce_str(raw_message.get("channel"))
        if not channel or not channel.startswith("mobile:"):
            raise ValueError("Mobile inbound payload is missing a valid channel")

        attachments = raw_message.get("attachments")
        if not isinstance(attachments, list):
            attachments = []

        content = self._coerce_str(raw_message.get("content"))
        if not content and attachments:
            content = self._attachment_placeholder(attachments)
        if not content:
            raise ValueError("Mobile query is missing content")

        conversation_context = raw_message.get("conversation_context")
        if not isinstance(conversation_context, list):
            conversation_context = []

        request_id = self._coerce_str(raw_message.get("request_id"))
        route_override = self._coerce_str(raw_message.get("route_override"))
        device_id = channel.split(":", 1)[1]
        metadata = {
            "platform": self.platform,
            "message_type": message_type,
            "device_id": device_id,
            "request_id": request_id,
            "route_override": route_override,
            "conversation_context": conversation_context,
            "attachments": attachments,
        }

        return {
            "content": content,
            "session_id": self._coerce_str(raw_message.get("session_id")),
            "channel": channel,
            "request_id": request_id,
            "route_override": route_override,
            "conversation_context": conversation_context,
            "metadata": metadata,
        }
