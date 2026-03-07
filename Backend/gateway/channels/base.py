from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

NormalizedMessage = dict[str, Any]
MessageCallback = Callable[[NormalizedMessage], Awaitable[Any]]


class ChannelAdapter(ABC):
    """Base class for Gateway channel adapters."""

    platform: str

    @abstractmethod
    async def start(self) -> None:
        """Initialize any external resources used by the adapter."""

    @abstractmethod
    async def stop(self) -> None:
        """Release external resources and stop graceful delivery."""

    @abstractmethod
    async def send(self, message: dict[str, Any], channel: str | None = None) -> None:
        """Deliver a Gateway event back to the originating channel."""

    @abstractmethod
    async def on_message(self, callback: MessageCallback) -> None:
        """Register the normalized inbound message handler."""

    def channel_id(self, platform_context: dict[str, Any]) -> str:
        return f"{self.platform}:{platform_context.get('id', 'default')}"

    def normalize_message(self, raw_message: Any) -> NormalizedMessage:
        raise NotImplementedError
