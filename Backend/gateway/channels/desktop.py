from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from .base import ChannelAdapter, MessageCallback, NormalizedMessage


@dataclass(slots=True)
class DesktopConnection:
    channel: str
    device_id: str
    websocket: WebSocket


class DesktopAdapter(ChannelAdapter):
    """Gateway-side desktop transport using one WebSocket per installation."""

    platform = "desktop"

    def __init__(self) -> None:
        self._callback: MessageCallback | None = None
        self._connections: dict[str, DesktopConnection] = {}
        self._primary_channel: str | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            self._primary_channel = None

        for connection in connections:
            try:
                await connection.websocket.close(code=1012, reason="Gateway shutting down")
            except Exception:
                continue

    async def on_message(self, callback: MessageCallback) -> None:
        self._callback = callback

    async def register_connection(
        self,
        websocket: WebSocket,
        *,
        device_id: str,
        channel: str,
    ) -> None:
        if not channel.startswith("desktop:"):
            raise ValueError(f"Invalid desktop channel: {channel!r}")

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

    async def unregister_connection(
        self,
        channel: str,
        websocket: WebSocket | None = None,
    ) -> None:
        async with self._lock:
            current = self._connections.get(channel)
            if current is None:
                return
            if websocket is not None and current.websocket is not websocket:
                return

            self._connections.pop(channel, None)
            if self._primary_channel == channel:
                self._primary_channel = next(iter(self._connections), None)

    async def send(self, message: dict[str, Any], channel: str | None = None) -> None:
        connection = await self._resolve_connection(channel)
        if connection is None:
            return

        try:
            await connection.websocket.send_json(message)
        except Exception:
            await self.unregister_connection(connection.channel, connection.websocket)

    async def get_status(self) -> dict[str, Any]:
        async with self._lock:
            channels = sorted(self._connections.keys())
            return {
                "status": "connected" if channels else "idle",
                "connection_count": len(channels),
                "channels": channels,
                "primary_channel": self._primary_channel,
            }

    async def handle_incoming_message(
        self,
        raw_message: dict[str, Any],
        *,
        channel: str,
    ) -> dict[str, Any]:
        payload = dict(raw_message)
        payload["_channel"] = channel
        normalized = self.normalize_message(payload)
        if self._callback is None:
            raise RuntimeError("DesktopAdapter.on_message() must be registered before handling inbound traffic")

        processed = await self._callback(normalized)
        if isinstance(processed, dict):
            return processed
        return normalized

    def normalize_message(self, raw_message: Any) -> NormalizedMessage:
        if not isinstance(raw_message, dict):
            raise TypeError("Desktop inbound payload must be a dict")

        message_type = self._coerce_str(raw_message.get("type")) or "query"
        if message_type != "query":
            raise ValueError(f"Desktop adapter only normalizes query messages, got: {message_type!r}")

        channel = self._coerce_str(raw_message.get("_channel")) or self._coerce_str(raw_message.get("channel"))
        if not channel or not channel.startswith("desktop:"):
            raise ValueError("Desktop inbound payload is missing a valid channel")

        content = self._coerce_str(raw_message.get("content"))
        if not content:
            raise ValueError("Desktop query is missing content")

        conversation_context = raw_message.get("conversation_context")
        if not isinstance(conversation_context, list):
            conversation_context = []

        request_id = self._coerce_str(raw_message.get("request_id"))
        device_id = channel.split(":", 1)[1]
        metadata = {
            "platform": self.platform,
            "message_type": message_type,
            "device_id": device_id,
            "request_id": request_id,
            "conversation_context": conversation_context,
        }

        return {
            "content": content,
            "session_id": self._coerce_str(raw_message.get("session_id")),
            "channel": channel,
            "request_id": request_id,
            "conversation_context": conversation_context,
            "metadata": metadata,
        }

    async def _resolve_connection(self, channel: str | None) -> DesktopConnection | None:
        async with self._lock:
            if channel and channel != self.platform:
                return self._connections.get(channel)
            if self._primary_channel:
                connection = self._connections.get(self._primary_channel)
                if connection is not None:
                    return connection
            return next(iter(self._connections.values()), None)

    def _coerce_str(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        if isinstance(value, (int, float)):
            return str(value)
        return None
