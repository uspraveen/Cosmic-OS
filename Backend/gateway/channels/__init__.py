"""Channel adapters exposed by the Gateway."""

from .agent_email import AgentEmailAdapter
from .base import ChannelAdapter, MessageCallback, NormalizedMessage
from .desktop import DesktopAdapter
from .registry import ChannelAdapterRegistry
from .telegram import TelegramAdapter, TelegramConfig
from .whatsapp import WhatsAppAdapter, WhatsAppConfig

__all__ = [
    "AgentEmailAdapter",
    "ChannelAdapter",
    "ChannelAdapterRegistry",
    "DesktopAdapter",
    "MessageCallback",
    "NormalizedMessage",
    "TelegramAdapter",
    "TelegramConfig",
    "WhatsAppAdapter",
    "WhatsAppConfig",
]
