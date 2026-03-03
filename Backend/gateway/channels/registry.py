from __future__ import annotations

from .base import ChannelAdapter


class ChannelAdapterRegistry:
    """Maps platform prefixes to registered adapters."""

    def __init__(self) -> None:
        self.adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.platform] = adapter

    def get_adapter(self, channel: str | None) -> ChannelAdapter | None:
        platform = channel.split(":", 1)[0] if channel else "desktop"
        return self.adapters.get(platform)

    async def start_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.start()

    async def stop_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()
