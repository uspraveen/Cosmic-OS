"""Channel adapters exposed by the Gateway."""

from .base import ChannelAdapter, MessageCallback, NormalizedMessage
from .desktop import DesktopAdapter
from .registry import ChannelAdapterRegistry
from .whatsapp import WhatsAppAdapter, WhatsAppConfig

__all__ = [
    "ChannelAdapter",
    "ChannelAdapterRegistry",
    "DesktopAdapter",
    "MessageCallback",
    "NormalizedMessage",
    "WhatsAppAdapter",
    "WhatsAppConfig",
]
