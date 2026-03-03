"""Channel adapters exposed by the Gateway."""

from .base import ChannelAdapter, MessageCallback, NormalizedMessage
from .registry import ChannelAdapterRegistry
from .whatsapp import WhatsAppAdapter, WhatsAppConfig

__all__ = [
    "ChannelAdapter",
    "ChannelAdapterRegistry",
    "MessageCallback",
    "NormalizedMessage",
    "WhatsAppAdapter",
    "WhatsAppConfig",
]
